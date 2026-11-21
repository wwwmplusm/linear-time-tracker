import sqlite3
from contextlib import closing

import pytest

from tracker import db
from tracker.config import Target


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

@pytest.mark.parametrize("accept", [False, True])
def test_pomodoro_completes_offline_and_settles_once(tmp_path, monkeypatch, accept):
    path = tmp_path / "pomo.sqlite3"
    database = db.Database(path)
    database.initialize()
    target = Target("task", "Task", "issue")
    now = 1000
    monkeypatch.setattr(db, "now_ms", lambda: now)
    database.start(target, "first", pomo_minutes=60)
    now += 300_000
    database.pause(1, 0)
    now += 7_200_000
    assert not database.completed_sessions(0, now + 1)
    database.resume(1, 1)
    deadline = now + 3_300_000
    now = deadline + 1_200_000
    # Statistics is the first request after restart. No client delivered a stop.
    database = db.Database(path)
    row, = database.completed_sessions(0, now + 1)
    assert (row["ended_ms"], row["worked_ms"]) == (deadline, 3_600_000)
    database.stop(1)
    database.extend(1, 2)
    database.start(target, "stale", pomo_minutes=25)
    pending, = database.active_timers()
    assert pending["id"] == 1 and pending["overtime_pending"]
    database.finish_overtime(1, accept=accept)
    now += 50_000
    database.finish_overtime(1, accept=not accept)
    database.start(target, "first")
    active, history, _ = database.snapshot()
    assert not active and len(history) == 1
    extra = 1_200_000 if accept else 0
    assert (history[0]["ended_ms"], history[0]["worked_ms"]) == (deadline + extra, 3_600_000 + extra)


def test_edit_preserves_pauses_and_rejects_stale_edits(tmp_path):
    database = db.Database(tmp_path / "edit.sqlite3")
    database.initialize()
    with database.transaction(write=True) as conn:
        conn.execute("INSERT INTO intervals (target, started_ms, ended_ms, worked_ms) VALUES ('a', 100, 600, 300)")
    database.record_agent_run("a", "agent", 100, 600)
    for kind in ("personal", "agents"):
        assert database.edit_entry(kind, 1, 100, 600, 1000, 1800)
        assert not database.edit_entry(kind, 1, 100, 600, 1500, 2400)
        assert database.history_entry(kind, 1)["started_ms"] == 1000
    assert database.history_entry("personal", 1)["worked_ms"] == 600
