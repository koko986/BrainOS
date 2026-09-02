"""MARLIN V2 SQLite migrations and persistent assistant memory."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 2

V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS marlin_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS alarms (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    due_at TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    fired_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    due_at TEXT,
    completed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recent_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_history (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS indexing_checkpoints (
    root TEXT PRIMARY KEY,
    cursor TEXT NOT NULL DEFAULT '',
    indexed_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assistant_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS file_search (
    entity_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    snippet TEXT NOT NULL DEFAULT '',
    modified_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_alarms_due ON alarms(enabled, due_at);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(completed, due_at);
CREATE INDEX IF NOT EXISTS idx_context_kind ON recent_context(kind, id DESC);
CREATE INDEX IF NOT EXISTS idx_actions_created ON action_history(created_at DESC);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class MarlinStore:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_path: Path | None = None

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def migrate(self, *, backup: bool = True) -> Path | None:
        current = self.schema_version()
        if current >= SCHEMA_VERSION:
            self._ensure_fts()
            return None
        if backup and self.database_path.exists() and self.database_path.stat().st_size:
            self.backup_path = self._backup_database()
        with self.connect() as connection:
            connection.executescript(V2_SCHEMA)
            connection.execute(
                "INSERT OR REPLACE INTO marlin_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        self._ensure_fts()
        self.backfill_file_search()
        return self.backup_path

    def schema_version(self) -> int:
        if not self.database_path.exists():
            return 0
        try:
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT value FROM marlin_meta WHERE key = 'schema_version'"
                ).fetchone()
        except sqlite3.OperationalError:
            return 0
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    def _backup_database(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.database_path.with_name(f"{self.database_path.stem}.pre-v2-{stamp}.db")
        source = sqlite3.connect(self.database_path)
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        return target

    def _ensure_fts(self) -> None:
        with self.connect() as connection:
            try:
                connection.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS file_search_fts USING fts5(entity_id UNINDEXED, path, name, snippet)"
                )
            except sqlite3.OperationalError:
                connection.execute(
                    "INSERT OR REPLACE INTO marlin_meta(key, value) VALUES('fts5', 'unavailable')"
                )

    def backfill_file_search(self) -> None:
        with self.connect() as connection:
            try:
                rows = connection.execute(
                    "SELECT id, name, metadata_json, modified_at FROM entities WHERE type = 'file'"
                ).fetchall()
            except sqlite3.OperationalError:
                return
            for row in rows:
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except json.JSONDecodeError:
                    metadata = {}
                self._upsert_file_search_connection(
                    connection,
                    row["id"],
                    str(metadata.get("path", "")),
                    row["name"],
                    str(metadata.get("snippet", "")),
                    row["modified_at"],
                )

    def upsert_file_search(self, entity_id: str, path: str, name: str, snippet: str, modified_at: str) -> None:
        with self.connect() as connection:
            self._upsert_file_search_connection(connection, entity_id, path, name, snippet, modified_at)

    @staticmethod
    def _upsert_file_search_connection(
        connection: sqlite3.Connection,
        entity_id: str,
        path: str,
        name: str,
        snippet: str,
        modified_at: str,
    ) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO file_search(entity_id, path, name, snippet, modified_at) VALUES(?, ?, ?, ?, ?)",
            (entity_id, path, name, snippet, modified_at),
        )
        try:
            connection.execute("DELETE FROM file_search_fts WHERE entity_id = ?", (entity_id,))
            connection.execute(
                "INSERT INTO file_search_fts(entity_id, path, name, snippet) VALUES(?, ?, ?, ?)",
                (entity_id, path, name, snippet),
            )
        except sqlite3.OperationalError:
            pass

    def search_files(self, query: str, limit: int = 25) -> list[dict[str, Any]]:
        normalized = str(query or "").strip()
        if not normalized:
            return []
        with self.connect() as connection:
            try:
                rows = connection.execute(
                    "SELECT entity_id, path, name, snippet FROM file_search_fts WHERE file_search_fts MATCH ? LIMIT ?",
                    (self._fts_query(normalized), limit),
                ).fetchall()
            except sqlite3.OperationalError:
                like = f"%{normalized}%"
                rows = connection.execute(
                    "SELECT entity_id, path, name, snippet FROM file_search WHERE path LIKE ? OR name LIKE ? OR snippet LIKE ? LIMIT ?",
                    (like, like, like, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _fts_query(value: str) -> str:
        tokens = [token.replace('"', "") for token in value.split() if token.replace('"', "")]
        return " AND ".join(f'"{token}"*' for token in tokens) or '""'

    def ensure_conversation(self, conversation_id: str = "default") -> str:
        now = now_iso()
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO conversations(id, title, created_at, updated_at) VALUES(?, '', ?, ?)",
                (conversation_id, now, now),
            )
        return conversation_id

    def add_message(self, role: str, content: str, *, conversation_id: str = "default", metadata: dict[str, Any] | None = None) -> None:
        self.ensure_conversation(conversation_id)
        now = now_iso()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO messages(conversation_id, role, content, metadata_json, created_at) VALUES(?, ?, ?, ?, ?)",
                (conversation_id, role, content, json.dumps(metadata or {}, ensure_ascii=False), now),
            )
            connection.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))

    def recent_messages(self, limit: int = 12, *, conversation_id: str = "default") -> list[dict[str, str]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def add_context(self, kind: str, value: str, metadata: dict[str, Any] | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO recent_context(kind, value, metadata_json, created_at) VALUES(?, ?, ?, ?)",
                (kind, value, json.dumps(metadata or {}, ensure_ascii=False), now_iso()),
            )
            connection.execute(
                "DELETE FROM recent_context WHERE id NOT IN (SELECT id FROM recent_context ORDER BY id DESC LIMIT 200)"
            )

    def recent_context(self, kind: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if kind:
                rows = connection.execute(
                    "SELECT * FROM recent_context WHERE kind = ? ORDER BY id DESC LIMIT ?",
                    (kind, limit),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM recent_context ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            result.append(item)
        return result

    def set_state(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO assistant_state(key, value, updated_at) VALUES(?, ?, ?)",
                (key, value, now_iso()),
            )

    def get_state(self, key: str, default: str = "") -> str:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM assistant_state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def add_alarm(self, label: str, due_at: datetime) -> dict[str, Any]:
        alarm_id = uuid.uuid4().hex
        item = {
            "id": alarm_id,
            "label": label,
            "due_at": due_at.astimezone(UTC).isoformat(timespec="seconds"),
            "enabled": True,
        }
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO alarms(id, label, due_at, enabled, created_at) VALUES(?, ?, ?, 1, ?)",
                (alarm_id, label, item["due_at"], now_iso()),
            )
        return item

    def list_alarms(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if active_only:
                rows = connection.execute("SELECT * FROM alarms WHERE enabled = 1 ORDER BY due_at").fetchall()
            else:
                rows = connection.execute("SELECT * FROM alarms ORDER BY due_at DESC").fetchall()
        return [dict(row) for row in rows]

    def due_alarms(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM alarms WHERE enabled = 1 AND due_at <= ? ORDER BY due_at",
                (now_iso(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_alarm_fired(self, alarm_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE alarms SET enabled = 0, fired_at = ? WHERE id = ?",
                (now_iso(), alarm_id),
            )

    def snooze_alarm(self, alarm_id: str, minutes: int = 5) -> dict[str, Any] | None:
        due = datetime.now(UTC) + timedelta(minutes=max(1, minutes))
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE alarms SET enabled = 1, fired_at = NULL, due_at = ? WHERE id = ?",
                (due.isoformat(timespec="seconds"), alarm_id),
            )
        if not cursor.rowcount:
            return None
        return {"id": alarm_id, "due_at": due.isoformat(timespec="seconds")}

    def add_reminder(self, text: str, due_at: datetime | None = None) -> dict[str, Any]:
        reminder_id = uuid.uuid4().hex
        due = due_at.astimezone(UTC).isoformat(timespec="seconds") if due_at else None
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO reminders(id, text, due_at, created_at) VALUES(?, ?, ?, ?)",
                (reminder_id, text, due, now_iso()),
            )
        return {"id": reminder_id, "text": text, "due_at": due, "completed": False}

    def list_reminders(self, *, pending_only: bool = True) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if pending_only:
                rows = connection.execute("SELECT * FROM reminders WHERE completed = 0 ORDER BY due_at IS NULL, due_at, created_at").fetchall()
            else:
                rows = connection.execute("SELECT * FROM reminders ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def record_action(self, kind: str, label: str, target: str, status: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        item = {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "label": label,
            "target": target,
            "status": status,
            "details": details or {},
            "created_at": now_iso(),
        }
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO action_history(id, kind, label, target, status, details_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (item["id"], kind, label, target, status, json.dumps(item["details"], ensure_ascii=False), item["created_at"]),
            )
        return item

    def recent_actions(self, limit: int = 25) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM action_history ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]
