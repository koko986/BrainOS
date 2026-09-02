# Phase 2: Local LLM + Safe Intent System

Phase 2 adds an Ollama-backed natural-language layer without changing the core
safety rule: Python validates and executes actions, while the LLM only produces
structured JSON intents.

## Flow

1. The CLI receives `ask "<text>"`.
2. `IntentParser` sends the text and JSON schema to Ollama `/api/chat`.
3. Pydantic validates the returned intent.
4. `ActionDispatcher` routes only allow-listed intents to Python services.
5. `ResponseGenerator` renders the safe result without exposing the full database
   back to the model.

## Supported Intents

- `list_entities`
- `list_files`
- `list_relationships`
- `search_entities`
- `get_important_tasks`
- `get_high_priority_tasks`
- `explain_high_priority`
- `seed_demo`
- `unknown`

`seed_demo` is recognized but refused through `ask` because it mutates data and
Phase 2 does not include an interactive confirmation flow yet. The explicit
`seed-demo` CLI command remains available.

## Configuration

```bash
SECOND_BRAIN_LLM_PROVIDER=ollama
SECOND_BRAIN_OLLAMA_URL=http://localhost:11434
SECOND_BRAIN_OLLAMA_MODEL=qwen3:8b
```
