"""Reasoning service that maps database knowledge into Prolog results."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from second_brain.database.models import Entity
from second_brain.knowledge.service import KnowledgeService
from second_brain.reasoning.prolog_engine import PrologEngine


@dataclass(frozen=True)
class ReasoningExplanation:
    title: str
    steps: list[str]


class ReasoningService:
    """Application-level API for symbolic reasoning."""

    def __init__(self, knowledge: KnowledgeService, prolog_dir: Path):
        self.knowledge = knowledge
        self.engine = PrologEngine(prolog_dir)

    def important_tasks(self) -> list[Entity]:
        ids = self._synced_engine().important_tasks()
        return self._entities_for_ids(ids)

    def high_priority_tasks(self) -> list[Entity]:
        ids = self._synced_engine().high_priority_tasks()
        return self._entities_for_ids(ids)

    def blocked_tasks(self) -> list[Entity]:
        return self._entities_for_ids(self._synced_engine().blocked_tasks())

    def overdue_tasks(self) -> list[Entity]:
        return self._entities_for_ids(self._synced_engine().overdue_tasks())

    def morning_priorities(self) -> list[Entity]:
        return self._entities_for_ids(self._synced_engine().morning_priorities())

    def current_project_focus(self) -> list[Entity]:
        return self._entities_for_ids(self._synced_engine().current_project_focus())

    def dependency_chain(self, task_id: str) -> list[Entity]:
        return self._entities_for_ids(self._synced_engine().dependency_chain(task_id))

    def why_high_priority(self, task_id: str) -> ReasoningExplanation:
        task = self.knowledge.get_entity(task_id)
        if task is None:
            return ReasoningExplanation(
                title=f"{task_id} was not found.",
                steps=["No entity with that ID exists in the knowledge database."],
            )

        engine = self._synced_engine()
        if not engine.is_high_priority(task_id):
            return ReasoningExplanation(
                title=f"{task.name} is not currently high priority.",
                steps=["The Prolog rules did not infer high_priority/1 for this task."],
            )

        reasons = engine.high_priority_reasons(task_id)
        steps = [self._humanize_reason(reason, task) for reason in reasons]
        if not steps:
            steps = ["Prolog inferred high_priority/1, but no reason predicate matched."]

        return ReasoningExplanation(
            title=f"{task.name} is high priority because:",
            steps=steps,
        )

    def _synced_engine(self) -> PrologEngine:
        # Filesystem nodes can number in the tens of thousands, but the current
        # symbolic rules reason only over projects, tasks, technologies and
        # their semantic relationships.
        entities = []
        for entity_type in ("project", "task", "technology"):
            entities.extend(self.knowledge.list_entities(entity_type))
        relationships = []
        for relationship_type in ("belongs_to", "depends_on", "uses"):
            relationships.extend(self.knowledge.list_relationships(relationship_type))
        self.engine.sync(
            entities,
            relationships,
        )
        return self.engine

    def _entities_for_ids(self, ids: list[str]) -> list[Entity]:
        entities = []
        for entity_id in ids:
            entity = self.knowledge.get_entity(entity_id)
            if entity is not None:
                entities.append(entity)
        return entities

    def _humanize_reason(self, reason: str, task: Entity) -> str:
        if reason == "belongs_to_active_project":
            return f"{task.name} belongs to an active project."
        if reason == "deadline_soon":
            return f"{task.name} has a soon deadline."
        if reason == "important_task":
            return "The task is important because it belongs to an active project."
        if reason == "dependency":
            return "The task depends on another knowledge item, so it may block related work."
        return reason.replace("_", " ")
