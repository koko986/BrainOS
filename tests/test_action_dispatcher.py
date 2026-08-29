from __future__ import annotations

from second_brain.ai.action_dispatcher import ActionDispatcher
from second_brain.ai.intent_schema import StructuredIntent
from second_brain.database.connection import initialize_database
from second_brain.knowledge.service import KnowledgeService


class FakeReasoning:
    def important_tasks(self):
        return []

    def high_priority_tasks(self):
        return []

    def why_high_priority(self, task_id):
        raise AssertionError("should not be called")


def test_dispatcher_lists_entities_for_allow_listed_intent(tmp_path):
    db_path = tmp_path / "brain.db"
    initialize_database(db_path)
    knowledge = KnowledgeService(db_path)
    knowledge.seed_demo()
    dispatcher = ActionDispatcher(knowledge, FakeReasoning())

    result = dispatcher.dispatch(
        StructuredIntent(
            intent="list_entities",
            language="en",
            confidence=0.9,
            parameters={},
            requires_confirmation=False,
        )
    )

    assert result.ok
    assert len(result.data["entities"]) == 7


def test_dispatcher_refuses_low_confidence_intent(tmp_path):
    db_path = tmp_path / "brain.db"
    initialize_database(db_path)
    dispatcher = ActionDispatcher(KnowledgeService(db_path), FakeReasoning())

    result = dispatcher.dispatch(
        StructuredIntent(
            intent="list_entities",
            language="en",
            confidence=0.2,
            parameters={},
            requires_confirmation=False,
        )
    )

    assert not result.ok
    assert "could not confidently" in result.message


def test_dispatcher_executes_intent_flagged_for_confirmation(tmp_path):
    """Autonomous mode ignores the LLM's confirmation hint and runs the action."""

    db_path = tmp_path / "brain.db"
    initialize_database(db_path)
    knowledge = KnowledgeService(db_path)
    dispatcher = ActionDispatcher(knowledge, FakeReasoning())

    result = dispatcher.dispatch(
        StructuredIntent(
            intent="seed_demo",
            language="en",
            confidence=0.9,
            parameters={},
            requires_confirmation=True,
        )
    )

    assert result.ok
    assert "Seeded demo knowledge" in result.message
    assert knowledge.count_entities() == 7


def test_dispatcher_seeds_demo_without_confirmation(tmp_path):
    db_path = tmp_path / "brain.db"
    initialize_database(db_path)
    knowledge = KnowledgeService(db_path)
    dispatcher = ActionDispatcher(knowledge, FakeReasoning())

    result = dispatcher.dispatch(
        StructuredIntent(
            intent="seed_demo",
            language="en",
            confidence=0.9,
            parameters={},
            requires_confirmation=False,
        )
    )

    assert result.ok
    assert knowledge.count_relationships() == 7
