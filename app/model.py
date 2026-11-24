"""Station graph model, topology calibration and outage evaluation.

The application is deliberately split in two halves:

1. **Topology calibration** (publish time) turns one station drawing into a
   *combination table*: for every platform and every direction, the complete
   antichain of inclusion-minimal elevator sets that make that direction
   work when every elevator on the drawing is available, or a marker saying
   the direction is impossible even with every elevator running.

2. **Evaluation** (read time) combines that published table with the current
   outage list.  It never walks the graph; it only filters the table.  This
   is what makes ``判定只读已生效的组合表和当前名单`` true.

Because the table is an antichain, filtering out sets that contain an
out-of-service elevator preserves minimality and yields *all* minimal
elevator sets that work right now.  Recovery sets are likewise derived from
the table: for a blocked direction, ``M ∩ outages`` for every working set
``M``; the inclusion-minimal ones are exactly the minimal sets of elevators
whose restoration makes the direction work again.

Direction semantics (both are universal over entrances):

* ``entrance_to_platform``: *every* entrance can reach the platform.
* ``platform_to_entrance``: the platform can reach *every* entrance.

Each may be empty (``[]``) — no elevator needed — or impossible (``None``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import combinations


class SchemaError(ValueError):
    """The uploaded station document is structurally invalid."""


@dataclass(frozen=True)
class Passage:
    passage_id: str
    from_node: str
    to_node: str
    elevator: str | None = None


@dataclass
class Station:
    station_id: str
    name: str
    entrances: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    elevators: list[str] = field(default_factory=list)
    passages: list[Passage] = field(default_factory=list)

    @property
    def entrance_nodes(self) -> list[str]:
        return [f"entrance:{e}" for e in self.entrances]

    @property
    def platform_nodes(self) -> list[str]:
        return [f"platform:{p}" for p in self.platforms]


# ---------------------------------------------------------------------------
# Uploaded JSON -> Station
# ---------------------------------------------------------------------------

def parse_station(doc: object) -> Station:
    """Validate an uploaded station document and return a :class:`Station`.

    Shape::

        {
          "station_id": "S1",
          "name": "人民广场",
          "entrances": ["A", "B"],
          "platforms": ["P1", "P2"],
          "passages": [
            {"id": "p1", "from": "entrance:A", "to": "platform:P1",
             "elevator": "S1-E1"},
            {"id": "p2", "from": "platform:P1", "to": "entrance:B"}
          ]
        }

    ``elevator`` is optional (``null`` means an elevator-free passage).
    Elevator identifiers are global; using station-prefixed identifiers such
    as ``S1-E1`` is recommended.  At most 10 distinct elevators per station.
    """

    if not isinstance(doc, dict):
        raise SchemaError("document must be a JSON object")

    station_id = doc.get("station_id")
    if not isinstance(station_id, str) or not station_id.strip():
        raise SchemaError("station_id must be a non-empty string")
    if any(c.isspace() for c in station_id) or "/" in station_id:
        raise SchemaError("station_id must not contain whitespace or '/'")

    name = doc.get("name", station_id)
    if not isinstance(name, str) or not name.strip():
        raise SchemaError("name must be a non-empty string")

    def _string_list(key: str) -> list[str]:
        raw = doc.get(key, [])
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise SchemaError(f"{key} must be a list")
        out: list[str] = []
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise SchemaError(f"{key} entries must be non-empty strings")
            if any(c.isspace() for c in item):
                raise SchemaError(f"{key} entries must not contain whitespace")
            if item in out:
                raise SchemaError(f"duplicate {key} entry: {item}")
            out.append(item)
        return out

    entrances = _string_list("entrances")
    platforms = _string_list("platforms")
    if not entrances:
        raise SchemaError("station must have at least one entrance")
    if not platforms:
        raise SchemaError("station must have at least one platform")

    raw_passages = doc.get("passages", [])
    if not isinstance(raw_passages, list):
        raise SchemaError("passages must be a list")

    known_nodes = {f"entrance:{e}" for e in entrances} | {
        f"platform:{p}" for p in platforms
    }
    passages: list[Passage] = []
    seen_ids: set[str] = set()
    for idx, raw in enumerate(raw_passages):
        if not isinstance(raw, dict):
            raise SchemaError(f"passages[{idx}] must be an object")
        pid = raw.get("id", f"p{idx + 1}")
        if not isinstance(pid, str) or not pid.strip():
            raise SchemaError(f"passages[{idx}].id must be a non-empty string")
        if pid in seen_ids:
            raise SchemaError(f"duplicate passage id: {pid}")
        seen_ids.add(pid)

        src = raw.get("from")
        dst = raw.get("to")
        if src not in known_nodes:
            raise SchemaError(f"passages[{idx}].from references unknown node: {src!r}")
        if dst not in known_nodes:
            raise SchemaError(f"passages[{idx}].to references unknown node: {dst!r}")

        elevator = raw.get("elevator")
        if elevator is not None:
            if not isinstance(elevator, str) or not elevator.strip():
                raise SchemaError(
                    f"passages[{idx}].elevator must be a non-empty string or null"
                )
            if any(c.isspace() for c in elevator):
                raise SchemaError(
                    f"passages[{idx}].elevator must not contain whitespace"
                )
        passages.append(Passage(pid, src, dst, elevator))

    elevators = sorted({p.elevator for p in passages if p.elevator is not None})
    if len(elevators) > 10:
        raise SchemaError(
            f"station may use at most 10 elevators, got {len(elevators)}"
        )

    return Station(
        station_id=station_id,
        name=name,
        entrances=entrances,
        platforms=platforms,
        elevators=elevators,
        passages=passages,
    )


def station_to_doc(station: Station) -> dict:
    """Canonical document used for staging/persistence."""

    return {
        "station_id": station.station_id,
        "name": station.name,
        "entrances": list(station.entrances),
        "platforms": list(station.platforms),
        "passages": [
            {
                "id": p.passage_id,
                "from": p.from_node,
                "to": p.to_node,
                "elevator": p.elevator,
            }
            for p in station.passages
        ],
    }


# ---------------------------------------------------------------------------
# Graph traversal (calibration only)
# ---------------------------------------------------------------------------

def _adjacency(station: Station, usable: frozenset[str]) -> dict[str, list[str]]:
    adj: dict[str, list[str]] = {
        n: [] for n in station.entrance_nodes + station.platform_nodes
    }
    for p in station.passages:
        if p.elevator is not None and p.elevator not in usable:
            continue
        adj[p.from_node].append(p.to_node)
    return adj


def _reachable_from(
    station: Station, start: str, usable: frozenset[str]
) -> set[str]:
    adj = _adjacency(station, usable)
    seen = {start}
    stack = [start]
    while stack:
        node = stack.pop()
        for nxt in adj[node]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def direction_works(
    station: Station, platform_node: str, direction: str, usable: frozenset[str]
) -> bool:
    if not station.entrances or not station.platforms:
        return False
    if direction == "entrance_to_platform":
        # Every entrance must reach the platform.
        return all(
            platform_node in _reachable_from(station, e, usable)
            for e in station.entrance_nodes
        )
    if direction == "platform_to_entrance":
        # The platform must reach every entrance.
        reached = _reachable_from(station, platform_node, usable)
        return all(e in reached for e in station.entrance_nodes)
    raise ValueError(f"unknown direction: {direction}")


DIRECTIONS = ("entrance_to_platform", "platform_to_entrance")


def _minimal_sets(
    station: Station, platform_node: str, direction: str, elevators: list[str]
) -> list[list[str]] | None:
    """Antichain of inclusion-minimal elevator sets making ``direction`` work.

    ``None`` means the direction cannot work even with every elevator.
    """

    ordered = sorted(elevators)
    found: list[frozenset[str]] = []
    for size in range(len(ordered) + 1):
        for combo in combinations(ordered, size):
            candidate = frozenset(combo)
            if any(m <= candidate for m in found):
                continue
            if direction_works(station, platform_node, direction, candidate):
                found.append(candidate)
    if not found:
        return None
    return sorted((sorted(s) for s in found), key=lambda s: (len(s), s))


def build_station_tables(station: Station) -> dict[str, dict[str, dict]]:
    """Calibrate a station drawing into combination tables.

    Result::

        {
          "P1": {
            "entrance_to_platform": {"possible": true, "sets": [[], ["E1"]]},
            "platform_to_entrance": {"possible": true, "sets": [["E1"]]}
          },
          ...
        }

    ``sets`` is ``None`` when the direction is impossible regardless of
    elevators; otherwise it is a non-empty antichain (``[[]]`` means no
    elevator is needed).
    """

    tables: dict[str, dict[str, dict]] = {}
    for platform in station.platforms:
        node = f"platform:{platform}"
        per_direction: dict[str, dict] = {}
        for direction in DIRECTIONS:
            sets = _minimal_sets(
                station, node, direction, list(station.elevators)
            )
            per_direction[direction] = {
                "possible": sets is not None,
                "sets": sets,
            }
        tables[platform] = per_direction
    return tables


# ---------------------------------------------------------------------------
# Read-time evaluation from published tables + current outage list
# ---------------------------------------------------------------------------

def _minimal_recovery_sets(
    working_sets: list[list[str]], outages: set[str]
) -> list[list[str]]:
    """Inclusion-minimal subsets of ``outages`` that restore the direction.

    ``working_sets`` is the calibrated antichain.  Restoring ``R`` makes a
    calibrated set usable iff ``set(M) ∩ outages ⊆ R``; therefore every
    working ``M`` contributes the recovery need ``M ∩ outages`` and the
    minimal such needs are the answer.
    """

    needs: list[set[str]] = []
    for w in working_sets:
        need = set(w) & outages
        if need and not any(n <= need for n in needs):
            needs.append(need)
    needs = [n for n in needs if not any(m < n for m in needs)]
    result = sorted((sorted(n) for n in needs), key=lambda s: (len(s), s))
    return result


def evaluate_platform(
    platform: str,
    table: dict,
    outages: set[str],
    station_elevators: set[str],
) -> dict:
    """Evaluate one platform using only its table and the current outages."""

    active_outages = set(outages) & station_elevators
    entry: dict = {"platform": platform}

    impossible_any = False
    possible_any = False
    accessible_all = True
    no_elevator_all = True

    for direction in DIRECTIONS:
        cell = table.get(direction) or {"possible": False, "sets": None}
        possible = bool(cell.get("possible"))
        working_sets = cell.get("sets")

        if not possible or working_sets is None:
            impossible_any = True
            detail = {
                "possible": False,
                "accessible": False,
                "no_elevator_needed": False,
                "minimal_elevator_sets": None,
                "minimal_recovery_sets": None,
            }
            accessible_all = False
            no_elevator_all = False
        else:
            possible_any = True
            usable_sets = [
                w for w in working_sets if not (set(w) & active_outages)
            ]
            accessible = bool(usable_sets)
            no_elevator = [] in usable_sets
            detail = {
                "possible": True,
                "accessible": accessible,
                "no_elevator_needed": no_elevator,
                "minimal_elevator_sets": usable_sets if accessible else [],
                "minimal_recovery_sets": (
                    None
                    if accessible
                    else _minimal_recovery_sets(working_sets, active_outages)
                ),
            }
            if not accessible:
                accessible_all = False
                no_elevator_all = False
            if not no_elevator:
                no_elevator_all = False
        entry[direction] = detail

    if impossible_any:
        # A direction that the drawing cannot provide at all means the
        # platform can never be both entered and exited, regardless of
        # elevators: report it as completely impassable.
        status = "unreachable"
    elif not possible_any:
        status = "unreachable"
    elif not accessible_all:
        status = "blocked"
    elif no_elevator_all:
        status = "no_elevator"
    else:
        status = "ok"

    entry["status"] = status
    entry["enterable"] = entry["entrance_to_platform"]["accessible"]
    entry["exitable"] = entry["platform_to_entrance"]["accessible"]
    return entry


def evaluate_station(
    meta: dict,
    tables: dict[str, dict],
    outages: set[str],
    version_id: int | None = None,
) -> dict:
    """Build the per-station JSON served to the web UI and the navigation app.

    ``meta`` holds ``station_id``, ``name``, ``entrances``, ``platforms`` and
    ``elevators`` as stored with the published map version.
    """

    elevators = set(meta.get("elevators") or [])
    active_outages = sorted(outages & elevators)
    ignored = sorted(outages - elevators)

    platforms = []
    for platform in meta.get("platforms") or []:
        table = tables.get(platform) or {}
        platforms.append(
            evaluate_platform(platform, table, outages, elevators)
        )

    return {
        "station_id": meta["station_id"],
        "name": meta["name"],
        "version_id": version_id,
        "entrances": list(meta.get("entrances") or []),
        "platforms": platforms,
        "elevators": sorted(elevators),
        "outages_applied": active_outages,
        "outages_not_in_station": ignored,
    }


def check_outaged_elevators_present(
    calibrated: dict[str, "Station"], outages: list[dict]
) -> list[str]:
    """Check every still-outaged elevator survives in the new drawing.

    If the outage names a station, that station's drawing must contain the
    elevator; a still-outaged elevator without a station hint must appear
    somewhere in the new map.
    """

    problems: list[str] = []
    present_anywhere: set[str] = set()
    by_station: dict[str, set[str]] = {}
    for sid, station in calibrated.items():
        elevators = set(station.elevators)
        by_station[sid] = elevators
        present_anywhere |= elevators

    for outage in outages:
        elevator = outage["elevator"]
        station_id = outage.get("station_id")
        if station_id:
            station_elevators = by_station.get(station_id)
            if station_elevators is None:
                problems.append(
                    f"电梯 {elevator} 仍在停运，但新图中没有站点 {station_id}"
                )
            elif elevator not in station_elevators:
                problems.append(
                    f"电梯 {elevator} 仍在停运，但新图中站点 {station_id} 缺少该电梯"
                )
        elif elevator not in present_anywhere:
            problems.append(f"电梯 {elevator} 仍在停运，但新图中缺少该电梯")
    return problems


# ---------------------------------------------------------------------------
# Small helpers shared with the web layer
# ---------------------------------------------------------------------------

def dumps(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
