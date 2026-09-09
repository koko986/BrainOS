"""Private Telegram bridge for safe remote MARLIN commands and approved messages."""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from queue import Empty
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from marlin.config import MarlinSettings
from marlin.events import EventBus
from marlin.storage import MarlinStore


REMOTE_CALLBACK_COMMANDS = {
    "open_chrome": "/open chrome",
    "open_vscode": "/open vscode",
    "open_canva": "/open canva",
    "open_youtube": "/open youtube",
    "open_edge": "/open edge",
    "open_firefox": "/open firefox",
    "open_notepad": "/open notepad",
    "open_calculator": "/open calculator",
    "open_explorer": "/open explorer",
    "open_documents": "/open documents",
    "open_downloads": "/open downloads",
    "camera_open": "/camera open",
    "camera_close": "/camera close",
    "play_music": "/play youtube relaxing music",
    "media_pause": "/pause",
    "media_next": "/next",
    "media_previous": "/previous",
    "media_mute": "/mute",
    "volume_30": "/volume 30",
    "volume_50": "/volume 50",
    "volume_70": "/volume 70",
    "show_reminders": "/reminders",
    "show_priority": "/priority",
    "show_status": "/status",
}


class TelegramError(RuntimeError):
    pass


def _enable_windows_trust_store() -> None:
    try:
        import truststore
        truststore.inject_into_ssl()
    except (ImportError, OSError):
        pass


class TelegramTransport(Protocol):
    def get_me(self) -> dict[str, Any]: ...
    def set_commands(self, commands: list[dict[str, str]]) -> None: ...
    def get_updates(self, offset: int, timeout: int = 25) -> list[dict[str, Any]]: ...
    def send_message(
        self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...
    def answer_callback(self, callback_id: str, text: str = "") -> None: ...


class TelegramHTTPTransport:
    """Minimal Bot API client whose exceptions never expose the bot token."""

    def __init__(self, token: str):
        _enable_windows_trust_store()
        self._base = f"https://api.telegram.org/bot{token}"

    def _call(self, method: str, payload: dict[str, Any], *, timeout: int = 35) -> Any:
        request = Request(
            f"{self._base}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise TelegramError("Telegram API request failed. Check the bot token and internet connection.") from exc
        if not body.get("ok"):
            description = str(body.get("description") or "Telegram rejected the request.")
            raise TelegramError(description[:300])
        return body.get("result")

    def get_me(self) -> dict[str, Any]:
        return dict(self._call("getMe", {}))

    def set_commands(self, commands: list[dict[str, str]]) -> None:
        self._call("setMyCommands", {"commands": commands})

    def get_updates(self, offset: int, timeout: int = 25) -> list[dict[str, Any]]:
        result = self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
            timeout=timeout + 10,
        )
        return list(result or [])

    def send_message(
        self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": int(chat_id),
            "text": str(text)[:4096],
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return dict(self._call("sendMessage", payload))

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})


class TelegramBridge:
    def __init__(
        self,
        settings: MarlinSettings,
        store: MarlinStore,
        events: EventBus,
        *,
        remote_command: Callable[[str], str],
        briefing: Callable[[], str],
        transport: TelegramTransport | None = None,
    ):
        self.settings = settings
        self.store = store
        self.events = events
        self.remote_command = remote_command
        self.briefing = briefing
        self.transport = transport or (
            TelegramHTTPTransport(settings.telegram_bot_token)
            if settings.telegram_enabled and settings.telegram_bot_token else None
        )
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._event_thread: threading.Thread | None = None
        self._connected = False
        self._error = ""
        self._bot_name = ""

    def start(self) -> bool:
        if not self.settings.telegram_enabled or self.transport is None:
            return False
        if self._poll_thread and self._poll_thread.is_alive():
            return True
        self._stop.clear()
        self._poll_thread = threading.Thread(target=self._poll, name="marlin-telegram", daemon=True)
        self._event_thread = threading.Thread(target=self._events, name="marlin-telegram-events", daemon=True)
        self._poll_thread.start()
        self._event_thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        for thread in (self._poll_thread, self._event_thread):
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2)

    def status(self) -> dict[str, Any]:
        owner = self.store.telegram_owner()
        contacts = [
            {
                "user_id": item["user_id"], "role": item["role"], "alias": item["alias"],
                "display_name": item["display_name"], "active": bool(item["active"]),
            }
            for item in self.store.telegram_contacts()
        ]
        drafts = [
            {
                key: item.get(key) for key in (
                    "id", "recipient_alias", "content", "status", "expires_at", "created_at",
                    "resolved_at", "error",
                )
            }
            for item in self.store.telegram_drafts()
        ]
        return {
            "enabled": self.settings.telegram_enabled,
            "configured": bool(self.settings.telegram_bot_token),
            "running": bool(self._poll_thread and self._poll_thread.is_alive()),
            "connected": self._connected,
            "error": self._error,
            "bot": self._bot_name,
            "owner": ({"user_id": owner["user_id"], "display_name": owner["display_name"]} if owner else None),
            "contacts": contacts,
            "drafts": drafts,
            "deliveries": self.store.telegram_deliveries(),
        }

    def create_pair_code(self) -> dict[str, Any]:
        if not self.settings.telegram_enabled or not self.settings.telegram_bot_token:
            raise TelegramError("Enable Telegram and add a BotFather token before pairing.")
        return self.store.create_telegram_pair_code()

    def unpair_owner(self) -> dict[str, Any]:
        owner = self.store.telegram_owner()
        if owner is None:
            raise ValueError("A Telegram owner is not currently paired.")
        if self.transport:
            try:
                self.transport.send_message(
                    int(owner["chat_id"]),
                    "This Telegram account was removed as the MARLIN owner from the local cockpit.",
                )
            except TelegramError:
                pass
        removed = self.store.unpair_telegram_owner()
        if removed is None:
            raise ValueError("A Telegram owner is not currently paired.")
        self.store.record_action(
            "telegram_unpair", "Remove Telegram owner", str(removed.get("display_name") or "owner"), "complete"
        )
        result = {
            "removed": True,
            "display_name": str(removed.get("display_name") or "Telegram owner"),
        }
        self.events.publish("telegram.owner.unpaired", owner=result)
        return result

    def create_draft(self, alias: str, content: str) -> dict[str, Any]:
        if not self.settings.telegram_enabled or self.transport is None:
            raise TelegramError("Telegram is disabled or not configured.")
        self._require_owner(None)
        draft = self.store.create_telegram_draft(alias, content)
        self.store.record_action("telegram_draft", "Telegram message draft", alias, "pending")
        self.events.publish("telegram.draft.created", draft=draft)
        owner = self.store.telegram_owner()
        if owner and self.transport:
            self._send_draft_preview(owner["chat_id"], draft)
        return draft

    def approve_contact(self, user_id: int, alias: str) -> dict[str, Any]:
        contact = self.store.approve_telegram_contact(user_id, alias)
        if contact is None:
            raise ValueError("That pending contact was not found.")
        if self.transport:
            try:
                self.transport.send_message(
                    int(contact["chat_id"]), "You can now receive approved MARLIN messages."
                )
            except TelegramError:
                pass
        self.events.publish("telegram.contact.approved", contact={
            "user_id": contact["user_id"], "alias": contact["alias"],
            "display_name": contact["display_name"],
        })
        return contact

    def approve_draft(self, draft_id: str, *, owner_user_id: int | None = None) -> dict[str, Any]:
        self._require_owner(owner_user_id)
        if self.transport is None:
            raise TelegramError("Telegram is not configured.")
        draft = self.store.claim_telegram_draft(draft_id)
        try:
            sent = self.transport.send_message(int(draft["chat_id"]), str(draft["content"]))
            message_id = int(sent.get("message_id") or 0) or None
            result = self.store.finish_telegram_draft(draft_id, status="sent", message_id=message_id) or {}
            self.store.record_telegram_delivery(
                kind="contact_message", content=str(draft["content"]),
                recipient_user_id=int(draft["recipient_user_id"]), status="sent", message_id=message_id,
            )
            self.store.record_action("telegram_send", "Telegram message", str(draft["recipient_alias"]), "complete")
            self.events.publish("telegram.draft.sent", draft=result)
            return result
        except Exception as exc:
            message = str(exc)[:500]
            self.store.finish_telegram_draft(draft_id, status="failed", error=message)
            self.store.record_telegram_delivery(
                kind="contact_message", content=str(draft["content"]),
                recipient_user_id=int(draft["recipient_user_id"]), status="failed", error=message,
            )
            self.store.record_action("telegram_send", "Telegram message", str(draft["recipient_alias"]), "failed")
            self.events.publish("telegram.draft.failed", draft_id=draft_id, error=message)
            raise

    def cancel_draft(self, draft_id: str, *, owner_user_id: int | None = None) -> dict[str, Any]:
        self._require_owner(owner_user_id)
        result = self.store.cancel_telegram_draft(draft_id)
        if result is None:
            raise ValueError("That message draft is missing or was already used.")
        self.store.record_action("telegram_draft", "Telegram message draft", result["recipient_alias"], "cancelled")
        self.events.publish("telegram.draft.cancelled", draft=result)
        return result

    def send_test(self) -> dict[str, Any]:
        owner = self.store.telegram_owner()
        if not owner:
            raise ValueError("Pair a Telegram owner first.")
        return self._notify_owner("test", "MARLIN Telegram is connected.")

    def _poll(self) -> None:
        backoff = 1.0
        assert self.transport is not None
        offset = int(self.store.get_state("telegram.update_offset", "0") or 0)
        while not self._stop.is_set():
            try:
                if not self._bot_name:
                    me = self.transport.get_me()
                    self._bot_name = str(me.get("username") or me.get("first_name") or "MARLIN")
                    self.transport.set_commands(self._bot_commands())
                    self.events.publish("telegram.connected", bot=self._bot_name)
                for update in self.transport.get_updates(offset):
                    update_id = int(update.get("update_id") or 0)
                    if update_id < offset:
                        continue
                    try:
                        self._handle_update(update)
                    except Exception as exc:
                        self._error = self._safe_error(exc)
                        self.events.publish("telegram.error", error=self._error)
                    finally:
                        offset = update_id + 1
                        self.store.set_state("telegram.update_offset", str(offset))
                self._send_timed_notifications()
                self._connected = True
                self._error = ""
                backoff = 1.0
            except Exception as exc:
                self._connected = False
                self._error = self._safe_error(exc)
                self.events.publish("telegram.error", error=self._error)
                self._stop.wait(backoff)
                backoff = min(60.0, backoff * 2)

    def _events(self) -> None:
        queue = self.events.create_queue()
        try:
            while not self._stop.is_set():
                try:
                    event = queue.get(timeout=.5)
                except Empty:
                    continue
                if event.type == "reminder.fired":
                    item = event.data.get("reminder") or {}
                    self._notify_owner("reminder", f"Reminder: {item.get('text', 'MARLIN reminder')}", f"reminder:{item.get('id')}")
                elif event.type == "alarm.fired":
                    item = event.data.get("alarm") or {}
                    self._notify_owner("alarm", f"Alarm: {item.get('label', 'MARLIN alarm')}", f"alarm:{item.get('id')}:{item.get('due_at')}")
                elif event.type == "action.result" and not event.data.get("ok", True):
                    self._notify_owner("failure", f"MARLIN action failed: {event.data.get('message', 'Unknown error')}")
        finally:
            self.events.remove_queue(queue)

    def _handle_update(self, update: dict[str, Any]) -> None:
        if isinstance(update.get("callback_query"), dict):
            self._handle_callback(update["callback_query"])
        elif isinstance(update.get("message"), dict):
            self._handle_message(update["message"])

    def _handle_message(self, message: dict[str, Any]) -> None:
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or not isinstance(message.get("text"), str):
            return
        user_id, chat_id = int(sender.get("id") or 0), int(chat.get("id") or 0)
        if not user_id or not chat_id:
            return
        name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])).strip()
        text = str(message["text"]).strip()
        if not text or len(text) > 4096:
            self.transport.send_message(chat_id, "Telegram commands must contain 1 to 4096 characters.")
            return
        owner = self.store.telegram_owner()
        if text.lower().startswith("/pair "):
            try:
                paired = self.store.pair_telegram_owner(
                    text.split(maxsplit=1)[1], user_id=user_id, chat_id=chat_id, display_name=name
                )
                self.transport.send_message(
                    chat_id, "Telegram is paired. You are MARLIN's owner.", self._command_keyboard()
                )
                self.events.publish("telegram.owner.paired", owner={"user_id": paired["user_id"], "display_name": paired["display_name"]})
            except ValueError as exc:
                self.transport.send_message(chat_id, str(exc))
            return
        if not owner or int(owner["user_id"]) != user_id:
            request = self.store.request_telegram_contact(user_id=user_id, chat_id=chat_id, display_name=name)
            if request["role"] == "contact" and request["active"]:
                self.transport.send_message(chat_id, "This contact can receive approved messages but cannot command MARLIN.")
                return
            self.transport.send_message(chat_id, "Your contact request was sent to the MARLIN owner. You cannot issue commands.")
            if owner and request["role"] == "pending":
                markup = {"inline_keyboard": [[
                    {"text": "Add", "callback_data": f"contact:add:{user_id}"},
                    {"text": "Reject", "callback_data": f"contact:reject:{user_id}"},
                ]]}
                self.transport.send_message(
                    int(owner["chat_id"]), f"Telegram contact request from {name or user_id} ({user_id}).", markup
                )
            return
        self._handle_owner_text(owner, text)

    def _handle_owner_text(self, owner: dict[str, Any], text: str) -> None:
        chat_id = int(owner["chat_id"])
        lower = text.lower().strip()
        if lower in {"/start", "/help", "/menu"}:
            self.transport.send_message(chat_id, self._help_text(), self._command_keyboard())
            return
        quick_menu = self._quick_action_menu(lower)
        if quick_menu is not None:
            title, markup = quick_menu
            self.transport.send_message(chat_id, title, markup)
            return
        name_match = re.fullmatch(r"/name\s+(\d+)\s+(.+)", text, re.I)
        if name_match:
            try:
                contact = self.approve_contact(int(name_match.group(1)), name_match.group(2))
                self.transport.send_message(chat_id, f"Contact saved as {contact['alias']}.")
            except ValueError as exc:
                self.transport.send_message(chat_id, str(exc))
            return
        if lower == "/contacts":
            contacts = self.store.telegram_contacts()
            lines = [f"{item.get('alias') or '(pending)'}: {item['display_name']} [{item['role']}]" for item in contacts]
            self.transport.send_message(chat_id, "Contacts:\n" + "\n".join(lines) if lines else "No Telegram contacts.")
            return
        draft_parts = self.draft_parts(text)
        if draft_parts:
            try:
                draft = self.create_draft(*draft_parts)
                if not self.store.telegram_owner():
                    self._send_draft_preview(chat_id, draft)
            except ValueError as exc:
                self.transport.send_message(chat_id, str(exc))
            return
        try:
            reply = self.remote_command(text)
        except Exception:
            reply = "That MARLIN command failed safely. Check the local cockpit for details."
        self.transport.send_message(chat_id, reply[:4096])

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        sender = callback.get("from") or {}
        user_id = int(sender.get("id") or 0)
        callback_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        try:
            self._require_owner(user_id)
            if data.startswith("draft:send:"):
                result = self.approve_draft(data.split(":", 2)[2], owner_user_id=user_id)
                text = f"Sent to {result['recipient_alias']}."
            elif data.startswith("draft:cancel:"):
                result = self.cancel_draft(data.split(":", 2)[2], owner_user_id=user_id)
                text = f"Cancelled message to {result['recipient_alias']}."
            elif data.startswith("contact:add:"):
                contact_id = int(data.rsplit(":", 1)[1])
                text = f"Reply with /name {contact_id} Alias to approve this contact."
            elif data.startswith("contact:reject:"):
                self.store.reject_telegram_contact(int(data.rsplit(":", 1)[1]))
                text = "Contact request rejected."
            elif data.startswith("remote:"):
                command = REMOTE_CALLBACK_COMMANDS.get(data.split(":", 1)[1])
                if command is None:
                    raise ValueError("That remote shortcut is not allowed.")
                if callback_id and self.transport:
                    self.transport.answer_callback(callback_id, "Running MARLIN command...")
                    callback_id = ""
                result = self.remote_command(command)
                owner = self.store.telegram_owner()
                if owner and self.transport:
                    self.transport.send_message(int(owner["chat_id"]), result[:4096])
                text = "Command complete."
            else:
                text = "That Telegram action is not supported."
        except Exception as exc:
            text = self._safe_error(exc)
        if callback_id and self.transport:
            self.transport.answer_callback(callback_id, text)

    def _send_draft_preview(self, chat_id: int, draft: dict[str, Any]) -> None:
        markup = {"inline_keyboard": [[
            {"text": "Send", "callback_data": f"draft:send:{draft['id']}"},
            {"text": "Cancel", "callback_data": f"draft:cancel:{draft['id']}"},
        ]]}
        self.transport.send_message(
            int(chat_id),
            f"Message draft\nTo: {draft['recipient_alias']}\n\n{draft['content']}\n\nExpires in two minutes.",
            markup,
        )

    def _notify_owner(self, kind: str, text: str, dedupe_key: str | None = None) -> dict[str, Any]:
        owner = self.store.telegram_owner()
        if not owner or not self.transport:
            return {"status": "skipped"}
        if dedupe_key and self.store.telegram_delivery_exists(dedupe_key):
            return {"status": "duplicate"}
        try:
            sent = self.transport.send_message(int(owner["chat_id"]), text[:4096])
            message_id = int(sent.get("message_id") or 0) or None
            return self.store.record_telegram_delivery(
                kind=kind, content=text, recipient_user_id=int(owner["user_id"]),
                status="sent", dedupe_key=dedupe_key, message_id=message_id,
            )
        except Exception as exc:
            return self.store.record_telegram_delivery(
                kind=kind, content=text, recipient_user_id=int(owner["user_id"]),
                status="failed", dedupe_key=dedupe_key, error=self._safe_error(exc),
            )

    def _send_timed_notifications(self) -> None:
        now = datetime.now().astimezone()
        try:
            hour, minute = (int(part) for part in self.settings.telegram_briefing_time.split(":", 1))
        except (TypeError, ValueError):
            hour, minute = 8, 0
        date_key = now.strftime("%Y-%m-%d")
        if (now.hour, now.minute) >= (hour, minute) and self.store.get_state("telegram.briefing_date") != date_key:
            result = self._notify_owner("briefing", self.briefing(), f"briefing:{date_key}")
            if result.get("status") in {"sent", "duplicate"}:
                self.store.set_state("telegram.briefing_date", date_key)
        until = (now + timedelta(minutes=10)).astimezone(UTC).isoformat(timespec="seconds")
        after = now.astimezone(UTC).isoformat(timespec="seconds")
        with self.store.connect() as connection:
            rows = connection.execute(
                """SELECT id,title,start_at,'event' AS source FROM schedule_items
                   WHERE status='pending' AND fixed=1 AND start_at>? AND start_at<=?
                   UNION ALL
                   SELECT id,title,start_at,'plan' AS source FROM schedule_blocks
                   WHERE status='applied' AND start_at>? AND start_at<=?
                   ORDER BY start_at""",
                (after, until, after, until),
            ).fetchall()
        for item in rows:
            local = datetime.fromisoformat(item["start_at"]).astimezone().strftime("%I:%M %p")
            self._notify_owner(
                "schedule_warning", f"Upcoming at {local}: {item['title']}",
                f"schedule:{item['source']}:{item['id']}:{item['start_at']}",
            )

    def _require_owner(self, user_id: int | None) -> dict[str, Any]:
        owner = self.store.telegram_owner()
        if owner is None:
            raise ValueError("A Telegram owner has not been paired.")
        if user_id is not None and int(owner["user_id"]) != int(user_id):
            raise ValueError("Only the paired MARLIN owner can do that.")
        return owner

    @staticmethod
    def draft_parts(text: str) -> tuple[str, str] | None:
        patterns = (
            r"/send\s+(\S+)\s+(.+)",
            r"(?:tell|message)\s+(\S+)\s+(.+)",
            r"send\s+(.+?)\s+to\s+(\S+)",
        )
        for index, pattern in enumerate(patterns):
            match = re.fullmatch(pattern, text.strip(), re.I | re.S)
            if match:
                if index == 2:
                    alias, content = match.group(2), match.group(1)
                else:
                    alias, content = match.group(1), match.group(2)
                if alias.lower() in {"me", "us", "him", "her", "them", "marlin"}:
                    return None
                return alias, content
        return None

    @staticmethod
    def _help_text() -> str:
        return (
            "MARLIN remote controls are ready. Tap Apps, Media, Camera, or Files below.\n\n"
            "Commands: /open app, /play topic, /volume 0-100, /pause, /next, "
            "/previous, /mute, /lock, /status, /briefing, /reminders, /schedule, "
            "/priority, /graph, /search topic, /research topic, /contacts, "
            "/send Alias message"
        )

    @staticmethod
    def _bot_commands() -> list[dict[str, str]]:
        return [
            {"command": "menu", "description": "Show tappable MARLIN controls"},
            {"command": "apps", "description": "Open the application menu"},
            {"command": "media", "description": "Show music and volume controls"},
            {"command": "camera", "description": "Open or close the local camera"},
            {"command": "files", "description": "Show folder and search controls"},
            {"command": "open", "description": "Open an allow-listed app"},
            {"command": "play", "description": "Play a YouTube search"},
            {"command": "volume", "description": "Set volume from 0 to 100"},
            {"command": "pause", "description": "Pause or resume media"},
            {"command": "next", "description": "Play the next media item"},
            {"command": "previous", "description": "Play the previous media item"},
            {"command": "mute", "description": "Mute or unmute Windows"},
            {"command": "search", "description": "Search indexed files"},
            {"command": "reminders", "description": "Show pending reminders"},
            {"command": "priority", "description": "Show Prolog priorities"},
            {"command": "lock", "description": "Lock this Windows computer"},
            {"command": "status", "description": "Show MARLIN status"},
            {"command": "help", "description": "Show all safe commands"},
        ]

    @staticmethod
    def _command_keyboard() -> dict[str, Any]:
        return {
            "keyboard": [
                [{"text": "/apps"}, {"text": "/media"}],
                [{"text": "/camera"}, {"text": "/files"}],
                [{"text": "/play youtube relaxing music"}, {"text": "/pause"}],
                [{"text": "/reminders"}, {"text": "/priority"}],
                [{"text": "/search report.pdf"}, {"text": "/status"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
            "input_field_placeholder": "Command MARLIN",
        }

    @staticmethod
    def _quick_action_menu(command: str) -> tuple[str, dict[str, Any]] | None:
        menus: dict[str, tuple[str, list[list[tuple[str, str]]]]] = {
            "/apps": (
                "Choose an application to open on this computer.",
                [
                    [("Chrome", "open_chrome"), ("VS Code", "open_vscode")],
                    [("Canva", "open_canva"), ("YouTube", "open_youtube")],
                    [("Edge", "open_edge"), ("Firefox", "open_firefox")],
                    [("Notepad", "open_notepad"), ("Calculator", "open_calculator")],
                    [("Explorer", "open_explorer")],
                ],
            ),
            "/media": (
                "Choose a media control.",
                [
                    [("Play music", "play_music"), ("Pause / resume", "media_pause")],
                    [("Previous", "media_previous"), ("Next", "media_next")],
                    [("Volume 30%", "volume_30"), ("Volume 50%", "volume_50"), ("Volume 70%", "volume_70")],
                    [("Mute", "media_mute")],
                ],
            ),
            "/camera": (
                "The camera stays local; Telegram receives no image or video.",
                [[("Open camera", "camera_open"), ("Close camera", "camera_close")]],
            ),
            "/files": (
                "Open a local folder or use /search followed by a filename.",
                [
                    [("Documents", "open_documents"), ("Downloads", "open_downloads")],
                    [("Reminders", "show_reminders"), ("Priorities", "show_priority")],
                    [("MARLIN status", "show_status")],
                ],
            ),
        }
        selected = menus.get(command)
        if selected is None:
            return None
        title, rows = selected
        return title, {
            "inline_keyboard": [
                [
                    {"text": label, "callback_data": f"remote:{action}"}
                    for label, action in row
                ]
                for row in rows
            ]
        }

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(exc, TelegramError):
            return str(exc)[:300]
        if isinstance(exc, ValueError):
            return str(exc)[:300]
        return "Telegram integration encountered an internal error."
