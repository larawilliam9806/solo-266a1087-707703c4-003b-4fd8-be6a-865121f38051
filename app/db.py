"""SQLite persistence layer.

One file holds four kinds of state:

* ``station_drafts`` — uploaded station drawings waiting to be published;
* ``map_versions`` / ``station_maps`` — immutable published drawings together
  with their calibrated combination tables;
* ``outages`` — the current, editable elevator outage list;
* ``publish_log`` — every publish attempt and why it failed.

The active published version is recorded in ``settings``.  Publishing writes
a brand-new version and switches the pointer in one transaction, so a failed
calibration or save can never leave a half-published map behind.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS station_drafts (
    station_id TEXT PRIMARY KEY,
    doc        TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS map_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS station_maps (
    version_id INTEGER NOT NULL REFERENCES map_versions(id) ON DELETE CASCADE,
    station_id TEXT NOT NULL,
    name       TEXT NOT NULL,
    doc        TEXT NOT NULL,
    meta       TEXT NOT NULL,
    tables     TEXT NOT NULL,
    PRIMARY KEY (version_id, station_id)
);

CREATE TABLE IF NOT EXISTS outages (
    elevator   TEXT PRIMARY KEY,
    station_id TEXT,
    note       TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publish_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    status     TEXT NOT NULL,
    message    TEXT NOT NULL DEFAULT '',
    version_id INTEGER
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: str) -> None:
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def active_version_id(conn: sqlite3.Connection) -> int | None:
    raw = get_setting(conn, "active_version_id")
    return int(raw) if raw is not None else None


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------

def save_draft(conn: sqlite3.Connection, station_id: str, doc: dict) -> None:
    conn.execute(
        "INSERT INTO station_drafts (station_id, doc, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(station_id) DO UPDATE SET "
        "doc = excluded.doc, updated_at = excluded.updated_at",
        (station_id, json.dumps(doc, ensure_ascii=False, sort_keys=True), utcnow()),
    )


def list_drafts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT station_id, doc, updated_at FROM station_drafts "
        "ORDER BY station_id"
    ).fetchall()
    return [
        {
            "station_id": r["station_id"],
            "doc": json.loads(r["doc"]),
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def delete_draft(conn: sqlite3.Connection, station_id: str) -> bool:
    cur = conn.execute(
        "DELETE FROM station_drafts WHERE station_id = ?", (station_id,)
    )
    return cur.rowcount > 0


def clear_drafts(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM station_drafts")


# ---------------------------------------------------------------------------
# Published maps
# ---------------------------------------------------------------------------

def list_versions(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT v.id, v.created_at, v.note, "
        "  (SELECT COUNT(*) FROM station_maps m WHERE m.version_id = v.id) AS stations "
        "FROM map_versions v ORDER BY v.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_station_maps(conn: sqlite3.Connection, version_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT station_id, name, meta, tables FROM station_maps "
        "WHERE version_id = ? ORDER BY station_id",
        (version_id,),
    ).fetchall()
    return [
        {
            "station_id": r["station_id"],
            "name": r["name"],
            "meta": json.loads(r["meta"]),
            "tables": json.loads(r["tables"]),
        }
        for r in rows
    ]


def get_station_map(
    conn: sqlite3.Connection, version_id: int, station_id: str
) -> dict | None:
    row = conn.execute(
        "SELECT station_id, name, meta, tables FROM station_maps "
        "WHERE version_id = ? AND station_id = ?",
        (version_id, station_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "station_id": row["station_id"],
        "name": row["name"],
        "meta": json.loads(row["meta"]),
        "tables": json.loads(row["tables"]),
    }


def version_docs(conn: sqlite3.Connection, version_id: int) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT station_id, doc FROM station_maps WHERE version_id = ?",
        (version_id,),
    ).fetchall()
    return {r["station_id"]: json.loads(r["doc"]) for r in rows}


def delete_version(conn: sqlite3.Connection, version_id: int) -> None:
    conn.execute("DELETE FROM map_versions WHERE id = ?", (version_id,))


# ---------------------------------------------------------------------------
# Outages
# ---------------------------------------------------------------------------

def list_outages(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT elevator, station_id, note, updated_at FROM outages "
        "ORDER BY elevator"
    ).fetchall()
    return [dict(r) for r in rows]


def outage_set(conn: sqlite3.Connection) -> set[str]:
    return {r["elevator"] for r in conn.execute("SELECT elevator FROM outages")}


def set_outage(
    conn: sqlite3.Connection,
    elevator: str,
    active: bool,
    station_id: str | None = None,
    note: str = "",
) -> None:
    if active:
        conn.execute(
            "INSERT INTO outages (elevator, station_id, note, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(elevator) DO UPDATE SET "
            "station_id = excluded.station_id, note = excluded.note, "
            "updated_at = excluded.updated_at",
            (elevator, station_id, note, utcnow()),
        )
    else:
        conn.execute("DELETE FROM outages WHERE elevator = ?", (elevator,))


def replace_outages(conn: sqlite3.Connection, elevators: list[str]) -> None:
    conn.execute("DELETE FROM outages")
    now = utcnow()
    for elevator in elevators:
        conn.execute(
            "INSERT INTO outages (elevator, station_id, note, updated_at) "
            "VALUES (?, NULL, '', ?)",
            (elevator, now),
        )


# ---------------------------------------------------------------------------
# Publish log
# ---------------------------------------------------------------------------

def log_publish(
    conn: sqlite3.Connection,
    status: str,
    message: str,
    version_id: int | None = None,
    commit: bool = True,
) -> None:
    conn.execute(
        "INSERT INTO publish_log (created_at, status, message, version_id) "
        "VALUES (?, ?, ?, ?)",
        (utcnow(), status, message, version_id),
    )
    if commit:
        conn.commit()


def list_publish_log(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT id, created_at, status, message, version_id FROM publish_log "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
