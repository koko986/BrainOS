"""Shared local audit log for MARLIN cockpit surfaces."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from second_brain.core.config import PROJECT_ROOT


AUDIT_PATH = PROJECT_ROOT / "data" / "database" / "marlin_audit.jsonl"


def record_audit(kind: str, label: str, target: str, status: str) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    item = {
        "time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "kind": kind,
        "label": label,
        "target": target,
        "status": status,
    }
    with AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def read_audit(limit: int = 50) -> list[dict[str, Any]]:
    if not AUDIT_PATH.exists():
        return []
    lines = AUDIT_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
    items: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            items.append(value)
    return items
