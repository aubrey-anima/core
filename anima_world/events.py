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

    # ── 唯一的门 ────────────────────────────────────────────────────────────
    #
    # 下面三个方法存在的理由:此前有 **10 处**直接写 SQL 读 `events` 表(state、
    # contract、history 的过滤分页、doctor、events export、report)。日志换个后端
    # (比如搬进 Redis)之后,那 10 处会读到一张空表**而且不报错** —— 返回 0 条,
    # 一切照跑。那正是这个仓库最怕的那类坏。
    #
    # 所以先让这张表只剩一扇门,再谈换后端。顺序反过来做,就是给自己埋一个静默的错。

    def count(self, *, who: str | None = None, kind: str | None = None) -> int:
        """有多少条(可按 who / type 过滤)。"""
        clause, params = _filter(who, kind)
        return int(
            self.conn.execute(f"SELECT COUNT(*) FROM events{clause}", params).fetchone()[0]
        )

    def max_seq(self) -> int:
        """最新那条的 seq;空日志是 0。"""
        row = self.conn.execute("SELECT MAX(seq) FROM events").fetchone()
        return int(row[0] or 0) if row else 0

    def page(
        self, *, since_seq: int = 0, limit: int = 100,
        who: str | None = None, kind: str | None = None,
    ) -> list[Event]:
        """一页事件。**多取一条**由调用方决定怎么用(判断"还有没有下一页")。"""
        clause, params = _filter(who, kind, since_seq=since_seq)
        cur = self.conn.execute(
            "SELECT seq, ts, type, who, loc, payload FROM events"
            f"{clause} ORDER BY seq ASC LIMIT ?",
            (*params, int(limit)),
        )
        return [Event.from_row(row) for row in cur.fetchall()]


def _filter(
    who: str | None, kind: str | None, *, since_seq: int | None = None
) -> tuple[str, list]:
    parts: list[str] = []
    params: list = []
    if since_seq is not None:
        parts.append("seq > ?")
        params.append(int(since_seq))
    if who:
        parts.append("who = ?")
        params.append(who)
    if kind:
        parts.append("type = ?")
        params.append(kind)
    return ((" WHERE " + " AND ".join(parts)) if parts else "", params)
