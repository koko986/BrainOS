"""Persistent JARVIS-style states, alarms, reminders, briefings, and follow-ups."""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from marlin.config import MarlinSettings
from marlin.events import EventBus
from marlin.storage import MarlinStore
from second_brain.reasoning.prolog_engine import PrologUnavailable
from second_brain.reasoning.service import ReasoningService


VALID_STATES = {"active", "standby", "listening", "thinking", "speaking", "executing"}


class AssistantState:
    def __init__(self, store: MarlinStore, events: EventBus):
        self.store = store
        self.events = events
        self._lock = threading.Lock()
        self._value = store.get_state("mode", "active")
        if self._value not in VALID_STATES:
            self._value = "active"

    @property
    def value(self) -> str:
        with self._lock:
            return self._value

    def set(self, value: str) -> str:
        if value not in VALID_STATES:
            raise ValueError(f"Unknown assistant state: {value}")
        with self._lock:
            self._value = value
        self.store.set_state("mode", value)
        self.events.publish("assistant.state", state=value)
        return value


class RoutineService:
    def __init__(
        self,
        settings: MarlinSettings,
        store: MarlinStore,
        reasoning: ReasoningService,
        state: AssistantState,
        events: EventBus,
        on_alarm: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.settings = settings
        self.store = store
        self.reasoning = reasoning
        self.state = state
        self.events = events
        self.on_alarm = on_alarm
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_fired_alarm_id = ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._alarm_loop, name="marlin-alarms", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def handle(self, text: str) -> str | None:
        command = " ".join(str(text or "").lower().replace(",", " ").split())
        if command in {"marlin stand by", "marlin standby", "stand by", "standby", "go to sleep"}:
            self.state.set("standby")
            return "Standing by. Say MARLIN, wake up when you need me."
        if command in {"marlin wake up", "wake up marlin", "wake up"}:
            self.state.set("active")
            return "Good day. MARLIN is awake and ready."
        if command in {"morning briefing", "good morning", "brief me", "what should i work on today"}:
            return self.morning_briefing()
        if command in {"list alarms", "show alarms", "my alarms"}:
            alarms = self.store.list_alarms()
            if not alarms:
                return "You have no active alarms."
            return "Active alarms: " + "; ".join(f"{item['label']} at {self._local_time(item['due_at'])}" for item in alarms[:8])
        if command in {"list reminders", "show reminders", "my reminders"}:
            reminders = self.store.list_reminders()
            if not reminders:
                return "You have no pending reminders."
            return "Pending reminders: " + "; ".join(str(item["text"]) for item in reminders[:8])
        if command in {"snooze", "five more minutes", "snooze five minutes"} and self.last_fired_alarm_id:
            self.store.snooze_alarm(self.last_fired_alarm_id, 5)
            return "Certainly. I will wake you again in five minutes."

        alarm = self._parse_alarm(text)
        if alarm:
            item = self.store.add_alarm(alarm[0], alarm[1])
            self.events.publish("alarm.created", alarm=item)
            return f"Alarm set for {alarm[1].astimezone().strftime('%I:%M %p')} with label {alarm[0]}."

        reminder = self._parse_reminder(text)
        if reminder:
            item = self.store.add_reminder(reminder[0], reminder[1])
            self.events.publish("reminder.created", reminder=item)
            if reminder[1]:
                return f"Reminder saved for {reminder[1].astimezone().strftime('%I:%M %p')}: {reminder[0]}."
            return f"Reminder saved: {reminder[0]}."
        return None

    def morning_briefing(self) -> str:
        now = datetime.now().astimezone()
        parts = [f"Good morning. It is {now.strftime('%A, %B %d, %I:%M %p')}."]
        weather = self._weather()
        if weather:
            parts.append(weather)
        alarms = self.store.list_alarms()
        if alarms:
            parts.append(f"You have {len(alarms)} active alarm{'s' if len(alarms) != 1 else ''}.")
        reminders = self.store.list_reminders()
        if reminders:
            parts.append("Your next reminder is: " + str(reminders[0]["text"]) + ".")
        try:
            tasks = self.reasoning.high_priority_tasks()
        except PrologUnavailable:
            tasks = []
        if tasks:
            parts.append("Your Prolog priority is " + tasks[0].name + ".")
        context = self.store.recent_context(limit=5)
        project = next((item for item in context if item["kind"] == "project"), None)
        recent_file = next((item for item in context if item["kind"] == "file"), None)
        if project:
            parts.append("Your last active project was " + str(project["value"]) + ".")
        elif recent_file:
            parts.append("Your most recent file was " + str(recent_file["value"]) + ".")
        return " ".join(parts)

    def resolve_follow_up(self, text: str) -> tuple[str, dict[str, Any]] | None:
        command = " ".join(text.lower().split())
        if command in {"open it", "open it again", "open that again"}:
            context = self.store.recent_context(limit=10)
            item = next((value for value in context if value["kind"] in {"file", "folder"}), None)
            if item:
                return "open_path", {"path": item["value"]}
        if command in {"close it", "close that", "close them"}:
            apps = self.store.recent_context("app", limit=3)
            if apps:
                return "close_app", {"app": apps[0]["value"]}
        if command in {"continue that project", "continue my last project"}:
            projects = self.store.recent_context("project", limit=1)
            if projects:
                return "open_path", {"path": projects[0]["value"]}
        return None

    def _alarm_loop(self) -> None:
        while not self._stop.wait(1.0):
            for alarm in self.store.due_alarms():
                self.last_fired_alarm_id = str(alarm["id"])
                self.store.mark_alarm_fired(self.last_fired_alarm_id)
                self.events.publish("alarm.fired", alarm=alarm)
                if self.on_alarm:
                    self.on_alarm(alarm)

    @staticmethod
    def _parse_alarm(text: str) -> tuple[str, datetime] | None:
        match = re.search(r"(?:set (?:an )?alarm|wake me)(?: up)?(?: called ([\w -]+?))?\s+(?:in|for)\s+(\d+)\s+(minute|minutes|hour|hours)", text, re.I)
        if match:
            amount = int(match.group(2))
            delta = timedelta(hours=amount) if match.group(3).lower().startswith("hour") else timedelta(minutes=amount)
            return (match.group(1) or "Alarm").strip(), datetime.now().astimezone() + delta
        match = re.search(r"set (?:an )?alarm(?: called ([\w -]+?))?\s+(?:at|for)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text, re.I)
        if not match:
            return None
        hour = int(match.group(2))
        minute = int(match.group(3) or 0)
        suffix = (match.group(4) or "").lower()
        if suffix == "pm" and hour < 12:
            hour += 12
        if suffix == "am" and hour == 12:
            hour = 0
        now = datetime.now().astimezone()
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due <= now:
            due += timedelta(days=1)
        return (match.group(1) or "Alarm").strip(), due

    @staticmethod
    def _parse_reminder(text: str) -> tuple[str, datetime | None] | None:
        match = re.search(r"remind me (?:to |about )(.+?)\s+in\s+(\d+)\s+(minute|minutes|hour|hours)$", text, re.I)
        if match:
            amount = int(match.group(2))
            delta = timedelta(hours=amount) if match.group(3).lower().startswith("hour") else timedelta(minutes=amount)
            return match.group(1).strip(), datetime.now().astimezone() + delta
        match = re.search(r"remind me (?:to |about )(.+)$", text, re.I)
        return (match.group(1).strip(), None) if match else None

    def _weather(self) -> str:
        if not self.settings.weather_enabled:
            return ""
        query = urlencode(
            {
                "latitude": self.settings.weather_latitude,
                "longitude": self.settings.weather_longitude,
                "current": "temperature_2m,weather_code",
                "timezone": "auto",
            }
        )
        try:
            with urlopen(f"https://api.open-meteo.com/v1/forecast?{query}", timeout=2.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            current = payload.get("current") or {}
            temperature = current.get("temperature_2m")
            if temperature is None:
                return ""
            return f"The local temperature is {temperature} degrees Celsius."
        except (OSError, URLError, TimeoutError, json.JSONDecodeError):
            return ""

    @staticmethod
    def _local_time(value: str) -> str:
        return datetime.fromisoformat(value).astimezone().strftime("%b %d at %I:%M %p")
