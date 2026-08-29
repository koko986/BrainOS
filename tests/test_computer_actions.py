from __future__ import annotations

from second_brain.ai.intent_schema import StructuredIntent
from second_brain.computer.actions import (
    ComputerActionService,
    looks_like_blocked_computer_request,
    parse_computer_command,
    resolve_app_command,
)
from second_brain.database.connection import initialize_database
from second_brain.knowledge.service import KnowledgeService


def _service(tmp_path):
    db_path = tmp_path / "brain.db"
    initialize_database(db_path)
    return ComputerActionService(
        KnowledgeService(db_path),
        allowed_apps={"notepad": "notepad", "vscode": "code"},
    )


def test_parse_open_camera_command_runs_without_confirmation():
    intent = parse_computer_command("open camera")

    assert intent is not None
    assert intent.intent == "open_camera"
    assert not intent.requires_confirmation


def test_preview_open_camera_is_client_action(tmp_path):
    service = _service(tmp_path)
    intent = parse_computer_command("open camera")

    action = service.preview(intent)

    assert action is not None
    assert action.client_action == "open_camera"
    assert action.risk == "camera"


def test_preview_open_folder_requires_existing_folder(tmp_path):
    service = _service(tmp_path)
    folder = tmp_path / "notes"
    folder.mkdir()
    intent = StructuredIntent(
        intent="open_folder",
        language="en",
        confidence=1,
        parameters={"path": str(folder)},
        requires_confirmation=False,
    )

    action = service.preview(intent)

    assert action is not None
    assert action.parameters["path"] == str(folder.resolve())


def test_any_app_may_be_launched(tmp_path):
    """The allowlist is now only an alias table, not a gate."""

    service = _service(tmp_path)
    intent = StructuredIntent(
        intent="open_app",
        language="en",
        confidence=1,
        parameters={"app_name": "some-other-app"},
        requires_confirmation=False,
    )

    action = service.preview(intent)

    assert action is not None
    assert action.parameters["command"] == "some-other-app"


def test_known_app_aliases_still_resolve():
    assert resolve_app_command("vscode") == "code"
    assert resolve_app_command("calculator") == "calc"
    assert resolve_app_command("obsidian.exe") == "obsidian.exe"


def test_only_shell_requests_are_blocked():
    assert looks_like_blocked_computer_request("run shell dir")
    assert looks_like_blocked_computer_request("open powershell")
    assert not looks_like_blocked_computer_request("delete my downloads")
    assert not looks_like_blocked_computer_request("rename this file to final.txt")
    assert not looks_like_blocked_computer_request("open camera")


def test_generic_open_requests_defer_to_the_agent():
    """"open <something unknown>" should reach the agent, not guess an app."""

    assert parse_computer_command("open my tax return") is None
    assert parse_computer_command("open notepad") is not None
