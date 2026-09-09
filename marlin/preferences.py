"""Structured, reversible preference memory for MARLIN."""

from __future__ import annotations

import re
from typing import Any

from marlin.events import EventBus
from marlin.storage import MarlinStore


SENSITIVE = re.compile(
    r"\b(password|passcode|api[ -]?key|secret|token|credit card|bank account|diagnos|medical|health)\b|"
    r"\b(?:sk|gsk|ghp|xoxb)[-_][a-z0-9_-]{12,}",
    re.I,
)


class PreferenceService:
    def __init__(self, store: MarlinStore, events: EventBus):
        self.store = store
        self.events = events

    def observe(self, text: str) -> dict[str, Any] | None:
        if SENSITIVE.search(text):
            return None
        match = re.search(r"\bi\s+(prefer|actually prefer|usually|like)\s+(.+?)[.!?]*$", text.strip(), re.I)
        if not match:
            return None
        phrase = match.group(2).strip()
        category, value = self._classify(phrase)
        explicit = "prefer" in match.group(1).lower()
        preference = self.store.remember_preference(
            category, value, text.strip(), .98 if explicit else .70
        )
        self.events.publish(
            "preference.learned" if preference.get("active") else "preference.observed",
            preference=preference,
        )
        return preference

    def handle(self, text: str) -> dict[str, Any] | None:
        lowered = " ".join(text.lower().split()).rstrip(".?")
        if lowered in {
            "what do you remember about me", "show my preferences", "list my preferences",
            "what are my preferences",
        }:
            return {"kind": "list", "preferences": self.store.list_preferences(active_only=True)}
        forget = re.match(r"(?:forget|remove) (?:that )?preference(?: about)?\s*(.*)$", text.strip(), re.I)
        if forget:
            query = forget.group(1).strip()
            active = self.store.list_preferences(active_only=True)
            if not query and len(active) == 1:
                query = active[0]["id"]
            count = self.store.forget_preference(query) if query else 0
            self.events.publish("preference.forgotten", query=query, count=count)
            return {"kind": "forgotten", "count": count, "query": query}
        learned = self.observe(text)
        return {"kind": "learned", "preference": learned} if learned else None

    @staticmethod
    def _classify(phrase: str) -> tuple[str, str]:
        lowered = phrase.lower()
        period = next((item for item in ("morning", "afternoon", "evening", "night") if item in lowered), None)
        if period:
            return "preferred_period", period
        duration = re.search(r"(\d+)\s*(minutes?|hours?)", lowered)
        if duration:
            minutes = int(duration.group(1)) * (60 if duration.group(2).startswith("hour") else 1)
            return "preferred_duration", str(minutes)
        app = re.search(r"(?:using|use|in)\s+([\w .+-]+)$", phrase, re.I)
        if app:
            return "preferred_app", app.group(1).strip()
        project = re.search(r"(?:working on|project)\s+(.+)$", phrase, re.I)
        if project:
            return "preferred_project", project.group(1).strip()
        return "general", phrase
