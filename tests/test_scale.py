from __future__ import annotations

import time

import pytest

from app.db import connect

from .conftest import cell, make_app, post_pkg
from .test_parsing import pkg


@pytest.fixture()
def client(tmp_path):
    app = make_app(tmp_path)
    with app.test_client() as c:
        yield c


def test_box_query_uses_current_index_not_history(client):
    post_pkg(client, pkg(1, 0, 1000, 0, 1000, [(500, 500)]))
    conn = connect(client.application.config["DATABASE"])
    plan = conn.execute(
        """
        EXPLAIN QUERY PLAN
        SELECT row, col, version FROM current_wet
        WHERE row >= 0 AND row < 1000 AND col >= 0 AND col < 1000
        ORDER BY row, col
        """
    ).fetchall()
    detail = " ".join(str(r["detail"]) for r in plan)
    assert "current_wet" in detail
    assert "packages" not in detail
    conn.close()


def test_query_cost_stays_flat_as_history_grows(client):
    # Many superseded generations of ponds in the same area.  Only current
    # state is stored, so a box query does not get slower with history.
    for version in range(1, 60):
        post_pkg(client, pkg(version, 0, 200, 0, 200, [(version % 50, 5), (6, 6)]))
    start = time.perf_counter()
    result = None
    for _ in range(20):
        result = client.get("/api/wet?row_start=0&row_end=200&col_start=0&col_end=200")
    elapsed = time.perf_counter() - start
    body = result.get_json()
    assert body["count"] == 2  # exactly the newest generation's two cells
    assert elapsed < 5.0


def test_coverage_storage_is_one_row_per_package(client):
    post_pkg(client, pkg(1, -500, 500, -500, 500, []))
    conn = connect(client.application.config["DATABASE"])
    assert conn.execute("SELECT COUNT(*) FROM coverage_rects").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM current_wet").fetchone()[0] == 0
    conn.close()


def test_fallback_without_rtree(tmp_path):
    """The app works on SQLite builds without the R*Tree module."""
    app = make_app(tmp_path, PREFER_RTREE=False)
    with app.test_client() as c:
        assert post_pkg(c, pkg(1, 0, 10, 0, 10, [(2, 2)])).status_code == 201
        assert cell(c, 2, 2)["status"] == "wet"
        assert cell(c, 3, 3)["status"] == "dry"
        assert post_pkg(c, pkg(2, 0, 10, 0, 10, [])).status_code == 201
        assert cell(c, 2, 2)["status"] == "dry"

    conn = connect(app.config["DATABASE"])
    kind = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'coverage_rects'"
    ).fetchone()[0]
    assert "VIRTUAL TABLE" not in kind
    conn.close()
