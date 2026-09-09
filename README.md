# MARLIN V2 / BrainOS

MARLIN V2 is a fully local, JARVIS-style Windows assistant. Conversation runs
through Ollama and `qwen3:4b-instruct`; voice input uses Faster-Whisper; voice
output uses Piper; knowledge is stored in SQLite; symbolic reasoning uses
SWI-Prolog. No paid service, account, or API key is required.
Telegram is an optional remote interface and uses a free BotFather bot token.

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

## Optional Telegram interface

Create a private bot with Telegram's `@BotFather`, then place its token only in
the ignored `.env` file:

```text
MARLIN_TELEGRAM_ENABLED=true
MARLIN_TELEGRAM_BOT_TOKEN=your_botfather_token
MARLIN_TELEGRAM_BRIEFING_TIME=08:00
```

Pair the owner account with a one-use code:

```powershell
py main.py telegram pair
py main.py telegram unpair
py main.py telegram status
py main.py telegram contacts
py main.py telegram test
```

Send `/pair CODE` to the bot. The paired owner can use `/status`, `/briefing`,
`/reminders`, `/schedule`, `/priority`, `/graph`, `/search topic`, `/research topic`,
and `/contacts`. A contact must first send `/start`; approve and name the request
in the cockpit or with `/name USER_ID Alias`. `tell Alex I will arrive at eight`
creates a two-minute draft with **Send** and **Cancel** controls. It never sends
before owner approval.

The Telegram owner can only be removed locally: open the cockpit's Messaging
panel and choose **Remove owner**, or run `py main.py telegram unpair`. This
immediately revokes remote command access and invalidates unused pairing codes.

Telegram can read safe status, reasoning, reminders, schedules, graph summaries,
and filtered file-search results. Apps outside the remote allow-list, shell,
and file-changing commands are blocked. Camera commands only open or close the
local Windows Camera app; no image or video is sent to Telegram. Interactive
Telegram access and alerts run only while MARLIN is open; local Windows reminder
delivery remains separate.

The paired owner receives a compact Telegram keyboard with Apps, Media, Camera,
and Files menus. Those menus provide tappable controls for:

```text
/open chrome|vscode|canva|youtube|edge|firefox|notepad|calculator|explorer
/open desktop|documents|downloads
/camera open
/camera close
/play youtube relaxing music
/volume 0-100
/pause  /next  /previous  /mute
/search report.pdf
/reminders
/priority
/lock
```

Only the listed applications and folders may be opened remotely. Camera actions
open or close the local Windows Camera app; they never transmit camera content
through Telegram. Shutdown, arbitrary applications, shell commands, and file
modifications remain blocked. `/lock` locks the Windows session immediately.

The desktop cockpit opens the native `pywebview` window when available and
falls back to the browser at `http://127.0.0.1:8765`.

The desktop and browser cockpit listen locally for **"Hey MARLIN"**. After
MARLIN answers, speak one command or question. The wake-word detector uses
Vosk and does not send microphone audio to an online service.

## Local functions

- Real local conversation with streamed Ollama output and persistent context.
- English voice input, local wake word, and cancellable, sentence-streamed British male speech.
- SQLite brain graph, incremental C-drive indexing, and FTS file search.
- Prolog priority, blocked/overdue task, dependency, and project-focus reasoning.
- Alarms, reminders, snooze, standby/wake state, morning briefings, and media controls.
- Typed file, folder, app, camera, and media actions.
- Native Windows Camera launch, local video playback through the default player, and local graph explanations.
- Prolog CLP(FD) day planning with deadlines, dependencies, working hours, fixed events, recurring events, conflict detection, and preview-before-apply.
- Reversible preference memory: explicit preferences apply immediately; behavioural patterns require three observations; secrets and sensitive categories are rejected.
- Incremental file understanding for source, text, Markdown, JSON, YAML, CSV, PDF, and DOCX files under Desktop, Documents, Downloads, and the project root.
- Explicit keyless internet research with public-page limits, robots.txt checks, citations, and Prolog evidence ranking.
- A Windows `MARLIN Reminder Runner` task, configured by setup, delivers due local reminders after the cockpit exits.

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
open camera
play C:\Videos\demo.mp4
play the video file
explain my graph
play music
hide MARLIN
turn yourself off
stand by
I prefer coding in the morning
schedule my Prolog report before Friday
plan my day
apply plan
show schedule conflicts
explain this project BrainOS
which files are related to main.py
what could be affected if I change main.py
research SWI-Prolog CLP(FD) scheduling
what do you remember about me
```

The cockpit includes movable **Schedule**, **Memory**, **Research**, and
**Prolog** panels. The Prolog panel shows the predefined predicate, selected
facts, matched rules, result, and readable proof. SQLite remains the only
persistent source of truth; Python validates all facts before invoking Prolog.

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
# Graph Folder Scope

The cockpit graph shows only `C:\Projects\Projects` and its nested folders and files.
Set `MARLIN_GRAPH_ROOT` to change this directory. Automatic startup indexing uses
the same root; unrelated stored knowledge is preserved but hidden from the graph.
All indexed folders are retained in the view, while file previews are capped.
System, hidden, dependency folders and filesystem links are skipped during indexing.

## Model speed

MARLIN defaults to `qwen3:4b-instruct` with `MARLIN_OLLAMA_THINK=false`. This is
the fastest practical option for a 4 GB GPU. The installed `qwen3:8b` model can
be selected with `MARLIN_OLLAMA_MODEL=qwen3:8b`, but it uses more system memory
and replies more slowly when it cannot fit fully in GPU memory.
