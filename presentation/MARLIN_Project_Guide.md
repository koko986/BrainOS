# MARLIN: Project Report and Six-Member Presentation

Prepared from the current source on 7 September 2026.

## 1. Project Overview

MARLIN is a local Windows personal assistant combining natural-language conversation, voice interaction, a visual knowledge graph, persistent memory, and rule-based Prolog reasoning. Python coordinates these components and executes supported computer actions.

The main objective is to make personal projects, tasks, files, and routine commands accessible through one desktop command center. It is a working student-project assistant, not unrestricted artificial general intelligence or a replacement for every desktop application.

Launch the desktop cockpit with `py main.py`. The native window uses a local backend; a browser fallback is available. Terminal mode is `py main.py terminal`.

## 2. What MARLIN Can Do

| Area | Implemented capability | Important boundary |
|---|---|---|
| Conversation | Local Ollama/Qwen conversation, streamed responses, stored conversation context | Requires downloaded model and running Ollama; responses can be wrong |
| Voice | Hey MARLIN wake detection, English transcription, spoken English replies, continuing turn-taking chat | Not full-duplex; accent, microphone noise and hardware affect accuracy |
| Desktop cockpit | Command input, replies, movable overlays, camera preview and live state updates | Camera and microphone require working local hardware |
| Knowledge graph | Folder/file nodes, relationships, project colours, zoom, pan, drag and connection highlighting | Current graph scope is C:\Projects\Projects, not the whole laptop |
| File knowledge | Incremental metadata/snippet indexing and filename/content search | Protected, unreadable and excluded folders are skipped |
| Symbolic reasoning | Important/high-priority tasks, overdue/blocked tasks, current focus, dependency chains and explanations | Conclusions depend on supplied facts and explicit rules |
| Reminders and alarms | SQLite persistence, reminder timeline, exact times, snooze, completion and local notifications | MARLIN must be running; no external calendar integration |
| Computer actions | Supported apps, websites, folders, files, media and camera commands | Python allow-list and validation; no arbitrary shell execution |
| File changes | Supported creation and confirmation-based modification operations | Destructive changes and app closing require approval |
| Resident mode | Hide/show cockpit while background assistant continues; explicit exit | Closing the window is not the same as exiting MARLIN |
| YouTube | Search/select a video and attempt playback in a dedicated Chrome session | Website sign-in/anti-bot checks can block playback |

Core conversation and voice do not need a paid API key after local model setup. Websites and optional weather still need internet. Burmese speech is not part of the current English-only configuration.

## 3. Architecture

```text
Typed command or microphone
        |
        v
Desktop UI / terminal -> Python runtime and command router
                          |
                          +-> Known local command -> validated Python action
                          +-> Task reasoning -> SQLite -> PySWIP -> SWI-Prolog
                          +-> Conversation -> local Ollama / Qwen
                          +-> Reminder / context -> SQLite
                          |
                          v
                    Structured result
                          |
                          +-> Text, graph, timeline and status events
                          +-> Local English speech
```

| Component | Responsibility | Main source location |
|---|---|---|
| Launcher and desktop host | Startup, native window, background lifetime | main.py, marlin/cli.py, marlin/desktop.py |
| Shared assistant pipeline | Command routing, sessions, events | marlin/runtime.py |
| Local conversation model | Ollama requests and streamed model replies | marlin/local_model.py |
| Voice | Wake handoff, transcription, speech queue/cancellation | marlin/voice.py, second_brain/app/voice.py |
| Computer actions | Typed tools, validation and approvals | marlin/actions.py |
| Browser media | Owned Chrome YouTube playback session | marlin/browser_media.py |
| Persistence and routines | SQLite tables, context, reminders and scheduler | marlin/storage.py, marlin/routine.py |
| Backend and UI | Local FastAPI, WebSocket events and cockpit | marlin/web.py, marlin/ui/ |
| Knowledge foundation | Entities, relationships and services | second_brain/database/, second_brain/knowledge/ |
| Symbolic reasoning | Python bridge and result explanations | second_brain/reasoning/ |
| Logic program | Facts, relationship declarations, rules and queries | prolog/ |

## 4. Exactly What Prolog Does

Prolog is the symbolic inference engine. It answers predefined logical questions from facts instead of generating plausible text. SQLite is the canonical store; Python loads relevant project/task/technology entities and semantic relationships into Prolog through PySWIP before querying it.

### Files

- `prolog/facts.pl`: dynamic entity and attribute predicate declarations.
- `prolog/relationships.pl`: relationship predicate declarations.
- `prolog/rules.pl`: actual inference rules.
- `prolog/queries.pl`: predefined query wrappers.
- `prolog/reasoning.pl`: consults the other Prolog files.
- `second_brain/reasoning/prolog_engine.py`: validates atoms, synchronizes facts and exposes fixed query methods.
- `second_brain/reasoning/service.py`: retrieves SQLite knowledge, calls the engine and maps results to entities and readable explanations.

### Actual Rules

| Predicate | Meaning in the current code |
|---|---|
| active_project/1 | Entity is a project and has an active fact |
| important_task/1 | Task belongs to an active project |
| high_priority/1 | Important task has a soon deadline OR has a dependency |
| high_priority_reason/2 | Returns matched reasons such as active project, deadline or dependency |
| blocked_task/1 | Task has an explicit blocked fact |
| overdue_task/1 | Task has an explicit overdue fact |
| dependency_chain/2 | Follows direct and indirect depends_on relationships |
| current_project_focus/1 | Active focused project; if no focused fact exists, active projects are candidates |
| morning_priority/1 | High-priority tasks OR overdue tasks |
| morning_priority_reason/2 | Explains matching overdue, blocked or high-priority properties for morning priorities |

Important: blocked alone does not make a task a morning priority. The current bridge supplies deadline_soon, overdue, blocked and current_focus from metadata; Prolog does not independently inspect the clock, read files or discover these attributes. A dependency currently increases priority by policy; this is not a full critical-path scheduling algorithm. Recursive dependency traversal should use acyclic data because the current rules do not include cycle protection.

### Simple Demonstration of Inference

These illustrative facts show how the existing rules work:

```prolog
project(project_marlin).
active(project_marlin).
task(task_voice_demo).
belongs_to(task_voice_demo, project_marlin).
deadline_soon(task_voice_demo).

important_task(Task) :-
    task(Task),
    belongs_to(Task, Project),
    active_project(Project).

high_priority(Task) :-
    important_task(Task),
    deadline_soon(Task).
```

Query: `high_priority(task_voice_demo).` Result: true.

Explanation: the item is a task, it belongs to an active project, therefore it is important; its deadline is soon, therefore it is high priority. This is a rule-supported conclusion, not an LLM guess.

### What Prolog Does Not Do

It does not transcribe speech, speak audio, generate general conversation, open Chrome, render the graph, or control Windows. Python performs those integrations. The graph's folder containment and file-similarity connections are not all Prolog deductions.

Presentation sentence: "Our project uses a hybrid architecture. Qwen handles natural-language conversation, Prolog derives explainable task conclusions from facts and rules, SQLite stores knowledge, and Python validates and executes supported actions."

## 5. Six-Member Presentation Allocation

Suggested duration: 18 minutes, approximately 3 minutes each. Replace Member 1-6 with your actual names. These are presentation responsibilities, not claims about who originally wrote each module.

### Member 1: Introduction and Project Purpose

Slides 1-2: title, problem and objectives.

Explain fragmented personal files/tasks and the need for one assistant. Introduce local operation, privacy, voice interaction and the knowledge graph. Distinguish a useful assistant from unrestricted computer control.

Suggested opening: "MARLIN is our local personal assistant. It brings conversation, files, reminders and explainable task reasoning into one desktop interface. Our aim is to reduce the effort of finding information and performing common tasks."

Demo: launch the cockpit and identify the command bar and graph.

Handoff: "Next, we will explain how these capabilities share one backend."

### Member 2: Architecture and Data Management

Slides 3-4: architecture diagram and SQLite knowledge model.

Explain Python coordination, UI/backend events, entities and relationships, persistent conversations and reminders. Show project -> folder -> file containment and explain indexing versus reading every file. Identify SQLite as the source of truth.

Demo: navigate the graph, select a main project and show its connections.

Prepare: marlin/runtime.py, marlin/storage.py, second_brain/database/ and knowledge service.

Handoff: "Stored knowledge becomes useful for logical decisions through Prolog."

### Member 3: Prolog Reasoning and Explainability

Slides 5-6: facts/rules/queries and one inference example.

Explain SWI-Prolog, PySWIP, important_task and high_priority, then demonstrate an explanation. Mention current focus, overdue tasks and dependency chains. Clearly distinguish metadata flags from inferred conclusions.

Demo: run the isolated seed/priority/explanation commands below.

Prepare: prolog/rules.pl, prolog_engine.py, service.py and test_prolog_reasoning.py.

Handoff: "Prolog provides logic; the conversation and voice components make it accessible naturally."

### Member 4: Local AI and Voice Conversation

Slides 7-8: local model and voice pipeline.

Explain Qwen via Ollama, Vosk wake detection, Faster-Whisper English transcription and Piper speech. Describe streaming, local command fast paths, recognition uncertainty and Stop voice. Explain that turn-taking chat resumes listening after MARLIN finishes speaking.

Demo: say "Hey MARLIN, hello", ask a short follow-up, then stop speech. Keep typed input ready as a fallback.

Prepare: marlin/local_model.py, marlin/voice.py and second_brain/app/voice.py.

Handoff: "Natural language reaches the computer through a controlled Python action layer."

### Member 5: Actions, Reminders and Safety

Slides 9-10: supported actions, reminder timeline and approvals.

Explain direct opening of apps/websites, local reminders, camera, supported file actions, exact targets and destructive-action approval. Describe local storage and why the assistant must remain running for notifications.

Demo: "open Chrome", create "remind me in five minutes to rehearse", show its saved location/time in the timeline. Use disposable files only when demonstrating approvals.

Prepare: marlin/actions.py, marlin/routine.py, marlin/storage.py and browser_media.py.

Handoff: "Finally, we will discuss verification, current limits and improvements."

### Member 6: Testing, Limitations and Conclusion

Slides 11-12: test evidence, limitations and future work.

Explain isolated test databases, mocked hardware tests versus live checks, voice cancellation, reminder persistence and action safety. State current limitations: local-model latency, noise sensitivity, English-only speech, YouTube sign-in restrictions and the need for downloaded dependencies.

Report verified evidence: on 7 September 2026, `py -m pytest tests/test_prolog_reasoning.py -q` completed with 3 passed. This confirms the tested Prolog integration in this Python environment, not every subsystem or every possible input.

Possible future work: robust dependency-cycle handling, better multilingual speech, broader accessibility and more controlled application integrations.

Close: "MARLIN combines local language understanding with explicit logical reasoning and controlled execution. The key contribution is not just conversation, but connecting knowledge, explanations and useful actions in one system."

## 6. Safe Demonstration Script

Use a separate database for the Prolog demonstration so personal brain data is untouched:

```powershell
py main.py --db data/database/presentation_demo.db seed-demo
py main.py --db data/database/presentation_demo.db reason important-tasks
py main.py --db data/database/presentation_demo.db reason high-priority
py main.py --db data/database/presentation_demo.db reason why-high-priority task_finish_graph_interface
```

Other demonstration commands: `hello marlin`, `show brain graph`, `open Chrome`, `open YouTube`, `high priority tasks`, `stop voice`, `end voice chat`. Do not rely on live YouTube playback if the dedicated browser requests sign-in. Rehearse voice on the actual room microphone and keep a text fallback.

## 7. Likely Teacher Questions

**Why Prolog if you already have an LLM?** Prolog gives reproducible conclusions from explicit facts/rules and matched explanations; the LLM is useful for flexible conversation but can hallucinate.

**Is this machine learning?** It combines pretrained neural models with symbolic logic. We integrate existing models; we did not train Qwen or Whisper from scratch.

**Where is the knowledge stored?** SQLite. Prolog receives a synchronized subset for reasoning, and the UI receives graph snapshots.

**Does Prolog create the visual graph?** No. Python supplies graph data and the frontend draws it. Prolog separately reasons over semantic task/project relationships.

**Is it completely offline?** Core local models can operate offline once installed. Downloads, websites and optional weather need internet.

**Can it do anything on the laptop?** No. It supports implemented typed actions with validation and confirmation for risky operations, not arbitrary shell access.

**Can reminders run with the computer shut down?** No. Records persist, but delivery requires MARLIN to run. Overdue records can be shown after restart.

**Is voice equivalent to ChatGPT Voice?** No. It is local turn-taking voice conversation; responsiveness depends on hardware and recognition quality, and it does not provide full-duplex interruption.

## 8. Softcopy Contents and Installation

The ZIP contains current source, UI assets, Prolog files, tests, dependency list, example configuration and this guide. It intentionally excludes private .env settings, personal databases, indexed file contents, browser sessions, logs, downloaded models, virtual environments and Git history. The original local data is not deleted or changed.

On the receiving Windows machine, extract the archive, open a terminal in its MARLIN folder, install a compatible Python and the external Ollama and SWI-Prolog runtimes, then run:

```powershell
py -m pip install -r requirements.txt
py main.py setup
py main.py doctor
py main.py
```

Ensure SWI-Prolog is on PATH and matches Python architecture. Initial model setup requires internet and several GB of storage. No local model weights are bundled in this source ZIP. Check setup output for missing prerequisites before presenting.

Optional local configuration: copy .env.example to .env and adjust graph root for the receiving machine. A machine without C:\Projects\Projects will not have this project's indexed graph automatically. Index a suitable demonstration folder instead; the package does not contain your laptop's files.

Run tests with `py -m pytest`. Hardware/model-dependent tests can require prerequisites or skip when unavailable. The package is a source handover, not a standalone installer or a guarantee that all tests pass on an unconfigured machine.
