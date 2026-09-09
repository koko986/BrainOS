from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marlin.config import MarlinSettings
from marlin.events import EventBus
from marlin.file_understanding import FileUnderstandingService
from marlin.planner import ScheduleService
from marlin.preferences import PreferenceService
from marlin.research import InternetResearchService
from marlin.runtime import MarlinRuntime
from marlin.storage import MarlinStore
from marlin.web import create_app
from second_brain.database.connection import initialize_database
from second_brain.knowledge.service import KnowledgeService
from second_brain.reasoning.prolog_engine import PrologEngine
from second_brain.reasoning.service import ReasoningService


def settings(tmp_path: Path) -> MarlinSettings:
    return MarlinSettings(
        database_path=tmp_path / "brain.db", prolog_dir=Path("prolog").resolve(),
        graph_root=tmp_path, auto_index_c_drive=False, wake_word_enabled=False,
        voice_output=False,
    )


def services(tmp_path: Path):
    config = settings(tmp_path)
    initialize_database(config.database_path)
    store = MarlinStore(config.database_path); store.migrate(backup=False)
    reasoning = ReasoningService(KnowledgeService(config.database_path), config.prolog_dir)
    reasoning.engine.load()
    return config, store, reasoning, EventBus()


def test_clpfd_schedule_respects_busy_time_and_dependencies(tmp_path: Path) -> None:
    _, store, reasoning, events = services(tmp_path)
    schedule = ScheduleService(store, reasoning, events)
    first = store.add_schedule_item("Foundation", duration_minutes=60, priority=80)
    second = store.add_schedule_item("Dependent", duration_minutes=60, priority=100, metadata={"depends_on": [first["id"]]})
    today = datetime.now().astimezone()
    schedule.add_event("Meeting", today.replace(hour=9, minute=0), today.replace(hour=10, minute=0))
    plan = schedule.plan_day(today)
    blocks = {block["item_id"]: block for block in plan["blocks"]}
    assert datetime.fromisoformat(blocks[first["id"]]["start_at"]).hour >= 10
    assert blocks[first["id"]]["end_at"] <= blocks[second["id"]]["start_at"]
    assert not store.schedule_blocks_for_date(today.date().isoformat())
    schedule.apply_plan(plan["id"])
    assert len(store.schedule_blocks_for_date(today.date().isoformat())) == 2


def test_preferences_are_explicit_or_need_three_observations(tmp_path: Path) -> None:
    _, store, _, events = services(tmp_path)
    memory = PreferenceService(store, events)
    assert memory.observe("My API key is secret") is None
    assert memory.observe("I prefer coding in the morning")["active"] == 1
    assert memory.observe("I usually use VS Code")["active"] == 0
    assert memory.observe("I usually use VS Code")["active"] == 0
    assert memory.observe("I usually use VS Code")["active"] == 1
    assert memory.handle("forget preference about morning")["count"] == 1


def test_file_analysis_is_incremental_and_scoped(tmp_path: Path) -> None:
    config, store, reasoning, _ = services(tmp_path)
    source = tmp_path / "sample.py"; source.write_text("import json\nclass BrainGraph:\n    pass\n", encoding="utf-8")
    store.upsert_file_search("file_sample", str(source), source.name, "", "")
    files = FileUnderstandingService(config, store, reasoning)
    first = files.analyze(str(source)); second = files.analyze(str(source))
    assert "Python" in first["technologies"]
    assert second["cached"] is True
    outside = tmp_path.parent / "outside-secret.txt"; outside.write_text("secret", encoding="utf-8")
    with pytest.raises(PermissionError): files.analyze(str(outside))


def test_research_blocks_private_networks_and_ranks_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, store, reasoning, events = services(tmp_path)
    research = InternetResearchService(store, reasoning, events)
    assert not research.url_allowed("http://127.0.0.1/private")
    monkeypatch.setattr(research, "url_allowed", lambda url: True)
    monkeypatch.setattr(research, "_robots_allowed", lambda url: False)
    monkeypatch.setattr(research, "_browser_search", lambda query: [
        {"title": "Official CLPFD", "url": "https://www.swi-prolog.org/pldoc/man?section=clpfd-intro", "snippet": "Constraint logic programming supports scheduling."},
        {"title": "Python robots", "url": "https://docs.python.org/3/library/urllib.robotparser.html", "snippet": "RobotFileParser reads robots.txt."},
    ])
    run = research.search("Prolog scheduling")
    assert run["sources"] and run["sources"][0]["title"] == "Official CLPFD"
    assert all(source["prolog_score"] is not None for source in run["sources"])


def test_research_accepts_natural_internet_search_phrases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, store, reasoning, events = services(tmp_path)
    research = InternetResearchService(store, reasoning, events)
    captured = []
    monkeypatch.setattr(research, "search", lambda query: captured.append(query) or {"query": query})
    for command in (
        "search on internet for SWI Prolog",
        "search the web for Python documentation",
        "internet search local AI",
        "look up MARLIN architecture",
        "find Prolog scheduling on the internet",
    ):
        assert research.handle(command)
    assert captured == [
        "SWI Prolog", "Python documentation", "local AI",
        "MARLIN architecture", "Prolog scheduling",
    ]
    clarification = research.handle("give me more details, search on internet")
    assert clarification and "clarification" in clarification


def test_agent_apis_require_token_and_preview_before_apply(tmp_path: Path) -> None:
    runtime = MarlinRuntime(settings(tmp_path), start_background=False)
    app = create_app(runtime); client = TestClient(app)
    assert client.post("/api/schedule/plan", json={}).status_code == 403
    token = app.state.token; headers = {"X-Marlin-Token": token}
    created = client.post("/api/schedule/items", headers=headers, json={"title": "Write report", "duration_minutes": 60}).json()
    plan = client.post("/api/schedule/plan", headers=headers, json={}).json()
    assert plan["status"] == "preview" and plan["blocks"][0]["item_id"] == created["id"]
    assert runtime.store.schedule_blocks_for_date(plan["plan_date"]) == []
    assert client.post(f"/api/schedule/plans/{plan['id']}/apply", headers=headers).status_code == 200
    runtime.shutdown()


def test_natural_reminder_and_timed_schedule_are_saved_for_cockpit(tmp_path: Path) -> None:
    runtime = MarlinRuntime(settings(tmp_path), start_background=False)
    reminder = runtime.command("set a reminder to submit my report at 8 pm tomorrow")
    scheduled = runtime.command("set a schedule for project meeting at 3 pm tomorrow")
    state = runtime.status()
    assert reminder["message"].startswith("Reminder saved for")
    assert state["reminders"][0]["text"] == "submit my report"
    assert scheduled["message"].startswith("Schedule event saved: project meeting on ")
    assert state["schedule_items"][0]["title"] == "project meeting"
    assert state["schedule_items"][0]["start_at"] is not None
    runtime.shutdown()


def test_time_first_schedule_command_is_persisted_without_llm(tmp_path: Path) -> None:
    runtime = MarlinRuntime(settings(tmp_path), start_background=False)
    result = runtime.command("add to schedule for tomorrow 10 pm dinner")
    items = runtime.store.list_schedule_items()
    assert result["message"].startswith("Schedule event saved: dinner on ")
    assert items[0]["title"] == "dinner"
    assert datetime.fromisoformat(items[0]["start_at"]).astimezone().hour == 22
    runtime.shutdown()


def test_schedule_command_keeps_day_before_time(tmp_path: Path) -> None:
    runtime = MarlinRuntime(settings(tmp_path), start_background=False)
    result = runtime.command("schedule dinner tomorrow at 10 pm")
    item = runtime.store.list_schedule_items()[0]
    scheduled = datetime.fromisoformat(item["start_at"]).astimezone()
    assert result["ok"]
    assert item["title"] == "dinner"
    assert scheduled.date() == (datetime.now().astimezone() + timedelta(days=1)).date()
    assert scheduled.hour == 22
    runtime.shutdown()


def test_incomplete_schedule_command_asks_for_details(tmp_path: Path) -> None:
    runtime = MarlinRuntime(settings(tmp_path), start_background=False)
    result = runtime.command("add schedule")
    assert not result["ok"]
    assert "event and time" in result["message"]
    assert runtime.store.list_schedule_items() == []
    runtime.shutdown()


def test_cockpit_renders_unplanned_schedule_items() -> None:
    javascript = Path("marlin/ui/app.js").read_text(encoding="utf-8")
    markup = Path("marlin/ui/index.html").read_text(encoding="utf-8")
    assert "state.schedule_items || []" in javascript
    assert "schedule.item.created" in javascript
    assert "reminder.created" in javascript
    assert 'id="openCamera"' not in markup
