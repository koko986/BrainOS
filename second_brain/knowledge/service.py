"""Knowledge service that coordinates entity and relationship repositories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from second_brain.database.models import Entity, Relationship
from second_brain.database.repository import EntityRepository, RelationshipRepository


@dataclass(frozen=True)
class SeedResult:
    entities_created: int
    relationships_created: int


class KnowledgeService:
    """High-level API for Phase 1 knowledge operations."""

    def __init__(self, database_path: Path):
        self.entities = EntityRepository(database_path)
        self.relationships = RelationshipRepository(database_path)

    def create_entity(
        self,
        entity_type: str,
        name: str,
        *,
        entity_id: str | None = None,
        source: str = "manual",
        metadata: dict | None = None,
    ) -> Entity:
        return self.entities.create(
            entity_type,
            name,
            entity_id=entity_id,
            source=source,
            metadata=metadata,
        )

    def create_relationship(
        self,
        source_id: str,
        relationship_type: str,
        target_id: str,
        *,
        relationship_id: str | None = None,
        metadata: dict | None = None,
    ) -> Relationship:
        return self.relationships.create(
            source_id,
            relationship_type,
            target_id,
            relationship_id=relationship_id,
            metadata=metadata,
        )

    def list_entities(self, entity_type: str | None = None) -> list[Entity]:
        return self.entities.list(entity_type)

    def get_entity(self, entity_id: str) -> Entity | None:
        return self.entities.get(entity_id)

    def list_relationships(self, relationship_type: str | None = None) -> list[Relationship]:
        return self.relationships.list(relationship_type)

    def seed_demo(self) -> SeedResult:
        """Create deterministic demo data for the reasoning prototype."""

        demo_entities = [
            ("project_second_brain", "project", "Second Brain AI", {"active": True}),
            ("task_finish_graph_interface", "task", "Finish graph interface", {"deadline_soon": True}),
            ("task_write_phase_two_plan", "task", "Write Phase 2 LLM plan", {"deadline_soon": False}),
            ("file_main_py", "file", "main.py", {"path": "second_brain/app/main.py"}),
            ("file_reasoning_pl", "file", "reasoning.pl", {"path": "prolog/reasoning.pl"}),
            ("tech_python", "technology", "Python", {}),
            ("tech_prolog", "technology", "Prolog", {}),
        ]
        demo_relationships = [
            (
                "rel_task_graph_project",
                "task_finish_graph_interface",
                "belongs_to",
                "project_second_brain",
            ),
            (
                "rel_task_phase_two_project",
                "task_write_phase_two_plan",
                "belongs_to",
                "project_second_brain",
            ),
            ("rel_main_project", "file_main_py", "belongs_to", "project_second_brain"),
            ("rel_reasoning_project", "file_reasoning_pl", "belongs_to", "project_second_brain"),
            ("rel_project_python", "project_second_brain", "uses", "tech_python"),
            ("rel_project_prolog", "project_second_brain", "uses", "tech_prolog"),
            (
                "rel_task_graph_reasoning",
                "task_finish_graph_interface",
                "depends_on",
                "file_reasoning_pl",
            ),
        ]

        for entity_id, entity_type, name, metadata in demo_entities:
            self.create_entity(
                entity_type,
                name,
                entity_id=entity_id,
                source="demo",
                metadata=metadata,
            )

        for relationship_id, source_id, relationship_type, target_id in demo_relationships:
            self.create_relationship(
                source_id,
                relationship_type,
                target_id,
                relationship_id=relationship_id,
                metadata={"source": "demo"},
            )

        return SeedResult(
            entities_created=len(demo_entities),
            relationships_created=len(demo_relationships),
        )

