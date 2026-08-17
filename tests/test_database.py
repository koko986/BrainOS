from __future__ import annotations

import pytest

from second_brain.database.connection import initialize_database
from second_brain.database.repository import EntityRepository, RelationshipRepository


def test_entity_crud(tmp_path):
    db_path = tmp_path / "brain.db"
    initialize_database(db_path)
    entities = EntityRepository(db_path)

    project = entities.create("project", "Second Brain AI", entity_id="project_second_brain")

    assert project.id == "project_second_brain"
    assert entities.get("project_second_brain").name == "Second Brain AI"
    assert entities.list("project")[0].id == "project_second_brain"


def test_relationship_creation_requires_existing_entities(tmp_path):
    db_path = tmp_path / "brain.db"
    initialize_database(db_path)
    entities = EntityRepository(db_path)
    relationships = RelationshipRepository(db_path)
    entities.create("project", "Second Brain AI", entity_id="project_second_brain")
    entities.create("task", "Finish graph interface", entity_id="task_finish_graph_interface")

    relationship = relationships.create(
        "task_finish_graph_interface",
        "belongs_to",
        "project_second_brain",
        relationship_id="rel_task_project",
    )

    assert relationship.type == "belongs_to"
    assert relationships.list("belongs_to")[0].id == "rel_task_project"

    with pytest.raises(ValueError):
        relationships.create("missing", "belongs_to", "project_second_brain")

