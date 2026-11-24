"""Unit tests for station calibration and table-only evaluation."""

from __future__ import annotations

import itertools

import pytest

from app import model
from app.demo import DEMO_STATIONS

from .conftest import (
    assert_minimal_antichain,
    publish_doc,
    station_doc,
    two_entrance_doc,
    with_outages,
)


def meta_for(station):
    return {
        "station_id": station.station_id,
        "name": station.name,
        "entrances": station.entrances,
        "platforms": station.platforms,
        "elevators": station.elevators,
    }


def test_parse_rejects_unknown_node():
    doc = station_doc(passages=[{"from": "entrance:A", "to": "platform:P9"}])
    with pytest.raises(model.SchemaError):
        model.parse_station(doc)


def test_parse_rejects_more_than_ten_elevators():
    passages = [
        {"from": "entrance:A", "to": "platform:P1", "elevator": f"E{i}"}
        for i in range(11)
    ]
    with pytest.raises(model.SchemaError):
        model.parse_station(station_doc(passages=passages))


def test_parse_allows_ten_elevators():
    passages = [
        {"from": "entrance:A", "to": "platform:P1", "elevator": f"E{i}"}
        for i in range(10)
    ]
    station = model.parse_station(station_doc(passages=passages))
    assert len(station.elevators) == 10


def test_minimal_sets_two_independent_routes():
    station = model.parse_station(two_entrance_doc())
    tables = model.build_station_tables(station)
    assert tables["P1"]["entrance_to_platform"]["sets"] == [["E1", "E2"]]
    assert tables["P1"]["platform_to_entrance"]["sets"] == [["E1", "E2"]]


def test_antichain_is_complete_and_minimal_on_demo():
    for doc in DEMO_STATIONS:
        station = model.parse_station(doc)
        tables = model.build_station_tables(station)
        for platform in station.platforms:
            for direction in model.DIRECTIONS:
                cell = tables[platform][direction]
                if not cell["possible"]:
                    continue
                assert_minimal_antichain(tables, platform, direction, doc)


def test_direction_can_be_inherently_impossible():
    # One-way passage from entrance to platform only: platform can never
    # reach the entrance, no elevator can fix that.
    doc = station_doc(
        entrances=("A",),
        passages=[{"from": "entrance:A", "to": "platform:P1", "elevator": "E1"}],
    )
    station = model.parse_station(doc)
    tables = model.build_station_tables(station)
    assert tables["P1"]["entrance_to_platform"]["possible"] is True
    assert tables["P1"]["platform_to_entrance"]["possible"] is False

    report = model.evaluate_station(meta_for(station), tables, set())
    platform = report["platforms"][0]
    assert platform["status"] == "unreachable"
    assert platform["platform_to_entrance"]["possible"] is False


def test_no_elevator_direction_reports_empty_set():
    doc = station_doc(
        entrances=("A",),
        passages=[
            {"from": "entrance:A", "to": "platform:P1"},
            {"from": "platform:P1", "to": "entrance:A"},
        ]
    )
    station = model.parse_station(doc)
    tables = model.build_station_tables(station)
    assert tables["P1"]["entrance_to_platform"]["sets"] == [[]]

    report = model.evaluate_station(meta_for(station), tables, {"X"})
    platform = report["platforms"][0]
    assert platform["status"] == "no_elevator"
    assert platform["entrance_to_platform"]["no_elevator_needed"] is True
    assert platform["entrance_to_platform"]["minimal_elevator_sets"] == [[]]


def test_empty_entrance_set_makes_platform_unreachable():
    doc = station_doc(
        entrances=("A",), passages=[{"from": "entrance:A", "to": "platform:P1"}]
    )
    station = model.parse_station(doc)
    tables = model.build_station_tables(station)
    report = model.evaluate_station(meta_for(station), tables, set())
    assert report["platforms"][0]["status"] == "unreachable"


# ---------------------------------------------------------------------------
# Read-time evaluation is table-only
# ---------------------------------------------------------------------------

def test_evaluation_filters_table_and_keeps_minimality():
    # Two alternative routes: E1 alone? No — build a case with sets
    # {E1,E2} and {E2,E3}.
    doc = station_doc(
        entrances=("A", "B", "C"),
        passages=[
            {"from": "entrance:A", "to": "platform:P1", "elevator": "E1"},
            {"from": "platform:P1", "to": "entrance:A", "elevator": "E1"},
            {"from": "entrance:B", "to": "platform:P1", "elevator": "E2"},
            {"from": "platform:P1", "to": "entrance:B", "elevator": "E2"},
            {"from": "entrance:C", "to": "platform:P1"},
            {"from": "platform:P1", "to": "entrance:C"},
        ],
    )
    station = model.parse_station(doc)
    tables = model.build_station_tables(station)
    assert tables["P1"]["entrance_to_platform"]["sets"] == [["E1", "E2"]]

    # E1 out: direction blocked, recovery must be exactly {E1}.
    report = model.evaluate_station(meta_for(station), tables, {"E1"})
    cell = report["platforms"][0]["entrance_to_platform"]
    assert cell["accessible"] is False
    assert cell["minimal_recovery_sets"] == [["E1"]]

    # E2 out too: the only recovery combination is restoring either one?
    # With both out, restoring either E1 or E2 alone is not enough (both are
    # needed), so the minimal recovery is {E1, E2}.
    report = model.evaluate_station(meta_for(station), tables, {"E1", "E2"})
    cell = report["platforms"][0]["entrance_to_platform"]
    assert cell["minimal_recovery_sets"] == [["E1", "E2"]]


def test_recovery_lists_every_minimal_combination():
    # Three entrances each needing its own elevator; every minimal working
    # set is the full trio, but if two are out the recovery is the pair.
    passages = []
    for name, elevator in (("A", "E1"), ("B", "E2"), ("C", "E3")):
        passages.append(
            {"from": f"entrance:{name}", "to": "platform:P1", "elevator": elevator}
        )
        passages.append(
            {"from": "platform:P1", "to": f"entrance:{name}", "elevator": elevator}
        )
    station = model.parse_station(station_doc(entrances=("A", "B", "C"), passages=passages))
    tables = model.build_station_tables(station)
    assert tables["P1"]["entrance_to_platform"]["sets"] == [["E1", "E2", "E3"]]

    report = model.evaluate_station(meta_for(station), tables, {"E1", "E2"})
    cell = report["platforms"][0]["entrance_to_platform"]
    assert cell["accessible"] is False
    assert cell["minimal_recovery_sets"] == [["E1", "E2"]]


def test_alternative_recovery_combinations():
    # Two disjoint routes, each needing two elevators:
    #   route 1 = {E1,E2}, route 2 = {E3,E4}
    doc = station_doc(
        entrances=("A", "B", "C", "D"),
        passages=[
            {"from": "entrance:A", "to": "platform:P1", "elevator": "E1"},
            {"from": "platform:P1", "to": "entrance:A", "elevator": "E1"},
            {"from": "entrance:B", "to": "platform:P1", "elevator": "E2"},
            {"from": "platform:P1", "to": "entrance:B", "elevator": "E2"},
            {"from": "entrance:C", "to": "platform:P1", "elevator": "E3"},
            {"from": "platform:P1", "to": "entrance:C", "elevator": "E3"},
            {"from": "entrance:D", "to": "platform:P1", "elevator": "E4"},
            {"from": "platform:P1", "to": "entrance:D", "elevator": "E4"},
        ],
    )
    station = model.parse_station(doc)
    tables = model.build_station_tables(station)
    working = tables["P1"]["entrance_to_platform"]["sets"]
    assert working == [["E1", "E2", "E3", "E4"]]

    report = model.evaluate_station(meta_for(station), tables, {"E1", "E3", "E4"})
    cell = report["platforms"][0]["entrance_to_platform"]
    assert cell["accessible"] is False
    # Every entrance needs its own elevator, so every outaged one has to
    # come back; the only minimal recovery is the whole outaged set.
    assert cell["minimal_recovery_sets"] == [["E1", "E3", "E4"]]


def test_alternative_routes_give_multiple_recovery_sets():
    # One entrance, two elevators per route:
    #   route 1: A --E1--> P2 --E2--> P1  (and the reverse for outbound)
    #   route 2: A --E3--> P3 --E4--> P1
    doc = station_doc(
        entrances=("A",),
        platforms=("P1", "P2", "P3"),
        passages=[
            {"from": "entrance:A", "to": "platform:P2", "elevator": "E1"},
            {"from": "platform:P2", "to": "entrance:A", "elevator": "E1"},
            {"from": "platform:P2", "to": "platform:P1", "elevator": "E2"},
            {"from": "platform:P1", "to": "platform:P2", "elevator": "E2"},
            {"from": "entrance:A", "to": "platform:P3", "elevator": "E3"},
            {"from": "platform:P3", "to": "entrance:A", "elevator": "E3"},
            {"from": "platform:P3", "to": "platform:P1", "elevator": "E4"},
            {"from": "platform:P1", "to": "platform:P3", "elevator": "E4"},
        ],
    )
    station = model.parse_station(doc)
    tables = model.build_station_tables(station)
    assert tables["P1"]["entrance_to_platform"]["sets"] == [
        ["E1", "E2"], ["E3", "E4"]
    ]
    assert tables["P1"]["platform_to_entrance"]["sets"] == [
        ["E1", "E2"], ["E3", "E4"]
    ]

    # E1 and E3 out: each route is missing its first elevator, so restoring
    # either E1 or E3 alone restores that route.  Both are minimal.
    report = model.evaluate_station(meta_for(station), tables, {"E1", "E3"})
    cell = report["platforms"][0]["entrance_to_platform"]
    assert cell["accessible"] is False
    assert cell["minimal_recovery_sets"] == [["E1"], ["E3"]]

    # E1 out only: route 2 is still fully usable.
    report = model.evaluate_station(meta_for(station), tables, {"E1"})
    cell = report["platforms"][0]["entrance_to_platform"]
    assert cell["accessible"] is True
    assert cell["minimal_elevator_sets"] == [["E3", "E4"]]


def test_evaluation_ignores_unknown_outages():
    doc = two_entrance_doc()
    station = model.parse_station(doc)
    tables = model.build_station_tables(station)
    report = model.evaluate_station(meta_for(station), tables, {"NOPE-1"})
    assert report["platforms"][0]["status"] == "ok"
    assert report["outages_not_in_station"] == ["NOPE-1"]
    assert report["outages_applied"] == []


def test_trimmed_tables_cannot_be_extended_by_client():
    # The API never ships full topology; mutating the returned sets in the
    # payload has no effect on later evaluations.
    doc = two_entrance_doc()
    station = model.parse_station(doc)
    tables = model.build_station_tables(station)
    first = model.evaluate_station(meta_for(station), tables, set())
    first["platforms"][0]["entrance_to_platform"]["minimal_elevator_sets"].clear()
    second = model.evaluate_station(meta_for(station), tables, set())
    assert second["platforms"][0]["entrance_to_platform"][
        "minimal_elevator_sets"
    ] == [["E1", "E2"]]


def test_every_table_subset_matches_direct_graph_search():
    """Cross-check the table-filter shortcut against brute force graph search."""

    for doc in DEMO_STATIONS:
        station = model.parse_station(doc)
        tables = model.build_station_tables(station)
        elevators = sorted(station.elevators)
        for r in range(len(elevators) + 1):
            for combo in itertools.combinations(elevators, r):
                outages = set(combo)
                report = model.evaluate_station(
                    meta_for(station), tables, outages
                )
                for platform in report["platforms"]:
                    node = f"platform:{platform['platform']}"
                    for direction in model.DIRECTIONS:
                        cell = platform[direction]
                        if not cell["possible"]:
                            continue
                        directly_works = model.direction_works(
                            station, node, direction, frozenset(
                                set(elevators) - outages
                            )
                        )
                        assert cell["accessible"] == directly_works, (
                            station.station_id,
                            platform["platform"],
                            direction,
                            outages,
                        )


def _reference_minimal_working(station, platform, direction, outages):
    """Brute-force minimal working sets given an outage set."""

    elevators = sorted(station.elevators)
    available = [e for e in elevators if e not in outages]
    working = []
    for size in range(len(available) + 1):
        for combo in itertools.combinations(available, size):
            fs = frozenset(combo)
            if model.direction_works(
                station, f"platform:{platform}", direction, fs
            ):
                working.append(fs)
    return sorted(
        (sorted(w) for w in working if not any(o < w for o in working)),
        key=lambda s: (len(s), s),
    )


def _reference_minimal_recovery(station, platform, direction, outages):
    """Brute-force minimal restoration sets for a blocked direction."""

    ordered = sorted(outages)
    restorable = []
    for size in range(len(ordered) + 1):
        for combo in itertools.combinations(ordered, size):
            restored = set(combo)
            usable = frozenset(
                e for e in station.elevators if e not in (outages - restored)
            )
            if model.direction_works(
                station, f"platform:{platform}", direction, usable
            ):
                restorable.append(frozenset(restored))
    if not restorable:
        return None
    return sorted(
        (sorted(r) for r in restorable if not any(o < r for o in restorable)),
        key=lambda s: (len(s), s),
    )


def test_table_evaluation_matches_brute_force_for_every_outage_subset():
    docs = list(DEMO_STATIONS)
    docs.append(
        station_doc(
            entrances=("A",),
            platforms=("P1", "P2", "P3"),
            passages=[
                {"from": "entrance:A", "to": "platform:P2", "elevator": "E1"},
                {"from": "platform:P2", "to": "entrance:A", "elevator": "E1"},
                {"from": "platform:P2", "to": "platform:P1", "elevator": "E2"},
                {"from": "platform:P1", "to": "platform:P2", "elevator": "E2"},
                {"from": "entrance:A", "to": "platform:P3", "elevator": "E3"},
                {"from": "platform:P3", "to": "entrance:A", "elevator": "E3"},
                {"from": "platform:P3", "to": "platform:P1", "elevator": "E4"},
                {"from": "platform:P1", "to": "platform:P3", "elevator": "E4"},
            ],
        )
    )

    for doc in docs:
        station = model.parse_station(doc)
        tables = model.build_station_tables(station)
        elevators = sorted(station.elevators)
        for r in range(len(elevators) + 1):
            for combo in itertools.combinations(elevators, r):
                outages = set(combo)
                report = model.evaluate_station(meta_for(station), tables, outages)
                for platform in report["platforms"]:
                    pid = platform["platform"]
                    for direction in model.DIRECTIONS:
                        cell = platform[direction]
                        expected_working = _reference_minimal_working(
                            station, pid, direction, outages
                        )
                        if not cell["possible"]:
                            continue
                        if expected_working:
                            assert cell["accessible"] is True
                            assert cell["minimal_elevator_sets"] == expected_working
                            assert cell["minimal_recovery_sets"] is None
                        else:
                            assert cell["accessible"] is False
                            expected_recovery = _reference_minimal_recovery(
                                station, pid, direction, outages
                            )
                            assert (
                                cell["minimal_recovery_sets"] == expected_recovery
                            )
                            # Minimality: dropping any elevator breaks recovery.
                            for recovery in cell["minimal_recovery_sets"] or []:
                                for elevator in recovery:
                                    reduced = set(outages) - (
                                        set(recovery) - {elevator}
                                    )
                                    usable = frozenset(
                                        e for e in station.elevators
                                        if e not in reduced
                                    )
                                    assert not model.direction_works(
                                        station,
                                        f"platform:{pid}",
                                        direction,
                                        usable,
                                    )


def test_station_with_zero_elevators():
    doc = station_doc(
        entrances=("A",),
        passages=[
            {"from": "entrance:A", "to": "platform:P1"},
            {"from": "platform:P1", "to": "entrance:A"},
        ],
    )
    station = model.parse_station(doc)
    assert station.elevators == []
    tables = model.build_station_tables(station)
    report = model.evaluate_station(meta_for(station), tables, set())
    platform = report["platforms"][0]
    assert platform["status"] == "no_elevator"
    assert platform["entrance_to_platform"]["minimal_elevator_sets"] == [[]]
    assert platform["entrance_to_platform"]["minimal_recovery_sets"] is None


def test_ten_elevator_station_calibrates_quickly():
    import time

    passages = []
    for i in range(10):
        passages.append(
            {"from": "entrance:A", "to": "platform:P1", "elevator": f"E{i}"}
        )
        passages.append(
            {"from": "platform:P1", "to": "entrance:A", "elevator": f"E{i}"}
        )
    station = model.parse_station(
        station_doc(entrances=("A",), passages=passages)
    )
    started = time.monotonic()
    tables = model.build_station_tables(station)
    assert time.monotonic() - started < 5
    assert tables["P1"]["entrance_to_platform"]["sets"] == [
        [f"E{i}"] for i in range(10)
    ]
    # Any single elevator outage still leaves the other nine routes.
    report = model.evaluate_station(meta_for(station), tables, {"E0", "E1"})
    cell = report["platforms"][0]["entrance_to_platform"]
    assert cell["accessible"] is True
    assert len(cell["minimal_elevator_sets"]) == 8
