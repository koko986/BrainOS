from __future__ import annotations

from fastapi.testclient import TestClient

from marlin.config import MarlinSettings
from marlin.local_model import LocalModelUnavailable
from marlin.runtime import MarlinRuntime
from marlin.web import create_app


def make_runtime(tmp_path) -> MarlinRuntime:
    settings = MarlinSettings(
        database_path=tmp_path / "brain.db",
        auto_index_c_drive=False,
        weather_enabled=False,
        voice_output=False,
        ollama_url="http://127.0.0.1:9",
    )
    return MarlinRuntime(settings, start_background=False)


def test_deterministic_commands_bypass_the_model(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    runtime.knowledge.seed_demo()
    monkeypatch.setattr(runtime.model, "chat", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called")))

    camera = runtime.command("open camera")
    priorities = runtime.command("high priority tasks")

    assert camera["client_action"] == "open_camera"
    assert priorities["ok"]
    assert "task" in priorities["message"].lower()


def test_local_model_error_is_clear_and_other_commands_still_work(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(runtime.model, "chat", lambda *args, **kwargs: (_ for _ in ()).throw(LocalModelUnavailable("Ollama is offline.")))

    reply = runtime.command("Tell me something interesting")
    assert not reply["ok"]
    assert reply["message"] == "Ollama is offline."
    assert runtime.command("show brain graph")["ok"]


def test_backend_requires_token_and_handles_commands(tmp_path):
    runtime = make_runtime(tmp_path)
    app = create_app(runtime)
    client = TestClient(app)

    assert client.get("/api/state").status_code == 200
    assert client.post("/api/commands", json={"text": "open camera"}).status_code == 403
    response = client.post(
        "/api/commands",
        json={"text": "open camera"},
        headers={"X-Marlin-Token": app.state.token},
    )
    assert response.status_code == 200
    assert response.json()["client_action"] == "open_camera"


def test_site_command_bypasses_model_and_opens_directly(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(runtime.model, "chat", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called")))
    monkeypatch.setattr(runtime.actions, "can_open_app", lambda _name: False)
    monkeypatch.setattr(runtime.actions, "invoke", lambda name, args: __import__("marlin.actions", fromlist=["ActionOutcome"]).ActionOutcome(True, f"{name}:{args['site']}"))

    result = runtime.command("open youtube")

    assert result["ok"]
    assert result["message"] == "open_url:youtube"


def test_web_voice_returns_transcript_before_executing_command(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    calls: list[bool] = []
    monkeypatch.setattr(runtime, "listen", lambda *, execute=True: calls.append(execute) or {"text": "open chrome"})
    app = create_app(runtime)
    client = TestClient(app)

    response = client.post("/api/voice/listen", headers={"X-Marlin-Token": app.state.token})

    assert response.status_code == 200
    assert response.json()["text"] == "open chrome"
    assert calls == [False]
