"""Shared request routing for MARLIN's terminal and hands-free modes.

Order of routing, fastest first:

1. Shell requests, which MARLIN refuses.
2. Deterministic brain commands, which need no model call.
3. Graph visualization requests.
4. Deterministic computer actions, executed immediately.
5. Optional canned local replies, when fast_local_conversation is enabled.
6. The tool-calling agent, which handles everything else including all file work.
"""

from __future__ import annotations

from typing import Any, Callable

from second_brain.ai.action_dispatcher import ActionDispatcher
from second_brain.ai.agent import create_agent
from second_brain.ai.intent_schema import StructuredIntent
from second_brain.ai.llm import LLMUnavailable
from second_brain.ai.local_conversation import LocalConversationService
from second_brain.ai.response_generator import ResponseGenerator
from second_brain.computer.actions import (
    COMPUTER_INTENTS,
    ComputerActionService,
    looks_like_blocked_computer_request,
    parse_allowed_apps,
    parse_computer_command,
)
from second_brain.core.audit import record_audit
from second_brain.core.cockpit_state import build_file_extension_chart, save_latest_chart
from second_brain.core.config import Settings
from second_brain.knowledge.service import KnowledgeService
from second_brain.reasoning.prolog_engine import PrologUnavailable
from second_brain.reasoning.service import ReasoningService

BLOCKED_REPLY = (
    "I cannot run shell or command-prompt commands. "
    "Ask me directly and I will use my file tools instead."
)

GRAPH_REQUESTS = {
    "graph my files",
    "graph my files by extension",
    "show file graph",
    "show file counts",
    "show file counts by extension",
}


class MarlinAssistant:
    """Route one request to the fastest capable handler."""

    def __init__(
        self,
        settings: Settings,
        knowledge: KnowledgeService,
        reasoning: ReasoningService,
        *,
        on_tool: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self.settings = settings
        self.knowledge = knowledge
        self.reasoning = reasoning
        self.computer = ComputerActionService(
            knowledge,
            allowed_apps=parse_allowed_apps(settings.allowed_apps),
            confirmation_mode=settings.computer_confirmation,
        )
        self.dispatcher = ActionDispatcher(knowledge, reasoning)
        self.responses = ResponseGenerator()
        self.local_conversation = LocalConversationService(knowledge)
        self.agent = create_agent(settings, knowledge, reasoning, on_tool=on_tool)

    def handle(self, text: str) -> str:
        prompt = str(text or "").strip()
        if not prompt:
            return "I did not catch that."

        if looks_like_blocked_computer_request(prompt):
            record_audit("blocked", "Shell request", prompt[:200], "blocked")
            return BLOCKED_REPLY

        brain_reply = self._brain_reply(prompt)
        if brain_reply is not None:
            return brain_reply

        if " ".join(prompt.lower().split()) in GRAPH_REQUESTS:
            chart = build_file_extension_chart(self.knowledge)
            save_latest_chart(chart)
            record_audit("visualization", chart["title"], "assistant", "complete")
            return f"Rendered {chart['title']} for the cockpit."

        computer_reply = self._computer_reply(prompt)
        if computer_reply is not None:
            return computer_reply

        if self.settings.fast_local_conversation:
            local_reply = self.local_conversation.try_reply(prompt)
            if local_reply is not None:
                return local_reply

        try:
            return self.agent.reply(prompt)
        except LLMUnavailable as exc:
            return f"My language model is unreachable: {exc}"

    def _computer_reply(self, prompt: str) -> str | None:
        intent = parse_computer_command(prompt)
        if intent is None or intent.intent not in COMPUTER_INTENTS:
            return None

        try:
            action = self.computer.preview(intent)
            if action is None:
                return None
            result = self.computer.execute(action)
        except (ValueError, OSError) as exc:
            record_audit("failed", intent.intent, prompt[:200], str(exc)[:200])
            return f"That action failed: {exc}"

        record_audit(
            "executed",
            action.label,
            action.target,
            "complete" if result.ok else "failed",
        )
        if action.intent == "index_folder":
            save_latest_chart(build_file_extension_chart(self.knowledge))
        return result.message

    def _brain_reply(self, text: str) -> str | None:
        command = " ".join(text.lower().split())
        simple_intents = {
            "list entities": "list_entities",
            "show entities": "list_entities",
            "list relationships": "list_relationships",
            "show relationships": "list_relationships",
            "important tasks": "get_important_tasks",
            "reason important-tasks": "get_important_tasks",
            "show important tasks": "get_important_tasks",
            "high priority tasks": "get_high_priority_tasks",
            "reason high-priority": "get_high_priority_tasks",
            "show high priority tasks": "get_high_priority_tasks",
            "seed demo": "seed_demo",
            "seed-demo": "seed_demo",
        }

        try:
            intent_name = simple_intents.get(command)
            if intent_name is not None:
                return self.responses.generate(self.dispatcher.dispatch(_intent(intent_name)))

            for prefix in ("why high priority ", "why high-priority "):
                if command.startswith(prefix):
                    task_id = text[len(prefix) :].strip()
                    return self.responses.generate(
                        self.dispatcher.dispatch(_intent("explain_high_priority", task_id=task_id))
                    )
        except PrologUnavailable as exc:
            return f"Prolog reasoning is unavailable: {exc}"
        return None


def _intent(name: str, **parameters: Any) -> StructuredIntent:
    return StructuredIntent(
        intent=name,
        language="en",
        confidence=1.0,
        parameters=parameters,
        requires_confirmation=False,
    )
