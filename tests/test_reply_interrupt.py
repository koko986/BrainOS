from fastapi.testclient import TestClient
import threading
from marlin.config import MarlinSettings
from marlin.runtime import MarlinRuntime
from marlin.web import create_app


def test_stop_cancels_generation_without_speaking_an_error(tmp_path, monkeypatch):
    runtime = MarlinRuntime(MarlinSettings(database_path=tmp_path / 'brain.db',
        voice_output=False, auto_index_c_drive=False), start_background=False)
    spoken = []
    monkeypatch.setattr(runtime.voice, 'speak', spoken.append)
    def chat(messages, *, tools, on_token):
        runtime.stop_voice()
        on_token('This stale answer must not appear.')
        raise AssertionError('Generation should have stopped')
    monkeypatch.setattr(runtime.model, 'chat', chat)
    result = runtime.command('tell me a story')
    assert result['interrupted']
    assert not spoken
    assert not any('stale answer' in item['content'] for item in runtime.store.recent_messages(8))
    runtime.shutdown()


def test_interrupt_endpoint_requires_token_and_resumes_chat(tmp_path, monkeypatch):
    runtime = MarlinRuntime(MarlinSettings(database_path=tmp_path / 'brain.db',
        voice_output=False, auto_index_c_drive=False), start_background=False)
    app = create_app(runtime)
    monkeypatch.setattr(runtime, 'start_voice_chat', lambda: {'active': True})
    with TestClient(app) as client:
        assert client.post('/api/voice/interrupt').status_code == 403
        response = client.post('/api/voice/interrupt', headers={'x-marlin-token': app.state.token})
        assert response.json()['active']
    runtime.shutdown()


def test_browser_falls_back_to_edge_without_using_personal_profile(tmp_path):
    from types import SimpleNamespace
    from marlin.browser_media import YouTubePlayer
    calls = []
    def launch(profile, **kwargs):
        calls.append((profile, kwargs['channel']))
        if kwargs['channel'] == 'chrome':
            raise RuntimeError('Chrome missing')
        return 'edge-context'
    driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    assert YouTubePlayer(tmp_path)._launch_browser(driver) == 'edge-context'
    assert calls == [(str(tmp_path), 'chrome'), (str(tmp_path / 'msedge'), 'msedge')]


def test_voice_turn_can_barge_in_and_cancel_generation(tmp_path, monkeypatch):
    runtime = MarlinRuntime(MarlinSettings(database_path=tmp_path / 'brain.db',
        voice_output=False, auto_index_c_drive=False), start_background=False)
    generation_started = threading.Event()
    generation_cancelled = threading.Event()

    def command(*_args, **_kwargs):
        generation_started.set()
        generation_cancelled.wait(1)
        return {'interrupted': True, 'message': '', 'pending': None}

    def barge(_stop_event):
        assert generation_started.wait(1)
        generation_cancelled.set()
        return True

    monkeypatch.setattr(runtime, 'command', command)
    monkeypatch.setattr(runtime.voice, 'wait_for_barge_in', barge)
    result = runtime._chat_command('tell me a long story')
    assert result['interrupted']
    assert runtime._voice_generation == 1
    runtime.shutdown()


def test_fast_voice_defaults_are_bounded():
    settings = MarlinSettings()
    assert settings.ollama_context <= 1536
    assert settings.ollama_max_tokens <= 96
    assert settings.voice_silence_seconds <= .4
