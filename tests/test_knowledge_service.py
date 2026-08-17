from __future__ import annotations

from second_brain.database.connection import initialize_database
from second_brain.knowledge.service import KnowledgeService


def test_seed_demo_creates_phase_one_sample_graph(tmp_path):
    db_path = tmp_path / "brain.db"
    initialize_database(db_path)
    knowledge = KnowledgeService(db_path)

    result = knowledge.seed_demo()

    assert result.entities_created == 7
    assert result.relationships_created == 7
    assert knowledge.get_entity("project_second_brain").metadata["active"] is True
    assert len(knowledge.list_relationships("belongs_to")) == 4

