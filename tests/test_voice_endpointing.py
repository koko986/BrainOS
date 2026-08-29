"""Deterministic tests for microphone endpointing.

A laptop array microphone with automatic gain swings well above its own noise
floor with nobody speaking, which previously made MARLIN record silence on
every turn. These tests feed synthetic audio through the real recording loop.
"""

from __future__ import annotations

import array

from second_brain.app import voice
from second_brain.app.voice import BLOCK_FRAMES, record_utterance

SILENT_BLOCK = bytes(BLOCK_FRAMES * 2)


def _tone_block(amplitude: int) -> bytes:
    samples = array.array(
        "h",
        [amplitude if index % 2 == 0 else -amplitude for index in range(BLOCK_FRAMES)],
    )
    return samples.tobytes()


class _FakeStream:
    def __init__(self, blocks: list[bytes], callback):
        self._blocks = blocks
        self._callback = callback

    def __enter__(self) -> "_FakeStream":
        for block in self._blocks:
            self._callback(block, BLOCK_FRAMES, None, None)
        return self

    def __exit__(self, *_exc_info) -> bool:
        return False


class _FakeSoundDevice:
    def __init__(self, blocks: list[bytes]):
        self.blocks = blocks

    def RawInputStream(self, *, callback, **_kwargs):  # noqa: N802
        return _FakeStream(self.blocks, callback)


def _record(monkeypatch, blocks: list[bytes], **kwargs) -> bytes:
    monkeypatch.setattr(voice, "_import_sounddevice", lambda: _FakeSoundDevice(blocks))
    options = {"max_seconds": 5.0, "silence_seconds": 0.8, "start_timeout": 0.5}
    options.update(kwargs)
    return record_utterance(**options)


def test_silence_is_not_recorded(monkeypatch):
    captured = _record(monkeypatch, [SILENT_BLOCK] * 40)

    assert captured == b""


def test_gain_ramp_without_speech_is_not_recorded(monkeypatch):
    """Reproduces the auto-gain ramp that used to trigger a false recording."""

    ramp = [_tone_block(level) for level in (300, 1800, 3300, 3500, 3600, 3400, 2400)]
    blocks = [SILENT_BLOCK] * 8 + ramp + [_tone_block(2000)] * 20

    assert _record(monkeypatch, blocks) == b""


def test_a_brief_click_is_not_recorded(monkeypatch):
    blocks = [SILENT_BLOCK] * 20 + [_tone_block(9000)] * 2 + [SILENT_BLOCK] * 20

    assert _record(monkeypatch, blocks) == b""


def test_sustained_loud_speech_is_recorded(monkeypatch):
    blocks = (
        [SILENT_BLOCK] * 20
        + [_tone_block(9000)] * 8
        + [SILENT_BLOCK] * 14
    )

    captured = _record(monkeypatch, blocks)

    assert captured.startswith(b"RIFF")
    # Pre-roll plus the loud run, minus the trailing silence used to close it.
    assert len(captured) > BLOCK_FRAMES * 2 * 8


def test_recording_stops_after_the_silence_window(monkeypatch):
    blocks = (
        [SILENT_BLOCK] * 20
        + [_tone_block(9000)] * 6
        + [SILENT_BLOCK] * 40
    )

    captured = _record(monkeypatch, blocks, silence_seconds=0.5)

    # 6 loud blocks + 4 pre-roll + about 5 blocks of trailing silence, well
    # short of the 40 silent blocks that were available.
    assert len(captured) < BLOCK_FRAMES * 2 * 25


def test_on_state_reports_progress(monkeypatch):
    states: list[str] = []
    blocks = [SILENT_BLOCK] * 20 + [_tone_block(9000)] * 8 + [SILENT_BLOCK] * 14

    _record(monkeypatch, blocks, on_state=states.append)

    assert states == ["listening", "recording", "thinking"]


def test_acoustic_model_can_trigger_recording_below_the_loudness_bar(monkeypatch):
    """A recognizer reporting speech starts recording even when quiet."""

    class QuietSpeechRecognizer:
        def __init__(self) -> None:
            self.calls = 0

        def AcceptWaveform(self, _chunk: bytes) -> bool:  # noqa: N802
            self.calls += 1
            return self.calls > 14

        def Result(self) -> str:  # noqa: N802
            return '{"text": "hello marlin"}'

        def PartialResult(self) -> str:  # noqa: N802
            return '{"partial": "hello"}' if self.calls > 10 else '{"partial": ""}'

    monkeypatch.setattr(voice, "_optional_recognizer", lambda _path: QuietSpeechRecognizer())
    blocks = [SILENT_BLOCK] * 40

    captured = _record(monkeypatch, blocks, vosk_model_path="pretend", start_timeout=2.0)

    assert captured.startswith(b"RIFF")
