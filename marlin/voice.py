"""Fully local speech-to-text, wake word, and cancellable text-to-speech."""

from __future__ import annotations

import io
import re
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any, Callable

from marlin.config import MarlinSettings
from marlin.events import EventBus
from second_brain.app.voice import (
    VoiceInputUnavailable,
    VoskSTT,
    WakeWordListener,
    WindowsSpeech,
    parse_wake_words,
    record_utterance,
)


_WHISPER_MODELS: dict[tuple[str, str, str], Any] = {}
_WHISPER_LOCK = threading.Lock()


class FasterWhisperSTT:
    def __init__(self, settings: MarlinSettings):
        self.settings = settings
        self.loading = False
        self.loaded = False

    def preload_async(self) -> None:
        if self.loading or self.loaded:
            return
        threading.Thread(target=self._preload, name="marlin-whisper-loader", daemon=True).start()

    def _preload(self) -> None:
        self.loading = True
        try:
            self._model()
            self.loaded = True
        except Exception:
            pass
        finally:
            self.loading = False

    def _model(self) -> Any:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise VoiceInputUnavailable(
                "Faster-Whisper is not installed. Run `py main.py setup`."
            ) from exc
        key = (self.settings.whisper_model, self.settings.whisper_device, self.settings.whisper_compute_type)
        with _WHISPER_LOCK:
            if key not in _WHISPER_MODELS:
                _WHISPER_MODELS[key] = WhisperModel(
                    self.settings.whisper_model,
                    device=self.settings.whisper_device,
                    compute_type=self.settings.whisper_compute_type,
                    local_files_only=True,
                )
        self.loaded = True
        return _WHISPER_MODELS[key]

    def transcribe(self, wav_bytes: bytes) -> tuple[str, str, float]:
        if not wav_bytes:
            return "", "unknown", 0.0
        model = self._model()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            handle.write(wav_bytes)
            temp_path = Path(handle.name)
        try:
            segments, info = model.transcribe(
                str(temp_path),
                beam_size=1,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                condition_on_previous_text=False,
            )
            values = list(segments)
            confidence = self._confidence(values)
            text = " ".join(str(segment.text).strip() for segment in values if str(segment.text).strip())
            return text.strip(), str(getattr(info, "language", "unknown")), confidence
        finally:
            temp_path.unlink(missing_ok=True)

    @staticmethod
    def _confidence(segments: list[Any]) -> float:
        if not segments:
            return 0.0
        probabilities = []
        for segment in segments:
            no_speech = float(getattr(segment, "no_speech_prob", 0.5))
            probabilities.append(max(0.0, min(1.0, 1.0 - no_speech)))
        return sum(probabilities) / len(probabilities)


class LocalVoiceService:
    def __init__(self, settings: MarlinSettings, events: EventBus):
        self.settings = settings
        self.events = events
        self.stt = FasterWhisperSTT(settings)
        self.fast_stt = VoskSTT(str(settings.vosk_model_path))
        self._fallback = WindowsSpeech()
        self._speech_thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._listen_cancel = threading.Event()
        self._listen_lock = threading.Lock()
        self._wake_pause = threading.Event()
        self._piper_voice: Any | None = None

    def listen_once(self) -> dict[str, Any]:
        self._wake_pause.set()
        if not self._listen_lock.acquire(timeout=1.5):
            self._wake_pause.clear()
            raise VoiceInputUnavailable("MARLIN is already listening.")
        self._listen_cancel.clear()
        try:
            self.events.publish("voice.state", state="listening")
            wav = record_utterance(
                max_seconds=8,
                silence_seconds=0.55,
                start_timeout=5,
                vosk_model_path=str(self.settings.vosk_model_path),
                on_state=lambda state: self.events.publish("voice.state", state=state),
                on_level=lambda level: self.events.publish("voice.level", level=round(level, 3)),
                cancel_event=self._listen_cancel,
                device=self._microphone_device(),
            )
            if self._listen_cancel.is_set():
                return {"text": "", "language": "unknown", "confidence": 0.0, "cancelled": True}
            if not wav:
                return {"text": "", "language": "unknown", "confidence": 0.0, "error": "I did not hear speech. Check the selected microphone and try again."}
            try:
                quick_text = self.fast_stt.transcribe(wav)
            except VoiceInputUnavailable:
                quick_text = ""
            if self._looks_like_command(quick_text):
                return {"text": quick_text, "language": "en", "confidence": 0.85, "engine": "vosk-fast"}
            self.events.publish("voice.state", state="transcribing")
            text, language, confidence = self.stt.transcribe(wav)
            if not text:
                return {"text": "", "language": language, "confidence": confidence, "error": "I heard audio but could not understand it. Please try again."}
            return {"text": text, "language": language, "confidence": confidence, "engine": "faster-whisper"}
        finally:
            self.events.publish("voice.level", level=0.0)
            self.events.publish("voice.state", state="ready")
            self._listen_lock.release()
            self._wake_pause.clear()

    def wait_for_wake(self, listener: WakeWordListener, stop_event: threading.Event) -> bool:
        if self._wake_pause.is_set() or stop_event.is_set():
            return False
        if not self._listen_lock.acquire(timeout=0.1):
            return False
        try:
            return listener.wait_for_wake(
                timeout=0.75,
                cancel_event=stop_event,
                device=self._microphone_device(),
            )
        finally:
            self._listen_lock.release()

    def wake_listener(self) -> WakeWordListener:
        return WakeWordListener(
            str(self.settings.vosk_model_path),
            parse_wake_words("marlin,hey marlin,hey marlon,hey merlin"),
        )

    def speak(self, text: str) -> None:
        clean = " ".join(str(text or "").split())[:1800]
        if not clean or self._contains_burmese(clean) or not self.settings.voice_output:
            return
        self.stop()
        self._cancel.clear()
        self._speech_thread = threading.Thread(target=self._speak_worker, args=(clean,), daemon=True)
        self._speech_thread.start()

    def stop(self) -> None:
        self._listen_cancel.set()
        self._cancel.set()
        self._fallback.stop()
        try:
            import sounddevice as sd
            sd.stop()
        except (ImportError, OSError):
            pass
        self.events.publish("voice.state", state="ready")

    def wait(self, timeout: float | None = None) -> None:
        thread = self._speech_thread
        if thread and thread.is_alive():
            thread.join(timeout)

    def status(self) -> dict[str, Any]:
        microphones = self.microphones()
        return {
            "stt": "faster-whisper" if self._module_available("faster_whisper") else "missing",
            "tts": "piper" if self._piper_model_path().exists() and self._module_available("piper") else "windows-fallback",
            "voice": self.settings.piper_voice,
            "vosk": self.settings.vosk_model_path.exists(),
            "microphone": self.settings.microphone_device or "system default",
            "microphones": microphones,
            "listening": self._listen_lock.locked(),
            "model": self.settings.whisper_model,
            "model_loaded": self.stt.loaded,
            "model_loading": self.stt.loading,
            "wake_word": self.settings.wake_word_enabled,
        }

    def microphones(self) -> list[dict[str, Any]]:
        try:
            import sounddevice as sd
            default_input = int(sd.default.device[0])
            return [
                {"id": index, "name": str(item["name"]), "default": index == default_input}
                for index, item in enumerate(sd.query_devices())
                if int(item["max_input_channels"]) > 0
            ]
        except (ImportError, OSError, ValueError):
            return []

    def _microphone_device(self) -> int | str | None:
        value = self.settings.microphone_device.strip()
        if not value:
            return None
        return int(value) if value.isdigit() else value

    @staticmethod
    def _looks_like_command(text: str) -> bool:
        command = " ".join(str(text or "").lower().split())
        prefixes = (
            "open ", "close ", "search ", "find ", "show ", "graph ", "index ",
            "play ", "pause ", "stop ", "next ", "previous ", "volume ", "mute",
            "high priority", "why high priority", "marlin wake", "marlin stand",
            "wake up", "stand by", "remind me", "set alarm", "snooze",
        )
        return bool(command) and command.startswith(prefixes)

    def _speak_worker(self, text: str) -> None:
        self.events.publish("voice.state", state="speaking")
        try:
            if not self._piper_model_path().exists() or not self._module_available("piper"):
                self._fallback.speak(text)
                return
            voice = self._load_piper()
            for sentence in self._sentences(text):
                if self._cancel.is_set():
                    break
                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as wav_file:
                    voice.synthesize_wav(sentence, wav_file)
                buffer.seek(0)
                with wave.open(buffer, "rb") as wav_file:
                    frames = wav_file.readframes(wav_file.getnframes())
                    rate = wav_file.getframerate()
                    channels = wav_file.getnchannels()
                import numpy as np
                import sounddevice as sd
                audio = np.frombuffer(frames, dtype=np.int16)
                if channels > 1:
                    audio = audio.reshape(-1, channels)
                sd.play(audio, rate, blocking=True)
        except Exception:
            if not self._cancel.is_set():
                self._fallback.speak(text)
        finally:
            if not self._cancel.is_set():
                self.events.publish("voice.state", state="ready")

    def _load_piper(self) -> Any:
        if self._piper_voice is None:
            try:
                from piper.voice import PiperVoice
            except ImportError:
                from piper import PiperVoice
            self._piper_voice = PiperVoice.load(str(self._piper_model_path()))
        return self._piper_voice

    def _piper_model_path(self) -> Path:
        return self.settings.piper_data_dir / f"{self.settings.piper_voice}.onnx"

    @staticmethod
    def _sentences(text: str) -> list[str]:
        parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+|(?<=,)\s+", text) if part.strip()]
        chunks: list[str] = []
        for part in parts:
            while len(part) > 180:
                split_at = part.rfind(" ", 0, 180)
                split_at = split_at if split_at > 60 else 180
                chunks.append(part[:split_at].strip())
                part = part[split_at:].strip()
            if part:
                chunks.append(part)
        return chunks

    @staticmethod
    def _contains_burmese(text: str) -> bool:
        return any("\u1000" <= char <= "\u109f" for char in text)

    @staticmethod
    def _module_available(name: str) -> bool:
        try:
            __import__(name)
            return True
        except (ImportError, OSError):
            return False
