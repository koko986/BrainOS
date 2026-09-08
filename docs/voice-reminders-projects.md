# Voice, Reminders, and Projects

Say "Hey MARLIN" to begin an English voice conversation. The wake listener uses
the selected microphone (system default unless changed) continuously and retains
audio spoken immediately after the wake phrase. Recognised speech appears in the
command bar and clear requests submit automatically. A typed draft is preserved.

After one unsuccessful recognition retry, MARLIN shows the transcript without
executing it and listens again automatically, without repeated spoken prompts.
Silence does not end the session. Stop voice cancels input and audio;
End voice chat ends the session. Speech remains turn-taking, not full-duplex.

The Reminders & Alarms overlay shows local dates, timezone, status, completion,
five-minute snooze, and storage details. Reminders live in MARLIN's SQLite database,
not an external calendar. MARLIN must be running for delivery. Due reminders are
claimed once in persistent storage; overdue pending reminders remain visible after
restart. Completed items remain in history. Schema version 3 adds notified_at and
backs up existing databases before migration. A crash after claiming but before
displaying an alert does not retry that alert; the overdue item remains visible.

The graph is restricted to MARLIN_GRAPH_ROOT (C:\Projects\Projects by default).
Immediate child directories receive larger coloured project labels. Hover or
select a node for its full name. Nested labels are suppressed when they collide.

New token-protected endpoints:

- POST /api/reminders/{id}/complete
- POST /api/reminders/{id}/snooze with {"minutes": 5}

State includes reminder_storage, voice_chat_paused, and voice.wake_status.
New events: wake.acknowledged, voice.chat.paused, voice.chat.resumed,
reminder.updated, and reminder.fired. Existing voice and command endpoints remain.
