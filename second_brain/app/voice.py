"""Local voice input and output for MARLIN.

Voice input has two stages so the always-on wake word stays offline:

1. ``WakeWordListener`` runs a small local Vosk model with a restricted
   grammar, so nothing leaves the machine until MARLIN is addressed.
2. ``transcribe_once`` records the actual command and sends it to Groq
   ``whisper-large-v3-turbo``, the fastest accurate multilingual option, with
   the same local Vosk model as an offline fallback.

Streaming everything to the cloud instead would exhaust the Groq free-tier
request budget within minutes.
"""

from __future__ import annotations

import array
import io
import json
import math
import os
import queue
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
from xml.sax.saxutils import escape

import certifi

SAMPLE_RATE = 16000
BLOCK_FRAMES = 1600  # 100 ms at 16 kHz
WARMUP_BLOCKS = 8
MIN_SPEECH_RMS = 600.0
RELEASE_MULTIPLIER = 1.4
PRE_ROLL_BLOCKS = 4
BACKGROUND_WINDOW = 40
BACKGROUND_READY_BLOCKS = 10

# Loudness is only a backstop for speech the acoustic model cannot decode, so
# the bar is deliberately high. A laptop array microphone with automatic gain
# swings several times above its own noise floor without anyone speaking.
STRONG_ONSET_MULTIPLIER = 4.0
STRONG_ONSET_BLOCKS = 4

_MODEL_CACHE: dict[str, Any] = {}


class VoiceInputUnavailable(RuntimeError):
    """Raised when local voice input dependencies are unavailable."""


@dataclass
class WindowsSpeech:
    """Small Windows speech process manager."""

    process: subprocess.Popen | None = None

    def speak(self, text: str) -> None:
        if sys.platform != "win32":
            return
        clean_text = " ".join(text.split())[:1800]
        if not clean_text:
            return
        self.stop()
        command = (
            "$ssml = [Console]::In.ReadToEnd(); "
            "Add-Type -AssemblyName System.Speech; "
            "$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$voices = $speaker.GetInstalledVoices() | Where-Object { $_.Enabled }; "
            "$preferred = @('Microsoft Ryan','Microsoft George','Microsoft David','Microsoft Sonia','Microsoft Libby','Microsoft Zira'); "
            "foreach ($name in $preferred) { "
            "$match = $voices | Where-Object { $_.VoiceInfo.Name -like \"*$name*\" } | Select-Object -First 1; "
            "if ($match) { $speaker.SelectVoice($match.VoiceInfo.Name); break } "
            "} "
            "$speaker.Rate = -1; "
            "$speaker.Volume = 92; "
            "$speaker.SpeakSsml($ssml)"
        )
        startupinfo = None
        creationflags = 0
        if hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW
        self.process = subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", command],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        if self.process.stdin:
            self.process.stdin.write(_speech_ssml(clean_text))
            self.process.stdin.close()

    def wait(self, timeout: float = 60.0) -> None:
        """Block until speaking finishes.

        The hands-free loop waits here before reopening the microphone so
        MARLIN does not hear its own voice and retrigger the wake word.
        """

        if self.process is None:
            return
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop()

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self.process = None


def _speech_ssml(text: str) -> str:
    escaped = escape(text)
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">'
        '<prosody rate="-5%" pitch="-2%">'
        f"{escaped}"
        "</prosody>"
        "</speak>"
    )


def _import_sounddevice() -> Any:
    try:
        import sounddevice as sd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise VoiceInputUnavailable(
            "Microphone capture needs the `sounddevice` package. "
            "Run: pip install -r requirements.txt"
        ) from exc
    except OSError as exc:
        raise VoiceInputUnavailable(f"Audio backend could not start: {exc}") from exc
    return sd


def _import_vosk() -> Any:
    try:
        import vosk  # type: ignore[import-not-found]
    except ImportError as exc:
        raise VoiceInputUnavailable(
            "Offline voice needs the `vosk` package. Run: pip install -r requirements.txt"
        ) from exc
    vosk.SetLogLevel(-1)
    return vosk


def load_vosk_model(model_path: str) -> Any:
    """Load and cache a Vosk model.

    Cached because the model takes seconds to load and the wake-word listener
    reopens a recognizer on every cycle.
    """

    if not model_path:
        raise VoiceInputUnavailable(
            "No Vosk model configured. Run: python scripts/setup_voice.py"
        )
    path = Path(model_path).expanduser()
    if not path.is_absolute():
        path = (Path(__file__).resolve().parents[2] / path).resolve()
    if not path.exists():
        raise VoiceInputUnavailable(
            f"Vosk model folder not found: {path}. Run: python scripts/setup_voice.py"
        )

    key = str(path)
    if key not in _MODEL_CACHE:
        vosk = _import_vosk()
        _MODEL_CACHE[key] = vosk.Model(str(path))
    return _MODEL_CACHE[key]


def _rms(block: bytes) -> float:
    if not block:
        return 0.0
    usable = len(block) - (len(block) % 2)
    if usable <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(block[:usable])
    if not samples:
        return 0.0
    total = 0
    for value in samples:
        total += value * value
    return math.sqrt(total / len(samples))


def _multipart_form(
    fields: dict[str, str],
    file_field: str,
    filename: str,
    file_bytes: bytes,
    content_type: str = "audio/wav",
) -> tuple[bytes, str]:
    """Build a multipart/form-data body for the transcriptions endpoint."""

    boundary = f"----MarlinBoundary{uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _pcm_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


class _BackgroundLevel:
    """Rolling estimate of the room's noise floor.

    Laptop array microphones with automatic gain can sit anywhere from an RMS
    of 30 to several thousand, so a fixed threshold cannot work. The median of
    recent quiet blocks tracks the room instead.
    """

    def __init__(self) -> None:
        self._levels: list[float] = []

    def observe(self, level: float) -> None:
        self._levels.append(level)
        if len(self._levels) > BACKGROUND_WINDOW:
            self._levels.pop(0)

    @property
    def floor(self) -> float:
        if not self._levels:
            return MIN_SPEECH_RMS
        ordered = sorted(self._levels)
        return ordered[len(ordered) // 2]

    @property
    def ready(self) -> bool:
        return len(self._levels) >= BACKGROUND_READY_BLOCKS

    @property
    def strong_onset(self) -> float:
        return max(MIN_SPEECH_RMS, self.floor * STRONG_ONSET_MULTIPLIER)

    @property
    def release(self) -> float:
        return max(MIN_SPEECH_RMS * 0.6, self.floor * RELEASE_MULTIPLIER)


def record_utterance(
    *,
    max_seconds: float = 12.0,
    silence_seconds: float = 0.8,
    start_timeout: float = 8.0,
    vosk_model_path: str = "",
    on_state: Callable[[str], None] | None = None,
    on_level: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
    device: int | str | None = None,
) -> bytes:
    """Record one spoken phrase and return WAV bytes.

    Endpointing is driven by the local Vosk acoustic model where available,
    because energy alone is unreliable in a noisy room. Loudness is kept as a
    parallel signal so speech in a language the small model cannot decode
    still registers. Returns empty bytes when nothing was said.
    """

    sd = _import_sounddevice()
    recognizer = _optional_recognizer(vosk_model_path)

    audio_queue: queue.Queue[bytes] = queue.Queue()

    def callback(indata, _frames, _time_info, _status) -> None:  # noqa: ANN001
        audio_queue.put(bytes(indata))

    def notify(state: str) -> None:
        if on_state is not None:
            on_state(state)

    collected: list[bytes] = []
    pre_roll: list[bytes] = []
    background = _BackgroundLevel()
    speaking = False
    loud_streak = 0
    quiet_for = 0.0
    warmed = 0
    block_seconds = BLOCK_FRAMES / SAMPLE_RATE

    try:
        stream = sd.RawInputStream(
            device=device,
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_FRAMES,
            dtype="int16",
            channels=1,
            callback=callback,
        )
    except Exception as exc:  # sounddevice raises PortAudioError subclasses
        raise VoiceInputUnavailable(f"Could not open the microphone: {exc}") from exc

    with stream:
        notify("listening")
        start_deadline = time.monotonic() + start_timeout
        hard_deadline = start_deadline + max_seconds

        while time.monotonic() < hard_deadline:
            if cancel_event is not None and cancel_event.is_set():
                notify("cancelled")
                return b""
            try:
                chunk = audio_queue.get(timeout=0.25)
            except queue.Empty:
                continue

            # The first blocks after opening a stream are often near-silent and
            # would drag the noise floor estimate far too low.
            if warmed < WARMUP_BLOCKS:
                warmed += 1
                continue

            level = _rms(chunk)
            if on_level is not None:
                on_level(min(1.0, level / 12000.0))
            model_speech, model_done = _recognizer_signals(recognizer, chunk)

            if not speaking:
                pre_roll.append(chunk)
                if len(pre_roll) > PRE_ROLL_BLOCKS:
                    pre_roll.pop(0)

                loud_streak = loud_streak + 1 if level >= background.strong_onset else 0
                loud_enough = (
                    background.ready and loud_streak >= STRONG_ONSET_BLOCKS
                )
                if model_speech or loud_enough:
                    speaking = True
                    notify("recording")
                    collected.extend(pre_roll)
                    continue

                background.observe(level)
                if time.monotonic() > start_deadline:
                    return b""
                continue

            collected.append(chunk)
            if model_done:
                break
            if level < background.release:
                quiet_for += block_seconds
                if quiet_for >= silence_seconds:
                    break
            else:
                quiet_for = 0.0

    if not collected:
        return b""
    notify("thinking")
    return _pcm_to_wav(b"".join(collected))


def _optional_recognizer(model_path: str) -> Any | None:
    """Build a Vosk recognizer for endpointing, or None if unavailable."""

    if not model_path:
        return None
    try:
        vosk = _import_vosk()
        return vosk.KaldiRecognizer(load_vosk_model(model_path), SAMPLE_RATE)
    except VoiceInputUnavailable:
        return None


def _recognizer_signals(recognizer: Any | None, chunk: bytes) -> tuple[bool, bool]:
    """Return (speech detected, utterance finished) from the acoustic model."""

    if recognizer is None:
        return False, False
    if recognizer.AcceptWaveform(chunk):
        text = str(json.loads(recognizer.Result()).get("text", "")).strip()
        return bool(text), bool(text)
    partial = str(json.loads(recognizer.PartialResult()).get("partial", "")).strip()
    return bool(partial), False


class GroqSTT:
    """Groq speech-to-text over the OpenAI-compatible transcriptions endpoint."""

    def __init__(
        self,
        api_key: str,
        model: str = "whisper-large-v3-turbo",
        *,
        base_url: str = "https://api.groq.com/openai/v1",
        language: str = "",
        timeout_seconds: float = 30.0,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.language = language.strip()
        self.timeout_seconds = timeout_seconds
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    @classmethod
    def from_settings(cls, settings: Any) -> "GroqSTT":
        api_key = os.getenv("GROQ_API_KEY", "")
        if not api_key:
            raise VoiceInputUnavailable("GROQ_API_KEY is not configured for speech-to-text.")
        return cls(
            api_key,
            getattr(settings, "groq_stt_model", "whisper-large-v3-turbo"),
            base_url=getattr(settings, "groq_base_url", "https://api.groq.com/openai/v1"),
            language=getattr(settings, "stt_language", "") or "",
        )

    def transcribe(self, wav_bytes: bytes) -> str:
        if not wav_bytes:
            return ""

        fields = {"model": self.model, "response_format": "json", "temperature": "0"}
        if self.language:
            fields["language"] = self.language
        body, content_type = _multipart_form(fields, "file", "command.wav", wav_bytes)

        request = Request(
            f"{self.base_url}/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": content_type,
                "User-Agent": "MARLIN-BrainOS/0.1",
            },
            method="POST",
        )
        try:
            with urlopen(
                request,
                timeout=self.timeout_seconds,
                context=self.ssl_context,
            ) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:300]
            raise VoiceInputUnavailable(f"Groq speech-to-text returned HTTP {exc.code}: {detail}") from exc
        except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            if sys.platform == "win32":
                data = self._transcribe_with_curl(wav_bytes, fields, exc)
            else:
                raise VoiceInputUnavailable(f"Could not reach Groq speech-to-text: {exc}") from exc

        text = data.get("text") if isinstance(data, dict) else None
        return str(text or "").strip()

    def _transcribe_with_curl(
        self,
        wav_bytes: bytes,
        fields: dict[str, str],
        original_error: BaseException,
    ) -> dict[str, Any]:
        """Fall back to bundled curl.exe when Windows blocks the Python TLS stack.

        The chat client already needs an equivalent PowerShell fallback, so the
        same TLS interference affects audio uploads.
        """

        temp_path = Path(tempfile.gettempdir()) / f"marlin-stt-{uuid4().hex}.wav"
        try:
            temp_path.write_bytes(wav_bytes)
            command = [
                "curl",
                "--silent",
                "--show-error",
                "--max-time",
                str(int(self.timeout_seconds)),
                "-X",
                "POST",
                f"{self.base_url}/audio/transcriptions",
                "-H",
                f"Authorization: Bearer {self.api_key}",
                "-F",
                f"file=@{temp_path}",
            ]
            for name, value in fields.items():
                command.extend(["-F", f"{name}={value}"])
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds + 10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VoiceInputUnavailable(
                f"Could not reach Groq speech-to-text: {original_error}"
            ) from exc
        finally:
            temp_path.unlink(missing_ok=True)

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:300]
            raise VoiceInputUnavailable(f"Groq speech-to-text failed: {detail}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise VoiceInputUnavailable("Groq speech-to-text returned an unreadable response.") from exc
        return payload if isinstance(payload, dict) else {}


class VoskSTT:
    """Offline transcription with the same local model as the wake word."""

    def __init__(self, model_path: str):
        self.model_path = model_path

    def transcribe(self, wav_bytes: bytes) -> str:
        if not wav_bytes:
            return ""
        vosk = _import_vosk()
        model = load_vosk_model(self.model_path)
        recognizer = vosk.KaldiRecognizer(model, SAMPLE_RATE)

        with wave.open(io.BytesIO(wav_bytes), "rb") as handle:
            pcm = handle.readframes(handle.getnframes())

        pieces: list[str] = []
        step = BLOCK_FRAMES * 2
        for offset in range(0, len(pcm), step):
            chunk = pcm[offset : offset + step]
            if recognizer.AcceptWaveform(chunk):
                pieces.append(str(json.loads(recognizer.Result()).get("text", "")).strip())
        pieces.append(str(json.loads(recognizer.FinalResult()).get("text", "")).strip())
        return " ".join(piece for piece in pieces if piece).strip()


class WakeWordListener:
    """Offline always-on wake word using a grammar-restricted Vosk recognizer."""

    def __init__(self, model_path: str, phrases: Iterable[str]):
        self.model_path = model_path
        self.phrases = [
            " ".join(str(phrase).lower().split())
            for phrase in phrases
            if str(phrase).strip()
        ] or ["marlin"]
        self._recognizer: Any | None = None

    def _build_recognizer(self) -> Any:
        vosk = _import_vosk()
        model = load_vosk_model(self.model_path)
        # A restricted grammar turns the recognizer into a keyword spotter: it
        # can only emit one of these phrases or the unknown token.
        grammar = json.dumps(self.phrases + ["[unk]"])
        return vosk.KaldiRecognizer(model, SAMPLE_RATE, grammar)

    def prepare(self) -> None:
        """Load the model up front so setup problems surface early."""

        if self._recognizer is None:
            self._recognizer = self._build_recognizer()

    def matches(self, text: str) -> bool:
        normalized = " ".join(str(text or "").lower().split())
        if not normalized:
            return False
        return any(phrase in normalized for phrase in self.phrases)

    def wait_for_wake(
        self,
        *,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
        device: int | str | None = None,
    ) -> bool:
        """Block until a wake phrase is heard. Returns False on timeout."""

        sd = _import_sounddevice()
        if self._recognizer is None:
            self._recognizer = self._build_recognizer()
        recognizer = self._recognizer
        recognizer.Reset()

        audio_queue: queue.Queue[bytes] = queue.Queue()

        def callback(indata, _frames, _time_info, _status) -> None:  # noqa: ANN001
            audio_queue.put(bytes(indata))

        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            stream = sd.RawInputStream(
                device=device,
                samplerate=SAMPLE_RATE,
                blocksize=4000,
                dtype="int16",
                channels=1,
                callback=callback,
            )
        except Exception as exc:
            raise VoiceInputUnavailable(f"Could not open the microphone: {exc}") from exc

        with stream:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return False
                if deadline is not None and time.monotonic() > deadline:
                    return False
                try:
                    chunk = audio_queue.get(timeout=0.25)
                except queue.Empty:
                    continue

                if recognizer.AcceptWaveform(chunk):
                    if self.matches(json.loads(recognizer.Result()).get("text", "")):
                        recognizer.Reset()
                        return True
                elif self.matches(json.loads(recognizer.PartialResult()).get("partial", "")):
                    recognizer.Reset()
                    return True


def resolve_vosk_model_path(settings: Any) -> str:
    """Prefer the explicit Vosk path, falling back to the legacy setting."""

    return (
        getattr(settings, "vosk_model_path", "")
        or getattr(settings, "terminal_voice_input_model_path", "")
        or ""
    )


def parse_wake_words(value: str) -> list[str]:
    phrases = [" ".join(item.lower().split()) for item in str(value or "").split(",")]
    return [phrase for phrase in phrases if phrase]


def transcribe_once(
    settings: Any,
    *,
    on_state: Callable[[str], None] | None = None,
) -> str:
    """Record one command and transcribe it with the configured provider."""

    model_path = resolve_vosk_model_path(settings)
    wav_bytes = record_utterance(
        max_seconds=float(getattr(settings, "voice_max_seconds", 12.0)),
        silence_seconds=float(getattr(settings, "voice_silence_seconds", 0.8)),
        start_timeout=float(getattr(settings, "voice_start_timeout", 8.0)),
        vosk_model_path=model_path,
        on_state=on_state,
    )
    if not wav_bytes:
        return ""

    provider = str(getattr(settings, "stt_provider", "groq")).lower().strip()

    if provider == "groq":
        try:
            return GroqSTT.from_settings(settings).transcribe(wav_bytes)
        except VoiceInputUnavailable:
            if not model_path:
                raise
            return VoskSTT(model_path).transcribe(wav_bytes)

    return VoskSTT(model_path).transcribe(wav_bytes)
