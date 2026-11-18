"""SQLite persistence helpers.

One short-lived connection is opened per operation.  SQLite's WAL mode lets
readers continue while an acceptance transaction is writing, and
``BEGIN IMMEDIATE`` serialises the two mutating paths (acceptance and map
publication).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS wafers (
    wafer_id       TEXT PRIMARY KEY,
    rows           INTEGER NOT NULL CHECK (rows > 0),
    cols           INTEGER NOT NULL CHECK (cols > 0),
    accepted_seq   INTEGER NOT NULL DEFAULT 0,
    committed_seq  INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    last_error_at  TEXT,
    last_error_seq INTEGER,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    CHECK (accepted_seq >= committed_seq)
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id       TEXT PRIMARY KEY,
    wafer_id       TEXT NOT NULL REFERENCES wafers(wafer_id),
    seq            INTEGER NOT NULL,
    machine_id     TEXT NOT NULL,
    origin_row     INTEGER NOT NULL,
    origin_col     INTEGER NOT NULL,
    rotation_deg   INTEGER NOT NULL,
    local_rows     INTEGER NOT NULL,
    local_cols     INTEGER NOT NULL,
    cell_count     INTEGER NOT NULL,
    received_at    TEXT NOT NULL,
    UNIQUE (wafer_id, seq)
);

CREATE TABLE IF NOT EXISTS observations (
    wafer_id       TEXT NOT NULL REFERENCES wafers(wafer_id),
    full_row       INTEGER NOT NULL,
    full_col       INTEGER NOT NULL,
    retest_no      INTEGER NOT NULL,
    verdict        TEXT NOT NULL,
    machine_id     TEXT NOT NULL,
    batch_id       TEXT NOT NULL REFERENCES batches(batch_id),
    local_row      INTEGER NOT NULL,
    local_col      INTEGER NOT NULL,
    PRIMARY KEY (wafer_id, full_row, full_col, retest_no)
);

CREATE INDEX IF NOT EXISTS idx_observations_cell
    ON observations (wafer_id, full_row, full_col);

CREATE TABLE IF NOT EXISTS map_cells (
    wafer_id             TEXT NOT NULL REFERENCES wafers(wafer_id),
    full_row             INTEGER NOT NULL,
    full_col             INTEGER NOT NULL,
    verdict              TEXT NOT NULL,
    source_retest_no     INTEGER NOT NULL,
    source_machine_id    TEXT NOT NULL,
    source_batch_id      TEXT NOT NULL REFERENCES batches(batch_id),
    source_batch_seq     INTEGER NOT NULL,
    source_local_row     INTEGER NOT NULL,
    source_local_col     INTEGER NOT NULL,
    computed_seq         INTEGER NOT NULL,
    PRIMARY KEY (wafer_id, full_row, full_col)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    # Acceptance survives an application restart.  FULL is chosen deliberately:
    # correctness of "accepted data" matters more than upload throughput here.
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def init_db(db_path: str | Path) -> None:
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """Hold one read snapshot so multi-query responses are internally consistent.

    SQLite fixes the visible snapshot at the first read inside a deferred
    transaction, so the wafer row and its map cells cannot straddle a publish.
    """
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
