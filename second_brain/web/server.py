"""Stdlib local web server for the MARLIN dashboard."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape
from urllib.parse import urlparse

from second_brain.ai.action_dispatcher import ActionDispatcher
from second_brain.ai.agent import MarlinAgent, create_agent
from second_brain.ai.intent_parser import IntentParser
from second_brain.ai.llm import LLMUnavailable, create_llm_client
from second_brain.ai.local_conversation import LocalConversationService
from second_brain.ai.opencode_client import OpenCodeConversationClient, OpenCodeUnavailable
from second_brain.ai.response_generator import ResponseGenerator
from second_brain.ai.intent_schema import StructuredIntent
from second_brain.ai.tts import TTSUnavailable, create_tts_client
from second_brain.computer.actions import (
    COMPUTER_INTENTS,
    ComputerActionService,
    looks_like_blocked_computer_request,
    parse_allowed_apps,
    parse_computer_command,
)
from second_brain.core.audit import read_audit, record_audit
from second_brain.core.cockpit_state import (
    build_file_extension_chart,
    read_latest_chart,
    save_latest_chart,
)
from second_brain.core.config import Settings
from second_brain.database.connection import initialize_database
from second_brain.files.indexer import FileIndexer
from second_brain.knowledge.service import KnowledgeService
from second_brain.reasoning.prolog_engine import PrologUnavailable
from second_brain.reasoning.service import ReasoningService


class MarlinRuntime:
    def __init__(self, settings: Settings):
        self.settings = settings
        initialize_database(settings.database_path)
        self.knowledge = KnowledgeService(settings.database_path)
        self.reasoning = ReasoningService(self.knowledge, settings.prolog_dir)
        self.dispatcher = ActionDispatcher(self.knowledge, self.reasoning)
        self.responses = ResponseGenerator()
        self.local_conversation = LocalConversationService(self.knowledge)
        self.conversation = OpenCodeConversationClient(
            command=settings.opencode_command,
            model=settings.opencode_model or None,
            timeout_seconds=settings.opencode_timeout_seconds,
            use_server=settings.opencode_use_server,
            server_host=settings.opencode_server_host,
            server_port=settings.opencode_server_port,
            continue_session=settings.opencode_continue_session,
        )
        self.voice_process: subprocess.Popen | None = None
        self.agent: MarlinAgent | None = None
        self.latest_chart: dict | None = None
        self.c_drive_index_status = "pending"
        self.prolog_activity = "idle"
        self.opencode_status = (
            f"warming {self.conversation.server_url}"
            if settings.conversation_provider.lower().strip() == "opencode" and settings.opencode_use_server
            else "one-shot"
        )
        self.computer = ComputerActionService(
            self.knowledge,
            allowed_apps=parse_allowed_apps(settings.allowed_apps),
            confirmation_mode=settings.computer_confirmation,
        )

    def state(self) -> dict:
        graph_entities, graph_relationships, totals = self._graph_snapshot()
        return {
            "assistant": "MARLIN",
            "llm_provider": self.settings.llm_provider,
            "conversation_provider": self.settings.conversation_provider,
            "llm_model": self.settings.openai_model
            if self.settings.llm_provider == "openai"
            else self.settings.openrouter_model
            if self.settings.llm_provider == "openrouter"
            else self.settings.groq_model
            if self.settings.llm_provider == "groq"
            else self.settings.ollama_model,
            "tts_provider": self.settings.tts_provider,
            "tts_voice": self.settings.gemini_tts_voice
            if self.settings.tts_provider.lower().strip() == "gemini"
            else "windows",
            "voice_mode": self.settings.voice_mode,
            "computer_confirmation": self.settings.computer_confirmation,
            "status": self.status(),
            "audit_log": read_audit(),
            "latest_chart": self.latest_chart or read_latest_chart(),
            "c_drive_index_status": self.c_drive_index_status,
            "prolog_activity": self.prolog_activity,
            "total_entities": totals["entities"],
            "total_relationships": totals["relationships"],
            "total_files": totals["files"],
            "entities": graph_entities,
            "relationships": graph_relationships,
        }

    def status(self) -> dict:
        return {
            "llm": f"{self.settings.llm_provider}:{self._llm_model_name()}",
            "conversation": self.settings.conversation_provider,
            "prolog": self.prolog_activity,
            "voice": f"{self.settings.voice_mode}/{self.settings.tts_provider}",
            "file_index": f"{self._file_count()} files",
            "c_drive": self.c_drive_index_status,
            "computer": "autonomous, full file access",
            "opencode_server": self.opencode_status,
        }

    def warm_opencode_server(self) -> None:
        if self.settings.conversation_provider.lower().strip() != "opencode" or not self.settings.opencode_use_server:
            return
        try:
            self.conversation.ensure_server()
            self.opencode_status = f"ready {self.conversation.server_url}"
        except OpenCodeUnavailable as exc:
            self.opencode_status = str(exc)

    def _file_count(self) -> int:
        return len(self.knowledge.list_entities("file"))

    def _graph_snapshot(self, *, max_entities: int = 1200, max_relationships: int = 4200) -> tuple[list[dict], list[dict], dict]:
        selected_relationships = self.knowledge.list_relationships_limited(max_relationships)
        ordered_ids: list[str] = []
        seen_ids: set[str] = set()
        for relationship in selected_relationships:
            for entity_id in (relationship.source_id, relationship.target_id):
                if entity_id not in seen_ids:
                    seen_ids.add(entity_id)
                    ordered_ids.append(entity_id)
                if len(ordered_ids) >= max_entities:
                    break
            if len(ordered_ids) >= max_entities:
                break

        selected_ids = set(ordered_ids)
        selected_relationships = [
            relationship
            for relationship in selected_relationships
            if relationship.source_id in selected_ids and relationship.target_id in selected_ids
        ]
        selected_entities = self.knowledge.get_entities(ordered_ids)
        return (
            [_entity_dict(entity) for entity in selected_entities],
            [
                {
                    "id": relationship.id,
                    "source_id": relationship.source_id,
                    "target_id": relationship.target_id,
                    "type": relationship.type,
                }
                for relationship in selected_relationships
            ],
            {
                "entities": self.knowledge.count_entities(),
                "relationships": self.knowledge.count_relationships(),
                "files": self.knowledge.count_entities("file"),
            },
        )

    def _llm_model_name(self) -> str:
        if self.settings.llm_provider == "openai":
            return self.settings.openai_model
        if self.settings.llm_provider == "openrouter":
            return self.settings.openrouter_model
        if self.settings.llm_provider == "groq":
            return self.settings.groq_model
        return self.settings.ollama_model

    def ask(self, text: str) -> str:
        direct = self.direct_command_reply(text)
        if direct is not None:
            return direct
        return self._conversation_reply(text)

    def _conversation_reply(self, text: str) -> str:
        prompt = text.strip()
        lowered = prompt.lower()
        force_opencode = False
        for prefix in ("opencode:", "deep:", "ask opencode "):
            if lowered.startswith(prefix):
                prompt = prompt[len(prefix):].strip()
                force_opencode = True
                break

        provider = self.settings.conversation_provider.lower().strip()
        if provider == "opencode" or force_opencode:
            try:
                return self.conversation.reply(prompt)
            except OpenCodeUnavailable as exc:
                if force_opencode:
                    return f"OpenCode could not answer: {exc}"
                return self._llm_conversation_reply(prompt)
        return self._llm_conversation_reply(prompt)

    def _llm_conversation_reply(self, text: str) -> str:
        try:
            if self.agent is None:
                self.agent = create_agent(self.settings, self.knowledge, self.reasoning)
            return self.agent.reply(text)
        except LLMUnavailable as exc:
            return (
                f"Real AI conversation is not available yet: {exc} "
                "For fastest replies, add GROQ_API_KEY to .env and keep SECOND_BRAIN_LLM_PROVIDER=groq."
            )

    def preview_intent(self, text: str) -> dict:
        if looks_like_blocked_computer_request(text):
            self._record_audit("blocked", "Blocked computer request", text, "blocked")
            return {
                "type": "blocked",
                "requires_confirmation": False,
                "reply": "That is blocked. MARLIN routes work through typed tools rather than a shell, so it cannot run shell or command-prompt commands.",
                "state": self.state(),
            }
        intent = parse_computer_command(text)
        if intent is None:
            chart = self.try_graph_request(text)
            if chart:
                self._record_audit("visualization", chart["title"], "cockpit", "complete")
                return {
                    "type": "visualization",
                    "requires_confirmation": False,
                    "reply": f"Rendered visualization: {chart['title']}.",
                    "chart": chart,
                    "state": self.state(),
                }
            direct = self.direct_command_reply(text)
            if direct is not None:
                self._record_audit("brain", "Brain command", text, "complete")
                return {
                    "type": "chat_or_brain",
                    "requires_confirmation": False,
                    "reply": direct,
                    "state": self.state(),
                }
            if self.settings.conversation_provider.lower().strip() in {
                "agent",
                "opencode",
                "llm",
                "ai",
            }:
                return {
                    "type": "conversation",
                    "requires_confirmation": False,
                    "reply": self._conversation_reply(text),
                    "state": self.state(),
                }
            client = create_llm_client(self.settings)
            intent = IntentParser(client).parse(text)
        if intent.intent in COMPUTER_INTENTS:
            action = self.computer.preview(intent)
            if action is None:
                raise ValueError("No supported computer action found.")
            # Full autonomous mode: computer actions run immediately and are
            # recorded in the audit log instead of waiting for a confirm card.
            result = self.computer.execute(action)
            payload = {
                "type": "computer_action",
                "requires_confirmation": False,
                "reply": result.message,
                "result": result.to_dict(),
            }
            if action.intent == "index_folder":
                self.latest_chart = build_file_extension_chart(self.knowledge)
                save_latest_chart(self.latest_chart)
                payload["chart"] = self.latest_chart
            self._record_audit(
                "executed",
                action.label,
                action.target,
                "complete" if result.ok else "failed",
            )
            payload["state"] = self.state()
            return payload
        return {
            "type": "chat_or_brain",
            "requires_confirmation": False,
            "reply": self.ask(text),
            "state": self.state(),
        }

    def confirm_action(self, action_payload: dict) -> dict:
        action = self.computer.preview_from_dict(action_payload)
        result = self.computer.execute(action)
        self._record_audit(
            "approved",
            action.label,
            action.target,
            "complete" if result.ok else "failed",
        )
        return {
            "result": result.to_dict(),
            "state": self.state(),
        }

    def seed_demo(self) -> dict:
        result = self.knowledge.seed_demo()
        self._record_audit("knowledge", "Seed demo brain", "demo dataset", "complete")
        return {
            "message": "Seeded demo knowledge.",
            "entities_created": result.entities_created,
            "relationships_created": result.relationships_created,
        }

    def index_folder(self, folder: str, max_files: int = 300) -> dict:
        result = FileIndexer(self.knowledge).index_folder(folder, max_files=max_files)
        self._record_audit("files", "Index folder", result.root, "complete")
        return {
            "message": f"Indexed {result.files_indexed} files from {result.root}.",
            "root": result.root,
            "files_indexed": result.files_indexed,
            "files_skipped": result.files_skipped,
        }

    def clear_index(self) -> dict:
        deleted = self.knowledge.clear_filesystem_index()
        self._record_audit("files", "Clear indexed files", "filesystem index", "complete")
        return {"message": f"Cleared {deleted} indexed filesystem entities.", "deleted": deleted}

    def start_c_drive_index(self, max_files: int = 5000) -> None:
        if self.c_drive_index_status == "running":
            return
        indexed_files = self._file_count()
        if indexed_files >= 1000:
            self.c_drive_index_status = f"ready, {indexed_files} files indexed"
            return
        thread = threading.Thread(
            target=self._index_c_drive_background,
            args=(max_files,),
            name="marlin-c-drive-index",
            daemon=True,
        )
        thread.start()

    def _index_c_drive_background(self, max_files: int) -> None:
        root = Path.home().anchor or "C:\\"
        self.c_drive_index_status = f"running, cap {max_files} files"
        self._record_audit("files", "Auto index Windows C", root, "running")
        try:
            result = FileIndexer(self.knowledge).index_folder(root, max_files=max_files)
            self.latest_chart = build_file_extension_chart(self.knowledge)
            save_latest_chart(self.latest_chart)
            self.c_drive_index_status = (
                f"indexed {result.files_indexed} files, skipped {result.files_skipped}"
            )
            self._record_audit("files", "Auto index Windows C", root, "complete")
        except Exception as exc:
            self.c_drive_index_status = f"failed: {exc}"
            self._record_audit("files", "Auto index Windows C", root, "failed")

    def try_graph_request(self, text: str) -> dict | None:
        command = " ".join(text.lower().split())
        if command not in {
            "graph my files",
            "graph my files by extension",
            "show file graph",
            "show file counts",
            "show file counts by extension",
        }:
            return None

        chart = build_file_extension_chart(self.knowledge)
        self.latest_chart = chart
        save_latest_chart(chart)
        return chart

    def direct_command_reply(self, text: str) -> str | None:
        command = " ".join(text.lower().split())
        try:
            if command in {"list entities", "show entities"}:
                return self.responses.generate(
                    self.dispatcher.dispatch(
                        StructuredIntent(
                            intent="list_entities",
                            language="en",
                            confidence=1.0,
                            parameters={},
                            requires_confirmation=False,
                        )
                    )
                )
            if command in {"list relationships", "show relationships"}:
                return self.responses.generate(
                    self.dispatcher.dispatch(
                        StructuredIntent(
                            intent="list_relationships",
                            language="en",
                            confidence=1.0,
                            parameters={},
                            requires_confirmation=False,
                        )
                    )
                )
            if command in {"list files", "show files"}:
                return self.responses.generate(
                    self.dispatcher.dispatch(
                        StructuredIntent(
                            intent="list_files",
                            language="en",
                            confidence=1.0,
                            parameters={},
                            requires_confirmation=False,
                        )
                    )
                )
            if command in {"important tasks", "reason important-tasks", "show important tasks"}:
                self.prolog_activity = "running important_tasks/0"
                return self.responses.generate(
                    self.dispatcher.dispatch(
                        StructuredIntent(
                            intent="get_important_tasks",
                            language="en",
                            confidence=1.0,
                            parameters={},
                            requires_confirmation=False,
                        )
                    )
                )
            if command in {"high priority tasks", "reason high-priority", "show high priority tasks"}:
                self.prolog_activity = "running high_priority/1"
                return self.responses.generate(
                    self.dispatcher.dispatch(
                        StructuredIntent(
                            intent="get_high_priority_tasks",
                            language="en",
                            confidence=1.0,
                            parameters={},
                            requires_confirmation=False,
                        )
                    )
                )
            for prefix in ("why high priority ", "why high-priority "):
                if command.startswith(prefix):
                    task_id = text[len(prefix) :].strip()
                    self.prolog_activity = f"explaining {task_id}"
                    return self.responses.generate(
                        self.dispatcher.dispatch(
                            StructuredIntent(
                                intent="explain_high_priority",
                                language="en",
                                confidence=1.0,
                                parameters={"task_id": task_id},
                                requires_confirmation=False,
                            )
                        )
                    )
            if command in {"seed demo", "seed-demo"}:
                result = self.knowledge.seed_demo()
                self._record_audit("knowledge", "Seed demo brain", "demo dataset", "complete")
                return (
                    "Seeded demo knowledge: "
                    f"{result.entities_created} entities, {result.relationships_created} relationships."
                )
            if command in {"show brain graph", "brain graph", "refresh brain graph"}:
                return "Brain graph refreshed."
        except PrologUnavailable as exc:
            return f"Prolog unavailable: {exc}"
        return None

    def speak(self, text: str, provider: str | None = None) -> dict:
        selected_provider = (provider or self.settings.tts_provider).lower().strip()
        if selected_provider == "gemini":
            clean_text = " ".join(text.split())[:1800]
            if not clean_text:
                return {"message": "No speech text provided.", "ok": False}
            audio = create_tts_client(self.settings).synthesize(clean_text)
            return {
                "message": "Speaking with Gemini.",
                "ok": True,
                "provider": "gemini",
                "mime_type": audio.mime_type,
                "audio_base64": audio.data_base64,
            }

        if selected_provider != "windows":
            return {"message": f"Unsupported voice provider: {selected_provider}", "ok": False}

        if sys.platform != "win32":
            return {"message": "Server voice is only available on Windows.", "ok": False}
        clean_text = " ".join(text.split())[:1800]
        if not clean_text:
            return {"message": "No speech text provided.", "ok": False}
        ssml = _speech_ssml(clean_text)
        self.stop_voice()
        command = (
            "$ssml = [Console]::In.ReadToEnd(); "
            "Add-Type -AssemblyName System.Speech; "
            "$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$voices = $speaker.GetInstalledVoices() | Where-Object { $_.Enabled }; "
            "$preferred = @('Microsoft Sonia','Microsoft Ryan','Microsoft Libby','Microsoft Aria','Microsoft Jenny','Microsoft Zira','Microsoft Hazel','Microsoft George','Microsoft David'); "
            "foreach ($name in $preferred) { "
            "$match = $voices | Where-Object { $_.VoiceInfo.Name -like \"*$name*\" } | Select-Object -First 1; "
            "if ($match) { $speaker.SelectVoice($match.VoiceInfo.Name); break } "
            "} "
            "$speaker.Rate = -2; "
            "$speaker.Volume = 92; "
            "$speaker.SpeakSsml($ssml)"
        )
        startupinfo = None
        creationflags = 0
        if hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW
        self.voice_process = subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", command],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        if self.voice_process.stdin:
            self.voice_process.stdin.write(ssml)
            self.voice_process.stdin.close()
        return {"message": "Speaking.", "ok": True}

    def stop_voice(self) -> dict:
        if self.voice_process and self.voice_process.poll() is None:
            self.voice_process.terminate()
        self.voice_process = None
        return {"message": "Voice stopped.", "ok": True}

    def _record_audit(self, kind: str, label: str, target: str, status: str) -> None:
        record_audit(kind, label, target, status)


def create_server(settings: Settings, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    runtime = MarlinRuntime(settings)
    runtime.start_c_drive_index()
    threading.Thread(
        target=runtime.warm_opencode_server,
        name="marlin-opencode-warmup",
        daemon=True,
    ).start()

    class Handler(MarlinHandler):
        marlin = runtime

    return ThreadingHTTPServer((host, port), Handler)


def run_server(settings: Settings, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = create_server(settings, host, port)
    print(f"MARLIN is running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


class MarlinHandler(BaseHTTPRequestHandler):
    marlin: MarlinRuntime

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_text(INDEX_HTML, "text/html; charset=utf-8")
            return
        if path == "/api/state":
            self._send_json(self.marlin.state())
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/ask":
                text = str(payload.get("text", "")).strip()
                if not text:
                    raise ValueError("Ask text is required.")
                self._send_json({"reply": self.marlin.ask(text), "state": self.marlin.state()})
                return
            if path == "/api/intent-preview":
                text = str(payload.get("text", "")).strip()
                if not text:
                    raise ValueError("Ask text is required.")
                self._send_json(self.marlin.preview_intent(text))
                return
            if path == "/api/confirm-action":
                action = payload.get("action")
                if not isinstance(action, dict):
                    raise ValueError("Action payload is required.")
                self._send_json(self.marlin.confirm_action(action))
                return
            if path == "/api/seed":
                self._send_json({"result": self.marlin.seed_demo(), "state": self.marlin.state()})
                return
            if path == "/api/index":
                folder = str(payload.get("path", "")).strip()
                max_files = int(payload.get("max_files", 300))
                if not folder:
                    raise ValueError("Folder path is required.")
                self._send_json(
                    {"result": self.marlin.index_folder(folder, max_files), "state": self.marlin.state()}
                )
                return
            if path == "/api/clear-index":
                self._send_json({"result": self.marlin.clear_index(), "state": self.marlin.state()})
                return
            if path == "/api/speak":
                text = str(payload.get("text", "")).strip()
                provider = str(payload.get("provider", "")).strip() or None
                self._send_json({"result": self.marlin.speak(text, provider=provider)})
                return
            if path == "/api/stop-voice":
                self._send_json({"result": self.marlin.stop_voice()})
                return
        except (
            ValueError,
            OSError,
            LLMUnavailable,
            OpenCodeUnavailable,
            PrologUnavailable,
            TTSUnavailable,
        ) as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args) -> None:
        return

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, content_type: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _entity_dict(entity) -> dict:
    return {
        "id": entity.id,
        "type": entity.type,
        "name": entity.name,
        "source": entity.source,
        "metadata": entity.metadata,
    }


def _speech_ssml(text: str) -> str:
    escaped_text = escape(text)
    parts = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|(?<=:)\s+|\n+", escaped_text)
        if part.strip()
    ]
    if not parts:
        parts = [escaped_text]
    spoken_parts = []
    for part in parts:
        spoken_parts.append(f"<s>{part}</s>")
        spoken_parts.append('<break time="180ms"/>')
    body = "".join(spoken_parts)
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xml:lang="en-US">'
        '<prosody rate="-8%" pitch="-2%">'
        f"{body}"
        "</prosody>"
        "</speak>"
    )


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MARLIN BrainOS</title>
  <style>
    :root { color-scheme: dark; font-family: Arial, sans-serif; }
    * { box-sizing: border-box; }
    html, body { width: 100%; height: 100%; }
    body { margin: 0; background: #05080b; color: #edf2f7; overflow: hidden; }
    button, input, select { font: inherit; }
    button { border: 1px solid rgba(97,132,161,.72); background: rgba(22,34,45,.82); color: #edf2f7; padding: 9px 11px; border-radius: 8px; cursor: pointer; }
    button:hover { background: rgba(39,57,72,.92); }
    .moveable { touch-action: none; }
    .moveable:hover { cursor: move; }
    input { width: 100%; min-width: 0; padding: 12px 14px; border: 1px solid rgba(97,132,161,.7); background: rgba(6,10,14,.86); color: #edf2f7; border-radius: 999px; }
    select { background: rgba(6,10,14,.86); color: #edf2f7; border: 1px solid rgba(97,132,161,.7); border-radius: 8px; padding: 8px; }
    h1, h2 { margin: 0; letter-spacing: 0; }
    h2 { font-size: 12px; color: #a9bacb; text-transform: uppercase; }
    .shell { position: relative; width: 100vw; height: 100dvh; overflow: hidden; background: #05080b; }
    aside { display: contents; }
    .main { position: absolute; inset: 0; overflow: hidden; background: #05080b; }
    #graph { width: 100vw; height: 100dvh; display: block; }
    .hud { position: absolute; left: 18px; top: 16px; max-width: min(360px, calc(100% - 36px)); color: #a9bacb; background: rgba(8,12,16,.52); border: 1px solid rgba(47,66,80,.7); border-radius: 8px; padding: 9px 11px; backdrop-filter: blur(8px); }
    .chat { position: absolute; inset: 0; pointer-events: none; }
    .chat-head { pointer-events: auto; position: absolute; left: 50%; bottom: 88px; width: min(360px, calc(100vw - 32px)); transform: translateX(-50%); display: grid; gap: 8px; justify-items: center; }
    .chat-head h1, .chat-head > div:first-of-type, .command-tools, .command-stats { display: none; }
    .voice-bar { display: flex; align-items: center; justify-content: center; gap: 7px; flex-wrap: nowrap; }
    .voice-bar label, .voice-status, .voice-bar select { display: none; }
    .voice-bar button { min-width: 78px; padding: 7px 9px; font-size: 13px; border-radius: 999px; }
    .row.command-row { pointer-events: auto; position: absolute; left: 50%; bottom: 30px; width: min(240px, calc(100vw - 32px)); transform: translateX(-50%); display: flex; gap: 6px; padding: 6px; border: 1px solid rgba(83,116,143,.72); background: rgba(7,11,15,.76); border-radius: 999px; backdrop-filter: blur(12px); }
    .command-row input { min-height: 34px; font-size: 13px; padding: 7px 11px; border-color: rgba(83,168,214,.8); }
    .command-row button { min-width: 48px; border-radius: 999px; font-weight: 700; padding: 6px 8px; font-size: 13px; }
    .messages { pointer-events: auto; position: absolute; left: 50%; bottom: 136px; width: min(420px, calc(100vw - 32px)); max-height: 18dvh; transform: translateX(-50%); overflow: auto; display: flex; flex-direction: column; gap: 8px; }
    .msg { border: 1px solid rgba(48,70,86,.86); border-radius: 8px; padding: 10px 12px; white-space: pre-wrap; line-height: 1.4; background: rgba(8,12,16,.74); backdrop-filter: blur(10px); }
    .user { border-color: rgba(82,146,224,.72); }
    .bot { border-color: rgba(45,198,158,.62); }
    .status-grid { pointer-events: auto; position: absolute; right: 18px; top: 16px; width: 170px; display: grid; gap: 6px; }
    .status-item { border: 1px solid rgba(43,56,69,.78); border-radius: 8px; padding: 7px 8px; background: rgba(8,12,16,.58); backdrop-filter: blur(8px); }
    .status-item strong { display: block; color: #edf2f7; font-size: 11px; }
    .status-item span { color: #a9bacb; font-size: 11px; overflow-wrap: anywhere; }
    #counts { pointer-events: auto; position: absolute; right: 18px; top: 224px; width: 170px; color: #cbd6e1; font-size: 12px; background: rgba(8,12,16,.58); border: 1px solid rgba(43,56,69,.78); border-radius: 8px; padding: 8px; backdrop-filter: blur(8px); }
    #types { pointer-events: auto; position: absolute; right: 18px; top: 286px; width: 170px; display: flex; flex-wrap: wrap; gap: 4px; }
    .pill { display: inline-block; border: 1px solid rgba(64,89,108,.86); padding: 2px 6px; border-radius: 999px; color: #c1cfdd; margin: 0; font-size: 11px; background: rgba(8,12,16,.54); }
    #provider, aside > h1, aside > div:first-of-type, aside > h2, .tabs, aside .stack { display: none; }
    .confirm-card { pointer-events: auto; position: absolute; left: 50%; top: calc(50% - 132px); width: min(560px, calc(100vw - 40px)); transform: translateX(-50%); border: 1px solid rgba(97,132,161,.8); background: rgba(12,19,25,.88); border-radius: 10px; padding: 12px; display: none; gap: 10px; backdrop-filter: blur(12px); }
    .confirm-card.active { display: grid; }
    .confirm-title { font-weight: 700; }
    .confirm-meta { color: #b8c5d1; font-size: 13px; line-height: 1.4; }
    .row { display: flex; gap: 8px; min-width: 0; width: 100%; }
    .row input { flex: 1; }
    .camera-panel { pointer-events: auto; position: absolute; right: 18px; bottom: 18px; width: min(340px, calc(100% - 36px)); background: rgba(8,12,16,.84); border: 1px solid rgba(52,68,84,.86); border-radius: 8px; overflow: hidden; display: none; backdrop-filter: blur(10px); }
    .camera-panel.active { display: block; }
    .camera-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 10px 12px; color: #dce7f2; }
    .camera-head span { color: #9fb3c8; font-size: 12px; }
    #cameraVideo { width: 100%; aspect-ratio: 16 / 9; background: #05070a; display: block; object-fit: cover; }
    .split-panels { display: none; }
    .panel { border: 1px solid rgba(43,56,69,.78); border-radius: 8px; padding: 9px; background: rgba(8,12,16,.58); backdrop-filter: blur(8px); }
    .panel h2 { margin-bottom: 7px; }
    .audit-list, .file-list { max-height: 86px; overflow: auto; display: grid; gap: 6px; }
    .audit-item, .file-item { border-bottom: 1px solid rgba(37,49,60,.9); padding-bottom: 6px; color: #b8c5d1; font-size: 11px; line-height: 1.35; }
    .audit-item strong, .file-item strong { color: #edf2f7; display: block; }
    .viz-panel { display: none; }
    @media (max-width: 760px) {
      .chat-head { bottom: 80px; width: min(320px, calc(100vw - 24px)); }
      .row.command-row { bottom: 22px; width: min(240px, calc(100vw - 24px)); flex-wrap: nowrap; border-radius: 999px; }
      .command-row input { flex-basis: auto; }
      .command-row button { flex: 0 0 auto; }
      .voice-bar button { min-width: 92px; }
      .status-grid, #counts, #types { right: 10px; width: 145px; }
      .split-panels { display: none; }
      .messages { bottom: 128px; width: min(360px, calc(100vw - 24px)); max-height: 18dvh; }
      .hud { left: 10px; top: 10px; font-size: 12px; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside>
      <h1>MARLIN</h1>
      <div>Personal command and reasoning assistant</div>
      <h2>Knowledge</h2>
      <div id="counts" class="moveable" data-move-key="counts">Loading...</div>
      <h2>LLM</h2>
      <div id="provider">Checking provider...</div>
      <h2>Status</h2>
      <div id="statusGrid" class="status-grid moveable" data-move-key="statusGrid"></div>
      <div class="tabs">
        <button onclick="focusPanel('console')">Console</button>
        <button onclick="focusPanel('graph')">Brain Graph</button>
        <button onclick="focusPanel('camera')">Camera</button>
        <button onclick="focusPanel('files')">Files</button>
        <button onclick="focusPanel('actions')">Actions</button>
      </div>
      <div class="stack" style="margin-top:14px">
        <button onclick="seedDemo()">Seed demo brain</button>
        <button onclick="clearIndex()">Clear indexed files</button>
        <button onclick="previewComputerAction('graph my files by extension')">Graph indexed files</button>
        <button onclick="previewComputerAction('index windows c')">Index Windows C</button>
      </div>
      <h2>Index Files</h2>
      <div class="stack">
        <input id="sidebarFolderPath" placeholder="Folder path, e.g. C:\Users\M S I\Documents">
        <button onclick="indexFolder()">Index folder into brain</button>
      </div>
      <h2>Node Types</h2>
      <div id="types" class="moveable" data-move-key="types"></div>
    </aside>
    <section class="main">
      <canvas id="graph"></canvas>
      <div id="hudPanel" class="hud moveable" data-move-key="hudPanel">Links: green contains, gold belongs_to, orange uses, pink depends_on</div>
      <div id="cameraPanel" class="camera-panel moveable" data-move-key="cameraPanel">
        <div class="camera-head">
          <strong>Camera</strong>
          <span id="cameraStatus">Local preview</span>
          <button onclick="closeCamera()">Close</button>
        </div>
        <video id="cameraVideo" autoplay muted playsinline></video>
      </div>
    </section>
    <section class="chat">
      <div id="voicePanel" class="chat-head moveable" data-move-key="voicePanel">
        <h1>MARLIN Command Center</h1>
        <div>Good day. MARLIN is online, local, and awaiting your instruction.</div>
        <div class="voice-bar">
          <label><input id="voiceEnabled" type="checkbox" checked> Voice replies</label>
          <select id="voiceMode" title="Voice mode">
            <option value="fast" selected>Fast voice</option>
            <option value="human">Human voice</option>
          </select>
          <button onclick="stopVoice()">Stop voice</button>
          <button id="listenButton" onclick="toggleListening()">Listen</button>
          <button onclick="openCamera()">Open camera</button>
          <span id="voiceStatus" class="voice-status">Voice ready</span>
        </div>
        <div id="commandStats" class="command-stats">Loading brain status...</div>
        <div class="command-tools">
          <div class="quick-actions">
            <button onclick="seedDemo()">Seed demo brain</button>
            <button onclick="previewComputerAction('graph my files by extension')">Graph files</button>
            <button onclick="previewComputerAction('index windows c')">Index Windows C</button>
            <button onclick="clearIndex()">Clear file index</button>
          </div>
          <div class="row">
            <input id="commandFolderPath" placeholder="Folder path, e.g. C:\Users\M S I\Documents">
            <button onclick="indexFolder()">Index folder</button>
          </div>
        </div>
      </div>
      <div id="confirmCard" class="confirm-card moveable" data-move-key="confirmCard">
        <div class="confirm-title" id="confirmTitle">Confirm action</div>
        <div class="confirm-meta" id="confirmMeta"></div>
        <div class="row">
          <button onclick="approveAction()">Approve</button>
          <button onclick="cancelAction()">Cancel</button>
        </div>
      </div>
      <div id="messages" class="messages moveable" data-move-key="messages"></div>

      <div id="vizPanel" class="panel viz-panel">
        <h2 id="vizTitle">Visualizations</h2>
        <canvas id="vizChart"></canvas>
      </div>
      <div id="commandBar" class="row command-row moveable" data-move-key="commandBar">
        <input id="askText" placeholder="Ask MARLIN" onkeydown="if(event.key==='Enter') askMarlin()" autofocus>
        <button onclick="askMarlin()">Run</button>
      </div>
    </section>
  </div>
  <script>
    let state = { entities: [], relationships: [] };
    let nodes = [];
    let dragging = null;
    let panning = null;
    let scale = 0.68;
    let panX = 0;
    let panY = 0;
    let graphWorldWidth = 1600;
    let graphWorldHeight = 1000;
    let animationStarted = false;
    let lastFrame = performance.now();
    let voiceAudio = null;
    let cameraStream = null;
    let pendingAction = null;
    let voiceModeInitialized = false;
    let speechRunId = 0;
    let recognition = null;
    let listening = false;
    let conversationInFlight = false;
    const MAX_GRAPH_NODES = 900;
    const canvas = document.getElementById('graph');
    const ctx = canvas.getContext('2d');
    const vizCanvas = document.getElementById('vizChart');
    const vizCtx = vizCanvas.getContext('2d');

    function initMoveablePanels() {
      document.querySelectorAll('.moveable').forEach(panel => {
        const key = panel.dataset.moveKey || panel.id;
        restorePanelPosition(panel, key);
        panel.addEventListener('pointerdown', event => startPanelDrag(event, panel, key));
      });
    }

    function restorePanelPosition(panel, key) {
      try {
        const saved = JSON.parse(localStorage.getItem(`marlin-panel-${key}`) || 'null');
        if (!saved || typeof saved.x !== 'number' || typeof saved.y !== 'number') return;
        panel.style.left = `${Math.max(0, Math.min(window.innerWidth - 48, saved.x))}px`;
        panel.style.top = `${Math.max(0, Math.min(window.innerHeight - 32, saved.y))}px`;
        panel.style.right = 'auto';
        panel.style.bottom = 'auto';
        panel.style.transform = 'none';
      } catch (err) {}
    }

    function startPanelDrag(event, panel, key) {
      if (event.button !== 0) return;
      if (event.target.closest('button, input, select, textarea, video')) return;
      event.preventDefault();
      event.stopPropagation();
      const rect = panel.getBoundingClientRect();
      const offsetX = event.clientX - rect.left;
      const offsetY = event.clientY - rect.top;
      panel.setPointerCapture(event.pointerId);
      panel.style.right = 'auto';
      panel.style.bottom = 'auto';
      panel.style.transform = 'none';
      const move = moveEvent => {
        const maxX = Math.max(0, window.innerWidth - rect.width);
        const maxY = Math.max(0, window.innerHeight - rect.height);
        const x = Math.max(0, Math.min(maxX, moveEvent.clientX - offsetX));
        const y = Math.max(0, Math.min(maxY, moveEvent.clientY - offsetY));
        panel.style.left = `${x}px`;
        panel.style.top = `${y}px`;
      };
      const stop = () => {
        panel.removeEventListener('pointermove', move);
        panel.removeEventListener('pointerup', stop);
        panel.removeEventListener('pointercancel', stop);
        const finalRect = panel.getBoundingClientRect();
        localStorage.setItem(`marlin-panel-${key}`, JSON.stringify({ x: finalRect.left, y: finalRect.top }));
      };
      panel.addEventListener('pointermove', move);
      panel.addEventListener('pointerup', stop);
      panel.addEventListener('pointercancel', stop);
    }
    function resize() {
      canvas.width = canvas.clientWidth;
      canvas.height = canvas.clientHeight;
      graphWorldWidth = Math.max(canvas.width * 2.2, 1600);
      graphWorldHeight = Math.max(canvas.height * 1.9, 1000);
      vizCanvas.width = vizCanvas.clientWidth;
      vizCanvas.height = vizCanvas.clientHeight;
      draw();
      renderChart(state.latest_chart);
    }
    window.addEventListener('resize', resize);

    async function api(path, body) {
      const opts = body ? { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) } : {};
      const res = await fetch(path, opts);
      const json = await res.json();
      if (!res.ok) throw new Error(json.error || 'Request failed');
      return json;
    }

    async function refresh() {
      state = await api('/api/state');
      layout();
      renderSide();
      draw();
    }

    async function refreshQuietly() {
      if (conversationInFlight) return;
      try {
        state = await api('/api/state');
        layout();
        renderSide();
        draw();
      } catch (err) {
        setVoiceStatus('Backend reconnecting');
      }
    }

    function layout() {
      const byId = new Map(nodes.map(n => [n.id, n]));
      const graphEntities = selectGraphEntities();
      const worldCx = graphWorldWidth / 2;
      const worldCy = graphWorldHeight / 2;
      nodes = graphEntities.map((e, i) => {
        const old = byId.get(e.id);
        const count = Math.max(1, graphEntities.length);
        const angle = i * 2.399963 + Math.sin(i) * 0.18;
        const radius = Math.sqrt(i / count) * Math.min(graphWorldWidth, graphWorldHeight) * 0.58;
        const sideBias = e.type === 'folder' ? -0.18 : e.type === 'file' ? 0.12 : 0;
        return old || {
          ...e,
          x: worldCx + graphWorldWidth * sideBias + Math.cos(angle)*radius,
          y: worldCy + Math.sin(angle)*radius*0.82,
          vx: 0,
          vy: 0,
          pulse: Math.random() * Math.PI * 2
        };
      });
      if (!animationStarted) {
        animationStarted = true;
        requestAnimationFrame(tick);
      }
    }

    function selectGraphEntities() {
      const all = state.entities || [];
      if (all.length <= MAX_GRAPH_NODES) return all;
      const relationships = state.relationships || [];
      const degree = {};
      relationships.forEach(r => {
        degree[r.source_id] = (degree[r.source_id] || 0) + 1;
        degree[r.target_id] = (degree[r.target_id] || 0) + 1;
      });
      return all.slice().sort((a, b) => {
        const typeScore = typeWeight(b.type) - typeWeight(a.type);
        if (typeScore !== 0) return typeScore;
        return (degree[b.id] || 0) - (degree[a.id] || 0);
      }).slice(0, MAX_GRAPH_NODES);
    }

    function renderSide() {
      document.getElementById('counts').innerHTML = `${state.total_entities || state.entities.length} entities<br>${state.total_relationships || state.relationships.length} relationships`;
      document.getElementById('provider').innerHTML = `conversation: ${state.conversation_provider || state.llm_provider || 'unknown'}<br>${state.llm_model || ''}<br>voice: ${state.tts_provider || 'windows'} ${state.tts_voice || ''}`;
      if (!voiceModeInitialized) {
        const voiceMode = document.getElementById('voiceMode');
        if (voiceMode) voiceMode.value = state.voice_mode || 'human';
        voiceModeInitialized = true;
      }
      const counts = {};
      state.entities.forEach(e => counts[e.type] = (counts[e.type] || 0) + 1);
      document.getElementById('types').innerHTML = Object.entries(counts).map(([k,v]) => `<span class="pill">${k}: ${v}</span>`).join('');
      const commandStats = document.getElementById('commandStats');
      if (commandStats) {
        commandStats.innerHTML = [
          `${state.total_entities || state.entities.length} entities`,
          `${state.total_relationships || state.relationships.length} relationships`,
          `conversation: ${state.conversation_provider || state.llm_provider || 'unknown'}`,
          `files: ${state.total_files || counts.file || 0}`
        ].map(item => `<span class="pill">${escapeHtml(item)}</span>`).join('');
      }
      renderStatus();
      renderAudit();
      renderFiles();
      renderChart(state.latest_chart);
    }

    function renderStatus() {
      const status = state.status || {};
      document.getElementById('statusGrid').innerHTML = Object.entries(status).map(([key, value]) => (
        `<div class="status-item"><strong>${escapeHtml(key.replace('_', ' '))}</strong><span>${escapeHtml(String(value))}</span></div>`
      )).join('');
    }

    function renderAudit() {
      const items = state.audit_log || [];
      const box = document.getElementById('auditLog');
      if (!box) return;
      if (!items.length) {
        box.textContent = 'No actions yet.';
        return;
      }
      box.innerHTML = items.slice().reverse().map(item => (
        `<div class="audit-item"><strong>${escapeHtml(item.label || item.kind)}</strong>${escapeHtml(item.status || '')}<br>${escapeHtml(item.target || '')}</div>`
      )).join('');
    }

    function renderFiles(entities = null) {
      const files = entities || (state.entities || []).filter(e => e.type === 'file').slice(0, 20);
      const box = document.getElementById('fileResults');
      if (!box) return;
      if (!files.length) {
        box.textContent = 'No indexed files yet.';
        return;
      }
      box.innerHTML = files.map(item => {
        const path = item.path || (item.metadata && item.metadata.path) || '';
        return `<div class="file-item"><strong>${escapeHtml(item.name || item.id)}</strong>${escapeHtml(path)}</div>`;
      }).join('');
    }

    function renderChart(chart) {
      if (!vizCtx) return;
      vizCtx.clearRect(0, 0, vizCanvas.width, vizCanvas.height);
      const title = document.getElementById('vizTitle');
      if (!chart || !chart.labels || !chart.labels.length) {
        title.textContent = 'Visualizations';
        vizCtx.fillStyle = '#718397';
        vizCtx.font = '13px Arial';
        vizCtx.fillText('Ask: graph my files by extension', 12, 28);
        return;
      }
      title.textContent = chart.title || 'Visualization';
      const max = Math.max(...chart.values, 1);
      const width = Math.max(1, (vizCanvas.width - 28) / chart.labels.length);
      chart.labels.forEach((label, index) => {
        const value = chart.values[index] || 0;
        const barHeight = (vizCanvas.height - 42) * (value / max);
        const x = 14 + index * width;
        const y = vizCanvas.height - 24 - barHeight;
        vizCtx.fillStyle = '#7bd88f';
        vizCtx.fillRect(x, y, Math.max(8, width - 8), barHeight);
        vizCtx.fillStyle = '#dce7f2';
        vizCtx.font = '11px Arial';
        vizCtx.fillText(String(value), x, Math.max(12, y - 5));
        vizCtx.fillStyle = '#9fb3c8';
        vizCtx.fillText(String(label).slice(0, 7), x, vizCanvas.height - 8);
      });
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }

    function focusPanel(panel) {
      if (panel === 'console') document.getElementById('askText').focus();
      if (panel === 'graph') canvas.scrollIntoView({ behavior: 'smooth', block: 'center' });
      if (panel === 'camera') openCamera();
      if (panel === 'files') addMsg('File results appear in MARLIN replies now.', 'bot');
      if (panel === 'actions') addMsg('Action history is kept in the backend audit log.', 'bot');
    }

    function draw() {
      if (!ctx) return;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const glow = ctx.createRadialGradient(canvas.width*0.5, canvas.height*0.52, 30, canvas.width*0.5, canvas.height*0.52, Math.max(canvas.width, canvas.height) * 0.72);
      glow.addColorStop(0, 'rgba(45,191,162,0.12)');
      glow.addColorStop(0.45, 'rgba(56,99,146,0.07)');
      glow.addColorStop(1, 'rgba(8,12,16,0)');
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.save();
      ctx.translate(canvas.width/2, canvas.height/2);
      ctx.scale(scale, scale);
      ctx.translate(-graphWorldWidth/2 + panX, -graphWorldHeight/2 + panY);
      const byId = new Map(nodes.map(n => [n.id, n]));
      const degree = {};
      state.relationships.forEach(r => {
        degree[r.source_id] = (degree[r.source_id] || 0) + 1;
        degree[r.target_id] = (degree[r.target_id] || 0) + 1;
      });
      const visibleRelationships = state.relationships.slice(0, 4200);
      visibleRelationships.forEach(r => drawRelationship(r, byId, false));
      visibleRelationships.slice(0, 900).forEach(r => drawRelationship(r, byId, true));
      nodes.forEach(n => {
        const size = nodeSize(n, degree[n.id] || 0);
        const pulse = 1.6 + Math.sin(performance.now() / 800 + n.pulse) * 1.2;
        ctx.beginPath();
        ctx.fillStyle = colorFor(n.type, 0.13);
        ctx.arc(n.x, n.y, size + 9 + pulse, 0, Math.PI*2);
        ctx.fill();
        ctx.beginPath();
        ctx.fillStyle = colorFor(n.type, 1);
        ctx.arc(n.x, n.y, size + pulse * 0.18, 0, Math.PI*2);
        ctx.fill();
        if (n.type !== 'file' || (degree[n.id] || 0) > 1 || size > 10) {
          ctx.fillStyle = 'rgba(237,242,247,.86)';
          ctx.font = `${n.type === 'file' ? 10 : 12}px Arial`;
          ctx.fillText(n.name.slice(0, 32), n.x + size + 8, n.y + 4);
        }
      });
      ctx.restore();
    }

    function colorFor(type, alpha = 1) {
      const colors = { project:'231,176,34', task:'225,86,142', file:'45,198,158', folder:'82,146,224', technology:'236,127,48' };
      return `rgba(${colors[type] || '196,208,221'}, ${alpha})`;
    }
    function drawRelationship(r, byId, foreground) {
      const a = byId.get(r.source_id), b = byId.get(r.target_id);
      if (!a || !b) return;
      const style = relationshipStyle(r.type, foreground);
      const curve = relationshipCurve(a, b);
      ctx.lineWidth = style.width;
      ctx.strokeStyle = style.color;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.quadraticCurveTo(curve.x, curve.y, b.x, b.y);
      ctx.stroke();
      if (foreground && shouldLabelRelationship(r, a, b)) {
        ctx.fillStyle = 'rgba(205,220,235,.78)';
        ctx.font = '10px Arial';
        ctx.fillText(r.type, curve.x + 5, curve.y - 4);
      }
    }

    function relationshipCurve(a, b) {
      return {
        x: (a.x + b.x) / 2 + Math.sin((a.x + b.y) * 0.01) * 22,
        y: (a.y + b.y) / 2 + Math.cos((a.y + b.x) * 0.01) * 22
      };
    }

    function relationshipStyle(type, foreground) {
      const alpha = foreground ? 0.62 : 0.24;
      const width = foreground ? 1.45 : 0.75;
      const colors = {
        contains: `rgba(45,198,158,${alpha})`,
        belongs_to: `rgba(231,176,34,${alpha})`,
        uses: `rgba(236,127,48,${alpha})`,
        depends_on: `rgba(225,86,142,${alpha})`
      };
      return { color: colors[type] || `rgba(142,166,190,${alpha})`, width };
    }

    function shouldLabelRelationship(r, a, b) {
      if (r.type === 'contains') return a.type !== 'folder' || b.type !== 'file';
      return a.type !== 'file' && b.type !== 'file';
    }

    function nodeSize(node, degree) {
      const base = { project: 16, task: 11, folder: 10, technology: 10, file: 5 }[node.type] || 6;
      return Math.min(28, base + Math.sqrt(degree) * 1.8);
    }

    function typeWeight(type) {
      return { project: 5, folder: 4, task: 3, technology: 2, file: 1 }[type] || 0;
    }

    function tick(now) {
      const dt = Math.min(0.032, (now - lastFrame) / 1000);
      lastFrame = now;
      simulate(dt);
      draw();
      requestAnimationFrame(tick);
    }

    function simulate(dt) {
      if (!nodes.length) return;
      const byId = new Map(nodes.map(n => [n.id, n]));
      const cx = canvas.width / 2;
      const cy = canvas.height / 2;
      const worldCx = graphWorldWidth / 2;
      const worldCy = graphWorldHeight / 2;
      nodes.forEach(n => {
        if (n === dragging) return;
        const typeOffset = n.type === 'folder' ? -graphWorldWidth * 0.09 : n.type === 'file' ? graphWorldWidth * 0.06 : 0;
        n.vx += (worldCx + typeOffset - n.x) * 0.007 * dt;
        n.vy += (worldCy - n.y) * 0.007 * dt;
      });
      state.relationships.forEach(r => {
        const a = byId.get(r.source_id), b = byId.get(r.target_id);
        if (!a || !b) return;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const distance = Math.max(1, Math.hypot(dx, dy));
        const desired = r.type === 'contains' ? 104 : 170;
        const force = (distance - desired) * 0.018 * dt;
        const fx = (dx / distance) * force;
        const fy = (dy / distance) * force;
        if (a !== dragging) { a.vx += fx; a.vy += fy; }
        if (b !== dragging) { b.vx -= fx; b.vy -= fy; }
      });
      const repelLimit = Math.min(nodes.length, 360);
      for (let i = 0; i < repelLimit; i++) {
        for (let j = i + 1; j < repelLimit; j++) {
          const a = nodes[i], b = nodes[j];
          const dx = b.x - a.x;
          const dy = b.y - a.y;
          const distance = Math.max(1, Math.hypot(dx, dy));
          if (distance > 150) continue;
          const force = (150 - distance) * 0.045 * dt;
          const fx = (dx / distance) * force;
          const fy = (dy / distance) * force;
          if (a !== dragging) { a.vx -= fx; a.vy -= fy; }
          if (b !== dragging) { b.vx += fx; b.vy += fy; }
        }
      }
      nodes.forEach(n => {
        if (n === dragging) return;
        n.vx *= 0.94;
        n.vy *= 0.94;
        n.x += n.vx * 60;
        n.y += n.vy * 60;
        n.x = Math.max(-graphWorldWidth * 0.22, Math.min(graphWorldWidth * 1.22, n.x));
        n.y = Math.max(-graphWorldHeight * 0.22, Math.min(graphWorldHeight * 1.22, n.y));
      });
    }

    canvas.addEventListener('mousedown', e => {
      const p = point(e);
      dragging = nodes.find(n => Math.hypot(n.x-p.x, n.y-p.y) < 16) || null;
      if (!dragging) panning = { x: e.clientX, y: e.clientY, panX, panY };
    });
    canvas.addEventListener('mousemove', e => {
      if (panning) {
        panX = panning.panX + (e.clientX - panning.x) / scale;
        panY = panning.panY + (e.clientY - panning.y) / scale;
        draw();
        return;
      }
      if (!dragging) return;
      const p = point(e);
      dragging.x = p.x; dragging.y = p.y; draw();
    });
    canvas.addEventListener('mouseup', () => { dragging = null; panning = null; });
    canvas.addEventListener('mouseleave', () => { dragging = null; panning = null; });
    canvas.addEventListener('wheel', e => {
      e.preventDefault();
      scale = Math.max(0.32, Math.min(1.8, scale * (e.deltaY > 0 ? 0.92 : 1.08)));
      draw();
    });
    function point(e) {
      const rect = canvas.getBoundingClientRect();
      return {
        x: (e.clientX - rect.left - canvas.width/2)/scale + graphWorldWidth/2 - panX,
        y: (e.clientY - rect.top - canvas.height/2)/scale + graphWorldHeight/2 - panY
      };
    }

    function addMsg(text, who) {
      const div = document.createElement('div');
      div.className = `msg ${who}`;
      div.textContent = text;
      document.getElementById('messages').appendChild(div);
      div.scrollIntoView();
      return div;
    }

    function browserCanSpeak() {
      return 'speechSynthesis' in window && 'SpeechSynthesisUtterance' in window;
    }

    function browserCanListen() {
      return 'SpeechRecognition' in window || 'webkitSpeechRecognition' in window;
    }

    function detectSpeechLanguage(text) {
      return /[\u1000-\u109f]/.test(text) ? 'my-MM' : 'en-GB';
    }

    function setVoiceStatus(text) {
      const status = document.getElementById('voiceStatus');
      if (status) status.textContent = text;
    }

    function cleanSpeechText(text) {
      return text
        .replace(/[`*_#>-]/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .slice(0, 1800);
    }

    function speakReply(text) {
      const enabled = document.getElementById('voiceEnabled');
      if (!enabled || !enabled.checked) return;
      const speechText = cleanSpeechText(text);
      if (!speechText) return;
      const runId = ++speechRunId;
      stopVoice(false);
      const mode = document.getElementById('voiceMode')?.value || 'fast';
      if (mode === 'human' && (state.tts_provider || '').toLowerCase() === 'gemini') {
        speakOnServer(speechText, 'gemini', runId);
        return;
      }
      if (!browserCanSpeak()) {
        speakOnServer(speechText, 'windows', runId);
        return;
      }
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(speechText);
      utterance.lang = detectSpeechLanguage(speechText);
      utterance.rate = 0.88;
      utterance.pitch = 0.96;
      utterance.volume = 0.92;
      const voices = window.speechSynthesis.getVoices();
      const preferred = voices.find(v => /natural|neural|premium|online/i.test(v.name) && v.lang.startsWith('en'))
        || voices.find(v => /samantha|daniel|zira|aria|jenny|libby|sonia|ryan/i.test(v.name) && v.lang.startsWith('en'))
        || voices.find(v => v.lang === utterance.lang)
        || voices.find(v => v.lang.startsWith(utterance.lang.split('-')[0]))
        || voices.find(v => v.lang.startsWith('en-GB'))
        || voices.find(v => v.lang.startsWith('en'));
      if (preferred) utterance.voice = preferred;
      utterance.onstart = () => setVoiceStatus('Speaking');
      utterance.onend = () => { if (runId === speechRunId) setVoiceStatus('Voice ready'); };
      utterance.onerror = () => { if (runId === speechRunId) setVoiceStatus('Voice stopped'); };
      if (runId === speechRunId) window.speechSynthesis.speak(utterance);
    }

    function stopVoice(invalidate = true) {
      if (invalidate) speechRunId++;
      if (browserCanSpeak()) window.speechSynthesis.cancel();
      if (voiceAudio) {
        voiceAudio.onended = null;
        voiceAudio.onerror = null;
        voiceAudio.pause();
        voiceAudio.src = '';
        voiceAudio.load();
        voiceAudio.currentTime = 0;
        voiceAudio = null;
      }
      api('/api/stop-voice', {}).catch(() => {});
      setVoiceStatus('Voice stopped');
    }

    async function speakOnServer(text, provider = null, runId = speechRunId) {
      try {
        setVoiceStatus('Speaking');
        const payload = provider ? { text, provider } : { text };
        const json = await api('/api/speak', payload);
        if (runId !== speechRunId) return;
        if (json.result && json.result.audio_base64) {
          if (voiceAudio) {
            voiceAudio.pause();
            voiceAudio.src = '';
          }
          voiceAudio = new Audio(`data:${json.result.mime_type || 'audio/wav'};base64,${json.result.audio_base64}`);
          voiceAudio.onended = () => { if (runId === speechRunId) setVoiceStatus('Voice ready'); };
          voiceAudio.onerror = () => { if (runId === speechRunId) setVoiceStatus('Voice unavailable'); };
          await voiceAudio.play();
          return;
        }
        if (runId === speechRunId) setVoiceStatus(json.result && json.result.ok ? 'Speaking' : 'Voice unavailable');
      } catch (err) {
        if (runId === speechRunId) setVoiceStatus('Voice unavailable');
      }
    }

    function ensureRecognition() {
      if (recognition) return recognition;
      if (!browserCanListen()) return null;
      const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      recognition = new Recognition();
      recognition.continuous = false;
      recognition.interimResults = true;
      recognition.lang = 'en-US';
      recognition.onstart = () => {
        listening = true;
        const button = document.getElementById('listenButton');
        if (button) {
          button.textContent = 'Listening';
          button.classList.add('listening');
        }
        setVoiceStatus('Listening');
        stopVoice();
      };
      recognition.onresult = event => {
        let finalText = '';
        let interimText = '';
        for (let i = event.resultIndex; i < event.results.length; i++) {
          const transcript = event.results[i][0].transcript.trim();
          if (event.results[i].isFinal) finalText += transcript;
          else interimText += transcript;
        }
        const input = document.getElementById('askText');
        if (input) input.value = finalText || interimText;
        if (finalText) submitVoiceCommand(finalText);
      };
      recognition.onerror = event => {
        setVoiceStatus(event.error === 'not-allowed' ? 'Microphone permission needed' : 'Voice input unavailable');
      };
      recognition.onend = () => {
        listening = false;
        const button = document.getElementById('listenButton');
        if (button) {
          button.textContent = 'Listen';
          button.classList.remove('listening');
        }
        if (document.getElementById('voiceStatus')?.textContent === 'Listening') setVoiceStatus('Voice ready');
      };
      return recognition;
    }

    function toggleListening() {
      const rec = ensureRecognition();
      if (!rec) {
        setVoiceStatus('Voice input not supported in this browser');
        addMsg('Voice input is not supported in this browser. Try Chrome or Edge for microphone commands.', 'bot');
        return;
      }
      if (listening) {
        rec.stop();
        return;
      }
      try {
        rec.start();
      } catch (err) {
        setVoiceStatus('Voice input already active');
      }
    }

    async function submitVoiceCommand(text) {
      const command = text.trim();
      if (!command) return;
      const input = document.getElementById('askText');
      if (input) input.value = command;
      await askMarlin();
    }

    function normalizeCommand(text) {
      return text.toLowerCase().replace(/[^\w\s]/g, ' ').replace(/\s+/g, ' ').trim();
    }

    async function handleLocalCommand(text) {
      const command = normalizeCommand(text);
      if (command === 'fast voice') {
        document.getElementById('voiceMode').value = 'fast';
        addMsg('Fast local voice selected.', 'bot');
        return true;
      }
      if (command === 'human voice') {
        document.getElementById('voiceMode').value = 'human';
        addMsg('Human Gemini voice selected.', 'bot');
        return true;
      }
      if (command === 'stop voice' || command === 'stop speaking' || command === 'be quiet') {
        stopVoice();
        return true;
      }
      return false;
    }

    async function previewComputerAction(text) {
      try {
        const json = await api('/api/intent-preview', { text });
        if (json.requires_confirmation && json.action) {
          showActionPreview(json.action);
          addMsg(`${json.action.label}\nTarget: ${json.action.target || 'local browser'}\nRisk: ${json.action.risk || 'low'}`, 'bot');
          if (json.state) {
            state = json.state;
            layout();
            renderSide();
            draw();
          }
        } else if (json.reply) {
          addMsg(json.reply, 'bot');
          if (json.chart) renderChart(json.chart);
          speakReply(json.reply);
        }
      } catch (err) {
        addMsg(String(err.message || err), 'bot');
      }
    }

    function showActionPreview(action) {
      pendingAction = action;
      document.getElementById('confirmTitle').textContent = action.label || 'Confirm action';
      document.getElementById('confirmMeta').textContent = `Target: ${action.target || 'local browser'} | Risk: ${action.risk || 'low'}`;
      document.getElementById('confirmCard').classList.add('active');
    }

    function hideActionPreview() {
      pendingAction = null;
      document.getElementById('confirmCard').classList.remove('active');
    }

    function cancelAction() {
      hideActionPreview();
      addMsg('Action cancelled.', 'bot');
    }

    async function approveAction() {
      if (!pendingAction) return;
      const action = pendingAction;
      hideActionPreview();
      try {
        const json = await api('/api/confirm-action', { action });
        const result = json.result || {};
        if (result.client_action === 'open_camera') await openCamera();
        if (result.client_action === 'close_camera') closeCamera();
        addMsg(result.message || 'Action complete.', 'bot');
        if (json.state) {
          state = json.state;
          layout();
          renderSide();
          draw();
        }
        if (result.data && result.data.entities) {
          renderFiles(result.data.entities);
          result.data.entities.forEach(item => addMsg(`${item.name}\n${item.path || ''}`.trim(), 'bot'));
        }
        speakReply(result.message || 'Action complete.');
      } catch (err) {
        addMsg(String(err.message || err), 'bot');
      }
    }

    async function openCamera() {
      const panel = document.getElementById('cameraPanel');
      const video = document.getElementById('cameraVideo');
      const status = document.getElementById('cameraStatus');
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        if (status) status.textContent = 'Camera unavailable';
        addMsg('Camera is not available in this browser.', 'bot');
        return;
      }
      try {
        closeCamera(false);
        cameraStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
        video.srcObject = cameraStream;
        panel.classList.add('active');
        if (status) status.textContent = 'Camera active';
      } catch (err) {
        panel.classList.add('active');
        if (status) status.textContent = 'Permission needed';
        addMsg('Camera could not be opened. Please allow camera permission in the browser if prompted.', 'bot');
      }
    }

    function closeCamera(updateStatus = true) {
      if (cameraStream) {
        cameraStream.getTracks().forEach(track => track.stop());
        cameraStream = null;
      }
      const video = document.getElementById('cameraVideo');
      const panel = document.getElementById('cameraPanel');
      const status = document.getElementById('cameraStatus');
      if (video) video.srcObject = null;
      if (panel) panel.classList.remove('active');
      if (updateStatus && status) status.textContent = 'Camera off';
    }

    async function askMarlin() {
      const input = document.getElementById('askText');
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      addMsg(text, 'user');
      if (await handleLocalCommand(text)) return;
      const thinking = addMsg('MARLIN is thinking...', 'bot');
      conversationInFlight = true;
      try {
        const json = await api('/api/intent-preview', { text });
        if (json.requires_confirmation && json.action) {
          showActionPreview(json.action);
          thinking.textContent = `${json.action.label}\nTarget: ${json.action.target || 'local browser'}\nRisk: ${json.action.risk || 'low'}`;
          if (json.state) {
            state = json.state;
            layout();
            renderSide();
            draw();
          }
          return;
        }
        const reply = json.reply || 'Done.';
        thinking.textContent = reply;
        speakReply(reply);
        if (json.chart) renderChart(json.chart);
        state = json.state; layout(); renderSide(); draw();
      } catch (err) {
        thinking.textContent = String(err.message || err);
      } finally {
        conversationInFlight = false;
      }
    }

    async function seedDemo() {
      try { const json = await api('/api/seed', {}); addMsg(json.result.message, 'bot'); state = json.state; layout(); renderSide(); draw(); }
      catch (err) { addMsg(String(err.message || err), 'bot'); }
    }

    async function indexFolder() {
      const commandInput = document.getElementById('commandFolderPath');
      const sidebarInput = document.getElementById('sidebarFolderPath');
      const path = (commandInput && commandInput.value.trim()) || (sidebarInput && sidebarInput.value.trim()) || '';
      if (!path) return;
      await previewComputerAction(`index this folder: ${path}`);
    }

    async function clearIndex() {
      try { const json = await api('/api/clear-index', {}); addMsg(json.result.message, 'bot'); state = json.state; layout(); renderSide(); draw(); }
      catch (err) { addMsg(String(err.message || err), 'bot'); }
    }

    initMoveablePanels();
    resize();
    refresh();
    setInterval(refreshQuietly, 12000);
  </script>
</body>
</html>
"""
















