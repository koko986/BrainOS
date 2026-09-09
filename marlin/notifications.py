"""Small local notification helpers shared by the cockpit and scheduler."""

from __future__ import annotations

import sys


def play_notification_sound() -> bool:
    """Play the Windows notification sound without external media."""
    if sys.platform != "win32":
        return False
    try:
        import winsound

        winsound.PlaySound(
            "SystemNotification",
            winsound.SND_ALIAS | winsound.SND_SYNC | winsound.SND_NODEFAULT,
        )
        return True
    except (ImportError, RuntimeError):
        try:
            import winsound

            return bool(winsound.MessageBeep(winsound.MB_ICONASTERISK))
        except (ImportError, RuntimeError):
            return False
