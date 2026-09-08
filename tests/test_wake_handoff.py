import io
import threading
import wave
from types import SimpleNamespace

from second_brain.app import voice


def test_continuous_wake_preserves_phrase_and_command(monkeypatch):
    block = b'\x00\x04' * 4000
    silence = bytes(8000)
    chunks = [block] * 8 + [silence] * 3
    class Recognizer:
        resets = 0
        count = 0
        def Reset(self): self.resets += 1
        def AcceptWaveform(self, chunk):
            self.count += 1
            return False
        def PartialResult(self):
            return '{"partial": "hey marlin"}' if self.count >= 5 else '{"partial": "hey"}'
    class Stream:
        def __init__(self, **kwargs): self.callback = kwargs['callback']
        def __enter__(self):
            for chunk in chunks: self.callback(chunk, 4000, None, None)
            return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(voice, '_import_sounddevice', lambda: SimpleNamespace(RawInputStream=Stream))
    listener = voice.WakeWordListener('', ['hey marlin'])
    recognizer = Recognizer()
    listener._recognizer = recognizer
    detected = []
    assert listener.wait_for_wake(capture_command=True, on_detected=lambda: detected.append(True))
    assert detected == [True]
    assert recognizer.resets == 2
    with wave.open(io.BytesIO(listener.handoff_wav)) as wav:
        assert wav.readframes(wav.getnframes()) == b''.join(chunks[:-1])


def test_cancelled_wake_does_not_deliver_audio(monkeypatch):
    cancelled = threading.Event(); cancelled.set()
    class Stream:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(voice, '_import_sounddevice', lambda: SimpleNamespace(RawInputStream=Stream))
    listener = voice.WakeWordListener('', ['hey marlin'])
    listener._recognizer = SimpleNamespace(Reset=lambda: None)
    assert not listener.wait_for_wake(cancel_event=cancelled, capture_command=True)
    assert listener.handoff_wav == b''


def test_handoff_transcript_strips_wake_without_recording_again(tmp_path, monkeypatch):
    from marlin.config import MarlinSettings
    from marlin.events import EventBus
    from marlin.voice import LocalVoiceService
    service = LocalVoiceService(MarlinSettings(voice_output=False), EventBus())
    service._handoff_wav = b'recorded wake and command'
    monkeypatch.setattr(service.stt, 'transcribe', lambda audio, **kwargs: ('Hey Marlin, open Chrome.', 'en', .95))
    monkeypatch.setattr('marlin.voice.record_utterance', lambda **kwargs: (_ for _ in ()).throw(AssertionError('audio lost')))
    assert service.listen_once()['text'] == 'open Chrome.'


def test_observed_accented_greeting_does_not_retry_or_execute_uncertain_targets(monkeypatch):
    from marlin.config import MarlinSettings
    from marlin.events import EventBus
    from marlin.voice import LocalVoiceService
    service = LocalVoiceService(MarlinSettings(voice_output=False), EventBus())
    service._handoff_wav = b'audio'
    calls = []
    def transcribe(audio, **kwargs):
        calls.append(kwargs)
        return 'Hey, Marlene. Hello.', 'en', .478
    monkeypatch.setattr(service.stt, 'transcribe', transcribe)
    result = service.listen_once()
    assert result['text'] == 'Hello.' and not result['requires_clarification']
    assert len(calls) == 1
    assert not service._clear_greeting('open Documents', .478)


def test_verified_wake_only_skips_whisper(monkeypatch):
    from marlin.config import MarlinSettings
    from marlin.events import EventBus
    from marlin.voice import LocalVoiceService
    service = LocalVoiceService(MarlinSettings(voice_output=False), EventBus())
    service._handoff_wav = b'audio'
    monkeypatch.setattr('marlin.voice.VoskSTT.transcribe', lambda self, audio: 'hey molly')
    monkeypatch.setattr(service.stt, 'transcribe', lambda *a, **k: (_ for _ in ()).throw(AssertionError('unnecessary Whisper')))
    assert service.listen_once()['wake_only']
