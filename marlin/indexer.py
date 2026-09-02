"""Incremental metadata and snippet indexing for the MARLIN brain graph."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from marlin.storage import MarlinStore
from second_brain.knowledge.service import KnowledgeService


TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".json", ".csv", ".pl", ".html", ".css",
    ".toml", ".yaml", ".yml", ".ini", ".log", ".sql", ".java", ".c", ".cpp", ".h",
}
SKIP_NAMES = {
    ".agents", ".cache", ".codex", ".git", ".idea", ".pytest_cache", ".venv", "$recycle.bin",
    "__pycache__", "appdata", "node_modules", "program files", "program files (x86)", "programdata",
    "recovery", "system volume information", "windows", "venv",
}
COMPLETE_CURSOR = "::complete::"


@dataclass(frozen=True, slots=True)
class IndexProgress:
    root: str
    indexed: int
    skipped: int
    complete: bool

    def to_dict(self) -> dict[str, object]:
        return {"root": self.root, "indexed": self.indexed, "skipped": self.skipped, "complete": self.complete}


class IncrementalIndexer:
    def __init__(self, knowledge: KnowledgeService, store: MarlinStore):
        self.knowledge = knowledge
        self.store = store

    def index(
        self,
        root_value: str | Path,
        *,
        max_files: int = 5000,
        snippet_max_bytes: int = 2 * 1024 * 1024,
        on_progress: Callable[[IndexProgress], None] | None = None,
    ) -> IndexProgress:
        root = Path(root_value).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError(f"Folder does not exist: {root}")

        checkpoint = self._checkpoint(str(root))
        cursor = checkpoint.get("cursor", "")
        previous_indexed = int(checkpoint.get("indexed_count", 0) or 0)
        previous_skipped = int(checkpoint.get("skipped_count", 0) or 0)
        if cursor == COMPLETE_CURSOR:
            progress = IndexProgress(str(root), previous_indexed, previous_skipped, True)
            if on_progress:
                on_progress(progress)
            return progress
        passed_cursor = not cursor
        indexed = 0
        skipped = 0
        last_path = ""
        complete = True
        folder_ids: dict[str, str] = {}

        for current_root, dirnames, filenames in os.walk(root, topdown=True, onerror=lambda _error: None):
            current = Path(current_root)
            dirnames[:] = sorted(
                [name for name in dirnames if not self._skip(current / name)],
                key=str.lower,
            )
            filenames.sort(key=str.lower)
            folder_id = self._ensure_folder(root, current, folder_ids)

            for filename in filenames:
                path = current / filename
                key = str(path).lower()
                if not passed_cursor:
                    if key <= cursor.lower():
                        continue
                    passed_cursor = True
                if indexed >= max_files:
                    complete = False
                    break
                last_path = str(path)
                if self._skip(path):
                    skipped += 1
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    skipped += 1
                    continue

                snippet = ""
                if stat.st_size <= snippet_max_bytes and path.suffix.lower() in TEXT_EXTENSIONS:
                    try:
                        snippet = path.read_text(encoding="utf-8", errors="ignore")[:1200].strip()
                    except OSError:
                        pass
                modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(timespec="seconds")
                entity_id = self._id(path, "file")
                metadata = {
                    "path": str(path),
                    "extension": path.suffix.lower(),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
                if snippet:
                    metadata["snippet"] = snippet
                self.knowledge.create_entity(
                    "file", path.name, entity_id=entity_id, source="filesystem", metadata=metadata
                )
                self.knowledge.create_relationship(
                    folder_id,
                    "contains",
                    entity_id,
                    relationship_id=f"rel_{folder_id}_contains_{entity_id}",
                    metadata={"source": "filesystem"},
                )
                self.store.upsert_file_search(entity_id, str(path), path.name, snippet, modified)
                indexed += 1
                if on_progress and indexed % 100 == 0:
                    on_progress(IndexProgress(str(root), previous_indexed + indexed, previous_skipped + skipped, False))
            if not complete:
                break

        total_indexed = previous_indexed + indexed
        total_skipped = previous_skipped + skipped
        self._save_checkpoint(
            str(root), COMPLETE_CURSOR if complete else last_path, total_indexed, total_skipped
        )
        progress = IndexProgress(str(root), total_indexed, total_skipped, complete)
        if on_progress:
            on_progress(progress)
        return progress

    def _ensure_folder(self, root: Path, folder: Path, cache: dict[str, str]) -> str:
        key = str(folder).lower()
        if key in cache:
            return cache[key]
        folder_id = self._id(folder, "folder")
        cache[key] = folder_id
        self.knowledge.create_entity(
            "folder", folder.name or str(folder), entity_id=folder_id, source="filesystem", metadata={"path": str(folder)}
        )
        if folder != root and str(folder).lower().startswith(str(root).lower()):
            parent_id = self._ensure_folder(root, folder.parent, cache)
            self.knowledge.create_relationship(
                parent_id,
                "contains",
                folder_id,
                relationship_id=f"rel_{parent_id}_contains_{folder_id}",
                metadata={"source": "filesystem"},
            )
        return folder_id

    def _checkpoint(self, root: str) -> dict[str, object]:
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM indexing_checkpoints WHERE root = ?", (root,)).fetchone()
        return dict(row) if row else {}

    def _save_checkpoint(self, root: str, cursor: str, indexed: int, skipped: int) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO indexing_checkpoints(root, cursor, indexed_count, skipped_count, updated_at) VALUES(?, ?, ?, ?, ?)",
                (root, cursor, indexed, skipped, datetime.now(UTC).isoformat(timespec="seconds")),
            )

    @staticmethod
    def _id(path: Path, prefix: str) -> str:
        digest = hashlib.sha1(str(path).lower().encode("utf-8")).hexdigest()[:16]
        return f"{prefix}_{digest}"

    @staticmethod
    def _skip(path: Path) -> bool:
        parts = {part.lower() for part in path.parts}
        if parts.intersection(SKIP_NAMES):
            return True
        return any(part.startswith(".") and part not in {".", ".."} for part in path.parts)
