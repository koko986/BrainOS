"""Local computer actions for MARLIN.

MARLIN runs in full autonomous mode: file and app actions execute without a
confirmation prompt. Only shell and command-interpreter requests are refused,
because MARLIN routes work through typed Python tools rather than a shell.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from second_brain.ai.intent_schema import StructuredIntent
from second_brain.files.indexer import FileIndexer
from second_brain.knowledge.service import KnowledgeService


COMPUTER_INTENTS = {
    "open_camera",
    "close_camera",
    "open_folder",
    "open_file",
    "open_app",
    "index_folder",
    "search_files",
}

BLOCKED_COMMAND_TERMS = {
    "run command",
    "run shell",
    "powershell",
    "cmd.exe",
    "command prompt",
}


@dataclass(frozen=True)
class ComputerAction:
    intent: str
    label: str
    risk: str
    target: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    client_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "label": self.label,
            "risk": self.risk,
            "target": self.target,
            "parameters": self.parameters,
            "client_action": self.client_action,
        }


@dataclass(frozen=True)
class ComputerActionResult:
    ok: bool
    message: str
    client_action: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "client_action": self.client_action,
            "data": self.data,
        }


class ComputerActionService:
    """Preview and execute explicitly confirmed local actions."""

    def __init__(
        self,
        knowledge: KnowledgeService,
        *,
        allowed_apps: dict[str, str],
        confirmation_mode: str = "always",
    ):
        self.knowledge = knowledge
        self.allowed_apps = {key.lower(): value for key, value in allowed_apps.items()}
        self.confirmation_mode = confirmation_mode

    def preview(self, intent: StructuredIntent) -> ComputerAction | None:
        if intent.intent not in COMPUTER_INTENTS or intent.is_low_confidence:
            return None

        if intent.intent == "open_camera":
            return ComputerAction(
                intent="open_camera",
                label="Open local camera preview",
                risk="camera",
                target="Browser camera preview",
                client_action="open_camera",
            )

        if intent.intent == "close_camera":
            return ComputerAction(
                intent="close_camera",
                label="Close local camera preview",
                risk="low",
                target="Browser camera preview",
                client_action="close_camera",
            )

        if intent.intent == "open_folder":
            folder = _clean_path(intent.parameters.get("path"))
            if not folder:
                raise ValueError("Folder path is required.")
            path = _resolve_named_path(folder)
            if not path.exists() or not path.is_dir():
                raise ValueError(f"Folder does not exist: {path}")
            return ComputerAction(
                intent="open_folder",
                label="Open folder in Windows Explorer",
                risk="opens_local_app",
                target=str(path),
                parameters={"path": str(path)},
            )

        if intent.intent == "open_file":
            file_path = _clean_path(intent.parameters.get("path"))
            if not file_path:
                raise ValueError("File path is required.")
            path = Path(file_path).expanduser().resolve()
            if not path.exists() or not path.is_file():
                raise ValueError(f"File does not exist: {path}")
            return ComputerAction(
                intent="open_file",
                label="Open file with the default app",
                risk="opens_local_app",
                target=str(path),
                parameters={"path": str(path)},
            )

        if intent.intent == "open_app":
            app_name = _clean_name(intent.parameters.get("app_name"))
            if not app_name:
                raise ValueError("App name is required.")
            app_key = _app_key(app_name)
            command = self.allowed_apps.get(app_key, app_name)
            return ComputerAction(
                intent="open_app",
                label="Open app",
                risk="opens_local_app",
                target=app_key,
                parameters={"app_name": app_key, "command": command},
            )

        if intent.intent == "index_folder":
            folder = _clean_path(intent.parameters.get("path"))
            if not folder:
                raise ValueError("Folder path is required.")
            path = _resolve_named_path(folder)
            if not path.exists() or not path.is_dir():
                raise ValueError(f"Folder does not exist: {path}")
            allow_drive_root = bool(intent.parameters.get("allow_drive_root"))
            max_files = _clean_int(intent.parameters.get("max_files"), default=300)
            if _is_drive_root(path):
                allow_drive_root = True
                max_files = max(max_files, 5000)
            risk = "reads_file_metadata_and_snippets"
            label = "Index folder into MARLIN brain"
            if _is_drive_root(path):
                risk = "high_volume_file_metadata_scan"
                label = "Index Windows C drive into MARLIN brain"
            return ComputerAction(
                intent="index_folder",
                label=label,
                risk=risk,
                target=str(path),
                parameters={
                    "path": str(path),
                    "max_files": max_files,
                    "allow_drive_root": allow_drive_root,
                },
            )

        if intent.intent == "search_files":
            query = str(intent.parameters.get("query") or "").strip()
            if not query:
                raise ValueError("Search query is required.")
            return ComputerAction(
                intent="search_files",
                label="Search indexed files",
                risk="low",
                target=query,
                parameters={"query": query},
            )

        return None

    def preview_from_dict(self, action: dict[str, Any]) -> ComputerAction:
        intent = str(action.get("intent", ""))
        parameters = action.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {}
        structured = StructuredIntent(
            intent=intent,
            language="en",
            confidence=1.0,
            parameters=parameters,
            requires_confirmation=False,
        )
        preview = self.preview(structured)
        if preview is None:
            raise ValueError("Unsupported computer action.")
        return preview

    def execute(self, action: ComputerAction) -> ComputerActionResult:
        if action.intent == "open_camera":
            return ComputerActionResult(
                ok=True,
                message="Camera preview approved.",
                client_action="open_camera",
            )
        if action.intent == "close_camera":
            return ComputerActionResult(
                ok=True,
                message="Camera preview closed.",
                client_action="close_camera",
            )
        if action.intent == "open_folder":
            _open_path(Path(str(action.parameters["path"])))
            return ComputerActionResult(ok=True, message=f"Opened folder: {action.target}")
        if action.intent == "open_file":
            _open_path(Path(str(action.parameters["path"])))
            return ComputerActionResult(ok=True, message=f"Opened file: {action.target}")
        if action.intent == "open_app":
            _open_app(str(action.parameters["command"]))
            return ComputerActionResult(ok=True, message=f"Opened app: {action.target}")
        if action.intent == "index_folder":
            result = FileIndexer(self.knowledge).index_folder(
                str(action.parameters["path"]),
                max_files=_clean_int(action.parameters.get("max_files"), default=300),
            )
            return ComputerActionResult(
                ok=True,
                message=f"Indexed {result.files_indexed} files from {result.root}.",
                data={
                    "root": result.root,
                    "files_indexed": result.files_indexed,
                    "files_skipped": result.files_skipped,
                },
            )
        if action.intent == "search_files":
            query = str(action.parameters["query"])
            entities = self.knowledge.search_entities(query, entity_type="file")
            return ComputerActionResult(
                ok=True,
                message=f"Found {len(entities)} indexed file matches for '{query}'.",
                data={
                    "entities": [
                        {
                            "id": entity.id,
                            "name": entity.name,
                            "path": entity.metadata.get("path", ""),
                            "snippet": entity.metadata.get("snippet", ""),
                        }
                        for entity in entities
                    ]
                },
            )
        raise ValueError("Unsupported computer action.")


def parse_computer_command(text: str) -> StructuredIntent | None:
    raw = text.strip()
    command = " ".join(raw.lower().replace("\\", "/").split())
    if command in {"open camera", "start camera", "turn on camera"}:
        return _intent("open_camera")
    if command in {"close camera", "stop camera", "turn off camera"}:
        return _intent("close_camera")
    if command in {"open documents", "open my documents", "open documents folder"}:
        return _intent("open_folder", path=str(Path.home() / "Documents"))
    if command.startswith("open this folder:"):
        return _intent("open_folder", path=raw.split(":", 1)[1].strip())
    if command.startswith("open folder:"):
        return _intent("open_folder", path=raw.split(":", 1)[1].strip())
    if command.startswith("open this file:"):
        return _intent("open_file", path=raw.split(":", 1)[1].strip())
    if command.startswith("open file:"):
        return _intent("open_file", path=raw.split(":", 1)[1].strip())
    if command.startswith("index this folder:"):
        return _intent("index_folder", path=raw.split(":", 1)[1].strip())
    if command.startswith("index folder:"):
        return _intent("index_folder", path=raw.split(":", 1)[1].strip())
    if command in {
        "index windows c",
        "index c drive",
        "add c drive to brain",
        "add windows c to brain",
        "add all my files to brain",
        "index all my files",
    }:
        return _intent(
            "index_folder",
            path=Path.home().anchor or "C:\\",
            max_files=5000,
            allow_drive_root=True,
        )
    if command.startswith("search my files for "):
        return _intent("search_files", query=raw[len("search my files for ") :].strip())
    if command.startswith("open "):
        candidate = raw[5:].strip()
        if _app_key(candidate) in parse_allowed_apps(""):
            return _intent("open_app", app_name=candidate)
        # Anything else ("open my resume", "open the budget file") goes to the
        # agent, which can search for the real target before opening it.
        return None
    return None


def parse_allowed_apps(value: str) -> dict[str, str]:
    defaults = {
        "notepad": "notepad",
        "calculator": "calc",
        "calc": "calc",
        "explorer": "explorer",
        "vscode": "code",
        "vs code": "code",
        "chrome": "chrome",
    }
    if not value.strip():
        return defaults
    apps = defaults.copy()
    for item in value.split(","):
        name = item.strip()
        if not name:
            continue
        if "=" in name:
            key, command = name.split("=", 1)
            apps[_app_key(key)] = command.strip()
        else:
            apps[_app_key(name)] = defaults.get(_app_key(name), name)
    return apps


def looks_like_blocked_computer_request(text: str) -> bool:
    """Refuse only shell and command-interpreter requests.

    File creation, editing, moving, and deletion are handled by the agent's
    typed filesystem tools and are no longer blocked here.
    """

    command = " ".join(text.lower().split())
    return any(term in command for term in BLOCKED_COMMAND_TERMS)


def _intent(intent: str, **parameters: str) -> StructuredIntent:
    return StructuredIntent(
        intent=intent,
        language="en",
        confidence=1.0,
        parameters=parameters,
        requires_confirmation=False,
    )


def _clean_path(value: Any) -> str:
    return str(value or "").strip().strip('"').strip("'")


def _clean_name(value: Any) -> str:
    return str(value or "").strip()


def _clean_int(value: Any, *, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, 50000))


def _app_key(value: str) -> str:
    return " ".join(value.lower().strip().split())


def _resolve_named_path(value: str) -> Path:
    normalized = value.strip().lower()
    if normalized in {"documents", "my documents"}:
        return (Path.home() / "Documents").resolve()
    if normalized in {"desktop", "my desktop"}:
        return (Path.home() / "Desktop").resolve()
    if normalized in {"downloads", "my downloads"}:
        return (Path.home() / "Downloads").resolve()
    return Path(value).expanduser().resolve()


def _is_drive_root(path: Path) -> bool:
    resolved = path.resolve()
    return str(resolved) == resolved.anchor


def resolve_app_command(name: str) -> str:
    """Map a friendly app name to a launch command.

    Unknown names pass through unchanged, so any executable on PATH or any
    absolute .exe path can be launched.
    """

    return parse_allowed_apps("").get(_app_key(name), name.strip())


def open_local_path(path: str) -> None:
    """Open any file or folder with its default application."""

    _open_path(Path(path))


def launch_app(name: str) -> None:
    """Launch any application by friendly name, command, or executable path."""

    _open_app(resolve_app_command(name))


def _open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
        return
    subprocess.Popen(["xdg-open", str(path)])


def _open_app(command: str) -> None:
    subprocess.Popen([command], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
