# Background MARLIN

`py main.py` launches a separate resident desktop process on Windows. The terminal
may close without ending MARLIN. A second launch restores the existing instance
for the same database; it does not start another microphone listener.

- Window X, "close", "close yourself", or "close MARLIN": hide the cockpit.
- "Hey MARLIN", followed by "show yourself" or "where are you": show it again.
- "Exit MARLIN" or "quit MARLIN": stop the assistant, microphone, and backend.
- The notification-area icon provides Show, Hide, and Exit commands.
- "Close that tab" or "close YouTube": close MARLIN's controlled YouTube tab.

The tab command never sends Ctrl+W to an arbitrary foreground window. If the
controlled tab navigated away from YouTube, it stays open to avoid losing work.
Other browsers and tabs are not controlled by this command.

Without pywebview, the resident host opens the browser cockpit. Close its tab to
hide it; use the tray to reopen or exit. Background listening does not survive a
full Exit, Windows shutdown, or sleep. This change does not enable Windows-login
startup automatically.

The per-launch endpoint/token/PID are stored in the ignored `.desktop.json` file
alongside the database. A Windows named mutex prevents duplicate resident hosts.
`POST /api/desktop/show`, `/hide`, and `/exit` require the existing session token.
The lightweight `/api/health` endpoint checks backend readiness without waiting
for model or microphone discovery. Startup errors are written to `.desktop.log`.
