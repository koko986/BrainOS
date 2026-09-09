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

    @property
    def loaded(self) -> bool:
        return self._prolog is not None

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
            raise PrologUnavailable(
                f"PySWIP is installed but could not load SWI-Prolog: {exc}"
            ) from exc

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

    def blocked_tasks(self) -> list[str]:
        return self._query_entity_ids("blocked_task(Task)", "Task")

    def overdue_tasks(self) -> list[str]:
        return self._query_entity_ids("overdue_task(Task)", "Task")

    def morning_priorities(self) -> list[str]:
        return self._query_entity_ids("morning_priority(Task)", "Task")

    def current_project_focus(self) -> list[str]:
        return self._query_entity_ids("current_project_focus(Project)", "Project")

    def dependency_chain(self, task_id: str) -> list[str]:
        task_atom = self._safe_atom(task_id)
        return sorted(
            {str(row["Dependency"]) for row in self._query(f"dependency_chain({task_atom}, Dependency)")}
        )

    def morning_priority_reasons(self, task_id: str) -> list[str]:
        task_atom = self._safe_atom(task_id)
        return sorted(
            {str(row["Reason"]) for row in self._query(f"morning_priority_reason({task_atom}, Reason)")}
        )

    def plan_day_slots(
        self,
        durations: list[int],
        earliest: list[int],
        latest: list[int],
        busy: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        if not durations:
            return []
        if not (len(durations) == len(earliest) == len(latest)) or len(durations) > 100:
            raise ValueError("Schedule inputs must have matching lengths of at most 100.")
        numbers = durations + earliest + latest + [value for pair in busy for value in pair]
        if any(not isinstance(value, int) or value < 0 or value > 1440 for value in numbers):
            raise ValueError("Schedule values must be integer minutes between 0 and 1440.")
        busy_term = "[" + ",".join(f"busy({start},{end})" for start, end in busy) + "]"
        query = (
            f"schedule_tasks({durations},{earliest},{latest},{busy_term},Starts,Ends)"
        )
        rows = list(self._query(query))
        if not rows:
            return []
        return [(int(start), int(end)) for start, end in zip(rows[0]["Starts"], rows[0]["Ends"])]

    def intervals_conflict(self, first: tuple[int, int], second: tuple[int, int]) -> bool:
        values = (*first, *second)
        if any(not isinstance(value, int) or value < 0 or value > 1440 for value in values):
            raise ValueError("Intervals must use integer minutes between 0 and 1440.")
        return bool(list(self._query(f"interval_conflict({first[0]},{first[1]},{second[0]},{second[1]})")))

    def dependency_order_valid(self, before: tuple[int, int], after_start: int) -> bool:
        values = (*before, after_start)
        if any(not isinstance(value, int) or value < 0 or value > 1440 for value in values):
            raise ValueError("Dependency times must use integer minutes between 0 and 1440.")
        duration = before[1] - before[0]
        return bool(list(self._query(f"dependency_order({before[0]},{duration},{after_start})")))

    def clear_agent_facts(self) -> None:
        for predicate in (
            "file_concept(_, _)", "file_import(_, _)", "file_project(_, _)",
            "file_modified_days(_, _)", "preference_fact(_, _, _)",
            "task_project(_, _)", "research_source(_, _, _, _)",
            "research_claim(_, _, _)",
        ):
            list(self._query(f"retractall({predicate})"))

    def assert_agent_fact(self, predicate: str, arguments: list[str | int]) -> None:
        allowed = {
            "file_concept", "file_import", "file_project", "file_modified_days",
            "preference_fact", "task_project", "research_source",
            "research_claim",
        }
        if predicate not in allowed:
            raise ValueError(f"Unsupported agent fact: {predicate}")
        encoded = []
        for value in arguments:
            encoded.append(str(value) if isinstance(value, int) else self._safe_atom(value))
        self._assertz(f"{predicate}({','.join(encoded)})")

    def related_file_reasons(self, file_id: str) -> list[tuple[str, str]]:
        atom = self._safe_atom(file_id)
        return sorted({
            (str(row["Related"]), str(row["Reason"]))
            for row in self._query(f"related_file({atom},Related,Reason)")
        })

    def file_change_impacts(self, file_id: str) -> list[tuple[str, str]]:
        atom = self._safe_atom(file_id)
        return sorted({
            (str(row["Affected"]), str(row["Reason"]))
            for row in self._query(f"file_change_impact({atom},Affected,Reason)")
        })

    def preference_influences(self, task_id: str) -> list[tuple[str, str]]:
        atom = self._safe_atom(task_id)
        return sorted({
            (str(row["Category"]), str(row["Value"]))
            for row in self._query(f"preference_influences({atom},Category,Value)")
        })

    def source_quality(self, source_id: str) -> int | None:
        atom = self._safe_atom(source_id)
        rows = list(self._query(f"source_quality({atom},Score)"))
        return int(rows[0]["Score"]) if rows else None

    def evidence_score(self, source_id: str) -> int | None:
        atom = self._safe_atom(source_id)
        rows = list(self._query(f"evidence_score({atom},Score)"))
        return int(rows[0]["Score"]) if rows else None

    def explain_web_evidence(self, source_id: str) -> dict[str, list[str]]:
        atom = self._safe_atom(source_id)
        supported = sorted({str(row["Claim"]) for row in self._query(f"corroborated_source({atom},_,Claim)")})
        conflicts = sorted({str(row["Claim"]) for row in self._query(f"conflicting_source({atom},_,Claim)")})
        return {"corroborated_claims": supported, "conflicting_claims": conflicts}

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
            "overdue(_)",
            "blocked(_)",
            "focused(_)",
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
        if entity.type == "task" and entity.metadata.get("overdue"):
            self._assertz(f"overdue({atom})")
        if entity.type == "task" and entity.metadata.get("blocked"):
            self._assertz(f"blocked({atom})")
        if entity.type == "project" and entity.metadata.get("current_focus"):
            self._assertz(f"focused({atom})")

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
