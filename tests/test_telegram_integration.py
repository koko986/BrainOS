from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import time
import pytest
from fastapi.testclient import TestClient

from marlin.config import MarlinSettings
from marlin.events import EventBus
from marlin.local_model import ModelTurn
from marlin.runtime import MarlinRuntime
from marlin.storage import MarlinStore
from marlin.telegram import TelegramBridge, TelegramError
from marlin.web import create_app


class FakeTelegram:
    def __init__(self):
        self.sent: list[dict] = []
        self.callbacks: list[tuple[str, str]] = []
        self.updates: list[dict] = []
        self.fail_send = False
        self.fail_get_me = 0

    def get_me(self):
        if self.fail_get_me:
            self.fail_get_me -= 1
            raise TelegramError("Telegram API request failed.")
        return {"id": 99, "username": "marlin_test_bot"}

    def get_updates(self, offset: int, timeout: int = 25):
        time.sleep(0.01)
        values = [item for item in self.updates if int(item["update_id"]) >= offset]
        self.updates = []
        return values

    def send_message(self, chat_id: int, text: str, reply_markup=None):
        if self.fail_send:
            raise TelegramError("Telegram API request failed.")
        item = {"chat_id": chat_id, "text": text, "reply_markup": reply_markup, "message_id": len(self.sent) + 1}
        self.sent.append(item)
        return item

    def answer_callback(self, callback_id: str, text: str = ""):
        self.callbacks.append((callback_id, text))


def settings(tmp_path: Path) -> MarlinSettings:
    return MarlinSettings(
        database_path=tmp_path / "brain.db",
        auto_index_c_drive=False,
        weather_enabled=False,
        voice_output=False,
        wake_word_enabled=False,
        telegram_enabled=True,
        telegram_bot_token="super-secret-bot-token",
        ollama_url="http://127.0.0.1:9",
    )


def bridge(tmp_path: Path):
    config = settings(tmp_path)
    store = MarlinStore(config.database_path)
    store.migrate(backup=False)
    fake = FakeTelegram()
    service = TelegramBridge(
        config, store, EventBus(), remote_command=lambda text: f"safe:{text}",
        briefing=lambda: "Morning briefing", transport=fake,
    )
    return service, store, fake


def pair_owner(store: MarlinStore, *, user_id: int = 1, chat_id: int = 10):
    code = store.create_telegram_pair_code()["code"]
    return store.pair_telegram_owner(code, user_id=user_id, chat_id=chat_id, display_name="Owner")


def add_contact(store: MarlinStore, *, user_id: int = 2, chat_id: int = 20, alias: str = "Alex"):
    store.request_telegram_contact(user_id=user_id, chat_id=chat_id, display_name=alias)
    return store.approve_telegram_contact(user_id, alias)


def private_message(user_id: int, chat_id: int, text: str, name: str = "User") -> dict:
    return {
        "from": {"id": user_id, "first_name": name},
        "chat": {"id": chat_id, "type": "private"},
        "text": text,
    }


def test_pairing_code_is_one_use_and_owner_is_unique(tmp_path):
    _, store, _ = bridge(tmp_path)
    code = store.create_telegram_pair_code()["code"]
    owner = store.pair_telegram_owner(code, user_id=1, chat_id=10, display_name="Owner")
    assert owner["role"] == "owner"
    with pytest.raises(ValueError, match="invalid or expired"):
        store.pair_telegram_owner(code, user_id=1, chat_id=10, display_name="Owner")
    other_code = store.create_telegram_pair_code()["code"]
    with pytest.raises(ValueError, match="already has"):
        store.pair_telegram_owner(other_code, user_id=2, chat_id=20, display_name="Other")


def test_expired_pairing_code_is_rejected(tmp_path):
    _, store, _ = bridge(tmp_path)
    code = store.create_telegram_pair_code()["code"]
    with store.connect() as connection:
        connection.execute(
            "UPDATE telegram_pair_codes SET expires_at=? WHERE code=?",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="seconds"), code),
        )
    with pytest.raises(ValueError, match="invalid or expired"):
        store.pair_telegram_owner(code, user_id=1, chat_id=10, display_name="Owner")


def test_unknown_user_becomes_pending_and_cannot_command(tmp_path):
    service, store, fake = bridge(tmp_path)
    pair_owner(store)
    service._handle_message(private_message(2, 20, "/status", "Alex"))
    pending = store.telegram_contacts()[0]
    assert pending["role"] == "pending"
    assert any("cannot issue commands" in item["text"] for item in fake.sent)
    assert any(item["chat_id"] == 10 and item["reply_markup"] for item in fake.sent)


def test_approved_contact_is_receive_only(tmp_path):
    service, store, fake = bridge(tmp_path)
    pair_owner(store)
    add_contact(store)
    service._handle_message(private_message(2, 20, "/priority", "Alex"))
    assert fake.sent[-1]["chat_id"] == 20
    assert "cannot command MARLIN" in fake.sent[-1]["text"]


def test_owner_remote_command_and_contact_naming(tmp_path):
    service, store, fake = bridge(tmp_path)
    pair_owner(store)
    store.request_telegram_contact(user_id=2, chat_id=20, display_name="Alexander")
    service._handle_message(private_message(1, 10, "/name 2 Alex", "Owner"))
    assert store.telegram_contact_by_alias("alex")["chat_id"] == 20
    service._handle_message(private_message(1, 10, "/status", "Owner"))
    assert fake.sent[-1]["text"] == "safe:/status"


def test_draft_requires_owner_and_can_only_send_once(tmp_path):
    service, store, fake = bridge(tmp_path)
    pair_owner(store)
    add_contact(store)
    draft = service.create_draft("alex", "I will arrive at eight.")
    assert store.get_telegram_draft(draft["id"])["status"] == "pending"
    with pytest.raises(ValueError, match="Only the paired"):
        service.approve_draft(draft["id"], owner_user_id=999)
    sent = service.approve_draft(draft["id"], owner_user_id=1)
    assert sent["status"] == "sent"
    assert any(item["chat_id"] == 20 and item["text"] == "I will arrive at eight." for item in fake.sent)
    with pytest.raises(ValueError, match="already used"):
        service.approve_draft(draft["id"], owner_user_id=1)


def test_expired_and_tampered_drafts_are_blocked(tmp_path):
    service, store, _ = bridge(tmp_path)
    pair_owner(store)
    add_contact(store)
    expired = service.create_draft("Alex", "Old draft")
    with store.connect() as connection:
        connection.execute(
            "UPDATE telegram_message_drafts SET expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="seconds"), expired["id"]),
        )
    with pytest.raises(ValueError, match="expired"):
        service.approve_draft(expired["id"], owner_user_id=1)
    altered = service.create_draft("Alex", "Original")
    with store.connect() as connection:
        connection.execute("UPDATE telegram_message_drafts SET content='Changed' WHERE id=?", (altered["id"],))
    with pytest.raises(ValueError, match="changed after preview"):
        service.approve_draft(altered["id"], owner_user_id=1)


def test_callbacks_validate_owner_and_cancel_draft(tmp_path):
    service, store, fake = bridge(tmp_path)
    pair_owner(store)
    add_contact(store)
    draft = service.create_draft("Alex", "Do not send")
    service._handle_callback({"id": "wrong", "from": {"id": 999}, "data": f"draft:send:{draft['id']}"})
    assert store.get_telegram_draft(draft["id"])["status"] == "pending"
    service._handle_callback({"id": "owner", "from": {"id": 1}, "data": f"draft:cancel:{draft['id']}"})
    assert store.get_telegram_draft(draft["id"])["status"] == "cancelled"
    assert "Cancelled" in fake.callbacks[-1][1]


def test_notification_delivery_is_deduplicated(tmp_path):
    service, store, fake = bridge(tmp_path)
    pair_owner(store)
    first = service._notify_owner("reminder", "Reminder: test", "reminder:1")
    second = service._notify_owner("reminder", "Reminder: test", "reminder:1")
    assert first["status"] == "sent"
    assert second["status"] == "duplicate"
    assert len(fake.sent) == 1


def test_failed_notification_can_retry_and_becomes_deduplicated(tmp_path):
    service, store, fake = bridge(tmp_path)
    pair_owner(store)
    fake.fail_send = True
    failed = service._notify_owner("reminder", "Reminder: retry", "reminder:retry")
    assert failed["status"] == "failed"
    fake.fail_send = False
    sent = service._notify_owner("reminder", "Reminder: retry", "reminder:retry")
    assert sent["status"] == "sent"
    assert service._notify_owner("reminder", "Reminder: retry", "reminder:retry")["status"] == "duplicate"
    rows = [item for item in store.telegram_deliveries() if item["dedupe_key"] == "reminder:retry"]
    assert len(rows) == 1
    assert rows[0]["status"] == "sent"


def test_send_failure_is_audited_without_leaking_secret(tmp_path):
    service, store, fake = bridge(tmp_path)
    pair_owner(store)
    add_contact(store)
    draft = service.create_draft("Alex", "Hello")
    fake.fail_send = True
    with pytest.raises(TelegramError):
        service.approve_draft(draft["id"], owner_user_id=1)
    failed = store.get_telegram_draft(draft["id"])
    assert failed["status"] == "failed"
    assert "super-secret-bot-token" not in str(service.status())
    assert "super-secret-bot-token" not in failed["error"]


def test_runtime_remote_surface_blocks_computer_tools_and_uses_toolless_chat(tmp_path, monkeypatch):
    runtime = MarlinRuntime(settings(tmp_path), start_background=False)
    assert "blocked remotely" in runtime.telegram_command("open chrome")
    assert "blocked remotely" in runtime.telegram_command("delete C:\\notes.txt")
    calls = []
    def chat(messages, **kwargs):
        calls.append(kwargs)
        return ModelTurn("Good day.", raw_message={"role": "assistant", "content": "Good day."})
    monkeypatch.setattr(runtime.model, "chat", chat)
    assert runtime.telegram_command("hello") == "Good day."
    assert calls[0]["tools"] is None
    runtime.shutdown()


def test_local_command_creates_contact_draft_instead_of_calling_model(tmp_path, monkeypatch):
    runtime = MarlinRuntime(settings(tmp_path), start_background=False)
    runtime.telegram.transport = FakeTelegram()
    pair_owner(runtime.store)
    add_contact(runtime.store)
    monkeypatch.setattr(runtime.model, "chat", lambda *args, **kwargs: pytest.fail("model should not run"))
    result = runtime.command("tell Alex I will arrive at eight")
    assert result["ok"]
    assert result["data"]["telegram_draft"]["status"] == "pending"
    assert "Send or Cancel" in result["message"]
    runtime.shutdown()


def test_remote_file_search_hides_sensitive_results(tmp_path, monkeypatch):
    runtime = MarlinRuntime(settings(tmp_path), start_background=False)
    monkeypatch.setattr(runtime.store, "search_files", lambda *_args, **_kwargs: [
        {"name": ".env", "path": r"C:\Projects\BrainOS\.env", "snippet": "TOKEN=secret"},
        {"name": "main.py", "path": r"C:\Projects\BrainOS\main.py", "snippet": "MARLIN entrypoint"},
    ])
    reply = runtime.telegram_command("/search marlin")
    assert "main.py" in reply
    assert ".env" not in reply
    assert "TOKEN=secret" not in reply
    runtime.shutdown()


def test_oversized_message_is_rejected_before_command_routing(tmp_path):
    service, store, fake = bridge(tmp_path)
    pair_owner(store)
    service._handle_message(private_message(1, 10, "x" * 4097, "Owner"))
    assert "1 to 4096" in fake.sent[-1]["text"]


def test_conversational_tell_me_is_not_mistaken_for_a_message_draft(tmp_path):
    service, _, _ = bridge(tmp_path)
    assert service.draft_parts("Tell me something interesting") is None
    assert service.draft_parts("tell Alex I will arrive at eight") == (
        "Alex", "I will arrive at eight",
    )


def test_telegram_web_api_requires_local_token_and_controls_draft(tmp_path):
    runtime = MarlinRuntime(settings(tmp_path), start_background=False)
    fake = FakeTelegram()
    runtime.telegram.transport = fake
    pair_owner(runtime.store)
    add_contact(runtime.store)
    app = create_app(runtime)
    client = TestClient(app)
    headers = {"X-Marlin-Token": app.state.token}
    assert client.get("/api/telegram/status").status_code == 403
    created = client.post(
        "/api/telegram/drafts", headers=headers, json={"alias": "Alex", "content": "Hello Alex"}
    )
    assert created.status_code == 200
    draft_id = created.json()["id"]
    approved = client.post(f"/api/telegram/drafts/{draft_id}/approve", headers=headers)
    assert approved.status_code == 200
    assert approved.json()["status"] == "sent"
    runtime.shutdown()


def test_status_and_cockpit_never_contain_bot_token(tmp_path):
    runtime = MarlinRuntime(settings(tmp_path), start_background=False)
    app = create_app(runtime)
    client = TestClient(app)
    state = client.get("/api/state").text
    page = client.get("/").text
    assert "super-secret-bot-token" not in state
    assert "super-secret-bot-token" not in page
    assert "toggleMessaging" in page
    runtime.shutdown()


def test_status_omits_chat_ids_and_draft_hashes(tmp_path):
    service, store, _ = bridge(tmp_path)
    pair_owner(store)
    add_contact(store)
    service.create_draft("Alex", "Preview")
    payload = service.status()
    assert "chat_id" not in str(payload)
    assert "content_hash" not in str(payload)


def test_timed_notifications_include_applied_schedule_blocks(tmp_path):
    service, store, fake = bridge(tmp_path)
    pair_owner(store)
    now = datetime.now(UTC)
    item = store.add_schedule_item("BrainOS work", duration_minutes=30)
    plan = store.create_schedule_plan(
        now.astimezone().date().isoformat(),
        [{
            "item_id": item["id"], "title": item["title"],
            "start_at": (now + timedelta(minutes=5)).isoformat(timespec="seconds"),
            "end_at": (now + timedelta(minutes=35)).isoformat(timespec="seconds"),
        }],
        {},
    )
    store.apply_schedule_plan(plan["id"])
    service.settings.telegram_briefing_time = "23:59"
    service._send_timed_notifications()
    assert any("BrainOS work" in item["text"] for item in fake.sent)


def test_polling_recovers_from_startup_failure_and_advances_bad_update(tmp_path):
    service, store, fake = bridge(tmp_path)
    pair_owner(store)
    fake.fail_get_me = 1
    fake.updates.append({
        "update_id": 7,
        "message": {"from": {"id": "invalid"}, "chat": {"id": 10, "type": "private"}, "text": "/status"},
    })
    assert service.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (
        store.get_state("telegram.update_offset", "0") != "8" or not service.status()["connected"]
    ):
        time.sleep(0.03)
    assert store.get_state("telegram.update_offset") == "8"
    assert service.status()["connected"]
    service.stop()


def test_disabled_bridge_does_not_start_or_create_pairing_code(tmp_path):
    config = settings(tmp_path)
    config.telegram_enabled = False
    store = MarlinStore(config.database_path)
    store.migrate(backup=False)
    service = TelegramBridge(
        config, store, EventBus(), remote_command=lambda text: text,
        briefing=lambda: "Briefing",
    )
    assert not service.start()
    with pytest.raises(TelegramError, match="Enable Telegram"):
        service.create_pair_code()
