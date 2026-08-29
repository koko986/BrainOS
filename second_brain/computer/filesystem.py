"""Unrestricted local filesystem access for MARLIN.

MARLIN is configured for full autonomous file control: read, write, create,
move, copy, and delete anywhere the operating system user can reach. There is
no path sandbox and no confirmation gate here on purpose. Every call is logged
by the caller through ``second_brain.core.audit`` so there is still a trail.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

MAX_TOOL_TEXT = 4000
"""Characters returned to the model per tool call.

The Groq free tier allows roughly 8K tokens per minute, so a single large file
would otherwise consume the whole budget and stall the agent loop.
"""

MAX_LIST_ENTRIES = 200
MAX_SEARCH_RESULTS = 100
MAX_GREP_RESULTS = 50
GREP_MAX_FILE_BYTES = 2_000_000

NAMED_FOLDERS = {
    "home": Path.home,
    "documents": lambda: Path.home() / "Documents",
    "my documents": lambda: Path.home() / "Documents",
    "desktop": lambda: Path.home() / "Desktop",
    "my desktop": lambda: Path.home() / "Desktop",
    "downloads": lambda: Path.home() / "Downloads",
    "my downloads": lambda: Path.home() / "Downloads",
    "pictures": lambda: Path.home() / "Pictures",
    "music": lambda: Path.home() / "Music",
    "videos": lambda: Path.home() / "Videos",
    "appdata": lambda: Path(os.getenv("APPDATA", str(Path.home()))),
    "temp": lambda: Path(os.getenv("TEMP", os.getenv("TMP", "/tmp"))),
}


class FileToolError(ValueError):
    """Raised when a filesystem tool cannot complete the request."""


@dataclass(frozen=True)
class TextPayload:
    """Text read back from disk, possibly truncated for the model."""

    path: str
    text: str
    truncated: bool
    total_chars: int
    encoding: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "text": self.text,
            "truncated": self.truncated,
            "total_chars": self.total_chars,
            "encoding": self.encoding,
        }


def resolve_path(value: object) -> Path:
    """Turn user or model input into an absolute path.

    Accepts friendly names such as ``documents`` and ``desktop`` so spoken
    commands do not need a literal path.
    """

    raw = str(value or "").strip().strip('"').strip("'")
    if not raw:
        raise FileToolError("A path is required.")

    named = NAMED_FOLDERS.get(" ".join(raw.lower().split()))
    if named is not None:
        return Path(named()).expanduser()

    return Path(os.path.expandvars(raw)).expanduser()


def _existing(path: Path) -> Path:
    if not path.exists():
        raise FileToolError(f"Path does not exist: {path}")
    return path


def _decode(data: bytes) -> tuple[str, str]:
    if b"\x00" in data[:8192]:
        raise FileToolError(
            "File looks binary, not text. Use list_folder or open_file instead of read_file."
        )
    for encoding in ("utf-8", "utf-16", "cp1252"):
        try:
            return data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace"), "utf-8-replace"


def _truncate(path: Path, text: str, limit: int, encoding: str) -> TextPayload:
    limit = max(200, min(int(limit), 200_000))
    return TextPayload(
        path=str(path),
        text=text[:limit],
        truncated=len(text) > limit,
        total_chars=len(text),
        encoding=encoding,
    )


def _write_text(target: Path, text: str, *, mode: str = "w") -> None:
    """Write text without newline translation.

    Python's text mode would rewrite every ``\\n`` as ``\\r\\n`` on Windows.
    Because ``read_file`` decodes raw bytes, an edit round trip would then turn
    existing ``\\r\\n`` into ``\\r\\r\\n`` and corrupt the file a little more on
    every edit.
    """

    with target.open(mode, encoding="utf-8", newline="") as handle:
        handle.write(text)


def read_file(path: object, *, max_chars: int = MAX_TOOL_TEXT, start_line: int = 1) -> TextPayload:
    """Read a text file from anywhere on disk."""

    target = _existing(resolve_path(path))
    if target.is_dir():
        raise FileToolError(f"Path is a folder, not a file: {target}")

    try:
        text, encoding = _decode(target.read_bytes())
    except OSError as exc:
        raise FileToolError(f"Could not read {target}: {exc}") from exc

    if start_line > 1:
        lines = text.splitlines(keepends=True)
        text = "".join(lines[max(0, start_line - 1) :])

    return _truncate(target, text, max_chars, encoding)


def write_file(path: object, content: str) -> str:
    """Create or overwrite a file, making parent folders as needed."""

    target = resolve_path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        _write_text(target, str(content))
    except OSError as exc:
        raise FileToolError(f"Could not write {target}: {exc}") from exc

    verb = "Overwrote" if existed else "Created"
    return f"{verb} {target} ({len(str(content))} characters)."


def append_file(path: object, content: str) -> str:
    """Append text to a file, creating it when missing."""

    target = resolve_path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_text(target, str(content), mode="a")
    except OSError as exc:
        raise FileToolError(f"Could not append to {target}: {exc}") from exc

    return f"Appended {len(str(content))} characters to {target}."


def edit_file(path: object, old_text: str, new_text: str, *, replace_all: bool = False) -> str:
    """Replace exact text inside an existing file."""

    target = _existing(resolve_path(path))
    if target.is_dir():
        raise FileToolError(f"Path is a folder, not a file: {target}")
    if not old_text:
        raise FileToolError("old_text must not be empty. Use write_file to replace whole files.")

    try:
        text, _ = _decode(target.read_bytes())
    except OSError as exc:
        raise FileToolError(f"Could not read {target}: {exc}") from exc

    occurrences = text.count(old_text)
    if occurrences == 0:
        raise FileToolError(f"old_text was not found in {target}.")
    if occurrences > 1 and not replace_all:
        raise FileToolError(
            f"old_text appears {occurrences} times in {target}. "
            "Pass replace_all=true or include more surrounding context."
        )

    updated = text.replace(old_text, str(new_text)) if replace_all else text.replace(old_text, str(new_text), 1)
    try:
        _write_text(target, updated)
    except OSError as exc:
        raise FileToolError(f"Could not write {target}: {exc}") from exc

    changed = occurrences if replace_all else 1
    return f"Edited {target} ({changed} replacement(s))."


def create_folder(path: object) -> str:
    """Create a folder and any missing parents."""

    target = resolve_path(path)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FileToolError(f"Could not create {target}: {exc}") from exc
    return f"Folder ready: {target}."


def delete_path(path: object, *, recursive: bool = False) -> str:
    """Delete a file, or a folder when recursive is set."""

    target = _existing(resolve_path(path))
    try:
        if target.is_dir():
            if not recursive:
                if any(target.iterdir()):
                    raise FileToolError(
                        f"{target} is a non-empty folder. Pass recursive=true to delete it."
                    )
                target.rmdir()
                return f"Deleted empty folder: {target}."
            shutil.rmtree(target)
            return f"Deleted folder and contents: {target}."
        target.unlink()
    except FileToolError:
        raise
    except OSError as exc:
        raise FileToolError(f"Could not delete {target}: {exc}") from exc
    return f"Deleted file: {target}."


def move_path(source: object, destination: object) -> str:
    """Move or rename a file or folder."""

    src = _existing(resolve_path(source))
    dst = resolve_path(destination)
    if dst.is_dir():
        dst = dst / src.name
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    except OSError as exc:
        raise FileToolError(f"Could not move {src} to {dst}: {exc}") from exc
    return f"Moved {src} to {dst}."


def copy_path(source: object, destination: object) -> str:
    """Copy a file, or a folder tree."""

    src = _existing(resolve_path(source))
    dst = resolve_path(destination)
    try:
        if src.is_dir():
            shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
        else:
            if dst.is_dir():
                dst = dst / src.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
    except OSError as exc:
        raise FileToolError(f"Could not copy {src} to {dst}: {exc}") from exc
    return f"Copied {src} to {dst}."


def list_folder(path: object, *, max_entries: int = MAX_LIST_ENTRIES) -> dict[str, object]:
    """List the immediate contents of a folder."""

    target = _existing(resolve_path(path))
    if not target.is_dir():
        raise FileToolError(f"Path is not a folder: {target}")

    limit = max(1, min(int(max_entries), 2000))
    folders: list[str] = []
    files: list[dict[str, object]] = []
    total = 0
    try:
        for entry in sorted(target.iterdir(), key=lambda item: item.name.lower()):
            total += 1
            if len(folders) + len(files) >= limit:
                continue
            try:
                if entry.is_dir():
                    folders.append(entry.name)
                else:
                    files.append({"name": entry.name, "bytes": entry.stat().st_size})
            except OSError:
                continue
    except OSError as exc:
        raise FileToolError(f"Could not list {target}: {exc}") from exc

    return {
        "path": str(target),
        "folders": folders,
        "files": files,
        "total_entries": total,
        "truncated": total > limit,
    }


def find_files(
    root: object,
    pattern: str,
    *,
    max_results: int = MAX_SEARCH_RESULTS,
) -> dict[str, object]:
    """Find files by name pattern under a folder, recursively."""

    base = _existing(resolve_path(root))
    if not base.is_dir():
        raise FileToolError(f"Path is not a folder: {base}")

    glob_pattern = str(pattern or "").strip() or "*"
    if not any(char in glob_pattern for char in "*?["):
        glob_pattern = f"*{glob_pattern}*"

    limit = max(1, min(int(max_results), 1000))
    matches: list[str] = []
    for current_root, dir_names, file_names in os.walk(base, onerror=lambda _exc: None):
        dir_names[:] = [name for name in dir_names if not _is_noise_dir(name)]
        for name in file_names:
            if fnmatch.fnmatch(name.lower(), glob_pattern.lower()):
                matches.append(str(Path(current_root) / name))
                if len(matches) >= limit:
                    return {
                        "root": str(base),
                        "pattern": glob_pattern,
                        "matches": matches,
                        "truncated": True,
                    }

    return {
        "root": str(base),
        "pattern": glob_pattern,
        "matches": matches,
        "truncated": False,
    }


def grep_files(
    root: object,
    query: str,
    *,
    name_pattern: str = "*",
    max_results: int = MAX_GREP_RESULTS,
) -> dict[str, object]:
    """Search file contents for a substring, case-insensitively."""

    base = _existing(resolve_path(root))
    needle = str(query or "").strip()
    if not needle:
        raise FileToolError("A search query is required.")

    if base.is_file():
        candidates = [base]
        base_root = base.parent
    else:
        base_root = base
        candidates = []

    limit = max(1, min(int(max_results), 500))
    glob_pattern = str(name_pattern or "*").strip() or "*"
    lowered = needle.lower()
    hits: list[dict[str, object]] = []

    def scan(file_path: Path) -> bool:
        try:
            if file_path.stat().st_size > GREP_MAX_FILE_BYTES:
                return False
            text, _ = _decode(file_path.read_bytes())
        except (OSError, FileToolError):
            return False
        for number, line in enumerate(text.splitlines(), start=1):
            if lowered in line.lower():
                hits.append(
                    {
                        "path": str(file_path),
                        "line": number,
                        "text": line.strip()[:300],
                    }
                )
                if len(hits) >= limit:
                    return True
        return False

    if candidates:
        for file_path in candidates:
            scan(file_path)
    else:
        for current_root, dir_names, file_names in os.walk(base_root, onerror=lambda _exc: None):
            dir_names[:] = [name for name in dir_names if not _is_noise_dir(name)]
            for name in file_names:
                if not fnmatch.fnmatch(name.lower(), glob_pattern.lower()):
                    continue
                if scan(Path(current_root) / name):
                    return {
                        "root": str(base),
                        "query": needle,
                        "hits": hits,
                        "truncated": True,
                    }

    return {"root": str(base), "query": needle, "hits": hits, "truncated": False}


def path_info(path: object) -> dict[str, object]:
    """Report whether a path exists and what it is."""

    target = resolve_path(path)
    if not target.exists():
        return {"path": str(target), "exists": False}
    try:
        stat = target.stat()
    except OSError as exc:
        raise FileToolError(f"Could not stat {target}: {exc}") from exc
    return {
        "path": str(target),
        "exists": True,
        "kind": "folder" if target.is_dir() else "file",
        "bytes": stat.st_size,
        "modified_epoch": int(stat.st_mtime),
    }


def _is_noise_dir(name: str) -> bool:
    """Skip caches and version-control noise while walking large trees.

    These are traversal-performance skips only. Any of these paths can still be
    read, written, or deleted when addressed directly.
    """

    lowered = name.lower()
    return lowered in {
        "$recycle.bin",
        ".git",
        ".pytest_cache",
        "__pycache__",
        "node_modules",
        "system volume information",
        ".venv",
        "venv",
    }
