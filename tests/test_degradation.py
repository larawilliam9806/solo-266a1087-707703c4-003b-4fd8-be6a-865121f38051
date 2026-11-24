"""Tests for the publish-time degradation check between two table versions.

The check is table-only: it never walks a graph, it filters the calibrated
combination tables of the active version and the pending drawing against
hypothetical outage lists.
"""

from __future__ import annotations

import itertools

from app import model

from .conftest import station_doc


def _version(doc):
    station = model.parse_station(doc)
    return {
        station.station_id: {
            "meta": {
                "station_id": station.station_id,
                "name": station.name,
                "entrances": station.entrances,
                "platforms": station.platforms,
                "elevators": station.elevators,
            },
            "tables": model.build_station_tables(station),
        }
    }


def _parallel_routes_doc(name, route_elevators):
    """One entrance, one platform, one two-way route per given elevator."""

    passages = []
    for i, elevator in enumerate(route_elevators):
        passages.append(
            {"id": f"in-{i}", "from": "entrance:A",
             "to": "platform:P1", "elevator": elevator}
        )
        passages.append(
            {"id": f"out-{i}", "from": "platform:P1",
             "to": "entrance:A", "elevator": elevator}
        )
    return station_doc(name=name, entrances=("A",), passages=passages)


def test_no_degradation_when_new_drawing_is_strictly_easier():
    # Old: entrance B needs E2; new: B gets an elevator-free passage.
    old = _version(
        station_doc(
            passages=[
                {"from": "entrance:A", "to": "platform:P1", "elevator": "E1"},
                {"from": "platform:P1", "to": "entrance:A", "elevator": "E1"},
                {"from": "entrance:B", "to": "platform:P1", "elevator": "E2"},
                {"from": "platform:P1", "to": "entrance:B", "elevator": "E2"},
            ]
        )
    )
    new = _version(
        station_doc(
            passages=[
                {"from": "entrance:A", "to": "platform:P1", "elevator": "E1"},
                {"from": "platform:P1", "to": "entrance:A", "elevator": "E1"},
                {"from": "entrance:B", "to": "platform:P1"},
                {"from": "platform:P1", "to": "entrance:B"},
            ]
        )
    )
    report = model.check_version_degradation(old, new)
    assert report["findings"] == []


def test_degradation_when_a_redundant_route_is_removed():
    old = _version(_parallel_routes_doc("old", ["E1", "E2"]))
    new = _version(_parallel_routes_doc("new", ["E1"]))
    report = model.check_version_degradation(old, new)
    assert len(report["findings"]) == 2  # one per direction
    for finding in report["findings"]:
        assert finding["station_id"] == "T1"
        assert finding["platform"] == "P1"
        assert finding["outage_elevators"] == ["E1"]
        assert finding["old_working_set"] == ["E2"]


def test_minimum_trigger_is_minimum_cardinality():
    # Old routes E1, E2; new routes E1, E3. No single outage blocks the new
    # drawing (the other route survives), so the minimum trigger is {E1, E3}
    # even though a smaller-looking list exists in the abstract.
    old = _version(_parallel_routes_doc("old", ["E1", "E2"]))
    new = _version(_parallel_routes_doc("new", ["E1", "E3"]))
    report = model.check_version_degradation(old, new)
    assert report["findings"], "expected a degradation"
    for finding in report["findings"]:
        assert finding["outage_elevators"] == ["E1", "E3"]
        assert finding["old_working_set"] == ["E2"]


def test_old_only_elevator_never_required_in_trigger():
    # E2 exists only in the old drawing. A minimum trigger must never name it
    # because it cannot break any new working set.
    old = _version(_parallel_routes_doc("old", ["E1", "E2"]))
    new = _version(_parallel_routes_doc("new", ["E1"]))
    for finding in model.check_version_degradation(old, new)["findings"]:
        assert "E2" not in finding["outage_elevators"]


def test_inherently_impossible_direction_in_new_drawing_degrades_with_empty_list():
    old = _version(
        station_doc(
            entrances=("A",),
            passages=[
                {"from": "entrance:A", "to": "platform:P1", "elevator": "E1"},
                {"from": "platform:P1", "to": "entrance:A", "elevator": "E1"},
            ],
        )
    )
    # New drawing drops the outbound passage: platform can never exit.
    new = _version(
        station_doc(
            entrances=("A",),
            passages=[
                {"from": "entrance:A", "to": "platform:P1", "elevator": "E1"},
            ],
        )
    )
    findings = model.check_version_degradation(old, new)["findings"]
    outbound = [
        f for f in findings if f["direction"] == "platform_to_entrance"
    ]
    assert len(outbound) == 1
    assert outbound[0]["outage_elevators"] == []
    assert outbound[0]["old_working_set"] == ["E1"]
    # The still-working inbound direction is not reported.
    assert not [
        f for f in findings if f["direction"] == "entrance_to_platform"
    ]


def test_deleted_platform_counts_as_not_passable():
    old = _version(
        station_doc(
            entrances=("A",),
            passages=[
                {"from": "entrance:A", "to": "platform:P1", "elevator": "E1"},
                {"from": "platform:P1", "to": "entrance:A", "elevator": "E1"},
            ],
        )
    )
    new_doc = station_doc(entrances=("A",), platforms=("P2",))
    new_doc["passages"] = [
        {"from": "entrance:A", "to": "platform:P2"},
        {"from": "platform:P2", "to": "entrance:A"},
    ]
    new = _version(new_doc)
    findings = model.check_version_degradation(old, new)["findings"]
    assert {f["platform"] for f in findings} == {"P1"}
    assert all(f["outage_elevators"] == [] for f in findings)


def test_direction_impossible_in_old_drawing_is_skipped():
    old_doc = station_doc(
        passages=[
            {"from": "entrance:A", "to": "platform:P1", "elevator": "E1"},
        ]
    )
    new_doc = station_doc(
        passages=[
            {"from": "entrance:A", "to": "platform:P1"},
            {"from": "platform:P1", "to": "entrance:A"},
        ]
    )
    report = model.check_version_degradation(_version(old_doc), _version(new_doc))
    assert report["findings"] == []


def test_only_old_platforms_are_examined():
    # A brand-new platform in the new drawing cannot regress anything.
    old = _version(
        station_doc(
            passages=[
                {"from": "entrance:A", "to": "platform:P1"},
                {"from": "platform:P1", "to": "entrance:A"},
            ]
        )
    )
    new_doc = station_doc(platforms=("P1", "P2"))
    new_doc["passages"] = [
        {"from": "entrance:A", "to": "platform:P1"},
        {"from": "platform:P1", "to": "entrance:A"},
        {"from": "entrance:A", "to": "platform:P2", "elevator": "E9"},
    ]
    report = model.check_version_degradation(old, _version(new_doc))
    assert report["findings"] == []


def test_removed_station_counts_all_its_platforms_as_not_passable():
    old = _version(
        station_doc(
            entrances=("A",),
            passages=[
                {"from": "entrance:A", "to": "platform:P1", "elevator": "E1"},
                {"from": "platform:P1", "to": "entrance:A", "elevator": "E1"},
            ],
        )
    )
    findings = model.check_version_degradation(old, {})["findings"]
    assert {f["station_id"] for f in findings} == {"T1"}
    assert {f["platform"] for f in findings} == {"P1"}
    assert all(f["outage_elevators"] == [] for f in findings)
    # Both directions of the surviving old platform are reported.
    assert {f["direction"] for f in findings} == set(model.DIRECTIONS)


def test_identical_drawings_never_degrade():
    doc = station_doc(
        entrances=("A", "B"),
        passages=[
            {"from": "entrance:A", "to": "platform:P1", "elevator": "E1"},
            {"from": "platform:P1", "to": "entrance:A", "elevator": "E1"},
            {"from": "entrance:B", "to": "platform:P1", "elevator": "E2"},
            {"from": "platform:P1", "to": "entrance:B", "elevator": "E2"},
        ],
    )
    version = _version(doc)
    assert model.check_version_degradation(version, version)["findings"] == []


def test_first_version_has_nothing_to_compare():
    new = _version(_parallel_routes_doc("new", ["E1"]))
    assert model.check_version_degradation({}, new)["findings"] == []


def test_agrees_with_brute_force_on_every_hypothetical_outage_list():
    # For every subset of the union of both versions' elevators, the check's
    # "old works / new fails" verdict must match filtering both raw tables.
    old = _version(_parallel_routes_doc("old", ["E1", "E2"]))
    new = _version(_parallel_routes_doc("new", ["E1", "E3"]))
    old_cell = old["T1"]["tables"]["P1"]["entrance_to_platform"]
    new_cell = new["T1"]["tables"]["P1"]["entrance_to_platform"]
    universe = ["E1", "E2", "E3"]

    def works(cell, outages):
        return any(not (set(s) & outages) for s in cell["sets"])

    brute_force_triggers = [
        frozenset(combo)
        for r in range(len(universe) + 1)
        for combo in itertools.combinations(universe, r)
        if works(old_cell, set(combo)) and not works(new_cell, set(combo))
    ]
    expected_size = min((len(s) for s in brute_force_triggers), default=0)

    findings = model.check_version_degradation(old, new)["findings"]
    entry = next(f for f in findings if f["direction"] == "entrance_to_platform")
    assert len(entry["outage_elevators"]) == expected_size
    trigger = set(entry["outage_elevators"])
    assert trigger in brute_force_triggers
    # The reported old working set really survives the trigger.
    assert not (set(entry["old_working_set"]) & trigger)


def test_ten_elevator_station_respects_the_1024_list_cap():
    passages = []
    for i in range(10):
        passages.append(
            {"id": f"in-{i}", "from": "entrance:A",
             "to": "platform:P1", "elevator": f"E{i}"}
        )
        passages.append(
            {"id": f"out-{i}", "from": "platform:P1",
             "to": "entrance:A", "elevator": f"E{i}"}
        )
    old = _version(station_doc(entrances=("A",), passages=passages))
    new = _version(
        station_doc(entrances=("A",), passages=[p for p in passages if p["elevator"] != "E9"])
    )
    report = model.check_version_degradation(old, new)
    assert report["findings"]
    # Two directions, each examining at most 2**9 lists.
    assert report["checked_lists"] <= 2 * model.MAX_HYPOTHETICAL_OUTAGE_LISTS
    for finding in report["findings"]:
        assert finding["outage_elevators"] == [f"E{i}" for i in range(9)]
        assert finding["old_working_set"] == ["E9"]


def test_constant_matches_power_set_limit():
    assert model.MAX_HYPOTHETICAL_OUTAGE_LISTS == 1024
