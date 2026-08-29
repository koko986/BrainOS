from __future__ import annotations

from second_brain.app.main import main


def test_cli_seed_demo_and_list_entities(tmp_path, capsys):
    db_path = tmp_path / "brain.db"

    assert main(["--db", str(db_path), "seed-demo"]) == 0
    seed_output = capsys.readouterr().out
    assert "Seeded demo knowledge" in seed_output

    assert main(["--db", str(db_path), "list-entities"]) == 0
    list_output = capsys.readouterr().out
    assert "project_second_brain" in list_output
    assert "task_finish_graph_interface" in list_output


def test_cli_ask_uses_safe_answer_path(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / "brain.db"

    def fake_answer(settings, knowledge, reasoning, text):
        assert text == "What should I work on today?"
        return "Important tasks:\n- task_finish_graph_interface: Finish graph interface"

    monkeypatch.setattr("second_brain.app.main._answer_question", fake_answer)

    assert main(["--db", str(db_path), "ask", "What should I work on today?"]) == 0
    output = capsys.readouterr().out

    assert "Important tasks" in output
