from datetime import datetime, UTC, timedelta

from fastapi.testclient import TestClient
from marlin.config import MarlinSettings
from marlin.runtime import MarlinRuntime
from marlin.web import create_app


def test_reminder_delivery_persistence_and_controls(tmp_path):
    settings = MarlinSettings(database_path=tmp_path / 'brain.db', voice_output=False, auto_index_c_drive=False)
    runtime = MarlinRuntime(settings, start_background=False)
    reminder = runtime.store.add_reminder('Review project', datetime.now(UTC) - timedelta(minutes=1))
    unscheduled = runtime.store.add_reminder('Later')
    assert [r['id'] for r in runtime.store.claim_due_reminders()] == [reminder['id']]
    assert not runtime.store.claim_due_reminders()
    runtime.shutdown()
    runtime = MarlinRuntime(settings, start_background=False)
    assert not runtime.store.claim_due_reminders()
    app = create_app(runtime)
    client = TestClient(app)
    headers = {'X-Marlin-Token': app.state.token}
    url = '/api/reminders/' + reminder['id']
    assert client.post(url + '/complete').status_code == 403
    assert client.post(url + '/snooze', json={'minutes': 0}, headers=headers).status_code == 422
    assert client.post(url + '/snooze', json={'minutes': 5}, headers=headers).json()['notified_at'] is None
    assert client.post(url + '/complete', headers=headers).json()['completed'] == 1
    assert len(runtime.store.list_reminders(pending_only=False)) == 2
    assert runtime.store.list_reminders()[0]['id'] == unscheduled['id']
    assert client.post('/api/reminders/missing/complete', headers=headers).status_code == 404
    runtime.shutdown()


def test_v3_migration_preserves_v2_reminders(tmp_path):
    import sqlite3
    from marlin.storage import MarlinStore
    from second_brain.database.connection import initialize_database
    path = tmp_path / 'old.db'
    initialize_database(path)
    with sqlite3.connect(path) as connection:
        connection.executescript("CREATE TABLE marlin_meta(key TEXT PRIMARY KEY, value TEXT); INSERT INTO marlin_meta VALUES('schema_version','2'); CREATE TABLE reminders(id TEXT PRIMARY KEY, text TEXT, due_at TEXT, completed INTEGER DEFAULT 0, created_at TEXT);")
        connection.execute("INSERT INTO reminders VALUES('old','Keep this',NULL,0,'2026-01-01')")
    store = MarlinStore(path)
    assert store.migrate().exists()
    assert store.list_reminders()[0]['text'] == 'Keep this'
    assert store.list_reminders()[0]['notified_at'] is None
    assert store.migrate() is None


def test_due_reminder_plays_chime_and_speaks(tmp_path, monkeypatch):
    settings = MarlinSettings(database_path=tmp_path / 'brain.db', voice_output=False, auto_index_c_drive=False)
    runtime = MarlinRuntime(settings, start_background=False)
    calls = []
    monkeypatch.setattr('marlin.runtime.play_notification_sound', lambda: calls.append('chime') or True)
    monkeypatch.setattr(runtime.voice, 'speak', lambda message: calls.append(message))

    runtime._reminder_fired({'id': 'reminder-1', 'text': 'Review Prolog'})

    assert calls == ['chime', 'Reminder: Review Prolog']
    runtime.shutdown()
