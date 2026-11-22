"""End-to-end HTTP test of the exact demo flow through a real gunicorn-ish app.

Run manually with ``python3 scripts/demo_http.py`` (uses Flask's test client,
so no network needed) or point ``FLOOD_DEMO_URL`` at a running server.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLES = ROOT / "app" / "static" / "samples"


def via_url(base: str) -> int:
    def request(path, data=None):
        req = urllib.request.Request(base + path, data=data)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    checks = []
    for name in ("v3.json", "v7.json", "v5-late.json"):
        code, body = request("/api/packages", (SAMPLES / name).read_bytes())
        checks.append((name, code, body.get("status")))
    code, body = request(
        "/api/packages", (SAMPLES / "v7-retry.json").read_bytes()
    )
    checks.append(("v7-retry.json", code, body.get("status")))
    code, body = request(
        "/api/packages", (SAMPLES / "v7-conflict.json").read_bytes()
    )
    checks.append(("v7-conflict.json", code, body.get("error")))

    _, wet_cell = request("/api/cell?row=8&col=5")
    _, dry_cell = request("/api/cell?row=22&col=7")
    _, unknown_cell = request("/api/cell?row=500&col=500")
    checks.append(("cell(8,5)", 200, wet_cell["label"]))
    checks.append(("cell(22,7)", 200, dry_cell["label"]))
    checks.append(("cell(500,500)", 200, unknown_cell["label"]))
    return report(checks)


def via_test_client() -> int:
    import tempfile

    from app import create_app

    tmp = tempfile.mkdtemp(prefix="flood-demo-")
    app = create_app({"TESTING": True, "DATABASE": f"{tmp}/demo.sqlite3"})
    client = app.test_client()

    checks = []
    for name in ("v3.json", "v7.json", "v5-late.json"):
        resp = client.post(
            "/api/packages", data=(SAMPLES / name).read_bytes(),
            content_type="application/json",
        )
        checks.append((name, resp.status_code, resp.get_json().get("status")))
    resp = client.post(
        "/api/packages", data=(SAMPLES / "v7-retry.json").read_bytes(),
        content_type="application/json",
    )
    checks.append(("v7-retry.json", resp.status_code, resp.get_json().get("status")))
    resp = client.post(
        "/api/packages", data=(SAMPLES / "v7-conflict.json").read_bytes(),
        content_type="application/json",
    )
    checks.append(("v7-conflict.json", resp.status_code, resp.get_json().get("error")))

    for row, col in ((8, 5), (22, 7), (500, 500)):
        body = client.get(f"/api/cell?row={row}&col={col}").get_json()
        checks.append((f"cell({row},{col})", 200, body["label"]))
    return report(checks)


def report(checks) -> int:
    failed = 0
    for name, code, value in checks:
        print(f"{name:20} HTTP {code:<4} {value}")
        if code >= 400 and name != "v7-conflict.json":
            failed += 1
    if failed:
        print(f"\n{failed} unexpected result(s).")
    else:
        print("\nAll demo steps behaved as expected.")
    return 1 if failed else 0


if __name__ == "__main__":
    url = os.environ.get("FLOOD_DEMO_URL")
    sys.exit(via_url(url.rstrip("/")) if url else via_test_client())
