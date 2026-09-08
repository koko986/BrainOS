import pytest
from marlin.browser_media import YouTubePlayer

from marlin.config import MarlinSettings
from marlin.runtime import MarlinRuntime


def test_youtube_bot_gate_is_reported_not_bypassed():
    assert 'requires you to sign in' in YouTubePlayer.access_message('Sign in to confirm you\u2019re not a bot')
    assert YouTubePlayer.access_message('Normal video title') is None


def test_empty_music_request_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        YouTubePlayer(tmp_path).play('')


@pytest.mark.parametrize('text,query', [
    ('open youtube and play some music', 'relaxing music'),
    ('Open YouTube, and play jazz', 'jazz'),
    ('play piano music on youtube', 'piano music'),
    ('play music', 'relaxing music'),
])
def test_music_request_bypasses_model_and_browser_homepage(tmp_path, monkeypatch, text, query):
    runtime = MarlinRuntime(MarlinSettings(database_path=tmp_path / 'brain.db', voice_output=False, auto_index_c_drive=False), start_background=False)
    calls = []
    monkeypatch.setattr(runtime.model, 'chat', lambda *args, **kwargs: pytest.fail('Model should not be needed'))
    monkeypatch.setattr(runtime.actions.youtube, 'play', lambda q: calls.append(q) or {'ok': True, 'message': 'Playing music', 'url': 'https://www.youtube.com/watch?v=example'})
    result = runtime.command(text)
    assert result['ok'] and calls == [query]
    runtime.shutdown()


def test_playback_failure_is_not_claimed_as_success(tmp_path, monkeypatch):
    runtime = MarlinRuntime(MarlinSettings(database_path=tmp_path / 'brain.db', voice_output=False, auto_index_c_drive=False), start_background=False)
    monkeypatch.setattr(runtime.actions.youtube, 'play', lambda q: {'ok': False, 'message': 'YouTube requires attention.'})
    result = runtime.command('play music')
    assert not result['ok']
    assert runtime.store.recent_actions(1)[0]['status'] == 'failed'
    runtime.shutdown()
