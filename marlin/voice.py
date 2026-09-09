"""Fully local speech-to-text, wake word, and cancellable text-to-speech."""

from __future__ import annotations

import io
import math
import queue
import re
import tempfile
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from marlin.config import MarlinSettings
from marlin.events import EventBus
from second_brain.app.voice import (
    VoiceInputUnavailable,
    WakeWordListener,
    WindowsSpeech,
    VoskSTT,
    parse_wake_words,
    record_utterance,
)


_WHISPER_MODELS: dict[tuple[str, str, str], Any] = {}
_WHISPER_LOCK = threading.Lock()


class SpeechStream:
    """One cancellable reply, delivered to synthesis at sentence boundaries."""

    def __init__(self, cancel: threading.Event):
        self.cancel = cancel
        self.sentences: queue.Queue = queue.Queue()
        self.buffer = ""
        self.closed = False

    def feed(self, token: str) -> None:
        if self.cancel.is_set() or self.closed:
            return
        self.buffer += token
        while match := re.search(r"[.!?](?=\s)", self.buffer):
            sentence, self.buffer = self.buffer[:match.end()], self.buffer[match.end():]
            if sentence.strip():
                self.sentences.put(LocalVoiceService._spoken_text(sentence))

    def finish(self) -> None:
        if self.closed:
            return
        self.closed = True
        if not self.cancel.is_set() and self.buffer.strip():
            self.sentences.put(LocalVoiceService._spoken_text(self.buffer))
        self.buffer = ""
        self.sentences.put(None)


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

    def transcribe(self, wav_bytes: bytes, *, beam_size: int = 1) -> tuple[str, str, float]:
        if not wav_bytes:
            return "", "unknown", 0.0
        model = self._model()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            handle.write(wav_bytes)
            temp_path = Path(handle.name)
        try:
            segments, info = model.transcribe(
                str(temp_path),
                beam_size=beam_size,
                temperature=0.0,
                max_new_tokens=64,
                language="en",
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
            likelihood = math.exp(min(0.0, float(getattr(segment, "avg_logprob", -2))))
            repetition = float(getattr(segment, "compression_ratio", 0))
            score = likelihood * max(0.0, 1.0 - no_speech)
            probabilities.append(score * (0.25 if repetition > 2.4 else 1.0))
        return sum(probabilities) / len(probabilities)


class LocalVoiceService:
    def __init__(self, settings: MarlinSettings, events: EventBus):
        self.settings = settings
        self.events = events
        self.stt = FasterWhisperSTT(settings)
        self._fallback = WindowsSpeech()
        self._speech_thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._listen_cancel = threading.Event()
        self._listen_lock = threading.Lock()
        self._wake_pause = threading.Event()
        self._wake_epoch = 0
        self.wake_status = 'starting' if settings.wake_word_enabled else 'disabled'
        self._piper_voice: Any | None = None
        self._piper_lock = threading.Lock()
        self._barge_listener: WakeWordListener | None = None
        self._playback_lock = threading.RLock()
        self._output_stream: Any | None = None
        self.last_timings: dict[str, float] = {}

    def listen_once(self) -> dict[str, Any]:
        self.stop()
        self._wake_pause.set()
        if not self._listen_lock.acquire(timeout=1.5):
            self._wake_pause.clear()
            raise VoiceInputUnavailable("MARLIN is already listening.")
        self._listen_cancel = threading.Event()
        started = time.perf_counter()
        try:
            self.events.publish("voice.state", state="listening")
            handoff = getattr(self, '_handoff_wav', b'')
            self._handoff_wav = b''
            wav = handoff or record_utterance(
                max_seconds=6,
                silence_seconds=self.settings.voice_silence_seconds,
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
            if handoff:
                try:
                    wake_text = VoskSTT(str(self.settings.vosk_model_path)).transcribe(handoff)
                    if wake_text and not self._strip_wake_prefix(wake_text):
                        return {'text': '', 'wake_only': True}
                except (VoiceInputUnavailable, ValueError, wave.Error, EOFError):
                    pass
            self.events.publish("voice.state", state="transcribing")
            recorded = time.perf_counter()
            text, language, confidence = self.stt.transcribe(wav)
            if handoff:
                text = self._strip_wake_prefix(text)
                if not text:
                    return {'text': '', 'wake_only': True}
            if not self._listen_cancel.is_set() and not text:
                self.events.publish("voice.state", state="retrying")
                text, language, confidence = self.stt.transcribe(wav, beam_size=3)
                if handoff:
                    text = self._strip_wake_prefix(text)
            if self._listen_cancel.is_set():
                return {"text": "", "cancelled": True, "status": "cancelled", "requires_clarification": False}
            unclear = not text
            low_confidence = bool(text) and confidence < 0.55
            self.last_timings = {"recording_ms": round((recorded - started) * 1000),
                                 "transcription_ms": round((time.perf_counter() - recorded) * 1000)}
            self.events.publish("voice.timing", **self.last_timings)
            result = {"text": text, "language": language, "confidence": confidence,
                      "engine": "faster-whisper", "status": "clarification" if unclear else "recognized",
                      "requires_clarification": unclear, "low_confidence": low_confidence,
                      "timings": self.last_timings}
            if unclear:
                result["error"] = "Please correct the transcript or try speaking again. No command was executed."
                self.events.publish("voice.clarification", **result)
            return result
        finally:
            self.events.publish("voice.level", level=0.0)
            self.events.publish("voice.state", state="ready")
            self._listen_lock.release()
            self._wake_pause.clear()

    def wait_for_wake(self, listener: WakeWordListener, stop_event: threading.Event) -> bool:
        if self._speech_thread and self._speech_thread.is_alive():
            stop_event.wait(.1)
            return False
        if self._wake_pause.is_set() or stop_event.is_set():
            stop_event.wait(.05)
            return False
        if not self._listen_lock.acquire(timeout=0.1):
            return False
        try:
            owner = self
            epoch = self._wake_epoch
            class WakeCancellation:
                def is_set(self):
                    return (stop_event.is_set() or owner._wake_pause.is_set() or owner._wake_epoch != epoch
                            or bool(owner._speech_thread and owner._speech_thread.is_alive()))
            detected = listener.wait_for_wake(
                timeout=None,
                cancel_event=WakeCancellation(),
                device=self._microphone_device(),
                capture_command=True,
                on_detected=self.acknowledge_wake,
            )
            if detected and not WakeCancellation().is_set():
                self._handoff_wav = listener.handoff_wav
                return True
            return False
        finally:
            self._listen_lock.release()

    def wake_listener(self) -> WakeWordListener:
        return WakeWordListener(
            str(self.settings.vosk_model_path),
            parse_wake_words("hey marlin,hey marlon,hey merlin"),
        )

    def wait_for_barge_in(self, stop_event: threading.Event) -> bool:
        """Listen for Hey MARLIN during speech and preserve the next command."""
        speech = self._speech_thread
        if speech is None or not speech.is_alive() or stop_event.is_set():
            return False
        if not self._listen_lock.acquire(timeout=.1):
            return False
        self._wake_pause.set()
        detected = threading.Event()
        try:
            if self._barge_listener is None:
                self._barge_listener = WakeWordListener(
                    str(self.settings.vosk_model_path),
                    parse_wake_words("hey marlin,hey marlon,hey merlin"),
                )
            owner = self

            class BargeCancellation:
                def is_set(self):
                    return stop_event.is_set() or (not detected.is_set() and not speech.is_alive())

            def on_detected() -> None:
                detected.set()
                owner._stop_output_only()
                owner.acknowledge_wake()
                owner.events.publish("voice.barge_in")

            heard = self._barge_listener.wait_for_wake(
                timeout=None,
                cancel_event=BargeCancellation(),
                device=self._microphone_device(),
                capture_command=True,
                on_detected=on_detected,
            )
            if heard and detected.is_set() and not stop_event.is_set():
                self._handoff_wav = self._barge_listener.handoff_wav
                return True
            return False
        except VoiceInputUnavailable as exc:
            self.events.publish("voice.error", error=f"Barge-in microphone unavailable: {exc}")
            return False
        finally:
            self._wake_pause.clear()
            self._listen_lock.release()

    def _stop_output_only(self) -> None:
        self._cancel.set()
        self._fallback.stop()
        with self._playback_lock:
            if self._output_stream is not None:
                try:
                    self._output_stream.abort()
                except Exception:
                    pass

    def acknowledge_wake(self) -> None:
        self.events.publish('wake.acknowledged')
        # A short tone does not start TTS or cancel the microphone handoff.
        if self.settings.voice_output:
            def tone():
                try:
                    import winsound
                    winsound.Beep(880, 80)
                except (ImportError, RuntimeError):
                    pass
            threading.Thread(target=tone, daemon=True, name='marlin-wake-tone').start()

    @staticmethod
    def _strip_wake_prefix(text: str) -> str:
        return re.sub(r'^\s*(?:hey[\s,]+)?(?:marlin|marlon|merlin|marlene|marilyn|molly)\b[\s,.!?]*', '', text, flags=re.I).strip()

    @staticmethod
    def _clear_greeting(text: str, confidence: float) -> bool:
        # Only harmless greetings may pass this lower threshold; action targets may not.
        return confidence >= .4 and text.lower().strip().rstrip('.!?') in {
            'hello', 'hi', 'hey', 'hello there', 'good morning', 'good evening', 'good afternoon',
        }

    def speak(self, text: str) -> None:
        clean = self._spoken_text(text)[:1800]
        if not clean or not self.settings.voice_output:
            return
        self.stop()
        self._cancel = threading.Event()
        self._speech_thread = threading.Thread(target=self._speak_worker, args=(clean, self._cancel), daemon=True)
        self._speech_thread.start()

    def stop(self) -> None:
        self._wake_epoch += 1
        self._listen_cancel.set()
        self._cancel.set()
        self._fallback.stop()
        with self._playback_lock:
            if self._output_stream is not None:
                try:
                    self._output_stream.abort()
                except Exception:
                    pass
        try:
            import sounddevice as sd
            sd.stop()
        except (ImportError, OSError):
            pass
        self.events.publish("voice.state", state="ready")

    def begin_stream(self) -> "SpeechStream":
        self.stop()
        self._cancel = threading.Event()
        session = SpeechStream(self._cancel)
        if self.settings.voice_output:
            self._speech_thread = threading.Thread(target=self._speak_worker,
                args=("", self._cancel, session.sentences), daemon=True)
            self._speech_thread.start()
        return session

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
            "default_microphone_name": next((item["name"] for item in microphones if item["default"]), "System default"),
            "microphones": microphones,
            "listening": self._listen_lock.locked(),
            "model": self.settings.whisper_model,
            "model_loaded": self.stt.loaded,
            "model_loading": self.stt.loading,
            "wake_word": self.settings.wake_word_enabled,
            "wake_status": self.wake_status,
            "input_language": "en",
            "timings": self.last_timings,
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

    def _speak_worker(self, text: str, cancel: threading.Event, sentences: queue.Queue | None = None) -> None:
        self.events.publish("voice.state", state="preparing speech")
        chunks: queue.Queue = queue.Queue(maxsize=3)
        stream = None
        stream_rate = None
        played = False

        def utterances():
            if sentences is None:
                yield text
                return
            while not cancel.is_set():
                try:
                    sentence = sentences.get(timeout=.05)
                except queue.Empty:
                    continue
                if sentence is None:
                    return
                yield sentence

        def enqueue(value: Any) -> None:
            while not cancel.is_set():
                try:
                    chunks.put(value, timeout=0.05)
                    return
                except queue.Full:
                    pass

        def produce() -> None:
            try:
                with self._piper_lock:
                    if cancel.is_set():
                        return
                    for utterance in utterances():
                        if cancel.is_set():
                            return
                        generated = self._load_piper().synthesize(utterance)
                        for chunk in generated:
                            if cancel.is_set():
                                return
                            enqueue(self._playback_chunk(chunk))
            except Exception as exc:
                enqueue(exc)
            finally:
                enqueue(None)

        try:
            import sounddevice as sd
            threading.Thread(target=produce, daemon=True, name="marlin-synthesis").start()
            while not cancel.is_set():
                try:
                    chunk = chunks.get(timeout=0.05)
                except queue.Empty:
                    continue
                if chunk is None:
                    break
                if isinstance(chunk, Exception):
                    raise chunk
                if stream is not None and stream_rate != chunk.sample_rate:
                    stream.stop()
                    stream.close()
                    stream = None
                if stream is None:
                    with self._playback_lock:
                        if cancel.is_set():
                            return
                        stream = sd.RawOutputStream(samplerate=chunk.sample_rate, channels=chunk.sample_channels, dtype="int16")
                        stream_rate = chunk.sample_rate
                        self._output_stream = stream
                        stream.start()
                    self.events.publish("voice.state", state="speaking")
                audio = chunk.audio_int16_bytes
                block = max(2, int(chunk.sample_rate * .04) * chunk.sample_channels * 2)
                for offset in range(0, len(audio), block):
                    if cancel.is_set():
                        break
                    stream.write(audio[offset:offset + block])
                    played = True
        except Exception as exc:
            if not cancel.is_set():
                if not played and sentences is None:
                    with self._playback_lock:
                        if not cancel.is_set():
                            self.events.publish("voice.state", state="speaking")
                            self._fallback.speak(text)
                    self._fallback.wait()
                else:
                    self.events.publish("voice.error", error=f"Speech unavailable or interrupted: {exc}. For missing models, run py main.py setup.")
        finally:
            was_cancelled = cancel.is_set()
            # Release a producer waiting on a full queue if the audio device failed.
            cancel.set()
            if stream is not None:
                try:
                    if not was_cancelled:
                        stream.stop()
                    stream.close()
                except Exception as exc:
                    self.events.publish("voice.error", error=f"Could not close audio output: {exc}")
                with self._playback_lock:
                    if self._output_stream is stream:
                        self._output_stream = None
            if not was_cancelled:
                self.events.publish("voice.state", state="ready")

    def preload_speech(self) -> None:
        def load() -> None:
            try:
                with self._piper_lock:
                    self._load_piper()
            except Exception:
                pass
        threading.Thread(target=load, daemon=True, name="marlin-piper-loader").start()

    @staticmethod
    def _playback_chunk(chunk: Any) -> Any:
        if chunk.sample_rate == 22050 and chunk.sample_channels == 1:
            return chunk
        import av
        import numpy as np
        samples = np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono" if chunk.sample_channels == 1 else "stereo")
        frame.sample_rate = chunk.sample_rate
        resampler = av.AudioResampler(format="s16", layout="mono", rate=22050)
        frames = resampler.resample(frame) + resampler.resample(None)
        return SimpleNamespace(sample_rate=22050, sample_channels=1,
                               audio_int16_bytes=b"".join(item.to_ndarray().tobytes() for item in frames))

    @staticmethod
    def _spoken_text(text: str) -> str:
        text = re.sub(r"```[\s\S]*?```", " Code is shown on screen. ", str(text or ""))
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"[*_`#]", "", text)
        return " ".join(text.split())

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
        parts = [part.strip() for part in re.split(r"(?<=[.!?])\s*", text) if part.strip()]
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
    def _module_available(name: str) -> bool:
        try:
            __import__(name)
            return True
        except (ImportError, OSError):
            return False
