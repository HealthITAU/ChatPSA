#!/usr/bin/env python3
"""
sync_state — Per-entity sync status tracking shared by all sync scripts.

Adds a single `sync_state` table to the CW data DB with one row per
(source, entity) pair. Each sync script calls record_started() at the
beginning of an entity sync and record_finished() (or record_failed())
at the end. The web app's /api/sync/status route can read this to show
operators what synced when, what failed, and what's currently running —
giving us proper observability across CW, CIPP, and any future
sources without re-implementing tracking in each script.

The existing sync_log table is left alone for backwards compatibility.

Usage in a sync script:
    from sync_state import init_sync_state, record_started, record_finished

    init_sync_state(conn)
    record_started(conn, "cw", "tickets")
    try:
        n = sync_tickets(...)
        record_finished(conn, "cw", "tickets", record_count=n)
    except Exception as e:
        record_failed(conn, "cw", "tickets", error=str(e))
        raise
"""

import time
from datetime import datetime, timezone


SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_state (
    source              TEXT NOT NULL,
    entity              TEXT NOT NULL,
    last_started_at     TEXT,
    last_completed_at   TEXT,
    last_status         TEXT,           -- 'running' | 'ok' | 'error'
    last_error          TEXT,
    last_record_count   INTEGER,
    last_duration_sec   REAL,
    PRIMARY KEY (source, entity)
);
"""


def init_sync_state(conn):
    """Create the sync_state table if it doesn't already exist."""
    conn.execute(SCHEMA)
    conn.commit()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_started(conn, source, entity):
    """Mark an entity sync as in-progress."""
    conn.execute("""
        INSERT INTO sync_state (source, entity, last_started_at, last_status)
        VALUES (?, ?, ?, 'running')
        ON CONFLICT(source, entity) DO UPDATE SET
            last_started_at = excluded.last_started_at,
            last_status     = 'running',
            last_error      = NULL
    """, (source, entity, _now()))
    conn.commit()


def record_finished(conn, source, entity, record_count=None, duration_sec=None):
    """Mark an entity sync as completed successfully."""
    conn.execute("""
        INSERT INTO sync_state (source, entity, last_completed_at, last_status,
                                last_record_count, last_duration_sec, last_error)
        VALUES (?, ?, ?, 'ok', ?, ?, NULL)
        ON CONFLICT(source, entity) DO UPDATE SET
            last_completed_at = excluded.last_completed_at,
            last_status       = 'ok',
            last_record_count = excluded.last_record_count,
            last_duration_sec = excluded.last_duration_sec,
            last_error        = NULL
    """, (source, entity, _now(), record_count, duration_sec))
    conn.commit()


def record_failed(conn, source, entity, error):
    """Mark an entity sync as failed and store the error message."""
    conn.execute("""
        INSERT INTO sync_state (source, entity, last_completed_at, last_status, last_error)
        VALUES (?, ?, ?, 'error', ?)
        ON CONFLICT(source, entity) DO UPDATE SET
            last_completed_at = excluded.last_completed_at,
            last_status       = 'error',
            last_error        = excluded.last_error
    """, (source, entity, _now(), str(error)[:1000]))
    conn.commit()


class TrackedSync:
    """Context manager wrapper that records start/finish/failure automatically.

        with TrackedSync(conn, "cw", "tickets") as t:
            n = sync_tickets(...)
            t.record_count = n
    """
    def __init__(self, conn, source, entity):
        self.conn = conn
        self.source = source
        self.entity = entity
        self.record_count = None
        self._start = None

    def __enter__(self):
        self._start = time.monotonic()
        record_started(self.conn, self.source, self.entity)
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed = round(time.monotonic() - self._start, 2) if self._start else None
        if exc:
            record_failed(self.conn, self.source, self.entity, error=exc)
            return False  # re-raise
        record_finished(self.conn, self.source, self.entity,
                        record_count=self.record_count, duration_sec=elapsed)
        return False
