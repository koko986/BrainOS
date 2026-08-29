"""Text-to-speech providers for MARLIN."""

from __future__ import annotations

import base64
import io
import json
import os
import wave
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TTSUnavailable(RuntimeError):
    """Raised when the configured TTS provider cannot synthesize speech."""


@dataclass(frozen=True)
class SpeechAudio:
    mime_type: str
    data_base64: str


class GeminiTTSClient:
    """Gemini text-to-speech client using the public REST API."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        model: str,
        voice: str,
        timeout_seconds: float = 60.0,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.voice = voice
        self.timeout_seconds = timeout_seconds

    def synthesize(self, text: str) -> SpeechAudio:
        if not self.api_key:
            raise TTSUnavailable("GEMINI_API_KEY is not configured.")

        prompt = (
            "Read this as MARLIN, a calm, mature male personal assistant. "
            "Use natural human pacing, subtle confidence, a composed British tone, "
            "and a warm but professional delivery. "
            "Do not add words. Text:\n"
            f"{text[:1800]}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {
                            "voiceName": self.voice,
                        }
                    }
                },
            },
            "model": self.model,
        }
        request = Request(
            f"{self.base_url}/models/{self.model}:generateContent",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")[:500]
            raise TTSUnavailable(f"Gemini TTS returned HTTP {exc.code}: {body}") from exc
        except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise TTSUnavailable("Could not reach Gemini TTS.") from exc

        inline_data = _extract_inline_audio(data)
        pcm_bytes = base64.b64decode(inline_data)
        wav_bytes = _pcm_to_wav(pcm_bytes)
        return SpeechAudio(
            mime_type="audio/wav",
            data_base64=base64.b64encode(wav_bytes).decode("ascii"),
        )


def create_tts_client(settings) -> GeminiTTSClient:
    provider = settings.tts_provider.lower().strip()
    if provider != "gemini":
        raise TTSUnavailable(f"Unsupported cloud TTS provider: {settings.tts_provider}")
    return GeminiTTSClient(
        os.getenv("GEMINI_API_KEY", ""),
        base_url=settings.gemini_tts_base_url,
        model=settings.gemini_tts_model,
        voice=settings.gemini_tts_voice,
    )


def _extract_inline_audio(data: dict[str, Any]) -> str:
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError) as exc:
        raise TTSUnavailable("Gemini TTS returned an unexpected response shape.") from exc
    for part in parts:
        inline_data = part.get("inlineData") or part.get("inline_data")
        if inline_data and inline_data.get("data"):
            return str(inline_data["data"])
    raise TTSUnavailable("Gemini TTS did not return audio data.")


def _pcm_to_wav(pcm_bytes: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(pcm_bytes)
    return buffer.getvalue()
