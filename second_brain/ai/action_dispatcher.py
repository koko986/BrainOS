"""Safe allow-listed dispatcher for validated intents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from second_brain.ai.intent_schema import StructuredIntent
from second_brain.database.models import Entity, Relationship
from second_brain.knowledge.service import KnowledgeService
from second_brain.reasoning.prolog_engine import PrologUnavailable
from second_brain.reasoning.service import ReasoningExplanation, ReasoningService


MUTATING_INTENTS: set[str] = set()
"""Intents held back pending confirmation.

Empty because MARLIN runs in full autonomous mode. Kept as a seam so a single
intent can be gated again without restructuring the dispatcher.
"""


@dataclass(frozen=True)
class ActionResult:
    intent: str
    ok: bool
    language: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)


class ActionDispatcher:
    """Execute only hardcoded safe actions from validated intents."""

    def __init__(self, knowledge: KnowledgeService, reasoning: ReasoningService):
        self.knowledge = knowledge
        self.reasoning = reasoning

    def dispatch(self, structured_intent: StructuredIntent) -> ActionResult:
        if structured_intent.intent == "unknown" or structured_intent.is_low_confidence:
            return ActionResult(
                intent=structured_intent.intent,
                ok=False,
                language=structured_intent.language,
                message="I could not confidently map that request to a safe Phase 2 action.",
            )

        if structured_intent.intent in MUTATING_INTENTS:
            return ActionResult(
                intent=structured_intent.intent,
                ok=False,
                language=structured_intent.language,
                message="This action requires confirmation and was not executed.",
            )

        try:
            return self._dispatch_allow_listed(structured_intent)
        except PrologUnavailable as exc:
            return ActionResult(
                intent=structured_intent.intent,
                ok=False,
                language=structured_intent.language,
                message=f"Prolog reasoning is unavailable: {exc}",
            )

    def _dispatch_allow_listed(self, structured_intent: StructuredIntent) -> ActionResult:
        language = structured_intent.language
        intent = structured_intent.intent

        if intent == "list_entities":
            entities = self.knowledge.list_entities()
            return ActionResult(
                intent=intent,
                ok=True,
                language=language,
                message="Listed entities.",
                data={"entities": [_entity_dict(entity) for entity in entities]},
            )

        if intent == "list_files":
            entities = self.knowledge.list_entities("file")
            return ActionResult(
                intent=intent,
                ok=True,
                language=language,
                message="Listed files.",
                data={"entities": [_entity_dict(entity) for entity in entities]},
            )

        if intent == "search_entities":
            query = str(structured_intent.parameters["query"])
            entity_type = structured_intent.parameters.get("entity_type")
            if entity_type is not None:
                entity_type = str(entity_type)
            entities = self.knowledge.search_entities(query, entity_type=entity_type)
            return ActionResult(
                intent=intent,
                ok=True,
                language=language,
                message="Searched entities.",
                data={"query": query, "entities": [_entity_dict(entity) for entity in entities]},
            )

        if intent == "list_relationships":
            relationships = self.knowledge.list_relationships()
            return ActionResult(
                intent=intent,
                ok=True,
                language=language,
                message="Listed relationships.",
                data={"relationships": [_relationship_dict(item) for item in relationships]},
            )

        if intent == "get_important_tasks":
            tasks = self.reasoning.important_tasks()
            return ActionResult(
                intent=intent,
                ok=True,
                language=language,
                message="Found important tasks.",
                data={"tasks": [_entity_dict(task) for task in tasks]},
            )

        if intent == "get_high_priority_tasks":
            tasks = self.reasoning.high_priority_tasks()
            return ActionResult(
                intent=intent,
                ok=True,
                language=language,
                message="Found high priority tasks.",
                data={"tasks": [_entity_dict(task) for task in tasks]},
            )

        if intent == "seed_demo":
            seeded = self.knowledge.seed_demo()
            return ActionResult(
                intent=intent,
                ok=True,
                language=language,
                message=(
                    f"Seeded demo knowledge: {seeded.entities_created} entities, "
                    f"{seeded.relationships_created} relationships."
                ),
            )

        if intent == "explain_high_priority":
            task_id = str(structured_intent.parameters["task_id"])
            explanation = self.reasoning.why_high_priority(task_id)
            return ActionResult(
                intent=intent,
                ok=True,
                language=language,
                message="Explained high priority task.",
                data={"explanation": _explanation_dict(explanation)},
            )

        return ActionResult(
            intent=intent,
            ok=False,
            language=language,
            message="Unsupported intent.",
        )


def _entity_dict(entity: Entity) -> dict[str, Any]:
    return {
        "id": entity.id,
        "type": entity.type,
        "name": entity.name,
        "source": entity.source,
        "metadata": entity.metadata,
    }


def _relationship_dict(relationship: Relationship) -> dict[str, Any]:
    return {
        "id": relationship.id,
        "source_id": relationship.source_id,
        "type": relationship.type,
        "target_id": relationship.target_id,
    }


def _explanation_dict(explanation: ReasoningExplanation) -> dict[str, Any]:
    return {
        "title": explanation.title,
        "steps": explanation.steps,
    }
