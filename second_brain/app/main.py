"""CLI entrypoint for the Phase 1 Second Brain AI prototype."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from second_brain.core.config import Settings
from second_brain.database.connection import initialize_database
from second_brain.knowledge.service import KnowledgeService
from second_brain.reasoning.prolog_engine import PrologUnavailable
from second_brain.reasoning.service import ReasoningService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="second-brain",
        description="Second Brain AI Phase 1 CLI prototype.",
    )
    parser.add_argument(
        "--db",
        dest="database_path",
        help="SQLite database path. Defaults to data/database/second_brain.db.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("seed-demo", help="Seed demo projects, tasks, files, and relationships.")
    subparsers.add_parser("list-entities", help="List stored knowledge entities.")
    subparsers.add_parser("list-relationships", help="List stored knowledge relationships.")

    reason_parser = subparsers.add_parser("reason", help="Run predefined Prolog reasoning queries.")
    reason_subparsers = reason_parser.add_subparsers(dest="reason_command", required=True)
    reason_subparsers.add_parser("important-tasks", help="List tasks inferred as important.")
    reason_subparsers.add_parser("high-priority", help="List tasks inferred as high priority.")
    why_parser = reason_subparsers.add_parser(
        "why-high-priority",
        help="Explain why a task is inferred as high priority.",
    )
    why_parser.add_argument("task_id", help="Task entity ID to explain.")

    return parser


def _build_services(database_path: str | None) -> tuple[KnowledgeService, ReasoningService]:
    settings = Settings.from_env()
    if database_path:
        settings.database_path = Path(database_path)
    initialize_database(settings.database_path)
    knowledge = KnowledgeService(settings.database_path)
    reasoning = ReasoningService(knowledge, settings.prolog_dir)
    return knowledge, reasoning


def _print_entities(knowledge: KnowledgeService) -> None:
    for entity in knowledge.list_entities():
        print(f"{entity.id}\t{entity.type}\t{entity.name}\t{entity.source}")


def _print_relationships(knowledge: KnowledgeService) -> None:
    for relationship in knowledge.list_relationships():
        print(
            f"{relationship.id}\t{relationship.source_id}\t"
            f"{relationship.type}\t{relationship.target_id}"
        )


def _print_reasoned_tasks(reasoning: ReasoningService, kind: str) -> None:
    if kind == "important-tasks":
        tasks = reasoning.important_tasks()
    elif kind == "high-priority":
        tasks = reasoning.high_priority_tasks()
    else:
        raise ValueError(f"Unknown reasoning command: {kind}")

    if not tasks:
        print("No tasks matched.")
        return

    for task in tasks:
        print(f"{task.id}\t{task.name}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    knowledge, reasoning = _build_services(args.database_path)

    try:
        if args.command == "seed-demo":
            result = knowledge.seed_demo()
            print(
                "Seeded demo knowledge: "
                f"{result.entities_created} entities, {result.relationships_created} relationships."
            )
            return 0

        if args.command == "list-entities":
            _print_entities(knowledge)
            return 0

        if args.command == "list-relationships":
            _print_relationships(knowledge)
            return 0

        if args.command == "reason":
            if args.reason_command in {"important-tasks", "high-priority"}:
                _print_reasoned_tasks(reasoning, args.reason_command)
                return 0

            if args.reason_command == "why-high-priority":
                explanation = reasoning.why_high_priority(args.task_id)
                print(explanation.title)
                for index, step in enumerate(explanation.steps, start=1):
                    print(f"{index}. {step}")
                return 0

    except PrologUnavailable as exc:
        print(f"Prolog unavailable: {exc}", file=sys.stderr)
        print("Install SWI-Prolog and PySWIP to enable reasoning commands.", file=sys.stderr)
        return 2

    parser.error("Unknown command")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

