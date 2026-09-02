from __future__ import annotations

from marlin.config import MarlinSettings
from marlin.events import EventBus
from marlin.voice import LocalVoiceService
from marlin.runtime import MarlinRuntime


def test_known_voice_command_uses_fast_vosk_path(monkeypatch):
    voice = LocalVoiceService(MarlinSettings(), EventBus())
    monkeypatch.setattr("marlin.voice.record_utterance", lambda **_kwargs: b"RIFFaudio")
    monkeypatch.setattr(voice.fast_stt, "transcribe", lambda _wav: "open chrome")
    monkeypatch.setattr(voice.stt, "transcribe", lambda _wav: (_ for _ in ()).throw(AssertionError("Whisper called")))

    result = voice.listen_once()

    assert result["text"] == "open chrome"
    assert result["engine"] == "vosk-fast"


def test_stop_cancels_active_input_and_output():
    voice = LocalVoiceService(MarlinSettings(), EventBus())
    voice._listen_cancel.clear()
    voice._cancel.clear()

    voice.stop()

    assert voice._listen_cancel.is_set()
    assert voice._cancel.is_set()


def test_wake_word_captures_and_executes_follow_up(tmp_path, monkeypatch):
    settings = MarlinSettings(
        database_path=tmp_path / "brain.db",
        auto_index_c_drive=False,
        weather_enabled=False,
        voice_output=False,
        wake_word_enabled=True,
    )
    runtime = MarlinRuntime(settings, start_background=False)
    events: list[tuple[str, dict]] = []

    class Listener:
        def prepare(self):
            return None

    monkeypatch.setattr(runtime.voice, "wake_listener", lambda: Listener())
    monkeypatch.setattr(runtime.voice, "wait_for_wake", lambda _listener, stop: stop.set() or True)
    monkeypatch.setattr(runtime.voice, "speak", lambda _text: None)
    monkeypatch.setattr(runtime.voice, "wait", lambda _timeout: None)
    monkeypatch.setattr(runtime, "listen", lambda **_kwargs: {"text": "open chrome"})
    monkeypatch.setattr(runtime, "command", lambda text, source="ui": {"ok": True, "message": f"{source}:{text}", "data": {}, "pending": None, "client_action": None})
    monkeypatch.setattr(runtime.events, "publish", lambda kind, **data: events.append((kind, data)))

    runtime._wake_worker()

    assert ("wake.detected", {"phrase": "Hey MARLIN"}) in events
    assert ("wake.heard", {"text": "open chrome"}) in events
    assert any(kind == "wake.result" and data["message"] == "wake:open chrome" for kind, data in events)
