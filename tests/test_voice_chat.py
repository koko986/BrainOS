import threading

from fastapi.testclient import TestClient
from marlin.config import MarlinSettings
from marlin.runtime import MarlinRuntime
from marlin.web import create_app


def runtime_for_test(tmp_path, monkeypatch, heard):
    runtime = MarlinRuntime(MarlinSettings(database_path=tmp_path / 'brain.db', voice_output=False,
        auto_index_c_drive=False, weather_enabled=False), start_background=False)
    turns = iter(heard)
    spoken, events = [], []
    monkeypatch.setattr(runtime.voice, 'speak', spoken.append)
    monkeypatch.setattr(runtime, 'listen', lambda **kwargs: next(turns))
    monkeypatch.setattr(runtime.events, 'publish', lambda kind, **data: events.append((kind, data)))
    return runtime, spoken, events


def heard(text, confidence=.95):
    return {'text': text, 'confidence': confidence}


def test_chat_submits_nonempty_uncertain_speech_without_reprompting(tmp_path, monkeypatch):
    runtime, spoken, events = runtime_for_test(tmp_path, monkeypatch, [
        heard('hello'), {'text': 'unclear', 'requires_clarification': True},
        heard('tell me more'), heard('end voice chat')])
    commands = []
    monkeypatch.setattr(runtime, 'command', lambda text, **kwargs: commands.append(text) or {'message': 'A real answer', 'pending': None})
    runtime._chat_worker()
    assert commands == ['hello', 'unclear', 'tell me more']
    assert 'Sorry, could you say that again?' not in spoken
    assert not any(kind == 'voice.chat.retrying' for kind, _ in events)
    assert not runtime._chat_paused.is_set()
    assert len([e for e in events if e[0] == 'voice.chat.result']) == 3
    assert events[-1] == ('voice.chat', {'active': False})


def test_voice_approval_requires_explicit_high_confidence_phrase(tmp_path, monkeypatch):
    runtime, spoken, events = runtime_for_test(tmp_path, monkeypatch, [
        heard('edit the file'), heard('yes'), heard('approve action', .6),
        heard('approve action', .95), heard('end voice chat')])
    pending = {'id': 'one-use-id', 'label': 'Edit file', 'target': r'C:\notes.txt'}
    monkeypatch.setattr(runtime, 'command', lambda *args, **kwargs: {'pending': pending})
    monkeypatch.setattr(runtime.actions, 'is_pending', lambda action_id: True)
    approvals = []
    monkeypatch.setattr(runtime, 'approve_action', lambda action_id: approvals.append(action_id) or {'message': 'approved'})
    runtime._chat_worker()
    assert approvals == ['one-use-id']
    assert any(r'C:\notes.txt' in text for text in spoken)
    assert spoken.count('Please say approve action or cancel action, or end voice chat.') == 2


def test_ending_chat_cancels_pending_action(tmp_path, monkeypatch):
    runtime, _, _ = runtime_for_test(tmp_path, monkeypatch, [heard('change file'), heard('stop listening')])
    monkeypatch.setattr(runtime, 'command', lambda *args, **kwargs: {'pending': {'id': 'id', 'label': 'Edit', 'target': 'file'}})
    cancelled = []
    monkeypatch.setattr(runtime.actions, 'cancel', cancelled.append)
    runtime._chat_worker()
    assert cancelled == ['id']


def test_stop_while_recognizing_discards_late_command(tmp_path, monkeypatch):
    runtime, _, _ = runtime_for_test(tmp_path, monkeypatch, [])
    def listen(**kwargs):
        runtime._chat_stop.set()
        return heard('open camera')
    monkeypatch.setattr(runtime, 'listen', listen)
    commands = []
    monkeypatch.setattr(runtime, 'command', lambda *args, **kwargs: commands.append(args))
    runtime._chat_worker()
    assert commands == []


def test_voice_chat_endpoints_require_token(tmp_path, monkeypatch):
    runtime, _, _ = runtime_for_test(tmp_path, monkeypatch, [])
    app = create_app(runtime)
    client = TestClient(app)
    monkeypatch.setattr(runtime, 'start_voice_chat', lambda: {'active': True})
    for route in ['start', 'stop']:
        assert client.post('/api/voice/chat/' + route).status_code == 403
    response = client.post('/api/voice/chat/start', headers={'X-Marlin-Token': app.state.token})
    assert response.json() == {'active': True}


def test_silence_does_not_end_chat_and_device_errors_recover(tmp_path, monkeypatch):
    runtime, _, events = runtime_for_test(tmp_path, monkeypatch, [
        *[{'text': '', 'error': 'I did not hear speech.'} for _ in range(8)],
        {'text': '', 'error': 'Microphone disconnected'}, heard('hello again'), heard('end voice chat')])
    commands = []
    monkeypatch.setattr(runtime, 'command', lambda text, **kwargs: commands.append(text) or {'message': 'Hello'})
    runtime._chat_worker()
    assert commands == ['hello again']
    assert any(kind == 'voice.error' for kind, _ in events)


def test_failed_request_does_not_kill_conversation(tmp_path, monkeypatch):
    runtime, _, events = runtime_for_test(tmp_path, monkeypatch, [heard('first'), heard('second'), heard('end voice chat')])
    def command(text, **kwargs):
        if text == 'first':
            raise RuntimeError('temporary failure')
        return {'message': 'Recovered'}
    monkeypatch.setattr(runtime, 'command', command)
    runtime._chat_worker()
    assert ('voice.chat.result', {'message': 'Recovered'}) in events


def test_wake_only_gets_spoken_acknowledgement(tmp_path, monkeypatch):
    runtime, spoken, events = runtime_for_test(tmp_path, monkeypatch, [
        {'text': '', 'wake_only': True}, heard('end voice chat')])
    runtime._chat_worker()
    assert spoken == ['Yes?']


def test_voice_chat_ignores_uncertain_camera_without_stopping(tmp_path, monkeypatch):
    runtime, _, events = runtime_for_test(tmp_path, monkeypatch, [
        heard('open camera', .25), heard('hello'), heard('end voice chat')])
    commands = []
    monkeypatch.setattr(runtime, 'command', lambda text, **kwargs: commands.append(text) or {'message': 'done', 'pending': None})

    runtime._chat_worker()

    assert commands == ['hello']
    clarification = next(data for kind, data in events if kind == 'voice.clarification')
    assert clarification['blocked_action'] == 'open_camera'


def test_paused_conversation_can_resume_from_wake_word(tmp_path, monkeypatch):
    runtime, _, events = runtime_for_test(tmp_path, monkeypatch, [])
    from unittest.mock import Mock
    runtime._chat_thread = Mock()
    runtime._chat_thread.is_alive.return_value = True
    runtime._chat_paused.set()
    monkeypatch.setattr(runtime.voice, 'wake_listener', lambda: Mock())
    monkeypatch.setattr(runtime.voice, 'wait_for_wake', lambda listener, stop: stop.set() or True)
    runtime._wake_worker()
    assert not runtime._chat_paused.is_set()
    assert ('voice.chat.resumed', {}) in events
