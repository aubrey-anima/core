"""TriggerEngine: decide whether an event is memory-worthy (M4 §3).

Rule-based, not full recall — only "sufficiently important" events promote
to a memory (design.md D2). Shared by the live scheduler write path and
`MemoryStore.rebuild()` so replay and live writes use one source of truth.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from typing import Any

from anima_world.memory_store import MemoryDescriptor
from anima_world.types import Projection
from anima_world.world_time import WALL_CLOCK_FLOOR

logger = logging.getLogger(__name__)

_DEFAULT_SENTIMENT_THRESHOLD = 0.3
_USER_CONVERSATION_IMPORTANCE = 0.8
_STATE_CHANGE_IMPORTANCE = 0.5

# 收到一件礼物有多重。比八卦重(那是别人嘴里的事),比"有人当面交代我一件事"
# (`directive`,0.7)轻一点 —— 一件礼物是心意,一句交代是**他等着我的回音**。
# 落在 0.6 以上是有原因的:`contact._contact_reasons` 拿 0.6 当"强记忆"的线,
# 于是这条记忆免费成为她主动想起送礼那个人的由头。
_GIFT_IMPORTANCE = 0.65

# 不是人的那几个账本 holder。**判据是"人给人的"**,所以这些一律不算 ——
# 城镇(`__town__`)是无中生有的源头、`shop:*` 是买卖的对手方。
_NON_PERSON_PREFIXES = ("shop:",)
_NON_PERSON_HOLDERS = {"__town__", "__world__"}   # economy.TOWN / beats.BEAT_WORLD_HOLDER

# relationship-stage-machine: fixed band edges over the sentiment axis. The
# band a value falls in is DERIVED, never stored (a stored copy would be a
# second source of truth — the bt-duties D3 / nested-map D7 lesson). Names
# are only ever rendered into memory summaries.
BAND_EDGES = (-0.6, -0.2, 0.2, 0.5, 0.8)
BAND_NAMES = ("宿敌", "交恶", "淡漠", "熟识", "亲近", "挚交")


def band(value: float) -> int:
    """Which relationship band a sentiment value falls in (0..5). Pure;
    boundary values belong to the upper band (band(-0.2) == 淡漠)."""
    return bisect_right(BAND_EDGES, value)


class TriggerEngine:
    """Evaluates one event at a time against `projection_state` (the state
    *before* this event is folded in) and decides whether it's memory-worthy.
    """

    def __init__(
        self,
        sentiment_threshold: float | None = None,
        config_store: Any | None = None,
    ) -> None:
        self._explicit_sentiment_threshold = sentiment_threshold
        self._config_store = config_store
        self._seen_event_seqs: set[int] = set()
        # 老日志里那些盖了墙钟的 `conversation` 事件,靠它折回世界时钟。见 `_tick_of`。
        self._last_world_tick = 0

    @property
    def _sentiment_threshold(self) -> float:
        """Explicit constructor arg wins (existing test/no-DB usage); else
        live from `config_store` (`memory.sentiment_threshold`, M5 §9)."""
        if self._explicit_sentiment_threshold is not None:
            return self._explicit_sentiment_threshold
        if self._config_store is not None:
            return self._config_store.get("memory.sentiment_threshold", default=_DEFAULT_SENTIMENT_THRESHOLD)
        return _DEFAULT_SENTIMENT_THRESHOLD

    def _tick_of(self, event: dict[str, Any]) -> int:
        """这条事件发生在世界的哪一 tick。

        **每一条事件都要过这里**(在 `process` 的开头),因为它同时在维护水位线。

        2.0 之前 `chat_session` 给 `conversation` 事件盖的是墙钟,于是老世界的日志
        里混着 1.78e9 那个量级的 ts;照抄进记忆的 `tick`,`MemoryStore.query` 的
        `(tick, id)` DESC 就把召回列表整个让给了跟玩家的对话。源头已经修了
        (事件不再自己盖 ts),这里管的是**老日志重放**那一半 —— 不然
        `MemoryStore.rebuild` 会把同一批脏 tick 原样再造一遍。

        折法:事件按 seq 顺序进来,而世界时钟单调不减,所以**上一条正常事件的
        tick 就是它真正发生的那一刻**。`World.repair_memory_ticks` 用的是同一条
        规则 —— 两处给出不同答案的话,修完再 rebuild 一次就又不一样了。

        **折了就要吭声。** 源头修掉之后,活路径上再出现墙钟 ts 只剩一种可能:
        某个宿主自己 emit 了一条带墙钟 `ts` 的事件。悄悄折回去等于把它藏起来 ——
        而这一层"能兜住"正是当初没人发现记忆被占满的原因。
        """
        ts = int(event.get("ts") or 0)
        if ts >= WALL_CLOCK_FLOOR:
            logger.warning(
                "事件 seq=%s type=%s 的 ts=%s 是墙钟不是世界时钟 —— 按上一条正常"
                "事件的 tick=%s 折算。老世界重放是正常的;活着的世界出现这条,"
                "说明有人 emit 事件时自己盖了 time.time()。",
                event.get("seq"), event.get("type"), ts, self._last_world_tick,
            )
            return self._last_world_tick
        self._last_world_tick = max(self._last_world_tick, ts)
        return ts

    def process(self, event: dict[str, Any], projection_state: Projection) -> MemoryDescriptor | None:
        tick = self._tick_of(event)
        seq = event.get("seq")
        if seq is not None:
            if seq in self._seen_event_seqs:
                return None
            self._seen_event_seqs.add(seq)

        event_type = event.get("type")
        if event_type == "conversation":
            return self._on_conversation(event, tick)
        if event_type == "item_transfer":
            return self._on_item_transfer(event, projection_state, tick)
        if event_type == "state_change":
            kind = event.get("payload", {}).get("kind")
            if kind == "sentiment":
                return self._on_sentiment(event, projection_state, tick)
            if kind == "sentiment_delta":
                return self._on_sentiment_delta(event, projection_state, tick)
            if kind == "agent_state":
                return self._on_agent_state(event, projection_state, tick)
        return None

    def _on_conversation(self, event: dict[str, Any], tick: int) -> MemoryDescriptor:
        payload = event.get("payload", {})
        return MemoryDescriptor(
            agent_id=payload.get("agent_id") or event.get("who"),
            tick=tick,
            kind="user_conversation",
            summary=payload.get("summary", ""),
            importance=_USER_CONVERSATION_IMPORTANCE,
            event_seq=event.get("seq"),
        )

    # ── 礼物被珍藏 ──────────────────────────────────────────────────────────

    @staticmethod
    def _is_person(holder: Any, projection_state: Projection) -> bool:
        """这个账本 holder 是不是**一个人**。

        两种算:登记在册的角色,和 `player:*`。其余(城镇金库、货架、以及任何
        宿主自己发明的 holder)一律不算 —— 一个人不会因为货架把咖啡递给他就
        记住货架。
        """
        name = str(holder or "")
        if not name:
            return False
        if name.startswith("player:"):
            return True
        if name in _NON_PERSON_HOLDERS or name.startswith(_NON_PERSON_PREFIXES):
            return False
        return name in projection_state.agents

    def _on_item_transfer(
        self, event: dict[str, Any], projection_state: Projection, tick: int
    ) -> MemoryDescriptor | None:
        """人给人的一件东西 → 她记得是谁给的(gift)。

        **这里最重要的是那道闸,不是那条记忆。** 账本上的 `item_transfer` 是一条
        大路:买东西走它、创世注入走它、节拍发货走它、玩家送礼也走它。给每一条都
        落一条记忆的话,一个跑着经济的世界一天几十条,而记忆表是**有界的** ——
        她真正的过去会被"从货架上拿了一杯咖啡"挤出去,一声不吭。这正是这个仓库
        最怕的坏法:表看着满满的,里面全是废话。

        判据两条,都必须成立:

        1. **两头都是人。** 货架、城镇金库、`__world__` 一律不是人;没有 `from`
           的那条(创世无中生有)也不是 —— 那不是谁给的,那是她一开始就有的。
        2. **不是一笔交易。** 带 `reason` 的转移是账目(领料、结算、买卖的货物腿),
           它有对家、有代价;礼物没有。写成"没有 reason 才算"而不是穷举交易的种类:
           往后新增一种账目时,漏掉它的后果是"少记一条记忆",而反过来漏掉的后果是
           "她的记忆被账目灌满",两者不对称。

        记的是**收礼的那个角色**。给出去那一头没有记忆:多半是玩家(玩家没有记忆表),
        而角色送角色时送出者身上真正值得记的是关系变化,那条路自己有(`relation_shift`)。
        """
        payload = event.get("payload") or {}
        source, target = payload.get("from"), payload.get("to")
        if not source or not target or source == target:
            return None
        if payload.get("reason"):
            return None
        if not self._is_person(source, projection_state):
            return None
        if not self._is_person(target, projection_state):
            return None
        # 收礼的必须是**角色** —— `player:*` 那一头没有记忆表可写。
        receiver = str(target)
        if receiver not in projection_state.agents:
            return None
        try:
            qty = int(payload.get("qty", 1))
        except (TypeError, ValueError):
            qty = 1
        if qty <= 0:
            return None

        # 名字随事件走(见 `World.give_item`)。写不出名字的老日志退回 id ——
        # 难看,但不会因此少记一条:一条读不通的记忆也好过她压根不记得。
        giver = str(payload.get("from_name") or "").strip() or self._person_name(
            source, projection_state
        )
        item = str(payload.get("item_name") or "").strip() or str(payload.get("item_id") or "")
        count = f" ×{qty}" if qty > 1 else ""
        return MemoryDescriptor(
            agent_id=receiver,
            tick=tick,
            kind="gift",
            summary=f"{giver}把{item}{count}给了我",
            importance=_GIFT_IMPORTANCE,
            event_seq=event.get("seq"),
        )

    @staticmethod
    def _person_name(holder: Any, projection_state: Projection) -> str:
        """账本 holder → 人话名字。查不到就用 id —— 空着比 id 更难读。"""
        name = str(holder or "")
        agent = projection_state.agents.get(name)
        if agent is not None:
            return str((agent.spec or {}).get("name") or name)
        return name.split(":", 1)[1] if name.startswith("player:") else name

    @classmethod
    def _relation_names(
        cls, payload: dict[str, Any], as_id: str, target_id: str,
        projection_state: Projection,
    ) -> tuple[str, str]:
        """这条关系变化的两头**叫什么**。

        记忆的摘要是给人读的,而它有一个特别重要的读者:**八卦**。
        `gossip.pick_gossip` 把摘要原样转述("听苏晚夏说:…"),所以一条写着 id 的
        `relation_shift` 传出去就是一句没人看得懂的话 —— 玩家那一头尤其,他的 id
        是一串 uuid。

        名字优先从**事件自己**上读(`as_name` / `target_name`,判定那一刻写下的),
        读不到才回落到投影里的角色名,最后才是 id。顺序是有讲究的:投影只认识
        角色,玩家不在里面;而事件是那一刻的事实,重放出来对得上。
        """
        as_name = str(payload.get("as_name") or "").strip()
        target_name = str(payload.get("target_name") or "").strip()
        return (
            as_name or cls._person_name(as_id, projection_state),
            target_name or cls._person_name(target_id, projection_state),
        )

    def _on_sentiment(
        self, event: dict[str, Any], projection_state: Projection, tick: int
    ) -> MemoryDescriptor | None:
        payload = event.get("payload", {})
        # beat-director: `seed: true` marks exogenous backfill (a beat
        # seeding a joining character's relations), not a lived relationship
        # swing — it must not mint a relation_shift memory, and through the
        # scheduler's relation_shift hook it would otherwise mint a
        # "friendship" edge even for a seeded -0.7 enmity.
        if payload.get("seed"):
            return None
        as_id = payload.get("as") or event.get("who")
        target_id = payload.get("target")
        if as_id is None or target_id is None or "sentiment" not in payload:
            return None
        new_sentiment = float(payload["sentiment"])
        relation = projection_state.relations.get((as_id, target_id))
        old_sentiment = relation.sentiment if relation is not None else 0.0
        delta = abs(new_sentiment - old_sentiment)
        if delta < self._sentiment_threshold:
            return None
        as_name, target_name = self._relation_names(payload, as_id, target_id, projection_state)
        return MemoryDescriptor(
            agent_id=as_id,
            tick=tick,
            kind="relation_shift",
            summary=f"{as_name} 对 {target_name} 的关系发生剧变（Δ={delta:.2f}）",
            importance=delta,
            event_seq=event.get("seq"),
        )

    def _on_sentiment_delta(
        self, event: dict[str, Any], projection_state: Projection, tick: int
    ) -> MemoryDescriptor | None:
        """relationship-stage-machine: a delta is memory-worthy when the
        ACCUMULATED value crosses a band edge — not per-event magnitude (the
        judge caps ±0.2/verdict, below the absolute-path threshold, which is
        why the graph never grew). `projection_state` is the value BEFORE this
        event folds in, same contract as `_on_sentiment`."""
        payload = event.get("payload", {})
        if payload.get("seed"):
            return None  # exogenous backfill, not a lived swing (beat-director)
        as_id = payload.get("as") or event.get("who")
        target_id = payload.get("target")
        if as_id is None or target_id is None:
            return None
        try:
            delta = float(payload.get("delta"))
        except (TypeError, ValueError):
            return None
        relation = projection_state.relations.get((as_id, target_id))
        old = relation.sentiment if relation is not None else 0.0
        new = max(-1.0, min(1.0, old + delta))
        old_band, new_band = band(old), band(new)
        if old_band == new_band:
            return None
        as_name, target_name = self._relation_names(payload, as_id, target_id, projection_state)
        return MemoryDescriptor(
            agent_id=as_id,
            tick=tick,
            kind="relation_shift",
            summary=(
                f"{as_name} 对 {target_name} 的关系从「{BAND_NAMES[old_band]}」"
                f"进入「{BAND_NAMES[new_band]}」（{old:+.2f}→{new:+.2f}）"
            ),
            importance=min(0.9, 0.5 + 0.1 * abs(new_band - old_band)),
            event_seq=event.get("seq"),
        )

    def _on_agent_state(
        self, event: dict[str, Any], projection_state: Projection, tick: int
    ) -> MemoryDescriptor | None:
        agent_id = event.get("who")
        if agent_id is None:
            return None
        new_status = event.get("payload", {}).get("state", {}).get("status")
        agent = projection_state.agents.get(agent_id)
        old_status = agent.state.get("status") if agent is not None else None
        if new_status == old_status:
            return None
        return MemoryDescriptor(
            agent_id=agent_id,
            tick=tick,
            kind="state_change",
            summary=f"{agent_id} 的状态从 {old_status} 变为 {new_status}",
            importance=_STATE_CHANGE_IMPORTANCE,
            event_seq=event.get("seq"),
        )
