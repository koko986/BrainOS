"""Runtime configuration for Second Brain AI."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    """Small settings object for the Phase 1 prototype."""

    database_path: Path = PROJECT_ROOT / "data" / "database" / "second_brain.db"
    prolog_dir: Path = PROJECT_ROOT / "prolog"

    @classmethod
    def from_env(cls) -> "Settings":
        database_path = Path(
            os.getenv("SECOND_BRAIN_DB_PATH", str(cls.database_path))
        ).expanduser()
        prolog_dir = Path(os.getenv("SECOND_BRAIN_PROLOG_DIR", str(cls.prolog_dir))).expanduser()
        return cls(database_path=database_path, prolog_dir=prolog_dir)

