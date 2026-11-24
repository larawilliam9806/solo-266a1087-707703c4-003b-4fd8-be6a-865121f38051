"""Flask application: outage-aware accessible station navigation data.

The web application is intentionally split into a data-entry/publishing side
(upload a station drawing, calibrate, publish) and a read side (dashboard and
per-station JSON for the navigation app).  The read side never walks the
graph: it evaluates the published combination tables against the current
outage list only.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from flask import (
    Flask,
    Response,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import db, model

STATUS_LABELS = {
    "ok": "可进出（需电梯）",
    "no_elevator": "可进出（无须电梯）",
    "blocked": "因停运不通",
    "unreachable": "完全不通",
}

STATUS_CLASSES = {
    "ok": "ok",
    "no_elevator": "plain",
    "blocked": "warn",
    "unreachable": "bad",
}


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)
    app.json.ensure_ascii = False
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-secret"),
        DATABASE=os.environ.get(
            "DATABASE", str(Path(app.root_path) / ".." / "data" / "nav.db")
        ),
    )
    if test_config:
        app.config.update(test_config)

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    db.init_db(app.config["DATABASE"])

    app.teardown_appcontext(_close_db)
    app.jinja_env.globals.update(
        status_label=lambda s: STATUS_LABELS.get(s, s),
        status_class=lambda s: STATUS_CLASSES.get(s, "plain"),
        direction_label=lambda d: (
            "出入口 → 站台" if d == "entrance_to_platform" else "站台 → 出入口"
        ),
    )

    _register_web_routes(app)
    _register_api_routes(app)
    _register_cli(app)
    return app


def _close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = db.connect(current_app.config["DATABASE"])
    return g.db


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _active_context(conn: sqlite3.Connection) -> dict | None:
    version_id = db.active_version_id(conn)
    if version_id is None:
        return None
    maps = db.list_station_maps(conn, version_id)
    if not maps:
        return None
    return {
        "version_id": version_id,
        "stations": maps,
        "outages": db.outage_set(conn),
    }


def _station_views(conn: sqlite3.Connection) -> list[dict]:
    """Evaluate every published station against the current outage list."""

    ctx = _active_context(conn)
    if ctx is None:
        return []
    views = []
    for station_map in ctx["stations"]:
        views.append(
            model.evaluate_station(
                station_map["meta"],
                station_map["tables"],
                ctx["outages"],
                version_id=ctx["version_id"],
            )
        )
    return views


def _elevators_by_station(maps: list[dict]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for station_map in maps:
        meta = station_map["meta"]
        result[meta["station_id"]] = list(meta.get("elevators") or [])
    return result


def _drafts_bundle(conn: sqlite3.Connection) -> tuple[dict[str, dict], str]:
    """Return draft docs keyed by station id plus a fingerprint of the set."""

    docs: dict[str, dict] = {}
    fingerprint = hashlib.sha256()
    for draft in db.list_drafts(conn):
        station_id = draft["doc"].get("station_id") or draft["station_id"]
        docs[station_id] = draft["doc"]
        fingerprint.update(draft["station_id"].encode())
        fingerprint.update(draft["doc"].get("station_id", "").encode())
        fingerprint.update(draft["updated_at"].encode())
        fingerprint.update(
            json.dumps(draft["doc"], sort_keys=True, ensure_ascii=False).encode()
        )
    return docs, fingerprint.hexdigest()


def _merged_docs(
    conn: sqlite3.Connection, drafts: dict[str, dict]
) -> dict[str, dict]:
    """New map = active published drawings overlaid with uploaded drafts."""

    version_id = db.active_version_id(conn)
    merged: dict[str, dict] = {}
    if version_id is not None:
        merged.update(db.version_docs(conn, version_id))
    merged.update(drafts)
    return merged


def _calibrate(docs: dict[str, dict]) -> dict[str, dict]:
    """Parse every document and calibrate its combination tables."""

    calibrated: dict[str, dict] = {}
    for station_id in sorted(docs):
        doc = docs[station_id]
        station = model.parse_station(doc)
        if station.station_id != station_id:
            raise model.SchemaError(
                f"document station_id {station.station_id!r} does not match "
                f"draft key {station_id!r}"
            )
        calibrated[station_id] = {
            "station": station,
            "doc": model.station_to_doc(station),
            "meta": {
                "station_id": station.station_id,
                "name": station.name,
                "entrances": station.entrances,
                "platforms": station.platforms,
                "elevators": station.elevators,
            },
            "tables": model.build_station_tables(station),
        }
    return calibrated


def _api_error(message: str, status: int = 400) -> Response:
    resp = jsonify({"error": message})
    resp.status_code = status
    return resp


def _load_station_view(conn: sqlite3.Connection, station_id: str) -> dict | None:
    version_id = db.active_version_id(conn)
    if version_id is None:
        return None
    station_map = db.get_station_map(conn, version_id, station_id)
    if station_map is None:
        return None
    return model.evaluate_station(
        station_map["meta"],
        station_map["tables"],
        db.outage_set(conn),
        version_id=version_id,
    )


# ---------------------------------------------------------------------------
# Web routes
# ---------------------------------------------------------------------------

def _register_web_routes(app: Flask) -> None:
    @app.get("/")
    def index():
        conn = get_db()
        ctx = _active_context(conn)
        views = _station_views(conn)
        outages = db.list_outages(conn)
        return render_template(
            "index.html",
            views=views,
            outages=outages,
            active_version=ctx["version_id"] if ctx else None,
            drafts=len(db.list_drafts(conn)),
        )

    @app.get("/stations/<station_id>")
    def station_detail(station_id: str):
        conn = get_db()
        view = _load_station_view(conn, station_id)
        if view is None:
            return render_template("station.html", view=None, station_id=station_id), 404
        drafts = {d["station_id"] for d in db.list_drafts(conn)}
        return render_template(
            "station.html",
            view=view,
            station_id=station_id,
            has_draft=station_id in drafts,
            outages=db.list_outages(conn),
        )

    @app.get("/admin/upload")
    def upload_form():
        conn = get_db()
        ctx = _active_context(conn)
        sample = _sample_document()
        return render_template(
            "upload.html",
            sample=json.dumps(sample, ensure_ascii=False, indent=2),
            active_station_ids=sorted(
                m["station_id"] for m in (ctx["stations"] if ctx else [])
            ),
            drafts=db.list_drafts(conn),
        )

    @app.post("/admin/upload")
    def upload_submit():
        raw = request.form.get("document", "")
        upload = request.files.get("file")
        if upload is not None and upload.filename:
            raw = upload.read().decode("utf-8", errors="replace")
        try:
            doc = json.loads(raw)
        except (TypeError, ValueError) as exc:
            flash(f"JSON 解析失败：{exc}", "error")
            return _upload_retry(raw)

        try:
            station = model.parse_station(doc)
        except model.SchemaError as exc:
            flash(f"校验失败：{exc}", "error")
            return _upload_retry(raw)

        form_station_id = request.form.get("station_id", "").strip()
        if form_station_id and form_station_id != station.station_id:
            flash(
                f"表单站点编号 {form_station_id} 与文档 station_id "
                f"{station.station_id} 不一致",
                "error",
            )
            return _upload_retry(raw)

        conn = get_db()
        with conn:
            db.save_draft(conn, station.station_id, model.station_to_doc(station))
        flash(
            f"站点 {station.station_id}（{station.name}）已存为草稿，"
            "发布后才会生效。",
            "info",
        )
        return redirect(url_for("upload_form"))

    def _upload_retry(raw: str):
        ctx = _active_context(get_db())
        return render_template(
            "upload.html",
            sample=json.dumps(_sample_document(), ensure_ascii=False, indent=2),
            active_station_ids=sorted(
                m["station_id"] for m in (ctx["stations"] if ctx else [])
            ),
            drafts=db.list_drafts(get_db()),
            previous=raw,
        )

    @app.post("/admin/drafts/delete")
    def delete_draft():
        station_id = request.form.get("station_id", "")
        conn = get_db()
        with conn:
            removed = db.delete_draft(conn, station_id)
        flash(
            f"已删除草稿 {station_id}" if removed else f"草稿 {station_id} 不存在",
            "info" if removed else "error",
        )
        return redirect(url_for("upload_form"))

    @app.get("/admin/publish")
    def publish_page():
        conn = get_db()
        ctx = _active_context(conn)
        active_docs = (
            db.version_docs(conn, ctx["version_id"]) if ctx else {}
        )
        drafts = db.list_drafts(conn)
        merged = _merged_docs(
            conn, {d["doc"].get("station_id", d["station_id"]): d["doc"] for d in drafts}
        )
        changes = []
        for station_id in sorted(merged):
            old = active_docs.get(station_id)
            draft = next(
                (d for d in drafts
                 if d["doc"].get("station_id", d["station_id"]) == station_id),
                None,
            )
            changes.append(
                {
                    "station_id": station_id,
                    "name": merged[station_id].get("name", station_id),
                    "state": "new" if old is None else ("draft" if draft else "unchanged"),
                }
            )
        return render_template(
            "publish.html",
            drafts=drafts,
            changes=changes,
            active_version=ctx["version_id"] if ctx else None,
            outages=db.list_outages(conn),
            log=db.list_publish_log(conn),
            versions=db.list_versions(conn),
        )

    @app.post("/admin/publish")
    def publish_submit():
        conn = get_db()
        note = request.form.get("note", "").strip()

        drafts, fingerprint = _drafts_bundle(conn)
        if not drafts:
            flash("没有草稿可发布。请先上传站点 JSON。", "error")
            return redirect(url_for("publish_page"))

        merged = _merged_docs(conn, drafts)

        # Calibration happens without holding the SQLite write lock, so the
        # outage list stays editable while the drawing is being calibrated.
        try:
            calibrated = _calibrate(merged)
        except model.SchemaError as exc:
            _record_publish_failure(conn, f"核算失败：{exc}")
            flash(f"核算失败，继续使用旧图：{exc}", "error")
            return redirect(url_for("publish_page"))
        except Exception as exc:  # pragma: no cover - defensive
            _record_publish_failure(conn, f"核算异常：{exc}")
            flash(f"核算异常，继续使用旧图：{exc}", "error")
            return redirect(url_for("publish_page"))

        try:
            version_id = _commit_publish(conn, calibrated, note, fingerprint)
        except _PublishAbort as exc:
            _record_publish_failure(conn, f"发布中止：{exc}")
            flash(f"发布中止，继续使用旧图：{exc}", "error")
            return redirect(url_for("publish_page"))
        except Exception as exc:  # pragma: no cover - defensive
            _record_publish_failure(conn, f"保存失败：{exc}")
            flash(f"保存失败，继续使用旧图：{exc}", "error")
            return redirect(url_for("publish_page"))

        flash(f"已发布地图版本 {version_id}，组合表与当前停运名单一同生效。", "info")
        return redirect(url_for("publish_page"))

    @app.get("/admin/outages")
    def outages_page():
        conn = get_db()
        ctx = _active_context(conn)
        maps = ctx["stations"] if ctx else []
        return render_template(
            "outages.html",
            outages=db.list_outages(conn),
            elevators_by_station=_elevators_by_station(maps),
            active_version=ctx["version_id"] if ctx else None,
        )

    @app.post("/admin/outages")
    def outages_submit():
        conn = get_db()
        action = request.form.get("action", "add")
        elevator = request.form.get("elevator", "").strip()
        station_id = request.form.get("station_id", "").strip() or None
        note = request.form.get("note", "").strip()

        if action == "add":
            if not elevator:
                flash("电梯编号不能为空", "error")
            else:
                with conn:
                    db.set_outage(conn, elevator, True, station_id, note)
                flash(f"{elevator} 已加入停运名单", "info")
        elif action == "remove":
            if not elevator:
                flash("电梯编号不能为空", "error")
            else:
                with conn:
                    db.set_outage(conn, elevator, False)
                flash(f"{elevator} 已从停运名单移除", "info")
        elif action == "replace":
            raw = request.form.get("elevators", "")
            names = [
                part.strip()
                for part in raw.replace("，", ",").replace("\n", ",").split(",")
                if part.strip()
            ]
            with conn:
                db.replace_outages(conn, names)
            flash(f"停运名单已更新，共 {len(set(names))} 台", "info")
        else:
            flash("未知操作", "error")
        return redirect(url_for("outages_page"))

    @app.get("/healthz")
    def healthz():
        get_db().execute("SELECT 1").fetchone()
        return jsonify({"ok": True})


class _PublishAbort(RuntimeError):
    pass


def _record_publish_failure(conn: sqlite3.Connection, message: str) -> None:
    try:
        db.log_publish(conn, "failed", message)
    except Exception:  # pragma: no cover - logging must not mask the error
        conn.rollback()


def _commit_publish(
    conn: sqlite3.Connection,
    calibrated: dict[str, dict],
    note: str,
    draft_fingerprint: str,
) -> int:
    """Atomically publish tables + switch the active pointer.

    The current outage list is read *inside* the write transaction, so the
    consistency check and the version switch always use the latest list even
    if data staff edited outages during calibration.
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        fresh_drafts, fresh_fingerprint = _drafts_bundle(conn)
        if fresh_fingerprint != draft_fingerprint:
            raise _PublishAbort("核算期间草稿被修改，请重新发布")

        # Re-read the outage list inside the transaction: it may have been
        # edited while calibration was running.
        outages = db.list_outages(conn)
        problems = model.check_outaged_elevators_present(
            {sid: info["station"] for sid, info in calibrated.items()}, outages
        )
        if problems:
            raise _PublishAbort("；".join(problems))

        cur = conn.execute(
            "INSERT INTO map_versions (created_at, note) VALUES (?, ?)",
            (db.utcnow(), note),
        )
        version_id = cur.lastrowid
        for station_id in sorted(calibrated):
            info = calibrated[station_id]
            conn.execute(
                "INSERT INTO station_maps "
                "(version_id, station_id, name, doc, meta, tables) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    version_id,
                    station_id,
                    info["station"].name,
                    model.dumps(info["doc"]),
                    model.dumps(info["meta"]),
                    model.dumps(info["tables"]),
                ),
            )
        db.set_setting(conn, "active_version_id", str(version_id))
        # Drafts are now part of the active drawing; the next publish starts
        # from this version.  No draft can slip in between the fingerprint
        # check above and here: BEGIN IMMEDIATE holds the write lock.
        db.clear_drafts(conn)
        db.log_publish(
            conn,
            "success",
            f"发布 {len(calibrated)} 个站点；停运名单 {len(outages)} 台",
            version_id,
            commit=False,
        )
        conn.commit()
        return version_id
    except BaseException:
        conn.rollback()
        raise
# ---------------------------------------------------------------------------
# API routes (navigation client)
# ---------------------------------------------------------------------------

def _register_api_routes(app: Flask) -> None:
    @app.get("/api/v1/version")
    def api_version():
        conn = get_db()
        version_id = db.active_version_id(conn)
        if version_id is None:
            return jsonify({"active_version": None, "stations": 0})
        rows = db.list_station_maps(conn, version_id)
        return jsonify(
            {
                "active_version": version_id,
                "stations": len(rows),
                "station_ids": [r["station_id"] for r in rows],
            }
        )

    @app.get("/api/v1/outages")
    def api_outages():
        conn = get_db()
        return jsonify(
            {
                "outages": [
                    {
                        "elevator": r["elevator"],
                        "station_id": r["station_id"],
                        "note": r["note"],
                        "updated_at": r["updated_at"],
                    }
                    for r in db.list_outages(conn)
                ]
            }
        )

    @app.get("/api/v1/stations")
    def api_stations():
        conn = get_db()
        views = _station_views(conn)
        version_id = db.active_version_id(conn)
        return jsonify(
            {
                "active_version": version_id,
                "stations": [
                    {
                        "station_id": v["station_id"],
                        "name": v["name"],
                        "status_summary": _status_summary(v),
                    }
                    for v in views
                ],
            }
        )

    @app.get("/api/v1/stations/<station_id>")
    def api_station(station_id: str):
        conn = get_db()
        version_id = db.active_version_id(conn)
        if version_id is None:
            return _api_error("尚未发布任何地图版本", 503)
        view = _load_station_view(conn, station_id)
        if view is None:
            return _api_error(f"站点 {station_id} 不在已生效地图中", 404)
        return jsonify(view)


def _status_summary(view: dict) -> dict:
    summary = {"ok": 0, "no_elevator": 0, "blocked": 0, "unreachable": 0}
    for platform in view["platforms"]:
        summary[platform["status"]] = summary.get(platform["status"], 0) + 1
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _register_cli(app: Flask) -> None:
    @app.cli.command("seed-demo")
    def seed_demo_command():
        """Load a small demo station as draft and publish it."""

        from .demo import DEMO_STATIONS

        conn = db.connect(app.config["DATABASE"])
        try:
            with conn:
                for doc in DEMO_STATIONS:
                    station = model.parse_station(doc)
                    db.save_draft(
                        conn, station.station_id, model.station_to_doc(station)
                    )
                if not db.list_outages(conn):
                    for elevator in ("S1-E2",):
                        db.set_outage(conn, elevator, True, "S1", "例行检修")
        finally:
            conn.close()
        print("demo drafts written; open /admin/publish to publish")

    @app.cli.command("init-db")
    def init_db_command():
        """Create database tables."""

        db.init_db(app.config["DATABASE"])
        print(f"initialised {app.config['DATABASE']}")


# ---------------------------------------------------------------------------
# Demo document used by the upload form
# ---------------------------------------------------------------------------

def _sample_document() -> dict:
    from .demo import DEMO_STATIONS

    return DEMO_STATIONS[0]
