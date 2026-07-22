"""Event engine: append-only event log over SQLite."""

from __future__ import annotations

import sqlite3

from anima_world.types import Event


class EventLog:
    """Append-only event log backed by an SQLite connection."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def append(self, event: dict) -> Event:
        """Insert one event and return the constructed Event with seq filled."""
        ts = event["ts"]
        if ts < 0:
            raise ValueError("event ts MUST be non-negative")

        e = Event(
            seq=0,  # placeholder, overwritten after insert
            ts=ts,
            type=event["type"],
            who=event.get("who"),
            loc=event.get("loc"),
            payload=event.get("payload", {}),
        )
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO events (ts, type, who, loc, payload) VALUES (?, ?, ?, ?, ?)",
                e.to_row(),
            )
            e.seq = cur.lastrowid
        return e

    def replay(self, since_seq: int = 0) -> list[Event]:
        """Return all events with seq > since_seq, ordered by seq ASC."""
        cur = self.conn.execute(
            "SELECT seq, ts, type, who, loc, payload FROM events WHERE seq > ? ORDER BY seq ASC",
            (since_seq,),
        )
        return [Event.from_row(row) for row in cur.fetchall()]
