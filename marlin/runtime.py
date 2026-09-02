"""Shared MARLIN V2 command, reasoning, memory, and action runtime."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from marlin.actions import ActionOutcome, ComputerActionService
from marlin.config import MarlinSettings
from marlin.events import EventBus
from marlin.indexer import IncrementalIndexer, IndexProgress
from marlin.local_model import LocalModelUnavailable, OllamaLocalModel
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
Keep spoken replies concise, normally one to three sentences. Reply in English, Burmese, or mixed language to match the user.
When the user refers to "it", "them", or "that project", use recent context instead of guessing.
"""


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "read_file", "description": "Read a text file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "list_folder", "description": "List folder contents.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "find_files", "description": "Find files by wildcard name.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "pattern": {"type": "string"}}, "required": ["path", "pattern"]}}},
    {"type": "function", "function": {"name": "grep_files", "description": "Search text inside files.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "query": {"type": "string"}}, "required": ["path", "query"]}}},
    {"type": "function", "function": {"name": "open_path", "description": "Open an existing file or folder.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
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
        self.events = EventBus()
        self.state = AssistantState(self.store, self.events)
        self.voice = LocalVoiceService(self.settings, self.events)
        self.actions = ComputerActionService(self.store)
        self.model = OllamaLocalModel(self.settings)
        self.indexer = IncrementalIndexer(self.knowledge, self.store)
        self.routine = RoutineService(
            self.settings,
            self.store,
            self.reasoning,
            self.state,
            self.events,
            on_alarm=self._alarm_fired,
        )
        self._wake_stop = threading.Event()
        self._wake_thread: threading.Thread | None = None
        if start_background:
            self.routine.start()
            self.voice.stt.preload_async()
            self.model.preload_async()
            self.start_wake_listener()
        self._index_thread: threading.Thread | None = None
        self.index_status = "idle"
        self.index_progress: dict[str, Any] = {"root": "", "indexed": 0, "skipped": 0, "complete": False}
        if start_background and self.settings.auto_index_c_drive:
            self.start_index(Path.home().anchor or "C:\\", max_files=self.settings.index_batch_size)

    def command(self, text: str, *, source: str = "ui") -> dict[str, Any]:
        prompt = str(text or "").strip()
        if not prompt:
            return self._reply("I did not catch that.")
        self.events.publish("user.message", text=prompt, source=source)
        self.store.add_message("user", prompt)

        if self.state.value == "standby" and "wake up" not in prompt.lower():
            return self._reply("MARLIN is standing by. Say MARLIN, wake up.")

        routine_reply = self.routine.handle(prompt)
        if routine_reply is not None:
            return self._reply(routine_reply)

        follow_up = self.routine.resolve_follow_up(prompt)
        if follow_up:
            return self._action_reply(self.actions.invoke(*follow_up))

        deterministic = self._deterministic(prompt)
        if deterministic is not None:
            return deterministic

        return self._model_command(prompt)

    def approve_action(self, action_id: str) -> dict[str, Any]:
        self.state.set("executing")
        try:
            return self._action_reply(self.actions.approve(action_id))
        finally:
            self.state.set("active")

    def cancel_action(self, action_id: str) -> dict[str, Any]:
        return self._action_reply(self.actions.cancel(action_id))

    def listen(self, *, execute: bool = True) -> dict[str, Any]:
        self.state.set("listening")
        try:
            try:
                heard = self.voice.listen_once()
            except VoiceInputUnavailable as exc:
                return {"text": "", "language": "unknown", "confidence": 0.0, "error": str(exc)}
            if execute and heard["text"]:
                heard["result"] = self.command(str(heard["text"]), source="voice")
            return heard
        finally:
            self.state.set("active")

    def stop_voice(self) -> dict[str, Any]:
        self.voice.stop()
        self.state.set("active")
        return {"ok": True, "message": "Voice input and output stopped."}

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
        self.voice.stop()
        self.routine.stop()

    def _wake_worker(self) -> None:
        listener = self.voice.wake_listener()
        try:
            listener.prepare()
            self.events.publish("wake.ready", phrase="Hey MARLIN")
        except VoiceInputUnavailable as exc:
            self.events.publish("wake.error", error=str(exc))
            return
        while not self._wake_stop.is_set():
            try:
                detected = self.voice.wait_for_wake(listener, self._wake_stop)
            except VoiceInputUnavailable as exc:
                self.events.publish("wake.error", error=str(exc))
                self._wake_stop.wait(2.0)
                continue
            if not detected:
                continue
            self.state.set("active")
            self.events.publish("wake.detected", phrase="Hey MARLIN")
            self.voice.speak("Yes, sir?")
            self.voice.wait(3.0)
            heard = self.listen(execute=False)
            text = str(heard.get("text") or "").strip()
            if not text:
                if heard.get("error"):
                    self.events.publish("wake.error", error=heard["error"])
                continue
            self.events.publish("wake.heard", text=text)
            result = self.command(text, source="wake")
            self.events.publish("wake.result", **result)

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
            "model": model,
            "prolog": {"available": self.reasoning.engine.is_available()},
            "voice": self.voice.status(),
            "index": self.index_status,
            "index_progress": self.index_progress,
            "entities": self.knowledge.count_entities(),
            "relationships": self.knowledge.count_relationships(),
            "alarms": self.store.list_alarms(),
            "reminders": self.store.list_reminders(),
            "recent_actions": self.store.recent_actions(12),
            "backup": str(self.backup_path) if self.backup_path else "",
        }

    def graph(self, limit: int = 1200) -> dict[str, Any]:
        limit = max(100, min(limit, 2000))
        semantic_budget = min(180, limit // 5)
        hub_budget = max(12, min(90, (limit - semantic_budget) // 14))
        child_budget = max(8, min(24, (limit - semantic_budget - hub_budget) // hub_budget))
        with self.store.connect() as connection:
            semantic = connection.execute(
                "SELECT id, type, name, metadata_json FROM entities "
                "WHERE type NOT IN ('file', 'folder') ORDER BY modified_at DESC LIMIT ?",
                (semantic_budget,),
            ).fetchall()
            hubs = connection.execute(
                "SELECT e.id, e.type, e.name, e.metadata_json, COUNT(r.id) AS child_count "
                "FROM entities e JOIN relationships r ON r.source_id=e.id AND r.type='contains' "
                "JOIN entities child ON child.id=r.target_id AND child.type='file' "
                "WHERE e.type='folder' GROUP BY e.id ORDER BY child_count DESC, e.modified_at DESC LIMIT ?",
                (hub_budget,),
            ).fetchall()
            hub_ids = [str(row["id"]) for row in hubs]
            children = []
            if hub_ids:
                placeholders = ",".join("?" for _ in hub_ids)
                children = connection.execute(
                    "SELECT id, type, name, metadata_json FROM ("
                    "SELECT e.id, e.type, e.name, e.metadata_json, r.source_id, "
                    "ROW_NUMBER() OVER (PARTITION BY r.source_id ORDER BY e.type DESC, e.modified_at DESC) AS child_rank "
                    "FROM relationships r JOIN entities e ON e.id=r.target_id "
                    f"WHERE r.type='contains' AND e.type='file' AND r.source_id IN ({placeholders})"
                    ") WHERE child_rank <= ?",
                    (*hub_ids, child_budget),
                ).fetchall()
            rows_by_id = {str(row["id"]): row for row in [*semantic, *hubs, *children]}
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

        return {
            "nodes": nodes,
            "links": links,
        }

    def _deterministic(self, prompt: str) -> dict[str, Any] | None:
        command = " ".join(prompt.lower().split())
        if command in {"open camera", "start camera", "turn on camera"}:
            return self._action_reply(self.actions.invoke("open_camera", {}))
        if command in {"close camera", "stop camera", "turn off camera"}:
            return self._action_reply(self.actions.invoke("close_camera", {}))
        if command in {"show brain graph", "graph my files", "show graph"}:
            self.events.publish("graph.refresh")
            return self._reply("The brain graph is ready.", data={"graph": True})
        if command in {"high priority tasks", "show high priority tasks"}:
            try:
                tasks = self.reasoning.high_priority_tasks()
                message = "High-priority tasks: " + ", ".join(task.name for task in tasks) if tasks else "No tasks are currently high priority."
                self.events.publish("prolog.result", query="high_priority", task_ids=[task.id for task in tasks])
                return self._reply(message, data={"tasks": [task.id for task in tasks]})
            except PrologUnavailable as exc:
                return self._reply(f"Prolog is unavailable: {exc}")
        for prefix in ("why high priority ", "why high-priority "):
            if command.startswith(prefix):
                task_id = prompt[len(prefix):].strip()
                try:
                    explanation = self.reasoning.why_high_priority(task_id)
                    self.events.publish("prolog.result", query="why_high_priority", task_id=task_id, steps=explanation.steps)
                    return self._reply(explanation.title + " " + " ".join(explanation.steps), data={"steps": explanation.steps})
                except PrologUnavailable as exc:
                    return self._reply(f"Prolog is unavailable: {exc}")
        match = re.match(r"search (?:my )?files for (.+)", prompt, re.I)
        if match:
            results = self.store.search_files(match.group(1), 30)
            return self._reply(f"Found {len(results)} indexed file matches.", data={"files": results})
        if command in {"open documents", "open my documents"}:
            return self._action_reply(self.actions.invoke("open_path", {"path": str(Path.home() / "Documents")}))
        match = re.match(r"open (?:this )?(?:file|folder):\s*(.+)", prompt, re.I)
        if match:
            return self._action_reply(self.actions.invoke("open_path", {"path": match.group(1).strip()}))
        match = re.match(r"open\s+(.+)$", prompt, re.I)
        if match:
            target = match.group(1).strip()
            if self.actions.can_open_app(target):
                return self._action_reply(self.actions.invoke("open_app", {"app": target}))
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

    def _model_command(self, prompt: str) -> dict[str, Any]:
        self.state.set("thinking")
        self.events.publish("assistant.thinking", text=prompt)
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(self.store.recent_messages(8))
        tools = TOOL_SCHEMAS if self._may_need_tool(prompt) else None
        try:
            for _round in range(4):
                turn = self.model.chat(
                    messages,
                    tools=tools,
                    on_token=lambda token: self.events.publish("assistant.delta", text=token),
                )
                messages.append(turn.raw_message)
                if not turn.tool_calls:
                    reply = turn.content or "I could not form a useful local response."
                    return self._reply(reply)
                for call in turn.tool_calls:
                    self.events.publish("tool.call", name=call.name, arguments=call.arguments)
                    outcome = self._execute_model_tool(call.name, call.arguments)
                    if outcome.pending:
                        return self._action_reply(outcome)
                    messages.append({"role": "tool", "content": json.dumps(outcome.to_dict(), ensure_ascii=False)[:7000]})
            final = self.model.chat(messages, tools=None, on_token=lambda token: self.events.publish("assistant.delta", text=token))
            return self._reply(final.content or "I completed the local tool steps but could not summarize them.")
        except LocalModelUnavailable as exc:
            return self._reply(str(exc), ok=False)
        finally:
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

    def _reply(self, message: str, *, ok: bool = True, data: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"ok": ok, "message": message, "data": data or {}, "pending": None, "client_action": None}
        self.store.add_message("assistant", message)
        self.events.publish("assistant.done", **payload)
        if ok:
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
        self.events.publish("index.progress", root=root, indexed=0, skipped=0, complete=False)
        try:
            result = self.indexer.index(root, max_files=max_files, on_progress=self._index_progress)
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
        self.voice.speak(message)
