from __future__ import annotations

from marlin.storage import MarlinStore
from second_brain.database.connection import initialize_database
from second_brain.knowledge.service import KnowledgeService


def test_v2_migration_backs_up_and_preserves_brain_data(tmp_path):
    database = tmp_path / "brain.db"
    initialize_database(database)
    knowledge = KnowledgeService(database)
    knowledge.create_entity(
        "file", "project_notes.txt", entity_id="file_notes",
        metadata={"path": str(tmp_path / "project_notes.txt"), "snippet": "MARLIN architecture"},
    )

    store = MarlinStore(database)
    backup = store.migrate()

    assert backup is not None and backup.exists()
    assert knowledge.get_entity("file_notes") is not None
    assert store.schema_version() == 2
    assert store.search_files("architecture")[0]["entity_id"] == "file_notes"


def test_v2_migration_is_idempotent(tmp_path):
    database = tmp_path / "brain.db"
    initialize_database(database)
    store = MarlinStore(database)
    assert store.migrate() is not None
    assert store.migrate() is None

