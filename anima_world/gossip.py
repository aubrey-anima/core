"""social-v5:八卦传播 —— 信息第一次可以间接到达。

同地聊天时,说者一条足够重要的记忆以概率复制给听者(kind='hearsayN',
N 是转手数),每转一手重要度打折,三手以后自然消亡 —— 谣言有半衰期。
声誉由此涌现:没见过你的人也可能"听说过你"。

纯函数模块:不掷骰子之外的任何主意,不 import 存储层;调用方(scheduler)
在 tick 帧内只做抽样,记忆落地走 memory_seed 事件。
"""

from __future__ import annotations

from typing import Any

GOSSIP_PROBABILITY = 0.25
DISTORTION = 0.15   # 每转手的重要度折损
MAX_HOPS = 3        # 三手之后不再传
MIN_IMPORTANCE = 0.5


def _hop(kind: str) -> int:
    if kind.startswith("hearsay"):
        try:
            return int(kind[len("hearsay"):])
        except ValueError:
            return MAX_HOPS
    return 0


def pick_gossip(
    rng: Any,
    speaker_id: str,
    speaker_memories: list[dict[str, Any]],
    listener_id: str,
) -> dict[str, Any] | None:
    """从说者的记忆里挑一条可传的八卦给听者;不传时返回 None。

    可传 = 重要度 ≥ 0.5、转手数 < 3、不是反思(内心活动不外传)。
    命中概率 GOSSIP_PROBABILITY,选重要度最高的一条。
    """
    if rng.random() > GOSSIP_PROBABILITY:
        return None
    candidates = [
        m for m in speaker_memories
        if float(m.get("importance") or 0.0) >= MIN_IMPORTANCE
        and _hop(str(m.get("kind") or "")) < MAX_HOPS
        and m.get("kind") != "reflection"
    ]
    if not candidates:
        return None
    src = max(candidates, key=lambda m: float(m.get("importance") or 0.0))
    hop = _hop(str(src.get("kind") or "")) + 1
    return {
        "agent_id": listener_id,
        "kind": f"hearsay{hop}",
        "summary": f"听{speaker_id}说:{src.get('summary', '')}",
        "importance": round(float(src.get("importance") or 0.0) * (1 - DISTORTION), 3),
    }
