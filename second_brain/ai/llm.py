"""LLM provider interface and implementations."""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import certifi


class LLMUnavailable(RuntimeError):
    """Raised when the configured LLM provider cannot be reached."""


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ToolCall:
    """A single tool the model asked MARLIN to run."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AssistantTurn:
    """One assistant response, which may request tools instead of replying."""

    content: str
    tool_calls: tuple[ToolCall, ...]
    raw_message: dict[str, Any]

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMClient(Protocol):
    """Provider interface for structured local model calls."""

    def structured_chat(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        temperature: float = 0.0,
    ) -> str:
        """Return the model response content as a string."""

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.4,
    ) -> str:
        """Return a conversational model response as a string."""

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        temperature: float = 0.3,
    ) -> AssistantTurn:
        """Return an assistant turn that may contain tool calls."""


def _as_list(value: Any) -> list[Any]:
    """Coerce a value to a list.

    The Windows PowerShell transport can collapse a single-element JSON array
    into a bare object, so array shapes are normalized before indexing.
    """

    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _parse_tool_calls(message: dict[str, Any]) -> tuple[ToolCall, ...]:
    calls: list[ToolCall] = []
    for index, entry in enumerate(_as_list(message.get("tool_calls"))):
        if not isinstance(entry, dict):
            continue
        function = entry.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        raw_arguments = function.get("arguments")
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            try:
                decoded = json.loads(raw_arguments or "{}")
            except (TypeError, json.JSONDecodeError):
                decoded = {}
            arguments = decoded if isinstance(decoded, dict) else {}
        calls.append(
            ToolCall(
                id=str(entry.get("id") or f"call_{index}"),
                name=name,
                arguments=arguments,
            )
        )
    return tuple(calls)


def _assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a provider-safe assistant message for the next request."""

    content = message.get("content")
    normalized: dict[str, Any] = {
        "role": "assistant",
        "content": content if isinstance(content, str) else "",
    }
    tool_calls = [
        {
            "id": call.id,
            "type": "function",
            "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
        }
        for call in _parse_tool_calls(message)
    ]
    if tool_calls:
        normalized["tool_calls"] = tool_calls
    return normalized


def _assistant_turn(message: dict[str, Any]) -> AssistantTurn:
    content = message.get("content")
    return AssistantTurn(
        content=content.strip() if isinstance(content, str) else "",
        tool_calls=_parse_tool_calls(message),
        raw_message=_assistant_message(message),
    )


class OllamaLLMClient:
    """Small Ollama /api/chat client using structured output JSON schema."""

    def __init__(self, base_url: str, model: str, timeout_seconds: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def structured_chat(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        temperature: float = 0.0,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "stream": False,
            "format": schema,
            "options": {"temperature": temperature},
        }
        request = Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMUnavailable(f"Could not reach Ollama at {self.base_url}.") from exc

        try:
            return data["message"]["content"]
        except KeyError as exc:
            raise LLMUnavailable("Ollama returned an unexpected response shape.") from exc

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.4,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "stream": False,
            "options": {"temperature": temperature},
        }
        request = Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMUnavailable(f"Could not reach Ollama at {self.base_url}.") from exc

        try:
            return data["message"]["content"]
        except KeyError as exc:
            raise LLMUnavailable("Ollama returned an unexpected response shape.") from exc

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        temperature: float = 0.3,
    ) -> AssistantTurn:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if tools:
            payload["tools"] = tools
        request = Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMUnavailable(f"Could not reach Ollama at {self.base_url}.") from exc

        message = data.get("message")
        if not isinstance(message, dict):
            raise LLMUnavailable("Ollama returned an unexpected response shape.")
        return _assistant_turn(message)


class OpenAIChatLLMClient:
    """OpenAI-compatible /v1/chat/completions client with structured outputs."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        extra_headers: dict[str, str] | None = None,
        api_key_name: str = "OPENAI_API_KEY",
        timeout_seconds: float = 60.0,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.extra_headers = extra_headers or {}
        self.api_key_name = api_key_name
        self.timeout_seconds = timeout_seconds
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    def structured_chat(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        temperature: float = 0.0,
    ) -> str:
        if not self.api_key:
            raise LLMUnavailable(f"{self.api_key_name} is not configured.")

        payload = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "temperature": temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "marlin_intent",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        data = self._post_chat_completions(payload)

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMUnavailable("API provider returned an unexpected response shape.") from exc

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.4,
    ) -> str:
        if not self.api_key:
            raise LLMUnavailable(f"{self.api_key_name} is not configured.")

        payload = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "temperature": temperature,
        }
        data = self._post_chat_completions(payload)

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMUnavailable("API provider returned an unexpected response shape.") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMUnavailable("API provider returned an empty response.")
        return content.strip()

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        temperature: float = 0.3,
    ) -> AssistantTurn:
        if not self.api_key:
            raise LLMUnavailable(f"{self.api_key_name} is not configured.")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = self._post_chat_completions(payload)

        choices = _as_list(data.get("choices"))
        if not choices or not isinstance(choices[0], dict):
            raise LLMUnavailable("API provider returned an unexpected response shape.")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise LLMUnavailable("API provider returned an unexpected response shape.")
        return _assistant_turn(message)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        headers.update({key: value for key, value in self.extra_headers.items() if value})
        return headers

    def _post_chat_completions(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urlopen(
                request,
                timeout=self.timeout_seconds,
                context=self.ssl_context,
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")[:500]
            raise LLMUnavailable(
                f"API provider returned HTTP {exc.code}: {body}"
            ) from exc
        except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            if sys.platform == "win32":
                return self._post_chat_completions_with_powershell(payload, exc)
            raise LLMUnavailable(f"Could not reach API provider at {self.base_url}.") from exc

    def _post_chat_completions_with_powershell(
        self,
        payload: dict[str, Any],
        original_error: BaseException,
    ) -> dict[str, Any]:
        env = os.environ.copy()
        env[self.api_key_name] = self.api_key
        env["MARLIN_API_BASE_URL"] = self.base_url
        env["MARLIN_API_KEY_NAME"] = self.api_key_name
        env["MARLIN_API_TIMEOUT"] = str(int(self.timeout_seconds))
        env["MARLIN_API_EXTRA_HEADERS"] = json.dumps(
            {key: value for key, value in self.extra_headers.items() if value}
        )
        script = r'''
$body = [Console]::In.ReadToEnd()
$headers = @{
  Authorization = "Bearer " + [Environment]::GetEnvironmentVariable($env:MARLIN_API_KEY_NAME)
}
$extraJson = [Environment]::GetEnvironmentVariable("MARLIN_API_EXTRA_HEADERS")
if ($extraJson) {
  $extra = $extraJson | ConvertFrom-Json
  foreach ($prop in $extra.PSObject.Properties) {
    $headers[$prop.Name] = [string]$prop.Value
  }
}
$timeoutSec = [int][Environment]::GetEnvironmentVariable("MARLIN_API_TIMEOUT")
$baseUrl = [Environment]::GetEnvironmentVariable("MARLIN_API_BASE_URL").TrimEnd("/")
$response = Invoke-RestMethod -Uri ($baseUrl + "/chat/completions") -Method Post -Headers $headers -ContentType "application/json" -Body $body -TimeoutSec $timeoutSec
$response | ConvertTo-Json -Depth 20 -Compress
'''
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                input=json.dumps(payload),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=self.timeout_seconds + 5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LLMUnavailable(f"Could not reach API provider at {self.base_url}.") from original_error or exc
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()[:500]
            raise LLMUnavailable(f"API provider request failed: {details}") from original_error
        try:
            return json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise LLMUnavailable("API provider returned an unexpected response shape.") from exc


def create_llm_client(settings) -> LLMClient:
    provider = settings.llm_provider.lower().strip()
    if provider == "ollama":
        return OllamaLLMClient(settings.ollama_url, settings.ollama_model)
    if provider == "openai":
        return OpenAIChatLLMClient(
            os.getenv("OPENAI_API_KEY", ""),
            settings.openai_model,
            base_url=settings.openai_base_url,
            api_key_name="OPENAI_API_KEY",
        )
    if provider == "openrouter":
        openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
        if openrouter_key and not openrouter_key.startswith("sk-or-v1-"):
            raise LLMUnavailable("OPENROUTER_API_KEY must start with sk-or-v1-.")
        return OpenAIChatLLMClient(
            openrouter_key,
            settings.openrouter_model,
            base_url=settings.openrouter_base_url,
            api_key_name="OPENROUTER_API_KEY",
            extra_headers={
                "HTTP-Referer": settings.openrouter_referer,
                "X-OpenRouter-Title": settings.openrouter_title,
            },
        )
    if provider == "groq":
        return OpenAIChatLLMClient(
            os.getenv("GROQ_API_KEY", ""),
            settings.groq_model,
            base_url=settings.groq_base_url,
            api_key_name="GROQ_API_KEY",
            extra_headers={
                "User-Agent": "MARLIN-BrainOS/0.1",
            },
            timeout_seconds=20.0,
        )
    raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")