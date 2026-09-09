from __future__ import annotations

import time

from marlin.actions import ComputerActionService
from marlin.storage import MarlinStore
from second_brain.database.connection import initialize_database


def service(tmp_path) -> ComputerActionService:
    database = tmp_path / "brain.db"
    initialize_database(database)
    store = MarlinStore(database)
    store.migrate(backup=False)
    return ComputerActionService(store)


def test_edit_requires_one_use_confirmation_and_revalidates_target(tmp_path):
    actions = service(tmp_path)
    target = tmp_path / "notes.txt"
    target.write_text("before", encoding="utf-8")
    preview = actions.invoke("edit_file", {"path": str(target), "old_text": "before", "new_text": "after"})

    assert preview.pending is not None
    assert "-before" in preview.pending.preview
    assert target.read_text(encoding="utf-8") == "before"
    target.write_text("changed elsewhere", encoding="utf-8")
    rejected = actions.approve(preview.pending.id)
    assert not rejected.ok and "changed" in rejected.message.lower()
    assert not actions.approve(preview.pending.id).ok


def test_confirmation_expires_and_unknown_apps_are_blocked(tmp_path):
    actions = service(tmp_path)
    target = tmp_path / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    preview = actions.invoke("delete_path", {"path": str(target)})
    assert preview.pending is not None
    preview.pending.expires_at = time.time() - 1

    assert "expired" in actions.approve(preview.pending.id).message.lower()
    blocked = actions.invoke("open_app", {"app": "unknown-program"})
    assert not blocked.ok and "allow-list" in blocked.message


def test_protected_windows_path_is_refused_without_crashing(tmp_path):
    outcome = service(tmp_path).invoke("delete_path", {"path": r"C:\Windows"})
    assert not outcome.ok
    assert "protected" in outcome.message.lower()


def test_websites_resolve_to_official_urls_and_unknown_terms_search(tmp_path, monkeypatch):
    actions = service(tmp_path)
    launched: list[list[str]] = []
    monkeypatch.setattr(actions, "_app_command", lambda _name: r"C:\Chrome\chrome.exe")
    monkeypatch.setattr("marlin.actions.subprocess.Popen", lambda command, **_kwargs: launched.append(command))

    known = actions.invoke("open_url", {"site": "youtube"})
    search = actions.invoke("open_url", {"site": "Python graph tutorials"})

    assert known.ok and launched[0][-1] == "https://www.youtube.com/"
    assert search.ok and launched[1][-1].startswith("https://www.google.com/search?q=Python+graph+tutorials")


def test_apps_can_be_discovered_from_desktop_shortcuts(tmp_path, monkeypatch):
    user = tmp_path / "User"
    public = tmp_path / "Public"
    desktop = user / "Desktop"
    public_desktop = public / "Desktop"
    desktop.mkdir(parents=True)
    public_desktop.mkdir(parents=True)
    eclipse = desktop / "Eclipse IDE for Java and DSL Developers - 2026-06.lnk"
    youtube = desktop / "YouTube Desktop.lnk"
    genymotion = public_desktop / "Genymotion.lnk"
    for shortcut in (eclipse, youtube, genymotion):
        shortcut.write_bytes(b"shortcut")
    monkeypatch.setenv("USERPROFILE", str(user))
    monkeypatch.setenv("PUBLIC", str(public))
    monkeypatch.delenv("OneDrive", raising=False)
    monkeypatch.delenv("OneDriveConsumer", raising=False)
    monkeypatch.setattr("marlin.actions.shutil.which", lambda _name: None)
    monkeypatch.setattr(ComputerActionService, "_registered_app_path", staticmethod(lambda _name: None))
    monkeypatch.setattr(ComputerActionService, "_start_menu_shortcut", staticmethod(lambda _name: None))

    assert ComputerActionService._app_command("eclipse") == str(eclipse)
    assert ComputerActionService._app_command("desktop youtube") == str(youtube)
    assert ComputerActionService._app_command("the desktop youtube app") == str(youtube)
    assert ComputerActionService._app_command("genymotion") == str(genymotion)
