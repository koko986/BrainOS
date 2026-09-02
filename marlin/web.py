"""FastAPI backend and local-only cockpit server for MARLIN V2."""

from __future__ import annotations

import asyncio
import secrets
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from marlin.runtime import MarlinRuntime


UI_DIR = Path(__file__).resolve().parent / "ui"


class CommandBody(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    source: str = "ui"


class AlarmBody(BaseModel):
    label: str = "Alarm"
    due_at: str


class SnoozeBody(BaseModel):
    minutes: int = Field(default=5, ge=1, le=1440)


class VoiceDeviceBody(BaseModel):
    device: str = Field(default="", max_length=200)


def create_app(runtime: MarlinRuntime | None = None) -> FastAPI:
    marlin = runtime or MarlinRuntime()
    token = secrets.token_urlsafe(32)
    app = FastAPI(title="MARLIN V2", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.marlin = marlin
    app.state.token = token

    def require_token(value: str | None) -> None:
        if not value or not secrets.compare_digest(value, token):
            raise HTTPException(status_code=403, detail="Invalid MARLIN session token.")

    @app.middleware("http")
    async def local_only(request: Request, call_next):  # type: ignore[no-untyped-def]
        client = request.client.host if request.client else ""
        if client not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            return HTMLResponse("MARLIN is local-only.", status_code=403)
        origin = request.headers.get("origin", "")
        if origin and not any(origin.startswith(prefix) for prefix in ("http://127.0.0.1", "http://localhost")):
            return HTMLResponse("Cross-origin requests are blocked.", status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        html = (UI_DIR / "index.html").read_text(encoding="utf-8")
        return html.replace("__MARLIN_TOKEN__", token)

    @app.get("/assets/{name}")
    def asset(name: str) -> FileResponse:
        if name not in {"app.js", "styles.css"}:
            raise HTTPException(status_code=404)
        return FileResponse(UI_DIR / name)

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return marlin.status()

    @app.get("/api/graph")
    def graph(limit: int = 1000) -> dict[str, Any]:
        return marlin.graph(limit)

    @app.post("/api/commands")
    def command(body: CommandBody, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        return marlin.command(body.text, source=body.source)

    @app.post("/api/actions/{action_id}/approve")
    def approve(action_id: str, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        return marlin.approve_action(action_id)

    @app.post("/api/actions/{action_id}/cancel")
    def cancel(action_id: str, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        return marlin.cancel_action(action_id)

    @app.post("/api/voice/listen")
    def listen(x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        return marlin.listen(execute=False)

    @app.post("/api/voice/stop")
    def stop_voice(x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        return marlin.stop_voice()

    @app.post("/api/voice/device")
    def voice_device(body: VoiceDeviceBody, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        marlin.settings.microphone_device = body.device.strip()
        return {"ok": True, "device": marlin.settings.microphone_device or "system default"}

    @app.post("/api/alarms")
    def create_alarm(body: AlarmBody, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        from datetime import datetime
        return marlin.store.add_alarm(body.label, datetime.fromisoformat(body.due_at))

    @app.post("/api/alarms/{alarm_id}/snooze")
    def snooze(alarm_id: str, body: SnoozeBody, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        result = marlin.store.snooze_alarm(alarm_id, body.minutes)
        if result is None:
            raise HTTPException(status_code=404, detail="Alarm not found.")
        return result

    @app.websocket("/api/events")
    async def events(websocket: WebSocket) -> None:
        if websocket.query_params.get("token") != token:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue = marlin.events.create_queue()
        try:
            while True:
                event = await marlin.events.next_async(queue)
                await websocket.send_json(event.to_dict())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            marlin.events.remove_queue(queue)

    return app


def run_server(runtime: MarlinRuntime, host: str, port: int) -> None:
    import uvicorn
    uvicorn.run(create_app(runtime), host=host, port=port, log_level="warning")


def start_server_thread(runtime: MarlinRuntime, host: str, port: int) -> threading.Thread:
    thread = threading.Thread(target=run_server, args=(runtime, host, port), name="marlin-web", daemon=True)
    thread.start()
    return thread
