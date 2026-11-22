from __future__ import annotations

import json

import pytest

from app.db import connect
from app.parsing import parse_package
from app.repository import ingest

from .conftest import cell, make_app, post_pkg, sample, wet
from .test_parsing import pkg


@pytest.fixture()
def client(tmp_path):
    app = make_app(tmp_path)
    with app.test_client() as c:
        yield c


def test_wet_box_is_half_open(client):
    post_pkg(client, pkg(1, 0, 20, 0, 20, [(5, 5), (10, 10), (19, 19)]))
    result = wet(client, 5, 10, 5, 10)
    assert [(c["row"], c["col"]) for c in result["cells"]] == [(5, 5)]
    assert (10, 10) not in [(c["row"], c["col"]) for c in result["cells"]]


def test_wet_box_limit_and_truncation_flag(client):
    cells = [(r, 0) for r in range(10)]
    post_pkg(client, pkg(1, 0, 20, 0, 20, cells))
    result = wet(client, 0, 20, 0, 20, limit=4)
    assert result["count"] == 4
    assert result["truncated"] is True
    assert result["limit"] == 4


def test_wet_box_returns_only_current_state(client):
    # Two historical ponds in the same place, then a resweep with none.
    post_pkg(client, pkg(1, 0, 10, 0, 10, [(3, 3)]))
    post_pkg(client, pkg(2, 0, 10, 0, 10, [(4, 4)]))
    post_pkg(client, pkg(3, 0, 10, 0, 10, []))
    result = wet(client, 0, 10, 0, 10)
    assert result["cells"] == []


def test_coverage_rects_are_reported_not_expanded(client):
    post_pkg(client, pkg(1, 0, 1000, 0, 1000, []))
    result = wet(client, 0, 10, 0, 10)
    assert len(result["coverage_rects"]) == 1
    assert result["coverage_rects"][0]["row_end"] == 1000


def test_bad_box_arguments(client):
    for query in (
        "row_start=0&row_end=5&col_start=0",
        "row_start=a&row_end=5&col_start=0&col_end=5",
        "row_start=5&row_end=0&col_start=0&col_end=5",
        "row_start=0&row_end=5&col_start=5&col_end=0",
    ):
        response = client.get(f"/api/wet?{query}")
        assert response.status_code == 400, query


def test_bad_cell_arguments(client):
    assert client.get("/api/cell?row=1").status_code == 400
    assert client.get("/api/cell?row=1&col=x").status_code == 400


def test_demo_samples_flow(client):
    # The exact sequence the demo buttons produce.
    assert post_pkg(client, sample("v3.json")).status_code == 201
    assert post_pkg(client, sample("v7.json")).status_code == 201
    late = post_pkg(client, sample("v5-late.json"))
    assert late.status_code == 201
    assert late.get_json()["late_arrival"] is True

    # (8,5) was listed by v3 and re-listed by v7; v7 owns it.
    assert cell(client, 8, 5)["version"] == 7
    assert cell(client, 8, 5)["status"] == "wet"
    # (4,15) came only from v3 and survives.
    assert cell(client, 4, 15)["version"] == 3
    # (8,4) is outside v7's rectangle (cols start at 5), so v3 still owns it.
    assert cell(client, 8, 4)["status"] == "wet"
    assert cell(client, 8, 4)["version"] == 3
    # (10,5): v3 water, v7 covers the cell without listing it -> dry.
    assert cell(client, 10, 5)["status"] == "dry"
    assert cell(client, 10, 5)["version"] == 7
    # v5 late pond lands in an area no newer package covered.
    assert cell(client, 22, 2)["status"] == "wet"
    assert cell(client, 22, 2)["version"] == 5
    # (22,7) is listed by late v5 but v7 already surveyed it as dry.
    assert cell(client, 22, 7)["status"] == "dry"

    retry = post_pkg(client, sample("v7-retry.json"))
    assert retry.status_code == 200
    assert retry.get_json()["status"] == "unchanged"

    conflict = post_pkg(client, sample("v7-conflict.json"))
    assert conflict.status_code == 409

    # Nothing from the rejected v7 conflict leaked in.
    assert cell(client, 24, 24)["status"] == "dry"


def test_html_page_served(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "积水格入仓" in body
    assert "/static/app.js" in body


def test_healthz(client):
    assert client.get("/healthz").get_json() == {"status": "ok"}


def test_reset_requires_flag(tmp_path):
    app = make_app(tmp_path, ALLOW_RESET=False)
    with app.test_client() as c:
        assert c.post("/api/reset").status_code == 403


def test_reset_clears_everything(client):
    post_pkg(client, pkg(1, 0, 10, 0, 10, [(1, 1)]))
    assert client.post("/api/reset").status_code == 200
    assert client.get("/api/packages").get_json()["packages"] == []
    assert cell(client, 1, 1)["status"] == "unsurveyed"


def test_ingest_failure_keeps_previous_state(client, monkeypatch):
    """A mid-transaction failure must roll back coverage and wet state."""
    post_pkg(client, pkg(1, 0, 10, 0, 10, [(1, 1)]))

    conn = connect(client.application.config["DATABASE"])

    class FailingConn:
        """Proxy that fails on the insert step, after earlier writes ran."""

        def __getattr__(self, name):
            return getattr(conn, name)

        def executemany(self, *_args, **_kwargs):
            raise RuntimeError("simulated write failure")

    package = parse_package(pkg(2, 0, 10, 0, 10, [(2, 2)]))
    with pytest.raises(RuntimeError):
        ingest(FailingConn(), package)

    # Package 2 must not have been recorded.
    assert conn.execute("SELECT COUNT(*) FROM packages WHERE version = 2").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM coverage_rects WHERE version = 2").fetchone()[0] == 0
    # The delete of (1,1) from the aborted transaction was rolled back.
    assert conn.execute(
        "SELECT COUNT(*) FROM current_wet WHERE row = 1 AND col = 1"
    ).fetchone()[0] == 1
    conn.close()

    # The web app still serves the original state.
    assert cell(client, 1, 1)["status"] == "wet"


def test_sqlite_recovery_after_failure(client):
    """The app keeps serving the previous state after a rejected import."""
    post_pkg(client, pkg(1, 0, 10, 0, 10, [(1, 1)]))
    # Invalid package (out of bounds) is refused before touching the DB.
    bad = pkg(2, 0, 10, 0, 10, [(50, 50)])
    assert post_pkg(client, bad).status_code == 400
    assert cell(client, 1, 1)["status"] == "wet"
    assert cell(client, 50, 50)["status"] == "unsurveyed"
    # And a subsequent good import still works.
    assert post_pkg(client, pkg(3, 0, 10, 0, 10, [])).status_code == 201
    assert cell(client, 1, 1)["status"] == "dry"


def test_concurrent_conflict_is_atomic(tmp_path):
    app = make_app(tmp_path)
    with app.test_client() as c:
        post_pkg(c, pkg(1, 0, 10, 0, 10, [(1, 1)]))
        before = c.get("/api/packages").get_json()
        assert post_pkg(c, pkg(1, 0, 10, 0, 10, [(2, 2)])).status_code == 409
        assert c.get("/api/packages").get_json() == before


def test_json_file_upload(client):
    raw = json.dumps(pkg(1, 0, 10, 0, 10, [(2, 2)])).encode()
    response = client.post(
        "/api/packages",
        data={"file": (__import__("io").BytesIO(raw), "pkg.json")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 201
    assert cell(client, 2, 2)["status"] == "wet"


def test_invalid_json_body(client):
    response = client.post("/api/packages", data="{oops", content_type="application/json")
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_package"


def test_payload_too_large(client):
    client.application.config["MAX_CONTENT_LENGTH"] = 1024
    response = client.post(
        "/api/packages",
        data=json.dumps(pkg(1, 0, 10, 0, 10, [(i, i) for i in range(200)])),
        content_type="application/json",
    )
    assert response.status_code == 413
    assert response.get_json()["error"] == "payload_too_large"
    assert response.get_json()["committed"] is False


def test_storage_error_is_503_and_keeps_state(client):
    post_pkg(client, pkg(1, 0, 10, 0, 10, [(1, 1)]))

    import sqlite3

    from app.db import connect as _connect

    conn = _connect(client.application.config["DATABASE"])

    class FailingConn:
        def __getattr__(self, name):
            return getattr(conn, name)

        def executemany(self, *_a, **_k):
            raise sqlite3.OperationalError("disk I/O error")

    from app.parsing import parse_package
    from app.repository import ingest as _ingest

    with pytest.raises(sqlite3.OperationalError):
        _ingest(FailingConn(), parse_package(pkg(2, 0, 10, 0, 10, [(2, 2)])))
    conn.close()
    assert cell(client, 1, 1)["status"] == "wet"
