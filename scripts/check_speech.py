"""Verify the speech pipeline without needing a live microphone.

Synthesizes a phrase with Windows speech, then feeds the audio through the
Groq transcriber and the offline wake-word recognizer.

    python scripts/check_speech.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from second_brain.app.voice import (  # noqa: E402
    SAMPLE_RATE,
    GroqSTT,
    VoiceInputUnavailable,
    VoskSTT,
    WakeWordListener,
    parse_wake_words,
    resolve_vosk_model_path,
)
from second_brain.core.config import Settings  # noqa: E402

SPOKEN_COMMAND = "hey marlin open my documents folder"


def synthesize(text: str, destination: Path) -> bool:
    """Render text to a 16 kHz mono WAV using Windows System.Speech."""

    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
        "16000, "
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
        "[System.Speech.AudioFormat.AudioChannel]::Mono); "
        f"$s.SetOutputToWaveFile('{destination}', $fmt); "
        f"$s.Speak('{text}'); "
        "$s.Dispose()"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"[FAIL] Could not synthesize test audio: {result.stderr.strip()[:200]}")
        return False
    return destination.exists() and destination.stat().st_size > 1000


def main() -> int:
    settings = Settings.from_env()
    results: list[bool] = []

    with tempfile.TemporaryDirectory() as work_dir:
        wav_path = Path(work_dir) / "spoken.wav"
        if not synthesize(SPOKEN_COMMAND, wav_path):
            return 1
        wav_bytes = wav_path.read_bytes()
        print(f"Synthesized {len(wav_bytes)} bytes of speech: {SPOKEN_COMMAND!r}\n")

        try:
            transcript = GroqSTT.from_settings(settings).transcribe(wav_bytes)
            ok = bool(transcript.strip())
            print(f"[{'PASS' if ok else 'FAIL'}] Groq transcription - {transcript.strip()!r}")
            results.append(ok)
        except VoiceInputUnavailable as exc:
            print(f"[FAIL] Groq transcription - {exc}")
            results.append(False)

        model_path = resolve_vosk_model_path(settings)
        try:
            offline = VoskSTT(model_path).transcribe(wav_bytes)
            ok = bool(offline.strip())
            print(f"[{'PASS' if ok else 'FAIL'}] Offline Vosk fallback - {offline.strip()!r}")
            results.append(ok)
        except VoiceInputUnavailable as exc:
            print(f"[FAIL] Offline Vosk fallback - {exc}")
            results.append(False)

        listener = WakeWordListener(model_path, parse_wake_words(settings.wake_words))
        try:
            recognizer = listener._build_recognizer()  # noqa: SLF001
            import wave

            with wave.open(str(wav_path), "rb") as handle:
                pcm = handle.readframes(handle.getnframes())
            step = SAMPLE_RATE * 2 // 10
            spotted = ""
            for offset in range(0, len(pcm), step):
                if recognizer.AcceptWaveform(pcm[offset : offset + step]):
                    spotted += " " + str(json.loads(recognizer.Result()).get("text", ""))
            spotted += " " + str(json.loads(recognizer.FinalResult()).get("text", ""))
            detected = listener.matches(spotted)
            print(
                f"[{'PASS' if detected else 'FAIL'}] Wake word spotted - "
                f"grammar heard {spotted.strip()!r}"
            )
            results.append(detected)
        except VoiceInputUnavailable as exc:
            print(f"[FAIL] Wake word spotted - {exc}")
            results.append(False)

    print()
    if all(results):
        print("Speech pipeline verified.")
        return 0
    print("Speech pipeline has failures.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
