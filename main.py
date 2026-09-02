"""Root launcher for the fully local MARLIN V2 assistant."""

from __future__ import annotations

import sys

from marlin.cli import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
