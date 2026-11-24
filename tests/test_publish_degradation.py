"""End-to-end tests for the publish-page degradation check."""

from __future__ import annotations

import json

from app import db, model

from .conftest import active_version, api_station, publish_doc, station_doc


def _parallel_routes(elevators, platforms=("P1",)):
    """One entrance, one platform, one two-way route per elevator."""

    passages = []
    for i, elevator in enumerate(elevators):
        passages.append(
            {"id": f"in-{i}", "from": "entrance:A",
             "to": "platform:P1", "elevator": elevator}
        )
        passages.append(
            {"id": f"out-{i}", "from": "platform:P1",
             "to": "entrance:A", "elevator": elevator}
        )
    return station_doc(entrances=("A",), platforms=tuple(platforms), passages=passages)


def _upload(app, client, doc):
    return client.post(
        "/admin/upload",
        data={"document": json.dumps(doc)},
        follow_redirects=True,
    )


def test_publish_blocked_on_degradation_keeps_active_version_and_drafts(app, client):
    publish_doc(app, _parallel_routes(["E1", "E2"]))
    first = active_version(app)

    # New drawing keeps only the E1 route: outage {E1} would block the new
    # drawing while the old one still works via E2.
    _upload(app, client, _parallel_routes(["E1"]))
    resp = client.post("/admin/publish", data={"note": "regress"}, follow_redirects=True)

    assert "换图退化".encode() in resp.data
    assert active_version(app) == first

    # Active version keeps serving the old drawing.
    body = api_station(app, "T1").get_json()
    assert body["version_id"] == first
    assert "E2" in body["elevators"]

    # The draft is preserved for another editing round.
    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        try:
            drafts = db.list_drafts(conn)
            log = db.list_publish_log(conn)
        finally:
            conn.close()
    assert [d["station_id"] for d in drafts] == ["T1"]
    assert log[0]["status"] == "failed"


def test_publish_preview_lists_trigger_and_old_working_set(app, client):
    publish_doc(app, _parallel_routes(["E1", "E2"]))
    _upload(app, client, _parallel_routes(["E1"]))

    page = client.get("/admin/publish").data.decode("utf-8")
    assert "核查未通过" in page
    assert "E1" in page
    # The old drawing's surviving combination is shown as the witness.
    assert "E2" in page


def test_publish_allowed_when_no_degradation(app, client):
    publish_doc(app, _parallel_routes(["E1", "E2"]))
    first = active_version(app)

    # Adding a redundant route can only make directions easier, never worse.
    _upload(app, client, _parallel_routes(["E1", "E2", "E3"]))
    resp = client.post("/admin/publish", data={"note": "safer"}, follow_redirects=True)
    assert "已发布".encode() in resp.data
    assert active_version(app) > first


def test_first_publish_skips_the_check(app, client):
    _upload(app, client, _parallel_routes(["E1"]))
    # Before the first publish there is no active version to compare against.
    page = client.get("/admin/publish").data.decode("utf-8")
    assert "首次发布没有旧图可对比" in page
    resp = client.post("/admin/publish", data={"note": "init"}, follow_redirects=True)
    assert "已发布".encode() in resp.data


def test_deleted_platform_blocks_publish(app, client):
    publish_doc(app, _parallel_routes(["E1"]))
    first = active_version(app)

    replacement = station_doc(
        entrances=("A",), platforms=("P2",),
        passages=[
            {"from": "entrance:A", "to": "platform:P2"},
            {"from": "platform:P2", "to": "entrance:A"},
        ],
    )
    _upload(app, client, replacement)
    resp = client.post("/admin/publish", data={}, follow_redirects=True)
    assert "换图退化".encode() in resp.data
    assert active_version(app) == first
    page = client.get("/admin/publish").data.decode("utf-8")
    assert "空名单即退化" in page


def test_outage_list_edits_during_check_are_honoured_at_commit(app, client, monkeypatch):
    # The degradation check ignores the outage list by design, but the
    # existing outage consistency check must still run against the latest
    # list read inside the commit transaction.
    publish_doc(app, _parallel_routes(["E1", "E2"]))

    safer = _parallel_routes(["E1", "E2", "E3"])
    _upload(app, client, safer)

    original = model.check_version_degradation
    calls = {"n": 0}

    def add_outage_then_check(old_maps, new_calibrated):
        calls["n"] += 1
        if calls["n"] == 1:
            # The pre-transaction check runs without the write lock, so the
            # outage list stays editable there.
            conn = db.connect(app.config["DATABASE"])
            try:
                with conn:
                    db.set_outage(conn, "E9", True, "T1")
            finally:
                conn.close()
        return original(old_maps, new_calibrated)

    monkeypatch.setattr(model, "check_version_degradation", add_outage_then_check)
    resp = client.post("/admin/publish", data={}, follow_redirects=True)
    # E9 is missing from the new drawing; the latest list blocks the publish.
    assert "缺少该电梯".encode() in resp.data


def test_publish_aborts_when_active_version_changes_during_calibration(app, monkeypatch):
    publish_doc(app, _parallel_routes(["E1", "E2"]))

    client = app.test_client()
    _upload(app, client, _parallel_routes(["E1", "E2", "E3"]))

    original = model.build_station_tables

    def switch_version(station):
        # Simulate another successful publish while calibration is running by
        # pointing the active setting at a freshly inserted version row.
        conn = db.connect(app.config["DATABASE"])
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO map_versions (created_at, note) VALUES (?, ?)",
                    (db.utcnow(), "concurrent"),
                )
                db.set_setting(conn, "active_version_id", str(cur.lastrowid))
        finally:
            conn.close()
        return original(station)

    monkeypatch.setattr(model, "build_station_tables", switch_version)
    resp = client.post("/admin/publish", data={}, follow_redirects=True)
    assert "生效版本已变化".encode() in resp.data


def test_publish_aborts_when_draft_changes_during_calibration(app, monkeypatch):
    publish_doc(app, _parallel_routes(["E1", "E2"]))

    client = app.test_client()
    _upload(app, client, _parallel_routes(["E1", "E2", "E3"]))

    original = model.check_version_degradation
    calls = {"n": 0}

    def tamper_drafts_then_check(old_maps, new_calibrated):
        calls["n"] += 1
        if calls["n"] == 1:
            conn = db.connect(app.config["DATABASE"])
            try:
                extra = station_doc(
                    station_id="T2", entrances=("Z",), platforms=("Q1",),
                    passages=[
                        {"from": "entrance:Z", "to": "platform:Q1"},
                        {"from": "platform:Q1", "to": "entrance:Z"},
                    ],
                )
                with conn:
                    db.save_draft(conn, "T2", extra)
            finally:
                conn.close()
        return original(old_maps, new_calibrated)

    monkeypatch.setattr(model, "check_version_degradation", tamper_drafts_then_check)
    resp = client.post("/admin/publish", data={}, follow_redirects=True)
    assert "草稿被修改".encode() in resp.data


def test_preview_renders_pass_state_when_clean(app, client):
    publish_doc(app, _parallel_routes(["E1", "E2"]))
    _upload(app, client, _parallel_routes(["E1", "E2", "E3"]))
    page = client.get("/admin/publish").data.decode("utf-8")
    assert "核查通过" in page


def test_in_transaction_recheck_blocks_concurrently_introduced_regression(app, monkeypatch):
    publish_doc(app, _parallel_routes(["E1", "E2"]))

    client = app.test_client()
    # The outside-transaction check sees a safe, redundant-route drawing.
    _upload(app, client, _parallel_routes(["E1", "E2", "E3"]))

    calls = {"n": 0}
    original = model.check_version_degradation

    def degrading_second_call(old_maps, new_calibrated):
        calls["n"] += 1
        if calls["n"] == 1:
            return original(old_maps, new_calibrated)
        # Inside the write lock: pretend a concurrent change now shows a
        # regression for the same calibrated tables.
        return {
            "findings": [
                {
                    "station_id": "T1",
                    "platform": "P1",
                    "direction": "entrance_to_platform",
                    "outage_elevators": ["E1"],
                    "old_working_set": ["E2"],
                }
            ],
            "checked_lists": 4,
        }

    monkeypatch.setattr(model, "check_version_degradation", degrading_second_call)
    before = active_version(app)
    resp = client.post("/admin/publish", data={}, follow_redirects=True)
    assert "换图退化".encode() in resp.data
    assert active_version(app) == before
