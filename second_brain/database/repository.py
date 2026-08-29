"""Repositories for entities and relationships."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from second_brain.database.connection import connect
from second_brain.database.models import Entity, Relationship, utc_now_iso


class EntityRepository:
    """CRUD access for knowledge entities."""

    def __init__(self, database_path: Path):
        self.database_path = database_path

    def create(
        self,
        entity_type: str,
        name: str,
        *,
        entity_id: str | None = None,
        source: str = "manual",
        metadata: dict[str, Any] | None = None,
    ) -> Entity:
        now = utc_now_iso()
        entity = Entity(
            id=entity_id or uuid.uuid4().hex,
            type=entity_type,
            name=name,
            source=source,
            metadata=metadata or {},
            created_at=now,
            modified_at=now,
        )
        with connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO entities
                    (id, type, name, source, metadata_json, created_at, modified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entity.id,
                    entity.type,
                    entity.name,
                    entity.source,
                    json.dumps(entity.metadata, sort_keys=True),
                    entity.created_at,
                    entity.modified_at,
                ),
            )
        return entity

    def get(self, entity_id: str) -> Entity | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?",
                (entity_id,),
            ).fetchone()
        return _entity_from_row(row) if row else None

    def get_many(self, entity_ids: list[str]) -> list[Entity]:
        if not entity_ids:
            return []
        placeholders = ",".join("?" for _ in entity_ids)
        with connect(self.database_path) as connection:
            rows = connection.execute(
                f"SELECT * FROM entities WHERE id IN ({placeholders})",
                entity_ids,
            ).fetchall()
        by_id = {_entity_from_row(row).id: _entity_from_row(row) for row in rows}
        return [by_id[entity_id] for entity_id in entity_ids if entity_id in by_id]

    def list(self, entity_type: str | None = None) -> list[Entity]:
        with connect(self.database_path) as connection:
            if entity_type:
                rows = connection.execute(
                    "SELECT * FROM entities WHERE type = ? ORDER BY type, name",
                    (entity_type,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM entities ORDER BY type, name",
                ).fetchall()
        return [_entity_from_row(row) for row in rows]

    def list_limited(self, limit: int, entity_type: str | None = None) -> list[Entity]:
        with connect(self.database_path) as connection:
            if entity_type:
                rows = connection.execute(
                    "SELECT * FROM entities WHERE type = ? ORDER BY type, name LIMIT ?",
                    (entity_type, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM entities ORDER BY type, name LIMIT ?",
                    (limit,),
                ).fetchall()
        return [_entity_from_row(row) for row in rows]

    def count(self, entity_type: str | None = None) -> int:
        with connect(self.database_path) as connection:
            if entity_type:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM entities WHERE type = ?",
                    (entity_type,),
                ).fetchone()
            else:
                row = connection.execute("SELECT COUNT(*) AS total FROM entities").fetchone()
        return int(row["total"])

    def delete_by_source(self, source: str) -> int:
        with connect(self.database_path) as connection:
            cursor = connection.execute(
                "DELETE FROM entities WHERE source = ?",
                (source,),
            )
            return cursor.rowcount


class RelationshipRepository:
    """CRUD access for graph relationships."""

    def __init__(self, database_path: Path):
        self.database_path = database_path

    def create(
        self,
        source_id: str,
        relationship_type: str,
        target_id: str,
        *,
        relationship_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Relationship:
        now = utc_now_iso()
        relationship = Relationship(
            id=relationship_id or uuid.uuid4().hex,
            source_id=source_id,
            target_id=target_id,
            type=relationship_type,
            metadata=metadata or {},
            created_at=now,
            modified_at=now,
        )
        with connect(self.database_path) as connection:
            try:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO relationships
                        (id, source_id, target_id, type, metadata_json, created_at, modified_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        relationship.id,
                        relationship.source_id,
                        relationship.target_id,
                        relationship.type,
                        json.dumps(relationship.metadata, sort_keys=True),
                        relationship.created_at,
                        relationship.modified_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Relationship endpoints must exist before linking them.") from exc
        return relationship

    def list(self, relationship_type: str | None = None) -> list[Relationship]:
        with connect(self.database_path) as connection:
            if relationship_type:
                rows = connection.execute(
                    "SELECT * FROM relationships WHERE type = ? ORDER BY type, source_id",
                    (relationship_type,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM relationships ORDER BY type, source_id",
                ).fetchall()
        return [_relationship_from_row(row) for row in rows]

    def list_limited(self, limit: int, relationship_type: str | None = None) -> list[Relationship]:
        with connect(self.database_path) as connection:
            if relationship_type:
                rows = connection.execute(
                    """
                    SELECT * FROM relationships
                    WHERE type = ?
                    ORDER BY type, source_id
                    LIMIT ?
                    """,
                    (relationship_type, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM relationships ORDER BY type, source_id LIMIT ?",
                    (limit,),
                ).fetchall()
        return [_relationship_from_row(row) for row in rows]

    def count(self, relationship_type: str | None = None) -> int:
        with connect(self.database_path) as connection:
            if relationship_type:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM relationships WHERE type = ?",
                    (relationship_type,),
                ).fetchone()
            else:
                row = connection.execute("SELECT COUNT(*) AS total FROM relationships").fetchone()
        return int(row["total"])

    def for_source(self, source_id: str, relationship_type: str | None = None) -> list[Relationship]:
        with connect(self.database_path) as connection:
            if relationship_type:
                rows = connection.execute(
                    """
                    SELECT * FROM relationships
                    WHERE source_id = ? AND type = ?
                    ORDER BY target_id
                    """,
                    (source_id, relationship_type),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM relationships WHERE source_id = ? ORDER BY type, target_id",
                    (source_id,),
                ).fetchall()
        return [_relationship_from_row(row) for row in rows]


def _entity_from_row(row: sqlite3.Row) -> Entity:
    return Entity(
        id=row["id"],
        type=row["type"],
        name=row["name"],
        source=row["source"],
        metadata=json.loads(row["metadata_json"]),
        created_at=row["created_at"],
        modified_at=row["modified_at"],
    )


def _relationship_from_row(row: sqlite3.Row) -> Relationship:
    return Relationship(
        id=row["id"],
        source_id=row["source_id"],
        target_id=row["target_id"],
        type=row["type"],
        metadata=json.loads(row["metadata_json"]),
        created_at=row["created_at"],
        modified_at=row["modified_at"],
    )
