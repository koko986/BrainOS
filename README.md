# MARLIN V2 / BrainOS

MARLIN V2 is a fully local, JARVIS-style Windows assistant. Conversation runs
through Ollama and `qwen3:4b-instruct`; voice input uses Faster-Whisper; voice
output uses Piper; knowledge is stored in SQLite; symbolic reasoning uses
SWI-Prolog. No paid service, account, or API key is required.

## First setup

Python 3.11 or newer, Ollama, and SWI-Prolog are required.

```powershell
py main.py setup
py main.py doctor
```

Setup installs Python dependencies and downloads the local Qwen, Whisper, and
Piper models. This is the only large network download MARLIN needs.

To start hands-free wake-word mode automatically after Windows login:

```powershell
py main.py setup --launch-on-login
```

## Run

```powershell
py main.py             # desktop brain cockpit
py main.py terminal    # terminal conversation and commands
py main.py voice       # wake-word mode
py main.py serve       # browser cockpit
```

The desktop cockpit opens the native `pywebview` window when available and
falls back to the browser at `http://127.0.0.1:8765`.

The desktop and browser cockpit listen locally for **"Hey MARLIN"**. After
MARLIN answers, speak one command or question. The wake-word detector uses
Vosk and does not send microphone audio to an online service.

## Local functions

- Real local conversation with streamed Ollama output and persistent context.
- English/Burmese voice input, local wake word, and cancellable British male speech.
- SQLite brain graph, incremental C-drive indexing, and FTS file search.
- Prolog priority, blocked/overdue task, dependency, and project-focus reasoning.
- Alarms, reminders, snooze, standby/wake state, morning briefings, and media controls.
- Typed file, folder, app, camera, and media actions.

Read/search/open/index/create actions run directly. Append, edit, overwrite,
move, rename, delete, and close-app operations require a one-use confirmation.
Deletes go to the Windows Recycle Bin. Windows system paths and arbitrary shell
execution remain blocked.

## Examples

```text
MARLIN, wake up
morning briefing
set an alarm in 20 minutes
remind me to finish the Prolog report
high priority tasks
why high priority task_finish_graph_interface
search my files for python
open Documents
play music
stand by
```

## Compatibility commands

```powershell
py main.py seed-demo
py main.py list-entities
py main.py list-relationships
py main.py reason important-tasks
py main.py reason high-priority
py main.py reason why-high-priority TASK_ID
```

## Tests

```powershell
py -m pytest
```

Model, microphone, camera, and desktop integration tests skip clearly when the
relevant local dependency or hardware is unavailable.
