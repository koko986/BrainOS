"""Conversation client that delegates normal chat to OpenCode."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
from atexit import register as register_atexit
from dataclasses import dataclass
from pathlib import Path


class OpenCodeUnavailable(RuntimeError):
    """Raised when OpenCode cannot provide a conversation reply."""


@dataclass
class OpenCodeConversationClient:
    """Run OpenCode as MARLIN's text conversation engine."""

    command: str = "opencode"
    model: str | None = None
    timeout_seconds: float = 25.0
    working_directory: Path | None = None
    use_server: bool = True
    server_host: str = "127.0.0.1"
    server_port: int = 4096
    continue_session: bool = True
    _server_process: subprocess.Popen | None = None
    _server_owned: bool = False

    def reply(self, text: str) -> str:
        executable = shutil.which(self.command)
        if executable is None:
            raise OpenCodeUnavailable(
                "OpenCode is not installed or is not on PATH. Install/configure OpenCode, then try again."
            )

        attempts = self._candidate_commands(executable, text)
        last_error = "OpenCode exited without details."
        for args in attempts:
            try:
                process, output, error = self._run(args)
            except OpenCodeUnavailable as exc:
                last_error = str(exc)
                if "too long" in last_error.lower():
                    raise
                continue

            reply = _extract_reply_text((output or "").strip())
            details = (error or reply or output or "").strip()
            if process.returncode == 0 and reply:
                return reply
            last_error = details or last_error
            if not _should_retry(last_error):
                break

        raise OpenCodeUnavailable(_friendly_error(last_error))

    def _candidate_commands(self, executable: str, text: str) -> list[list[str]]:
        base = [executable, "run", text, "--format", "json"]
        attempts: list[list[str]] = []
        if self.use_server:
            try:
                self.ensure_server(executable)
                attached = [*base, "--attach", self.server_url]
                if self.continue_session:
                    attempts.append([*attached, "--continue"])
                attempts.append(attached)
            except OpenCodeUnavailable:
                pass
        attempts.append(base)
        if self.model:
            return [command + ["--model", self.model] for command in attempts]
        return attempts

    def _run(self, args: list[str]) -> tuple[subprocess.Popen, str, str]:
        creationflags = 0
        if sys.platform == "win32" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            process = subprocess.Popen(
                args,
                cwd=str(self.working_directory) if self.working_directory else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=creationflags,
            )
            output, error = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _kill_process_tree(process)
            raise OpenCodeUnavailable(
                f"OpenCode took too long to respond after {self.timeout_seconds:.0f}s. Try a faster OpenCode model/provider."
            ) from exc
        except OSError as exc:
            raise OpenCodeUnavailable(f"Could not start OpenCode: {exc}") from exc
        return process, output, error

    @property
    def server_url(self) -> str:
        return f"http://{self.server_host}:{self.server_port}"

    def ensure_server(self, executable: str | None = None) -> None:
        if not self.use_server:
            return
        executable = executable or shutil.which(self.command)
        if executable is None:
            raise OpenCodeUnavailable(
                "OpenCode is not installed or is not on PATH. Install/configure OpenCode, then try again."
            )
        if _is_port_open(self.server_host, self.server_port):
            return
        if self._server_process and self._server_process.poll() is None:
            self._wait_for_server()
            return

        creationflags = 0
        startupinfo = None
        if sys.platform == "win32":
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationflags |= subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "STARTUPINFO"):
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        try:
            self._server_process = subprocess.Popen(
                [
                    executable,
                    "serve",
                    "--hostname",
                    self.server_host,
                    "--port",
                    str(self.server_port),
                ],
                cwd=str(self.working_directory) if self.working_directory else None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
        except OSError as exc:
            raise OpenCodeUnavailable(f"Could not start OpenCode server: {exc}") from exc

        self._server_owned = True
        register_atexit(self.stop_server)
        self._wait_for_server()

    def stop_server(self) -> None:
        if not self._server_owned or self._server_process is None:
            return
        if self._server_process.poll() is None:
            _kill_process_tree(self._server_process)
        self._server_process = None
        self._server_owned = False

    def _wait_for_server(self, timeout_seconds: float = 10.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if _is_port_open(self.server_host, self.server_port):
                return
            if self._server_process and self._server_process.poll() is not None:
                raise OpenCodeUnavailable("OpenCode server stopped before it was ready.")
            time.sleep(0.2)
        raise OpenCodeUnavailable(f"OpenCode server did not start at {self.server_url}.")


def _extract_reply_text(output: str) -> str:
    if not output:
        return ""

    chunks: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            chunks.append(line)
            continue
        part = event.get("part")
        if isinstance(part, dict) and part.get("type") == "text":
            text = str(part.get("text") or "").strip()
            if text:
                chunks.append(text)

    return "\n".join(chunks).strip()


def _should_retry(details: str) -> bool:
    lowered = details.lower()
    return any(token in lowered for token in ("session not found", "continue", "attach", "server"))


def _friendly_error(details: str) -> str:
    lowered = details.lower()
    if "took too long" in lowered:
        return details
    if "auth" in lowered or "login" in lowered:
        return "OpenCode needs authentication or provider setup. Run `opencode` and use `/connect`."
    if "model" in lowered:
        return "OpenCode has no usable model selected. Run `opencode`, choose a fast model, then try again."
    if "session not found" in lowered or "attach" in lowered:
        return "OpenCode server mode is not accepting attached runs yet. MARLIN fell back safely; check your OpenCode server/session setup."
    if "connect" in lowered or "econnrefused" in lowered:
        return "OpenCode server is unavailable. MARLIN will try to start it again on the next message."
    return f"OpenCode failed: {details[:700]}"


def _is_port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.35):
            return True
    except OSError:
        return False


def _kill_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    process.kill()