"""ChatStateStore:一轮聊天要读写的"此刻"状态(chat-agent,1.3.0)。

纯存储 —— 不发事件、不调 LLM,和 `ChatStore` 同一条纪律,共用世界那把唯一的锁。
六张表都是**当前值**,不是历史:

- `agent_stance`         她此刻对某人的关系性意图(#18)
- `agent_mutes`          软静音 / "等会儿再说"(#15)
- `agent_refused_topics` 她拒绝谈的话题(#15)
- `agent_followups`      到点回来找你的约(#15 的 delay_reply 兑现)
- `persona_overrides`    玩家教给她的对话规则,按 (角色, 玩家) 永久(#16)
- `messages` 的四个新列   一轮的 stance / intent / tool_call,给观测用

**为什么不发每轮事件。** issue #18/#16 里写的是"每轮发一个 agent_stance /
user_intent 事件"。CLAUDE.md 的不变量是「聊天子系统与事件核解耦:整场会话只在关闭
时发一个事件」—— 每轮一个事件等于把聊天转录搬进世界的历史。这里的取法是:观测量
落在这些表和消息行上(运维台照样能逐轮显示 tag),**后果**照旧走事件日志(她真的
走开 = travel/location_join 事件,广播 = 一条事件),而整场会话的 stance/intent
分布在关闭时随那一个 `conversation` 事件一起出去。事件日志仍然只记世界里发生的事。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Any

from anima_world import stance as stance_mod

logger = logging.getLogger(__name__)

# 玩家能教的规则种类。白名单是有意的:开放键位等于让分类器往库里写任意字段,
# 而提示词那一段会照着念 —— 一个错分类就能把角色的身份改掉。
OVERRIDE_KINDS: dict[str, str] = {
    "address_form": "怎么称呼玩家",
    "description_style": "动作与神态怎么写",
    "tone_preference": "口气偏好",
    "forbidden_topics": "不要提的东西",
    "nickname_for_player": "玩家的昵称",
}


class ChatStateStore:
    """SQLite 后端的聊天当前值存储。所有访问经 `lock` 串行化。"""

    def __init__(self, conn: sqlite3.Connection, lock: Any | None = None) -> None:
        self._conn = conn
        self._lock = lock if lock is not None else threading.RLock()

    @staticmethod
    def _now() -> int:
        return int(time.time())

    # ── stance(#18) ─────────────────────────────────────────────────────────

    def stance(self, agent_id: str, target_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT stance, declared, updated_tick, updated_at FROM agent_stance "
                "WHERE agent_id = ? AND target_id = ?",
                (agent_id, target_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "stance": row[0],
            "declared": bool(row[1]),
            "updated_tick": int(row[2] or 0),
            "updated_at": int(row[3] or 0),
        }

    def set_stance(
        self, agent_id: str, target_id: str, stance: str, *,
        declared: bool = True, tick: int = 0,
    ) -> None:
        if not stance_mod.is_stance(stance):
            raise ValueError(f"unknown stance {stance!r}")
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_stance (agent_id, target_id, stance, declared, updated_tick, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(agent_id, target_id) DO UPDATE SET "
                "stance=excluded.stance, declared=excluded.declared, "
                "updated_tick=excluded.updated_tick, updated_at=excluded.updated_at",
                (agent_id, target_id, stance, 1 if declared else 0, int(tick), self._now()),
            )
            self._conn.commit()

    def stances(self, agent_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT target_id, stance, declared, updated_tick, updated_at FROM agent_stance "
                "WHERE agent_id = ? ORDER BY target_id",
                (agent_id,),
            ).fetchall()
        return [
            {"target": r[0], "stance": r[1], "declared": bool(r[2]),
             "updated_tick": int(r[3] or 0), "updated_at": int(r[4] or 0)}
            for r in rows
        ]

    # ── 静音 / 等会儿再说(#15) ──────────────────────────────────────────────

    def set_quiet(
        self, agent_id: str, player_id: str, *, kind: str = "mute",
        minutes: float, reason: str | None = None, now: int | None = None,
    ) -> int:
        """静音到某个墙钟时刻。返回过期时间戳。

        墙钟而不是 tick:玩家那一侧的"五分钟"是真的五分钟,而世界时钟可能是
        演示速度(1 tick/秒)也可能是实时速度。用 tick 会让"五分钟别理我"在演示
        速度下变成不到一分钟。
        """
        if kind not in ("mute", "delay"):
            raise ValueError(f"unknown quiet kind {kind!r}")
        base = self._now() if now is None else int(now)
        expires = base + int(max(0.0, float(minutes)) * 60)
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_mutes (agent_id, player_id, kind, expires_at, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(agent_id, player_id, kind) DO UPDATE SET "
                "expires_at=excluded.expires_at, reason=excluded.reason, created_at=excluded.created_at",
                (agent_id, player_id, kind, expires, reason, base),
            )
            self._conn.commit()
        return expires

    def quiet_until(
        self, agent_id: str, player_id: str, *, now: int | None = None
    ) -> dict[str, Any] | None:
        """她这会儿不理这个玩家吗?返回最晚的那条,过期的顺手清掉。"""
        moment = self._now() if now is None else int(now)
        with self._lock:
            self._conn.execute(
                "DELETE FROM agent_mutes WHERE expires_at <= ?", (moment,)
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT kind, expires_at, reason FROM agent_mutes "
                "WHERE agent_id = ? AND player_id = ? AND expires_at > ? "
                "ORDER BY expires_at DESC LIMIT 1",
                (agent_id, player_id, moment),
            ).fetchone()
        if row is None:
            return None
        return {"kind": row[0], "expires_at": int(row[1]), "reason": row[2],
                "seconds_left": max(0, int(row[1]) - moment)}

    def clear_quiet(self, agent_id: str, player_id: str, *, kind: str | None = None) -> None:
        with self._lock:
            if kind is None:
                self._conn.execute(
                    "DELETE FROM agent_mutes WHERE agent_id = ? AND player_id = ?",
                    (agent_id, player_id),
                )
            else:
                self._conn.execute(
                    "DELETE FROM agent_mutes WHERE agent_id = ? AND player_id = ? AND kind = ?",
                    (agent_id, player_id, kind),
                )
            self._conn.commit()

    def mutes(self, agent_id: str | None = None, *, now: int | None = None) -> list[dict[str, Any]]:
        moment = self._now() if now is None else int(now)
        with self._lock:
            if agent_id is None:
                rows = self._conn.execute(
                    "SELECT agent_id, player_id, kind, expires_at, reason FROM agent_mutes "
                    "WHERE expires_at > ? ORDER BY expires_at",
                    (moment,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT agent_id, player_id, kind, expires_at, reason FROM agent_mutes "
                    "WHERE agent_id = ? AND expires_at > ? ORDER BY expires_at",
                    (agent_id, moment),
                ).fetchall()
        return [
            {"agent_id": r[0], "player_id": r[1], "kind": r[2],
             "expires_at": int(r[3]), "reason": r[4], "seconds_left": max(0, int(r[3]) - moment)}
            for r in rows
        ]

    # ── 拒谈话题(#15) ──────────────────────────────────────────────────────

    def refuse_topic(
        self, agent_id: str, keyword: str, *, minutes: float | None = None,
        now: int | None = None,
    ) -> None:
        keyword = str(keyword).strip()
        if not keyword:
            raise ValueError("keyword is required")
        base = self._now() if now is None else int(now)
        expires = None if minutes is None else base + int(max(0.0, float(minutes)) * 60)
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_refused_topics (agent_id, keyword, expires_at, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(agent_id, keyword) DO UPDATE SET "
                "expires_at=excluded.expires_at, created_at=excluded.created_at",
                (agent_id, keyword, expires, base),
            )
            self._conn.commit()

    def refused_topics(self, agent_id: str, *, now: int | None = None) -> list[dict[str, Any]]:
        moment = self._now() if now is None else int(now)
        with self._lock:
            self._conn.execute(
                "DELETE FROM agent_refused_topics WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (moment,),
            )
            self._conn.commit()
            rows = self._conn.execute(
                "SELECT keyword, expires_at FROM agent_refused_topics WHERE agent_id = ? "
                "ORDER BY keyword",
                (agent_id,),
            ).fetchall()
        return [{"keyword": r[0], "expires_at": None if r[1] is None else int(r[1])} for r in rows]

    def topics_hit_by(self, agent_id: str, text: str, *, now: int | None = None) -> list[str]:
        """这条消息又踩到了哪些她拒绝谈的话题。"""
        lowered = (text or "").lower()
        return [
            row["keyword"] for row in self.refused_topics(agent_id, now=now)
            if row["keyword"].lower() in lowered
        ]

    # ── 回头找你的约(#15 delay_reply) ──────────────────────────────────────

    def add_followup(
        self, agent_id: str, player_id: str, *, due_tick: int,
        kind: str = "delayed_reply", reason: str | None = None, created_tick: int = 0,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO agent_followups (agent_id, player_id, due_tick, kind, reason, created_tick) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (agent_id, player_id, int(due_tick), kind, reason, int(created_tick)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def due_followups(self, tick: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, agent_id, player_id, due_tick, kind, reason FROM agent_followups "
                "WHERE fired_tick IS NULL AND due_tick <= ? ORDER BY due_tick, id",
                (int(tick),),
            ).fetchall()
        return [
            {"id": int(r[0]), "agent_id": r[1], "player_id": r[2],
             "due_tick": int(r[3]), "kind": r[4], "reason": r[5]}
            for r in rows
        ]

    def mark_followup_fired(self, followup_id: int, tick: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE agent_followups SET fired_tick = ? WHERE id = ?",
                (int(tick), int(followup_id)),
            )
            self._conn.commit()

    def pending_followups(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if agent_id is None:
                rows = self._conn.execute(
                    "SELECT id, agent_id, player_id, due_tick, kind, reason FROM agent_followups "
                    "WHERE fired_tick IS NULL ORDER BY due_tick, id"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, agent_id, player_id, due_tick, kind, reason FROM agent_followups "
                    "WHERE fired_tick IS NULL AND agent_id = ? ORDER BY due_tick, id",
                    (agent_id,),
                ).fetchall()
        return [
            {"id": int(r[0]), "agent_id": r[1], "player_id": r[2],
             "due_tick": int(r[3]), "kind": r[4], "reason": r[5]}
            for r in rows
        ]

    # ── persona override(#16) ───────────────────────────────────────────────

    def set_override(self, agent_id: str, player_id: str, kind: str, value: str) -> None:
        """后到覆盖(同 kind 唯一):玩家改主意就是改主意。"""
        if kind not in OVERRIDE_KINDS:
            raise ValueError(f"unknown override kind {kind!r}")
        value = str(value).strip()
        if not value:
            raise ValueError("override value is required")
        with self._lock:
            self._conn.execute(
                "INSERT INTO persona_overrides (agent_id, player_id, kind, value, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(agent_id, player_id, kind) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (agent_id, player_id, kind, value, self._now()),
            )
            self._conn.commit()

    def overrides(self, agent_id: str, player_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, value, updated_at FROM persona_overrides "
                "WHERE agent_id = ? AND player_id = ? ORDER BY kind",
                (agent_id, player_id),
            ).fetchall()
        return [{"kind": r[0], "value": r[1], "updated_at": int(r[2] or 0)} for r in rows]

    def clear_override(self, agent_id: str, player_id: str, kind: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM persona_overrides WHERE agent_id = ? AND player_id = ? AND kind = ?",
                (agent_id, player_id, kind),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ── 消息行上的观测量 ────────────────────────────────────────────────────

    def annotate_message(
        self, message_id: int, *, stance: str | None = None, intent: str | None = None,
        intent_confidence: float | None = None, tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        """把一轮的观测量写到消息行上。给出什么写什么,None 一律不动。"""
        sets: list[str] = []
        values: list[Any] = []
        if stance is not None:
            sets.append("stance = ?")
            values.append(stance)
        if intent is not None:
            sets.append("intent = ?")
            values.append(intent)
        if intent_confidence is not None:
            sets.append("intent_confidence = ?")
            values.append(float(intent_confidence))
        if tool_calls is not None:
            sets.append("tool_calls = ?")
            values.append(json.dumps(tool_calls, ensure_ascii=False))
        if not sets:
            return
        values.append(int(message_id))
        with self._lock:
            self._conn.execute(
                f"UPDATE messages SET {', '.join(sets)} WHERE id = ?", values
            )
            self._conn.commit()

    def conversation_meta(self, conversation_id: int) -> dict[str, Any]:
        """一场会话的 stance / intent 分布 + 调过的工具 —— 关闭事件带的那份。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, stance, intent, tool_calls FROM messages "
                "WHERE conversation_id = ? ORDER BY id",
                (int(conversation_id),),
            ).fetchall()
        stances: dict[str, int] = {}
        intents: dict[str, int] = {}
        tools: list[str] = []
        for role, stance_value, intent_value, tool_json in rows:
            if stance_value:
                stances[stance_value] = stances.get(stance_value, 0) + 1
            if intent_value:
                intents[intent_value] = intents.get(intent_value, 0) + 1
            if tool_json:
                try:
                    for call in json.loads(tool_json) or []:
                        name = (call or {}).get("tool")
                        if name:
                            tools.append(str(name))
                except ValueError:
                    logger.warning("消息 tool_calls 不是合法 JSON,跳过")
        meta: dict[str, Any] = {}
        if stances:
            meta["stances"] = stances
        if intents:
            meta["intents"] = intents
        if tools:
            meta["tools_used"] = tools
        return meta
