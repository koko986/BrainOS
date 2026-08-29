"""Safe Python-to-Prolog bridge for predefined reasoning queries."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Iterable

from second_brain.database.models import Entity, Relationship


SAFE_ATOM_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SUPPORTED_RELATIONSHIPS = {"belongs_to", "uses", "depends_on", "contains"}


class PrologUnavailable(RuntimeError):
    """Raised when SWI-Prolog or PySWIP is not available."""


class PrologEngine:
    """Loads Prolog rules and exposes only safe predefined queries."""

    def __init__(self, prolog_dir: Path):
        self.prolog_dir = prolog_dir
        self._prolog = None

    @staticmethod
    def is_available() -> bool:
        if shutil.which("swipl") is None and shutil.which("swipl.exe") is None:
            return False
        try:
            import pyswip  # noqa: F401
        except Exception:
            return False
        return True

    def load(self) -> None:
        if self._prolog is not None:
            return
        if shutil.which("swipl") is None and shutil.which("swipl.exe") is None:
            raise PrologUnavailable("SWI-Prolog executable was not found on PATH.")
        try:
            from pyswip import Prolog
        except Exception as exc:
            raise PrologUnavailable("PySWIP is not installed or could not load SWI-Prolog.") from exc

        reasoning_file = self.prolog_dir / "reasoning.pl"
        if not reasoning_file.exists():
            raise PrologUnavailable(f"Missing Prolog rules file: {reasoning_file}")

        self._prolog = Prolog()
        self._prolog.consult(str(reasoning_file))

    def sync(self, entities: Iterable[Entity], relationships: Iterable[Relationship]) -> None:
        self.load()
        self._clear_dynamic_facts()
        for entity in entities:
            self._assert_entity(entity)
        for relationship in relationships:
            self._assert_relationship(relationship)

    def important_tasks(self) -> list[str]:
        return self._query_entity_ids("important_task(Task)", "Task")

    def high_priority_tasks(self) -> list[str]:
        return self._query_entity_ids("high_priority(Task)", "Task")

    def is_high_priority(self, task_id: str) -> bool:
        task_atom = self._safe_atom(task_id)
        return bool(list(self._query(f"high_priority({task_atom})")))

    def high_priority_reasons(self, task_id: str) -> list[str]:
        task_atom = self._safe_atom(task_id)
        return sorted(
            {str(row["Reason"]) for row in self._query(f"high_priority_reason({task_atom}, Reason)")}
        )

    def _clear_dynamic_facts(self) -> None:
        for predicate in [
            "project(_)",
            "task(_)",
            "file(_)",
            "note(_)",
            "topic(_)",
            "technology(_)",
            "active(_)",
            "deadline_soon(_)",
            "belongs_to(_, _)",
            "contains(_, _)",
            "uses(_, _)",
            "depends_on(_, _)",
        ]:
            list(self._query(f"retractall({predicate})"))

    def _assert_entity(self, entity: Entity) -> None:
        atom = self._safe_atom(entity.id)
        predicate = entity.type.lower()
        if predicate in {"project", "task", "file", "note", "topic", "technology"}:
            self._assertz(f"{predicate}({atom})")
        if entity.type == "project" and entity.metadata.get("active"):
            self._assertz(f"active({atom})")
        if entity.type == "task" and entity.metadata.get("deadline_soon"):
            self._assertz(f"deadline_soon({atom})")

    def _assert_relationship(self, relationship: Relationship) -> None:
        if relationship.type not in SUPPORTED_RELATIONSHIPS:
            return
        source = self._safe_atom(relationship.source_id)
        target = self._safe_atom(relationship.target_id)
        self._assertz(f"{relationship.type}({source}, {target})")

    def _query_entity_ids(self, query: str, variable: str) -> list[str]:
        return sorted({str(row[variable]) for row in self._query(query)})

    def _assertz(self, fact: str) -> None:
        list(self._query(f"assertz({fact})"))

    def _query(self, query: str):
        self.load()
        return self._prolog.query(query)

    @staticmethod
    def _safe_atom(value: str) -> str:
        if not SAFE_ATOM_RE.match(value):
            raise ValueError(
                f"Unsafe Prolog atom {value!r}. Use lowercase letters, numbers, and underscores."
            )
        return value
