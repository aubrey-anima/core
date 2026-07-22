"""needs-v3:需求曲线 —— 角色从"按表办事"到"活着"。

四条需求:energy / hunger / social 随 tick 衰减、由动作恢复;mood 是前三者的
派生(木桶效应),**永不存储**(第二真相源教训)。连续曲线也不进事件日志 ——
它们是纯数学,重放 tick 即可重建;持久化只有 agent_needs 表里的当前值
(data-plane),日切与关闭时落盘。

默认关闭(config `needs.enabled`):不点亮的世界行为与 v2 逐 tick 一致。
"""

from __future__ import annotations

import sqlite3
from typing import Any

NEEDS = ("energy", "hunger", "social")

# 每 tick(默认 5 世界分钟)的衰减:一天不睡精力见底、约 18 小时饿透、
# 一天半不社交就孤独。
DECAY_PER_TICK = {"energy": 1.0 / 288, "hunger": 1.0 / 216, "social": 1.0 / 432}

# 动作的恢复速率(净值 = 恢复 - 衰减):睡 8 小时(96 tick)回满,
# 吃一顿(约 1 小时)回 0.6,聊天/闲坐缓慢回社交。
RESTORE_PER_TICK: dict[str, dict[str, float]] = {
    "sleep": {"energy": 1.0 / 80},
    "eat": {"hunger": 0.05},
    "chat": {"social": 0.02},
    "idle_social": {"social": 0.015},
}

URGENT = 0.15  # 需求带的触发线:低于它,恢复动作压过 duty


def settle(
    values: dict[str, float], elapsed_ticks: int, action_kind: str | None
) -> dict[str, float]:
    """把需求推进 elapsed_ticks:衰减 + 当前动作的恢复,clamp 到 [0,1]。
    纯函数;mood 现算不入 values 存储。"""
    restore = RESTORE_PER_TICK.get(action_kind or "", {})
    out: dict[str, float] = {}
    for need in NEEDS:
        value = float(values.get(need, 1.0))
        value -= DECAY_PER_TICK[need] * elapsed_ticks
        value += restore.get(need, 0.0) * elapsed_ticks
        out[need] = max(0.0, min(1.0, value))
    out["mood"] = 0.2 + 0.8 * min(out[n] for n in NEEDS)
    return out


def load(conn: sqlite3.Connection, agent_id: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT need, value FROM agent_needs WHERE agent_id = ?", (agent_id,)
    ).fetchall()
    values = {need: float(value) for need, value in rows if need in NEEDS}
    for need in NEEDS:
        values.setdefault(need, 1.0)
    return values


def persist(conn: sqlite3.Connection, agent_id: str, values: dict[str, Any], tick: int) -> None:
    for need in NEEDS:
        conn.execute(
            "INSERT INTO agent_needs (agent_id, need, value, updated_tick) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(agent_id, need) DO UPDATE SET value=excluded.value,"
            " updated_tick=excluded.updated_tick",
            (agent_id, need, float(values.get(need, 1.0)), tick),
        )
    conn.commit()
