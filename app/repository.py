"""Persistence and query logic.

Design notes
------------
* ``coverage_rects`` stores one row per package rectangle.  Coverage is never
  expanded into individual cells, so a package spanning millions of cells
  costs a handful of bytes.
* ``current_wet`` holds only the *currently effective* wet cells.  Packages are
  resolved against each other on write, so a box query is an indexed range scan
  over current state and never replays historical water points.
* Every import runs in a single ``BEGIN IMMEDIATE`` transaction.  A rejected
  package leaves both coverage and current wet state exactly as they were.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from .parsing import Coverage, Package


class ConflictError(Exception):
    """Same version number, different content."""


@dataclass(frozen=True)
class Box:
    row_start: int
    row_end: int
    col_start: int
    col_end: int

    def as_dict(self) -> dict:
        return {
            "row_start": self.row_start,
            "row_end": self.row_end,
            "col_start": self.col_start,
            "col_end": self.col_end,
        }

    @property
    def area(self) -> int:
        return (self.row_end - self.row_start) * (self.col_end - self.col_start)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _max_version(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM packages").fetchone()[0]


def _newer_rects_in(
    conn: sqlite3.Connection, coverage: Coverage, than_version: int
) -> list[tuple[int, int, int, int]]:
    """Coverage rectangles newer than ``than_version`` that intersect ``coverage``."""
    rows = conn.execute(
        """
        SELECT r0, r1, c0, c1 FROM coverage_rects
        WHERE r0 < ? AND r1 > ?
          AND c0 < ? AND c1 > ?
          AND version > ?
        """,
        (
            coverage.row_end,
            coverage.row_start,
            coverage.col_end,
            coverage.col_start,
            than_version,
        ),
    ).fetchall()
    return [(r["r0"], r["r1"], r["c0"], r["c1"]) for r in rows]


def _shadowed(row: int, col: int, rects: list[tuple[int, int, int, int]]) -> bool:
    return any(r0 <= row < r1 and c0 <= col < c1 for r0, r1, c0, c1 in rects)


@contextmanager
def _transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")


def ingest(conn: sqlite3.Connection, package: Package) -> dict:
    """Apply ``package`` atomically.

    Returns a summary with ``status`` one of ``imported`` or ``unchanged``.
    Raises :class:`ConflictError` for a version collision with different
    content, and lets validation / SQLite errors propagate.  Because the whole
    write happens in one transaction, a failure leaves the previous coverage
    and wet state untouched.
    """
    coverage = package.coverage
    max_before = _max_version(conn)

    with _transaction(conn):
        existing = conn.execute(
            "SELECT digest FROM packages WHERE version = ?", (package.version,)
        ).fetchone()
        if existing is not None:
            if existing["digest"] == package.digest:
                return {
                    "status": "unchanged",
                    "version": package.version,
                    "digest": package.digest,
                    "max_version": max_before,
                    "message": "同版同内容重传，结果未改变。",
                }
            raise ConflictError(
                f"版号 {package.version} 已存在且内容不同，整包退回。"
            )

        conn.execute(
            """
            INSERT INTO packages
                (version, digest, r0, r1, c0, c1, wet_count, imported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                package.version,
                package.digest,
                coverage.row_start,
                coverage.row_end,
                coverage.col_start,
                coverage.col_end,
                len(package.wet_cells),
                _now(),
            ),
        )
        # Coverage stays a rectangle.  Only the R*Tree index sees it.
        conn.execute(
            "INSERT INTO coverage_rects (version, r0, r1, c0, c1) VALUES (?, ?, ?, ?, ?)",
            (
                package.version,
                coverage.row_start,
                coverage.row_end,
                coverage.col_start,
                coverage.col_end,
            ),
        )

        # This package now owns the whole rectangle.  Any current wet cell in
        # it left by an older-or-equal version is superseded.
        removed = conn.execute(
            """
            DELETE FROM current_wet
            WHERE row >= ? AND row < ?
              AND col >= ? AND col < ?
              AND version <= ?
            """,
            (
                coverage.row_start,
                coverage.row_end,
                coverage.col_start,
                coverage.col_end,
                package.version,
            ),
        ).rowcount

        inserted = 0
        shadowed = 0
        newer = _newer_rects_in(conn, coverage, package.version)
        # Rows still in the rectangle after the delete above belong to a newer
        # version that only partially overlaps.  Those verdicts win.
        newer_cells = {
            (r["row"], r["col"])
            for r in conn.execute(
                """
                SELECT row, col FROM current_wet
                WHERE row >= ? AND row < ?
                  AND col >= ? AND col < ?
                """,
                (
                    coverage.row_start,
                    coverage.row_end,
                    coverage.col_start,
                    coverage.col_end,
                ),
            )
        }
        rows_to_insert = []
        for row, col in package.wet_cells:
            if (row, col) in newer_cells or _shadowed(row, col, newer):
                # A newer survey already checked this cell; the late older
                # package must not overwrite the newer verdict.
                shadowed += 1
                continue
            rows_to_insert.append((row, col, package.version))
        if rows_to_insert:
            conn.executemany(
                "INSERT INTO current_wet (row, col, version) VALUES (?, ?, ?) "
                "ON CONFLICT(row, col) DO UPDATE SET version = excluded.version",
                rows_to_insert,
            )
            inserted = len(rows_to_insert)

    return {
        "status": "imported",
        "version": package.version,
        "digest": package.digest,
        "max_version": _max_version(conn),
        "late_arrival": package.version < max_before,
        "warning": package.warning,
        "summary": {
            "coverage": coverage_dict(coverage),
            "coverage_area": coverage.area,
            "listed_wet_points": len(package.wet_cells),
            "current_wet_added": inserted,
            "current_wet_removed": removed,
            "wet_points_shadowed_by_newer": shadowed,
        },
    }


def coverage_dict(coverage: Coverage) -> dict:
    return {
        "row_start": coverage.row_start,
        "row_end": coverage.row_end,
        "col_start": coverage.col_start,
        "col_end": coverage.col_end,
    }


def query_wet(conn: sqlite3.Connection, box: Box, limit: int) -> dict:
    """Current wet cells inside ``box`` (half-open)."""
    rows = conn.execute(
        """
        SELECT row, col, version
        FROM current_wet
        WHERE row >= ? AND row < ?
          AND col >= ? AND col < ?
        ORDER BY row, col
        LIMIT ?
        """,
        (box.row_start, box.row_end, box.col_start, box.col_end, limit + 1),
    ).fetchall()
    truncated = len(rows) > limit
    cells = [
        {"row": r["row"], "col": r["col"], "version": r["version"]}
        for r in rows[:limit]
    ]
    return {
        "box": box.as_dict(),
        "cells": cells,
        "count": len(cells),
        "truncated": truncated,
        "limit": limit,
    }


def coverage_in_box(conn: sqlite3.Connection, box: Box) -> list[dict]:
    """Survey rectangles intersecting ``box``; used only for UI painting."""
    rows = conn.execute(
        """
        SELECT t.version, t.r0, t.r1, t.c0, t.c1
        FROM coverage_rects t
        WHERE t.r0 < ? AND t.r1 > ?
          AND t.c0 < ? AND t.c1 > ?
        ORDER BY t.version
        """,
        (box.row_end, box.row_start, box.col_end, box.col_start),
    ).fetchall()
    return [
        {
            "version": r["version"],
            "row_start": r["r0"],
            "row_end": r["r1"],
            "col_start": r["c0"],
            "col_end": r["c1"],
        }
        for r in rows
    ]


def cell_status(conn: sqlite3.Connection, row: int, col: int) -> dict:
    """Return wet / dry / unsurveyed for one cell.

    ``dry`` means the cell is inside at least one survey rectangle but is not
    a current wet cell.  ``unsurveyed`` means no package ever covered it.
    """
    wet = conn.execute(
        "SELECT version FROM current_wet WHERE row = ? AND col = ?", (row, col)
    ).fetchone()
    if wet is not None:
        return {
            "row": row,
            "col": col,
            "status": "wet",
            "version": wet["version"],
            "label": "有积水",
        }

    cover = conn.execute(
        """
        SELECT version FROM coverage_rects
        WHERE r0 <= ? AND r1 > ? AND c0 <= ? AND c1 > ?
        ORDER BY version DESC
        LIMIT 1
        """,
        (row, row, col, col),
    ).fetchone()
    if cover is not None:
        # The highest covering version is the one that produced this verdict.
        # If an even higher dry package covered the cell it would be the max.
        return {
            "row": row,
            "col": col,
            "status": "dry",
            "version": cover["version"],
            "label": "已查无积水",
        }
    return {
        "row": row,
        "col": col,
        "status": "unsurveyed",
        "version": None,
        "label": "未巡查",
    }


def list_packages(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        SELECT version, digest, r0, r1, c0, c1, wet_count, imported_at
        FROM packages
        ORDER BY version DESC
        """
    ).fetchall()
    current_counts = dict(
        conn.execute(
            "SELECT version, COUNT(*) FROM current_wet GROUP BY version"
        ).fetchall()
    )
    total_current_wet = sum(current_counts.values())
    return {
        "packages": [
            {
                "version": r["version"],
                "digest": r["digest"],
                "coverage": {
                    "row_start": r["r0"],
                    "row_end": r["r1"],
                    "col_start": r["c0"],
                    "col_end": r["c1"],
                },
                "listed_wet_points": r["wet_count"],
                "current_wet_points": current_counts.get(r["version"], 0),
                "imported_at": r["imported_at"],
            }
            for r in rows
        ],
        "max_version": _max_version(conn),
        "total_current_wet": total_current_wet,
    }
