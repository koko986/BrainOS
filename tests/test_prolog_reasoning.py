from __future__ import annotations

import pytest

from second_brain.core.config import PROJECT_ROOT
from second_brain.database.connection import initialize_database
from second_brain.knowledge.service import KnowledgeService
from second_brain.reasoning.prolog_engine import PrologEngine
from second_brain.reasoning.service import ReasoningService


pytestmark = pytest.mark.skipif(
    not PrologEngine.is_available(),
    reason="SWI-Prolog and PySWIP are required for Prolog integration tests.",
)


def test_prolog_infers_important_and_high_priority_tasks(tmp_path):
    db_path = tmp_path / "brain.db"
    initialize_database(db_path)
    knowledge = KnowledgeService(db_path)
    knowledge.seed_demo()
    reasoning = ReasoningService(knowledge, PROJECT_ROOT / "prolog")

    important = {task.id for task in reasoning.important_tasks()}
    high_priority = {task.id for task in reasoning.high_priority_tasks()}

    assert "task_finish_graph_interface" in important
    assert "task_write_phase_two_plan" in important
    assert "task_finish_graph_interface" in high_priority


def test_prolog_explains_high_priority_task(tmp_path):
    db_path = tmp_path / "brain.db"
    initialize_database(db_path)
    knowledge = KnowledgeService(db_path)
    knowledge.seed_demo()
    reasoning = ReasoningService(knowledge, PROJECT_ROOT / "prolog")

    explanation = reasoning.why_high_priority("task_finish_graph_interface")

    assert "high priority" in explanation.title
    assert any("active project" in step for step in explanation.steps)
    assert any("soon deadline" in step for step in explanation.steps)

