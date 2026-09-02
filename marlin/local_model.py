"""Ollama-only local language model client for MARLIN V2."""

from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from marlin.config import MarlinSettings


class LocalModelUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class ModelToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ModelTurn:
    content: str
    tool_calls: list[ModelToolCall] = field(default_factory=list)
    raw_message: dict[str, Any] = field(default_factory=dict)


class OllamaLocalModel:
    def __init__(self, settings: MarlinSettings):
        self.settings = settings
        parsed = urlparse(settings.ollama_url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("MARLIN V2 only permits a localhost Ollama endpoint.")
        self.last_metrics: dict[str, Any] = {"tokens": 0, "total_ms": 0}

    def preload_async(self) -> None:
        threading.Thread(target=self._preload, name="marlin-ollama-loader", daemon=True).start()

    def _preload(self) -> None:
        payload = {
            "model": self.model,
            "prompt": "",
            "stream": False,
            "keep_alive": self.settings.ollama_keep_alive,
        }
        request = Request(
            f"{self.settings.ollama_url.rstrip('/')}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.settings.ollama_timeout_seconds) as response:
                response.read()
        except (URLError, HTTPError, TimeoutError, socket.timeout, OSError):
            return

    @property
    def model(self) -> str:
        return self.settings.ollama_model

    def health(self) -> dict[str, Any]:
        try:
            payload = self._json_request("GET", "/api/tags", timeout=2.0)
        except LocalModelUnavailable as exc:
            return {"available": False, "model": self.model, "loaded": False, "error": str(exc)}
        names = {
            str(item.get("name", "")).split(":latest", 1)[0]
            for item in payload.get("models", [])
            if isinstance(item, dict)
        }
        requested = self.model.split(":latest", 1)[0]
        return {"available": True, "model": self.model, "loaded": requested in names, **self.last_metrics}

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> ModelTurn:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "keep_alive": self.settings.ollama_keep_alive,
            "options": {
                "temperature": 0.2,
                "num_ctx": self.settings.ollama_context,
                "num_predict": self.settings.ollama_max_tokens,
            },
        }
        if tools:
            payload["tools"] = tools

        request = Request(
            f"{self.settings.ollama_url.rstrip('/')}/api/chat",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "MARLIN-V2/2.0"},
            method="POST",
        )
        content: list[str] = []
        tool_calls: list[ModelToolCall] = []
        final_message: dict[str, Any] = {}
        try:
            with urlopen(request, timeout=self.settings.ollama_timeout_seconds) as response:
                for raw_line in response:
                    if not raw_line.strip():
                        continue
                    packet = json.loads(raw_line.decode("utf-8"))
                    if packet.get("error"):
                        raise LocalModelUnavailable(str(packet["error"]))
                    message = packet.get("message")
                    if packet.get("done"):
                        self.last_metrics = {
                            "tokens": int(packet.get("eval_count") or 0),
                            "prompt_tokens": int(packet.get("prompt_eval_count") or 0),
                            "total_ms": round(float(packet.get("total_duration") or 0) / 1_000_000),
                        }
                    if not isinstance(message, dict):
                        continue
                    final_message = message
                    token = message.get("content")
                    if isinstance(token, str) and token:
                        content.append(token)
                        if on_token:
                            on_token(token)
                    for call in message.get("tool_calls") or []:
                        function = call.get("function") if isinstance(call, dict) else None
                        if not isinstance(function, dict):
                            continue
                        arguments = function.get("arguments")
                        if isinstance(arguments, str):
                            try:
                                arguments = json.loads(arguments)
                            except json.JSONDecodeError:
                                arguments = {}
                        tool_calls.append(
                            ModelToolCall(
                                name=str(function.get("name", "")),
                                arguments=arguments if isinstance(arguments, dict) else {},
                            )
                        )
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:300]
            raise LocalModelUnavailable(f"Ollama returned HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, socket.timeout, OSError, json.JSONDecodeError) as exc:
            raise LocalModelUnavailable(
                f"Local model is unavailable at {self.settings.ollama_url}. Run `py main.py setup`."
            ) from exc

        text = "".join(content).strip()
        raw = {"role": "assistant", "content": text}
        if tool_calls:
            raw["tool_calls"] = [
                {"type": "function", "function": {"name": call.name, "arguments": call.arguments}}
                for call in tool_calls
            ]
        return ModelTurn(text, tool_calls, raw or final_message)

    def _json_request(self, method: str, path: str, *, timeout: float) -> dict[str, Any]:
        request = Request(f"{self.settings.ollama_url.rstrip('/')}{path}", method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (URLError, HTTPError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise LocalModelUnavailable(f"Could not reach local Ollama at {self.settings.ollama_url}.") from exc
        return value if isinstance(value, dict) else {}
