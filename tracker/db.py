import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Lock
from time import time_ns
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from tracker.config import Target


SCHEMA = """
CREATE TABLE IF NOT EXISTS intervals (
    id {generated_id},
    target TEXT NOT NULL,
    started_ms INTEGER NOT NULL,
    ended_ms INTEGER CHECK (ended_ms >= started_ms)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_running_timer
    ON intervals(target) WHERE ended_ms IS NULL;
CREATE INDEX IF NOT EXISTS recent_intervals ON intervals(ended_ms DESC, id DESC);

CREATE TABLE IF NOT EXISTS starts (
    target TEXT NOT NULL,
    request_key TEXT NOT NULL,
    interval_id INTEGER NOT NULL REFERENCES intervals(id),
    PRIMARY KEY (target, request_key)
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id {generated_id},
    target TEXT NOT NULL,
    request_key TEXT NOT NULL,
    started_ms INTEGER NOT NULL,
    ended_ms INTEGER NOT NULL CHECK (ended_ms > started_ms),
    UNIQUE (target, request_key)
);
CREATE INDEX IF NOT EXISTS recent_agent_runs ON agent_runs(ended_ms DESC, id DESC);

CREATE TABLE IF NOT EXISTS sessions (
    digest TEXT PRIMARY KEY,
    expires_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS linear_oauth_requests (
    state TEXT PRIMARY KEY,
    session_digest TEXT NOT NULL REFERENCES sessions(digest) ON DELETE CASCADE,
    verifier TEXT NOT NULL,
    expires_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS linear_connection (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    client_id TEXT NOT NULL,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_ms INTEGER NOT NULL,
    account_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS linear_targets (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('project', 'issue')),
    name TEXT NOT NULL,
    reference TEXT NOT NULL,
    project_link_id TEXT NOT NULL,
    linear_url TEXT
);

CREATE TABLE IF NOT EXISTS focus_tasks (
    target_id TEXT PRIMARY KEY REFERENCES linear_targets(id),
    added_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ticktick_targets (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ticktick_sessions (
    source_id TEXT PRIMARY KEY,
    interval_id INTEGER NOT NULL UNIQUE REFERENCES intervals(id),
    type INTEGER NOT NULL CHECK (type IN (0, 1))
);

CREATE TABLE IF NOT EXISTS calendar_blocks (
    id TEXT PRIMARY KEY,
    target TEXT NOT NULL,
    day TEXT NOT NULL,
    start_minute INTEGER CHECK (start_minute BETWEEN 0 AND 1439),
    duration INTEGER NOT NULL CHECK (duration BETWEEN 1 AND 1440),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    CHECK (start_minute + duration <= 1440)
);
CREATE INDEX IF NOT EXISTS calendar_blocks_day ON calendar_blocks(day) WHERE deleted = 0;
"""

POSTGRES_LOCK_NAMESPACE = 1279872596
POSTGRES_SCHEMA_LOCK = 1
POSTGRES_WRITE_LOCK = 2
POSTGRES_LINEAR_LOCK = 3

INTERVAL_FIELDS = {
    "pomo_ms": "INTEGER",
    "overtime_pending": "INTEGER NOT NULL DEFAULT 0",
    "worked_ms": "INTEGER",
    "segment_started_ms": "INTEGER",
    "paused_ms": "INTEGER",
    "version": "INTEGER NOT NULL DEFAULT 0",
}

Row = sqlite3.Row | dict


class PostgresConnection(psycopg.Connection):
    def execute(self, query, params=None, **kwargs):
        # The tracker shares the same parameterized SQL with SQLite.
        return super().execute(query.replace("?", "%s"), params, **kwargs)

    def advisory_lock(self, key: int) -> None:
        self.execute("SELECT pg_advisory_xact_lock(?, ?)", (POSTGRES_LOCK_NAMESPACE, key))


def now_ms() -> int:
    return time_ns() // 1_000_000


def running_since(row: Row) -> int | None:
    if row["ended_ms"] is not None or row["paused_ms"] is not None:
        return None
    return row["segment_started_ms"] if row["segment_started_ms"] is not None else row["started_ms"]


def elapsed_ms(row: Row, now: int) -> int:
    if row["ended_ms"] is not None and row["worked_ms"] is None:
        return row["ended_ms"] - row["started_ms"]
    since = running_since(row)
    return (row["worked_ms"] or 0) + (max(0, now - since) if since is not None else 0)


class Database:
    def __init__(self, path: Path, database_url: str | None = None):
        self.path = path
        self.database_url = database_url
        self.local_linear_lock = Lock()

    def connect(self) -> sqlite3.Connection | PostgresConnection:
        if self.database_url:
            return PostgresConnection.connect(
                self.database_url, connect_timeout=5, sslmode="verify-full", sslrootcert="system",
                row_factory=dict_row,
            )
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.create_function("greatest", 2, max, deterministic=True)
        return conn

    def initialize(self) -> None:
        if self.database_url:
            schema = SCHEMA.format(generated_id="BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY")
            with self.connect() as conn:
                conn.advisory_lock(POSTGRES_SCHEMA_LOCK)
                conn.execute(schema.replace("INTEGER", "BIGINT"))
                conn.execute("ALTER TABLE linear_targets ADD COLUMN IF NOT EXISTS linear_url TEXT")
                existing = {column.name for column in conn.execute("SELECT * FROM intervals LIMIT 0").description}
                for name, definition in INTERVAL_FIELDS.items():
                    if name not in existing:
                        postgres_definition = definition.replace("INTEGER", "BIGINT")
                        conn.execute(f"ALTER TABLE intervals ADD COLUMN {name} {postgres_definition}")
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with closing(self.connect()) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA.format(generated_id="INTEGER PRIMARY KEY"))
        with self.transaction(write=True) as conn:
            if "linear_url" not in {row["name"] for row in conn.execute("PRAGMA table_info(linear_targets)")}:
                conn.execute("ALTER TABLE linear_targets ADD COLUMN linear_url TEXT")
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(intervals)")}
            for name, definition in INTERVAL_FIELDS.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE intervals ADD COLUMN {name} {definition}")

    @contextmanager
    def transaction(self, *, write: bool = False):
        with closing(self.connect()) as conn, conn:
            if self.database_url:
                if write:
                    conn.advisory_lock(POSTGRES_WRITE_LOCK)
                else:
                    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            else:
                conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn

    @contextmanager
    def linear_connection_lock(self):
        if self.database_url:
            with self.connect() as conn:
                conn.advisory_lock(POSTGRES_LINEAR_LOCK)
                yield
        else:
            with self.local_linear_lock:
                yield

    # The deadline follows the current running segment, so time on pause never
    # counts. Materialize expiry at that deadline even after an offline restart.
    _deadline = "coalesce(segment_started_ms, started_ms) + pomo_ms - coalesce(worked_ms, 0)"
    _due = f"ended_ms IS NULL AND paused_ms IS NULL AND pomo_ms IS NOT NULL AND {_deadline} <= ?"

    @contextmanager
    def timer_transaction(self, *, write: bool = False):
        if not write:
            with self.transaction() as conn:
                now = now_ms()
                if not conn.execute(f"SELECT 1 FROM intervals WHERE {self._due} LIMIT 1", (now,)).fetchone():
                    yield conn, now
                    return
        with self.transaction(write=True) as conn:
            now = now_ms()
            conn.execute(
                f"""UPDATE intervals SET ended_ms = {self._deadline}, worked_ms = pomo_ms,
                    overtime_pending = 1, version = version + 1 WHERE {self._due}""", (now,)
            )
            yield conn, now

    def start(self, target: Target, request_key: str, *, linear_reference: str | None = None,
              pomo_minutes: int | None = None) -> None:
        with self.timer_transaction(write=True) as (conn, now):
            if linear_reference is not None:
                self._register_linear_target(conn, target, linear_reference)
            self._start(conn, target.slug, request_key, now, pomo_minutes)

    @staticmethod
    def _start(conn: sqlite3.Connection | PostgresConnection, slug: str, request_key: str,
               now: int, pomo_minutes: int | None = None) -> None:
        seen = conn.execute(
            "SELECT 1 FROM starts WHERE target = ? AND request_key = ?", (slug, request_key)
        ).fetchone()
        if seen:
            return
        active = conn.execute(
            "SELECT id FROM intervals WHERE target = ? AND (ended_ms IS NULL OR overtime_pending = 1)", (slug,)
        ).fetchone()
        if active:
            interval_id = active["id"]
        else:
            interval_id = conn.execute(
                "INSERT INTO intervals (target, started_ms, pomo_ms) VALUES (?, ?, ?) RETURNING id",
                (slug, now, pomo_minutes * 60_000 if pomo_minutes is not None else None),
            ).fetchone()["id"]
        conn.execute(
            "INSERT INTO starts (target, request_key, interval_id) VALUES (?, ?, ?)",
            (slug, request_key, interval_id),
        )

    def restart(self, interval_id: int, request_key: str) -> bool:
        with self.timer_transaction(write=True) as (conn, now):
            row = conn.execute(
                "SELECT target FROM intervals WHERE id = ? AND ended_ms IS NOT NULL", (interval_id,)
            ).fetchone()
            if row is None:
                return False
            self._start(conn, row["target"], request_key, now)
            return True

    @staticmethod
    def _register_linear_target(conn: sqlite3.Connection | PostgresConnection, target: Target, reference: str) -> None:
        conn.execute(
            """
            INSERT INTO linear_targets (id, kind, name, reference, project_link_id, linear_url) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET name = excluded.name, reference = excluded.reference,
                linear_url = COALESCE(excluded.linear_url, linear_targets.linear_url)
            """,
            (target.slug.removeprefix("linear-"), target.kind, target.name, reference, str(uuid4()), target.linear_url),
        )

    def register_linear_target(self, target: Target, reference: str) -> None:
        with self.transaction(write=True) as conn:
            self._register_linear_target(conn, target, reference)

    def focus_tasks(self) -> tuple[str, ...]:
        with self.transaction() as conn:
            rows = conn.execute("SELECT target_id FROM focus_tasks ORDER BY added_ms DESC, target_id").fetchall()
            return tuple("linear-" + row["target_id"] for row in rows)

    def add_focus_task(self, target: Target, reference: str) -> None:
        with self.transaction(write=True) as conn:
            self._register_linear_target(conn, target, reference)
            conn.execute(
                "INSERT INTO focus_tasks VALUES (?, ?) ON CONFLICT(target_id) DO NOTHING",
                (target.slug.removeprefix("linear-"), now_ms()),
            )

    def remove_focus_task(self, target_id: str) -> None:
        with self.transaction(write=True) as conn:
            conn.execute("DELETE FROM focus_tasks WHERE target_id = ?", (target_id,))

    @staticmethod
    def _active_timers(conn: sqlite3.Connection | PostgresConnection) -> list[Row]:
        return conn.execute(
            "SELECT * FROM intervals WHERE ended_ms IS NULL OR overtime_pending = 1 ORDER BY started_ms, id"
        ).fetchall()

    def active_timers(self) -> list[Row]:
        with self.timer_transaction() as (conn, _):
            return self._active_timers(conn)

    def stop(self, interval_id: int, keep_seconds: int | None = None) -> None:
        with self.timer_transaction(write=True) as (conn, now):
            stopped = conn.execute(
                """
                UPDATE intervals SET
                    ended_ms = greatest(coalesce(paused_ms, segment_started_ms, started_ms), ?),
                    worked_ms = coalesce(worked_ms, 0) + CASE
                        WHEN paused_ms IS NULL THEN greatest(0, ? - coalesce(segment_started_ms, started_ms))
                        ELSE 0
                    END,
                    paused_ms = NULL
                WHERE id = ? AND ended_ms IS NULL
                """,
                (now, now, interval_id),
            )

            if stopped.rowcount and keep_seconds is not None:
                keep_ms = keep_seconds * 1000
                conn.execute(
                    "UPDATE intervals SET worked_ms = ? WHERE id = ? AND worked_ms > ?",
                    (keep_ms, interval_id, keep_ms),
                )

    def pause(self, interval_id: int, version: int) -> None:
        with self.timer_transaction(write=True) as (conn, now):
            conn.execute(
                """
                UPDATE intervals SET
                    paused_ms = greatest(coalesce(segment_started_ms, started_ms), ?),
                    worked_ms = coalesce(worked_ms, 0) + greatest(0, ? - coalesce(segment_started_ms, started_ms)),
                    version = version + 1
                WHERE id = ? AND version = ? AND ended_ms IS NULL AND paused_ms IS NULL
                """,
                (now, now, interval_id, version),
            )

    def resume(self, interval_id: int, version: int) -> None:
        with self.timer_transaction(write=True) as (conn, now):
            conn.execute(
                """
                UPDATE intervals SET segment_started_ms = greatest(paused_ms, ?),
                    paused_ms = NULL, version = version + 1
                WHERE id = ? AND version = ? AND ended_ms IS NULL AND paused_ms IS NOT NULL
                """,
                (now, interval_id, version),
            )

    def extend(self, interval_id: int, version: int) -> None:
        with self.timer_transaction(write=True) as (conn, _):
            conn.execute(
                """UPDATE intervals SET pomo_ms = pomo_ms + 300000, version = version + 1
                   WHERE id = ? AND version = ? AND ended_ms IS NULL AND pomo_ms IS NOT NULL""",
                (interval_id, version),
            )

    def finish_overtime(self, interval_id: int, *, accept: bool) -> None:
        with self.timer_transaction(write=True) as (conn, now):
            conn.execute(
                """UPDATE intervals SET overtime_pending = 0, version = version + 1,
                       worked_ms = worked_ms + CASE WHEN ? THEN greatest(0, ? - ended_ms) ELSE 0 END,
                       ended_ms = CASE WHEN ? THEN greatest(ended_ms, ?) ELSE ended_ms END
                   WHERE id = ? AND overtime_pending = 1""",
                (accept, now, accept, now, interval_id),
            )

    def finished_issue(self, interval_id: int) -> Row | None:
        with self.transaction() as conn:
            return conn.execute(
                """SELECT t.id, t.name FROM intervals i JOIN linear_targets t ON i.target = 'linear-' || t.id
                   WHERE i.id = ? AND i.ended_ms IS NOT NULL AND i.overtime_pending = 0 AND t.kind = 'issue'""",
                (interval_id,),
            ).fetchone()

    def history_entry(self, kind: str, interval_id: int) -> Row | None:
        table = "intervals" if kind == "personal" else "agent_runs"
        with self.timer_transaction() as (conn, _):
            return conn.execute(
                f"SELECT * FROM {table} WHERE id = ? AND ended_ms IS NOT NULL", (interval_id,)
            ).fetchone()

    def edit_entry(self, kind: str, interval_id: int, original_start: int, original_end: int,
                   start: int, end: int) -> bool:
        with self.timer_transaction(write=True) as (conn, _):
            condition = "WHERE id = ? AND started_ms = ? AND ended_ms = ?"
            if kind == "personal":
                # Preserve time excluded by pauses when correcting the boundaries.
                query = """UPDATE intervals SET started_ms = ?, ended_ms = ?, version = version + 1,
                    worked_ms = CASE WHEN worked_ms IS NOT NULL THEN
                        greatest(0, worked_ms + ? - (ended_ms - started_ms)) END """
                result = conn.execute(query + condition + " AND overtime_pending = 0",
                                      (start, end, end - start, interval_id, original_start, original_end))
            else:
                result = conn.execute("UPDATE agent_runs SET started_ms = ?, ended_ms = ? " + condition,
                                      (start, end, interval_id, original_start, original_end))
            return result.rowcount == 1

    def record_personal_time(self, target: str, request_key: str, start: int, finish: int) -> None:
        with self.transaction(write=True) as conn:
            seen = conn.execute(
                "SELECT 1 FROM starts WHERE target = ? AND request_key = ?", (target, request_key)
            ).fetchone()
            if seen:
                return
            interval_id = conn.execute(
                "INSERT INTO intervals (target, started_ms, ended_ms) VALUES (?, ?, ?) RETURNING id",
                (target, start, finish),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO starts (target, request_key, interval_id) VALUES (?, ?, ?)",
                (target, request_key, interval_id),
            )

    def record_agent_run(self, target: str, request_key: str, start: int, finish: int) -> None:
        with self.transaction(write=True) as conn:
            conn.execute(
                """
                INSERT INTO agent_runs (target, request_key, started_ms, ended_ms)
                VALUES (?, ?, ?, ?) ON CONFLICT(target, request_key) DO NOTHING
                """,
                (target, request_key, start, finish),
            )

    def completed_sessions(self, start_ms: int, end_ms: int) -> list[Row]:
        with self.timer_transaction() as (conn, _):
            return conn.execute(
                """
                SELECT * FROM intervals WHERE ended_ms >= ? AND ended_ms < ?
                ORDER BY ended_ms DESC, id DESC
                """,
                (start_ms, end_ms),
            ).fetchall()

    def calendar_blocks(self, monday: str, sunday: str) -> list[dict]:
        with self.transaction() as conn:
            rows = conn.execute(
                """SELECT id, target, day, start_minute, duration, version
                   FROM calendar_blocks WHERE deleted = 0 AND day BETWEEN ? AND ?
                   ORDER BY day, COALESCE(start_minute, -1), id""",
                (monday, sunday),
            ).fetchall()
            return [dict(row) for row in rows]

    def save_calendar_block(self, block_id: str, target: str, day: str,
                            start_minute: int | None, duration: int, version: int) -> bool:
        with self.transaction(write=True) as conn:
            if version == 0:
                result = conn.execute(
                    """INSERT INTO calendar_blocks (id, target, day, start_minute, duration)
                       VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING""",
                    (block_id, target, day, start_minute, duration),
                )
            else:
                result = conn.execute(
                    """UPDATE calendar_blocks
                       SET target = ?, day = ?, start_minute = ?, duration = ?, version = version + 1
                       WHERE id = ? AND version = ? AND deleted = 0 AND version < 9223372036854775807""",
                    (target, day, start_minute, duration, block_id, version),
                )
            return result.rowcount == 1

    def delete_calendar_block(self, block_id: str, version: int) -> bool:
        with self.transaction(write=True) as conn:
            # Retain the ID so a delayed create retry cannot resurrect a deleted block.
            result = conn.execute(
                "UPDATE calendar_blocks SET deleted = 1 WHERE id = ? AND version = ? AND deleted = 0",
                (block_id, version),
            )
            return result.rowcount == 1

    def snapshot(self) -> tuple[list[Row], list[Row], list[Row]]:
        with self.timer_transaction() as (conn, _):
            active = self._active_timers(conn)
            personal = conn.execute(
                """
                SELECT * FROM intervals WHERE ended_ms IS NOT NULL
                ORDER BY ended_ms DESC, id DESC LIMIT 15
                """
            ).fetchall()
            agents = conn.execute(
                "SELECT * FROM agent_runs ORDER BY ended_ms DESC, id DESC LIMIT 15"
            ).fetchall()
        return active, personal, agents

    def add_session(self, digest: str, expires_ms: int) -> None:
        with self.transaction(write=True) as conn:
            conn.execute("DELETE FROM sessions WHERE expires_ms <= ?", (now_ms(),))
            conn.execute(
                "INSERT INTO sessions (digest, expires_ms) VALUES (?, ?)", (digest, expires_ms)
            )

    def has_session(self, digest: str) -> bool:
        with self.transaction() as conn:
            return conn.execute(
                "SELECT 1 FROM sessions WHERE digest = ? AND expires_ms > ?", (digest, now_ms())
            ).fetchone() is not None

    def remove_session(self, digest: str) -> None:
        with self.transaction(write=True) as conn:
            conn.execute("DELETE FROM sessions WHERE digest = ?", (digest,))
