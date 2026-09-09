from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from marlin.config import MarlinSettings
from marlin.events import EventBus
from marlin.routine import AssistantState, RoutineService
from marlin.storage import MarlinStore
from second_brain.database.connection import initialize_database
from second_brain.knowledge.service import KnowledgeService
from second_brain.reasoning.service import ReasoningService


def make_routine(tmp_path):
    database = tmp_path / "brain.db"
    initialize_database(database)
    store = MarlinStore(database)
    store.migrate(backup=False)
    events = EventBus()
    state = AssistantState(store, events)
    settings = MarlinSettings(database_path=database, weather_enabled=False, voice_output=False, auto_index_c_drive=False)
    reasoning = ReasoningService(KnowledgeService(database), settings.prolog_dir)
    return store, state, RoutineService(settings, store, reasoning, state, events)


def test_standby_wake_alarm_reminder_and_snooze(tmp_path):
    store, state, routine = make_routine(tmp_path)
    assert "Standing by" in routine.handle("MARLIN, stand by")
    assert state.value == "standby"
    assert "awake" in routine.handle("MARLIN, wake up").lower()
    assert state.value == "active"
    assert "Alarm set" in routine.handle("set an alarm called focus in 5 minutes")
    assert store.list_alarms()[0]["label"] == "focus"
    assert "Reminder saved" in routine.handle("remind me to review Prolog")
    assert store.list_reminders()[0]["text"] == "review Prolog"
    routine.last_fired_alarm_id = store.list_alarms()[0]["id"]
    assert "five minutes" in routine.handle("five more minutes")


def test_follow_up_uses_recent_local_context(tmp_path):
    store, _state, routine = make_routine(tmp_path)
    target = str(tmp_path / "notes.txt")
    store.add_context("file", target)
    assert routine.resolve_follow_up("open it again") == ("open_path", {"path": target})


def test_add_reminder_follow_up_uses_latest_schedule(tmp_path):
    store, _state, routine = make_routine(tmp_path)
    due = datetime.now().astimezone() + timedelta(hours=2)
    store.add_schedule_item("Exam", kind="event", start_at=due.isoformat(), end_at=(due + timedelta(hours=1)).isoformat(), fixed=True)
    reply = routine.handle("also add reminder")
    reminder = store.list_reminders()[0]
    assert reply and "Reminder saved for" in reply
    assert reminder["text"] == "Exam"
    assert datetime.fromisoformat(reminder["due_at"]).astimezone().replace(microsecond=0) == due.replace(microsecond=0)


def test_reminder_time_first_and_day_before_clock(tmp_path):
    store, _state, routine = make_routine(tmp_path)
    assert routine.handle("set reminder for tomorrow 10 am exam")
    assert routine.handle("remind me to study tomorrow at 11 am")
    reminders = store.list_reminders()
    assert {item["text"] for item in reminders} == {"exam", "study"}
    assert all(datetime.fromisoformat(item["due_at"]).astimezone().date() > datetime.now().astimezone().date() for item in reminders)


def test_due_reminder_inv_delivered_to_callback(tmp_path):
    store, state, _routine = make_routine(tmp_path)
    delivered = []
    routine = RoutineService(
        MarlinSettings(database_path=store.database_path, weather_enabled=False, voice_output=False, auto_index_c_drive=False),
        store, _routine.reasoning, state, _routine.events, on_reminder=delivered.append,
    )
    store.add_reminder("Check MARLIN", datetime.now(UTC) - timedelta(seconds=1))
    routine.start()
    deadline = time.monotonic() + 3
    while not delivered and time.monotonic() < deadline:
        time.sleep(.05)
    routine.stop()
    assert delivered[0]["text"] == "Check MARLIN"
