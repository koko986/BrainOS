"""Safe conversational layer for MARLIN."""

from __future__ import annotations

from second_brain.ai.llm import ChatMessage, LLMClient


MARLIN_SYSTEM_PROMPT = """You are MARLIN, the user's personal brain assistant.
Your style is calm, polished, capable, and lightly JARVIS-like: formal but warm,
efficient, precise, and never theatrical.

Safety and privacy boundaries:
- You may converse, explain, brainstorm, plan, and summarize safe context.
- You do not claim that you executed OS, shell, file, calendar, email, or database actions.
- You do not invent private files or memories that were not supplied in the context.
- For actions, say that MARLIN can route safe brain commands through validated Python tools.
- If the user asks for risky file or system control, ask for a specific, confirmable action.
- Keep replies concise unless the user asks for detail.
- Reply in Burmese when the user writes Burmese, English when they write English, and mixed style for mixed input.
"""


class ConversationService:
    """Generate non-action conversational replies through the configured LLM."""

    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client

    def reply(self, user_text: str) -> str:
        messages = [
            ChatMessage(role="system", content=MARLIN_SYSTEM_PROMPT),
            ChatMessage(
                role="system",
                content=(
                    "MARLIN has a local SQLite knowledge graph and a Prolog reasoning "
                    "layer available through validated Python actions. You are not given "
                    "the user's graph contents in this conversational fallback. If the "
                    "user asks for graph data, suggest a safe brain command such as "
                    "listing entities, listing relationships, or asking for high priority tasks."
                ),
            ),
            ChatMessage(role="user", content=user_text),
        ]
        return self.llm_client.chat(messages, temperature=0.35)
