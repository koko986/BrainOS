from unittest.mock import Mock

from fastapi.testclient import TestClient
from marlin.desktop import DesktopController
from marlin.config import MarlinSettings
from marlin.runtime import MarlinRuntime
from marlin.web import create_app
from marlin.browser_media import YouTubePlayer


def test_tab_close_targets_only_owned_youtube_page():
    page = Mock()
    page.is_closed.return_value = False
    page.url = 'https://www.youtube.com/watch?v=example'
    assert YouTubePlayer._close_page(page)['ok']
    page.close.assert_called_once()
    page.close.reset_mock()
    page.url = 'https://docs.google.com/document/edit'
    assert not YouTubePlayer._close_page(page)['ok']
    page.close.assert_not_called()


def test_window_close_hides_without_stopping_runtime():
    runtime = Mock()
    controller = DesktopController(runtime, 'http://127.0.0.1:8765')
    controller.window = Mock()
    controller.ready = True
    assert controller.closing() is False
    controller.window.hide.assert_called_once()
    runtime.shutdown.assert_not_called()
    controller.control('show')
    controller.window.show.assert_called_once()
    controller.window.restore.assert_called_once()
    controller.exiting = True
    assert controller.closing() is True


def test_self_commands_bypass_model_and_use_desktop(tmp_path, monkeypatch):
    runtime = MarlinRuntime(MarlinSettings(database_path=tmp_path/'brain.db', voice_output=False, auto_index_c_drive=False), start_background=False)
    runtime.desktop = Mock()
    runtime.desktop.control.return_value = 'Done'
    monkeypatch.setattr(runtime.model, 'chat', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('model called')))
    for text, action in [('show yourself', 'show'), ('where are you?', 'show'), ('hide marlin', 'hide'), ('close', 'exit'), ('close marlin', 'exit'), ('turn yourself off', 'exit'), ('turnoff hisself', 'exit'), ('MARLIN, please shut yourself down now', 'exit'), ('go offline', 'exit'), ('exit marlin', 'exit')]:
        runtime.command(text)
        runtime.desktop.control.assert_called_with(action)
    monkeypatch.setattr(runtime.actions.youtube, 'close_tab', lambda: {'ok': True, 'message': 'Closed my tab.'})
    assert runtime.command('close that tap')['message'] == 'Closed my tab.'
    client = TestClient(create_app(runtime))
    assert client.post('/api/desktop/exit').status_code == 403
    runtime.shutdown()


def test_exit_stops_voice_and_destroys_window():
    controller = DesktopController(Mock(), 'http://127.0.0.1:8765')
    controller.window = Mock()
    controller.tray = Mock()
    controller.server = Mock()
    controller._exit()
    controller.runtime.shutdown.assert_called_once()
    assert controller.server.should_exit is True
    controller.window.destroy.assert_called_once()
    controller.tray.stop.assert_called_once()


def test_exit_removes_instance_marker(tmp_path):
    marker = tmp_path / 'marlin.desktop.json'
    marker.write_text('{}', encoding='utf-8')
    controller = DesktopController(Mock(), 'http://127.0.0.1:8765')
    controller.state_file = marker

    controller._exit()

    assert not marker.exists()
