"""End-to-end check of MARLIN's AI, tools, and speech-to-text wiring.

Run after setup to confirm the provider key, tool calling, file access, and
microphone path all work:

    python scripts/smoke_check.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from second_brain.ai.agent import create_agent  # noqa: E402
from second_brain.ai.llm import ChatMessage, LLMUnavailable, create_llm_client  # noqa: E402
from second_brain.core.config import Settings  # noqa: E402
from second_brain.database.connection import initialize_database  # noqa: E402
from second_brain.knowledge.service import KnowledgeService  # noqa: E402
from second_brain.reasoning.service import ReasoningService  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"


def report(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{PASS if ok else FAIL}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def check_chat(settings: Settings) -> bool:
    try:
        client = create_llm_client(settings)
        reply = client.chat(
            [ChatMessage(role="user", content="Reply with the single word: online")],
            temperature=0.0,
        )
    except LLMUnavailable as exc:
        return report("LLM chat round trip", False, str(exc)[:200])
    return report("LLM chat round trip", bool(reply), reply.strip()[:80])


def check_agent_file_work(settings: Settings) -> bool:
    with tempfile.TemporaryDirectory() as work_dir:
        db_path = Path(work_dir) / "brain.db"
        initialize_database(db_path)
        knowledge = KnowledgeService(db_path)
        reasoning = ReasoningService(knowledge, settings.prolog_dir)
        target = Path(work_dir) / "marlin_smoke.txt"

        tools_used: list[str] = []
        agent = create_agent(
            settings,
            knowledge,
            reasoning,
            on_tool=lambda name, _args: tools_used.append(name),
        )

        try:
            agent.reply(
                f"Create the file {target} containing exactly the line: hello from marlin"
            )
        except LLMUnavailable as exc:
            return report("Agent writes a file", False, str(exc)[:200])

        if not target.exists():
            return report("Agent writes a file", False, f"tools used: {tools_used}")
        body = target.read_text(encoding="utf-8").strip()
        report("Agent writes a file", True, f"content: {body[:60]!r}")

        try:
            agent.reply(f"Now delete the file {target}")
        except LLMUnavailable as exc:
            return report("Agent deletes a file", False, str(exc)[:200])

        return report(
            "Agent deletes a file",
            not target.exists(),
            f"tools used: {tools_used}",
        )


def check_microphone() -> bool:
    try:
        import sounddevice as sd
    except Exception as exc:  # noqa: BLE001
        return report("Microphone available", False, str(exc)[:200])

    try:
        default_input = sd.query_devices(kind="input")
    except Exception as exc:  # noqa: BLE001
        return report("Microphone available", False, str(exc)[:200])
    return report("Microphone available", True, str(default_input.get("name", "unknown"))[:80])


def check_wake_model(settings: Settings) -> bool:
    from second_brain.app.voice import (
        VoiceInputUnavailable,
        WakeWordListener,
        parse_wake_words,
        resolve_vosk_model_path,
    )

    listener = WakeWordListener(
        resolve_vosk_model_path(settings),
        parse_wake_words(settings.wake_words),
    )
    try:
        listener.prepare()
    except VoiceInputUnavailable as exc:
        return report("Wake word model", False, str(exc)[:200])
    return report("Wake word model", True, ", ".join(listener.phrases))


def check_stt_credentials(settings: Settings) -> bool:
    from second_brain.app.voice import GroqSTT, VoiceInputUnavailable

    if str(settings.stt_provider).lower().strip() != "groq":
        return report("Speech-to-text provider", True, f"offline: {settings.stt_provider}")
    try:
        client = GroqSTT.from_settings(settings)
    except VoiceInputUnavailable as exc:
        return report("Speech-to-text provider", False, str(exc)[:200])
    return report("Speech-to-text provider", True, f"groq {client.model}")


def main() -> int:
    settings = Settings.from_env()
    print(f"Provider: {settings.llm_provider} / {settings.groq_model}")
    print(f"Conversation: {settings.conversation_provider}\n")

    results = [
        check_chat(settings),
        check_agent_file_work(settings),
        check_stt_credentials(settings),
        check_microphone(),
        check_wake_model(settings),
    ]

    print()
    if all(results):
        print("All checks passed. Run: py main.py")
        return 0
    print("Some checks failed. See the notes above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
