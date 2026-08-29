"""Fast local conversation fallback for MARLIN."""

from __future__ import annotations

from dataclasses import dataclass

from second_brain.knowledge.service import KnowledgeService


@dataclass
class LocalConversationService:
    """Answer common MARLIN conversation prompts without calling an external model."""

    knowledge: KnowledgeService

    def try_reply(self, text: str) -> str | None:
        prompt = " ".join(text.lower().strip().split())
        if not prompt:
            return None

        # Short-message guard. Without it, a real request such as "help me
        # rewrite this file" would be answered with canned text instead of
        # reaching the agent and its tools.
        is_short = len(prompt.split()) <= 4

        if is_short and any(
            word in prompt for word in ("hello", "hi", "hey", "good morning", "good evening")
        ):
            return "Good day. MARLIN is online and ready."

        if "what is second brain" in prompt or "second brain ai" in prompt:
            return (
                "Second Brain AI is your personal knowledge and reasoning system. "
                "It stores projects, files, tasks, technologies, and relationships in SQLite, "
                "then uses Prolog rules to infer useful things such as important or high-priority tasks."
            )

        if "who are you" in prompt or "what are you" in prompt or ("marlin" in prompt and "who" in prompt):
            return (
                "I am MARLIN, your local command and reasoning assistant. "
                "I can read and edit any file on this PC, open apps, work with your brain graph "
                "and Prolog reasoning, and answer by voice."
            )

        if (is_short and "help" in prompt) or "what can you do" in prompt:
            return (
                "Ask me to read, write, edit, or delete any file, open folders and apps, "
                "graph my files, search my files for python, high priority tasks, "
                "or just talk normally."
            )

        if "prolog" in prompt:
            return (
                "Prolog is MARLIN's rule-based reasoning layer. Python stores facts in SQLite, "
                "then Prolog applies logic rules like important_task and high_priority_task to explain why something matters."
            )

        if "slow" in prompt or "speed" in prompt or "faster" in prompt:
            return (
                "For speed, MARLIN now answers common requests locally and keeps OpenCode on a short timeout. "
                "Brain commands, graph commands, file search, camera, and Prolog reasoning do not wait for OpenCode."
            )

        if "status" in prompt:
            entities = self.knowledge.count_entities()
            relationships = self.knowledge.count_relationships()
            files = self.knowledge.count_entities("file")
            return (
                f"MARLIN status: {entities} entities, {relationships} relationships, and {files} indexed files. "
                "The brain graph and Prolog paths are local."
            )

        if "privacy" in prompt or "security" in prompt:
            return (
                "MARLIN runs in full autonomous mode: it can read, edit, and delete any file "
                "on this PC without asking. Every action is written to the local audit log. "
                "Shell commands are the one thing it will not run."
            )

        return None

    def fallback_reply(self, text: str, reason: str | None = None) -> str:
        note = f" ({reason})" if reason else ""
        return (
            f"OpenCode is too slow right now{note}. "
            "I stayed in local mode so MARLIN remains responsive. "
            "Try a direct command such as graph my files, high priority tasks, search my files for python, "
            "or ask a shorter question."
        )
