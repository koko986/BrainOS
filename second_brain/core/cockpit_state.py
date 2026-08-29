"""Shared cockpit state helpers for web, desktop, and terminal surfaces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from second_brain.core.config import PROJECT_ROOT
from second_brain.knowledge.service import KnowledgeService


STATE_PATH = PROJECT_ROOT / "data" / "database" / "marlin_cockpit_state.json"


def build_file_extension_chart(knowledge: KnowledgeService) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for entity in knowledge.list_entities("file"):
        path = str(entity.metadata.get("path", ""))
        suffix = Path(path).suffix.lower() or "(none)"
        counts[suffix] = counts.get(suffix, 0) + 1
    top = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:12]
    return {
        "chart_type": "bar",
        "title": "Indexed Files by Extension",
        "labels": [item[0] for item in top],
        "values": [item[1] for item in top],
    }


def save_latest_chart(chart: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"latest_chart": chart}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_latest_chart() -> dict[str, Any] | None:
    if not STATE_PATH.exists():
        return None
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return None
    chart = payload.get("latest_chart")
    return chart if isinstance(chart, dict) else None
