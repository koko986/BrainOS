"""Validated intent schema for Phase 2 natural-language commands."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


AllowedIntent = Literal[
    "list_entities",
    "list_files",
    "list_relationships",
    "search_entities",
    "search_files",
    "get_important_tasks",
    "get_high_priority_tasks",
    "explain_high_priority",
    "seed_demo",
    "open_camera",
    "close_camera",
    "open_folder",
    "open_file",
    "open_app",
    "index_folder",
    "unknown",
]

LanguageCode = Literal["en", "my", "mixed", "unknown"]


class IntentParameters(BaseModel):
    """Allow-listed parameters the LLM may attach to an intent."""

    model_config = ConfigDict(extra="forbid")

    task_id: str | None
    query: str | None
    entity_type: str | None
    path: str | None
    app_name: str | None
    max_files: int | None
    allow_drive_root: bool | None

    @model_validator(mode="before")
    @classmethod
    def fill_missing_optional_values(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                "task_id": value.get("task_id"),
                "query": value.get("query"),
                "entity_type": value.get("entity_type"),
                "path": value.get("path"),
                "app_name": value.get("app_name"),
                "max_files": value.get("max_files"),
                "allow_drive_root": value.get("allow_drive_root"),
            }
        return value

    def get(self, key: str, default: Any = None) -> Any:
        return self.model_dump().get(key, default)

    def __getitem__(self, key: str) -> Any:
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value


class StructuredIntent(BaseModel):
    """LLM-produced intent after strict Python validation."""

    model_config = ConfigDict(extra="forbid")

    intent: AllowedIntent
    language: LanguageCode
    confidence: float = Field(ge=0.0, le=1.0)
    parameters: IntentParameters
    requires_confirmation: bool

    @field_validator("parameters")
    @classmethod
    def parameters_must_be_json_safe(cls, value: IntentParameters) -> IntentParameters:
        try:
            json.dumps(value.model_dump())
        except TypeError as exc:
            raise ValueError("parameters must be JSON-serializable") from exc
        return value

    @model_validator(mode="after")
    def validate_required_parameters(self) -> "StructuredIntent":
        if self.intent == "explain_high_priority":
            task_id = self.parameters.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("explain_high_priority requires parameters.task_id")
        if self.intent == "search_entities":
            query = self.parameters.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("search_entities requires parameters.query")
        if self.intent == "search_files":
            query = self.parameters.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("search_files requires parameters.query")
        if self.intent in {"open_folder", "open_file", "index_folder"}:
            path = self.parameters.get("path")
            if not isinstance(path, str) or not path.strip():
                raise ValueError(f"{self.intent} requires parameters.path")
        if self.intent == "open_app":
            app_name = self.parameters.get("app_name")
            if not isinstance(app_name, str) or not app_name.strip():
                raise ValueError("open_app requires parameters.app_name")
        return self

    @property
    def is_low_confidence(self) -> bool:
        return self.confidence < 0.6


def unknown_intent(language: LanguageCode = "unknown") -> StructuredIntent:
    return StructuredIntent(
        intent="unknown",
        language=language,
        confidence=0.0,
        parameters={},
        requires_confirmation=False,
    )
