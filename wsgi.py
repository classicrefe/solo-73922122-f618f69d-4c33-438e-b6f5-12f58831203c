"""Production-ish entry point: ``python -m app_server`` or a WSGI server."""

from __future__ import annotations

import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLOOD_HOST", "127.0.0.1"),
        port=int(os.environ.get("FLOOD_PORT", "8000")),
        debug=False,
    )
