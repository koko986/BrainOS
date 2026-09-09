"""MARLIN V2 SQLite migrations and persistent assistant memory."""

from __future__ import annotations

import json
import hashlib
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 6

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
CREATE TABLE IF NOT EXISTS schedule_items (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'task',
    start_at TEXT,
    end_at TEXT,
    duration_minutes INTEGER NOT NULL DEFAULT 60,
    deadline_at TEXT,
    fixed INTEGER NOT NULL DEFAULT 0,
    recurrence TEXT NOT NULL DEFAULT 'none',
    priority INTEGER NOT NULL DEFAULT 50,
    status TEXT NOT NULL DEFAULT 'pending',
    project_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_plans (
    id TEXT PRIMARY KEY,
    plan_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'preview',
    explanation_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    applied_at TEXT
);
CREATE TABLE IF NOT EXISTS schedule_blocks (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    title TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    explanation_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'preview',
    FOREIGN KEY(plan_id) REFERENCES schedule_plans(id) ON DELETE CASCADE,
    FOREIGN KEY(item_id) REFERENCES schedule_items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS preferences (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence TEXT NOT NULL,
    observations INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(category, value)
);
CREATE TABLE IF NOT EXISTS research_runs (
    id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_sources (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    domain TEXT NOT NULL,
    snippet TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    accessed_at TEXT NOT NULL,
    saved INTEGER NOT NULL DEFAULT 0,
    prolog_score INTEGER,
    FOREIGN KEY(run_id) REFERENCES research_runs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS file_insights (
    entity_id TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    concepts_json TEXT NOT NULL DEFAULT '[]',
    imports_json TEXT NOT NULL DEFAULT '[]',
    technologies_json TEXT NOT NULL DEFAULT '[]',
    analyzed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_identities (
    user_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('owner', 'contact', 'pending')),
    alias TEXT COLLATE NOCASE UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_pair_codes (
    code TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_message_drafts (
    id TEXT PRIMARY KEY,
    recipient_user_id INTEGER NOT NULL,
    recipient_alias TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    expires_at TEXT NOT NULL,
    telegram_message_id INTEGER,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY(recipient_user_id) REFERENCES telegram_identities(user_id)
);
CREATE TABLE IF NOT EXISTS telegram_deliveries (
    id TEXT PRIMARY KEY,
    dedupe_key TEXT UNIQUE,
    kind TEXT NOT NULL,
    recipient_user_id INTEGER,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    telegram_message_id INTEGER,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_alarms_due ON alarms(enabled, due_at);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(completed, due_at);
CREATE INDEX IF NOT EXISTS idx_context_kind ON recent_context(kind, id DESC);
CREATE INDEX IF NOT EXISTS idx_actions_created ON action_history(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_schedule_items_due ON schedule_items(status, deadline_at);
CREATE INDEX IF NOT EXISTS idx_schedule_blocks_time ON schedule_blocks(start_at, end_at);
CREATE INDEX IF NOT EXISTS idx_preferences_active ON preferences(active, category);
CREATE INDEX IF NOT EXISTS idx_research_sources_run ON research_sources(run_id, rank);
CREATE INDEX IF NOT EXISTS idx_telegram_identity_role ON telegram_identities(role, active);
CREATE INDEX IF NOT EXISTS idx_telegram_drafts_status ON telegram_message_drafts(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_telegram_delivery_created ON telegram_deliveries(created_at DESC);
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
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(reminders)')}
            if 'notified_at' not in columns:
                connection.execute('ALTER TABLE reminders ADD COLUMN notified_at TEXT')
            research_columns = {row['name'] for row in connection.execute('PRAGMA table_info(research_sources)')}
            if 'prolog_score' not in research_columns:
                connection.execute('ALTER TABLE research_sources ADD COLUMN prolog_score INTEGER')
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

    def claim_due_alarms(self) -> list[dict[str, Any]]:
        stamp = now_iso()
        with self.connect() as connection:
            rows = connection.execute(
                "UPDATE alarms SET enabled=0,fired_at=? WHERE enabled=1 AND due_at<=? RETURNING *",
                (stamp, stamp),
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

    def update_reminder(self, reminder_id: str, *, minutes: int | None = None) -> dict[str, Any] | None:
        if minutes is not None and not 1 <= minutes <= 1440:
            raise ValueError('Snooze must be between 1 and 1440 minutes.')
        with self.connect() as connection:
            if minutes is None:
                cursor = connection.execute('UPDATE reminders SET completed=1 WHERE id=?', (reminder_id,))
            else:
                due = (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat(timespec='seconds')
                cursor = connection.execute('UPDATE reminders SET completed=0, due_at=?, notified_at=NULL WHERE id=?', (due, reminder_id))
            if not cursor.rowcount:
                return None
            return dict(connection.execute('SELECT * FROM reminders WHERE id=?', (reminder_id,)).fetchone())

    def claim_due_reminders(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                'UPDATE reminders SET notified_at=? WHERE completed=0 AND notified_at IS NULL AND due_at <= ? RETURNING *',
                (now_iso(), now_iso()),
            ).fetchall()
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

    def add_schedule_item(
        self,
        title: str,
        *,
        kind: str = "task",
        start_at: str | None = None,
        end_at: str | None = None,
        duration_minutes: int = 60,
        deadline_at: str | None = None,
        fixed: bool = False,
        recurrence: str = "none",
        priority: int = 50,
        project_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        item_id, stamp = uuid.uuid4().hex, now_iso()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO schedule_items
                   (id,title,kind,start_at,end_at,duration_minutes,deadline_at,fixed,recurrence,priority,status,project_id,metadata_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)""",
                (item_id, title, kind, start_at, end_at, max(30, duration_minutes), deadline_at,
                 int(fixed), recurrence, max(0, min(100, priority)), project_id,
                 json.dumps(metadata or {}, ensure_ascii=False), stamp, stamp),
            )
        return self.get_schedule_item(item_id) or {}

    def get_schedule_item(self, item_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM schedule_items WHERE id=?", (item_id,)).fetchone()
        return self._json_row(row, "metadata_json", "metadata") if row else None

    def list_schedule_items(self, *, include_completed: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM schedule_items"
        args: tuple[Any, ...] = ()
        if not include_completed:
            sql += " WHERE status <> ?"
            args = ("completed",)
        sql += " ORDER BY start_at IS NULL, start_at, deadline_at IS NULL, deadline_at, priority DESC"
        with self.connect() as connection:
            rows = connection.execute(sql, args).fetchall()
        return [self._json_row(row, "metadata_json", "metadata") for row in rows]

    def complete_schedule_item(self, item_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE schedule_items SET status='completed',updated_at=? WHERE id=?",
                (now_iso(), item_id),
            )
            if not cursor.rowcount:
                return None
        return self.get_schedule_item(item_id)

    def create_schedule_plan(
        self,
        plan_date: str,
        blocks: list[dict[str, Any]],
        explanation: dict[str, Any],
    ) -> dict[str, Any]:
        plan_id, stamp = uuid.uuid4().hex, now_iso()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO schedule_plans(id,plan_date,status,explanation_json,created_at) VALUES(?,?,'preview',?,?)",
                (plan_id, plan_date, json.dumps(explanation, ensure_ascii=False), stamp),
            )
            for block in blocks:
                connection.execute(
                    """INSERT INTO schedule_blocks
                       (id,plan_id,item_id,title,start_at,end_at,explanation_json,status)
                       VALUES(?,?,?,?,?,?,?,'preview')""",
                    (uuid.uuid4().hex, plan_id, block["item_id"], block["title"], block["start_at"],
                     block["end_at"], json.dumps(block.get("explanation", {}), ensure_ascii=False)),
                )
        return self.get_schedule_plan(plan_id) or {}

    def get_schedule_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            plan = connection.execute("SELECT * FROM schedule_plans WHERE id=?", (plan_id,)).fetchone()
            if not plan:
                return None
            blocks = connection.execute(
                "SELECT * FROM schedule_blocks WHERE plan_id=? ORDER BY start_at", (plan_id,)
            ).fetchall()
        result = self._json_row(plan, "explanation_json", "explanation")
        result["blocks"] = [self._json_row(row, "explanation_json", "explanation") for row in blocks]
        return result

    def latest_schedule_plan(self, *, status: str | None = None) -> dict[str, Any] | None:
        with self.connect() as connection:
            if status:
                row = connection.execute(
                    "SELECT id FROM schedule_plans WHERE status=? ORDER BY created_at DESC LIMIT 1", (status,)
                ).fetchone()
            else:
                row = connection.execute("SELECT id FROM schedule_plans ORDER BY created_at DESC LIMIT 1").fetchone()
        return self.get_schedule_plan(str(row["id"])) if row else None

    def apply_schedule_plan(self, plan_id: str) -> dict[str, Any] | None:
        stamp = now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE schedule_plans SET status='applied',applied_at=? WHERE id=? AND status='preview'",
                (stamp, plan_id),
            )
            if not cursor.rowcount:
                return None
            connection.execute("UPDATE schedule_blocks SET status='applied' WHERE plan_id=?", (plan_id,))
        return self.get_schedule_plan(plan_id)

    def schedule_blocks_for_date(self, date_prefix: str, *, applied_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM schedule_blocks WHERE start_at LIKE ?"
        args: list[Any] = [f"{date_prefix}%"]
        if applied_only:
            sql += " AND status='applied'"
        sql += " ORDER BY start_at"
        with self.connect() as connection:
            rows = connection.execute(sql, args).fetchall()
        return [self._json_row(row, "explanation_json", "explanation") for row in rows]

    def remember_preference(self, category: str, value: str, evidence: str, confidence: float) -> dict[str, Any]:
        stamp = now_iso()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM preferences WHERE category=? AND lower(value)=lower(?)", (category, value)
            ).fetchone()
            if existing:
                observations = int(existing["observations"]) + 1
                active = int(confidence >= .9 or observations >= 3)
                connection.execute(
                    """UPDATE preferences SET confidence=max(confidence,?),evidence=?,observations=?,active=?,updated_at=?
                       WHERE id=?""",
                    (confidence, evidence[:500], observations, active, stamp, existing["id"]),
                )
                preference_id = str(existing["id"])
            else:
                preference_id = uuid.uuid4().hex
                connection.execute(
                    """INSERT INTO preferences(id,category,value,confidence,evidence,observations,active,created_at,updated_at)
                       VALUES(?,?,?,?,?,1,?,?,?)""",
                    (preference_id, category, value, confidence, evidence[:500], int(confidence >= .9), stamp, stamp),
                )
        return self.get_preference(preference_id) or {}

    def get_preference(self, preference_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM preferences WHERE id=?", (preference_id,)).fetchone()
        return dict(row) if row else None

    def list_preferences(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM preferences" + (" WHERE active=1" if active_only else "") + " ORDER BY active DESC,updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def forget_preference(self, query: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE preferences SET active=0,updated_at=? WHERE id=? OR lower(value) LIKE lower(?)",
                (now_iso(), query, f"%{query}%"),
            )
        return cursor.rowcount

    def add_research(self, query: str, sources: list[dict[str, Any]], summary: str = "") -> dict[str, Any]:
        run_id, stamp = uuid.uuid4().hex, now_iso()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO research_runs(id,query,summary,created_at) VALUES(?,?,?,?)",
                (run_id, query, summary[:4000], stamp),
            )
            for rank, source in enumerate(sources, 1):
                connection.execute(
                    """INSERT INTO research_sources(id,run_id,rank,title,url,domain,snippet,note,accessed_at,saved)
                       VALUES(?,?,?,?,?,?,?,?,?,0)""",
                    (uuid.uuid4().hex, run_id, rank, source.get("title", "Untitled")[:500],
                     source.get("url", "")[:2000], source.get("domain", "")[:255],
                     source.get("snippet", "")[:2000], source.get("note", "")[:1000], stamp),
                )
        return self.get_research(run_id) or {}

    def get_research(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            run = connection.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                return None
            sources = connection.execute(
                "SELECT * FROM research_sources WHERE run_id=? ORDER BY rank", (run_id,)
            ).fetchall()
        result = dict(run)
        result["sources"] = [dict(row) for row in sources]
        return result

    def recent_research(self, limit: int = 5) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT id FROM research_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [item for row in rows if (item := self.get_research(str(row["id"]))) is not None]

    def save_research_source(self, source_id: str, note: str = "") -> dict[str, Any] | None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE research_sources SET saved=1,note=CASE WHEN ?='' THEN note ELSE ? END WHERE id=?",
                (note, note[:1000], source_id),
            )
            if not cursor.rowcount:
                return None
            row = connection.execute("SELECT * FROM research_sources WHERE id=?", (source_id,)).fetchone()
        return dict(row) if row else None

    def get_research_source(self, source_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM research_sources WHERE id=?", (source_id,)).fetchone()
        return dict(row) if row else None

    def set_research_source_score(self, source_id: str, score: int) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE research_sources SET prolog_score=? WHERE id=?", (score, source_id))

    def create_telegram_pair_code(self, *, minutes: int = 10) -> dict[str, Any]:
        created = datetime.now(UTC)
        expires = created + timedelta(minutes=minutes)
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM telegram_pair_codes WHERE used_at IS NOT NULL OR expires_at <= ?",
                (created.isoformat(timespec="seconds"),),
            )
            for _ in range(20):
                code = f"{secrets.randbelow(1_000_000):06d}"
                try:
                    connection.execute(
                        "INSERT INTO telegram_pair_codes(code,expires_at,created_at) VALUES(?,?,?)",
                        (code, expires.isoformat(timespec="seconds"), created.isoformat(timespec="seconds")),
                    )
                    return {"code": code, "expires_at": expires.isoformat(timespec="seconds")}
                except sqlite3.IntegrityError:
                    continue
        raise RuntimeError("Could not generate a unique Telegram pairing code.")

    def pair_telegram_owner(
        self, code: str, *, user_id: int, chat_id: int, display_name: str
    ) -> dict[str, Any]:
        stamp = now_iso()
        with self.connect() as connection:
            pair = connection.execute(
                "SELECT * FROM telegram_pair_codes WHERE code=? AND used_at IS NULL AND expires_at>?",
                (str(code).strip(), stamp),
            ).fetchone()
            if not pair:
                raise ValueError("That pairing code is invalid or expired.")
            owner = connection.execute(
                "SELECT * FROM telegram_identities WHERE role='owner' AND active=1"
            ).fetchone()
            if owner and int(owner["user_id"]) != int(user_id):
                raise ValueError("MARLIN already has a paired Telegram owner.")
            connection.execute(
                "UPDATE telegram_identities SET role='pending',active=0,updated_at=? "
                "WHERE role='owner' AND user_id<>?",
                (stamp, int(user_id)),
            )
            connection.execute(
                """INSERT INTO telegram_identities(user_id,chat_id,role,alias,display_name,active,created_at,updated_at)
                   VALUES(?,?,'owner','owner',?,1,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,role='owner',alias='owner',
                   display_name=excluded.display_name,active=1,updated_at=excluded.updated_at""",
                (int(user_id), int(chat_id), display_name[:200], stamp, stamp),
            )
            connection.execute("UPDATE telegram_pair_codes SET used_at=? WHERE code=?", (stamp, code))
            row = connection.execute(
                "SELECT * FROM telegram_identities WHERE user_id=?", (int(user_id),)
            ).fetchone()
        return dict(row)

    def telegram_owner(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM telegram_identities WHERE role='owner' AND active=1 LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def request_telegram_contact(
        self, *, user_id: int, chat_id: int, display_name: str
    ) -> dict[str, Any]:
        stamp = now_iso()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM telegram_identities WHERE user_id=?", (int(user_id),)
            ).fetchone()
            if existing and existing["role"] in {"owner", "contact"}:
                return dict(existing)
            connection.execute(
                """INSERT INTO telegram_identities(user_id,chat_id,role,display_name,active,created_at,updated_at)
                   VALUES(?,?,'pending',?,0,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,display_name=excluded.display_name,
                   updated_at=excluded.updated_at""",
                (int(user_id), int(chat_id), display_name[:200], stamp, stamp),
            )
            row = connection.execute(
                "SELECT * FROM telegram_identities WHERE user_id=?", (int(user_id),)
            ).fetchone()
        return dict(row)

    def approve_telegram_contact(self, user_id: int, alias: str) -> dict[str, Any] | None:
        normalized = " ".join(str(alias).strip().split())
        if not normalized or len(normalized) > 80:
            raise ValueError("Contact alias must be between 1 and 80 characters.")
        stamp = now_iso()
        with self.connect() as connection:
            try:
                cursor = connection.execute(
                    "UPDATE telegram_identities SET role='contact',alias=?,active=1,updated_at=? "
                    "WHERE user_id=? AND role='pending'",
                    (normalized, stamp, int(user_id)),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("That Telegram contact alias is already in use.") from exc
            if not cursor.rowcount:
                return None
            row = connection.execute(
                "SELECT * FROM telegram_identities WHERE user_id=?", (int(user_id),)
            ).fetchone()
        return dict(row)

    def reject_telegram_contact(self, user_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM telegram_identities WHERE user_id=? AND role='pending'", (int(user_id),)
            )
        return bool(cursor.rowcount)

    def revoke_telegram_contact(self, user_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE telegram_identities SET active=0,updated_at=? WHERE user_id=? AND role='contact'",
                (now_iso(), int(user_id)),
            )
        return bool(cursor.rowcount)

    def telegram_contacts(self, *, include_pending: bool = True) -> list[dict[str, Any]]:
        clause = "WHERE role IN ('contact','pending')" if include_pending else "WHERE role='contact' AND active=1"
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM telegram_identities {clause} ORDER BY role,alias,display_name"
            ).fetchall()
        return [dict(row) for row in rows]

    def telegram_contact_by_alias(self, alias: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM telegram_identities WHERE role='contact' AND active=1 AND alias=? COLLATE NOCASE",
                (str(alias).strip(),),
            ).fetchone()
        return dict(row) if row else None

    def create_telegram_draft(self, alias: str, content: str, *, minutes: int = 2) -> dict[str, Any]:
        target = self.telegram_contact_by_alias(alias)
        message = str(content).strip()
        if target is None:
            raise ValueError(f"No active Telegram contact is named {alias}.")
        if not message or len(message) > 4096:
            raise ValueError("Telegram messages must contain 1 to 4096 characters.")
        draft_id = uuid.uuid4().hex
        digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
        expires = datetime.now(UTC) + timedelta(minutes=minutes)
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO telegram_message_drafts
                   (id,recipient_user_id,recipient_alias,content,content_hash,status,expires_at,created_at)
                   VALUES(?,?,?,?,?,'pending',?,?)""",
                (draft_id, target["user_id"], target["alias"], message, digest,
                 expires.isoformat(timespec="seconds"), now_iso()),
            )
        return self.get_telegram_draft(draft_id) or {}

    def get_telegram_draft(self, draft_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM telegram_message_drafts WHERE id=?", (draft_id,)
            ).fetchone()
        return dict(row) if row else None

    def claim_telegram_draft(self, draft_id: str) -> dict[str, Any]:
        stamp = now_iso()
        with self.connect() as connection:
            row = connection.execute(
                """SELECT d.*,i.chat_id,i.active AS recipient_active
                   FROM telegram_message_drafts d JOIN telegram_identities i ON i.user_id=d.recipient_user_id
                   WHERE d.id=?""",
                (draft_id,),
            ).fetchone()
            if not row or row["status"] != "pending":
                raise ValueError("That message draft is missing or was already used.")
            if row["expires_at"] <= stamp:
                connection.execute(
                    "UPDATE telegram_message_drafts SET status='expired',resolved_at=? WHERE id=?",
                    (stamp, draft_id),
                )
                raise ValueError("That message draft expired. Please create it again.")
            if not row["recipient_active"]:
                raise ValueError("That Telegram contact is no longer active.")
            digest = hashlib.sha256(str(row["content"]).encode("utf-8")).hexdigest()
            if not secrets.compare_digest(digest, str(row["content_hash"])):
                raise ValueError("The message draft changed after preview and was blocked.")
            cursor = connection.execute(
                "UPDATE telegram_message_drafts SET status='sending' WHERE id=? AND status='pending'",
                (draft_id,),
            )
            if not cursor.rowcount:
                raise ValueError("That message draft was already used.")
        return dict(row)

    def finish_telegram_draft(
        self, draft_id: str, *, status: str, message_id: int | None = None, error: str = ""
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE telegram_message_drafts SET status=?,telegram_message_id=?,error=?,resolved_at=? WHERE id=?",
                (status, message_id, error[:500], now_iso(), draft_id),
            )
        return self.get_telegram_draft(draft_id)

    def cancel_telegram_draft(self, draft_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE telegram_message_drafts SET status='cancelled',resolved_at=? WHERE id=? AND status='pending'",
                (now_iso(), draft_id),
            )
        return self.get_telegram_draft(draft_id) if cursor.rowcount else None

    def telegram_drafts(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM telegram_message_drafts ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def record_telegram_delivery(
        self, *, kind: str, content: str, recipient_user_id: int | None,
        status: str, dedupe_key: str | None = None, message_id: int | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        item = {
            "id": uuid.uuid4().hex, "dedupe_key": dedupe_key, "kind": kind,
            "recipient_user_id": recipient_user_id, "content": content[:4096], "status": status,
            "telegram_message_id": message_id, "error": error[:500], "created_at": now_iso(),
            "sent_at": now_iso() if status == "sent" else None,
        }
        with self.connect() as connection:
            if dedupe_key:
                connection.execute(
                    """INSERT INTO telegram_deliveries
                       (id,dedupe_key,kind,recipient_user_id,content,status,telegram_message_id,error,created_at,sent_at)
                       VALUES(:id,:dedupe_key,:kind,:recipient_user_id,:content,:status,:telegram_message_id,:error,:created_at,:sent_at)
                       ON CONFLICT(dedupe_key) DO UPDATE SET
                         kind=excluded.kind,
                         recipient_user_id=excluded.recipient_user_id,
                         content=excluded.content,
                         status=excluded.status,
                         telegram_message_id=excluded.telegram_message_id,
                         error=excluded.error,
                         sent_at=excluded.sent_at""",
                    item,
                )
            else:
                connection.execute(
                    """INSERT INTO telegram_deliveries
                       (id,dedupe_key,kind,recipient_user_id,content,status,telegram_message_id,error,created_at,sent_at)
                       VALUES(:id,:dedupe_key,:kind,:recipient_user_id,:content,:status,:telegram_message_id,:error,:created_at,:sent_at)""",
                    item,
                )
            if dedupe_key:
                row = connection.execute(
                    "SELECT * FROM telegram_deliveries WHERE dedupe_key=?", (dedupe_key,)
                ).fetchone()
                return dict(row)
        return item

    def telegram_delivery_exists(self, dedupe_key: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM telegram_deliveries WHERE dedupe_key=? AND status='sent'", (dedupe_key,)
            ).fetchone()
        return bool(row)

    def telegram_deliveries(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM telegram_deliveries ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_file_insight(
        self,
        entity_id: str,
        checksum: str,
        summary: str,
        concepts: list[str],
        imports: list[str],
        technologies: list[str],
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO file_insights
                   (entity_id,checksum,summary,concepts_json,imports_json,technologies_json,analyzed_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (entity_id, checksum, summary[:2000], json.dumps(concepts[:40]), json.dumps(imports[:80]),
                 json.dumps(technologies[:30]), now_iso()),
            )
            row = connection.execute("SELECT * FROM file_insights WHERE entity_id=?", (entity_id,)).fetchone()
        return self._insight_row(row)

    def get_file_insight(self, entity_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM file_insights WHERE entity_id=?", (entity_id,)).fetchone()
        return self._insight_row(row) if row else None

    @staticmethod
    def _json_row(row: sqlite3.Row, source_key: str, target_key: str) -> dict[str, Any]:
        item = dict(row)
        try:
            item[target_key] = json.loads(item.pop(source_key) or "{}")
        except json.JSONDecodeError:
            item[target_key] = {}
        return item

    @staticmethod
    def _insight_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for source, target in (("concepts_json", "concepts"), ("imports_json", "imports"), ("technologies_json", "technologies")):
            try:
                item[target] = json.loads(item.pop(source) or "[]")
            except json.JSONDecodeError:
                item[target] = []
        return item
