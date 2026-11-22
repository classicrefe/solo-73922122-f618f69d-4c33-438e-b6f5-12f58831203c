from __future__ import annotations

import pytest

from .conftest import cell, make_app, post_pkg, wet
from .test_parsing import pkg


@pytest.fixture()
def client(tmp_path):
    app = make_app(tmp_path)
    with app.test_client() as c:
        yield c


def test_newer_version_overrides_overlap(client):
    post_pkg(client, pkg(1, 0, 10, 0, 10, [(5, 5)]))
    assert cell(client, 5, 5)["status"] == "wet"

    # Version 2 covers the cell but does not list it -> dry.
    post_pkg(client, pkg(2, 0, 10, 0, 10, []))
    assert cell(client, 5, 5) == {
        "row": 5,
        "col": 5,
        "status": "dry",
        "version": 2,
        "label": "已查无积水",
    }


def test_newer_version_raw_package_without_pond_reports_dry(client):
    # The headline scenario: a resweep with no pond means the water receded.
    post_pkg(client, pkg(1, 0, 20, 0, 20, [(8, 4), (8, 5), (9, 4)]))
    assert wet(client, 0, 20, 0, 20)["count"] == 3
    post_pkg(client, pkg(2, 0, 20, 0, 20, []))
    result = wet(client, 0, 20, 0, 20)
    assert result["cells"] == []
    assert cell(client, 8, 4)["status"] == "dry"


def test_late_older_package_only_fills_uncovered_area(client):
    post_pkg(client, pkg(10, 0, 10, 0, 10, [(1, 1)]))
    # Late v7 reaches into rows/cols 10..15, which v10 never surveyed.
    response = post_pkg(client, pkg(7, 5, 15, 5, 15, [(12, 12), (14, 14)]))
    assert response.status_code == 201
    summary = response.get_json()["summary"]
    assert summary["current_wet_added"] == 2
    assert response.get_json()["late_arrival"] is True
    # v10's verdict for (1,1) survives outside v7's rectangle.
    assert cell(client, 1, 1)["status"] == "wet"


def test_late_older_package_cannot_clear_newer_dry_verdict(client):
    # v10 surveyed (8,8) and found it dry.  A late v5 listing it wet must not
    # resurrect the water: newer coverage wins.
    post_pkg(client, pkg(10, 0, 10, 0, 10, []))
    response = post_pkg(client, pkg(5, 5, 15, 5, 15, [(8, 8), (14, 14)]))
    summary = response.get_json()["summary"]
    assert summary["wet_points_shadowed_by_newer"] == 1
    assert summary["current_wet_added"] == 1
    assert cell(client, 8, 8)["status"] == "dry"
    assert cell(client, 8, 8)["version"] == 10
    assert cell(client, 14, 14)["status"] == "wet"
    assert cell(client, 14, 14)["version"] == 5


def test_late_older_package_cannot_overwrite_newer_wet_verdict(client):
    post_pkg(client, pkg(10, 5, 15, 5, 15, [(8, 8)]))
    # Late v5 covers (8,8) but lists no water.  It must neither clear the newer
    # wet verdict nor claim the cell.
    response = post_pkg(client, pkg(5, 0, 10, 0, 10, []))
    assert response.get_json()["summary"]["current_wet_removed"] == 0
    status = cell(client, 8, 8)
    assert status["status"] == "wet"
    assert status["version"] == 10


def test_same_version_same_content_is_noop(client):
    first = post_pkg(client, pkg(3, 0, 10, 0, 10, [(2, 2)]))
    assert first.status_code == 201
    before = wet(client, 0, 10, 0, 10)

    content = pkg(3, 0, 10, 0, 10, [(2, 2)])
    content = {k: content[k] for k in ("wet_cells", "coverage", "version")}  # key order differs
    second = post_pkg(client, content)
    assert second.status_code == 200
    body = second.get_json()
    assert body["status"] == "unchanged"
    assert body["digest"] == first.get_json()["digest"]
    assert wet(client, 0, 10, 0, 10) == before


def test_same_version_different_content_is_rejected_whole(client):
    post_pkg(client, pkg(3, 0, 10, 0, 10, [(2, 2)]))
    response = post_pkg(client, pkg(3, 0, 10, 0, 10, [(2, 2), (3, 3)]))
    assert response.status_code == 409
    assert response.get_json()["error"] == "version_conflict"
    assert cell(client, 3, 3)["status"] == "dry"
    assert cell(client, 2, 2)["status"] == "wet"


def test_conflict_after_late_package_does_not_change_anything(client):
    post_pkg(client, pkg(9, 0, 10, 0, 10, [(1, 1)]))
    before = wet(client, 0, 20, 0, 20)
    response = post_pkg(client, pkg(9, 10, 20, 10, 20, [(15, 15)]))
    assert response.status_code == 409
    assert wet(client, 0, 20, 0, 20) == before


def test_cell_states(client):
    post_pkg(client, pkg(1, 0, 10, 0, 10, [(5, 5)]))
    assert cell(client, 5, 5)["status"] == "wet"
    assert cell(client, 5, 6)["status"] == "dry"
    assert cell(client, 10, 5)["status"] == "unsurveyed"


def test_package_listing_counts(client):
    post_pkg(client, pkg(1, 0, 10, 0, 10, [(1, 1), (2, 2)]))
    post_pkg(client, pkg(2, 0, 5, 0, 5, [(1, 1)]))
    body = client.get("/api/packages").get_json()
    assert body["max_version"] == 2
    by_version = {p["version"]: p for p in body["packages"]}
    assert by_version[1]["listed_wet_points"] == 2
    # v2 re-surveyed the whole 0..5 box: it cleared (2,2) and now owns (1,1).
    assert by_version[1]["current_wet_points"] == 0
    assert by_version[2]["current_wet_points"] == 1
    assert body["total_current_wet"] == 1
