"""Shared test fixtures and helpers."""

from __future__ import annotations

import itertools

import pytest

from app import db, model
from app.app import create_app


@pytest.fixture()
def app(tmp_path):
    database = tmp_path / "test.db"
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "DATABASE": str(database),
        }
    )
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def station_doc(
    station_id="T1",
    entrances=("A", "B"),
    platforms=("P1",),
    passages=(),
    name=None,
):
    return {
        "station_id": station_id,
        "name": name or station_id,
        "entrances": list(entrances),
        "platforms": list(platforms),
        "passages": [
            {
                "id": p.get("id", f"p{i}"),
                "from": p["from"],
                "to": p["to"],
                "elevator": p.get("elevator"),
            }
            for i, p in enumerate(passages)
        ],
    }


def two_entrance_doc():
    """A/B entrances, P1 platform; each entrance has its own elevator each way."""

    return station_doc(
        passages=[
            {"from": "entrance:A", "to": "platform:P1", "elevator": "E1"},
            {"from": "platform:P1", "to": "entrance:A", "elevator": "E1"},
            {"from": "entrance:B", "to": "platform:P1", "elevator": "E2"},
            {"from": "platform:P1", "to": "entrance:B", "elevator": "E2"},
        ]
    )


def publish_doc(app, doc):
    """Upload + publish one or more station documents via the web UI."""

    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        try:
            station = model.parse_station(doc)
            with conn:
                db.save_draft(conn, station.station_id, model.station_to_doc(station))
        finally:
            conn.close()
    client = app.test_client()
    return client.post("/admin/publish", data={"note": "test"}, follow_redirects=True)


def with_outages(app, elevators):
    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        try:
            with conn:
                db.replace_outages(conn, list(elevators))
        finally:
            conn.close()


def active_version(app):
    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        try:
            return db.active_version_id(conn)
        finally:
            conn.close()


def api_station(app, station_id):
    return app.test_client().get(f"/api/v1/stations/{station_id}")


def assert_minimal_antichain(tables, platform, direction, station_doc_):
    """Verify a calibrated combination list is a true minimal antichain.

    Every listed set must work when it alone is running; removing any one
    elevator must break it; no listed set may contain another; and no
    working set outside the list may be minimal (i.e. the list is complete).
    """

    station = model.parse_station(station_doc_)
    cell = tables[platform][direction]
    assert cell["possible"] is True
    listed = [frozenset(s) for s in cell["sets"]]
    assert listed, "at least one combination expected"

    all_elevators = set(station.elevators)
    for s in listed:
        assert model.direction_works(
            station, f"platform:{platform}", direction, frozenset(s)
        )
        for elevator in s:
            assert not model.direction_works(
                station,
                f"platform:{platform}",
                direction,
                frozenset(s - {elevator}),
            )
    for a, b in itertools.permutations(listed, 2):
        assert not a < b, f"{a} is a strict subset of {b}"

    # Completeness: brute force every subset of elevators and check that any
    # minimal working set is in the list.
    ordered = sorted(all_elevators)
    working = []
    for size in range(len(ordered) + 1):
        for combo in itertools.combinations(ordered, size):
            fs = frozenset(combo)
            if model.direction_works(
                station, f"platform:{platform}", direction, fs
            ):
                working.append(fs)
    minimal = [
        w for w in working
        if not any(other < w for other in working)
    ]
    assert set(minimal) == set(listed)
