"""Strict parsing and validation of inspection package JSON.

Package shape (all coordinates are integers, rectangles are half-open)::

    {
      "version": 7,
      "coverage": {"row_start": 0, "row_end": 50,
                   "col_start": 0, "col_end": 20},
      "wet_cells": [[12, 4], [12, 5], [31, 2]]
    }

Rows/columns not listed in ``wet_cells`` inside the coverage rectangle are
surveyed and dry.  Anything outside the rectangle is un-surveyed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


MAX_COORD = 2_000_000_000  # stays inside SQLite R*Tree's signed 32-bit range
MAX_VERSION = 2_000_000_000
MAX_WET_CELLS = 500_000  # listed points, not coverage area

_TOP_LEVEL_KEYS = {"version", "coverage", "wet_cells"}
_COVERAGE_KEYS = {"row_start", "row_end", "col_start", "col_end"}


class PackageError(ValueError):
    """Raised when a package is structurally invalid."""

    def __init__(self, message: str, *, details: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


def _is_int(value: Any) -> bool:
    # bool is an int subclass; JSON true/false must not become coordinates.
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class Coverage:
    row_start: int
    row_end: int
    col_start: int
    col_end: int

    @property
    def area(self) -> int:
        return (self.row_end - self.row_start) * (self.col_end - self.col_start)

    def contains(self, row: int, col: int) -> bool:
        return (
            self.row_start <= row < self.row_end
            and self.col_start <= col < self.col_end
        )


@dataclass(frozen=True)
class Package:
    version: int
    coverage: Coverage
    wet_cells: tuple[tuple[int, int], ...]
    digest: str

    @property
    def warning(self) -> str | None:
        if not self.wet_cells:
            # Not an error: a clean sweep is the normal way a pond recedes.
            return "包内没有积水点；该覆盖范围将被记录为已巡查且无积水。"
        return None


def _require_object(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise PackageError("包体必须是 JSON 对象。")
    unknown = sorted(set(raw) - _TOP_LEVEL_KEYS)
    if unknown:
        # Rejecting unknown keys prevents a silently mistyped "wet_cell" from
        # being read as an empty (all-dry) survey.
        raise PackageError(
            f"包体含未知字段: {', '.join(unknown)}。",
            details=["允许的字段: coverage, version, wet_cells"],
        )
    return raw


def _parse_coverage(raw: Any) -> Coverage:
    if not isinstance(raw, dict):
        raise PackageError("coverage 必须是对象，包含 row_start/row_end/col_start/col_end。")
    unknown = sorted(set(raw) - _COVERAGE_KEYS)
    if unknown:
        raise PackageError(f"coverage 含未知字段: {', '.join(unknown)}。")
    missing = [
        k
        for k in ("row_start", "row_end", "col_start", "col_end")
        if k not in raw
    ]
    if missing:
        raise PackageError(f"coverage 缺少字段: {', '.join(missing)}。")
    values = {}
    bad = []
    for key in ("row_start", "row_end", "col_start", "col_end"):
        value = raw[key]
        if not _is_int(value):
            bad.append(f"{key} 必须是整数")
        elif abs(value) > MAX_COORD:
            bad.append(f"{key} 超出允许范围 ±{MAX_COORD}")
        else:
            values[key] = value
    if bad:
        raise PackageError("coverage 字段非法。", details=bad)
    coverage = Coverage(**values)
    if coverage.row_end <= coverage.row_start:
        raise PackageError("coverage 行区间必须满足 row_start < row_end（左闭右开且非空）。")
    if coverage.col_end <= coverage.col_start:
        raise PackageError("coverage 列区间必须满足 col_start < col_end（左闭右开且非空）。")
    return coverage


def _parse_wet_cells(raw: Any, coverage: Coverage) -> tuple[tuple[int, int], ...]:
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise PackageError("wet_cells 必须是坐标数组，例如 [[row, col], ...]。")
    if len(raw) > MAX_WET_CELLS:
        raise PackageError(
            f"wet_cells 超过上限 {MAX_WET_CELLS} 个坐标点。",
            details=["覆盖范围本身不计入上限，只限制列出的积水点。"],
        )
    seen: set[tuple[int, int]] = set()
    errors: list[str] = []
    ordered: list[tuple[int, int]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, list) or len(item) != 2:
            errors.append(f"wet_cells[{index}] 必须是 [row, col]。")
            continue
        row, col = item
        if not (_is_int(row) and _is_int(col)):
            errors.append(f"wet_cells[{index}] 的行列必须是整数。")
            continue
        if abs(row) > MAX_COORD or abs(col) > MAX_COORD:
            errors.append(f"wet_cells[{index}] 坐标超出允许范围 ±{MAX_COORD}。")
            continue
        if not coverage.contains(row, col):
            errors.append(
                f"wet_cells[{index}] = ({row}, {col}) 落在覆盖矩形之外。"
            )
            continue
        if (row, col) in seen:
            # Duplicate coordinates carry no extra meaning; collapse them so a
            # retry of the same content always hashes identically.
            continue
        seen.add((row, col))
        ordered.append((row, col))
    if errors:
        raise PackageError("wet_cells 校验失败。", details=errors[:20])
    ordered.sort()
    return tuple(ordered)


def canonical_payload(version: int, coverage: Coverage, cells: tuple[tuple[int, int], ...]) -> dict:
    """Normalised representation used for hashing and equality checks.

    Normalising (sorted, de-duplicated cells; fixed key order) means the same
    survey re-serialised differently still counts as identical content.
    """
    return {
        "version": version,
        "coverage": {
            "row_start": coverage.row_start,
            "row_end": coverage.row_end,
            "col_start": coverage.col_start,
            "col_end": coverage.col_end,
        },
        "wet_cells": [[r, c] for r, c in cells],
    }


def digest_of(payload: dict) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_package(raw: Any) -> Package:
    """Validate ``raw`` and return a normalised :class:`Package`."""
    obj = _require_object(raw)
    version = obj.get("version")
    if not _is_int(version) or version <= 0:
        raise PackageError("version 必须是正整数。")
    if version > MAX_VERSION:
        raise PackageError(f"version 超出允许范围 1..{MAX_VERSION}。")
    coverage = _parse_coverage(obj.get("coverage"))
    cells = _parse_wet_cells(obj.get("wet_cells", []), coverage)
    payload = canonical_payload(version, coverage, cells)
    return Package(
        version=version,
        coverage=coverage,
        wet_cells=cells,
        digest=digest_of(payload),
    )
