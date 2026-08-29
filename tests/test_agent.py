from __future__ import annotations

import json

from second_brain.ai.agent import MarlinAgent
from second_brain.ai.llm import AssistantTurn, _assistant_turn
from second_brain.ai.tools import ToolRegistry
from second_brain.database.connection import initialize_database
from second_brain.knowledge.service import KnowledgeService


class FakeReasoning:
    def important_tasks(self):
        return []

    def high_priority_tasks(self):
        return []

    def why_high_priority(self, task_id):
        raise AssertionError("should not be called")


class ScriptedLLM:
    """Replays a fixed list of provider message payloads."""

    def __init__(self, messages: list[dict]):
        self.messages = list(messages)
        self.calls: list[list[dict]] = []
        self.tool_sets: list[list[dict]] = []

    def chat_with_tools(self, messages, *, tools, temperature=0.3) -> AssistantTurn:
        self.calls.append(list(messages))
        self.tool_sets.append(list(tools))
        payload = self.messages.pop(0) if self.messages else {"content": "Done."}
        return _assistant_turn(payload)


def _registry(tmp_path) -> ToolRegistry:
    db_path = tmp_path / "brain.db"
    initialize_database(db_path)
    return ToolRegistry(KnowledgeService(db_path), FakeReasoning())


def _tool_call(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def test_agent_returns_plain_reply_without_tools(tmp_path):
    llm = ScriptedLLM([{"content": "All quiet."}])
    agent = MarlinAgent(llm, _registry(tmp_path))

    assert agent.reply("status?") == "All quiet."
    assert len(llm.calls) == 1


def test_agent_executes_a_tool_then_answers(tmp_path):
    target = tmp_path / "note.txt"
    llm = ScriptedLLM(
        [
            _tool_call("write_file", {"path": str(target), "content": "hello"}),
            {"content": "Saved the note."},
        ]
    )
    agent = MarlinAgent(llm, _registry(tmp_path))

    reply = agent.reply("save a note")

    assert reply == "Saved the note."
    assert target.read_text(encoding="utf-8") == "hello"


def test_tool_result_is_fed_back_to_the_model(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("secret value", encoding="utf-8")
    llm = ScriptedLLM(
        [
            _tool_call("read_file", {"path": str(target)}),
            {"content": "It says secret value."},
        ]
    )
    agent = MarlinAgent(llm, _registry(tmp_path))

    agent.reply("what does the note say?")

    second_request = llm.calls[1]
    tool_messages = [item for item in second_request if item.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert "secret value" in tool_messages[0]["content"]


def test_tool_errors_are_reported_not_raised(tmp_path):
    llm = ScriptedLLM(
        [
            _tool_call("read_file", {"path": str(tmp_path / "missing.txt")}),
            {"content": "That file does not exist."},
        ]
    )
    agent = MarlinAgent(llm, _registry(tmp_path))

    assert agent.reply("read missing") == "That file does not exist."
    tool_messages = [item for item in llm.calls[1] if item.get("role") == "tool"]
    assert tool_messages[0]["content"].startswith("ERROR:")


def test_unknown_tool_is_reported(tmp_path):
    registry = _registry(tmp_path)

    assert registry.execute("launch_missiles", {}).startswith("ERROR: unknown tool")


def test_agent_stops_looping_and_summarizes(tmp_path):
    target = tmp_path / "loop.txt"
    target.write_text("x", encoding="utf-8")
    # The model keeps asking for tools and never produces prose.
    llm = ScriptedLLM([_tool_call("read_file", {"path": str(target)})] * 10)
    agent = MarlinAgent(llm, _registry(tmp_path), max_rounds=3)

    reply = agent.reply("go")

    assert reply
    # Three tool rounds, plus one final call with tools withheld.
    assert len(llm.calls) == 4
    assert llm.tool_sets[-1] == []


def test_history_carries_across_turns(tmp_path):
    llm = ScriptedLLM([{"content": "First."}, {"content": "Second."}])
    agent = MarlinAgent(llm, _registry(tmp_path))

    agent.reply("one")
    agent.reply("two")

    roles = [item.get("role") for item in llm.calls[1]]
    assert roles == ["system", "user", "assistant", "user"]


def test_history_trim_keeps_a_user_boundary(tmp_path):
    llm = ScriptedLLM([{"content": f"reply {index}"} for index in range(10)])
    agent = MarlinAgent(llm, _registry(tmp_path), max_history=4)

    for index in range(6):
        agent.reply(f"question {index}")

    assert agent.history[0]["role"] == "user"
    assert len(agent.history) <= 4


def test_empty_input_short_circuits(tmp_path):
    llm = ScriptedLLM([])
    agent = MarlinAgent(llm, _registry(tmp_path))

    assert agent.reply("   ") == "I did not catch that."
    assert llm.calls == []


def test_dict_arguments_from_ollama_style_response(tmp_path):
    """Ollama returns tool arguments as an object rather than a JSON string."""

    target = tmp_path / "ollama.txt"
    llm = ScriptedLLM(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "write_file",
                            "arguments": {"path": str(target), "content": "ok"},
                        }
                    }
                ],
            },
            {"content": "Written."},
        ]
    )
    agent = MarlinAgent(llm, _registry(tmp_path))

    assert agent.reply("write it") == "Written."
    assert target.read_text(encoding="utf-8") == "ok"


def test_single_tool_call_collapsed_to_object_is_normalized(tmp_path):
    """The Windows PowerShell transport can collapse one-item arrays."""

    target = tmp_path / "collapsed.txt"
    llm = ScriptedLLM(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": {
                    "id": "call_x",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": str(target), "content": "y"}),
                    },
                },
            },
            {"content": "Done."},
        ]
    )
    agent = MarlinAgent(llm, _registry(tmp_path))

    assert agent.reply("write") == "Done."
    assert target.read_text(encoding="utf-8") == "y"
