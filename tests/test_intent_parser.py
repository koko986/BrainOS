from __future__ import annotations

import json

from second_brain.ai.intent_parser import IntentParser
from second_brain.ai.llm import ChatMessage


class FakeLLM:
    def __init__(self, response: str):
        self.response = response
        self.messages: list[ChatMessage] = []
        self.schema = {}

    def structured_chat(self, messages, *, schema, temperature=0.0):
        self.messages = messages
        self.schema = schema
        return self.response


def test_intent_parser_uses_structured_schema():
    fake = FakeLLM(
        json.dumps(
            {
                "intent": "get_important_tasks",
                "language": "en",
                "confidence": 0.93,
                "parameters": {},
                "requires_confirmation": False,
            }
        )
    )

    intent = IntentParser(fake).parse("What should I work on today?")

    assert intent.intent == "get_important_tasks"
    assert fake.schema["type"] == "object"
    assert any(message.role == "system" for message in fake.messages)


def test_intent_parser_falls_back_to_unknown_for_malformed_json():
    fake = FakeLLM("not json")

    intent = IntentParser(fake).parse("ဒီ project နဲ့ပတ်သက်တဲ့ tasks ပြပါ")

    assert intent.intent == "unknown"
    assert intent.language == "mixed"
