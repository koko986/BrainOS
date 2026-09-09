"""Windows Task Scheduler integration for reminders after cockpit exit."""

from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

from marlin.config import MarlinSettings
from marlin.notifications import play_notification_sound
from marlin.storage import MarlinStore


TASK_NAME = "MARLIN Reminder Runner"


def register_runner() -> tuple[bool, str]:
    main = Path(__file__).resolve().parents[1] / "main.py"
    action = f'"{sys.executable}" "{main}" scheduler-tick'
    result = subprocess.run(
        ["schtasks.exe", "/Create", "/TN", TASK_NAME, "/TR", action, "/SC", "MINUTE", "/MO", "1", "/F"],
        capture_output=True, text=True, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    message = (result.stdout or result.stderr).strip()
    return result.returncode == 0, message


def runner_status() -> tuple[bool, str]:
    result = subprocess.run(
        ["schtasks.exe", "/Query", "/TN", TASK_NAME], capture_output=True, text=True,
        check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.returncode == 0, (result.stdout or result.stderr).strip()


def run_once(settings: MarlinSettings | None = None) -> int:
    settings = settings or MarlinSettings.from_env()
    store = MarlinStore(settings.database_path); store.migrate()
    due = store.claim_due_reminders() + store.claim_due_alarms()
    for item in due:
        text = item.get("text") or item.get("label") or "MARLIN reminder"
        play_notification_sound()
        try:
            ctypes.windll.user32.MessageBoxW(0, text, "MARLIN", 0x00001000 | 0x40)
        except Exception:
            print(f"MARLIN reminder: {text}")
    return 0
