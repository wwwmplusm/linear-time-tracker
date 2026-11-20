import sqlite3
from contextlib import closing

from tracker import db


def test_legacy_intervals_survive_pause_and_history_restart(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript("""
            CREATE TABLE intervals (
                id INTEGER PRIMARY KEY, target TEXT NOT NULL, started_ms INTEGER NOT NULL,
                ended_ms INTEGER CHECK (ended_ms >= started_ms)
            );
            INSERT INTO intervals VALUES (1, 'closed', 100, 600), (2, 'open', 1000, NULL);
        """)

    database = db.Database(path)
    database.initialize()
    database.initialize()
    now = 2000
    monkeypatch.setattr(db, "now_ms", lambda: now)
    active, history, _ = database.snapshot()
    assert db.elapsed_ms(active[0], now) == 1000
    assert db.elapsed_ms(history[0], now) == 500

    database.pause(2, version=0)
    now = 5000
    assert db.elapsed_ms(database.active_timers()[0], now) == 1000
    database.resume(2, version=1)
    now = 5500
    database.pause(2, version=0)
    assert db.running_since(database.active_timers()[0]) == 5000
    database.pause(2, version=2)
    now = 7000
    database.resume(2, version=1)
    assert db.running_since(database.active_timers()[0]) is None
    database.stop(2)
    _, history, _ = database.snapshot()
    assert history[0]["ended_ms"] == 7000
    assert db.elapsed_ms(history[0], now) == 1500

    assert database.restart(2, "restart-key")
    assert database.restart(2, "restart-key")
    new, = database.active_timers()
    assert new["target"] == "open" and db.elapsed_ms(new, now) == 0
    database.stop(new["id"])
    assert database.restart(2, "restart-key")
    assert not database.active_timers()
