"""SQLite connection helpers and schema for the flood-cell warehouse."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    version     INTEGER PRIMARY KEY,
    digest      TEXT    NOT NULL,
    r0          INTEGER NOT NULL,
    r1          INTEGER NOT NULL,
    c0          INTEGER NOT NULL,
    c1          INTEGER NOT NULL,
    wet_count   INTEGER NOT NULL CHECK (wet_count >= 0),
    imported_at TEXT    NOT NULL,
    CHECK (r0 < r1),
    CHECK (c0 < c1)
);

-- Coverage is kept as rectangles only, never expanded to cells.  When SQLite
-- was built with R*Tree support we use it; otherwise a plain rectangle table
-- with indexes answers the same intersection queries.  Both have identical
-- column names so repository SQL is shared.
-- (created by init_db)

-- Materialised *current* wet cells.  There is deliberately no history table:
-- package content is resolved while ingesting, so rectangle queries never
-- rescan superseded water points.
CREATE TABLE IF NOT EXISTS current_wet (
    row     INTEGER NOT NULL,
    col     INTEGER NOT NULL,
    version INTEGER NOT NULL REFERENCES packages(version) ON DELETE CASCADE,
    PRIMARY KEY (row, col)
) WITHOUT ROWID;
"""

RTREE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS coverage_rects USING rtree_i32(
    version, r0, r1, c0, c1
)
"""

RECT_SQL = """
CREATE TABLE IF NOT EXISTS coverage_rects (
    version INTEGER PRIMARY KEY,
    r0      INTEGER NOT NULL,
    r1      INTEGER NOT NULL,
    c0      INTEGER NOT NULL,
    c1      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS coverage_rects_rows ON coverage_rects (r0, r1);
CREATE INDEX IF NOT EXISTS coverage_rects_cols ON coverage_rects (c0, c1);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with the pragmas this application relies on."""
    path = str(path)
    conn = sqlite3.connect(path, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        # Another process is switching the journal mode; not fatal, the
        # default rollback journal still works.
        pass
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection, prefer_rtree: bool = True) -> None:
    conn.executescript(SCHEMA)
    # The choice of coverage index is made once, when the database file is
    # first created; reopening an existing file reuses whatever is there.
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'coverage_rects'"
    ).fetchone()
    if has_table:
        conn.execute("PRAGMA user_version = 1")
        return
    if prefer_rtree:
        try:
            conn.executescript(RTREE_SQL)
            conn.execute("PRAGMA user_version = 1")
            return
        except sqlite3.OperationalError:
            pass
    # SQLite without the R*Tree module is still fully supported.
    conn.executescript(RECT_SQL)
    conn.execute("PRAGMA user_version = 1")


def _schema_present(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'packages'"
        ).fetchone()
        is not None
    )


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        path = current_app.config["DATABASE"]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = connect(path)
        # If the database file was removed or replaced underneath a running
        # process, recreate the schema instead of failing every request.
        if not _schema_present(conn):
            init_db(conn, prefer_rtree=current_app.config.get("PREFER_RTREE", True))
        g.db = conn
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()
