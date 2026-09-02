from __future__ import annotations

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

