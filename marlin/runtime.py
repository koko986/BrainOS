"""Shared MARLIN V2 command, reasoning, memory, and action runtime."""

from __future__ import annotations

import json
import os
import re
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from marlin.actions import ActionOutcome, ComputerActionService
from marlin.config import MarlinSettings
from marlin.events import EventBus
from marlin.indexer import IncrementalIndexer, IndexProgress
from marlin.file_understanding import FileUnderstandingService
from marlin.local_model import LocalModelUnavailable, OllamaLocalModel
from marlin.notifications import play_notification_sound
from marlin.planner import ScheduleService
from marlin.preferences import PreferenceService
from marlin.research import InternetResearchService
from marlin.routine import AssistantState, RoutineService
from marlin.storage import MarlinStore
from marlin.voice import LocalVoiceService
from second_brain.database.connection import initialize_database
from second_brain.knowledge.service import KnowledgeService
from second_brain.reasoning.prolog_engine import PrologUnavailable
from second_brain.reasoning.service import ReasoningService
from second_brain.app.voice import VoiceInputUnavailable


SYSTEM_PROMPT = """You are MARLIN, a private local Windows assistant with a calm, precise, lightly British manner.
Use tools when the user asks about their computer, files, apps, reminders, media, knowledge graph, or tasks.
Never invent a path, file content, completed action, or tool result. You cannot execute shell, CMD, or PowerShell.
Python is the only executor. Destructive tools create a confirmation preview; never claim they completed before approval.
Always reply in English. Default to one or two short sentences, at most 45 words.
Do not deliver a lecture. Give more detail only when the user explicitly asks for it.
Treat Marlon, Merlin, Marlene, Marilyn, and Molly as likely pronunciations of MARLIN in voice transcripts. Never correct or tease the user about the name.
Do not use emoji.
Avoid introductory filler. Give the useful answer first.
When the user refers to "it", "them", or "that project", use recent context instead of guessing.
Never silently correct an uncertain action target. Ask a short clarification instead.
"""


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "play_youtube", "description": "Search YouTube and start a video or music. Use this for playback, not open_url or a generic media key.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 300}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a text file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "list_folder", "description": "List folder contents.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "find_files", "description": "Find files by wildcard name.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "pattern": {"type": "string"}}, "required": ["path", "pattern"]}}},
    {"type": "function", "function": {"name": "grep_files", "description": "Search text inside files.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "query": {"type": "string"}}, "required": ["path", "query"]}}},
    {"type": "function", "function": {"name": "open_path", "description": "Open an existing file or folder.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "play_video", "description": "Play an existing local video file in the Windows default player.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "open_app", "description": "Open a Windows application.", "parameters": {"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]}}},
    {"type": "function", "function": {"name": "open_url", "description": "Open a website or web search in Chrome.", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "close_app", "description": "Close an app after user approval.", "parameters": {"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]}}},
    {"type": "function", "function": {"name": "create_file", "description": "Create a new text file. Existing targets require approval.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "append_file", "description": "Append text after approval.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "edit_file", "description": "Replace exact text after approval.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "replace_all": {"type": "boolean"}}, "required": ["path", "old_text", "new_text"]}}},
    {"type": "function", "function": {"name": "move_path", "description": "Move or rename a path after approval.", "parameters": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}, "required": ["source", "destination"]}}},
    {"type": "function", "function": {"name": "copy_path", "description": "Copy a file or folder.", "parameters": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}, "required": ["source", "destination"]}}},
    {"type": "function", "function": {"name": "delete_path", "description": "Move a file or folder to Recycle Bin after approval.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "search_brain", "description": "Search indexed brain files.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "high_priority_tasks", "description": "Run Prolog high-priority task reasoning.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "media_control", "description": "Control Windows media.", "parameters": {"type": "object", "properties": {"command": {"type": "string", "enum": ["play_pause", "stop", "next", "previous", "volume_up", "volume_down", "mute"]}}, "required": ["command"]}}},
]


class MarlinRuntime:
    def __init__(self, settings: MarlinSettings | None = None, *, start_background: bool = True):
        self.settings = settings or MarlinSettings.from_env()
        initialize_database(self.settings.database_path)
        self.store = MarlinStore(self.settings.database_path)
        self.backup_path = self.store.migrate()
        self.knowledge = KnowledgeService(self.settings.database_path)
        self.reasoning = ReasoningService(self.knowledge, self.settings.prolog_dir)
        # Load SWI-Prolog before Whisper/Piper load native DLLs. On Windows,
        # importing those libraries first can make PySWIP fail with WinError 127.
        self.prolog_startup_error = ""
        try:
            self.reasoning.engine.load()
        except PrologUnavailable as exc:
            self.prolog_startup_error = str(exc)
        self.events = EventBus()
        self.state = AssistantState(self.store, self.events)
        self.voice = LocalVoiceService(self.settings, self.events)
        self._voice_generation = 0
        self.desktop = None
        self.actions = ComputerActionService(self.store)
        self.model = OllamaLocalModel(self.settings)
        self.indexer = IncrementalIndexer(self.knowledge, self.store)
        self.schedule = ScheduleService(self.store, self.reasoning, self.events)
        self.preferences = PreferenceService(self.store, self.events)
        self.files = FileUnderstandingService(self.settings, self.store, self.reasoning)
        # Return inspected sources immediately. A second local-model pass made
        # healthy internet searches appear to hang for tens of seconds.
        self.research = InternetResearchService(self.store, self.reasoning, self.events)
        self._last_prolog_activity: dict[str, Any] = {}
        self.routine = RoutineService(
            self.settings,
            self.store,
            self.reasoning,
            self.state,
            self.events,
            on_alarm=self._alarm_fired,
            on_reminder=self._reminder_fired,
        )
        self._wake_stop = threading.Event()
        self._wake_thread: threading.Thread | None = None
        self._chat_stop = threading.Event()
        self._chat_thread: threading.Thread | None = None
        self._chat_lock = threading.Lock()
        self._chat_paused = threading.Event()
        self._graph_cache: dict[tuple[str, int], dict[str, Any]] = {}
        self._graph_lock = threading.Lock()
        if start_background:
            self.routine.start()
            self.voice.stt.preload_async()
            self.voice.preload_speech()
            self.model.preload_async()
            self.start_wake_listener()
        self._index_thread: threading.Thread | None = None
        self.index_status = "idle"
        self.index_progress: dict[str, Any] = {"root": "", "indexed": 0, "skipped": 0, "complete": False}
        if start_background and self.settings.auto_index_c_drive:
            self.start_index(self.settings.graph_root, max_files=self.settings.index_batch_size)

    def command(self, text: str, *, source: str = "ui") -> dict[str, Any]:
        prompt = str(text or "").strip()
        if not prompt:
            return self._reply("I did not catch that.")
        self.events.publish("user.message", text=prompt, source=source)
        self.store.add_message("user", prompt)
        if source in {"voice", "voice_chat", "wake"}:
            prompt = re.sub(r"\b(?:marlon|merlin|marlene|marilyn|molly)\b", "MARLIN", prompt, count=1, flags=re.I)

        if self.state.value == "standby" and "wake up" not in prompt.lower():
            return self._reply("MARLIN is standing by. Say MARLIN, wake up.")

        preference = self.preferences.handle(prompt)
        if preference is not None:
            return self._preference_reply(preference)

        try:
            schedule = self.schedule.handle(prompt)
            if schedule is not None:
                return self._schedule_reply(schedule)
            research = self.research.handle(prompt)
            if research is not None:
                return self._research_reply(research)
            file_result = self.files.handle(prompt)
            if file_result is not None:
                return self._file_reply(file_result)
        except PrologUnavailable as exc:
            return self._reply(f"Prolog is unavailable: {exc}", ok=False)
        except (OSError, ValueError, PermissionError, ConnectionError) as exc:
            return self._reply(str(exc), ok=False)

        routine_reply = self.routine.handle(prompt)
        if routine_reply is not None:
            return self._reply(routine_reply)

        follow_up = self.routine.resolve_follow_up(prompt)
        if follow_up:
            return self._action_reply(self.actions.invoke(*follow_up))

        deterministic = self._deterministic(prompt)
        if deterministic is not None:
            return deterministic

        return self._model_command(prompt, cancel_event=self._chat_stop if source == "voice_chat" else None)

    def approve_action(self, action_id: str) -> dict[str, Any]:
        self.state.set("executing")
        try:
            return self._action_reply(self.actions.approve(action_id))
        finally:
            self.state.set("active")

    def cancel_action(self, action_id: str) -> dict[str, Any]:
        return self._action_reply(self.actions.cancel(action_id))

    def listen(self, *, execute: bool = True, conversation: bool = False) -> dict[str, Any]:
        if not conversation and self.voice_chat_active and self._chat_paused.is_set():
            self._chat_paused.clear()
            self.events.publish('voice.chat.resumed')
            return {'resumed': True}
        if not conversation and self.voice_chat_active:
            return {"text": "", "error": "Voice chat is already listening. End voice chat to use single-command Listen."}
        generation = self._voice_generation
        self.state.set("listening")
        try:
            try:
                heard = self.voice.listen_once()
            except VoiceInputUnavailable as exc:
                return {"text": "", "language": "unknown", "confidence": 0.0, "error": str(exc)}
            if generation != self._voice_generation:
                return {"text": "", "cancelled": True, "status": "cancelled"}
            if execute and self.voice_result_ready(heard):
                if self._voice_action_allowed(heard):
                    heard["result"] = self.command(str(heard["text"]), source="voice")
                else:
                    self._mark_uncertain_camera(heard)
            return heard
        finally:
            self.state.set("active")

    def stop_voice(self) -> dict[str, Any]:
        self._voice_generation += 1
        self.voice._handoff_wav = b''
        self.voice.stop()
        self.state.set("active")
        return {"ok": True, "message": "Voice input and output stopped."}

    @staticmethod
    def voice_result_ready(heard: dict[str, Any]) -> bool:
        return bool(str(heard.get("text", "")).strip()) and not bool(heard.get("cancelled"))

    @staticmethod
    def _is_camera_command(text: str) -> bool:
        command = " ".join(str(text or "").lower().split()).rstrip(".!?")
        return command in {
            "open camera", "start camera", "turn on camera", "open the camera",
            "start the camera", "turn on the camera",
        }

    @classmethod
    def _voice_action_allowed(cls, heard: dict[str, Any]) -> bool:
        """Require a reliable transcript for privacy-sensitive voice actions."""
        if not cls._is_camera_command(str(heard.get("text", ""))):
            return True
        if "confidence" not in heard:
            return True
        return (
            float(heard.get("confidence") or 0.0) >= 0.90
            and not bool(heard.get("low_confidence"))
            and not bool(heard.get("requires_clarification"))
        )

    def _mark_uncertain_camera(self, heard: dict[str, Any]) -> None:
        heard["requires_clarification"] = True
        heard["blocked_action"] = "open_camera"
        heard["error"] = "Camera stayed closed because the voice command was uncertain. The transcript is ready to edit or submit."
        self.events.publish("voice.clarification", **heard)

    @property
    def voice_chat_active(self) -> bool:
        return bool(self._chat_thread and self._chat_thread.is_alive() and not self._chat_stop.is_set())

    def start_voice_chat(self) -> dict[str, Any]:
        with self._chat_lock:
            if self._chat_thread and self._chat_thread.is_alive():
                return {"active": self.voice_chat_active}
            self._chat_stop = threading.Event()
            self._chat_paused.clear()
            self._chat_thread = threading.Thread(target=self._chat_worker, name="marlin-voice-chat", daemon=True)
            self._chat_thread.start()
        return {"active": True}

    def stop_voice_chat(self) -> dict[str, Any]:
        self._chat_stop.set()
        self.stop_voice()
        self.events.publish("voice.chat", active=False)
        return {"active": False}

    def _chat_wait_for_speech(self) -> bool:
        if self.voice.wait_for_barge_in(self._chat_stop):
            self._voice_generation += 1
            self.events.publish("voice.chat.interrupted")
            return True
        while not self._chat_stop.is_set():
            self.voice.wait(.1)
            thread = self.voice._speech_thread
            if thread is None or not thread.is_alive():
                return False
        return False

    def _chat_command(self, text: str) -> dict[str, Any]:
        """Run a voice turn while keeping wake-word interruption responsive."""
        completed = threading.Event()
        outcome: dict[str, Any] = {}

        def run() -> None:
            try:
                outcome["result"] = self.command(text, source="voice_chat")
            except Exception as exc:
                outcome["error"] = exc
            finally:
                completed.set()

        worker = threading.Thread(target=run, name="marlin-voice-turn", daemon=True)
        worker.start()
        while not completed.wait(.04) and not self._chat_stop.is_set():
            if self.voice.wait_for_barge_in(self._chat_stop):
                self._voice_generation += 1
                self.events.publish("voice.chat.interrupted")
                completed.wait(2)
                return {"interrupted": True, "message": "", "pending": None}
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("result", {"interrupted": True, "message": "", "pending": None})

    def _chat_worker(self) -> None:
        pending = None
        input_errors = 0
        self.events.publish("voice.chat", active=True)
        self.state.set("active")
        try:
            while not self._chat_stop.is_set():
                if self._chat_paused.is_set():
                    self._chat_stop.wait(.1)
                    continue
                interrupted = self._chat_wait_for_speech()
                if self._chat_stop.wait(0 if interrupted else .08):
                    break
                try:
                    heard = self.listen(execute=False, conversation=True)
                except Exception as exc:
                    self.events.publish('voice.error', error=f'Microphone retry: {exc}')
                    self._chat_stop.wait(2)
                    continue
                if self._chat_stop.is_set():
                    break
                if heard.get("cancelled"):
                    continue
                if heard.get('wake_only'):
                    self.voice.speak('Yes?')
                    continue
                if not self.voice_result_ready(heard):
                    if heard.get("text") or heard.get("requires_clarification"):
                        self.events.publish('voice.chat.retrying', text=heard.get('text', ''))
                        self._chat_stop.wait(.5)
                    elif heard.get("error") and "did not hear speech" not in str(heard["error"]).lower():
                        input_errors += 1
                        self.events.publish("voice.error", error=heard["error"])
                        self._chat_stop.wait(min(5, input_errors))
                    continue
                input_errors = 0
                text = str(heard["text"]).strip()
                normalized = text.lower().rstrip(".!?")
                self.events.publish("voice.chat.heard", text=text)
                if not self._voice_action_allowed(heard):
                    self._mark_uncertain_camera(heard)
                    continue
                if normalized in {"end voice chat", "stop conversation", "goodbye marlin", "stop listening"}:
                    break
                if pending is not None and not self.actions.is_pending(pending["id"]):
                    pending = None
                if pending is not None:
                    if normalized in {"approve", "approve action", "yes approve"} and float(heard.get("confidence", 0)) >= .8:
                        result = self.approve_action(pending["id"])
                        pending = None
                    elif normalized in {"cancel", "cancel action", "no cancel"}:
                        result = self.cancel_action(pending["id"])
                        pending = None
                    else:
                        self.voice.speak("Please say approve action or cancel action, or end voice chat.")
                        continue
                else:
                    try:
                        result = self._chat_command(text)
                    except Exception as exc:
                        self.events.publish('voice.error', error=f'That request failed: {exc}. Voice chat is still listening.')
                        continue
                    if result.get("interrupted"):
                        continue
                    pending = result.get("pending")
                    if pending and not self._chat_stop.is_set():
                        self.voice.speak(f"{pending['label']}. Target: {pending['target']}. Say approve action or cancel action.")
                if not self._chat_stop.is_set():
                    self.events.publish("voice.chat.result", **result)
        except Exception as exc:
            self.events.publish("voice.error", error=f"Voice chat stopped: {exc}")
        finally:
            if pending:
                self.actions.cancel(pending["id"])
                self.events.publish("voice.chat.cancelled", action_id=pending["id"])
            self._chat_stop.set()
            self.voice.stop()
            self.events.publish("voice.chat", active=False)

    def start_wake_listener(self) -> bool:
        if not self.settings.wake_word_enabled:
            return False
        if self._wake_thread and self._wake_thread.is_alive():
            return False
        self._wake_stop.clear()
        self._wake_thread = threading.Thread(target=self._wake_worker, name="marlin-wake-word", daemon=True)
        self._wake_thread.start()
        return True

    def shutdown(self) -> None:
        self._wake_stop.set()
        self.stop_voice_chat()
        self.voice.stop()
        self.routine.stop()

    def _wake_worker(self) -> None:
        listener = self.voice.wake_listener()
        try:
            listener.prepare()
            self.voice.wake_status = 'ready'
            self.events.publish("wake.ready", phrase="Hey MARLIN")
        except VoiceInputUnavailable as exc:
            self.voice.wake_status = str(exc)
            self.events.publish("wake.error", error=str(exc))
            return
        while not self._wake_stop.is_set():
            if self._chat_thread and self._chat_thread.is_alive() and not self._chat_paused.is_set():
                self._wake_stop.wait(.2)
                continue
            try:
                generation = self._voice_generation
                detected = self.voice.wait_for_wake(listener, self._wake_stop)
            except VoiceInputUnavailable as exc:
                self.voice.wake_status = str(exc)
                self.events.publish("wake.error", error=str(exc))
                self._wake_stop.wait(2.0)
                continue
            if not detected or generation != self._voice_generation:
                continue
            self.voice.wake_status = 'ready'
            self.state.set("active")
            self.events.publish("wake.detected", phrase="Hey MARLIN")
            if self.voice_chat_active and self._chat_paused.is_set():
                self._chat_paused.clear()
                self.events.publish('voice.chat.resumed')
            else:
                self.start_voice_chat()

    def start_index(self, root: str | Path, *, max_files: int | None = None) -> bool:
        if self._index_thread and self._index_thread.is_alive():
            return False
        self._index_thread = threading.Thread(
            target=self._index_worker,
            args=(str(root), max_files or self.settings.index_batch_size),
            name="marlin-indexer",
            daemon=True,
        )
        self._index_thread.start()
        return True

    def status(self) -> dict[str, Any]:
        model = self.model.health()
        return {
            "version": "2.0",
            "state": self.state.value,
            "voice_chat": self.voice_chat_active,
            "voice_chat_paused": self._chat_paused.is_set(),
            "model": model,
            "prolog": {
                "available": self.reasoning.engine.loaded,
                "error": self.prolog_startup_error,
            },
            "voice": self.voice.status(),
            "index": self.index_status,
            "index_progress": self.index_progress,
            "entities": self.knowledge.count_entities(),
            "relationships": self.knowledge.count_relationships(),
            "alarms": self.store.list_alarms(),
            "reminders": self.store.list_reminders(pending_only=False),
            "reminder_storage": str(self.store.database_path.resolve()),
            "schedule_items": self.store.list_schedule_items(),
            "schedule_plan": self.store.latest_schedule_plan(),
            "preferences": self.store.list_preferences(),
            "research": self.store.recent_research(3),
            "prolog_activity": self._last_prolog_activity,
            "recent_actions": self.store.recent_actions(12),
            "backup": str(self.backup_path) if self.backup_path else "",
        }

    def _summarize_research(self, prompt: str) -> str:
        turn = self.model.chat([
            {"role": "system", "content": "Summarise supplied web evidence only. Be concise and retain [number] citations."},
            {"role": "user", "content": prompt},
        ])
        return turn.content

    def _schedule_reply(self, result: dict[str, Any]) -> dict[str, Any]:
        kind = result["kind"]
        plan = result.get("plan")
        if plan:
            self._last_prolog_activity = {
                "predicate": "schedule_tasks/6", "facts": len(plan.get("blocks", [])),
                "rules": plan.get("explanation", {}).get("rules", []),
                "result": plan.get("status"), "proof": plan.get("explanation", {}),
            }
        if kind == "preview" or (kind == "created" and result.get("plan")):
            plan = result.get("plan")
            count = len((plan or {}).get("blocks", []))
            unscheduled = len((plan or {}).get("explanation", {}).get("unscheduled", []))
            return self._reply(f"Prolog prepared a schedule preview with {count} blocks and {unscheduled} unscheduled tasks. Apply or discard it.", data={"schedule": result})
        if kind == "created": return self._reply(f"Scheduled task saved: {result['item']['title']}.", data={"schedule": result})
        if kind == "event_created":
            item = result["item"]
            start = datetime.fromisoformat(item["start_at"]).astimezone().strftime("%A, %d %B at %I:%M %p")
            return self._reply(f"Schedule event saved: {item['title']} on {start}.", data={"schedule": result})
        if kind == "clarification":
            return self._reply(result["message"], ok=False, data={"schedule": result})
        if kind == "applied": return self._reply("Schedule applied." if plan else "There is no preview to apply.", ok=bool(plan), data={"schedule": result})
        if kind == "discarded": return self._reply("Schedule preview discarded." if plan else "There is no preview to discard.", data={"schedule": result})
        if kind == "conflicts":
            conflicts = result["conflicts"]
            self._last_prolog_activity = {"predicate": "interval_conflict/4", "facts": len(conflicts), "rules": ["overlapping_intervals"], "result": conflicts}
            return self._reply(f"Prolog found {len(conflicts)} schedule conflicts.", data={"schedule": result, "prolog_activity": self._last_prolog_activity})
        if kind == "explanation":
            block = result.get("block")
            reasons = (block or {}).get("explanation", {}).get("reasons", [])
            return self._reply(" ".join(reasons) if reasons else "I could not find that schedule block.", ok=bool(block), data={"schedule": result})
        return self._reply(f"Your schedule contains {len(result.get('items', []))} items.", data={"schedule": result})

    def _preference_reply(self, result: dict[str, Any]) -> dict[str, Any]:
        if result["kind"] == "list":
            prefs = result["preferences"]
            message = "I remember: " + "; ".join(f"{p['category'].replace('_', ' ')} = {p['value']}" for p in prefs) if prefs else "I have no active preferences saved."
        elif result["kind"] == "forgotten": message = f"Forgotten {result['count']} matching preference." if result["count"] else "I could not find that preference."
        else:
            pref = result["preference"]
            message = f"I will remember that you prefer {pref['value']}." if pref.get("active") else "I noted that pattern; I will wait for more evidence before treating it as a preference."
        return self._reply(message, data={"memory": result})

    def _research_reply(self, result: dict[str, Any]) -> dict[str, Any]:
        if "clarification" in result:
            return self._reply(result["clarification"], ok=False, data={"research": result}, speak=False)
        run = result if "sources" in result else (result.get("runs") or [{}])[0]
        if "saved" in result:
            return self._reply("Source saved to BrainOS." if result["saved"] else "I could not find that source.", ok=bool(result["saved"]), data={"research": result})
        if not run:
            return self._reply("No research has been saved yet.", data={"research": result})
        self._last_prolog_activity = {"predicate": "source_quality/2", "facts": len(run.get("sources", [])), "rules": ["freshness", "domain_diversity", "search_position"], "result": "ranked"}
        return self._reply(run.get("summary") or f"Found {len(run.get('sources', []))} sources.", data={"research": run, "prolog_activity": self._last_prolog_activity}, speak=False)

    def _file_reply(self, result: dict[str, Any]) -> dict[str, Any]:
        item = result["result"]
        if result["kind"] == "project":
            message = f"I found {item['files']} project files. Main technologies: {', '.join(item['technologies']) or 'not identified'}."
        elif result["kind"] == "related":
            message = f"Prolog found {len(item['related'])} related files."
        elif result["kind"] == "impact":
            message = f"Changing this file could affect {len(item['affected'])} files."
        else:
            message = item.get("summary", "File analysis complete.")[:500]
        if item.get("predicate"):
            self._last_prolog_activity = {"predicate": item["predicate"], "facts": len(item.get("related", item.get("affected", []))), "rules": ["imports", "shared_concept", "same_project"], "result": item}
        return self._reply(message, data={"file_understanding": result, "prolog_activity": self._last_prolog_activity}, speak=False)

    def graph(self, limit: int = 1200) -> dict[str, Any]:
        limit = max(100, min(limit, 2000))
        root_path = self.settings.graph_root.absolute()
        root = str(root_path).replace("\\", "/").rstrip("/").lower()
        cache_key = (root, limit)
        with self._graph_lock:
            cached = self._graph_cache.get(cache_key)
        if cached is not None:
            return cached
        # Match a whole path component, not similarly named sibling directories.
        scoped_path = "lower(replace(json_extract(metadata_json, '$.path'), char(92), '/'))"
        scope = f"({scoped_path} = ? OR substr({scoped_path}, 1, ?) = ?)"
        scope_args = (root, len(root) + 1, root + "/")
        with self.store.connect() as connection:
            hubs = connection.execute(
                "SELECT id, type, name, metadata_json FROM entities "
                f"WHERE type='folder' AND {scope} ORDER BY length(metadata_json), name",
                scope_args,
            ).fetchall()
            hub_ids = [str(row["id"]) for row in hubs]
            children = []
            if hub_ids:
                placeholders = ",".join("?" for _ in hub_ids)
                child_scope = scope.replace("metadata_json", "e.metadata_json")
                children = connection.execute(
                    "SELECT id, type, name, metadata_json FROM ("
                    "SELECT e.id, e.type, e.name, e.metadata_json, r.source_id, "
                    "ROW_NUMBER() OVER (PARTITION BY r.source_id ORDER BY e.type DESC, e.modified_at DESC) AS child_rank "
                    "FROM relationships r JOIN entities e ON e.id=r.target_id "
                    f"WHERE r.type='contains' AND e.type='file' AND r.source_id IN ({placeholders}) AND {child_scope}"
                    ") ORDER BY child_rank, source_id LIMIT ?",
                    (*hub_ids, *scope_args, max(0, limit - len(hubs))),
                ).fetchall()
            rows_by_id = {str(row["id"]): row for row in [*hubs, *children]}
            allowed = list(rows_by_id)
            relationships = []
            if allowed:
                placeholders = ",".join("?" for _ in allowed)
                relationships = connection.execute(
                    "SELECT id, source_id, target_id, type FROM relationships "
                    f"WHERE source_id IN ({placeholders}) AND target_id IN ({placeholders}) LIMIT ?",
                    (*allowed, *allowed, limit * 8),
                ).fetchall()

        def metadata(row: Any) -> dict[str, Any]:
            try:
                return json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                return {}

        nodes = [
            {"id": row["id"], "type": row["type"], "label": row["name"], "metadata": metadata(row)}
            for row in rows_by_id.values()
        ]
        links = [
            {"id": row["id"], "source": row["source_id"], "target": row["target_id"], "type": row["type"]}
            for row in relationships
        ]
        node_lookup = {node["id"]: node for node in nodes}
        extensions_by_hub: dict[str, set[str]] = {}
        for link in links:
            if link["type"] != "contains":
                continue
            child = node_lookup.get(link["target"])
            if not child or child["type"] != "file":
                continue
            suffix = Path(str(child["metadata"].get("path") or child["label"])).suffix.lower()
            if suffix:
                extensions_by_hub.setdefault(str(link["source"]), set()).add(suffix)
        previous_by_extension: dict[str, str] = {}
        for hub_id, extensions in extensions_by_hub.items():
            for suffix in sorted(extensions)[:3]:
                previous = previous_by_extension.get(suffix)
                if previous and previous != hub_id:
                    links.append({
                        "id": f"derived_{suffix}_{previous}_{hub_id}",
                        "source": previous,
                        "target": hub_id,
                        "type": "similar_files",
                        "derived": True,
                    })
                previous_by_extension[suffix] = hub_id

        result = {
            "nodes": nodes,
            "links": links,
            "root": str(root_path),
        }
        with self._graph_lock:
            self._graph_cache[cache_key] = result
        return result

    def _deterministic(self, prompt: str) -> dict[str, Any] | None:
        command = " ".join(prompt.lower().split()).rstrip('.!?')
        command = re.sub(r"^marlin[, ]+", "", command).strip()
        command = re.sub(r"\bturnoff\b", "turn off", command)
        command = re.sub(r"\bhisself\b|\bhis self\b", "yourself", command)
        polite_command = re.sub(r"^please\s+", "", command)
        polite_command = re.sub(r"\s+(?:please|now)$", "", polite_command).strip()
        self_exit = bool(re.fullmatch(
            r"(?:(?:turn|switch)\s+(?:yourself|your self|marlin)\s+off|"
            r"(?:turn|switch)\s+off\s+(?:yourself|your self|marlin)|"
            r"(?:shut|power)\s+(?:yourself|your self|marlin)\s+down|"
            r"(?:shut|power)\s+down\s+(?:yourself|your self|marlin)|"
            r"(?:close|exit|quit|stop)\s+(?:yourself|your self|marlin)|go offline)",
            polite_command,
        ))
        desktop_action = ('show' if command in {'show yourself', 'where are you', 'show marlin', 'open marlin', 'show the cockpit'}
                          else 'hide' if command in {'hide yourself', 'hide marlin', 'hide the cockpit', 'go to background'}
                          else 'exit' if self_exit or command in {
                              'close', 'close yourself', 'close marlin', 'exit', 'exit marlin', 'quit', 'quit marlin',
                              'turn off', 'turn off yourself', 'turn yourself off', 'turn off marlin',
                              'turn marlin off', 'power off', 'power off marlin', 'close down',
                              'shut down', 'shut down yourself', 'shut yourself down', 'shut down marlin',
                              'shutdown', 'shutdown marlin', 'goodbye marlin', 'stop marlin',
                          } else None)
        if desktop_action:
            if self.desktop is None:
                return self._reply('Start me with py main.py to use the background desktop controls.')
            return self._reply(self.desktop.control(desktop_action))
        if command in {'close that tab', 'close this tab', 'close that tap', 'close youtube', 'close the youtube tab'}:
            return self._action_reply(self.actions.invoke('close_browser_tab', {}))
        music = re.fullmatch(
            r'(?:please\s+)?(?:open\s+(?:the\s+)?(?:desktop\s+)?youtube(?:\s+desktop)?\s*,?\s*'
            r'(?:and|then|&)\s*play\s+(.+)|play\s+(.+?)\s+on\s+youtube|play\s+(some music|music))\s*[.!?]?',
            prompt.strip(),
            re.I,
        )
        if music:
            query = next(value for value in music.groups() if value).strip().rstrip('.!?')
            if query.lower() in {'some music', 'music', 'any music'}:
                query = 'relaxing music'
            return self._action_reply(self.actions.invoke('play_youtube', {'query': query}))
        if command == "stop voice":
            return self.stop_voice()
        if self._is_camera_command(command):
            return self._action_reply(self.actions.invoke("open_camera", {"_explicit_command": True}))
        if command in {"close camera", "stop camera", "turn off camera"}:
            return self._action_reply(self.actions.invoke("close_camera", {}))
        if command in {"show brain graph", "graph my files", "show graph"}:
            self.events.publish("graph.refresh")
            return self._reply("The brain graph is ready.", data={"graph": True})
        for prefix in ("why high priority ", "why high-priority "):
            if command.startswith(prefix):
                task_id = prompt[len(prefix):].strip()
                try:
                    explanation = self.reasoning.why_high_priority(task_id)
                    self.events.publish("prolog.result", query="why_high_priority", task_id=task_id, steps=explanation.steps)
                    return self._reply(explanation.title + " " + " ".join(explanation.steps), data={"steps": explanation.steps})
                except PrologUnavailable as exc:
                    return self._reply(f"Prolog is unavailable: {exc}")
        if command in {
            "explain graph", "explain the graph", "explain my graph", "explain brain graph",
            "what is in the graph", "what's in the graph", "what does the graph show",
            "tell me about the graph", "tell me about my graph", "explain my brain",
        }:
            return self._answer_graph_question(prompt)
        if self._looks_like_graph_question(command):
            return self._answer_graph_question(prompt)
        if self._is_high_priority_request(command):
            return self._high_priority_reply(explain_activity=self._asks_for_prolog_activity(command))
        match = re.match(r"search (?:my )?files for (.+)", prompt, re.I)
        if match:
            results = self.store.search_files(match.group(1), 30)
            return self._reply(f"Found {len(results)} indexed file matches.", data={"files": results})
        if command in {"open documents", "open my documents"}:
            return self._action_reply(self.actions.invoke("open_path", {"path": str(Path.home() / "Documents")}))
        known_folders = {
            "open desktop": Path.home() / "Desktop",
            "open my desktop": Path.home() / "Desktop",
            "open downloads": Path.home() / "Downloads",
            "open my downloads": Path.home() / "Downloads",
            "open pictures": Path.home() / "Pictures",
            "open my pictures": Path.home() / "Pictures",
            "open videos": Path.home() / "Videos",
            "open my videos": Path.home() / "Videos",
        }
        if command in known_folders:
            return self._action_reply(self.actions.invoke("open_path", {"path": str(known_folders[command])}))
        match = re.match(r"open (?:this )?(?:file|folder):\s*(.+)", prompt, re.I)
        if match:
            return self._action_reply(self.actions.invoke("open_path", {"path": match.group(1).strip()}))
        video_path = re.match(
            r"(?:play|watch|open|start)\s+(?:(?:this|the|my)\s+)?(?:video(?:\s+file)?\s*:?[ \t]*)?[\"']?(.+\.(?:3gp|avi|m4v|mkv|mov|mp4|mpeg|mpg|webm|wmv))[\"']?$",
            prompt.strip(),
            re.I,
        )
        if video_path:
            return self._action_reply(self.actions.invoke("play_video", {"path": video_path.group(1).strip()}))
        video_request = re.match(r"(?:play|watch|open|start)\s+(?:(?:this|the|my)\s+)?video(?:\s+file)?(?:\s+(.*))?$", prompt.strip(), re.I)
        if video_request:
            query = (video_request.group(1) or "").strip()
            path = self.actions.find_video(query)
            if path is None:
                detail = f" matching '{query}'" if query else ""
                return self._reply(f"I could not find a recent or indexed video{detail}. Give me its full path, for example: play C:\\Videos\\clip.mp4.", ok=False)
            return self._action_reply(self.actions.invoke("play_video", {"path": str(path)}))
        close_app = re.match(r"close\s+(?:the\s+)?(?:app(?:lication)?\s+)?(.+)$", prompt.strip(), re.I)
        if close_app:
            app = close_app.group(1).strip().rstrip('.!?')
            return self._action_reply(self.actions.invoke("close_app", {"app": app}))
        match = re.match(r"open\s+(.+)$", prompt, re.I)
        if match:
            target = match.group(1).strip().strip('"\'')
            expanded = Path(os.path.expandvars(target)).expanduser()
            if expanded.exists():
                return self._action_reply(self.actions.invoke("open_path", {"path": str(expanded)}))
            if self.actions.can_open_app(target):
                return self._action_reply(self.actions.invoke("open_app", {"app": target}))
            indexed = self.store.search_files(target, 6)
            existing = [item for item in indexed if Path(str(item.get("path") or "")).exists()]
            exact = [item for item in existing if str(item.get("name") or "").lower() == target.lower()]
            matches = exact or existing
            if len(matches) == 1:
                return self._action_reply(self.actions.invoke("open_path", {"path": matches[0]["path"]}))
            if len(matches) > 1:
                names = ", ".join(str(item["path"]) for item in matches[:3])
                return self._reply(f"I found several matching files. Say the full path: {names}", ok=False, data={"files": matches})
            return self._action_reply(self.actions.invoke("open_url", {"site": target}))
        media = {
            "play music": "play_pause", "pause music": "play_pause", "stop music": "stop",
            "next song": "next", "previous song": "previous", "volume up": "volume_up",
            "volume down": "volume_down", "mute": "mute",
        }
        if command in media:
            return self._action_reply(self.actions.invoke("media_control", {"command": media[command]}))
        if command in {"index c drive", "index windows c", "add c drive to brain"}:
            started = self.start_index(Path.home().anchor or "C:\\")
            return self._reply("C-drive indexing started in the background." if started else "C-drive indexing is already running.")
        return None

    @staticmethod
    def _looks_like_graph_question(command: str) -> bool:
        graph_terms = (
            "graph", "brain map", "knowledge map", "node", "nodes", "connected",
            "connection", "connections", "relationship", "relationships",
        )
        return any(term in command for term in graph_terms)

    @staticmethod
    def _is_high_priority_request(command: str) -> bool:
        return bool(re.search(r"\bhigh[- ]?priority\b", command)) and bool(
            re.search(r"\b(tasks?|work|priorities|prolog|activity|reasoning)\b", command)
        )

    @staticmethod
    def _asks_for_prolog_activity(command: str) -> bool:
        return any(term in command for term in (
            "prolog", "activity", "identify", "explain", "reason", "how did", "why",
        ))

    def _high_priority_reply(self, *, explain_activity: bool = False) -> dict[str, Any]:
        """Run the predefined Prolog query and expose its safe reasoning trace."""
        try:
            tasks = self.reasoning.high_priority_tasks()
            explanations: list[dict[str, Any]] = []
            for task in tasks:
                reason = self.reasoning.why_high_priority(task.id)
                explanations.append({"task_id": task.id, "task": task.name, "steps": reason.steps})
            task_ids = [task.id for task in tasks]
            activity = {
                "engine": "SWI-Prolog via PySWIP",
                "query": "high_priority(Task)",
                "facts_source": "SQLite projects, tasks, and relationships",
                "matched_task_ids": task_ids,
                "explanations": explanations,
            }
            if tasks:
                message = "High-priority tasks: " + ", ".join(task.name for task in tasks) + "."
            else:
                message = "No tasks are currently high priority."
            if explain_activity:
                if explanations:
                    reason_text = "; ".join(
                        f"{item['task']}: {' '.join(item['steps'])}" for item in explanations
                    )
                    message += (
                        " Prolog activity: Python loaded the SQLite task facts, ran "
                        f"high_priority(Task), and matched {reason_text}"
                    )
                else:
                    message += (
                        " Prolog activity: Python loaded the SQLite task facts and ran "
                        "high_priority(Task), but no rule matched."
                    )
            self.events.publish("prolog.result", **activity)
            return self._reply(message, data={"tasks": task_ids, "prolog_activity": activity})
        except PrologUnavailable as exc:
            return self._reply(f"Prolog is unavailable: {exc}", ok=False)

    def _answer_graph_question(self, prompt: str) -> dict[str, Any]:
        """Answer common graph questions from the exact snapshot used by the cockpit."""
        graph = self.graph(1800)
        nodes = graph["nodes"]
        links = graph["links"]
        root = Path(graph["root"])
        if not nodes:
            return self._reply(
                f"The graph has no indexed data under {root}. Index that folder, then ask me again.",
                ok=False,
                data={"graph": True, "graph_summary": {"root": str(root), "projects": []}},
            )
        folders = [node for node in nodes if node["type"] == "folder"]
        files = [node for node in nodes if node["type"] == "file"]
        projects: dict[str, dict[str, Any]] = {}
        extensions: dict[str, int] = {}
        project_for_node: dict[str, str] = {}
        for node in nodes:
            path_value = str(node.get("metadata", {}).get("path") or "")
            if path_value:
                try:
                    relative = Path(path_value).resolve().relative_to(root.resolve())
                    if relative.parts:
                        project = relative.parts[0]
                        project_for_node[str(node["id"])] = project
                        details = projects.setdefault(project, {"files": 0, "folders": 0, "extensions": {}})
                        if node["type"] == "file":
                            details["files"] += 1
                        elif len(relative.parts) > 1:
                            details["folders"] += 1
                except (OSError, ValueError):
                    pass
            if node["type"] == "file":
                suffix = Path(path_value or node["label"]).suffix.lower() or "no extension"
                extensions[suffix] = extensions.get(suffix, 0) + 1
                project = project_for_node.get(str(node["id"]))
                if project:
                    project_extensions = projects[project]["extensions"]
                    project_extensions[suffix] = project_extensions.get(suffix, 0) + 1
        relationship_counts: dict[str, int] = {}
        for link in links:
            relation = str(link.get("type") or "related")
            relationship_counts[relation] = relationship_counts.get(relation, 0) + 1
        top_extensions = sorted(extensions.items(), key=lambda item: (-item[1], item[0]))[:5]
        project_names = sorted(projects)
        project_text = ", ".join(project_names[:6]) or "no indexed projects yet"
        if len(project_names) > 6:
            project_text += f", and {len(project_names) - 6} more"
        extension_text = ", ".join(f"{name} ({count})" for name, count in top_extensions) or "none"
        summary_message = (
            f"This graph represents {len(project_names)} projects under {root}, with {len(folders)} folders, "
            f"{len(files)} files, and {len(links)} visible relationships. Main projects: {project_text}. "
            f"The most common file types are {extension_text}; solid links mean folder containment and cross-links connect folders sharing file types."
        )
        normalized = " ".join(prompt.lower().split())
        focused_project = next(
            (name for name in sorted(project_names, key=len, reverse=True) if name.lower() in normalized),
            None,
        )
        focus_nodes = [
            node for node in nodes
            if str(node.get("label", "")).lower() in normalized
            and len(str(node.get("label", ""))) >= 3
        ]
        related: list[str] = []
        if focus_nodes and any(term in normalized for term in ("connect", "relation", "link")):
            focus_ids = {str(node["id"]) for node in focus_nodes}
            by_id = {str(node["id"]): node for node in nodes}
            related_ids: set[str] = set()
            for link in links:
                source, target = str(link["source"]), str(link["target"])
                if source in focus_ids:
                    related_ids.add(target)
                if target in focus_ids:
                    related_ids.add(source)
            related = sorted({str(by_id[node_id]["label"]) for node_id in related_ids if node_id in by_id})[:12]
            focus_label = str(focus_nodes[0]["label"])
            message = (
                f"{focus_label} has {len(related_ids)} visible direct connections. "
                + (f"Connected nodes include {', '.join(related)}." if related else "No direct connected nodes are visible in the current snapshot.")
            )
        elif focused_project:
            details = projects[focused_project]
            common = sorted(details["extensions"].items(), key=lambda item: (-item[1], item[0]))[:4]
            common_text = ", ".join(f"{ext} ({count})" for ext, count in common) or "no file types yet"
            project_files = [
                str(node["label"]) for node in files
                if project_for_node.get(str(node["id"])) == focused_project
            ]
            examples = ", ".join(project_files[:8])
            message = (
                f"{focused_project} contains {details['folders']} nested folders and {details['files']} visible files. "
                f"Its main file types are {common_text}."
            )
            if examples and any(term in normalized for term in ("file", "contain", "inside", "show")):
                message += f" Example files: {examples}."
        elif any(term in normalized for term in ("largest", "biggest", "most files", "main project")):
            ranked = sorted(projects.items(), key=lambda item: (-item[1]["files"], item[0].lower()))[:5]
            ranking = ", ".join(f"{name} ({details['files']} files)" for name, details in ranked)
            message = f"The largest visible projects by file count are {ranking}."
        elif any(term in normalized for term in ("how many", "count", "statistics", "stats")):
            message = (
                f"The visible graph has {len(project_names)} main projects, {len(folders)} folders, "
                f"{len(files)} files, and {len(links)} relationships."
            )
        elif any(term in normalized for term in ("which project", "list project", "what project")):
            message = f"The graph contains {len(project_names)} main projects: {project_text}."
        else:
            message = summary_message
        summary = {
            "root": str(root),
            "projects": project_names,
            "folders": len(folders),
            "files": len(files),
            "relationships": relationship_counts,
            "top_extensions": [{"extension": name, "count": count} for name, count in top_extensions],
            "focused_project": focused_project,
            "related_nodes": related,
        }
        self.events.publish("graph.explained", summary=summary)
        return self._reply(message, data={"graph": True, "graph_summary": summary})

    def _graph_explanation(self) -> dict[str, Any]:
        """Compatibility wrapper for callers using the original helper name."""
        return self._answer_graph_question("explain my graph")

    def _model_command(self, prompt: str, *, cancel_event: threading.Event | None = None) -> dict[str, Any]:
        generation = self._voice_generation
        turn_started = time.perf_counter()
        first_token_seen = False
        def interrupted() -> bool:
            return generation != self._voice_generation or bool(cancel_event and cancel_event.is_set())

        self.state.set("thinking")
        self.events.publish("assistant.thinking", text=prompt)
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(self.store.recent_messages(6))
        tools = TOOL_SCHEMAS if self._may_need_tool(prompt) else None
        speech = self.voice.begin_stream() if tools is None else None

        def token_received(token: str) -> None:
            nonlocal first_token_seen
            if interrupted():
                raise LocalModelUnavailable("Reply interrupted.")
            if not first_token_seen:
                first_token_seen = True
                self.events.publish("assistant.timing", first_token_ms=round((time.perf_counter() - turn_started) * 1000))
            self.events.publish("assistant.delta", text=token)
            if speech is not None:
                speech.feed(token)

        try:
            for _round in range(4):
                turn = self.model.chat(
                    messages,
                    tools=tools,
                    on_token=token_received,
                )
                if interrupted():
                    raise LocalModelUnavailable("Reply interrupted.")
                messages.append(turn.raw_message)
                if not turn.tool_calls:
                    reply = turn.content or "I could not form a useful local response."
                    if speech is not None:
                        speech.finish()
                    return self._reply(reply, speak=speech is None)
                for call in turn.tool_calls:
                    if interrupted():
                        raise LocalModelUnavailable("Reply interrupted.")
                    self.events.publish("tool.call", name=call.name, arguments=call.arguments)
                    outcome = self._execute_model_tool(call.name, call.arguments)
                    if outcome.pending:
                        return self._action_reply(outcome)
                    messages.append({"role": "tool", "content": json.dumps(outcome.to_dict(), ensure_ascii=False)[:7000]})
            final = self.model.chat(messages, tools=None, on_token=token_received)
            if interrupted():
                raise LocalModelUnavailable("Reply interrupted.")
            return self._reply(final.content or "I completed the local tool steps but could not summarize them.")
        except LocalModelUnavailable as exc:
            if speech is not None:
                speech.cancel.set()
            if interrupted():
                self.events.publish("assistant.interrupted")
                return {"ok": True, "message": "", "interrupted": True, "pending": None, "client_action": None}
            return self._reply(str(exc), ok=False)
        finally:
            if speech is not None:
                speech.finish()
            self.state.set("active")

    @staticmethod
    def _may_need_tool(prompt: str) -> bool:
        command = " ".join(prompt.lower().split())
        tool_words = {
            "file", "folder", "document", "project", "task", "camera", "app",
            "open", "close", "delete", "move", "rename", "copy", "create",
            "search", "find", "read", "index", "graph", "remind", "alarm",
            "music", "volume", "priority",
        }
        return any(re.search(rf"\b{re.escape(word)}\b", command) for word in tool_words)

    def _execute_model_tool(self, name: str, arguments: dict[str, Any]) -> ActionOutcome:
        if name == "open_app" and str(arguments.get("app") or "").strip().lower() in {
            "camera", "the camera", "windows camera", "microsoft camera",
        }:
            return ActionOutcome(False, "Camera stayed closed. Use the exact command: open camera.")
        if name == "search_brain":
            results = self.store.search_files(str(arguments.get("query", "")), 25)
            return ActionOutcome(True, f"Found {len(results)} brain matches.", {"files": results})
        if name == "high_priority_tasks":
            try:
                tasks = self.reasoning.high_priority_tasks()
                return ActionOutcome(True, "Prolog reasoning complete.", {"tasks": [{"id": task.id, "name": task.name} for task in tasks]})
            except PrologUnavailable as exc:
                return ActionOutcome(False, str(exc))
        return self.actions.invoke(name, arguments)

    def _reply(self, message: str, *, ok: bool = True, data: dict[str, Any] | None = None, speak: bool = True) -> dict[str, Any]:
        payload = {"ok": ok, "message": message, "data": data or {}, "pending": None, "client_action": None}
        self.store.add_message("assistant", message)
        self.events.publish("assistant.done", **payload)
        if ok and speak:
            self.voice.speak(message)
        return payload

    def _action_reply(self, outcome: ActionOutcome) -> dict[str, Any]:
        payload = outcome.to_dict()
        if outcome.pending:
            self.events.publish("action.preview", action=outcome.pending.to_dict())
        else:
            self.events.publish("action.result", **payload)
        if outcome.message:
            self.store.add_message("assistant", outcome.message)
            if outcome.ok and not outcome.pending:
                self.voice.speak(outcome.message)
        return payload

    def _index_worker(self, root: str, max_files: int) -> None:
        self.index_status = "running"
        with self._graph_lock:
            self._graph_cache.clear()
        self.events.publish("index.progress", root=root, indexed=0, skipped=0, complete=False)
        try:
            result = self.indexer.index(root, max_files=max_files, on_progress=self._index_progress)
            if Path(root).resolve() == self.settings.graph_root.resolve():
                for user_root in (Path.home() / "Desktop", Path.home() / "Documents", Path.home() / "Downloads"):
                    if user_root.exists() and user_root.resolve() != self.settings.graph_root.resolve():
                        self.indexer.index(user_root, max_files=max_files, on_progress=self._index_progress)
            self.index_status = "complete" if result.complete else "paused"
            self.events.publish("graph.refresh")
        except Exception as exc:
            self.index_status = f"failed: {exc}"
            self.events.publish("index.error", error=str(exc))

    def _index_progress(self, progress: IndexProgress) -> None:
        self.index_progress = progress.to_dict()
        self.events.publish("index.progress", **self.index_progress)

    def _alarm_fired(self, alarm: dict[str, Any]) -> None:
        label = str(alarm.get("label") or "Alarm")
        message = f"{label}. Would you like five more minutes?"
        self.events.publish("assistant.done", ok=True, message=message, data={"alarm": alarm})
        play_notification_sound()
        self.voice.speak(message)

    def _reminder_fired(self, reminder: dict[str, Any]) -> None:
        message = f"Reminder: {reminder.get('text') or 'You have something scheduled.'}"
        self.events.publish("assistant.done", ok=True, message=message, data={"reminder": reminder})
        play_notification_sound()
        self.voice.speak(message)
