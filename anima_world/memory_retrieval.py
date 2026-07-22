"""memory-2.0:三因子记忆检索打分(Stanford Generative Agents 配方)。

score = w_r·recency + w_i·importance + w_v·relevance

- recency:按 tick 年龄指数衰减(半衰期可配,默认 3 个世界日)
- importance:写入时定的重要度(0~1)
- relevance:查询与记忆文本的字符二元组 Jaccard 相似度 —— 对中文友好、
  零依赖;将来接嵌入(sqlite-vec)只需替换 similarity(),打分公式不动。

纯函数模块:不 import 存储层,不碰锁 —— MemoryStore.retrieve 是唯一调用方。
"""

from __future__ import annotations

import math
from typing import Any

HALF_LIFE_DAYS_DEFAULT = 3.0


def bigrams(text: str) -> set[str]:
    text = "".join(ch for ch in text if not ch.isspace())
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def similarity(query: str, text: str) -> float:
    """字符二元组重叠系数(交集/较小集)。查询通常远短于记忆文本,
    Jaccard 会把"查询词全命中"稀释成 0.2 不到;重叠系数让全命中 = 1。
    空查询 = 0(检索退化为 recency+importance)。"""
    a, b = bigrams(query), bigrams(text)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def score(
    memory: dict[str, Any],
    *,
    now_tick: int,
    query: str | None,
    ticks_per_day: int = 288,
    half_life_days: float = HALF_LIFE_DAYS_DEFAULT,
    w_recency: float = 1.0,
    w_importance: float = 1.0,
    w_relevance: float = 1.5,
) -> float:
    half_life_ticks = max(1.0, float(half_life_days) * ticks_per_day)
    age = max(0, int(now_tick) - int(memory.get("tick") or 0))
    recency = math.exp(-math.log(2) * age / half_life_ticks)
    importance = min(1.0, max(0.0, float(memory.get("importance") or 0.0)))
    relevance = similarity(query, str(memory.get("summary") or "")) if query else 0.0
    return w_recency * recency + w_importance * importance + w_relevance * relevance
