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

# 释放线:开始恢复之后,**吃到饱/睡到醒**才收工,而不是跨回触发线就走。
#
# 单阈值(只有 URGENT)的后果实测过:hunger 掉到 0.15 → 吃一个 tick 净回 0.045 →
# 已经 0.195 高于触发线 → 立刻回去干活 → 十来个 tick 后再饿回来。角色永远卡在
# 16% 的饥饿度上,一顿饱饭都没吃过;而每一次切换都发一条 agent_action + 一条
# narrative,于是 12 世界日的事件量 19.7×、narrative 32×(配了真 key 就是 32 倍
# LLM 账单)、耗时 7×。世界并没有变得 32 倍有趣,只是抖得厉害。
#
# 取值按"一次恢复动作应该持续多久"倒推,与 RESTORE_PER_TICK 的设计口径一致:
#   hunger 0.15→0.75 ≈ 13 tick ≈ 一小时,正好是上面写的"吃一顿约 1 小时回 0.6"
#   energy 0.15→0.85 ≈ 78 tick ≈ 6.5 小时,一个补觉的长度(整夜是 duty 的事)
#   social 0.15→0.50 ≈ 20 tick ≈ 一个半小时的长谈
RELEASE = {"energy": 0.85, "hunger": 0.75, "social": 0.50}


def restores(action_kind: str | None) -> frozenset[str]:
    """这个动作正在补哪几条需求。迟滞的判据 —— 正在补的那条才用释放线。"""
    return frozenset(RESTORE_PER_TICK.get(action_kind or "", {}))


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
