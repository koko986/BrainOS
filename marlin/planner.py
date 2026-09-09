"""Local schedule storage and SWI-Prolog CLP(FD) planning."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import Any

from marlin.events import EventBus
from marlin.storage import MarlinStore
from second_brain.reasoning.prolog_engine import PrologUnavailable
from second_brain.reasoning.service import ReasoningService


PERIODS = {
    "morning": (9 * 60, 12 * 60),
    "afternoon": (12 * 60, 18 * 60),
    "evening": (16 * 60, 18 * 60),
}


class ScheduleService:
    def __init__(self, store: MarlinStore, reasoning: ReasoningService, events: EventBus):
        self.store = store
        self.reasoning = reasoning
        self.events = events

    def add_task(
        self,
        title: str,
        *,
        duration_minutes: int = 60,
        deadline: datetime | None = None,
        priority: int = 50,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        item = self.store.add_schedule_item(
            title.strip(), duration_minutes=self._slot_duration(duration_minutes),
            deadline_at=deadline.isoformat(timespec="seconds") if deadline else None,
            priority=priority, project_id=project_id,
        )
        self.events.publish("schedule.item.created", item=item)
        return item

    def add_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        *,
        recurrence: str = "none",
    ) -> dict[str, Any]:
        if end <= start:
            raise ValueError("A schedule event must end after it starts.")
        item = self.store.add_schedule_item(
            title.strip(), kind="event", start_at=start.isoformat(timespec="seconds"),
            end_at=end.isoformat(timespec="seconds"),
            duration_minutes=int((end - start).total_seconds() // 60), fixed=True,
            recurrence=recurrence,
        )
        self.events.publish("schedule.item.created", item=item)
        return item

    def plan_day(self, day: datetime | None = None) -> dict[str, Any]:
        target = (day or datetime.now().astimezone()).astimezone()
        date_text = target.date().isoformat()
        if target.weekday() >= 5:
            explanation = {"engine": "SWI-Prolog CLP(FD)", "rules": ["weekday_work_window"],
                           "unscheduled": [], "message": "The default planner does not place work on weekends."}
            return self.store.create_schedule_plan(date_text, [], explanation)

        items = self.store.list_schedule_items()
        tasks = [item for item in items if item["kind"] == "task" and self._eligible(item, target)]
        tasks = self._order_tasks(tasks, target)
        fixed = [item for item in items if item["fixed"] and self._event_occurs(item, target)]
        applied = self.store.schedule_blocks_for_date(date_text)
        busy = [self._minutes(item["start_at"], item["end_at"]) for item in fixed]
        busy += [self._minutes(item["start_at"], item["end_at"]) for item in applied]
        preferences = self.store.list_preferences(active_only=True)
        preferred_period = next((p["value"].lower() for p in preferences if p["category"] == "preferred_period"), "")
        engine = self.reasoning.engine
        engine.clear_agent_facts()
        for preference in preferences:
            category = self._atom(preference["category"])
            value = self._atom(preference["value"])
            engine.assert_agent_fact("preference_fact", [category, value, int(float(preference["confidence"]) * 100)])

        scheduled: list[dict[str, Any]] = []
        scheduled_by_item: dict[str, tuple[int, int]] = {}
        unscheduled: list[dict[str, str]] = []
        for item in tasks[:50]:
            duration = self._slot_duration(int(item["duration_minutes"]))
            earliest, latest = 9 * 60, 18 * 60
            if preferred_period in PERIODS:
                earliest, latest = PERIODS[preferred_period]
            deadline = self._parse_iso(item.get("deadline_at"))
            dependencies = item.get("metadata", {}).get("depends_on", [])
            if isinstance(dependencies, str):
                dependencies = [dependencies]
            dependency_ends = [scheduled_by_item[dependency][1] for dependency in dependencies if dependency in scheduled_by_item]
            if dependency_ends:
                earliest = max(earliest, max(dependency_ends))
            if deadline and deadline.date() == target.date():
                latest = min(latest, deadline.hour * 60 + deadline.minute)
            try:
                result = self.reasoning.engine.plan_day_slots([duration], [earliest], [latest], busy)
            except PrologUnavailable:
                raise
            if not result:
                unscheduled.append({"item_id": item["id"], "title": item["title"], "reason": "No valid non-overlapping slot before the deadline."})
                continue
            start_minute, end_minute = result[0]
            busy.append((start_minute, end_minute))
            scheduled_by_item[item["id"]] = (start_minute, end_minute)
            start = datetime.combine(target.date(), time.min, target.tzinfo) + timedelta(minutes=start_minute)
            end = datetime.combine(target.date(), time.min, target.tzinfo) + timedelta(minutes=end_minute)
            reasons = ["Prolog found a non-overlapping slot inside 09:00-18:00."]
            task_atom = "task_" + item["id"]
            if item.get("project_id"):
                engine.assert_agent_fact("task_project", [task_atom, self._atom(item["project_id"])])
            influences = engine.preference_influences(task_atom)
            if preferred_period:
                reasons.append(f"Your preferred work period is {preferred_period}.")
            if deadline:
                reasons.append(f"The task deadline is {deadline.astimezone().strftime('%d %b %H:%M')}.")
            if dependency_ends:
                reasons.append("Prolog placed this after its scheduled dependencies.")
            for category, value in influences:
                reasons.append(f"Preference {category.replace('_', ' ')} = {value.replace('_', ' ')} influenced this slot.")
            scheduled.append({
                "item_id": item["id"], "title": item["title"],
                "start_at": start.isoformat(timespec="seconds"), "end_at": end.isoformat(timespec="seconds"),
                "explanation": {"predicate": "schedule_tasks/6", "reasons": reasons},
            })

        explanation = {
            "engine": "SWI-Prolog CLP(FD)",
            "predicate": "schedule_tasks(Durations,Earliest,Latest,Busy,Starts,Ends)",
            "rules": ["working_hours", "serialized_tasks", "fixed_event_avoidance", "deadline", "preferred_period"],
            "unscheduled": unscheduled,
        }
        plan = self.store.create_schedule_plan(date_text, scheduled, explanation)
        self.events.publish("schedule.plan.preview", plan=plan)
        return plan

    def apply_plan(self, plan_id: str) -> dict[str, Any] | None:
        plan = self.store.apply_schedule_plan(plan_id)
        if plan:
            self.events.publish("schedule.plan.applied", plan=plan)
        return plan

    def conflicts(self, day: datetime | None = None) -> list[dict[str, Any]]:
        target = (day or datetime.now().astimezone()).date().isoformat()
        items = [item for item in self.store.list_schedule_items() if str(item.get("start_at") or "").startswith(target)]
        items += self.store.schedule_blocks_for_date(target)
        conflicts = []
        for index, first in enumerate(items):
            if not first.get("start_at") or not first.get("end_at"):
                continue
            first_interval = self._minutes(first["start_at"], first["end_at"])
            for second in items[index + 1:]:
                if not second.get("start_at") or not second.get("end_at"):
                    continue
                second_interval = self._minutes(second["start_at"], second["end_at"])
                if self.reasoning.engine.intervals_conflict(first_interval, second_interval):
                    conflicts.append({"first": first.get("title"), "second": second.get("title"), "date": target})
        self.events.publish("prolog.result", query="interval_conflict/4", conflicts=conflicts)
        return conflicts

    def explain_block(self, block_id: str) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM schedule_blocks WHERE id=?", (block_id,)).fetchone()
        return self.store._json_row(row, "explanation_json", "explanation") if row else None

    def handle(self, text: str) -> dict[str, Any] | None:
        command = " ".join(text.strip().split())
        lowered = command.lower().rstrip(".?")
        if lowered in {"plan my day", "plan today", "make my schedule", "schedule my day"}:
            return {"kind": "preview", "plan": self.plan_day()}
        if lowered in {"apply plan", "apply schedule", "accept plan"}:
            plan = self.store.latest_schedule_plan(status="preview")
            return {"kind": "applied", "plan": self.apply_plan(plan["id"]) if plan else None}
        if lowered in {"discard plan", "discard schedule"}:
            plan = self.store.latest_schedule_plan(status="preview")
            if not plan:
                return {"kind": "discarded", "plan": None}
            with self.store.connect() as connection:
                connection.execute("UPDATE schedule_plans SET status='discarded' WHERE id=?", (plan["id"],))
            self.events.publish("schedule.plan.discarded", plan_id=plan["id"])
            return {"kind": "discarded", "plan": plan}
        if lowered in {"show schedule", "show my schedule", "my schedule"}:
            return {"kind": "list", "items": self.store.list_schedule_items(),
                    "blocks": self.store.schedule_blocks_for_date(datetime.now().astimezone().date().isoformat())}
        if lowered in {"show schedule conflicts", "check schedule conflicts", "show conflicts"}:
            return {"kind": "conflicts", "conflicts": self.conflicts()}
        why = re.match(r"why did you schedule (.+?)(?: at .+)?$", command, re.I)
        if why:
            plan = self.store.latest_schedule_plan()
            block = next((block for block in (plan or {}).get("blocks", []) if why.group(1).lower() in block["title"].lower()), None)
            return {"kind": "explanation", "block": block}
        find_time = re.match(r"find\s+(\d+|one|two|three)\s+(hour|hours|minute|minutes)\s+for\s+(.+)$", command, re.I)
        if find_time:
            amount = {"one": 1, "two": 2, "three": 3}.get(find_time.group(1).lower(), int(find_time.group(1)) if find_time.group(1).isdigit() else 1)
            duration = amount * (60 if find_time.group(2).lower().startswith("hour") else 1)
            item = self.add_task(find_time.group(3), duration_minutes=duration)
            return {"kind": "created", "item": item, "plan": self.plan_day()}
        time_first = re.match(
            r"(?:add|put)(?: this)? to (?:my )?schedule(?: for)?\s+"
            r"((?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
            r"(?:\s+at)?\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+(.+)$",
            command, re.I,
        )
        if time_first:
            from marlin.routine import RoutineService
            start = RoutineService._parse_due_phrase(time_first.group(1))
            if start is None:
                raise ValueError("I could not understand that schedule time.")
            item = self.add_event(time_first.group(2).strip(), start, start + timedelta(hours=1))
            return {"kind": "event_created", "item": item}
        add_to_schedule = re.match(
            r"(?:add|put)\s+(.+?)\s+to (?:my )?schedule\s+(?:at|on|for)\s+(.+)$",
            command, re.I,
        )
        if add_to_schedule:
            from marlin.routine import RoutineService
            start = RoutineService._parse_due_phrase(add_to_schedule.group(2))
            if start is None:
                raise ValueError("I could not understand that schedule time.")
            item = self.add_event(add_to_schedule.group(1).strip(), start, start + timedelta(hours=1))
            return {"kind": "event_created", "item": item}
        timed = re.match(
            r"(?:schedule|set (?:a |my )?schedule(?: for)?|add (?:an )?event|create (?:an )?event)\s+(.+?)\s+(?:at|on)\s+(.+)$",
            command, re.I,
        )
        if timed:
            from marlin.routine import RoutineService
            title = timed.group(1).strip()
            when = timed.group(2).strip()
            trailing_day = re.match(
                r"(.+?)\s+(?:for\s+)?(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)$",
                title,
                re.I,
            )
            if trailing_day:
                title = trailing_day.group(1).strip()
                when = f"{trailing_day.group(2)} {when}"
            start = RoutineService._parse_due_phrase(when)
            if start is None:
                raise ValueError("I could not understand that schedule time.")
            item = self.add_event(title, start, start + timedelta(hours=1))
            return {"kind": "event_created", "item": item}
        schedule = re.match(r"schedule\s+(?:my\s+)?(.+?)(?:\s+before\s+(.+))?$", command, re.I)
        if schedule:
            title = schedule.group(1).strip()
            deadline = self._natural_deadline(schedule.group(2)) if schedule.group(2) else None
            return {"kind": "created", "item": self.add_task(title, deadline=deadline)}
        if re.match(r"^(?:add|set|make|create|put).*\bschedule\b|^schedule$", lowered, re.I):
            return {
                "kind": "clarification",
                "message": "Tell me the event and time, for example: schedule dinner tomorrow at 10 PM.",
            }
        return None

    @staticmethod
    def _eligible(item: dict[str, Any], target: datetime) -> bool:
        # Overdue work must remain visible to the planner; its high priority is
        # useful precisely because its deadline has already passed.
        return item.get("status") != "completed"

    @staticmethod
    def _event_occurs(item: dict[str, Any], target: datetime) -> bool:
        start = ScheduleService._parse_iso(item.get("start_at"))
        if not start:
            return False
        recurrence = str(item.get("recurrence") or "none").lower()
        return (
            start.date() == target.date()
            or recurrence == "daily"
            or recurrence == "weekday" and target.weekday() < 5
            or recurrence == "weekly" and start.weekday() == target.weekday()
        )

    @staticmethod
    def _order_tasks(tasks: list[dict[str, Any]], target: datetime) -> list[dict[str, Any]]:
        by_id = {item["id"]: item for item in tasks}
        ordered: list[dict[str, Any]] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def score(item: dict[str, Any]) -> tuple[Any, ...]:
            deadline = ScheduleService._parse_iso(item.get("deadline_at"))
            overdue = bool(deadline and deadline < target)
            return (-int(overdue), -int(item.get("priority", 50)), item.get("project_id") or "", item.get("deadline_at") or "9999")

        def visit(item: dict[str, Any]) -> None:
            if item["id"] in visited or item["id"] in visiting:
                return
            visiting.add(item["id"])
            dependencies = item.get("metadata", {}).get("depends_on", [])
            if isinstance(dependencies, str): dependencies = [dependencies]
            for dependency in dependencies:
                if dependency in by_id: visit(by_id[dependency])
            visiting.remove(item["id"]); visited.add(item["id"]); ordered.append(item)

        for item in sorted(tasks, key=score): visit(item)
        return ordered

    @staticmethod
    def _minutes(start: str, end: str) -> tuple[int, int]:
        first, second = datetime.fromisoformat(start).astimezone(), datetime.fromisoformat(end).astimezone()
        return first.hour * 60 + first.minute, second.hour * 60 + second.minute

    @staticmethod
    def _slot_duration(minutes: int) -> int:
        return max(30, min(8 * 60, ((minutes + 29) // 30) * 30))

    @staticmethod
    def _parse_iso(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value).astimezone()
        except ValueError:
            return None

    @staticmethod
    def _natural_deadline(value: str | None) -> datetime | None:
        if not value:
            return None
        text = value.strip().lower()
        now = datetime.now().astimezone()
        if text == "today":
            return now.replace(hour=18, minute=0, second=0, microsecond=0)
        if text == "tomorrow":
            return (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
        weekdays = {name.lower(): index for index, name in enumerate(("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"))}
        if text in weekdays:
            days = (weekdays[text] - now.weekday()) % 7 or 7
            return (now + timedelta(days=days)).replace(hour=18, minute=0, second=0, microsecond=0)
        try:
            return datetime.fromisoformat(value).astimezone()
        except ValueError:
            return None

    @staticmethod
    def _atom(value: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")
        return (cleaned if cleaned and cleaned[0].isalpha() else "v_" + cleaned)[:80]
