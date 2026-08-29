"""Tool schemas and dispatch for the MARLIN agent.

Tool descriptions are deliberately terse. The Groq free tier allows roughly
8K tokens per minute, and the schema block is resent on every request in the
agent loop, so verbose descriptions directly reduce how many turns fit in the
budget.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from second_brain.computer import filesystem as fs
from second_brain.computer.filesystem import FileToolError
from second_brain.core.audit import record_audit
from second_brain.files.indexer import FileIndexer
from second_brain.knowledge.service import KnowledgeService
from second_brain.reasoning.prolog_engine import PrologUnavailable
from second_brain.reasoning.service import ReasoningService

MAX_RESULT_CHARS = 4000

WRITE_TOOLS = {
    "write_file",
    "append_file",
    "edit_file",
    "create_folder",
    "delete_path",
    "move_path",
    "copy_path",
}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_PATH = {"type": "string", "description": "Absolute path, or documents/desktop/downloads/home."}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    _tool(
        "read_file",
        "Read a text file.",
        {"path": _PATH, "start_line": _INT},
        ["path"],
    ),
    _tool(
        "write_file",
        "Create or overwrite a file with full content.",
        {"path": _PATH, "content": _STR},
        ["path", "content"],
    ),
    _tool(
        "append_file",
        "Append text to the end of a file.",
        {"path": _PATH, "content": _STR},
        ["path", "content"],
    ),
    _tool(
        "edit_file",
        "Replace exact existing text in a file. Prefer this over rewriting.",
        {"path": _PATH, "old_text": _STR, "new_text": _STR, "replace_all": _BOOL},
        ["path", "old_text", "new_text"],
    ),
    _tool("create_folder", "Create a folder.", {"path": _PATH}, ["path"]),
    _tool(
        "delete_path",
        "Delete a file, or a folder when recursive is true. Permanent.",
        {"path": _PATH, "recursive": _BOOL},
        ["path"],
    ),
    _tool(
        "move_path",
        "Move or rename a file or folder.",
        {"source": _PATH, "destination": _PATH},
        ["source", "destination"],
    ),
    _tool(
        "copy_path",
        "Copy a file or folder.",
        {"source": _PATH, "destination": _PATH},
        ["source", "destination"],
    ),
    _tool("list_folder", "List folder contents.", {"path": _PATH, "max_entries": _INT}, ["path"]),
    _tool(
        "find_files",
        "Find files by name pattern under a folder, recursively.",
        {"root": _PATH, "pattern": _STR, "max_results": _INT},
        ["root", "pattern"],
    ),
    _tool(
        "grep_files",
        "Search file contents for text under a folder.",
        {"root": _PATH, "query": _STR, "name_pattern": _STR, "max_results": _INT},
        ["root", "query"],
    ),
    _tool("path_info", "Check whether a path exists and its size.", {"path": _PATH}, ["path"]),
    _tool("open_path", "Open a file or folder in its default app.", {"path": _PATH}, ["path"]),
    _tool(
        "open_app",
        "Launch an app by name or executable path, for example notepad or chrome.",
        {"name": _STR},
        ["name"],
    ),
    _tool(
        "search_brain",
        "Search the indexed knowledge graph.",
        {"query": _STR, "entity_type": _STR},
        ["query"],
    ),
    _tool("list_brain_entities", "List knowledge graph entities.", {"entity_type": _STR}, []),
    _tool(
        "index_folder",
        "Index a folder into the knowledge graph for later search.",
        {"path": _PATH, "max_files": _INT},
        ["path"],
    ),
    _tool("high_priority_tasks", "Prolog-inferred high priority tasks.", {}, []),
    _tool("important_tasks", "Prolog-inferred important tasks.", {}, []),
    _tool(
        "explain_high_priority",
        "Explain why a task is high priority.",
        {"task_id": _STR},
        ["task_id"],
    ),
]


class ToolRegistry:
    """Execute MARLIN tool calls against the local machine and brain."""

    def __init__(
        self,
        knowledge: KnowledgeService,
        reasoning: ReasoningService,
        *,
        open_path: Callable[[str], None] | None = None,
        open_app: Callable[[str], None] | None = None,
    ):
        self.knowledge = knowledge
        self.reasoning = reasoning
        self._open_path = open_path
        self._open_app = open_app
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "read_file": self._read_file,
            "write_file": self._write_file,
            "append_file": self._append_file,
            "edit_file": self._edit_file,
            "create_folder": self._create_folder,
            "delete_path": self._delete_path,
            "move_path": self._move_path,
            "copy_path": self._copy_path,
            "list_folder": self._list_folder,
            "find_files": self._find_files,
            "grep_files": self._grep_files,
            "path_info": self._path_info,
            "open_path": self._open_path_tool,
            "open_app": self._open_app_tool,
            "search_brain": self._search_brain,
            "list_brain_entities": self._list_brain_entities,
            "index_folder": self._index_folder,
            "high_priority_tasks": self._high_priority_tasks,
            "important_tasks": self._important_tasks,
            "explain_high_priority": self._explain_high_priority,
        }

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return TOOL_SCHEMAS

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Run one tool and return a model-readable string result."""

        handler = self.handlers.get(name)
        if handler is None:
            return f"ERROR: unknown tool '{name}'."

        try:
            result = handler(arguments)
        except (FileToolError, ValueError) as exc:
            record_audit("tool_failed", name, _target_of(arguments), str(exc)[:200])
            return f"ERROR: {exc}"
        except PrologUnavailable as exc:
            return f"ERROR: Prolog reasoning unavailable: {exc}"
        except OSError as exc:
            record_audit("tool_failed", name, _target_of(arguments), str(exc)[:200])
            return f"ERROR: {exc}"

        status = "wrote" if name in WRITE_TOOLS else "ok"
        record_audit("tool", name, _target_of(arguments), status)
        return _stringify(result)

    def _read_file(self, args: dict[str, Any]) -> Any:
        payload = fs.read_file(
            args.get("path"),
            max_chars=_int(args.get("max_chars"), fs.MAX_TOOL_TEXT),
            start_line=_int(args.get("start_line"), 1),
        )
        return payload.to_dict()

    def _write_file(self, args: dict[str, Any]) -> Any:
        return fs.write_file(args.get("path"), str(args.get("content") or ""))

    def _append_file(self, args: dict[str, Any]) -> Any:
        return fs.append_file(args.get("path"), str(args.get("content") or ""))

    def _edit_file(self, args: dict[str, Any]) -> Any:
        return fs.edit_file(
            args.get("path"),
            str(args.get("old_text") or ""),
            str(args.get("new_text") or ""),
            replace_all=bool(args.get("replace_all")),
        )

    def _create_folder(self, args: dict[str, Any]) -> Any:
        return fs.create_folder(args.get("path"))

    def _delete_path(self, args: dict[str, Any]) -> Any:
        return fs.delete_path(args.get("path"), recursive=bool(args.get("recursive")))

    def _move_path(self, args: dict[str, Any]) -> Any:
        return fs.move_path(args.get("source"), args.get("destination"))

    def _copy_path(self, args: dict[str, Any]) -> Any:
        return fs.copy_path(args.get("source"), args.get("destination"))

    def _list_folder(self, args: dict[str, Any]) -> Any:
        return fs.list_folder(
            args.get("path"),
            max_entries=_int(args.get("max_entries"), fs.MAX_LIST_ENTRIES),
        )

    def _find_files(self, args: dict[str, Any]) -> Any:
        return fs.find_files(
            args.get("root"),
            str(args.get("pattern") or "*"),
            max_results=_int(args.get("max_results"), fs.MAX_SEARCH_RESULTS),
        )

    def _grep_files(self, args: dict[str, Any]) -> Any:
        return fs.grep_files(
            args.get("root"),
            str(args.get("query") or ""),
            name_pattern=str(args.get("name_pattern") or "*"),
            max_results=_int(args.get("max_results"), fs.MAX_GREP_RESULTS),
        )

    def _path_info(self, args: dict[str, Any]) -> Any:
        return fs.path_info(args.get("path"))

    def _open_path_tool(self, args: dict[str, Any]) -> Any:
        target = fs.resolve_path(args.get("path"))
        if not target.exists():
            raise FileToolError(f"Path does not exist: {target}")
        if self._open_path is None:
            raise FileToolError("Opening local paths is not available in this context.")
        self._open_path(str(target))
        return f"Opened {target}."

    def _open_app_tool(self, args: dict[str, Any]) -> Any:
        name = str(args.get("name") or "").strip()
        if not name:
            raise FileToolError("An app name is required.")
        if self._open_app is None:
            raise FileToolError("Launching apps is not available in this context.")
        self._open_app(name)
        return f"Launched {name}."

    def _search_brain(self, args: dict[str, Any]) -> Any:
        entity_type = args.get("entity_type")
        entities = self.knowledge.search_entities(
            str(args.get("query") or ""),
            entity_type=str(entity_type) if entity_type else None,
        )
        return {"count": len(entities), "entities": [_entity_brief(item) for item in entities]}

    def _list_brain_entities(self, args: dict[str, Any]) -> Any:
        entity_type = args.get("entity_type")
        entities = self.knowledge.list_entities_limited(
            50,
            str(entity_type) if entity_type else None,
        )
        return {"count": len(entities), "entities": [_entity_brief(item) for item in entities]}

    def _index_folder(self, args: dict[str, Any]) -> Any:
        target = fs.resolve_path(args.get("path"))
        result = FileIndexer(self.knowledge).index_folder(
            str(target),
            max_files=_int(args.get("max_files"), 300),
        )
        return (
            f"Indexed {result.files_indexed} files from {result.root} "
            f"({result.files_skipped} skipped)."
        )

    def _high_priority_tasks(self, _args: dict[str, Any]) -> Any:
        tasks = self.reasoning.high_priority_tasks()
        return {"tasks": [{"id": task.id, "name": task.name} for task in tasks]}

    def _important_tasks(self, _args: dict[str, Any]) -> Any:
        tasks = self.reasoning.important_tasks()
        return {"tasks": [{"id": task.id, "name": task.name} for task in tasks]}

    def _explain_high_priority(self, args: dict[str, Any]) -> Any:
        explanation = self.reasoning.why_high_priority(str(args.get("task_id") or ""))
        return {"title": explanation.title, "steps": explanation.steps}


def _entity_brief(entity: Any) -> dict[str, Any]:
    return {
        "id": entity.id,
        "type": entity.type,
        "name": entity.name,
        "path": entity.metadata.get("path", ""),
    }


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _target_of(arguments: dict[str, Any]) -> str:
    for key in ("path", "source", "root", "name", "query", "task_id"):
        value = arguments.get(key)
        if value:
            return str(value)[:300]
    return ""


def _stringify(result: Any) -> str:
    if isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(result)
    if len(text) > MAX_RESULT_CHARS:
        return text[:MAX_RESULT_CHARS] + "\n...[truncated]"
    return text
