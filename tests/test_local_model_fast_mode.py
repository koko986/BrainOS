from __future__ import annotations

import json

from marlin.config import MarlinSettings
from marlin.local_model import OllamaLocalModel


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        yield b'{"message":{"role":"assistant","content":"Fast."},"done":false}\n'
        yield b'{"message":{"role":"assistant","content":""},"done":true,"eval_count":1,"total_duration":1000000}\n'


def test_chat_explicitly_disables_hidden_thinking(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured.update(json.loads(request.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr("marlin.local_model.urlopen", fake_urlopen)
    model = OllamaLocalModel(MarlinSettings(ollama_think=False))

    result = model.chat([{"role": "user", "content": "hello"}])

    assert result.content == "Fast."
    assert captured["think"] is False
