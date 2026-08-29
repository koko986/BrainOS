"""Hands-free voice mode for MARLIN."""

from __future__ import annotations

from typing import Any

from second_brain.app.assistant import MarlinAssistant
from second_brain.app.voice import (
    VoiceInputUnavailable,
    WakeWordListener,
    WindowsSpeech,
    parse_wake_words,
    resolve_vosk_model_path,
    transcribe_once,
)
from second_brain.core.config import Settings
from second_brain.knowledge.service import KnowledgeService
from second_brain.reasoning.service import ReasoningService

STOP_PHRASES = {
    "exit",
    "quit",
    "stop listening",
    "goodbye marlin",
    "marlin offline",
    "stand down",
    "that is all",
    "thats all",
}


def run_voice_loop(
    settings: Settings,
    knowledge: KnowledgeService,
    reasoning: ReasoningService,
) -> None:
    """Listen for the wake word, answer out loud, and repeat."""

    speech = WindowsSpeech()
    assistant = MarlinAssistant(settings, knowledge, reasoning, on_tool=_print_tool)
    wake = _build_wake_listener(settings)

    phrases = parse_wake_words(settings.wake_words)
    print("MARLIN hands-free mode online.")
    if wake is not None:
        print(f"Say '{phrases[0]}' to wake me. Press Ctrl+C to stop.")
    else:
        print("Wake word is off. Press Enter to talk, or Ctrl+C to stop.")
    print("MARLIN has full read and write access to this PC.")

    try:
        while True:
            if not _await_turn(wake, phrases, speech, settings):
                continue

            try:
                heard = transcribe_once(settings, on_state=_print_state)
            except VoiceInputUnavailable as exc:
                print(f"Voice input failed: {exc}")
                return

            if not heard:
                print("I did not hear a command.")
                continue

            print(f"You: {heard}")
            if " ".join(heard.lower().split()).strip(" .!") in STOP_PHRASES:
                _say(speech, settings, "Standing down.")
                print("MARLIN offline.")
                return

            reply = assistant.handle(heard)
            print(f"MARLIN: {reply}")
            _say(speech, settings, reply)
    except KeyboardInterrupt:
        speech.stop()
        print("\nMARLIN offline.")


def _await_turn(
    wake: WakeWordListener | None,
    phrases: list[str],
    speech: WindowsSpeech,
    settings: Settings,
) -> bool:
    """Wait for the user to address MARLIN. Returns False to retry the loop."""

    if wake is None:
        try:
            input("\n[Enter to talk] ")
        except EOFError:
            raise KeyboardInterrupt from None
        return True

    print(f"\nWaiting for '{phrases[0]}'...")
    try:
        if not wake.wait_for_wake():
            return False
    except VoiceInputUnavailable as exc:
        print(f"Wake word listener stopped: {exc}")
        raise KeyboardInterrupt from None

    print("Wake word heard.")
    _say(speech, settings, "Yes?")
    return True


def _build_wake_listener(settings: Settings) -> WakeWordListener | None:
    if not settings.wake_word_enabled:
        return None

    phrases = parse_wake_words(settings.wake_words)
    listener = WakeWordListener(resolve_vosk_model_path(settings), phrases)
    try:
        # Load the model now so a missing model is reported before the loop
        # starts rather than on the first spoken word.
        listener.prepare()
    except VoiceInputUnavailable as exc:
        print(f"Wake word unavailable: {exc}")
        print("Falling back to press-Enter-to-talk.")
        return None
    return listener


def _say(speech: WindowsSpeech, settings: Settings, text: str) -> None:
    if not settings.terminal_voice_output:
        return
    speech.speak(text)
    # Wait for playback to finish so the microphone does not pick up MARLIN's
    # own voice and retrigger the wake word.
    speech.wait()


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
