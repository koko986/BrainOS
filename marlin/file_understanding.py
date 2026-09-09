"""Safe, incremental understanding of user-owned files."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from marlin.config import MarlinSettings
from marlin.storage import MarlinStore
from second_brain.reasoning.service import ReasoningService


TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".cs",
    ".go", ".rs", ".pl", ".sql", ".html", ".css", ".md", ".txt", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".csv",
}
BLOCKED_PARTS = {
    "appdata", "$recycle.bin", "system volume information", ".git", ".svn",
    "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".cache",
}
SENSITIVE_NAMES = {".env", "credentials.json", "id_rsa", "id_ed25519", "secrets.json"}
STOPWORDS = {
    "this", "that", "with", "from", "return", "import", "class", "function", "const",
    "self", "true", "false", "none", "string", "value", "data", "file", "path", "type",
}


class FileUnderstandingService:
    def __init__(self, settings: MarlinSettings, store: MarlinStore, reasoning: ReasoningService):
        self.settings, self.store, self.reasoning = settings, store, reasoning

    @property
    def roots(self) -> list[Path]:
        home = Path.home()
        return [home / "Desktop", home / "Documents", home / "Downloads", self.settings.graph_root]

    def analyze(self, query: str) -> dict[str, Any]:
        match = self._resolve(query)
        if not match:
            raise FileNotFoundError(f"No indexed file matched {query!r}.")
        path = Path(match["path"])
        self._validate(path)
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Content extraction is limited to files of 2 MB or less.")
        content = self._extract(path)
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        previous = self.store.get_file_insight(match["entity_id"])
        if previous and previous["checksum"] == checksum:
            return {**match, **previous, "cached": True}
        concepts = [word for word, _ in Counter(
            word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", content)
            if word.lower() not in STOPWORDS
        ).most_common(20)]
        imports = self._imports(path, content)
        technologies = self._technologies(path, content)
        lines = [line.strip() for line in content.splitlines() if line.strip()][:5]
        summary = " ".join(lines)[:1000] or f"{path.name} contains no extractable text."
        insight = self.store.upsert_file_insight(
            match["entity_id"], checksum, summary, concepts, imports, technologies
        )
        self.store.upsert_file_search(match["entity_id"], str(path), path.name, summary[:500], match.get("modified_at", ""))
        return {**match, **insight, "cached": False}

    def explain_project(self, query: str) -> dict[str, Any]:
        candidates = self.store.search_files(query, 40)
        if not candidates:
            root = Path(query)
            candidates = self._files_under(root, 40) if root.exists() else []
        insights = []
        for candidate in candidates[:20]:
            try:
                insights.append(self.analyze(candidate["path"]))
            except (OSError, ValueError):
                continue
        technologies = Counter(item for insight in insights for item in insight.get("technologies", []))
        concepts = Counter(item for insight in insights for item in insight.get("concepts", []))
        return {
            "query": query, "files": len(candidates), "analysed": len(insights),
            "technologies": [item for item, _ in technologies.most_common(10)],
            "concepts": [item for item, _ in concepts.most_common(12)],
            "sample_files": [item["path"] for item in candidates[:8]],
        }

    def related_files(self, query: str) -> dict[str, Any]:
        source = self.analyze(query)
        parent = str(Path(source["path"]).parent)
        candidates = self._files_under(Path(parent), 30)
        analysed = []
        for candidate in candidates:
            try:
                analysed.append(self.analyze(candidate["path"]))
            except (OSError, ValueError):
                continue
        engine = self.reasoning.engine
        engine.clear_agent_facts()
        id_map: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(analysed):
            atom = f"file_{index}"
            id_map[atom] = item
            for concept in item.get("concepts", [])[:12]:
                engine.assert_agent_fact("file_concept", [atom, self._atom(concept)])
            engine.assert_agent_fact("file_project", [atom, self._atom(parent)])
        atoms_by_stem = {Path(item["path"]).stem.lower(): atom for atom, item in id_map.items()}
        for atom, item in id_map.items():
            for imported in item.get("imports", []):
                target = atoms_by_stem.get(Path(imported).stem.lower())
                if target: engine.assert_agent_fact("file_import", [atom, target])
        source_atom = next((key for key, value in id_map.items() if value["entity_id"] == source["entity_id"]), "file_0")
        reasons = engine.related_file_reasons(source_atom)
        related = [{"path": id_map[atom]["path"], "reason": reason} for atom, reason in reasons if atom in id_map]
        return {"file": source["path"], "related": related[:25], "predicate": "related_file/3"}

    def change_impact(self, query: str) -> dict[str, Any]:
        source = self.analyze(query)
        candidates = self._files_under(Path(source["path"]).parent, 60)
        analysed = []
        for item in candidates:
            try:
                analysed.append(self.analyze(item["path"]))
            except (OSError, ValueError):
                continue
        engine = self.reasoning.engine; engine.clear_agent_facts()
        id_map = {f"file_{index}": item for index, item in enumerate(analysed)}
        atoms_by_stem = {Path(item["path"]).stem.lower(): atom for atom, item in id_map.items()}
        for atom, item in id_map.items():
            for imported in item.get("imports", []):
                target = atoms_by_stem.get(Path(imported).stem.lower())
                if target: engine.assert_agent_fact("file_import", [atom, target])
        source_atom = next((atom for atom, item in id_map.items() if item["entity_id"] == source["entity_id"]), "file_0")
        affected = [{"path": id_map[atom]["path"], "reason": reason} for atom, reason in engine.file_change_impacts(source_atom) if atom in id_map]
        return {"file": source["path"], "affected": affected, "predicate": "file_change_impact/3"}

    def handle(self, text: str) -> dict[str, Any] | None:
        patterns = [
            (r"explain (?:this )?project(?:\s+(.+))?$", "project"),
            (r"which files are related to\s+(.+)$", "related"),
            (r"what (?:could|will) be affected if i change\s+(.+)$", "impact"),
            (r"explain (?:this )?file\s+(.+)$", "file"),
        ]
        for pattern, kind in patterns:
            match = re.match(pattern, text.strip(), re.I)
            if not match:
                continue
            query = (match.group(1) if match.lastindex else "") or str(self.settings.graph_root)
            if kind == "project": return {"kind": kind, "result": self.explain_project(query)}
            if kind == "related": return {"kind": kind, "result": self.related_files(query)}
            if kind == "impact": return {"kind": kind, "result": self.change_impact(query)}
            return {"kind": kind, "result": self.analyze(query)}
        return None

    def _resolve(self, query: str) -> dict[str, Any] | None:
        direct = Path(query.strip().strip('"'))
        if direct.is_file():
            rows = self.store.search_files(str(direct), 10)
            return next((row for row in rows if Path(row["path"]) == direct), {"entity_id": "file_" + hashlib.sha1(str(direct).encode()).hexdigest(), "path": str(direct), "name": direct.name, "snippet": "", "modified_at": ""})
        return next(iter(self.store.search_files(query, 1)), None)

    def _files_under(self, root: Path, limit: int) -> list[dict[str, Any]]:
        prefix = str(root.resolve()) + "%"
        with self.store.connect() as connection:
            rows = connection.execute("SELECT entity_id,path,name,snippet,modified_at FROM file_search WHERE path LIKE ? LIMIT ?", (prefix, limit)).fetchall()
        return [dict(row) for row in rows]

    def _validate(self, path: Path) -> None:
        resolved = path.resolve()
        allowed_root = next((root.resolve() for root in self.roots if root.exists() and (resolved == root.resolve() or root.resolve() in resolved.parents)), None)
        if allowed_root is None:
            raise PermissionError("File analysis is limited to Desktop, Documents, Downloads, and the project root.")
        lowered = {part.lower() for part in resolved.relative_to(allowed_root).parts}
        if lowered & BLOCKED_PARTS or resolved.name.lower() in SENSITIVE_NAMES or resolved.name.lower().startswith(".env"):
            raise PermissionError("That path is excluded from file understanding.")

    @staticmethod
    def _extract(path: Path) -> str:
        if path.suffix.lower() in TEXT_EXTENSIONS:
            return path.read_text(encoding="utf-8", errors="ignore")
        if path.suffix.lower() == ".pdf":
            from pypdf import PdfReader
            return "\n".join((page.extract_text() or "") for page in PdfReader(path).pages[:30])
        if path.suffix.lower() == ".docx":
            from docx import Document
            return "\n".join(paragraph.text for paragraph in Document(path).paragraphs)
        raise ValueError(f"Unsupported file type: {path.suffix or 'unknown'}")

    @staticmethod
    def _imports(path: Path, content: str) -> list[str]:
        patterns = [r"(?:from|import)\s+([\w.]+)", r"(?:require|from)\s*\(?[\"']([^\"']+)", r"#include\s*[<\"]([^>\"]+)"]
        return list(dict.fromkeys(value for pattern in patterns for value in re.findall(pattern, content)))[:80]

    @staticmethod
    def _technologies(path: Path, content: str) -> list[str]:
        mapping = {".py": "Python", ".pl": "Prolog", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "React", ".java": "Java", ".cs": "C#", ".sql": "SQL", ".html": "HTML", ".css": "CSS"}
        result = [mapping[path.suffix.lower()]] if path.suffix.lower() in mapping else []
        for needle, name in (("fastapi", "FastAPI"), ("sqlite", "SQLite"), ("react", "React"), ("pydantic", "Pydantic"), ("clpfd", "SWI-Prolog CLP(FD)")):
            if needle in content.lower() and name not in result: result.append(name)
        return result

    @staticmethod
    def _atom(value: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
        return ("v_" + cleaned if not cleaned or not cleaned[0].isalpha() else cleaned)[:80]
