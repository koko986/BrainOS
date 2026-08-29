# MARLIN / BrainOS

A local-first, multilingual, JARVIS-style assistant. MARLIN listens for a wake
word, answers out loud, and has full read and write access to the machine it
runs on.

## What MARLIN can do

- **Hands-free voice.** An offline wake word ("Marlin") opens the microphone,
  then Groq `whisper-large-v3-turbo` transcribes the command.
- **Full file control.** Read, create, overwrite, edit, move, copy, and delete
  any file or folder, plus recursive name and content search.
- **Open things.** Launch any app or open any file or folder in its default app.
- **Knowledge graph.** SQLite-backed entities and relationships, with folder
  indexing and search.
- **Symbolic reasoning.** Prolog rules infer important and high-priority tasks
  and explain why.
- **Spoken replies.** Windows `System.Speech` output, no API key needed.

MARLIN acts without asking for confirmation. See
[Safety](#safety-and-what-marlin-will-not-do) below.

## Setup

Python 3.11 or newer.

```bash
pip install -r requirements.txt
python scripts/setup_voice.py
```

`setup_voice.py` downloads the ~40 MB offline wake-word model into `models/`.

Copy `.env.example` to `.env` and add a Groq API key from
<https://console.groq.com/keys>:

```bash
GROQ_API_KEY=your_key_here
```

For the reasoning commands, install SWI-Prolog and make sure `swipl` is on PATH.
Everything else works without it.

Verify the whole stack:

```bash
python scripts/smoke_check.py    # model, tool calling, file access, microphone
python scripts/check_speech.py   # transcription and wake-word detection
```

## Running

```bash
py main.py                 # hands-free voice mode (default)
py main.py marlin          # interactive terminal
py main.py desktop         # desktop cockpit window
py main.py serve           # local web dashboard
```

Or double-click `run_marlin.bat`.

In hands-free mode, say "Marlin", wait for the acknowledgement, then speak your
request. Say "stand down" or press Ctrl+C to stop.

In terminal mode, type requests directly. `listen` captures a single spoken
command, `hands free` switches to wake-word mode, `voice off` mutes replies, and
`help` lists examples.

## Example requests

```
read C:\Users\me\notes\todo.md
add a line to my desktop file ideas.txt saying call the bank
change every "localhost" to "127.0.0.1" in config.ini
find every invoice PDF in my Documents folder
search my project folder for the text TODO
delete C:\Users\me\Downloads\old-installer.exe
open notepad
index this folder: C:\Users\me\Projects
high priority tasks
why high priority task_finish_graph_interface
```

Burmese and mixed-language input work too; MARLIN replies in the language you
used.

## CLI

```bash
python -m second_brain.app.main jarvis
python -m second_brain.app.main marlin
python -m second_brain.app.main ask "what should I work on today?"
python -m second_brain.app.main seed-demo
python -m second_brain.app.main list-entities
python -m second_brain.app.main list-relationships
python -m second_brain.app.main reason important-tasks
python -m second_brain.app.main reason high-priority
python -m second_brain.app.main reason why-high-priority task_finish_graph_interface
python -m second_brain.app.main serve
python -m second_brain.app.main desktop
```

## How it fits together

```
microphone -> offline wake word (Vosk) -> recording with acoustic endpointing
           -> Groq whisper-large-v3-turbo -> MarlinAgent tool loop
           -> filesystem / knowledge graph / Prolog / app launching
           -> spoken reply
```

The agent loop lives in `second_brain/ai/agent.py`, the tool schemas and
dispatch in `second_brain/ai/tools.py`, unrestricted file operations in
`second_brain/computer/filesystem.py`, and voice capture in
`second_brain/app/voice.py`. `second_brain/app/assistant.py` routes each
request to the fastest capable handler, so deterministic brain and computer
commands never wait on a model call.

## Safety and what MARLIN will not do

MARLIN runs in full autonomous mode. There is no confirmation prompt, no path
sandbox, and no allowlist on file operations. A misheard command can overwrite
or permanently delete real files, including in Windows system directories.
Deletions do not go to the Recycle Bin.

- Every action is appended to `data/database/marlin_audit.jsonl`. That log is
  the only record of what MARLIN did.
- Shell and command-prompt execution is refused. MARLIN works through typed
  Python tools instead.
- Tool output is truncated to about 4000 characters per call to stay inside the
  Groq free-tier token budget.
- `.env` holds your API key and is git-ignored. `models/` is git-ignored too.

## Tests

```bash
pytest
```

Prolog integration tests are skipped with a clear reason when SWI-Prolog or
PySWIP is missing.
