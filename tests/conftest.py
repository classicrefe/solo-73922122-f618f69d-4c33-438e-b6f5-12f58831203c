from __future__ import annotations

import json
from pathlib import Path

from app import create_app

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "app" / "static" / "samples"


def make_app(tmp_path: Path, **overrides):
    config = {
        "TESTING": True,
        "DATABASE": str(tmp_path / "test.sqlite3"),
        "ALLOW_RESET": True,
    }
    config.update(overrides)
    return create_app(config)


def sample(name: str) -> dict:
    return json.loads((SAMPLE_DIR / name).read_text(encoding="utf-8"))


def post_pkg(client, payload):
    return client.post("/api/packages", data=json.dumps(payload), content_type="application/json")


def cell(client, row, col):
    response = client.get(f"/api/cell?row={row}&col={col}")
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def wet(client, r0, r1, c0, c1, limit=2000):
    response = client.get(
        f"/api/wet?row_start={r0}&row_end={r1}&col_start={c0}&col_end={c1}&limit={limit}"
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()
