# Phase 1: Python + SQLite + Prolog CLI Prototype

Phase 1 proves the core architecture before adding an LLM, GUI, voice, or semantic
search. SQLite is the canonical store for entities and relationships. Prolog is a
required symbolic reasoning layer used for explainable inference.

## Prototype Flow

1. Seed or create knowledge entities in SQLite.
2. Store relationships such as `belongs_to`, `uses`, and `depends_on`.
3. Synchronize selected symbolic facts into Prolog.
4. Run predefined safe Prolog queries.
5. Map inferred entity IDs back to database entities.
6. Print plain-text reasoning explanations.

## Reasoning Boundary

The application does not accept arbitrary Prolog from a user or future LLM. Python
owns validation and exposes only predefined reasoning methods.

