from tracker.db import Database, elapsed_ms
from tracker.ticktick import import_focuses


def test_ticktick_import_preserves_worked_time_and_can_be_rerun(tmp_path):
    db = Database(tmp_path / "tracker.sqlite3")
    db.initialize()
    record = {
        "id": "source-1", "type": 0,
        "startTime": "2026-11-20T08:00:00+0000", "endTime": "2026-11-20T08:45:00+0000",
        "duration": 1_800_321,
        "tasks": [
            {"taskId": "task-1", "title": "Чтение"},
            {"taskId": "task-1", "title": "Чтение"},
        ],
    }
    stopwatch = {
        **record, "id": "source-2", "type": 1, "duration": 3_000_000, "tasks": [],
    }
    rows = [record, stopwatch, record]
    preview = import_focuses(db, rows, check=True)
    assert (preview["pending"], preview["inserted"]) == (2, 0)

    result = import_focuses(db, rows)
    assert result["inserted"] == 2
    assert result["source_ms"] == 4_800_321
    assert import_focuses(db, rows)["already_imported"] == 2

    with db.transaction() as conn:
        stored = conn.execute(
            """
            SELECT i.*, t.name, s.type FROM intervals i
            JOIN ticktick_targets t ON i.target = t.slug
            JOIN ticktick_sessions s ON i.id = s.interval_id
            ORDER BY s.type
            """
        ).fetchall()
    assert [(row["name"], elapsed_ms(row, row["ended_ms"])) for row in stored] == [
        ("Чтение", 1_800_321), ("Свободный фокус", 3_000_000),
    ]
