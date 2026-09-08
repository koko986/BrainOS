from __future__ import annotations

from marlin.config import MarlinSettings
from marlin.events import EventBus
from marlin.voice import LocalVoiceService
from marlin.runtime import MarlinRuntime
import threading
from types import SimpleNamespace
import pytest
from marlin.voice import FasterWhisperSTT, SpeechStream


def test_known_voice_command_uses_english_whisper(monkeypatch):
    voice = LocalVoiceService(MarlinSettings(), EventBus())
    recordings = []
    monkeypatch.setattr("marlin.voice.record_utterance", lambda **kwargs: recordings.append(kwargs) or b"RIFFaudio")
    monkeypatch.setattr(voice.stt, "transcribe", lambda _wav, **kwargs: ("open chrome", "en", .9))

    result = voice.listen_once()

    assert result["text"] == "open chrome"
    assert result["engine"] == "faster-whisper"
    assert recordings[0]['vosk_model_path'] == str(voice.settings.vosk_model_path)
    assert recordings[0]['device'] is None


def test_stop_cancels_active_input_and_output():
    voice = LocalVoiceService(MarlinSettings(), EventBus())
    voice._listen_cancel.clear()
    voice._cancel.clear()

    voice.stop()

    assert voice._listen_cancel.is_set()
    assert voice._cancel.is_set()


def test_stream_emits_sentences_before_reply_finishes_and_cancel_drops_tail():
    cancel = threading.Event()
    stream = SpeechStream(cancel)
    stream.feed('Hello. Next')
    assert stream.sentences.get_nowait() == 'Hello.'
    assert stream.buffer == ' Next'
    stream.feed(' sentence. ')
    assert stream.sentences.get_nowait() == 'Next sentence.'
    cancel.set()
    stream.feed('Do not speak this.')
    stream.finish()
    assert stream.sentences.get_nowait() is None
    stream.finish()
    assert stream.sentences.empty()


def test_audio_resampled_to_english_playback_format():
    chunk = SimpleNamespace(sample_rate=16000, sample_channels=1, audio_int16_bytes=b'\0\0' * 16000)
    converted = LocalVoiceService._playback_chunk(chunk)
    assert converted.sample_rate == 22050
    assert converted.sample_channels == 1
    assert abs(len(converted.audio_int16_bytes) - 44100) <= 4


def test_audio_device_failure_releases_synthesis_lock(monkeypatch):
    voice = LocalVoiceService(MarlinSettings(), EventBus())
    chunk = SimpleNamespace(sample_rate=22050, sample_channels=1, audio_int16_bytes=b'\0\0' * 100)
    monkeypatch.setattr(voice, '_load_piper', lambda: SimpleNamespace(synthesize=lambda text: iter([chunk] * 20)))
    monkeypatch.setattr(voice._fallback, 'speak', lambda text: None)
    monkeypatch.setattr(voice._fallback, 'wait', lambda: None)
    monkeypatch.setattr('sounddevice.RawOutputStream', lambda **kwargs: (_ for _ in ()).throw(OSError('output disconnected')))
    cancel = threading.Event()
    voice._speak_worker('Hello.', cancel)
    assert cancel.is_set()
    assert voice._piper_lock.acquire(timeout=2)
    voice._piper_lock.release()


def test_whisper_has_no_hidden_temperature_retries(monkeypatch):
    stt = FasterWhisperSTT(MarlinSettings())
    calls = []
    def transcribe(path, **kwargs):
        calls.append(kwargs)
        return iter([]), SimpleNamespace(language='en')
    monkeypatch.setattr(stt, '_model', lambda: SimpleNamespace(transcribe=transcribe))
    stt.transcribe(b'audio')
    assert calls[0]['language'] == 'en'
    assert calls[0]['temperature'] == 0.0
    assert calls[0]['max_new_tokens'] == 64


@pytest.mark.parametrize('text', ['open chrome', 'what time is it'])
def test_low_confidence_transcript_runs_without_retry(monkeypatch, text):
    voice = LocalVoiceService(MarlinSettings(), EventBus())
    calls = []
    monkeypatch.setattr('marlin.voice.record_utterance', lambda **kwargs: b'audio')
    def transcribe(wav, **kwargs):
        calls.append((wav, kwargs))
        return text, 'en', .2
    monkeypatch.setattr(voice.stt, 'transcribe', transcribe)
    result = voice.listen_once()
    assert not result['requires_clarification']
    assert result['low_confidence']
    assert MarlinRuntime.voice_result_ready(result)
    assert len(calls) == 1


def test_empty_transcript_retries_once_and_requires_correction(monkeypatch):
    voice = LocalVoiceService(MarlinSettings(), EventBus())
    calls = []
    monkeypatch.setattr('marlin.voice.record_utterance', lambda **kwargs: b'audio')
    def transcribe(wav, **kwargs):
        calls.append((wav, kwargs))
        return '', 'en', .0
    monkeypatch.setattr(voice.stt, 'transcribe', transcribe)
    result = voice.listen_once()
    assert result['requires_clarification']
    assert not MarlinRuntime.voice_result_ready(result)
    assert len(calls) == 2 and calls[1][1] == {'beam_size': 3}


def test_cancelled_transcription_is_not_executed(monkeypatch):
    voice = LocalVoiceService(MarlinSettings(), EventBus())
    monkeypatch.setattr('marlin.voice.record_utterance', lambda **kwargs: b'audio')
    def transcribe(wav, **kwargs):
        voice._listen_cancel.set()
        return 'open chrome', 'en', .95
    monkeypatch.setattr(voice.stt, 'transcribe', transcribe)
    result = voice.listen_once()
    assert result['cancelled'] and not MarlinRuntime.voice_result_ready(result)


def test_uncertain_camera_transcript_is_not_executed(tmp_path, monkeypatch):
    runtime = MarlinRuntime(MarlinSettings(database_path=tmp_path / 'brain.db', voice_output=False,
        auto_index_c_drive=False, weather_enabled=False), start_background=False)
    monkeypatch.setattr(runtime.voice, 'listen_once', lambda: {
        'text': 'open camera', 'confidence': .31, 'low_confidence': True,
        'requires_clarification': False,
    })
    monkeypatch.setattr(runtime.actions, 'invoke', lambda *args: (_ for _ in ()).throw(AssertionError('camera opened')))

    result = runtime.listen(execute=True)

    assert result['blocked_action'] == 'open_camera'
    assert result['requires_clarification']
    assert 'result' not in result


def test_clear_camera_transcript_still_opens_immediately(tmp_path, monkeypatch):
    runtime = MarlinRuntime(MarlinSettings(database_path=tmp_path / 'brain.db', voice_output=False,
        auto_index_c_drive=False, weather_enabled=False), start_background=False)
    monkeypatch.setattr(runtime.voice, 'listen_once', lambda: {
        'text': 'open camera', 'confidence': .91, 'low_confidence': False,
        'requires_clarification': False,
    })
    opened = []
    monkeypatch.setattr(runtime.actions, 'invoke', lambda name, args: opened.append(name) or __import__(
        'marlin.actions', fromlist=['ActionOutcome']).ActionOutcome(True, 'opened'))

    result = runtime.listen(execute=True)

    assert result['result']['ok']
    assert opened == ['open_camera']


def test_quality_score_rejects_repetition_and_bad_recognition():
    score = FasterWhisperSTT._confidence
    good = SimpleNamespace(no_speech_prob=.01, avg_logprob=-.1, compression_ratio=1)
    bad = SimpleNamespace(no_speech_prob=.01, avg_logprob=-2, compression_ratio=1)
    repeat = SimpleNamespace(no_speech_prob=.01, avg_logprob=-.1, compression_ratio=3)
    quiet = SimpleNamespace(no_speech_prob=.9, avg_logprob=-.1, compression_ratio=1)
    assert score([good]) > .55
    assert all(score([item]) < .55 for item in (bad, repeat, quiet))


def test_old_language_preference_ignored_and_nonempty_transcript_dispatches(tmp_path, monkeypatch):
    settings = MarlinSettings(database_path=tmp_path / 'brain.db', voice_output=False)
    runtime = MarlinRuntime(settings, start_background=False)
    runtime.store.set_state('voice_input_language', 'my')
    again = MarlinRuntime(settings, start_background=False)
    assert again.voice.status()['input_language'] == 'en'
    monkeypatch.setattr(runtime.voice, 'listen_once', lambda: {'text': 'open chrome', 'requires_clarification': True})
    monkeypatch.setattr(runtime, 'command', lambda text, **kwargs: {'message': text})
    assert runtime.listen()['result']['message'] == 'open chrome'


def test_speech_uses_one_stream_for_multiple_sentences(monkeypatch):
    voice = LocalVoiceService(MarlinSettings(), EventBus())
    streams = []
    written = []

    class Stream:
        def __init__(self, **kwargs):
            streams.append(self)
        def start(self):
            pass
        def write(self, audio):
            written.append(audio)
        def close(self):
            pass
        def stop(self):
            pass

    chunks = [SimpleNamespace(sample_rate=22050, sample_channels=1, audio_int16_bytes=b'\0\0' * 200) for _ in range(2)]
    monkeypatch.setattr(voice, '_load_piper', lambda: SimpleNamespace(synthesize=lambda text: iter(chunks)))
    monkeypatch.setattr('sounddevice.RawOutputStream', Stream)
    voice._speak_worker('Good morning. How can I help?', threading.Event())
    assert len(streams) == 1
    assert len(written) == 2


def test_cancel_during_synthesis_never_plays_late_audio(monkeypatch):
    voice = LocalVoiceService(MarlinSettings(), EventBus())
    cancel = threading.Event()
    entered = threading.Event()
    release = threading.Event()

    def synthesize(text):
        entered.set()
        release.wait(2)
        yield SimpleNamespace(sample_rate=22050, sample_channels=1, audio_int16_bytes=b'\0\0')

    monkeypatch.setattr(voice, '_load_piper', lambda: SimpleNamespace(synthesize=synthesize))
    opened = []
    monkeypatch.setattr('sounddevice.RawOutputStream', lambda **kwargs: opened.append(kwargs))
    worker = threading.Thread(target=voice._speak_worker, args=('Hello.', cancel))
    worker.start()
    assert entered.wait(2)
    cancel.set()
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert opened == []


def test_wake_word_starts_continuous_voice_chat(tmp_path, monkeypatch):
    settings = MarlinSettings(
        database_path=tmp_path / "brain.db",
        auto_index_c_drive=False,
        weather_enabled=False,
        voice_output=False,
        wake_word_enabled=True,
    )
    runtime = MarlinRuntime(settings, start_background=False)
    events: list[tuple[str, dict]] = []

    class Listener:
        def prepare(self):
            return None

    monkeypatch.setattr(runtime.voice, "wake_listener", lambda: Listener())
    monkeypatch.setattr(runtime.voice, "wait_for_wake", lambda _listener, stop: stop.set() or True)
    monkeypatch.setattr(runtime.voice, "speak", lambda _text: None)
    monkeypatch.setattr(runtime.voice, "wait", lambda _timeout: None)
    monkeypatch.setattr(runtime, "listen", lambda **_kwargs: {"text": "open chrome"})
    monkeypatch.setattr(runtime, "command", lambda text, source="ui": {"ok": True, "message": f"{source}:{text}", "data": {}, "pending": None, "client_action": None})
    monkeypatch.setattr(runtime.events, "publish", lambda kind, **data: events.append((kind, data)))
    started = []
    monkeypatch.setattr(runtime, 'start_voice_chat', lambda: started.append(True))

    runtime._wake_worker()

    assert ("wake.detected", {"phrase": "Hey MARLIN"}) in events
    assert started == [True]
