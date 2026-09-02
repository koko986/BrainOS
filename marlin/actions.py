"""Typed local computer actions with destructive-action confirmation."""

from __future__ import annotations

import ctypes
import difflib
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus, urlparse

from marlin.storage import MarlinStore
from second_brain.computer.filesystem import (
    FileToolError,
    create_folder,
    find_files,
    grep_files,
    list_folder,
    path_info,
    read_file,
    resolve_path,
)


DESTRUCTIVE_ACTIONS = {
    "append_file", "edit_file", "overwrite_file", "replace_path",
    "move_path", "rename_path", "delete_path", "close_app",
}
PROTECTED_PARTS = {
    "$recycle.bin",
    "boot",
    "program files",
    "program files (x86)",
    "programdata",
    "recovery",
    "system volume information",
    "windows",
}
SITE_SHORTCUTS = {
    "youtube": "https://www.youtube.com/",
    "canva": "https://www.canva.com/",
    "github": "https://github.com/",
    "gmail": "https://mail.google.com/",
    "google": "https://www.google.com/",
    "facebook": "https://www.facebook.com/",
    "instagram": "https://www.instagram.com/",
    "linkedin": "https://www.linkedin.com/",
    "chatgpt": "https://chatgpt.com/",
}


@dataclass(slots=True)
class PendingAction:
    id: str
    name: str
    label: str
    target: str
    risk: str
    arguments: dict[str, Any]
    preview: str = ""
    fingerprint: str = ""
    expires_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "label": self.label,
            "target": self.target,
            "risk": self.risk,
            "arguments": self.arguments,
            "preview": self.preview,
            "expires_at": self.expires_at,
        }


@dataclass(slots=True)
class ActionOutcome:
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    pending: PendingAction | None = None
    client_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "data": self.data,
            "pending": self.pending.to_dict() if self.pending else None,
            "client_action": self.client_action,
        }


class ComputerActionService:
    def __init__(self, store: MarlinStore, *, on_context: Callable[[str, str], None] | None = None):
        self.store = store
        self.on_context = on_context
        self._pending: dict[str, PendingAction] = {}

    def invoke(self, name: str, arguments: dict[str, Any], *, approved: bool = False) -> ActionOutcome:
        try:
            if name in DESTRUCTIVE_ACTIONS and not approved:
                return self._preview(name, arguments)
            outcome = self._execute(name, arguments)
        except (FileToolError, ValueError, OSError) as exc:
            self.store.record_action(name, name.replace("_", " "), self._target(arguments), "failed", {"error": str(exc)})
            return ActionOutcome(False, f"That action failed: {exc}")
        self.store.record_action(name, name.replace("_", " "), self._target(arguments), "complete")
        return outcome

    def approve(self, action_id: str) -> ActionOutcome:
        pending = self._pending.pop(action_id, None)
        if pending is None:
            return ActionOutcome(False, "That confirmation is missing or was already used.")
        if time.time() > pending.expires_at:
            return ActionOutcome(False, "That confirmation expired. Please ask again.")
        if pending.fingerprint and pending.fingerprint != self._fingerprint(Path(pending.target)):
            return ActionOutcome(False, "The target changed after the preview, so I did not modify it.")
        return self.invoke(pending.name, pending.arguments, approved=True)

    def cancel(self, action_id: str) -> ActionOutcome:
        pending = self._pending.pop(action_id, None)
        return ActionOutcome(bool(pending), "Action cancelled." if pending else "Action was not pending.")

    def can_open_app(self, name: str) -> bool:
        try:
            self._app_command(name)
            return True
        except ValueError:
            return False

    def _preview(self, name: str, arguments: dict[str, Any]) -> ActionOutcome:
        target = self._target(arguments)
        path = resolve_path(target) if name != "close_app" else None
        if path is not None:
            self._assert_not_protected(path)
        preview = self._diff_preview(name, arguments, path)
        pending = PendingAction(
            id=uuid.uuid4().hex,
            name=name,
            label=name.replace("_", " ").title(),
            target=str(path) if path is not None else target,
            risk="destructive",
            arguments=dict(arguments),
            preview=preview,
            fingerprint=self._fingerprint(path) if path is not None else "",
            expires_at=time.time() + 120,
        )
        self._pending[pending.id] = pending
        self.store.record_action(name, pending.label, pending.target, "pending")
        return ActionOutcome(True, "Approval is required before I change that target.", pending=pending)

    def _execute(self, name: str, args: dict[str, Any]) -> ActionOutcome:
        if name == "read_file":
            payload = read_file(args.get("path"), max_chars=int(args.get("max_chars", 6000)))
            self._remember("file", payload.path)
            return ActionOutcome(True, f"Read {payload.path}.", payload.to_dict())
        if name == "list_folder":
            data = list_folder(args.get("path"), max_entries=int(args.get("limit", 100)))
            self._remember("folder", str(args.get("path", "")))
            return ActionOutcome(True, f"Listed {args.get('path')}.", data)
        if name == "find_files":
            data = find_files(args.get("path"), str(args.get("pattern", "*")), max_results=int(args.get("limit", 50)))
            return ActionOutcome(True, f"Found {len(data.get('matches', []))} matching paths.", data)
        if name == "grep_files":
            data = grep_files(args.get("path"), str(args.get("query", "")), max_results=int(args.get("limit", 30)))
            return ActionOutcome(True, f"Found {len(data.get('hits', []))} content matches.", data)
        if name == "path_info":
            return ActionOutcome(True, "Path information ready.", path_info(args.get("path")))
        if name == "create_folder":
            path = resolve_path(args.get("path"))
            self._assert_not_protected(path)
            return ActionOutcome(True, create_folder(path))
        if name == "create_file":
            path = resolve_path(args.get("path"))
            self._assert_not_protected(path)
            if path.exists():
                return self._preview("overwrite_file", args)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(args.get("content", "")), encoding="utf-8", newline="")
            self._remember("file", str(path))
            return ActionOutcome(True, f"Created {path}.")
        if name == "overwrite_file":
            path = resolve_path(args.get("path"))
            self._assert_not_protected(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(args.get("content", "")), encoding="utf-8", newline="")
            self._remember("file", str(path))
            return ActionOutcome(True, f"Overwrote {path}.")
        if name == "append_file":
            path = resolve_path(args.get("path"))
            self._assert_not_protected(path)
            with path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(str(args.get("content", "")))
            return ActionOutcome(True, f"Appended text to {path}.")
        if name == "edit_file":
            path = resolve_path(args.get("path"))
            self._assert_not_protected(path)
            text = path.read_text(encoding="utf-8", errors="ignore")
            old = str(args.get("old_text", ""))
            if not old or old not in text:
                raise ValueError("The requested old text was not found.")
            count = -1 if bool(args.get("replace_all")) else 1
            path.write_text(text.replace(old, str(args.get("new_text", "")), count), encoding="utf-8", newline="")
            return ActionOutcome(True, f"Edited {path}.")
        if name in {"move_path", "rename_path"}:
            source = resolve_path(args.get("source") or args.get("path"))
            target = resolve_path(args.get("destination") or args.get("new_path"))
            self._assert_not_protected(source)
            self._assert_not_protected(target)
            if target.exists():
                raise ValueError(f"Destination already exists: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            self._remember("file", str(target))
            return ActionOutcome(True, f"Moved {source} to {target}.")
        if name == "copy_path":
            source = resolve_path(args.get("source"))
            target = resolve_path(args.get("destination"))
            self._assert_not_protected(target)
            if target.exists():
                return self._preview("replace_path", {"source": str(source), "destination": str(target)})
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            return ActionOutcome(True, f"Copied {source} to {target}.")
        if name == "replace_path":
            source = resolve_path(args.get("source"))
            target = resolve_path(args.get("destination"))
            self._assert_not_protected(target)
            if not source.exists() or not target.exists():
                raise ValueError("Both the replacement source and existing target must exist.")
            from send2trash import send2trash
            send2trash(str(target))
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            return ActionOutcome(True, f"Replaced {target}; its previous version is in the Recycle Bin.")
        if name == "delete_path":
            path = resolve_path(args.get("path"))
            self._assert_not_protected(path)
            from send2trash import send2trash
            send2trash(str(path))
            return ActionOutcome(True, f"Moved {path} to the Recycle Bin.")
        if name == "open_path":
            path = resolve_path(args.get("path"))
            if not path.exists():
                raise ValueError(f"Path does not exist: {path}")
            os.startfile(str(path))  # type: ignore[attr-defined]
            self._remember("folder" if path.is_dir() else "file", str(path))
            return ActionOutcome(True, f"Opened {path}.")
        if name == "open_app":
            app = str(args.get("app", "")).strip()
            command = self._app_command(app)
            if Path(command).suffix.lower() == ".lnk":
                os.startfile(command)  # type: ignore[attr-defined]
            else:
                subprocess.Popen([command], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._remember("app", app)
            return ActionOutcome(True, f"Opened {app}.")
        if name == "open_url":
            url = self.resolve_website(str(args.get("url") or args.get("site") or ""))
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("Only valid HTTP or HTTPS websites can be opened.")
            chrome = self._app_command("chrome")
            subprocess.Popen([chrome, "--new-tab", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._remember("website", url)
            return ActionOutcome(True, f"Opened {parsed.netloc} in Chrome.", {"url": url})
        if name == "close_app":
            app = str(args.get("app", "")).strip().lower().removesuffix(".exe")
            import psutil
            closed = 0
            for process in psutil.process_iter(["name"]):
                if str(process.info.get("name") or "").lower().removesuffix(".exe") == app:
                    process.terminate()
                    closed += 1
            return ActionOutcome(bool(closed), f"Closed {closed} {app} process(es).")
        if name == "media_control":
            command = str(args.get("command", "play_pause"))
            self._media_key(command)
            return ActionOutcome(True, f"Media command sent: {command.replace('_', ' ')}.")
        if name == "open_camera":
            return ActionOutcome(True, "Opening the local camera preview.", client_action="open_camera")
        if name == "close_camera":
            return ActionOutcome(True, "Camera preview closed.", client_action="close_camera")
        raise ValueError(f"Unsupported local action: {name}")

    def _diff_preview(self, name: str, args: dict[str, Any], path: Path | None) -> str:
        if path is None or name not in {"edit_file", "overwrite_file", "append_file"}:
            return ""
        before = path.read_text(encoding="utf-8", errors="ignore") if path.exists() and path.is_file() else ""
        if name == "edit_file":
            old = str(args.get("old_text", ""))
            after = before.replace(old, str(args.get("new_text", "")), -1 if args.get("replace_all") else 1)
        elif name == "append_file":
            after = before + str(args.get("content", ""))
        else:
            after = str(args.get("content", ""))
        return "".join(
            difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile="before", tofile="after", n=2)
        )[:5000]

    @staticmethod
    def _fingerprint(path: Path | None) -> str:
        if path is None or not path.exists():
            return "missing"
        stat = path.stat()
        sample = b""
        if path.is_file():
            try:
                with path.open("rb") as handle:
                    sample = handle.read(65536)
            except OSError:
                sample = b""
        return hashlib.sha256(f"{stat.st_size}:{stat.st_mtime_ns}".encode() + sample).hexdigest()

    @staticmethod
    def _assert_not_protected(path: Path) -> None:
        resolved = path.resolve(strict=False)
        parts = {part.lower() for part in resolved.parts}
        if parts.intersection(PROTECTED_PARTS) or str(resolved) == resolved.anchor:
            raise ValueError(f"Protected Windows path cannot be modified: {resolved}")

    @staticmethod
    def _target(args: dict[str, Any]) -> str:
        return str(args.get("path") or args.get("source") or args.get("app") or args.get("url") or args.get("site") or args.get("command") or "")

    def _remember(self, kind: str, value: str) -> None:
        self.store.add_context(kind, value)
        if self.on_context:
            self.on_context(kind, value)

    @classmethod
    def _app_command(cls, name: str) -> str:
        aliases = {
            "notepad": "notepad.exe",
            "calculator": "calc.exe",
            "calc": "calc.exe",
            "explorer": "explorer.exe",
            "vscode": "code",
            "vs code": "code",
            "chrome": "chrome.exe",
            "google chrome": "chrome.exe",
            "edge": "msedge.exe",
            "microsoft edge": "msedge.exe",
        }
        normalized = " ".join(name.lower().split()).removesuffix(".exe")
        if not normalized or any(char in normalized for char in "\\/:*?\"<>|"):
            raise ValueError(f"Invalid application name: {name}")
        candidate = aliases.get(normalized, name.strip())
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
        registry = cls._registered_app_path(candidate)
        if registry:
            return registry
        shortcut = cls._start_menu_shortcut(normalized)
        if shortcut:
            return shortcut
        raise ValueError(f"Application is not installed or registered on MARLIN's allow-list: {name}")

    @staticmethod
    def resolve_website(value: str) -> str:
        target = " ".join(value.strip().split())
        known = SITE_SHORTCUTS.get(target.lower())
        if known:
            return known
        if re.match(r"^https?://", target, re.I):
            return target
        if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}(?:/.*)?$", target, re.I):
            return "https://" + target
        return "https://www.google.com/search?q=" + quote_plus(target)

    @staticmethod
    def _registered_app_path(candidate: str) -> str | None:
        if sys.platform != "win32":
            return None
        try:
            import winreg
        except ImportError:
            return None
        executable = candidate if candidate.lower().endswith(".exe") else candidate + ".exe"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for key_name in (
                rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}",
                rf"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{executable}",
            ):
                try:
                    with winreg.OpenKey(hive, key_name) as key:
                        value = str(winreg.QueryValue(key, None))
                    if Path(value).exists():
                        return value
                except OSError:
                    continue
        return None

    @staticmethod
    def _start_menu_shortcut(normalized: str) -> str | None:
        roots = [
            Path(os.getenv("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
            Path(os.getenv("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        ]
        wanted = normalized.replace(" ", "")
        for root in roots:
            if not root.exists():
                continue
            try:
                for shortcut in root.rglob("*.lnk"):
                    stem = shortcut.stem.lower().replace(" ", "")
                    if stem == wanted:
                        return str(shortcut)
            except OSError:
                continue
        return None

    @staticmethod
    def _media_key(command: str) -> None:
        if sys.platform != "win32":
            raise ValueError("Media controls are only available on Windows.")
        keys = {
            "play": 0xB3,
            "pause": 0xB3,
            "play_pause": 0xB3,
            "stop": 0xB2,
            "next": 0xB0,
            "previous": 0xB1,
            "volume_up": 0xAF,
            "volume_down": 0xAE,
            "mute": 0xAD,
        }
        key = keys.get(command)
        if key is None:
            raise ValueError(f"Unknown media command: {command}")
        ctypes.windll.user32.keybd_event(key, 0, 0, 0)
        ctypes.windll.user32.keybd_event(key, 0, 2, 0)
