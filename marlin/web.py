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


class VoiceListenBody(BaseModel):
    execute: bool = False


class ScheduleTaskBody(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    duration_minutes: int = Field(default=60, ge=30, le=480)
    deadline_at: str | None = None
    priority: int = Field(default=50, ge=0, le=100)


class SchedulePlanBody(BaseModel):
    date: str | None = None


class ScheduleEventBody(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    start_at: str
    end_at: str
    recurrence: str = Field(default="none", pattern="^(none|daily|weekday|weekly)$")


class ResearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class FileAnalysisBody(BaseModel):
    query: str = Field(min_length=1, max_length=2000)


class SourceSaveBody(BaseModel):
    note: str = Field(default="", max_length=1000)


class TelegramContactBody(BaseModel):
    alias: str = Field(min_length=1, max_length=80)


class TelegramDraftBody(BaseModel):
    alias: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=4096)


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
        if name not in {"app.js", "styles.css", "marlin-mark.svg"}:
            raise HTTPException(status_code=404)
        return FileResponse(UI_DIR / name)

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return marlin.status()

    @app.get('/api/health')
    def health() -> dict[str, str]:
        return {'version': '2.0'}

    @app.get("/api/graph")
    def graph(limit: int = 1000) -> dict[str, Any]:
        return marlin.graph(limit)

    @app.post("/api/commands")
    def command(body: CommandBody, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        result = marlin.command(body.text, source=body.source)
        if marlin._chat_paused.is_set() and not result.get('pending'):
            marlin._chat_paused.clear()
            marlin.events.publish('voice.chat.resumed')
        return result

    @app.post("/api/actions/{action_id}/approve")
    def approve(action_id: str, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        return marlin.approve_action(action_id)

    @app.post("/api/actions/{action_id}/cancel")
    def cancel(action_id: str, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        return marlin.cancel_action(action_id)

    @app.post('/api/desktop/{action}')
    def desktop_control(action: str, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        if action not in {'show', 'hide', 'exit'}:
            raise HTTPException(status_code=400, detail='Unknown desktop action.')
        if marlin.desktop is None:
            raise HTTPException(status_code=409, detail='MARLIN is running in server mode, not desktop mode.')
        return {'message': marlin.desktop.control(action)}

    @app.post("/api/voice/interrupt")
    def interrupt(x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        marlin.stop_voice()
        marlin._chat_paused.clear()
        return marlin.start_voice_chat()

    @app.post("/api/voice/listen")
    def listen(body: VoiceListenBody | None = None, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        return marlin.listen(execute=bool(body and body.execute))

    @app.post("/api/voice/stop")
    def stop_voice(x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        return marlin.stop_voice()

    @app.post("/api/voice/chat/start")
    def start_voice_chat(x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        return marlin.start_voice_chat()

    @app.post("/api/voice/chat/stop")
    def stop_voice_chat(x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        return marlin.stop_voice_chat()

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

    @app.post('/api/reminders/{reminder_id}/complete')
    def complete_reminder(reminder_id: str, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        result = marlin.store.update_reminder(reminder_id)
        if result is None:
            raise HTTPException(status_code=404, detail='Reminder not found.')
        marlin.events.publish('reminder.updated', reminder=result)
        return result

    @app.post('/api/reminders/{reminder_id}/snooze')
    def snooze_reminder(reminder_id: str, body: SnoozeBody, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        result = marlin.store.update_reminder(reminder_id, minutes=body.minutes)
        if result is None:
            raise HTTPException(status_code=404, detail='Reminder not found.')
        marlin.events.publish('reminder.updated', reminder=result)
        return result

    @app.post('/api/schedule/items')
    def create_schedule_item(body: ScheduleTaskBody, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        from datetime import datetime
        deadline = datetime.fromisoformat(body.deadline_at) if body.deadline_at else None
        return marlin.schedule.add_task(body.title, duration_minutes=body.duration_minutes, deadline=deadline, priority=body.priority)

    @app.post('/api/schedule/plan')
    def plan_schedule(body: SchedulePlanBody | None = None, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        from datetime import datetime
        day = datetime.fromisoformat(body.date) if body and body.date else None
        return marlin.schedule.plan_day(day)

    @app.post('/api/schedule/events')
    def create_schedule_event(body: ScheduleEventBody, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        from datetime import datetime
        return marlin.schedule.add_event(body.title, datetime.fromisoformat(body.start_at), datetime.fromisoformat(body.end_at), recurrence=body.recurrence)

    @app.get('/api/schedule/conflicts')
    def schedule_conflicts(date: str | None = None, x_marlin_token: str | None = Header(default=None)) -> list[dict[str, Any]]:
        require_token(x_marlin_token)
        from datetime import datetime
        return marlin.schedule.conflicts(datetime.fromisoformat(date) if date else None)

    @app.get('/api/schedule/blocks/{block_id}/explanation')
    def schedule_explanation(block_id: str, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        result = marlin.schedule.explain_block(block_id)
        if not result: raise HTTPException(status_code=404, detail='Schedule block not found.')
        return result

    @app.post('/api/schedule/plans/{plan_id}/apply')
    def apply_schedule(plan_id: str, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        result = marlin.schedule.apply_plan(plan_id)
        if not result: raise HTTPException(status_code=409, detail='Plan is missing or no longer a preview.')
        return result

    @app.post('/api/schedule/plans/{plan_id}/discard')
    def discard_schedule(plan_id: str, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        with marlin.store.connect() as connection:
            cursor = connection.execute("UPDATE schedule_plans SET status='discarded' WHERE id=? AND status='preview'", (plan_id,))
        if not cursor.rowcount: raise HTTPException(status_code=409, detail='Plan is missing or no longer a preview.')
        marlin.events.publish('schedule.plan.discarded', plan_id=plan_id)
        return {'id': plan_id, 'status': 'discarded'}

    @app.post('/api/schedule/items/{item_id}/complete')
    def complete_schedule(item_id: str, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        result = marlin.store.complete_schedule_item(item_id)
        if not result: raise HTTPException(status_code=404, detail='Schedule item not found.')
        return result

    @app.get('/api/preferences')
    def preferences(x_marlin_token: str | None = Header(default=None)) -> list[dict[str, Any]]:
        require_token(x_marlin_token)
        return marlin.store.list_preferences()

    @app.delete('/api/preferences/{preference_id}')
    def forget_preference(preference_id: str, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        count = marlin.store.forget_preference(preference_id)
        if not count: raise HTTPException(status_code=404, detail='Preference not found.')
        marlin.events.publish('preference.forgotten', preference_id=preference_id)
        return {'forgotten': count}

    @app.post('/api/research')
    def research(body: ResearchBody, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        return marlin.research.search(body.query)

    @app.post('/api/research/sources/{source_id}/save')
    def save_source(source_id: str, body: SourceSaveBody | None = None, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        result = marlin.store.save_research_source(source_id, body.note if body else '')
        if not result: raise HTTPException(status_code=404, detail='Research source not found.')
        return result

    @app.get('/api/research/sources/{source_id}')
    def source_details(source_id: str, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        result = marlin.store.get_research_source(source_id)
        if not result: raise HTTPException(status_code=404, detail='Research source not found.')
        return result

    @app.post('/api/files/analyze')
    def analyze_file(body: FileAnalysisBody, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        try: return marlin.files.analyze(body.query)
        except (OSError, ValueError, PermissionError) as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post('/api/files/related')
    def related_files(body: FileAnalysisBody, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        try: return marlin.files.related_files(body.query)
        except (OSError, ValueError, PermissionError) as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post('/api/files/change-impact')
    def file_change_impact(body: FileAnalysisBody, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        try: return marlin.files.change_impact(body.query)
        except (OSError, ValueError, PermissionError) as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post('/api/files/explain-project')
    def explain_project(body: FileAnalysisBody, x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        try: return marlin.files.explain_project(body.query)
        except (OSError, ValueError, PermissionError) as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get('/api/telegram/status')
    def telegram_status(x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        return marlin.telegram.status()

    @app.post('/api/telegram/pair-code')
    def telegram_pair_code(x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        try:
            return marlin.telegram.create_pair_code()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get('/api/telegram/contacts')
    def telegram_contacts(x_marlin_token: str | None = Header(default=None)) -> list[dict[str, Any]]:
        require_token(x_marlin_token)
        return marlin.store.telegram_contacts()

    @app.delete('/api/telegram/owner')
    def telegram_unpair_owner(
        x_marlin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_marlin_token)
        try:
            return marlin.telegram.unpair_owner()
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post('/api/telegram/contacts/{user_id}/approve')
    def telegram_approve_contact(
        user_id: int, body: TelegramContactBody, x_marlin_token: str | None = Header(default=None)
    ) -> dict[str, Any]:
        require_token(x_marlin_token)
        try:
            result = marlin.telegram.approve_contact(user_id, body.alias)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result

    @app.delete('/api/telegram/contacts/{user_id}')
    def telegram_revoke_contact(
        user_id: int, x_marlin_token: str | None = Header(default=None)
    ) -> dict[str, bool]:
        require_token(x_marlin_token)
        changed = marlin.store.reject_telegram_contact(user_id) or marlin.store.revoke_telegram_contact(user_id)
        if not changed:
            raise HTTPException(status_code=404, detail='Telegram contact not found.')
        marlin.events.publish('telegram.contact.revoked', user_id=user_id)
        return {'revoked': True}

    @app.get('/api/telegram/drafts')
    def telegram_drafts(x_marlin_token: str | None = Header(default=None)) -> list[dict[str, Any]]:
        require_token(x_marlin_token)
        return marlin.store.telegram_drafts()

    @app.post('/api/telegram/drafts')
    def telegram_create_draft(
        body: TelegramDraftBody, x_marlin_token: str | None = Header(default=None)
    ) -> dict[str, Any]:
        require_token(x_marlin_token)
        try:
            return marlin.telegram.create_draft(body.alias, body.content)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post('/api/telegram/drafts/{draft_id}/approve')
    def telegram_approve_draft(
        draft_id: str, x_marlin_token: str | None = Header(default=None)
    ) -> dict[str, Any]:
        require_token(x_marlin_token)
        try:
            return marlin.telegram.approve_draft(draft_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post('/api/telegram/drafts/{draft_id}/cancel')
    def telegram_cancel_draft(
        draft_id: str, x_marlin_token: str | None = Header(default=None)
    ) -> dict[str, Any]:
        require_token(x_marlin_token)
        try:
            return marlin.telegram.cancel_draft(draft_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post('/api/telegram/test')
    def telegram_test(x_marlin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_marlin_token)
        try:
            return marlin.telegram.send_test()
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
