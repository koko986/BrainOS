# YouTube Playback

Say "open YouTube and play some music", "play jazz on YouTube", or "play music".
These commands bypass the language model and use Python-controlled Chrome to
search YouTube, select the first video, and verify that its player is running
with audio enabled. Generic music defaults to a "relaxing music" search.
Other phrasings can use the typed play_youtube tool through the local model.

Chrome opens visibly with a separate MARLIN profile under the database directory
in browser-media. It does not modify your personal Chrome profile or copy cookies.
This profile is excluded from Git. Playwright uses the installed Chrome browser;
install requirements.txt if the dependency is missing.

YouTube may require sign-in, a bot verification, or a cookie/privacy choice.
MARLIN reports that requirement rather than bypassing it or falsely saying music
is playing. Complete the website's step in that window and repeat the request.
Opening and playing music do not require MARLIN approval. This is a specific
YouTube workflow, not unrestricted browser control or arbitrary shell access.
