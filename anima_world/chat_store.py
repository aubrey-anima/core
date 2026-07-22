"""ChatStore: durable persistence for conversations and messages (M3.5).

Pure storage — no event emission, no LLM. The `conversations`/`messages`
tables live in `world.db` (see `db.py`). Session closing and summary
generation are layered on top in the chat service / reaper; `ChatStore.close`
only performs the row mutation.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any


class ChatStore:
    """SQLite-backed store for chat conversations and their messages.

    All access is serialized through `lock`. In the web server this is the
    scheduler's RLock, so the single shared SQLite connection is only ever
    touched by one thread at a time (request handlers + the background reaper).
    """

    def __init__(self, conn: sqlite3.Connection, lock: Any | None = None) -> None:
        self._conn = conn
        self._lock = lock if lock is not None else threading.RLock()

    @staticmethod
    def _dicts(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        for row in rows:
            raw = row.get("participants")
            if isinstance(raw, str) and raw:
                try:
                    row["participants"] = json.loads(raw)
                except ValueError:
                    pass
        return rows

    def _one(self, cur: sqlite3.Cursor) -> dict[str, Any] | None:
        rows = self._dicts(cur)
        return rows[0] if rows else None

    # ── conversations ───────────────────────────────────────────────────────

    def active_conversation(
        self, agent_id: str, player_id: str | None = None
    ) -> dict[str, Any] | None:
        """The agent's open conversation — with THIS player when `player_id`
        is given (player-visitor: sessions are keyed per (agent, player);
        legacy NULL rows read as the implicit "user")."""
        with self._lock:
            if player_id is None:
                cur = self._conn.execute(
                    "SELECT * FROM conversations WHERE agent_id = ? AND status = 'open' "
                    "ORDER BY id DESC LIMIT 1",
                    (agent_id,),
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM conversations WHERE agent_id = ? AND status = 'open' "
                    "AND COALESCE(player_id, 'user') = ? ORDER BY id DESC LIMIT 1",
                    (agent_id, player_id),
                )
            return self._one(cur)

    def start_conversation(
        self,
        agent_id: str,
        ts: int,
        participants: list[dict[str, Any]] | None = None,
        location: str | None = None,
        player_id: str | None = None,
        player_name: str | None = None,
    ) -> int:
        """Open a conversation. `participants` is a JSON-able list of
        {"id", "kind"} dicts (kind: "user" | "agent"), defaulting to the
        calling player plus `agent_id` — multi-party ready but two-party by
        default. `location` is where it happens in-world; `player_id` keys
        the session per player (legacy callers leave it as "user").
        """
        pid = player_id or "user"
        if participants is None:
            user_entry: dict[str, Any] = {"id": pid, "kind": "user"}
            if player_name:
                user_entry["name"] = player_name
            participants = [user_entry, {"id": agent_id, "kind": "agent"}]
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO conversations "
                "(agent_id, status, started_at, last_activity_at, message_count, "
                "participants, location, player_id) "
                "VALUES (?, 'open', ?, ?, 0, ?, ?, ?)",
                (agent_id, ts, ts, json.dumps(participants, ensure_ascii=False), location, pid),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def active_or_start(
        self,
        agent_id: str,
        ts: int,
        participants: list[dict[str, Any]] | None = None,
        location: str | None = None,
        player_id: str | None = None,
        player_name: str | None = None,
    ) -> int:
        """Return the id of the agent's open conversation with this player,
        starting one if none."""
        with self._lock:
            active = self.active_conversation(agent_id, player_id=player_id)
            if active is not None:
                return int(active["id"])
            return self.start_conversation(
                agent_id, ts, participants=participants, location=location,
                player_id=player_id, player_name=player_name,
            )

    def get(self, conversation_id: int) -> dict[str, Any] | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            )
            return self._one(cur)

    def list_conversations(self, agent_id: str) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM conversations WHERE agent_id = ? ORDER BY id DESC",
                (agent_id,),
            )
            return self._dicts(cur)

    def close(self, conversation_id: int, summary: str, ts: int) -> None:
        """Mark a conversation closed with a summary. Storage only — no event."""
        with self._lock:
            self._conn.execute(
                "UPDATE conversations SET status = 'closed', closed_at = ?, summary = ? "
                "WHERE id = ?",
                (ts, summary, conversation_id),
            )
            self._conn.commit()

    def touch(self, conversation_id: int, ts: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE conversations SET last_activity_at = ? WHERE id = ?",
                (ts, conversation_id),
            )
            self._conn.commit()

    def idle_open_conversations(self, now: int, timeout: int) -> list[dict[str, Any]]:
        """Return open conversations whose last activity is at least `timeout` old."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM conversations WHERE status = 'open' AND last_activity_at <= ?",
                (now - timeout,),
            )
            return self._dicts(cur)

    # ── messages ────────────────────────────────────────────────────────────

    def add_message(self, conversation_id: int, role: str, content: str, ts: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (conversation_id, role, content, ts),
            )
            self._conn.execute(
                "UPDATE conversations SET message_count = message_count + 1, "
                "last_activity_at = ? WHERE id = ?",
                (ts, conversation_id),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def messages_for(self, conversation_id: int) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT role, content, created_at FROM messages "
                "WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            )
            return self._dicts(cur)

    def recent_messages(self, conversation_id: int, n: int) -> list[dict[str, Any]]:
        """Return the last `n` messages of a conversation in chronological order."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT role, content, created_at FROM messages "
                "WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                (conversation_id, n),
            )
            return list(reversed(self._dicts(cur)))

    def past_summaries(self, agent_id: str, k: int, player_id: str | None = None) -> list[str]:
        """The agent's last `k` closed-session summaries, most recent first —
        scoped to one player when `player_id` is given (cross-session memory
        is per relationship, not per agent)."""
        with self._lock:
            if player_id is None:
                cur = self._conn.execute(
                    "SELECT summary FROM conversations "
                    "WHERE agent_id = ? AND status = 'closed' AND summary IS NOT NULL "
                    "ORDER BY closed_at DESC, id DESC LIMIT ?",
                    (agent_id, k),
                )
            else:
                cur = self._conn.execute(
                    "SELECT summary FROM conversations "
                    "WHERE agent_id = ? AND status = 'closed' AND summary IS NOT NULL "
                    "AND COALESCE(player_id, 'user') = ? "
                    "ORDER BY closed_at DESC, id DESC LIMIT ?",
                    (agent_id, player_id, k),
                )
            return [row[0] for row in cur.fetchall()]
