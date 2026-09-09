import pytest
from types import SimpleNamespace
from marlin.browser_media import YouTubePlayer

from marlin.config import MarlinSettings
from marlin.runtime import MarlinRuntime


def test_youtube_bot_gate_is_reported_not_bypassed():
    assert 'requires you to sign in' in YouTubePlayer.access_message('Sign in to confirm you\u2019re not a bot')
    assert YouTubePlayer.access_message('Normal video title') is None


def test_youtube_recovers_from_chrome_replacing_its_startup_tab():
    detached = SimpleNamespace(url='about:blank', is_closed=lambda: True)
    youtube = SimpleNamespace(url='https://www.youtube.com/results?search_query=music', is_closed=lambda: False)
    context = SimpleNamespace(pages=[detached, youtube], new_page=lambda: pytest.fail('existing YouTube page should be reused'))

    assert YouTubePlayer._recoverable_navigation_error(Exception('net::ERR_ABORTED; maybe frame was detached'))
    assert YouTubePlayer._replacement_page(context, detached) is youtube


def test_youtube_desktop_fallback_only_accepts_youtube_urls(tmp_path, monkeypatch):
    executable = tmp_path / 'YouTube Desktop' / 'ytdesktop.exe'
    executable.parent.mkdir()
    executable.write_bytes(b'app')
    launched = []
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setattr('marlin.browser_media.subprocess.Popen', lambda args, **kwargs: launched.append((args, kwargs)))

    assert not YouTubePlayer._open_desktop_player('https://example.com/watch?v=no')
    assert YouTubePlayer._open_desktop_player('https://www.youtube.com/watch?v=music')
    assert launched[0][0] == [str(executable), 'https://www.youtube.com/watch?v=music&autoplay=1']


def test_empty_music_request_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        YouTubePlayer(tmp_path).play('')


@pytest.mark.parametrize('text,query', [
    ('open youtube and play some music', 'relaxing music'),
    ('open desktop youtube and play some music', 'relaxing music'),
    ('open YouTube Desktop and play study music', 'study music'),
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
