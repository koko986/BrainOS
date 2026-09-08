# English voice

MARLIN recognizes English with local Faster-Whisper and speaks with Piper Alan.
Vosk detects the start/end of English speech, including quiet commands; Whisper
still produces the final transcript. System default uses Windows' default input
device, not a separately selected microphone. Playback stops before listening.
Recognition skips automatic language detection and hidden temperature retries.
Unclear speech gets one bounded retry, then a correction prompt without executing.

Normal conversation streams completed sentences to speech while Qwen generates
the rest. Local commands bypass Qwen; tool responses wait for validated results.
Stop voice cancels both active and queued audio. Voice models preload at launch.

The multilingual selector, Burmese speech model integration, and its setup
dependencies have been removed. Existing brain data and conversations remain intact.
Any older saved input-language setting is ignored; recognition always uses English.

## Voice Chat

Click Voice chat, or say Hey MARLIN, to begin an ongoing conversation. MARLIN
speaks, then listens automatically for the next turn. End voice chat or say
"end voice chat" to stop. Stop voice interrupts playback without ending the
session. This is turn-based local voice, not full-duplex talk-over detection.

Unclear speech is shown for correction; MARLIN listens again without requiring typing.
Normal conversation requires no approval. A pending file-changing action is
read aloud with its target; say "approve action" or "cancel action". Approval
must have high recognition confidence and still uses the existing expiring,
one-use token and target checks. Ending voice chat cancels its pending action.
Silence does not end voice chat. Temporary input errors retry with a bounded
backoff, and a failed request does not end the conversation. Explicitly end
voice chat when finished; the microphone remains active between turns otherwise.
