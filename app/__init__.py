from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from flask import Flask, current_app, jsonify, render_template, request

from .db import close_db, connect, get_db, init_db
from .parsing import MAX_WET_CELLS, PackageError, parse_package
from .repository import (
    Box,
    ConflictError,
    cell_status,
    coverage_in_box,
    ingest,
    list_packages,
    query_wet,
)

DEFAULT_LIMIT = 2000
MAX_LIMIT = 5000
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = str(PROJECT_ROOT / "instance" / "flood.sqlite3")


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE=os.environ.get("FLOOD_DB", DEFAULT_DB),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        ALLOW_RESET=os.environ.get("FLOOD_ALLOW_RESET", "0") == "1",
        PREFER_RTREE=True,
    )
    if config:
        app.config.update(config)

    db_path = app.config["DATABASE"]
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    bootstrap = connect(db_path)
    init_db(bootstrap, prefer_rtree=app.config["PREFER_RTREE"])
    bootstrap.close()

    app.teardown_appcontext(close_db)
    app.register_error_handler(PackageError, _handle_package_error)
    app.register_error_handler(ConflictError, _handle_conflict)
    app.register_error_handler(sqlite3.Error, _handle_sqlite_error)
    app.register_error_handler(413, _handle_too_large)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/healthz")
    def healthz():
        get_db().execute("SELECT 1").fetchone()
        return jsonify({"status": "ok"})

    @app.get("/api/packages")
    def packages():
        return jsonify(list_packages(get_db()))

    @app.post("/api/packages")
    def import_package():
        package = parse_package(_read_payload())
        result = ingest(get_db(), package)
        return jsonify(result), 200 if result["status"] == "unchanged" else 201

    @app.get("/api/wet")
    def wet_boxes():
        box = _parse_box(request.args)
        limit = _parse_limit(request.args)
        conn = get_db()
        result = query_wet(conn, box, limit)
        result["coverage_rects"] = coverage_in_box(conn, box)
        return jsonify(result)

    @app.get("/api/cell")
    def cell():
        row = _parse_int_arg("row")
        col = _parse_int_arg("col")
        return jsonify(cell_status(get_db(), row, col))

    @app.get("/api/limits")
    def limits():
        return jsonify(
            {
                "max_wet_cells": MAX_WET_CELLS,
                "default_query_limit": DEFAULT_LIMIT,
                "max_query_limit": MAX_LIMIT,
            }
        )

    @app.post("/api/reset")
    def reset():
        # Demo helper only.  Refuses to run unless explicitly enabled.
        if not app.config.get("ALLOW_RESET"):
            return jsonify({"error": "reset_disabled", "message": "演示库清空未启用。"}), 403
        conn = get_db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM current_wet")
            conn.execute("DELETE FROM coverage_rects")
            conn.execute("DELETE FROM packages")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
        return jsonify({"status": "reset"})

    return app


def _read_payload():
    """Accept a ``file`` upload, a raw JSON body, or a JSON object body."""
    if "file" in request.files:
        data = request.files["file"].read()
        if not data:
            raise PackageError("上传文件为空。")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise PackageError("上传文件不是 UTF-8 编码。") from exc
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise PackageError(
                "上传文件不是合法 JSON。",
                details=[f"第 {exc.lineno} 行第 {exc.colno} 列: {exc.msg}"],
            ) from exc

    raw = request.get_data(cache=True)
    if not raw:
        raise PackageError("请求体为空，请上传 JSON 文件或直接提交 JSON。")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise PackageError("请求体不是 UTF-8 编码。") from exc
    except json.JSONDecodeError as exc:
        raise PackageError(
            "请求体不是合法 JSON。",
            details=[f"第 {exc.lineno} 行第 {exc.colno} 列: {exc.msg}"],
        ) from exc


def _handle_package_error(exc: PackageError):
    return (
        jsonify(
            {
                "error": "invalid_package",
                "message": exc.message,
                "details": exc.details,
                "committed": False,
            }
        ),
        400,
    )


def _handle_conflict(exc: ConflictError):
    return (
        jsonify(
            {
                "error": "version_conflict",
                "message": str(exc),
                "committed": False,
            }
        ),
        409,
    )


def _handle_sqlite_error(exc: sqlite3.Error):
    # The transaction helper has already rolled back.
    current_app.logger.exception("sqlite failure")
    return (
        jsonify(
            {
                "error": "storage_error",
                "message": "存储暂时不可用，已保留原有结果，请稍后重试。",
                "details": [],
                "committed": False,
            }
        ),
        503,
    )


def _handle_too_large(_exc):
    return (
        jsonify(
            {
                "error": "payload_too_large",
                "message": "上传文件超过大小上限。",
                "details": [],
                "committed": False,
            }
        ),
        413,
    )


def _parse_int_arg(name: str) -> int:
    raw = request.args.get(name)
    if raw in (None, ""):
        raise PackageError(f"缺少查询参数 {name}。")
    try:
        value = int(raw)
    except ValueError as exc:
        raise PackageError(f"{name} 必须是整数。") from exc
    if abs(value) > 2_000_000_000:
        raise PackageError(f"{name} 超出允许范围 ±2000000000。")
    return value


def _parse_box(args) -> Box:
    names = ("row_start", "row_end", "col_start", "col_end")
    missing = [n for n in names if args.get(n) in (None, "")]
    if missing:
        raise PackageError(f"框查缺少参数: {', '.join(missing)}。")
    box = Box(*(_parse_int_arg(n) for n in names))
    if box.row_end <= box.row_start or box.col_end <= box.col_start:
        raise PackageError("框查范围必须满足 start < end（左闭右开且非空）。")
    return box


def _parse_limit(args) -> int:
    raw = args.get("limit")
    if raw in (None, ""):
        return DEFAULT_LIMIT
    try:
        limit = int(raw)
    except ValueError as exc:
        raise PackageError("limit 必须是整数。") from exc
    if limit <= 0:
        raise PackageError("limit 必须是正整数。")
    return min(limit, MAX_LIMIT)
