from __future__ import annotations

import pytest

from .conftest import make_app, post_pkg


def pkg(version, r0, r1, c0, c1, cells=()):
    return {
        "version": version,
        "coverage": {
            "row_start": r0,
            "row_end": r1,
            "col_start": c0,
            "col_end": c1,
        },
        "wet_cells": [list(c) for c in cells],
    }


@pytest.fixture()
def client(tmp_path):
    app = make_app(tmp_path)
    with app.test_client() as c:
        yield c


def test_valid_package_returns_201(client):
    response = post_pkg(client, pkg(1, 0, 10, 0, 10, [(2, 3)]))
    assert response.status_code == 201
    body = response.get_json()
    assert body["status"] == "imported"
    assert body["version"] == 1
    assert body["summary"]["current_wet_added"] == 1


@pytest.mark.parametrize("version", [0, -1, 1.5, "3", None, True])
def test_version_must_be_positive_integer(client, version):
    response = post_pkg(client, pkg(version, 0, 5, 0, 5))
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_package"


@pytest.mark.parametrize(
    "coverage",
    [
        {"row_start": 0, "row_end": 0, "col_start": 0, "col_end": 5},
        {"row_start": 5, "row_end": 4, "col_start": 0, "col_end": 5},
        {"row_start": 0, "row_end": 5, "col_start": 0, "col_end": 0},
        {"row_start": 0.5, "row_end": 5, "col_start": 0, "col_end": 5},
        {"row_start": 0, "row_end": 5, "col_start": 0},
    ],
)
def test_bad_coverage_rejected(client, coverage):
    response = post_pkg(client, {"version": 1, "coverage": coverage, "wet_cells": []})
    assert response.status_code == 400


def test_wet_cell_outside_coverage_rejected(client):
    response = post_pkg(client, pkg(1, 0, 10, 0, 10, [(10, 0)]))
    assert response.status_code == 400
    assert "覆盖矩形之外" in " ".join(response.get_json()["details"])


def test_half_open_boundaries(client):
    # row_end / col_end are exclusive; row_start / col_start inclusive.
    response = post_pkg(client, pkg(1, 0, 10, 0, 10, [(9, 9)]))
    assert response.status_code == 201
    assert client.get("/api/cell?row=10&col=0").get_json()["status"] == "unsurveyed"
    assert client.get("/api/cell?row=0&col=0").get_json()["status"] == "dry"
    assert client.get("/api/cell?row=9&col=9").get_json()["status"] == "wet"


def test_bool_is_not_an_integer_coordinate(client):
    response = post_pkg(client, pkg(1, 0, 10, 0, 10, [(True, 2)]))
    assert response.status_code == 400


def test_unknown_fields_rejected(client):
    payload = pkg(1, 0, 10, 0, 10)
    payload["wet_cell"] = [[1, 1]]  # typo must not silently mean "all dry"
    response = post_pkg(client, payload)
    assert response.status_code == 400
    assert "未知字段" in response.get_json()["message"]


def test_duplicate_cells_are_collapsed_and_hash_stable(client, tmp_path):
    with_pairs = pkg(1, 0, 10, 0, 10, [(2, 2), (2, 2), (1, 1)])
    first = post_pkg(client, with_pairs).get_json()
    assert first["summary"]["current_wet_added"] == 2
    # Same content, different list order -> same version is "unchanged".
    reordered = pkg(1, 0, 10, 0, 10, [(1, 1), (2, 2)])
    second = post_pkg(client, reordered)
    assert second.status_code == 200
    assert second.get_json()["status"] == "unchanged"


def test_wet_cell_limit(client):
    from app.parsing import MAX_WET_CELLS

    cells = [(i % 10, (i // 10) % 10) for i in range(MAX_WET_CELLS + 1)]
    response = post_pkg(client, pkg(1, 0, 10, 0, 10, cells[:10]))
    assert response.status_code == 201
    # A package whose listed points exceed the cap is refused outright.
    too_many = pkg(2, 0, 1000, 0, 1000, cells)
    assert post_pkg(client, too_many).status_code == 400


def test_coverage_area_is_not_expanded(client):
    # 1,000 x 1,000 = 1e6 cells; must import without materialising them.
    response = post_pkg(client, pkg(1, 0, 1000, 0, 1000, [(500, 500)]))
    assert response.status_code == 201
    assert response.get_json()["summary"]["coverage_area"] == 1_000_000
    assert client.get("/api/cell?row=999&col=999").get_json()["status"] == "dry"


def test_coordinate_overflow_rejected(client):
    response = post_pkg(client, pkg(1, 0, 3_000_000_000, 0, 5))
    assert response.status_code == 400
