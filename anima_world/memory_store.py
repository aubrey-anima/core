"""MemoryStore: per-agent episodic memory persistence (M4).

Memory is derived truth, not source truth (see design.md D1) — the `memories`
table lives alongside `events` in `world.db` but is purely a
projection of "sufficiently important" events, decided by a trigger callable
supplied by the caller (kept decoupled from `memory_triggers.TriggerEngine`
so the two modules can be built/tested independently, per design.md D7:
MemoryStore stays a neutral persistence layer with no cross-agent ACL —
access-scoping is a call-site convention, not a store-level check).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from anima_world.memory_retrieval import (
    HALF_LIFE_DAYS_DEFAULT, decayed_strength, score,
)

_DEFAULT_CAPACITY = 50


@dataclass(frozen=True)
class MemoryDescriptor:
    """What a trigger decides should become a memory row."""

    agent_id: str
    tick: int
    kind: str
    summary: str
    importance: float = 0.5
    anchor: bool = False
    event_seq: int | None = None


class MemoryStore:
    """SQLite-backed store for per-agent memories, with LRU + anchor eviction.

    All access is serialized through `lock` (defaults to its own RLock; the
    web server passes the scheduler's shared lock since the connection is
    shared across threads, same pattern as `ChatStore`).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        capacity: int | None = None,
        lock: Any | None = None,
        config_store: Any | None = None,
    ) -> None:
        self._conn = conn
        self._explicit_capacity = capacity
        self._config_store = config_store
        self._lock = lock if lock is not None else threading.RLock()

    @property
    def _capacity(self) -> int:
        """Explicit constructor arg wins (existing test/no-DB usage); else
        live from `config_store` (`memory.capacity`, M5 §8); else default."""
        if self._explicit_capacity is not None:
            return self._explicit_capacity
        if self._config_store is not None:
            return self._config_store.get("memory.capacity", default=_DEFAULT_CAPACITY)
        return _DEFAULT_CAPACITY

    @staticmethod
    def _dicts(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def add(
        self,
        agent_id: str,
        tick: int,
        kind: str,
        summary: str,
        importance: float = 0.5,
        anchor: bool = False,
        event_seq: int | None = None,
        created_at: int | None = None,
        source_ids: list[int] | None = None,
    ) -> int:
        if created_at is None:
            created_at = tick
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memories "
                "(agent_id, tick, kind, summary, importance, anchor, event_seq, created_at, source_ids) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    agent_id, tick, kind, summary, importance, int(anchor),
                    event_seq, created_at,
                    json.dumps(source_ids) if source_ids else None,
                ),
            )
            memory_id = int(cur.lastrowid)
            self._conn.commit()
            self._evict_if_over_capacity(agent_id)
            return memory_id

    def _evict_if_over_capacity(self, agent_id: str) -> None:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE agent_id = ?",
            (agent_id,),
        )
        (total,) = cur.fetchone()
        overflow = total - self._capacity
        if overflow <= 0:
            return
        # memory-2.0: evict the WEAKEST, not the oldest — a frequently
        # retrieved old memory outlives an ignored new one. Anchors never go.
        self._conn.execute(
            "DELETE FROM memories WHERE id IN ("
            "  SELECT id FROM memories WHERE agent_id = ? AND anchor = 0 "
            "  ORDER BY strength ASC, id ASC LIMIT ?"
            ")",
            (agent_id, overflow),
        )
        self._conn.commit()

    def retrieve(
        self,
        agent_id: str,
        *,
        now_tick: int,
        query: str | None = None,
        k: int = 5,
        ticks_per_day: int = 288,
        reinforce: bool = True,
    ) -> list[dict[str, Any]]:
        """memory-2.0 三因子检索(时近×重要×相关),命中即加固。

        检索就是复习:返回的记忆 strength +0.3(上限 3.0),遗忘曲线由此
        对"被想起的事"变平。无 query 时退化为 recency+importance。

        **注意这个"读"接口会写库** —— 加固是设计的一部分,不是副作用。
        只想看看而不想影响角色记忆(调试、后台分析、给运维台做只读视图)
        就传 `reinforce=False`,那样它是纯读。
        """
        half_life = HALF_LIFE_DAYS_DEFAULT
        if self._config_store is not None:
            half_life = self._config_store.get("memory.half_life_days", default=half_life)
        rows = self.query(agent_id=agent_id)
        rows.sort(
            key=lambda m: -score(
                m, now_tick=now_tick, query=query,
                ticks_per_day=ticks_per_day, half_life_days=float(half_life),
            )
        )
        top = rows[: max(0, int(k))]
        if top and reinforce:
            with self._lock:
                self._conn.executemany(
                    "UPDATE memories SET strength = MIN(strength + 0.3, 3.0),"
                    " last_access = ?, access_count = access_count + 1 WHERE id = ?",
                    [(now_tick, m["id"]) for m in top],
                )
                self._conn.commit()
        return top

    def decay_pass(self, agent_id: str, now_tick: int, ticks_per_day: int = 288) -> None:
        """每世界日一次的遗忘曲线结算(Ebbinghaus):强度越高忘得越慢,
        闲置越久掉得越多。anchor 不衰减。

        算术在 Python 里做,不用 SQL 的 `pow()` —— 那个函数要 SQLite 编译时
        打开 SQLITE_ENABLE_MATH_FUNCTIONS 才有(3.35+ 也只是"可能有")。
        缺了它这条 UPDATE 会抛 OperationalError,而调用方(日切)把异常吞成
        一条 warning,于是**遗忘会静默地永不发生**。一天一次、每人几十行,
        取回来算完写回去的代价可以忽略。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, strength, COALESCE(last_access, tick) FROM memories"
                " WHERE agent_id = ? AND anchor = 0",
                (agent_id,),
            ).fetchall()
            updates = [
                (decayed_strength(strength, last_touch, now_tick, ticks_per_day), memory_id)
                for memory_id, strength, last_touch in rows
            ]
            if updates:
                self._conn.executemany(
                    "UPDATE memories SET strength = ? WHERE id = ?", updates
                )
                self._conn.commit()

    def query(
        self,
        agent_id: str,
        kind: str | None = None,
        min_importance: float | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM memories WHERE agent_id = ?"
        params: list[Any] = [agent_id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        if min_importance is not None:
            sql += " AND importance >= ?"
            params.append(min_importance)
        # 次序键补到 id 为止:tick 相同的记忆(创世注入的 memory_seed 全是 tick=0)
        # 光靠 tick DESC 分不出先后,余下的次序就交给 SQLite 的物理布局了。
        # `retrieve` 的打分排序是稳定排序,所以这里的次序就是同分时的最终次序 ——
        # 不确定的次序意味着同一个世界在两台机器上召回不同的记忆,而且不报错。
        sql += " ORDER BY tick DESC, id DESC"
        with self._lock:
            cur = self._conn.execute(sql, params)
            return self._dicts(cur)

    def set_anchor(self, memory_id: int, anchor: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET anchor = ? WHERE id = ?",
                (int(anchor), memory_id),
            )
            self._conn.commit()

    def anchors(self, agent_id: str) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM memories WHERE agent_id = ? AND anchor = 1 ORDER BY tick DESC",
                (agent_id,),
            )
            return self._dicts(cur)

    def rebuild(
        self,
        events: Iterable[Any],
        trigger: Callable[[Any], MemoryDescriptor | None],
    ) -> int:
        """Replay events through `trigger` only if `memories` is empty (idempotent).

        Returns the total memory count afterward either way, so callers can
        tell "already had data" from "just rebuilt" only via the row count
        they observe before calling, matching the spec's idempotency scenario.
        """
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM memories")
            (total,) = cur.fetchone()
            if total > 0:
                return int(total)
        for event in events:
            descriptor = trigger(event)
            if descriptor is not None:
                self.add(
                    agent_id=descriptor.agent_id,
                    tick=descriptor.tick,
                    kind=descriptor.kind,
                    summary=descriptor.summary,
                    importance=descriptor.importance,
                    anchor=descriptor.anchor,
                    event_seq=descriptor.event_seq,
                )
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM memories")
            (total,) = cur.fetchone()
            return int(total)
