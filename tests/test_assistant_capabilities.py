from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from marlin.actions import ActionOutcome, ComputerActionService
from marlin.config import MarlinSettings
from marlin.runtime import MarlinRuntime
from marlin.storage import MarlinStore
from second_brain.database.connection import initialize_database


def make_runtime(tmp_path: Path) -> MarlinRuntime:
    return MarlinRuntime(
        MarlinSettings(
            database_path=tmp_path / "brain.db",
            graph_root=tmp_path / "Projects",
            auto_index_c_drive=False,
            weather_enabled=False,
            voice_output=False,
        ),
        start_background=False,
    )


def test_turn_off_and_hide_are_distinct_desktop_commands(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    runtime.desktop = Mock()
    runtime.desktop.control.side_effect = lambda action: action
    monkeypatch.setattr(runtime.model, "chat", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called")))

    assert runtime.command("hide MARLIN")["message"] == "hide"
    assert runtime.command("turn yourself off")["message"] == "exit"
    assert runtime.desktop.control.call_args_list[-2].args == ("hide",)
    assert runtime.desktop.control.call_args_list[-1].args == ("exit",)


def test_camera_launches_native_windows_app(tmp_path, monkeypatch):
    database = tmp_path / "brain.db"
    initialize_database(database)
    store = MarlinStore(database)
    store.migrate(backup=False)
    actions = ComputerActionService(store)
    launched = []
    monkeypatch.setattr("marlin.actions.sys.platform", "win32")
    monkeypatch.setattr("marlin.actions.subprocess.Popen", lambda command, **kwargs: launched.append(command))

    result = actions.invoke("open_camera", {"_explicit_command": True})

    assert result.ok
    assert result.client_action == "open_camera_native"
    assert launched == [["explorer.exe", r"shell:AppsFolder\Microsoft.WindowsCamera_8wekyb3d8bbwe!App"]]


def test_camera_cannot_open_without_an_explicit_command_marker(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(
        runtime.actions,
        "_open_windows_camera",
        lambda: (_ for _ in ()).throw(AssertionError("camera opened")),
    )

    direct = runtime.actions.invoke("open_camera", {})
    model = runtime._execute_model_tool("open_app", {"app": "Camera"})

    assert not direct.ok
    assert not model.ok
    assert "stayed closed" in direct.message
    assert "stayed closed" in model.message


def test_explicit_local_video_bypasses_model_and_uses_default_player(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    video = tmp_path / "demo clip.mp4"
    video.write_bytes(b"video")
    opened = []
    monkeypatch.setattr("marlin.actions.os.startfile", lambda path: opened.append(path), raising=False)
    monkeypatch.setattr(runtime.model, "chat", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called")))

    result = runtime.command(f'play "{video}"')

    assert result["ok"]
    assert opened == [str(video.resolve())]
    assert "default video player" in result["message"]


def test_natural_open_path_bypasses_web_search(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    document = tmp_path / "project brief.txt"
    document.write_text("hello", encoding="utf-8")
    opened = []
    monkeypatch.setattr("marlin.actions.os.startfile", lambda path: opened.append(path), raising=False)
    monkeypatch.setattr(runtime.model, "chat", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called")))

    result = runtime.command(f'open "{document}"')

    assert result["ok"]
    assert opened == [str(document.resolve())]


def test_close_app_is_directly_parsed_and_requires_confirmation(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(runtime.model, "chat", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called")))

    result = runtime.command("close VS Code")

    assert result["ok"]
    assert result["pending"]["name"] == "close_app"
    assert result["pending"]["target"] == "VS Code"


def test_process_aliases_match_common_windows_apps(tmp_path):
    runtime = make_runtime(tmp_path)

    assert "code" in runtime.actions._process_names("VS Code")
    assert "chrome" in runtime.actions._process_names("Google Chrome")
    assert "msedge" in runtime.actions._process_names("Edge")


def test_graph_explanation_is_local_and_structured(tmp_path, monkeypatch):
    from marlin import indexer

    monkeypatch.setattr(indexer, "SKIP_NAMES", indexer.SKIP_NAMES - {"appdata"})
    root = tmp_path / "Projects"
    (root / "Alpha").mkdir(parents=True)
    (root / "Alpha" / "main.py").write_text("print('hello')", encoding="utf-8")
    runtime = make_runtime(tmp_path)
    runtime.indexer.index(root)
    monkeypatch.setattr(runtime.model, "chat", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called")))

    result = runtime.command("explain my graph")

    assert result["ok"]
    assert result["data"]["graph_summary"]["projects"] == ["Alpha"]
    assert result["data"]["graph_summary"]["files"] == 1
    assert "1 projects" in result["message"]


def test_natural_graph_questions_use_visible_graph_without_model(tmp_path, monkeypatch):
    from marlin import indexer

    monkeypatch.setattr(indexer, "SKIP_NAMES", indexer.SKIP_NAMES - {"appdata"})
    root = tmp_path / "Projects"
    (root / "Alpha" / "src").mkdir(parents=True)
    (root / "Alpha" / "src" / "main.py").write_text("print('hello')", encoding="utf-8")
    (root / "Beta").mkdir(parents=True)
    (root / "Beta" / "readme.md").write_text("Beta", encoding="utf-8")
    runtime = make_runtime(tmp_path)
    runtime.indexer.index(root)
    monkeypatch.setattr(runtime.model, "chat", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called")))

    count = runtime.command("How many files and connections are in my graph?")
    project = runtime.command("What files does Alpha contain in the graph?")

    assert count["data"]["graph_summary"]["files"] == 2
    assert "relationships" in count["message"]
    assert project["data"]["graph_summary"]["focused_project"] == "Alpha"
    assert "main.py" in project["message"]
