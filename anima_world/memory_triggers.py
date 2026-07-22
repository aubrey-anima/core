"""TriggerEngine: decide whether an event is memory-worthy (M4 §3).

Rule-based, not full recall — only "sufficiently important" events promote
to a memory (design.md D2). Shared by the live scheduler write path and
`MemoryStore.rebuild()` so replay and live writes use one source of truth.
"""

from __future__ import annotations

from bisect import bisect_right
from typing import Any

from anima_world.memory_store import MemoryDescriptor
from anima_world.types import Projection

_DEFAULT_SENTIMENT_THRESHOLD = 0.3
_USER_CONVERSATION_IMPORTANCE = 0.8
_STATE_CHANGE_IMPORTANCE = 0.5

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

    @property
    def _sentiment_threshold(self) -> float:
        """Explicit constructor arg wins (existing test/no-DB usage); else
        live from `config_store` (`memory.sentiment_threshold`, M5 §9)."""
        if self._explicit_sentiment_threshold is not None:
            return self._explicit_sentiment_threshold
        if self._config_store is not None:
            return self._config_store.get("memory.sentiment_threshold", default=_DEFAULT_SENTIMENT_THRESHOLD)
        return _DEFAULT_SENTIMENT_THRESHOLD

    def process(self, event: dict[str, Any], projection_state: Projection) -> MemoryDescriptor | None:
        seq = event.get("seq")
        if seq is not None:
            if seq in self._seen_event_seqs:
                return None
            self._seen_event_seqs.add(seq)

        event_type = event.get("type")
        if event_type == "conversation":
            return self._on_conversation(event)
        if event_type == "state_change":
            kind = event.get("payload", {}).get("kind")
            if kind == "sentiment":
                return self._on_sentiment(event, projection_state)
            if kind == "sentiment_delta":
                return self._on_sentiment_delta(event, projection_state)
            if kind == "agent_state":
                return self._on_agent_state(event, projection_state)
        return None

    def _on_conversation(self, event: dict[str, Any]) -> MemoryDescriptor:
        payload = event.get("payload", {})
        return MemoryDescriptor(
            agent_id=payload.get("agent_id") or event.get("who"),
            tick=int(event.get("ts", 0)),
            kind="user_conversation",
            summary=payload.get("summary", ""),
            importance=_USER_CONVERSATION_IMPORTANCE,
            event_seq=event.get("seq"),
        )

    def _on_sentiment(
        self, event: dict[str, Any], projection_state: Projection
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
        return MemoryDescriptor(
            agent_id=as_id,
            tick=int(event.get("ts", 0)),
            kind="relation_shift",
            summary=f"{as_id} 对 {target_id} 的关系发生剧变（Δ={delta:.2f}）",
            importance=delta,
            event_seq=event.get("seq"),
        )

    def _on_sentiment_delta(
        self, event: dict[str, Any], projection_state: Projection
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
        return MemoryDescriptor(
            agent_id=as_id,
            tick=int(event.get("ts", 0)),
            kind="relation_shift",
            summary=(
                f"{as_id} 对 {target_id} 的关系从「{BAND_NAMES[old_band]}」"
                f"进入「{BAND_NAMES[new_band]}」（{old:+.2f}→{new:+.2f}）"
            ),
            importance=min(0.9, 0.5 + 0.1 * abs(new_band - old_band)),
            event_seq=event.get("seq"),
        )

    def _on_agent_state(
        self, event: dict[str, Any], projection_state: Projection
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
            tick=int(event.get("ts", 0)),
            kind="state_change",
            summary=f"{agent_id} 的状态从 {old_status} 变为 {new_status}",
            importance=_STATE_CHANGE_IMPORTANCE,
            event_seq=event.get("seq"),
        )
