from __future__ import annotations

import pytest
from pydantic import ValidationError

from second_brain.ai.intent_schema import StructuredIntent


def test_structured_intent_accepts_burmese_safe_intent():
    intent = StructuredIntent(
        intent="get_high_priority_tasks",
        language="my",
        confidence=0.91,
        parameters={},
        requires_confirmation=False,
    )

    assert intent.intent == "get_high_priority_tasks"
    assert not intent.is_low_confidence


def test_structured_intent_rejects_unsupported_intent():
    with pytest.raises(ValidationError):
        StructuredIntent(
            intent="run_shell",
            language="en",
            confidence=0.9,
            parameters={"command": "dir"},
            requires_confirmation=False,
        )


def test_explain_high_priority_requires_task_id():
    with pytest.raises(ValidationError):
        StructuredIntent(
            intent="explain_high_priority",
            language="en",
            confidence=0.9,
            parameters={},
            requires_confirmation=False,
        )


def test_confidence_must_be_between_zero_and_one():
    with pytest.raises(ValidationError):
        StructuredIntent(
            intent="list_entities",
            language="en",
            confidence=1.5,
            parameters={},
            requires_confirmation=False,
        )


def test_open_folder_requires_path():
    with pytest.raises(ValidationError):
        StructuredIntent(
            intent="open_folder",
            language="en",
            confidence=0.9,
            parameters={},
            requires_confirmation=True,
        )


def test_open_app_requires_app_name():
    with pytest.raises(ValidationError):
        StructuredIntent(
            intent="open_app",
            language="en",
            confidence=0.9,
            parameters={},
            requires_confirmation=True,
        )


def test_camera_intent_is_valid_when_confirmation_required():
    intent = StructuredIntent(
        intent="open_camera",
        language="en",
        confidence=0.9,
        parameters={},
        requires_confirmation=True,
    )

    assert intent.intent == "open_camera"
