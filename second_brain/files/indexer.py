"""Controlled local file indexing for MARLIN's knowledge graph."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from second_brain.knowledge.service import KnowledgeService


TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".py",
    ".js",
    ".ts",
    ".json",
    ".csv",
    ".pl",
    ".html",
    ".css",
}


@dataclass(frozen=True)
class IndexResult:
    root: str
    files_indexed: int
    files_skipped: int


class FileIndexer:
    """Indexes user-chosen folders into the knowledge database.

    The indexer reads metadata and short snippets only. It does not modify,
    delete, move, or execute user files.
    """

    def __init__(self, knowledge: KnowledgeService):
        self.knowledge = knowledge

    def index_folder(
        self,
        folder: str,
        *,
        max_files: int = 300,
        max_file_size_bytes: int = 10 * 1024 * 1024,
    ) -> IndexResult:
        root = Path(folder).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError(f"Folder does not exist: {root}")

        indexed = 0
        skipped = 0
        root_id = _entity_id_for_path(root, "folder")
        self.knowledge.create_entity(
            "folder",
            root.name or str(root),
            entity_id=root_id,
            source="filesystem",
            metadata={"path": str(root)},
        )

        for current_root, dirnames, filenames in os.walk(root, onerror=lambda _error: None):
            current_folder = Path(current_root)
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if not _should_skip(current_folder / dirname)
            ]

            parent_folder_id = _ensure_folder(self.knowledge, root, current_folder)
            for filename in filenames:
                if indexed >= max_files:
                    return IndexResult(root=str(root), files_indexed=indexed, files_skipped=skipped)
                path = current_folder / filename
                if _should_skip(path):
                    skipped += 1
                    continue

                try:
                    stat = path.stat()
                except OSError:
                    skipped += 1
                    continue
                if stat.st_size > max_file_size_bytes:
                    skipped += 1
                    continue

                metadata = {
                    "path": str(path),
                    "extension": path.suffix.lower(),
                    "size": stat.st_size,
                }
                snippet = _read_snippet(path)
                if snippet:
                    metadata["snippet"] = snippet

                file_id = _entity_id_for_path(path, "file")
                self.knowledge.create_entity(
                    "file",
                    path.name,
                    entity_id=file_id,
                    source="filesystem",
                    metadata=metadata,
                )
                self.knowledge.create_relationship(
                    parent_folder_id,
                    "contains",
                    file_id,
                    relationship_id=f"rel_{parent_folder_id}_contains_{file_id}",
                    metadata={"source": "filesystem"},
                )
                indexed += 1

        return IndexResult(root=str(root), files_indexed=indexed, files_skipped=skipped)


def _entity_id_for_path(path: Path, prefix: str) -> str:
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _ensure_folder(knowledge: KnowledgeService, root: Path, folder: Path) -> str:
    folder_id = _entity_id_for_path(folder, "folder")
    knowledge.create_entity(
        "folder",
        folder.name or str(folder),
        entity_id=folder_id,
        source="filesystem",
        metadata={"path": str(folder)},
    )
    if folder != root:
        parent = folder.parent
        if str(parent).startswith(str(root)):
            parent_id = _ensure_folder(knowledge, root, parent)
            knowledge.create_relationship(
                parent_id,
                "contains",
                folder_id,
                relationship_id=f"rel_{parent_id}_contains_{folder_id}",
                metadata={"source": "filesystem"},
            )
    return folder_id


def _should_skip(path: Path) -> bool:
    blocked = {
        ".agents",
        ".codex",
        ".git",
        ".kilo",
        ".pytest_cache",
        "$recycle.bin",
        "__pycache__",
        "program files",
        "program files (x86)",
        "programdata",
        "recovery",
        "system volume information",
        "windows",
        "node_modules",
        ".venv",
        "venv",
    }
    parts = {part.lower() for part in path.parts}
    if parts.intersection(blocked):
        return True
    if any(part.startswith(".") for part in parts if part not in {".", ".."}):
        return True
    return False


def _read_snippet(path: Path, limit: int = 1200) -> str:
    if path.suffix.lower() not in TEXT_EXTENSIONS:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit].strip()
    except OSError:
        return ""

