from __future__ import annotations

from second_brain.ai.action_dispatcher import ActionResult
from second_brain.ai.response_generator import ResponseGenerator


def test_response_generator_renders_safe_entity_result():
    result = ActionResult(
        intent="list_entities",
        ok=True,
        language="en",
        message="Listed entities.",
        data={
            "entities": [
                {
                    "id": "project_second_brain",
                    "type": "project",
                    "name": "Second Brain AI",
                    "source": "demo",
                    "metadata": {},
                }
            ]
        },
    )

    response = ResponseGenerator().generate(result)

    assert "Found 1 entities" in response
    assert "project_second_brain" in response


def test_response_generator_localizes_simple_burmese_failures():
    result = ActionResult(
        intent="unknown",
        ok=False,
        language="my",
        message="I could not confidently map that request to a safe Phase 2 action.",
    )

    response = ResponseGenerator().generate(result)

    assert "safe Phase 2 action" in response
    assert "မလုပ်ဆောင်ပါ" in response
