"""KnowledgeGraph: lightweight subject-predicate-object triples (M4 §4).

Deliberately not a graph database — small-world scale (design.md D3), just
`edges` in SQLite with `WHERE subject/predicate/object = ?`. Shared across
agents (unlike per-agent `MemoryStore`): edges only encode relationship
*structure* (friendship / conversation / participated_in), never message
content — see design.md D7 for the private-memory/shared-graph boundary.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any


class KnowledgeGraph:
    """SQLite-backed store for (subject, predicate, object) triples."""

    def __init__(self, conn: sqlite3.Connection, lock: Any | None = None) -> None:
        self._conn = conn
        self._lock = lock if lock is not None else threading.RLock()

    @staticmethod
    def _dicts(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def add(
        self,
        subject: str,
        predicate: str,
        object: str,
        source_event_seq: int | None = None,
        created_at: int = 0,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO edges "
                "(subject, predicate, object, source_event_seq, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (subject, predicate, object, source_event_seq, created_at),
            )
            self._conn.commit()

    def query(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        object: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM edges WHERE 1=1"
        params: list[Any] = []
        if subject is not None:
            sql += " AND subject = ?"
            params.append(subject)
        if predicate is not None:
            sql += " AND predicate = ?"
            params.append(predicate)
        if object is not None:
            sql += " AND object = ?"
            params.append(object)
        with self._lock:
            cur = self._conn.execute(sql, params)
            return self._dicts(cur)

    def query_by_event(self, event_seq: int) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM edges WHERE source_event_seq = ?", (event_seq,)
            )
            return self._dicts(cur)
