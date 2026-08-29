"""Root launcher for MARLIN.

Defaults to hands-free voice mode. Pass a subcommand for anything else, for
example ``py main.py marlin`` for the terminal or ``py main.py desktop`` for
the cockpit window.
"""

from __future__ import annotations

import sys

from second_brain.app.main import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["jarvis"]))
