# Conversation Controls

MARLIN defaults to brief English answers (one or two sentences, with a 45-word
prompt target). Explicit requests for detail can produce longer answers.

Use **Talk now** to stop current speech, invalidate the current model reply and
resume voice chat. **Stop voice** stops audio and invalidates the reply without
starting a new chat. Submitting a typed command also interrupts old speech.

Cancellation is checked as model tokens arrive and before executing subsequent
model-selected tools. A stalled model connection can delay the chat worker until
the next token or network timeout; this is not full-duplex acoustic interruption.
Completed computer actions are not undone by interruption.

The cockpit keeps System and Reminders behind compact toggles. A new or fired
reminder reveals its timeline. The graph and movable conversation overlay remain.

Music commands use MARLIN's persistent Chrome profile, falling back to a separate
Edge profile if Chrome cannot launch. Existing Chrome sign-in data in MARLIN's
profile is retained; everyday browser profiles are not copied or modified.
Firefox automation is not implemented. A YouTube sign-in or anti-bot gate must be
resolved by the user in the browser. MARLIN reports success only after verifying
active, unmuted video playback.

Verification: 26 focused tests passed for reply interruption, token protection,
voice chat, YouTube routing/fallback and backend behavior. Desktop and mobile
cockpit rendering and Talk now activation were checked live. The live YouTube
test selected a video but playback was blocked by a sign-in requirement.
