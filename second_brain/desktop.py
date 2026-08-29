"""Native desktop launcher for the local MARLIN cockpit."""

from __future__ import annotations

import threading
import time
import webbrowser
from urllib.error import URLError
from urllib.request import urlopen

from second_brain.core.config import Settings
from second_brain.web.server import create_server


def run_desktop(settings: Settings, host: str | None = None, port: int | None = None) -> None:
    """Start or reuse the local cockpit and open it in a desktop window."""

    selected_host = host or settings.cockpit_host
    selected_port = port or settings.cockpit_port
    url = f"http://{selected_host}:{selected_port}"

    if not _is_cockpit_ready(url):
        server = create_server(settings, selected_host, selected_port)
        thread = threading.Thread(target=server.serve_forever, name="marlin-cockpit", daemon=True)
        thread.start()
        _wait_for_cockpit(url)

    if _open_native_window(url):
        return

    print("pywebview is not installed. Opening MARLIN in your browser instead.")
    webbrowser.open(url)
    print(f"MARLIN cockpit is running at {url}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nMARLIN desktop launcher stopped.")


def _open_native_window(url: str) -> bool:
    try:
        import webview  # type: ignore[import-not-found]
    except ImportError:
        return False

    webview.create_window("MARLIN Live Cockpit", url, width=1320, height=860)
    webview.start()
    return True


def _is_cockpit_ready(url: str) -> bool:
    try:
        with urlopen(f"{url}/api/state", timeout=0.8) as response:
            return response.status == 200
    except (OSError, URLError):
        return False


def _wait_for_cockpit(url: str, timeout_seconds: float = 8.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _is_cockpit_ready(url):
            return
        time.sleep(0.2)
    raise RuntimeError(f"MARLIN cockpit did not start at {url}")
