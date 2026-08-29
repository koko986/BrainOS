"""Interactive terminal mode for MARLIN."""

from __future__ import annotations

from typing import Any

from second_brain.app.assistant import MarlinAssistant
from second_brain.app.voice import VoiceInputUnavailable, WindowsSpeech, transcribe_once
from second_brain.core.config import Settings
from second_brain.knowledge.service import KnowledgeService
from second_brain.reasoning.service import ReasoningService

HELP_TEXT = """Try:
  read this file: C:\\path\\to\\file.txt
  write a note on my desktop called ideas.txt saying ...
  delete C:\\path\\to\\file.txt
  rename the report in Documents to final.docx
  open Documents | open notepad | index this folder: C:\\path
  search my files for python | high priority tasks
  listen        speak one command
  hands free    switch to wake-word mode
  voice off / voice on / stop voice
  exit"""


def run_terminal_cockpit(
    settings: Settings,
    knowledge: KnowledgeService,
    reasoning: ReasoningService,
) -> None:
    """Run MARLIN as an interactive terminal assistant."""

    speech = WindowsSpeech()
    voice_output_enabled = settings.terminal_voice_output
    assistant = MarlinAssistant(settings, knowledge, reasoning, on_tool=_print_tool)

    print("MARLIN terminal cockpit online.")
    print("Type a request, 'listen' for one voice command, or 'hands free' for wake-word mode.")
    print("MARLIN has full read and write access to this PC. Type 'help' for examples.")

    while True:
        try:
            text = input("MARLIN> ").strip()
        except (EOFError, KeyboardInterrupt):
            speech.stop()
            print("\nMARLIN offline.")
            return

        if not text:
            continue

        command = text.lower()
        if command in {"exit", "quit"}:
            speech.stop()
            print("MARLIN offline.")
            return
        if command == "help":
            print(HELP_TEXT)
            continue
        if command in {"stop voice", "stop speaking", "be quiet"}:
            speech.stop()
            print("Voice stopped.")
            continue
        if command in {"voice off", "mute voice"}:
            voice_output_enabled = False
            speech.stop()
            print("Voice output disabled.")
            continue
        if command in {"voice on", "speak replies"}:
            voice_output_enabled = True
            print("Voice output enabled.")
            continue
        if command in {"hands free", "hands-free", "jarvis", "wake word"}:
            from second_brain.app.jarvis import run_voice_loop

            speech.stop()
            run_voice_loop(settings, knowledge, reasoning)
            print("Back in terminal mode.")
            continue
        if command in {"listen", "voice input"}:
            try:
                text = transcribe_once(settings, on_state=_print_state).strip()
            except VoiceInputUnavailable as exc:
                print(str(exc))
                continue
            if not text:
                print("I did not hear a command.")
                continue
            print(f"You: {text}")

        reply = assistant.handle(text)
        _reply(speech, voice_output_enabled, reply)


def _reply(speech: WindowsSpeech, voice_output_enabled: bool, text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))
    if voice_output_enabled:
        speech.speak(text)


def _print_state(state: str) -> None:
    labels = {
        "listening": "Listening...",
        "recording": "Recording...",
        "thinking": "Transcribing...",
    }
    print(labels.get(state, state))


def _print_tool(name: str, arguments: dict[str, Any]) -> None:
    target = ""
    for key in ("path", "source", "root", "name", "query", "task_id"):
        value = arguments.get(key)
        if value:
            target = f" {value}"
            break
    print(f"  -> {name}{target}")
