"""Acoustic loopback check: play speech aloud and confirm MARLIN hears it.

Plays a synthesized command through the speakers while the normal microphone
recording path runs, then transcribes what was captured. Needs speakers and
microphone both enabled and unmuted.

    python scripts/check_loopback.py
"""

from __future__ import annotations

import sys
import threading
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_speech import synthesize  # noqa: E402
from second_brain.app.voice import (  # noqa: E402
    GroqSTT,
    record_utterance,
    resolve_vosk_model_path,
)
from second_brain.core.config import Settings  # noqa: E402

PHRASE = "Marlin please list the files on my desktop"


def main() -> int:
    import numpy as np
    import sounddevice as sd

    settings = Settings.from_env()
    model_path = resolve_vosk_model_path(settings)

    wav_path = Path(__file__).resolve().parent / "_loopback.wav"
    try:
        if not synthesize(PHRASE, wav_path):
            return 1
        with wave.open(str(wav_path), "rb") as handle:
            samples = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)

        print(f"Playing {len(samples) / 16000:.1f}s of speech: {PHRASE!r}")

        def play() -> None:
            time.sleep(1.5)
            sd.play(np.concatenate([samples, np.zeros(16000, dtype=np.int16)]), 16000)
            sd.wait()

        threading.Thread(target=play, daemon=True).start()

        captured = record_utterance(
            max_seconds=12.0,
            silence_seconds=0.9,
            start_timeout=8.0,
            vosk_model_path=model_path,
            on_state=lambda state: print(f"  state: {state}"),
        )
    finally:
        wav_path.unlink(missing_ok=True)

    if not captured:
        print("[FAIL] Nothing was captured. Check speaker volume and microphone input.")
        return 1

    print(f"[PASS] Captured {len(captured) / 32000:.1f}s of audio")
    transcript = GroqSTT.from_settings(settings).transcribe(captured)
    print(f"[{'PASS' if transcript.strip() else 'FAIL'}] Transcript: {transcript.strip()!r}")
    return 0 if transcript.strip() else 1


if __name__ == "__main__":
    raise SystemExit(main())
