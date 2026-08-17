# Second Brain AI

Phase 1 foundation for a local-first multilingual personal knowledge and reasoning
assistant.

## What Works in Phase 1

- SQLite-backed entities and relationships
- Prolog rule files for symbolic reasoning
- Safe Python-to-Prolog query service
- CLI demo data seeding
- CLI reasoning commands
- Pytest coverage with Prolog tests skipped when SWI-Prolog or PySWIP is missing

## Setup

Install Python 3.11+, then install dependencies:

```bash
pip install -r requirements.txt
```

For reasoning commands, install SWI-Prolog and ensure `swipl` is on PATH.

## CLI

```bash
python -m second_brain.app.main seed-demo
python -m second_brain.app.main list-entities
python -m second_brain.app.main list-relationships
python -m second_brain.app.main reason important-tasks
python -m second_brain.app.main reason high-priority
python -m second_brain.app.main reason why-high-priority task_finish_graph_interface
```

## Tests

```bash
pytest
```

If SWI-Prolog or PySWIP is not installed, Prolog integration tests are skipped
with a clear reason.
