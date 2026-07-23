"""Snapshot + World facade: persistent snapshotting and recovery."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from anima_world.db import open_db
from anima_world.events import EventLog
from anima_world.projection import project_events
from anima_world.types import (
    AgentState,
    Capability,
    Location,
    Projection,
    Relation,
)


def create_snapshot(proj: Projection, seq: int) -> dict:
    """Serialize a Projection into a JSON-safe dict with seq + ts."""
    return {
        "seq": seq,
        "ts": int(time.time() * 1000),
        "agents": {
            aid: {
                "spec": a.spec,
                "state": a.state,
                "location": a.location,
                "joined_at": a.joined_at,
                "updated_at": a.updated_at,
            }
            for aid, a in proj.agents.items()
        },
        "relations": {
            f"{a}|{b}": {
                "r_type": r.r_type,
                "r_type_back": r.r_type_back,
                "sentiment": r.sentiment,
                "trust": r.trust,
                "affection": r.affection,
                "respect": r.respect,
            }
            for (a, b), r in proj.relations.items()
        },
        "locations": {
            lid: {
                "id": l_.id,
                "name": l_.name,
                "description": l_.description,
            }
            for lid, l_ in proj.locations.items()
        },
        "narrative_log": list(proj.narrative_log),
        "balances": dict(proj.balances),
        "inventories": {holder: dict(items) for holder, items in proj.inventories.items()},
        "capabilities": {
            cid: {
                "id": c.id,
                "kind": c.kind,
                "description": c.description,
                "params_schema": c.params_schema,
            }
            for cid, c in proj.capabilities.items()
        },
    }


def load_snapshot(data: dict) -> Projection:
    """Deserialize a snapshot dict back into a Projection."""
    return Projection(
        agents={
            aid: AgentState(
                spec=a["spec"],
                state=a["state"],
                location=a["location"],
                joined_at=a["joined_at"],
                updated_at=a["updated_at"],
            )
            for aid, a in data.get("agents", {}).items()
        },
        relations={
            tuple(k.split("|", 1)): Relation(
                r_type=v["r_type"],
                r_type_back=v["r_type_back"],
                sentiment=v["sentiment"],
                trust=float(v.get("trust", 0.0)),
                affection=float(v.get("affection", 0.0)),
                respect=float(v.get("respect", 0.0)),
            )
            for k, v in data.get("relations", {}).items()
        },
        locations={
            lid: Location(
                id=l_["id"],
                name=l_["name"],
                description=l_["description"],
            )
            for lid, l_ in data.get("locations", {}).items()
        },
        narrative_log=list(data.get("narrative_log", [])),
        balances={k: float(v) for k, v in data.get("balances", {}).items()},
        inventories={
            holder: {i: int(q) for i, q in items.items()}
            for holder, items in data.get("inventories", {}).items()
        },
        capabilities={
            cid: Capability(
                id=c["id"],
                kind=c["kind"],
                description=c["description"],
                params_schema=c["params_schema"],
            )
            for cid, c in data.get("capabilities", {}).items()
        },
    )


def save_snapshot(conn: sqlite3.Connection, snap: dict) -> None:
    """Persist a snapshot dict into the snapshots table."""
    with conn:
        conn.execute(
            """
            INSERT INTO snapshots (seq, ts, snapshot, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(seq) DO UPDATE SET
                ts = excluded.ts,
                snapshot = excluded.snapshot,
                created_at = excluded.created_at
            """,
            (snap["seq"], snap["ts"], json.dumps(snap, ensure_ascii=False), snap["ts"]),
        )


def load_latest_snapshot(conn: sqlite3.Connection) -> dict | None:
    """Return the snapshot dict with the highest seq, or None if empty."""
    row = conn.execute(
        "SELECT snapshot FROM snapshots ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


class World:
    """Top-level facade binding EventLog + Projection + Snapshot."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.events = EventLog(conn)
        self.projection: Projection = Projection()

    @classmethod
    def open(cls, path: str | Path) -> "World":
        """Create a fresh World bound to an SQLite file (creates tables if needed)."""
        conn = open_db(path)
        return cls(conn)

    def refresh(self) -> None:
        """Re-fold all events into the in-memory projection."""
        self.projection = project_events(self.events.replay())

    def save_snapshot(self) -> dict:
        """Snapshot the current projection (uses the max event seq)."""
        snap = create_snapshot(self.projection, seq=self._max_seq())
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO snapshots (seq, ts, snapshot, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(seq) DO UPDATE SET
                    ts = excluded.ts,
                    snapshot = excluded.snapshot,
                    created_at = excluded.created_at
                """,
                (snap["seq"], snap["ts"], json.dumps(snap, ensure_ascii=False), snap["ts"]),
            )
        return snap

    def _max_seq(self) -> int:
        row = self.conn.execute("SELECT MAX(seq) FROM events").fetchone()
        return row[0] if row[0] is not None else 0

    def close(self) -> None:
        self.conn.close()


def recover(path: str | Path) -> World:
    """Recover a World from an SQLite file: load_latest_snapshot + replay."""
    conn = open_db(path)
    world = World(conn)

    snap = load_latest_snapshot(conn)
    if snap is not None:
        world.projection = load_snapshot(snap)
        since_seq = snap["seq"]
    else:
        since_seq = 0

    new_events = world.events.replay(since_seq=since_seq)
    if new_events:
        world.projection = project_events(new_events, base=world.projection)
    return world
