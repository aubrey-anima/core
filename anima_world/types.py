"""Core dataclasses for the anima_world event engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    seq: int
    ts: int
    type: str
    loc: str | None
    payload: dict[str, Any]
    who: str | None = None

    def to_row(self) -> tuple:
        """Convert to a tuple for SQLite insertion (seq omitted—AUTOINCREMENT)."""
        import json

        return (self.ts, self.type, self.who, self.loc, json.dumps(self.payload))

    @classmethod
    def from_row(cls, row: tuple) -> "Event":
        """Build Event from a SQLite row (seq, ts, type, who, loc, payload)."""
        import json

        seq, ts, type_, who, loc, payload_json = row
        return cls(seq=seq, ts=ts, type=type_, who=who, loc=loc, payload=json.loads(payload_json))


@dataclass
class AgentState:
    spec: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    location: str | None = None
    joined_at: int = 0
    updated_at: int = 0


@dataclass
class Relation:
    r_type: str = "acquaintance"
    r_type_back: str = "acquaintance"
    sentiment: float = 0.0
    # relations-v5: three finer axes riding the same delta machinery.
    # `sentiment` stays the headline (bands/edges/relabel key off it).
    trust: float = 0.0
    affection: float = 0.0
    respect: float = 0.0
    # 上一次改变它的那一条 —— 出处。
    #
    # 它**也是折出来的**,和 sentiment 同一条路、同一次重放:所以它不是缓存,
    # 没有"和事实对不上"的坏法。放在这儿而不是每次去翻日志,是因为翻日志要
    # 从头扫(事件表只有 ts / type 索引),而这一层是**每渲染一帧问一次**的。
    #
    # `None` 的意思是"这段关系没有一条说得出来的出处"(创世注入的好感度就是
    # 这样:那是作者的一句声明,不是这两个人之间发生过的事)。
    # 形状:{seq, tick, delta, direction, conversation_id, summary,
    #        as_name, target_name}
    last_change: dict[str, Any] | None = None


@dataclass
class Location:
    id: str = ""
    name: str = ""
    description: str = ""


@dataclass
class Capability:
    id: str = ""
    kind: str = ""
    description: str = ""
    params_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class Projection:
    agents: dict[str, AgentState] = field(default_factory=dict)
    relations: dict[tuple[str, str], Relation] = field(default_factory=dict)
    locations: dict[str, Location] = field(default_factory=dict)
    narrative_log: list[dict[str, Any]] = field(default_factory=list)
    """M2: accumulated narrative entries (agent, text, ts) — backward compat default empty."""
    capabilities: dict[str, Capability] = field(default_factory=dict)
    """M6: capability catalog, folded from capability_registered events."""
    balances: dict[str, float] = field(default_factory=dict)
    """economy-v4: folded from payment events. Audit = replay."""
    inventories: dict[str, dict[str, int]] = field(default_factory=dict)
    """economy-v4: holder → {item_id: qty}, folded from item_transfer/consume."""
    allowances: set[str] = field(default_factory=set)
    """领过见面礼的 holder。**这是"只给一次"那个一次的家。**

    它必须挂在一件不会过期的事上,而在场(`RedisPlayerPresence`)有 TTL —— 挂在
    那里的话,"他这辈子头一回露面"会悄悄变成"他这一刻钟里头一回露面"。账本是
    投影、由全量重放折出来,所以这一格重启、换进程、TTL 到头都还在。

    **和 `balances` 同生共死**:`player_forget` 不清余额,所以也不清这一格 ——
    清了的话一个被忘掉又回来的人会带着旧钱再领一次新钱。
    """
