import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime

from tracker.config import Settings
from tracker.db import Database


@dataclass(frozen=True)
class Focus:
    source_id: str
    kind: int
    slug: str
    name: str
    started_ms: int
    ended_ms: int
    worked_ms: int


def timestamp(value: str) -> int:
    date = datetime.fromisoformat(value)
    if date.utcoffset() is None:
        raise ValueError("TickTick timestamps must include a timezone")
    return round(date.timestamp() * 1000)


def focus_from_record(row: dict) -> Focus:
    source_id = row["id"]
    kind = row["type"]
    started = timestamp(row["startTime"])
    ended = timestamp(row["endTime"])
    worked = row["duration"]
    if not source_id or kind not in (0, 1) or type(worked) is not int or worked <= 0 or ended < started:
        raise ValueError("Invalid completed TickTick focus record")

    targets = {}
    for task in row.get("tasks") or ():
        name = (task.get("title") or task.get("timerName") or "").strip()
        identity = next(
            ((field, task[field]) for field in ("taskId", "habitId", "timerId") if task.get(field)),
            ("title", name.casefold()),
        )
        if name:
            targets[identity] = name
        elif identity[1]:
            targets.setdefault(identity, "")
    if not targets and row.get("taskId"):
        targets[("taskId", row["taskId"])] = ""
    key = json.dumps(sorted(targets), ensure_ascii=False)
    slug = "ticktick-" + hashlib.sha256(key.encode()).hexdigest()[:32]
    names = list(dict.fromkeys(name for name in targets.values() if name))
    name = " / ".join(names) or ("Задача TickTick" if targets else "Свободный фокус")
    return Focus(source_id, kind, slug, name, started, ended, worked)


def import_focuses(db: Database, rows: list[dict], *, check: bool = False) -> dict:
    records = {}
    for row in rows:
        focus = focus_from_record(row)
        if records.setdefault(focus.source_id, focus) != focus:
            raise ValueError("Conflicting records with the same TickTick ID")

    inserted = 0
    with db.transaction(write=not check) as conn:
        imported = {row["source_id"] for row in conn.execute("SELECT source_id FROM ticktick_sessions")}
        pending = [focus for focus in records.values() if focus.source_id not in imported]
        existing_times = {
            (row["started_ms"], row["ended_ms"], row["worked_ms"])
            for row in conn.execute(
                """
                SELECT i.started_ms, i.ended_ms, COALESCE(i.worked_ms, i.ended_ms - i.started_ms) AS worked_ms
                FROM intervals i LEFT JOIN ticktick_sessions t ON t.interval_id = i.id
                WHERE i.ended_ms IS NOT NULL AND t.interval_id IS NULL
                """
            )
        }
        matches = sum((focus.started_ms, focus.ended_ms, focus.worked_ms) in existing_times for focus in pending)
        if not check:
            for focus in sorted(pending, key=lambda focus: focus.ended_ms, reverse=True):
                conn.execute(
                    "INSERT INTO ticktick_targets VALUES (?, ?) ON CONFLICT(slug) DO NOTHING",
                    (focus.slug, focus.name),
                )
                interval_id = conn.execute(
                    "INSERT INTO intervals (target, started_ms, ended_ms, worked_ms) VALUES (?, ?, ?, ?) RETURNING id",
                    (focus.slug, focus.started_ms, focus.ended_ms, focus.worked_ms),
                ).fetchone()["id"]
                conn.execute("INSERT INTO ticktick_sessions VALUES (?, ?, ?)", (focus.source_id, interval_id, focus.kind))
                inserted += 1
    return {
        "source_count": len(records),
        "source_ms": sum(focus.worked_ms for focus in records.values()),
        "pending": len(pending),
        "already_imported": len(records) - len(pending),
        "same_time_in_tracker": matches,
        "inserted": inserted,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Import TickTick focus records from JSON on stdin")
    parser.add_argument("--check", action="store_true", help="Compare with the tracker without importing")
    args = parser.parse_args()
    settings = Settings.from_env()
    db = Database(settings.database, settings.database_url)
    rows = json.load(sys.stdin)
    print(json.dumps(import_focuses(db, rows, check=args.check)))


if __name__ == "__main__":
    main()
