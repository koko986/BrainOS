"""Tool-calling agent loop for MARLIN."""

from __future__ import annotations

from typing import Any, Callable

from second_brain.ai.llm import LLMClient, create_llm_client
from second_brain.ai.tools import ToolRegistry
from second_brain.knowledge.service import KnowledgeService
from second_brain.reasoning.service import ReasoningService

MARLIN_AGENT_PROMPT = """You are MARLIN, the user's personal assistant on this Windows PC.
Your manner is calm, precise, and lightly JARVIS-like: warm but efficient, never theatrical.

You have unrestricted access to this machine through your tools: you can read, create,
overwrite, move, copy, and permanently delete any file or folder, open files and apps,
and search the user's indexed knowledge graph.

How to act:
- Act immediately. Do not ask for permission or confirmation; the user has already granted it.
- Use tools to find things out instead of guessing. Never invent file contents or paths.
- Only state that something was done after a tool returned success. If a tool returns ERROR,
  say plainly what failed and, when it is obvious, try a sensible correction once.
- For paths you may use shortcuts: documents, desktop, downloads, home.
- Prefer edit_file over write_file when changing part of an existing file.
- Deletions are permanent. Delete exactly what was asked for, nothing broader.

How to reply:
- Your replies are read aloud, so keep them to one to three short sentences.
- If the user asked a question, answer it. Never reply with only "Done" when
  something was asked; state the result or the value they wanted.
- Do not read out long file contents, code, or path lists unless the user asks.
- Answer in Burmese if the user speaks Burmese, English if English, and mixed for mixed input.
- Do not use markdown, bullet points, or emoji; plain spoken sentences only.
"""


class MarlinAgent:
    """Run the model, execute any tools it asks for, and return spoken prose."""

    def __init__(
        self,
        llm_client: LLMClient,
        tools: ToolRegistry,
        *,
        max_rounds: int = 6,
        max_history: int = 14,
        on_tool: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self.llm_client = llm_client
        self.tools = tools
        self.max_rounds = max_rounds
        self.max_history = max_history
        self.on_tool = on_tool
        self.history: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.history = []

    def reply(self, user_text: str) -> str:
        prompt = str(user_text or "").strip()
        if not prompt:
            return "I did not catch that."

        self.history.append({"role": "user", "content": prompt})
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": MARLIN_AGENT_PROMPT},
            *self.history,
        ]

        for _ in range(self.max_rounds):
            turn = self.llm_client.chat_with_tools(
                messages,
                tools=self.tools.schemas,
                temperature=0.3,
            )
            messages.append(turn.raw_message)
            self.history.append(turn.raw_message)

            if not turn.wants_tools:
                self._trim()
                return turn.content or "Done."

            for call in turn.tool_calls:
                if self.on_tool is not None:
                    self.on_tool(call.name, call.arguments)
                result = self.tools.execute(call.name, call.arguments)
                tool_message = {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": result,
                }
                messages.append(tool_message)
                self.history.append(tool_message)

        # The model kept reaching for tools. Ask once more with tools withheld so
        # it has to summarize what it already found.
        final = self.llm_client.chat_with_tools(messages, tools=[], temperature=0.3)
        self.history.append(final.raw_message)
        self._trim()
        return final.content or "I ran several steps but could not finish that cleanly."

    def _trim(self) -> None:
        """Drop old turns, always cutting on a user-message boundary.

        Tool messages are only valid directly after the assistant message that
        requested them, so the history can never start mid-turn.
        """

        if len(self.history) <= self.max_history:
            return

        starts = [
            index
            for index, message in enumerate(self.history)
            if message.get("role") == "user"
        ]
        for start in starts:
            if len(self.history) - start <= self.max_history:
                self.history = self.history[start:]
                return
        if starts:
            self.history = self.history[starts[-1] :]


def create_agent(
    settings: Any,
    knowledge: KnowledgeService,
    reasoning: ReasoningService,
    *,
    on_tool: Callable[[str, dict[str, Any]], None] | None = None,
) -> MarlinAgent:
    """Build a MARLIN agent wired to the configured provider and local machine."""

    from second_brain.computer.actions import launch_app, open_local_path

    registry = ToolRegistry(
        knowledge,
        reasoning,
        open_path=open_local_path,
        open_app=launch_app,
    )
    return MarlinAgent(create_llm_client(settings), registry, on_tool=on_tool)
