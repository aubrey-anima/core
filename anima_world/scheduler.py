"""Event-drivenScheduler: dispatches events, drives world clock, handles idle + action emission."""

from __future__ import annotations

import logging
import math
import threading
import time
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Protocol

from anima_world.actions import ActionDescriptor, to_event
from anima_world.agent import Agent
from anima_world.beats import (
    BeatDirector,
    BeatScript,
    coerce_goals,
    expand_event_op,
    expand_relations,
    memory_seed_event,
)
from anima_world.bt_nodes import Blackboard
from anima_world.events import EventLog
from anima_world.narrative import NarrativeProvider
from anima_world.projection import project_events
from anima_world.types import Event
from anima_world.world_time import (
    DEFAULT_MINUTES_PER_TICK,
    WALL_CLOCK_FLOOR,
    WorldTime,
    world_time,
)


logger = logging.getLogger(__name__)

# The `events.ts` column carries two different time bases — see
# `world_time.WALL_CLOCK_FLOOR` for the full statement of the rule. Restoring
# the clock skips wall-clock stamps; so does the run report (`sim_report`).
_WALL_CLOCK_FLOOR = WALL_CLOCK_FLOOR

# Performance guardrails
MAX_AGENTS = 100

# llm-relationship-judge: minimum ticks between verdicts for the same
# (unordered) agent pair — both sides landing chats at each other within
# one window is one conversation, not two. 6 ticks = 30 world minutes at
# the default 5 min/tick.
JUDGE_PAIR_COOLDOWN_TICKS = 6
MAX_TICKS_PER_SECOND = 1000
MAX_IDLE_LOOP_DEPTH = 5


class BrainLike(Protocol):
    """Brain-compatible interface for the scheduler."""

    agent: Agent

    def tick(self, events: list[dict[str, Any]]) -> ActionDescriptor | None: ...

    def tick_direct(self) -> ActionDescriptor | None: ...


class Scheduler:
    """Orchestrates agent-to-agent event flow.

    Responsibilities:
    - Maintain world clock (monotonic ticks)
    - Dispatch events to agent mailboxes (targeted or broadcast)
    - Idle watchdog: inject idle events to dormant agents
    - Action→event pipeline: convert agent actions to world events and dispatch them
    - Narrative emission (optional)
    """

    def __init__(
        self,
        tick_delta: int = 1,
        narrative_provider: NarrativeProvider | None = None,
        event_log: EventLog | None = None,
        db_path: str | None = None,
        memory_store: Any | None = None,
        knowledge_graph: Any | None = None,
        trigger_engine: Any | None = None,
        config_store: Any | None = None,
        prompt_store: Any | None = None,
        location_store: Any | None = None,
        planner: Any | None = None,
        relationship_judge: Any | None = None,
        reflector: Any | None = None,
        lock: threading.RLock | None = None,
        beat_script: BeatScript | None = None,
        beat_agent_factory: Any | None = None,
    ) -> None:
        self.agents: dict[str, BrainLike] = {}
        self._queue: deque[dict[str, Any]] = deque()
        self.tick_delta = tick_delta
        self.clock: int = 0
        self._stopped: bool = False
        self.narrative_provider = narrative_provider
        self.event_log = event_log
        self.db_path = db_path
        self.narrative_history: list[str] = [] if narrative_provider else None
        # M4: memory/graph wiring is optional (mirrors narrative_provider) — a
        # bare Scheduler() behaves exactly as before. Duck-typed (`Any`) so
        # this foundational module doesn't import the storage layer.
        self.memory_store = memory_store
        self.knowledge_graph = knowledge_graph
        self.trigger_engine = trigger_engine
        # M5: optional live-config/prompt sources; a bare Scheduler() is unchanged.
        self.config_store = config_store
        self.prompt_store = prompt_store
        # nested-map D7: the map lives in the `locations` table, not the event
        # log. Without a store there is no map to edit — see
        # `update_location_description`.
        self.location_store = location_store
        # M6: seeded from persisted history immediately when an event_log is
        # given, so update_agent_persona/update_location_description's
        # "known entity" checks are correct for ANY Scheduler built against a
        # populated log — not just one that a caller (build_serve_scheduler)
        # separately replayed into this projection after construction.
        boot_events = event_log.replay() if event_log is not None else []
        self._memory_projection = project_events(boot_events)
        # beat-director D1: the script is config; which beats FIRED is history.
        # The fired-set is rebuilt here from replayed `beat_fired` events, so a
        # restart never re-fires a beat (aligned by beat id — a script edit
        # adding new ids participates normally).
        self._beat_agent_factory = beat_agent_factory
        self.beat_director: BeatDirector | None = None
        if beat_script is not None:
            fired = {
                e.payload.get("beat_id")
                for e in boot_events
                if e.type == "beat_fired" and e.payload.get("beat_id")
            }
            self.beat_director = BeatDirector(beat_script, fired=fired)
        self._tick_count: int = 0
        self._tick_window_start: float = time.monotonic()
        # M3 web support
        # M5: accepts an externally created lock so ConfigStore/PromptStore
        # (constructed before the Scheduler, over the same connection) can
        # share it instead of racing this lock with their own private RLock.
        self._lock = lock if lock is not None else threading.RLock()
        self.recent_events: deque[dict[str, Any]] = deque(maxlen=200)
        self._event_signal = threading.Event()
        self._next_event_seq: int = 1
        # bt-duties D2: last action emitted per agent — the seam that turns a
        # per-tick BT decision into a per-transition event. Memory only: on
        # restart it's empty, so each agent re-announces what it's doing once.
        self._current_action: dict[str, ActionDescriptor] = {}
        # bt-duties D7: narrative generation calls an LLM. It used to run
        # synchronously inside emit_action while holding self._lock — that is
        # what froze this world for five days (events stopped 2026-07-08 while
        # the process stayed alive). It now runs off the tick thread.
        self._narrative_pool = (
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="narrative")
            if narrative_provider is not None
            else None
        )
        # llm-freetime-planner: the planner calls an LLM, so it runs on its own
        # pool and the tick thread only ever READS an installed plan. Duck-typed
        # (`Any`) — scheduler stays a foundational module that imports no LLM.
        # travel-and-colocation: agents in transit. Memory only — `loc` still
        # changes solely on `location_join`, so the map keeps one source of
        # truth. A restart drops journeys: the agent is simply still where it
        # set out from, and the BT sends it on its way again next tick.
        self._transit: dict[str, dict[str, Any]] = {}
        # economy-v4: per-day sales counter feeding the price drift. Memory
        # only — prices in shop_stock are the durable part.
        self._shop_sales: dict[tuple[str, str], int] = {}
        # social-v5: one gossip roll per (speaker, listener) per world day.
        # Memory only — resets at rollover and on restart.
        self._gossip_rolled: set[tuple[str, str, int]] = set()
        # memory-2.0: reflection watermark, hydrated from reflection_state on
        # first touch. Kept in memory so the per-memory path stays db-free;
        # `_reflection_dirty` is what still needs a checkpoint.
        self._reflection_watermark: dict[str, float] = {}
        self._reflection_dirty: set[str] = set()
        self.planner = planner
        self._plans: dict[str, Any] = {}          # agent_id → Plan (cache of the `plan` event)
        self._planning: set[str] = set()          # agents with a replan in flight
        # sim-ff-usability: notified whenever _planning empties, so a
        # fast-forward caller can wait for in-flight plans without polling
        # (a 50ms poll per tick made mock runs 30x slower).
        self._planning_idle = threading.Condition(self._lock)
        self._planner_pool = (
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="planner")
            if planner is not None
            else None
        )
        # llm-relationship-judge: chat → async LLM verdict (summary + deltas).
        # Same off-tick-thread discipline as narrative/planner; results are
        # recording-only (relations/memories, never behavior), so nothing
        # ever waits on this pool except the final stop() drain.
        self.relationship_judge = relationship_judge
        # memory-2.0: reflection is the fourth LLM job. It rides the judge
        # pool (recording-only, same latency profile) — no new thread pool.
        self.reflector = reflector
        self._judge_pool = (
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="judge")
            if relationship_judge is not None
            else None
        )
        # Per-pair cooldown (code review #3): when both agents' trees land
        # chats at each other in one window, two judgments would apply BOTH
        # directions' deltas twice (±0.4 worst case vs the ±0.2 design
        # ceiling per conversation). One conversation ⇒ one verdict. Memory
        # only — a restart just allows an early re-judge, which is harmless.
        self._judged_pairs: dict[frozenset[str], int] = {}
        # relationship-stage-machine D6: same-day repeat judgments for one
        # pair carry halving weight (0.5^(N-1)) — ±0.2/verdict at full weight
        # per conversation is what drove 乐瑶↔罗本 to saturate the [-1,1]
        # range in 23 world days (w1 Round 5). Memory only: a restart grants
        # one extra full-weight verdict that day, ±0.2 worst case, harmless.
        self._judge_day_counts: dict[frozenset[str], tuple[int, int]] = {}

    def set_planner(self, planner: Any | None) -> None:
        """Attach a planner after construction.

        The planner needs the live agent roster (chat targets) and their duty
        trees, which only exist once the agents are registered — so `serve`
        builds the scheduler, registers the agents, then hands the planner in.
        """
        with self._lock:
            self.planner = planner
            if planner is not None and self._planner_pool is None:
                self._planner_pool = ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="planner"
                )

    # ── Registry ──────────────────────────────────────────────────────────────

    def register(self, brain: BrainLike) -> None:
        with self._lock:
            if len(self.agents) >= MAX_AGENTS:
                raise RuntimeError(f"agent cap reached ({MAX_AGENTS})")
            self.agents[brain.agent.id] = brain

    def unregister(self, agent_id: str) -> None:
        with self._lock:
            self.agents.pop(agent_id, None)

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def dispatch(self, event: dict[str, Any]) -> None:
        """Deliver event immediately to target mailbox (targeted or broadcast)."""
        with self._lock:
            self._deliver(event)

    def _deliver(self, event: dict[str, Any]) -> None:
        """Put event into target agent's mailbox (or all mailboxes for broadcast)."""
        target = event.get("target_agent_id")
        if target is not None:
            brain = self.agents.get(target)
            if brain:
                self._append_to_mailbox(brain.agent, event)
        else:
            for brain in self.agents.values():
                self._append_to_mailbox(brain.agent, event)

    @staticmethod
    def _append_to_mailbox(agent: Agent, event: dict[str, Any]) -> None:
        mailbox = agent.blackboard.read("mailbox")
        if mailbox is None:
            mailbox = []
            agent.blackboard.write("mailbox", mailbox)
        mailbox.append(event)

    # ── Event recording (for SSE + state) ──────────────────────────────────────

    def _record_event(self, event: dict[str, Any]) -> None:
        """Append to bounded recent_events buffer and signal listeners."""
        with self._lock:
            event = dict(event)
            event.setdefault("ts", self.clock)
            if self.event_log is not None and "seq" not in event:
                persisted = self.event_log.append(self._event_log_payload(event))
                event["seq"] = persisted.seq
            if "seq" not in event:
                event["seq"] = self._next_event_seq
                self._next_event_seq += 1
            else:
                self._next_event_seq = max(self._next_event_seq, int(event["seq"]) + 1)
            # 折叠必须发生在 `_stream_event` **之前**。`_stream_event` 把
            # `state_change/location_join` 改写成 `agent_action{action:"walk"}`;
            # 折叠改写后的副本 = 位移永远进不了投影,而重放路径折的是原始事件。
            # 于是"谁在哪"取决于你有没有重启过 —— 比统一错更难查。
            # 触发器不受影响:它只认 conversation 与 state_change 的三种 kind,
            # 位移事件改写后本来就被它丢掉(memory_triggers.py:65-76)。
            self._apply_memory_trigger(event)
            self.recent_events.append(self._stream_event(event))
            self._event_signal.set()
            self._event_signal.clear()

    def _apply_memory_trigger(self, event: dict[str, Any]) -> None:
        """M4: promote memory-worthy events, then fold the event into the
        internal projection kept for computing future trigger deltas.

        A no-op unless both `trigger_engine` and `memory_store` are wired up.
        """
        # llm-relationship-judge: memory_seed is an EXPLICIT memory
        # declaration (judge chat summaries at runtime, seed injection at
        # genesis) — folded directly, bypassing TriggerEngine, with the same
        # branch _rebuild_memories' closure uses. Live path and rebuild path
        # are symmetric from here on (rich-injection only covered rebuild).
        if event.get("type") == "memory_seed" and self.memory_store is not None:
            payload = event.get("payload") or {}
            agent_id = payload.get("agent_id")
            if not agent_id:
                logger.warning("memory_seed event has no agent_id; skipping")
            else:
                kind = payload.get("kind", "seed")
                self.memory_store.add(
                    agent_id=agent_id,
                    tick=int(event.get("ts") or 0),
                    kind=kind,
                    summary=payload.get("summary", ""),
                    importance=payload.get("importance", 0.5),
                    anchor=bool(payload.get("anchor", False)),
                    event_seq=event.get("seq"),
                    source_ids=payload.get("source_ids"),
                )
                self._note_memory_written(agent_id, float(payload.get("importance", 0.5)), kind)
            return
        if self.trigger_engine is not None and self.memory_store is not None:
            descriptor = self.trigger_engine.process(event, self._memory_projection)
            if descriptor is not None:
                self.memory_store.add(
                    agent_id=descriptor.agent_id,
                    tick=descriptor.tick,
                    kind=descriptor.kind,
                    summary=descriptor.summary,
                    importance=descriptor.importance,
                    anchor=descriptor.anchor,
                    event_seq=descriptor.event_seq,
                )
                self._note_memory_written(
                    descriptor.agent_id, float(descriptor.importance), descriptor.kind
                )
                if descriptor.kind == "relation_shift":
                    self._on_relation_shift(event, descriptor)

        ev = Event(
            seq=int(event.get("seq") or 0),
            ts=int(event.get("ts") or 0),
            type=str(event.get("type") or ""),
            who=event.get("who"),
            loc=event.get("loc"),
            payload=dict(event.get("payload") or {}),
        )
        project_events([ev], base=self._memory_projection)

    # ── Reflection (memory-2.0) ────────────────────────────────────────────

    REFLECTION_KIND = "reflection"

    def _note_memory_written(self, agent_id: str, importance: float, kind: str) -> None:
        """Accumulate importance toward the reflection threshold (lock held).

        The watermark lives in memory and only touches the db when it matters:
        on the first read per agent (so a restart resumes mid-accumulation),
        when a reflection fires, and at the day-rollover checkpoint. It used to
        do INSERT + SELECT + COMMIT on EVERY memory write, inside the world's
        only lock — a per-tick db round-trip to maintain a counter that costs
        nothing to lose (a dropped watermark just delays one reflection).

        Reflections themselves don't accumulate — an insight spawning more
        insights is a storm, not thinking.
        """
        if (
            kind == self.REFLECTION_KIND
            or self.reflector is None
            or self._judge_pool is None
            or self.event_log is None
        ):
            return
        threshold = 3.0
        if self.config_store is not None:
            threshold = float(
                self.config_store.get("memory.reflection_threshold", default=threshold)
            )
        if agent_id not in self._reflection_watermark:
            row = self.event_log.conn.execute(
                "SELECT accumulated_importance FROM reflection_state WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            self._reflection_watermark[agent_id] = float(row[0]) if row else 0.0
        total = self._reflection_watermark[agent_id] + max(0.0, importance)
        if total < threshold:
            self._reflection_watermark[agent_id] = total
            self._reflection_dirty.add(agent_id)
            return
        self._reflection_watermark[agent_id] = 0.0
        self._reflection_dirty.discard(agent_id)
        conn = self.event_log.conn
        conn.execute(
            "INSERT INTO reflection_state (agent_id, accumulated_importance, last_reflection_tick)"
            " VALUES (?, 0, ?) ON CONFLICT(agent_id) DO UPDATE SET"
            " accumulated_importance = 0, last_reflection_tick = excluded.last_reflection_tick",
            (agent_id, self.clock),
        )
        conn.commit()
        self._submit_reflection(agent_id)

    def _persist_clock(self) -> None:
        """Checkpoint the world clock into `db_meta` (lock held).

        The clock cannot be recovered from the event log alone: a stretch of
        ticks where nobody does anything leaves no trace, so restoring from
        max(event ts) silently rewinds the world to its last eventful moment.
        The deficit is permanent — the world never catches back up — and it is
        the common case, since agents are idle for most of the night.

        This is the `agent_needs` pattern, and the same architectural criterion:
        "发生了一件事" belongs in the event log, "现在是多少" belongs in a
        data-plane row. Best-effort — a failed checkpoint costs at most the
        quiet tail, which is what the old behaviour lost on every close.
        """
        if self.event_log is None:
            return
        try:
            conn = self.event_log.conn
            conn.execute(
                "INSERT INTO db_meta (key, value) VALUES ('clock', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(int(self.clock)),),
            )
            conn.commit()
        except Exception:  # noqa: BLE001 - a clock checkpoint is never fatal
            logger.warning("world clock checkpoint failed", exc_info=True)

    def _restore_clock(self) -> None:
        """Adopt the checkpointed clock if it is ahead of the replayed events.

        `max()` rather than a plain read: a database written by an older build
        has no `clock` row, and one killed mid-run may have a stale one, but
        events are always at least as new as the last checkpoint.
        """
        if self.event_log is None:
            return
        try:
            row = self.event_log.conn.execute(
                "SELECT value FROM db_meta WHERE key = 'clock'"
            ).fetchone()
        except Exception:  # noqa: BLE001 - fall back to the event-derived clock
            return
        if row is None:
            return
        try:
            self.clock = max(self.clock, int(row[0]))
        except (TypeError, ValueError):
            logger.warning("db_meta.clock is not an integer (%r); ignoring", row[0])

    def _persist_reflection_watermarks(self) -> None:
        """Checkpoint the in-memory watermarks (lock held). Best-effort: losing
        one only means a reflection arrives a little later."""
        if self.event_log is None or not self._reflection_dirty:
            return
        try:
            conn = self.event_log.conn
            for agent_id in self._reflection_dirty:
                conn.execute(
                    "INSERT INTO reflection_state (agent_id, accumulated_importance)"
                    " VALUES (?, ?) ON CONFLICT(agent_id) DO UPDATE SET"
                    " accumulated_importance = excluded.accumulated_importance",
                    (agent_id, float(self._reflection_watermark.get(agent_id, 0.0))),
                )
            conn.commit()
            self._reflection_dirty.clear()
        except Exception:  # noqa: BLE001 - a watermark checkpoint is never fatal
            logger.warning("reflection watermark checkpoint failed", exc_info=True)

    def checkpoint(self) -> None:
        """Flush every lazy checkpoint (needs / reflection watermarks / clock)
        so the db is complete as of NOW, without stopping the world.

        The trio is deliberately lazy on the tick path (day rollover /
        shutdown) — but an interaction moment is the opposite trade: a player
        just touched the world, and "the db is whole the instant you spoke"
        is what live export and crash recovery lean on. Cost: one single-row
        commit per store. Idempotent; each _persist_* is already never-fatal.
        """
        with self._lock:
            self._persist_all_needs()
            self._persist_reflection_watermarks()
            self._persist_clock()

    def _submit_reflection(self, agent_id: str) -> None:
        """Snapshot context under the lock, reflect on the judge pool."""
        brain = self.agents.get(agent_id)
        if brain is None or self.memory_store is None:
            return
        recent = self.memory_store.query(agent_id=agent_id)[:10]
        context = {
            "name": brain.agent.name,
            "personality": brain.agent.blackboard.read("personality") or "",
            "memories": [(int(m["id"]), str(m["summary"])) for m in recent],
        }
        pool = self._judge_pool
        if pool is None or self._stopped:
            return
        pool.submit(self._reflection_worker, agent_id, context)

    def _reflection_worker(self, agent_id: str, context: dict[str, Any]) -> None:
        """Pool thread: LLM synthesizes insights; each lands as an ordinary
        memory_seed event (kind='reflection') — replayable history, and the
        strict path: the reflector proposes, the event log records."""
        try:
            insights = self.reflector(
                context["name"], context["personality"],
                [summary for _, summary in context["memories"]],
            )
        except Exception:  # noqa: BLE001 - a dead reflector must not stop the world
            logger.warning("reflection failed for %s", agent_id, exc_info=True)
            return
        source_ids = [mid for mid, _ in context["memories"]]
        for insight in list(insights or [])[:3]:
            text = str(insight).strip()
            if not text:
                continue
            with self._lock:
                if self._stopped:
                    return
                self._record_event({
                    "type": "memory_seed",
                    "who": agent_id,
                    "payload": {
                        "agent_id": agent_id,
                        "kind": self.REFLECTION_KIND,
                        "summary": text,
                        "importance": 0.8,
                        "source_ids": source_ids,
                    },
                })

    def _on_relation_shift(self, event: dict[str, Any], descriptor: Any) -> None:
        """relationship-stage-machine: a relation_shift now feeds two things —
        a sign-aware graph edge, and (for judge-driven deltas) an async
        r_type relabel. Called from _apply_memory_trigger with the lock held,
        BEFORE the event folds into _memory_projection, so the projection
        still reads as "state before"."""
        payload = event.get("payload") or {}
        agent_id = descriptor.agent_id
        target_id = payload.get("target")
        if not target_id:
            return
        new_value = self._relation_value_after(payload, agent_id, target_id)
        if self.knowledge_graph is not None and new_value is not None:
            # Sign-aware (stage-machine D3): the old sign-blind version minted
            # a "friendship" edge for a plunge into enmity. Settling into the
            # neutral middle band builds no structure at all.
            predicate = None
            if new_value >= 0.2:
                predicate = "friendship"
            elif new_value <= -0.2:
                predicate = "rivalry"
            if predicate is not None:
                self.knowledge_graph.add(
                    f"agent:{agent_id}", predicate, f"agent:{target_id}",
                    source_event_seq=descriptor.event_seq,
                )
                self.knowledge_graph.add(
                    f"agent:{target_id}", predicate, f"agent:{agent_id}",
                    source_event_seq=descriptor.event_seq,
                )
        if (
            payload.get("kind") == "sentiment_delta"
            and self._judge_pool is not None
            and new_value is not None
        ):
            self._submit_relabel(agent_id, target_id, new_value)

    def _relation_value_after(
        self, payload: dict[str, Any], agent_id: str, target_id: str
    ) -> float | None:
        """The sentiment value this state_change lands on: absolute events
        carry it; delta events derive it from the pre-fold projection."""
        try:
            if payload.get("kind") == "sentiment":
                return float(payload["sentiment"])
            if payload.get("kind") == "sentiment_delta":
                rel = self._memory_projection.relations.get((agent_id, target_id))
                old = rel.sentiment if rel is not None else 0.0
                return max(-1.0, min(1.0, old + float(payload["delta"])))
        except (KeyError, TypeError, ValueError):
            return None
        return None

    def _submit_relabel(self, agent_id: str, target_id: str, new_value: float) -> None:
        """Snapshot relabel context under the lock, hand it to the judge pool.
        The label is flavor on top of the crossing — memory and edge are
        already recorded synchronously; a failed relabel keeps the old text."""
        from anima_world.memory_triggers import BAND_NAMES, band

        rel = self._memory_projection.relations.get((agent_id, target_id))
        old_sentiment = rel.sentiment if rel is not None else 0.0

        def persona(aid: str) -> dict[str, Any]:
            brain = self.agents.get(aid)
            if brain is None:
                return {"name": aid, "personality": ""}
            return {
                "name": brain.agent.name,
                "personality": brain.agent.blackboard.read("personality") or "",
            }

        memories: list[str] = []
        if self.memory_store is not None:
            try:
                memories = [m["summary"] for m in self.memory_store.query(agent_id=agent_id)[:3]]
            except Exception:  # noqa: BLE001 - memories are flavor here, never fatal
                memories = []
        context = {
            "old_r_type": rel.r_type if rel is not None else "acquaintance",
            "old_band": BAND_NAMES[band(old_sentiment)],
            "new_band": BAND_NAMES[band(new_value)],
            "a": persona(agent_id),
            "b": persona(target_id),
            "memories": memories,
        }
        self._judge_pool.submit(self._relabel_worker, agent_id, target_id, context)

    def _relabel_worker(self, agent_id: str, target_id: str, context: dict[str, Any]) -> None:
        """Worker body: LLM relabel → one r_type event. Never on the tick
        thread; any failure keeps the old label (worst case = the pre-change
        world, where r_type never moved at all)."""
        judge = self.relationship_judge
        if judge is None:
            return
        try:
            label = judge.relabel(**context)
        except Exception:  # noqa: BLE001 - a dead judge must not stop the world
            logger.warning("r_type relabel failed for %s→%s", agent_id, target_id, exc_info=True)
            return
        if not label:
            return
        with self._lock:
            if self._stopped:
                return
            self._record_and_deliver({
                "type": "state_change",
                "who": agent_id,
                "payload": {"kind": "r_type", "as": agent_id, "target": target_id, "r_type": label},
            })

    @staticmethod
    def _event_log_payload(event: dict[str, Any]) -> dict[str, Any]:
        payload = dict(event.get("payload", {}) or {})
        if event.get("type") == "narrative" and "text" in event and "text" not in payload:
            payload["text"] = event["text"]
        elif event.get("type") == "player_action":
            # 玩家动作的内容全在事件顶层(api.py:801-808),而只有 `payload` 落库
            # —— 于是玩家在世界里做过的每一件事都存成了 `{}`。**复制而不是搬走**:
            # 顶层形状是实时流的既有契约(REFERENCE §2.1 / test_api.py),动它等于
            # 破坏宿主。老库里的 `{}` 不补也不迁移,只对此后的事件成立。
            for key in ("player_id", "role", "action", "details"):
                if key in event and key not in payload:
                    payload[key] = event[key]
        return {
            "ts": int(event.get("ts", 0) or 0),
            "type": str(event.get("type", "")),
            "who": event.get("who"),
            "loc": event.get("loc"),
            "payload": payload,
        }

    def load_persisted_events(self, events: list[Event]) -> None:
        """Replay persisted events into the web/SSE recent event buffer."""
        with self._lock:
            for persisted in events:
                event = self._stream_event(
                    {
                        "seq": persisted.seq,
                        "ts": persisted.ts,
                        "type": persisted.type,
                        "who": persisted.who,
                        "loc": persisted.loc,
                        "payload": persisted.payload,
                    }
                )
                self.recent_events.append(event)
                self._next_event_seq = max(self._next_event_seq, persisted.seq + 1)
                ts = int(persisted.ts)
                if ts < _WALL_CLOCK_FLOOR:
                    self.clock = max(self.clock, ts)
            # Events only pin the clock to the last eventful tick; the quiet
            # tail after it lives in `db_meta` (see `_persist_clock`).
            self._restore_clock()

    @staticmethod
    def _stream_event(event: dict[str, Any]) -> dict[str, Any]:
        """Return a web/SSE-friendly copy of a raw scheduler event."""
        stream = dict(event)
        payload = dict(stream.get("payload", {}) or {})
        stream["payload"] = payload

        if stream.get("type") == "state_change" and payload.get("kind") == "location_join":
            stream.update(
                {
                    "type": "agent_action",
                    "action": "walk",
                    "to": payload.get("location"),
                }
            )
        elif stream.get("type") == "narrative":
            stream["text"] = payload.get("text", stream.get("text", ""))
            if "speaker" in payload:
                stream["speaker"] = payload["speaker"]
        elif stream.get("type") == "agent_action":
            stream.setdefault("action", payload.get("action"))
        elif stream.get("type") == "player_action":
            # 重放要还原实时流的顶层形状。**四个键都要**:只回填 `action` 就是
            # 把"内容没落盘"这个 bug 换个地方犯,而现有断言(只查 player_id)照绿。
            # 老库里的 player_action payload 是 `{}`,回填不出东西 —— 那些事件的
            # 顶层字段本来就没存过,不无中生有。
            for key in ("player_id", "role", "action", "details"):
                if key in payload:
                    stream.setdefault(key, payload[key])

        return stream

    # ── Tick loop ─────────────────────────────────────────────────────────────

    def tick(self) -> None:
        """Advance world clock by tick_delta and process one frame."""
        with self._lock:
            if self._stopped:
                return

            prev_day = self.world_time().day
            self.clock += self.tick_delta
            if self.world_time().day != prev_day:
                self._on_day_rollover()

            # 1. Drain pending events (process, then trigger brains)
            pending = list(self._queue)
            self._queue.clear()

            for event in pending:
                self._deliver(event)

            # 2. Arrivals first: an agent whose journey ends this tick is put
            #    down at its destination before the tree decides anything, so it
            #    can act on actually being there.
            self._land_arrivals()

            now = self.world_time()

            # 3. Authored beats fire BEFORE any tree runs this tick, so every
            #    decision below already sees the injected state (the same
            #    "derived state must not lag the events" rule as arrivals).
            self._check_beats(now)

            # 4. World clock → every agent's blackboard, then run its BT.
            #    bt-duties D1: the tree is driven by TIME, not by boredom. The
            #    old loop only reached the BT through the idle watchdog, so a
            #    duty that starts at 08:00 could never fire.
            needs_enabled = self._needs_enabled()
            for brain in list(self.agents.values()):
                bb = brain.agent.blackboard
                bb.write("time.day", now.day)
                bb.write("time.hour", now.hour)
                bb.write("time.minute", now.minute)
                bb.write("time.minute_of_day", now.minute_of_day)
                if needs_enabled:
                    self._settle_agent_needs(brain)

                mailbox = bb.read("mailbox") or []
                if mailbox:
                    bb.write("mailbox", [])
                    brain.tick(mailbox)  # applies events to the blackboard

                # Free time: hand the tree the step the planner scheduled for
                # right now. The duty branches sit above the `follow_plan` leaf,
                # so a duty in its window always wins; with no plan the leaf
                # fails and the tree falls through to idle_wander.
                self._write_plan_step(brain.agent, now)
                self._request_replan_if_needed(brain.agent.id, now)

                action = brain.tick_direct()
                self._emit_on_transition(brain.agent, action)

            # 5. Idle watchdog (inject idle events to dormant agents)
            self._idle_watchdog()

    def _on_day_rollover(self) -> None:
        """World-day boundary housekeeping (called with the lock held).

        Everything here must be pure arithmetic/SQL — the same no-LLM rule as
        the rest of the tick frame. Per-day cost, not per-tick."""
        now_tick = self.clock
        if self.memory_store is not None and hasattr(self.memory_store, "decay_pass"):
            ticks_per_day = max(1, 1440 // self._minutes_per_tick())
            for agent_id in list(self.agents):
                try:
                    self.memory_store.decay_pass(agent_id, now_tick, ticks_per_day)
                except Exception:  # noqa: BLE001 - forgetting is best-effort
                    logger.warning("memory decay pass failed for %s", agent_id, exc_info=True)
        self._persist_all_needs()
        self._persist_reflection_watermarks()
        self._settle_economy_day()
        self._gossip_rolled.clear()
        if self._social_enabled() and self.knowledge_graph is not None and self.event_log is not None:
            from anima_world import cliques as cliques_mod

            try:
                groups = cliques_mod.compute_cliques(self.knowledge_graph.query())
                cliques_mod.store_cliques(self.event_log.conn, groups, now_tick)
            except Exception:  # noqa: BLE001 - a derived cache must not stop the day
                logger.warning("clique recompute failed", exc_info=True)

    def _economy_enabled(self) -> bool:
        return bool(
            self.config_store is not None
            and self.config_store.get("economy.enabled", default=False)
        )

    def _handle_eat_purchase(self, agent: Any) -> None:
        """Lock held. Buy the cheapest meal on the local shelf if the agent
        can afford it: stock decrements in the table (data-plane), the money
        and the meal go through events (the ledger is a projection)."""
        if not self._economy_enabled() or self.event_log is None:
            return
        from anima_world import economy

        loc = agent.blackboard.read("loc") or agent.location
        if not loc:
            return
        meal = economy.cheapest_meal(self.event_log.conn, loc)
        if meal is None:
            return
        balance = self._memory_projection.balances.get(agent.id, 0.0)
        if balance < meal["price"]:
            return
        if not economy.take_stock(self.event_log.conn, loc, meal["item_id"]):
            return
        self._shop_sales[(loc, meal["item_id"])] = (
            self._shop_sales.get((loc, meal["item_id"]), 0) + 1
        )
        self._record_event({
            "type": "payment", "who": agent.id, "loc": loc,
            "payload": {"from": agent.id, "to": economy.TOWN,
                        "amount": meal["price"], "reason": f"meal:{meal['item_id']}"},
        })
        self._record_event({
            "type": "item_consume", "who": agent.id, "loc": loc,
            "payload": {"who": agent.id, "item_id": meal["item_id"], "source": f"shop:{loc}"},
        })

    def _settle_economy_day(self) -> None:
        """Day rollover (lock held): wages in, shelves restocked, prices drift."""
        if not self._economy_enabled() or self.event_log is None:
            return
        from anima_world import economy

        wage = 20.0
        if self.config_store is not None:
            wage = float(self.config_store.get("economy.daily_wage", default=wage))
        if wage > 0:
            for agent_id in list(self.agents):
                self._record_event({
                    "type": "payment", "who": agent_id,
                    "payload": {"from": economy.TOWN, "to": agent_id,
                                "amount": wage, "reason": "daily_wage"},
                })
        try:
            economy.daily_price_pass(self.event_log.conn, self._shop_sales)
        except Exception:  # noqa: BLE001 - a broken shelf must not stop the day
            logger.warning("daily price pass failed", exc_info=True)
        self._shop_sales.clear()

    def _needs_enabled(self) -> bool:
        return bool(
            self.config_store is not None
            and self.config_store.get("needs.enabled", default=False)
        )

    def _social_enabled(self) -> bool:
        return bool(
            self.config_store is not None
            and self.config_store.get("social.enabled", default=False)
        )

    def _colocated_agents(self, agent: Agent) -> list[str]:
        """Everyone standing where `agent` stands, minus itself. Same rule as
        `_is_colocated` — an agent in transit is nowhere, so it neither
        overhears nor is overheard. Sorted so the roll order is stable."""
        here = agent.blackboard.read("loc") or agent.location
        if not here or agent.id in self._transit:
            return []
        return sorted(
            other_id
            for other_id, loc in self._agent_locations().items()
            if other_id != agent.id and loc == here
        )

    def _gossip_seed(self, speaker_id: str, listener_id: str) -> int:
        """Stable across processes — `hash()` on str is salted per interpreter
        (PYTHONHASHSEED), which would make the same (tick, pair) roll
        differently on every run and cost `simulate` its reproducibility."""
        pair = zlib.crc32(f"{speaker_id}|{listener_id}".encode("utf-8"))
        return (self.clock << 16) ^ (pair & 0xFFFF)

    def _maybe_gossip(self, agent: Agent, listener_ids: Iterable[str | None]) -> None:
        """social-v5 (lock held): one dice roll per pair per day. The tick
        thread only samples; the hearsay lands as an ordinary memory_seed
        event — replayable, and the trigger pipeline stays untouched.

        `chat` names its listener; `idle_social` ("looked for someone to talk
        to") names nobody, so its listeners are whoever is standing there —
        that is how a rumor reaches someone who was merely in the room.
        """
        import random

        from anima_world import gossip as gossip_mod

        if not self._social_enabled() or self.memory_store is None:
            return
        day = self.world_time().day
        listeners = []
        for listener_id in listener_ids:
            if not listener_id or listener_id == agent.id or listener_id not in self.agents:
                continue
            key = (agent.id, listener_id, day)
            if key in self._gossip_rolled:
                continue
            self._gossip_rolled.add(key)
            listeners.append(listener_id)
        if not listeners:
            return
        try:
            # Read once, roll many: the speaker's memories are the same for
            # every listener in the room.
            memories = self.memory_store.query(agent_id=agent.id)[:10]
        except Exception:  # noqa: BLE001 - gossip is flavor, never fatal
            return
        for listener_id in listeners:
            rng = random.Random(self._gossip_seed(agent.id, listener_id))
            picked = gossip_mod.pick_gossip(rng, agent.name, memories, listener_id)
            if picked is None:
                continue
            self._record_event({
                "type": "memory_seed",
                "who": listener_id,
                "payload": picked,
            })

    def _settle_agent_needs(self, brain: BrainLike) -> None:
        """needs-v3: advance one agent's need curves by tick_delta (lock held).

        Pure arithmetic on the blackboard; the agent_needs table is only a
        checkpoint (day rollover / shutdown). First touch hydrates from it."""
        from anima_world import needs as needs_mod

        bb = brain.agent.blackboard
        agent_id = brain.agent.id
        if bb.read("need.energy") is None:
            values = (
                needs_mod.load(self.event_log.conn, agent_id)
                if self.event_log is not None
                else {n: 1.0 for n in needs_mod.NEEDS}
            )
        else:
            values = {n: bb.read(f"need.{n}") for n in needs_mod.NEEDS}
        action = self._current_action.get(agent_id)
        kind = action.kind if action else None
        settled = needs_mod.settle(values, self.tick_delta, kind)
        for key, value in settled.items():
            bb.write(f"need.{key}", value)
        # 迟滞的判据(见 NeedAction):当前动作正在补哪几条需求。写成派生值而不是
        # 一份新状态 —— 它就是 settle 刚用过的那个 kind,两处不可能对不上。
        bb.write("need._restoring", tuple(sorted(needs_mod.restores(kind))))

    def _persist_all_needs(self) -> None:
        if not self._needs_enabled() or self.event_log is None:
            return
        from anima_world import needs as needs_mod

        for agent_id, brain in self.agents.items():
            bb = brain.agent.blackboard
            if bb.read("need.energy") is None:
                continue
            values = {n: bb.read(f"need.{n}") for n in needs_mod.NEEDS}
            try:
                needs_mod.persist(self.event_log.conn, agent_id, values, self.clock)
            except Exception:  # noqa: BLE001 - a checkpoint is best-effort
                logger.warning("needs persist failed for %s", agent_id, exc_info=True)

    def _minutes_per_tick(self) -> int:
        if self.config_store is not None:
            return int(self.config_store.get("world.minutes_per_tick", default=DEFAULT_MINUTES_PER_TICK))
        return DEFAULT_MINUTES_PER_TICK

    # ── Beat director (beat-director) ─────────────────────────────────────────

    def _agent_locations(self) -> dict[str, str]:
        """Where everyone is RIGHT NOW, off the live blackboards. An agent in
        transit is nowhere (same rule as `_is_colocated`) — it can neither
        satisfy a co_located predicate nor witness a broadcast."""
        locs: dict[str, str] = {}
        for agent_id, brain in self.agents.items():
            if agent_id in self._transit:
                continue
            loc = brain.agent.blackboard.read("loc") or brain.agent.location
            if loc:
                locs[agent_id] = loc
        return locs

    def _check_beats(self, now: WorldTime) -> None:
        """Fire every due beat. Called from tick() with self._lock held.
        Short-circuits once the whole script has fired, so an exhausted
        script costs nothing on the tick path."""
        if self.beat_director is None or not self.beat_director.has_pending():
            return
        agent_locs = self._agent_locations()
        for beat in self.beat_director.due_beats(now, self._memory_projection, agent_locs):
            self._fire_beat(beat, now)

    def _fire_beat(self, beat: dict[str, Any], now: WorldTime) -> None:
        """Expand one beat's payload and record it, `beat_fired` first.

        Pure data expansion — no LLM call ever happens here (the text is
        authored), so holding the tick lock is fine. A failing op is skipped
        with a warning; the beat is marked fired regardless, so a broken op
        can never wedge the script (D4).
        """
        beat_id = beat["id"]
        self.beat_director.mark_fired(beat_id)
        events: list[dict[str, Any]] = []
        ops_applied: list[str] = []
        for op in beat.get("payload", []):
            kind = op.get("op")
            try:
                expanded = self._expand_beat_op(op)
            except Exception:  # noqa: BLE001 - one bad op must not stop the world
                logger.warning("beat %r op %r failed — skipping the op", beat_id, kind, exc_info=True)
                continue
            if expanded is None:
                continue
            events.extend(expanded)
            ops_applied.append(kind)
        self._record_event({
            "type": "beat_fired",
            "payload": {
                "beat_id": beat_id,
                "day": now.day,
                "minute_of_day": now.minute_of_day,
                "ops_applied": ops_applied,
            },
        })
        for ev in events:
            self._record_and_deliver(ev)
        logger.info("beat %r fired (day %d, %02d:%02d): %s",
                    beat_id, now.day, now.hour, now.minute, ops_applied)

    def _expand_beat_op(self, op: dict[str, Any]) -> list[dict[str, Any]] | None:
        """One op → raw events (plus side effects for the two special ops).
        None ⇒ the op was skipped and must not appear in ops_applied.

        Locations are read fresh PER OP, not snapshotted per tick: an
        agent_join earlier in this same tick — even earlier in this same
        beat — must be visible to a broadcast_memory right after it.
        """
        kind = op.get("op")
        if kind == "agent_join":
            return self._beat_agent_join(op)
        if kind == "agent_leave":
            return self._beat_agent_leave(op)
        if kind == "agent_return":
            return self._beat_agent_return(op)
        if kind == "location_desc":
            # config path (nested-map D7): writes the locations table, no event.
            self.update_location_description(str(op.get("location")), str(op.get("description", "")))
            return []
        # World-known, not merely registered: an away agent (agent_leave) still
        # forms memories and its relations still shift — the guard exists to
        # catch authoring typos, and the projection knows every real agent.
        known = set(self.agents) | set(self._memory_projection.agents)
        events = expand_event_op(op, agent_locs=self._agent_locations(), known_agents=known)
        if not events and kind != "broadcast_memory":
            return None  # skipped (unknown agent) — broadcast may legitimately be empty
        if kind == "persona_update":
            self._apply_spec_to_blackboard(op.get("agent_id"), (op.get("spec") or {}))
        return events

    def _apply_spec_to_blackboard(self, agent_id: str | None, spec: dict[str, Any]) -> None:
        """Live-apply a persona/goals edit so the planner and judge see it this
        tick — the persona_update event covers replay, the blackboard covers
        the running world (same pairing as update_agent_persona)."""
        brain = self.agents.get(agent_id) if agent_id else None
        if brain is None:
            return
        if "personality" in spec:
            brain.agent.blackboard.write("personality", str(spec["personality"]))
        if "goals" in spec:
            brain.agent.blackboard.write("goals", coerce_goals(spec["goals"]))

    def _beat_agent_join(self, op: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Mid-run character entry: register a live Brain via the injected
        factory, then emit the same event bundle genesis seeding would have
        (agent_join + persona goals + symmetric relations + memories)."""
        bundle = dict(op.get("agent") or {})
        agent_id = bundle.get("id")
        if not agent_id:
            logger.warning("beat agent_join has no agent.id — skipping")
            return None
        if agent_id in self.agents:
            logger.warning("beat agent_join: agent %r already registered — skipping", agent_id)
            return None
        if self._beat_agent_factory is None:
            logger.warning("beat agent_join for %r needs an agent factory; none wired — skipping", agent_id)
            return None

        # Build the FULL event bundle before registering: if anything here
        # raises (or register hits the agent cap), the per-op guard skips the
        # whole op consistently — no live agent without an agent_join event,
        # which would silently vanish on restart (the scan finds nothing).
        events: list[dict[str, Any]] = [{
            "type": "agent_join",
            "who": agent_id,
            "loc": bundle.get("location"),
            "payload": {
                "spec": {"name": bundle.get("name", agent_id),
                         "personality": bundle.get("personality", "")},
                "state": {},
                "location": bundle.get("location"),
            },
        }]
        goals = coerce_goals(bundle.get("goals"))
        if goals:
            events.append({
                "type": "state_change",
                "who": agent_id,
                "payload": {"kind": "persona_update", "spec": {"goals": goals}},
            })
        # Relations both directions, genesis-style; seed-marked so the
        # TriggerEngine treats them as backfill, not a relationship swing.
        events.extend(expand_relations(agent_id, bundle.get("relations") or [], set(self.agents)))
        events.extend(
            memory_seed_event(agent_id, mem, default_kind="seed")
            for mem in bundle.get("memories") or []
        )

        self.register(self._beat_agent_factory(bundle))
        return events

    def _beat_agent_leave(self, op: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Off-stage: unregister IS the whole physical semantics — every
        presence read (BT tick, planner roster, co-location, witnesses,
        judge) goes through `self.agents`, so removal makes the agent
        invisible to the world with zero consumer changes (agent-leave-return
        D2). The event is the replayable record of who is away."""
        agent_id = op.get("agent_id")
        if agent_id not in self.agents:
            logger.warning("beat agent_leave: %r is not present — skipping", agent_id)
            return None
        self.unregister(agent_id)
        self._transit.pop(agent_id, None)       # a journey ends with its traveler
        self._plans.pop(agent_id, None)
        self._current_action.pop(agent_id, None)
        return [{"type": "agent_leave", "who": agent_id, "payload": {}}]

    def _beat_agent_return(self, op: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Back on stage: same path as beat-director's restart scan — persona
        and goals come from the projection spec (persona_update history wins),
        duties live on in the agent's bt_nodes tree, the injected factory
        rebuilds the Brain (agent-leave-return D3)."""
        agent_id = op.get("agent_id")
        location = op.get("location")
        if agent_id in self.agents:
            logger.warning("beat agent_return: %r is already present — skipping", agent_id)
            return None
        if self._beat_agent_factory is None:
            logger.warning("beat agent_return for %r needs an agent factory — skipping", agent_id)
            return None
        projected = self._memory_projection.agents.get(agent_id)
        if projected is None:
            logger.warning("beat agent_return: %r is unknown to this world — skipping", agent_id)
            return None
        spec = projected.spec
        self.register(self._beat_agent_factory({
            "id": agent_id,
            "name": spec.get("name", agent_id),
            "location": location,
            "personality": spec.get("personality", ""),
            "goals": coerce_goals(spec.get("goals")),
        }))
        return [{
            "type": "agent_return",
            "who": agent_id,
            "loc": location,
            "payload": {"location": location},
        }]

    def _land_arrivals(self) -> None:
        """Emit `location_join` for every journey that ends at or before now."""
        for agent_id, trip in list(self._transit.items()):
            if self.clock < trip["arrive_at"]:
                continue
            self._transit.pop(agent_id, None)
            brain = self.agents.get(agent_id)
            if brain is None:
                continue
            # Put them down NOW, not when they get around to reading their
            # mailbox. `loc` is applied lazily by Brain._apply_events, which
            # leaves a one-tick window where an agent has landed (no longer in
            # transit) but its blackboard still names the place it left — and
            # another agent, deciding earlier in this same loop, would see that
            # stale spot and "meet" someone who is no longer there. The event is
            # still the record of what happened; this just keeps the live state
            # from lagging it inside the tick.
            brain.agent.blackboard.write("loc", trip["to"])
            brain.agent.location = trip["to"]
            self._record_and_deliver({
                "type": "state_change",
                "who": agent_id,
                "payload": {"kind": "location_join", "location": trip["to"]},
            })

    def _record_and_deliver(self, event: dict[str, Any]) -> None:
        self._deliver(event)
        self._record_event(event)

    def _travel_minutes(self, origin: str | None, destination: str | None) -> float | None:
        """How long the walk takes, or None when it can't be measured (no map,
        unplaceable location) — the caller then teleports, as it always did."""
        if self.location_store is None or not origin or not destination:
            return None
        if origin == destination:
            return 0.0
        try:
            dist = self.location_store.distance(origin, destination)
        except Exception:  # noqa: BLE001 - a broken map must not stop the world
            logger.warning("distance lookup failed for %s → %s", origin, destination, exc_info=True)
            return None
        if dist is None:
            return None
        rate = 60.0
        if self.config_store is not None:
            rate = self.config_store.get("world.travel_minutes_per_unit", default=rate)
        return dist * float(rate)

    def world_time(self) -> WorldTime:
        """The world calendar, derived from `clock` — never stored (D3)."""
        mpt = DEFAULT_MINUTES_PER_TICK
        if self.config_store is not None:
            mpt = self.config_store.get("world.minutes_per_tick", default=mpt)
        return world_time(self.clock, int(mpt))

    def _write_plan_step(self, agent: Agent, now: WorldTime) -> None:
        """Put the current plan step on the blackboard (clearing it when the
        agent has none) so the `follow_plan` leaf can read it."""
        plan = self._plans.get(agent.id)
        step = plan.step_at(now.minute_of_day) if plan is not None and plan.day == now.day else None
        agent.blackboard.write("plan.kind", step.kind if step else None)
        agent.blackboard.write("plan.params", dict(step.params) if step else None)

    def _request_replan_if_needed(self, agent_id: str, now: WorldTime) -> None:
        """Enqueue a replan when the agent has no plan for today. Returns
        immediately — the LLM call happens on the planner pool, never here."""
        if self._planner_pool is None or agent_id in self._planning:
            return
        plan = self._plans.get(agent_id)
        if plan is not None and plan.day == now.day:
            return
        self._planning.add(agent_id)
        self._planner_pool.submit(self._make_and_install_plan, agent_id, now.day)

    def planning_in_flight(self) -> bool:
        """Read-only: is any replan still pending on the planner pool?

        sim-ff-usability: fast-forward callers (`simulate`) use this to wait
        at day boundaries so a plan lands inside the world day it was made
        for — thousands of ticks can otherwise pass during one LLM call and
        the plan is never consumed (`plan.day == now.day` gate). No side
        effects, no blocking; serve never calls it.
        """
        with self._lock:
            return bool(self._planning)

    def wait_planning_idle(self, timeout: float) -> bool:
        """Block until no replan is in flight; True=idle, False=timed out.

        Condition-based (notified when `_planning` empties) so callers pay
        only the real in-flight duration — a fast mock resolves in
        milliseconds, a real LLM in seconds — never a polling interval.
        Returns immediately when nothing is pending.
        """
        deadline = time.monotonic() + timeout
        with self._planning_idle:
            while self._planning:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._planning_idle.wait(remaining)
            return True

    def _make_and_install_plan(self, agent_id: str, day: int) -> None:
        """Worker body: build a plan off the tick thread, then install it.

        A plan is replaced WHOLESALE and recorded as one `plan` event — the
        in-memory plan is a cache of that event, never edited in place. `None`
        (LLM down, garbage output, every step invalid) leaves the agent
        planless, and the tree falls back to idle_wander: the floor is the
        world as it behaved before the planner existed.
        """
        try:
            plan = self.planner.make_plan(agent_id, day)
        except Exception:  # noqa: BLE001 - a dead planner must not stop the world
            logger.warning("planner failed for %s", agent_id, exc_info=True)
            plan = None
        # Discard + install under ONE lock acquisition (sim-ff-usability code
        # review): a gap between them let planning_in_flight() report False
        # while the plan wasn't in _plans yet — a fast-forward poller could
        # slip through and trigger a duplicate replan + wholesale overwrite.
        with self._lock:
            self._planning.discard(agent_id)
            try:
                if plan is not None and not self._stopped:
                    self._plans[agent_id] = plan
                    self._record_event(
                        {"type": "plan", "who": agent_id, "payload": plan.to_payload()}
                    )
            finally:
                if not self._planning:
                    self._planning_idle.notify_all()

    def _emit_on_transition(self, agent: Any, action: ActionDescriptor | None) -> None:
        """Emit only when the chosen action CHANGES (bt-duties D2).

        The BT re-picks the same duty on every tick while its window is open;
        emitting each time would grow the log by one event per agent per tick
        (3/s here) and fire one narrative LLM call per event. A transition is
        also the truer semantics: `walk` is a move, not a state you re-enter
        every second.
        """
        current = self._current_action.get(agent.id)
        if action is None or action == current:
            return
        # Record it as current ONLY if the world let it happen. A chat with
        # someone who isn't here, or anything at all while in transit, must stay
        # un-recorded so the tree tries again next tick — that is what turns a
        # constraint into a wait, and a wait into an encounter.
        if self.emit_action(agent, action):
            self._current_action[agent.id] = action

    @staticmethod
    def _should_run_direct(events: list[dict[str, Any]]) -> bool:
        return any(
            ev.get("type") == "user_message"
            or ev.get("payload", {}).get("kind") == "idle"
            for ev in events
        )

    def _idle_watchdog(self) -> None:
        """Detect idle agents and dispatch an idle event to their mailbox.

        Mailbox-only by design: the idle nudge is internal plumbing, not world
        history — recording it produced one dead `agent_idle` row per cycle
        (a third of the event log) with zero replay value. The action the
        nudge provokes is still recorded normally via `emit_action`.
        """
        monotonic_now = time.monotonic()
        for brain in list(self.agents.values()):
            agent = brain.agent
            last = agent.last_active_ts
            idle_timeout = (
                self.config_store.get("agent.idle_timeout", default=agent.idle_timeout)
                if self.config_store is not None
                else agent.idle_timeout
            )
            if last == 0.0 or (monotonic_now - last) >= idle_timeout:
                self._append_to_mailbox(
                    agent,
                    {"target_agent_id": agent.id, "payload": {"kind": "idle"}},
                )
                agent.mark_active()

    # ── Action emission ──────────────────────────────────────────────────────

    def emit_action(self, agent: Agent, action: ActionDescriptor) -> bool:
        """Convert an ActionDescriptor to M1 event(s) and dispatch them.

        Returns whether the action actually took effect. False means the world
        said "not yet" — the agent is mid-journey, or wants to talk to someone
        who isn't here. The caller must then NOT record it as the agent's
        current action, so the tree retries next tick and the action happens by
        itself once the world allows it.

        Autonomous behavior only — user chat is handled by the standalone M3.5
        chat subsystem, not through the scheduler.
        """
        with self._lock:
            trip = self._transit.get(agent.id)
            if trip is not None:
                # In transit is COMMITTED. Only a walk somewhere else redirects
                # you; everything else is something you do where you are, and
                # you are not there yet. Without this, a duty window closing
                # mid-journey (夏's walk_home is only 18:30–19:00, shorter than
                # the walk itself) would strand the agent on the road forever.
                if action.kind == "walk" and action.params.get("location") != trip["to"]:
                    self._transit.pop(agent.id, None)
                else:
                    return False

            if action.kind == "walk" and self._start_journey(agent, action):
                return True  # under way; `location_join` follows on arrival
            if action.kind == "eat":
                # economy-v4: paying is a side effect, eating always succeeds —
                # no stock / no money degrades to "吃随身干粮", never a stuck agent.
                self._handle_eat_purchase(agent)
            if action.kind == "chat" and not self._is_colocated(agent, action.params.get("target")):
                # Not a failure — a wait. The action is NOT recorded as current,
                # so the BT retries next tick and the chat happens by itself the
                # moment the other one walks in.
                logger.debug(
                    "%s wanted to chat %r but they are not here — waiting",
                    agent.id, action.params.get("target"),
                )
                return False

            events = to_event(action, agent_id=agent.id)
            for ev in events:
                if ev.get("who") == agent.id:
                    # Targeted to self or broadcast
                    self._deliver(ev)
                    self._record_event(ev)
                else:
                    # Targeted to another agent
                    ev["target_agent_id"] = ev.get("who")
                    self._deliver(ev)
                    self._record_event(ev)

            # Narrative for the autonomous action — generated OFF this thread
            # (bt-duties D7). The action events above are already recorded, so
            # the world advances whether or not the LLM ever answers; the
            # narrative event lands whenever it does. Snapshot the blackboard
            # here, under the lock, so the worker never reads live agent state.
            if self._narrative_pool is not None and self.narrative_history is not None:
                self._narrative_pool.submit(
                    self._generate_narrative,
                    agent.id,
                    {"kind": action.kind, "params": action.params},
                    self._blackboard_to_dict(agent.blackboard),
                )
            # llm-relationship-judge: a landed chat gets an async verdict
            # (summary + asymmetric deltas). Context is snapshotted here,
            # under the lock — the worker never reads live state.
            if action.kind == "chat" and self._judge_pool is not None:
                self._submit_chat_judgment(agent, action.params.get("target"))
            if action.kind == "chat":
                self._maybe_gossip(agent, [action.params.get("target")])
            elif action.kind == "idle_social":
                # `idle_social` carries no target (ActionTable gives it empty
                # params), so passing action.params here meant the listener was
                # always None and this whole branch never fired.
                self._maybe_gossip(agent, self._colocated_agents(agent))
            return True

    def _submit_chat_judgment(self, agent: Agent, target_id: str | None) -> None:
        """Snapshot both sides' context under the lock, then hand it to the
        judge pool. Called from emit_action (lock held)."""
        target = self.agents.get(target_id) if target_id else None
        if target is None:
            return
        pair = frozenset((agent.id, target_id))
        last = self._judged_pairs.get(pair)
        if last is not None and self.clock - last < JUDGE_PAIR_COOLDOWN_TICKS:
            return  # one conversation, one verdict — the reverse-direction chat rides it
        self._judged_pairs[pair] = self.clock

        def persona(a: Agent) -> dict[str, Any]:
            return {
                "name": a.name,
                "personality": a.blackboard.read("personality") or "",
                "goals": list(a.blackboard.read("goals") or []),
            }

        def recent(agent_id: str) -> list[str]:
            if self.memory_store is None:
                return []
            try:
                return [m["summary"] for m in self.memory_store.query(agent_id=agent_id)[:3]]
            except Exception:  # noqa: BLE001 - memories are flavor here, never fatal
                return []

        rel_ab = self._memory_projection.relations.get((agent.id, target_id))
        rel_ba = self._memory_projection.relations.get((target_id, agent.id))
        context = {
            "a": persona(agent),
            "b": persona(target.agent),
            "relation": {
                "a_to_b": rel_ab.sentiment if rel_ab else 0.0,
                "b_to_a": rel_ba.sentiment if rel_ba else 0.0,
                "r_type": rel_ab.r_type if rel_ab else "acquaintance",
                "r_type_back": rel_ab.r_type_back if rel_ab else "acquaintance",
            },
            "memories_a": recent(agent.id),
            "memories_b": recent(target_id),
            "location": agent.blackboard.read("loc") or agent.location or "",
        }
        self._judge_pool.submit(self._judge_chat_worker, agent.id, target_id, context)

    def _judge_chat_worker(self, a_id: str, b_id: str, context: dict[str, Any]) -> None:
        """Worker body: LLM verdict → delta + memory events. Never on the
        tick thread; any failure means this chat produces no relationship
        data — which beats producing wrong data (the old hardcoded absolute
        overwrite erased a seeded -0.7 enmity with one small talk)."""
        judge = self.relationship_judge
        if judge is None:
            return
        try:
            result = judge.judge(**context)
        except Exception:  # noqa: BLE001 - a dead judge must not stop the world
            logger.warning("relationship judge failed for %s↔%s", a_id, b_id, exc_info=True)
            return
        if result is None:
            return
        importance = min(0.9, 0.5 + max(abs(result.delta_a_to_b), abs(result.delta_b_to_a)))
        with self._lock:
            if self._stopped:
                return
            # Frequency damping (stage-machine D6): the judge answers "how
            # much did THIS conversation matter"; how much the Nth same-day
            # conversation still moves the world is the world's call.
            factor = self._damped_factor(a_id, b_id)
            for as_id, target_id, delta, axes in (
                (a_id, b_id, result.delta_a_to_b * factor, result.axes_a_to_b),
                (b_id, a_id, result.delta_b_to_a * factor, result.axes_b_to_a),
            ):
                if abs(delta) < 0.01:
                    continue  # damped-to-noise or a no-op delta — event-log/SSE noise
                payload = {"kind": "sentiment_delta", "as": as_id, "target": target_id, "delta": delta}
                if axes:  # relations-v5: finer axes ride the same event, same damping
                    payload["axes"] = {k: v * factor for k, v in axes.items()}
                self._record_and_deliver({
                    "type": "state_change",
                    "who": as_id,
                    "payload": payload,
                })
            for agent_id in (a_id, b_id):
                self._record_and_deliver({
                    "type": "memory_seed",
                    "who": agent_id,
                    "payload": {
                        "agent_id": agent_id,
                        "kind": "chat",
                        "summary": result.summary,
                        "importance": importance,
                        "anchor": False,
                    },
                })

    def _damped_factor(self, a_id: str, b_id: str) -> float:
        """Same-day repeat weight for one pair (0.5^(N-1)); call with the
        lock held. Shared by the NPC chat judge and the player-conversation
        judge — a player id is just another pair endpoint."""
        day = self.world_time().day
        pair = frozenset((a_id, b_id))
        last_day, count = self._judge_day_counts.get(pair, (day, 0))
        if last_day != day:
            count = 0
        self._judge_day_counts[pair] = (day, count + 1)
        return 0.5 ** count

    def submit_user_chat_judgment(
        self, agent_id: str, player_id: str, player_name: str | None, transcript: list[dict[str, Any]]
    ) -> None:
        """player-visitor: a closed PLAYER conversation gets a verdict from
        the real transcript. Deltas only (the `conversation` event already
        mints the agent's memory — D3); both directions agent↔player, so the
        player rides the whole relationship machinery (bands/edges/relabel)
        with zero changes there. Never blocks: snapshot under the lock,
        judge on the pool."""
        if self.relationship_judge is None or self._judge_pool is None:
            return
        with self._lock:
            brain = self.agents.get(agent_id)
            if brain is None:
                return
            rel_ab = self._memory_projection.relations.get((agent_id, player_id))
            rel_ba = self._memory_projection.relations.get((player_id, agent_id))
            context = {
                "a": {
                    "name": brain.agent.name,
                    "personality": brain.agent.blackboard.read("personality") or "",
                },
                "player_name": player_name or player_id,
                "relation": {
                    "a_to_b": rel_ab.sentiment if rel_ab else 0.0,
                    "b_to_a": rel_ba.sentiment if rel_ba else 0.0,
                    "r_type": rel_ab.r_type if rel_ab else "初次见面的访客",
                },
                "transcript": "\n".join(
                    f"{m.get('role', '?')}: {m.get('content', '')}" for m in transcript[-12:]
                ),
                "location": brain.agent.blackboard.read("loc") or brain.agent.location or "",
            }
            # Submit under the lock: stop() nulls the pool refs while holding
            # it, so here the pool is either alive or None — re-read it.
            pool = self._judge_pool
            if pool is None or self._stopped:
                return
            pool.submit(self._user_judge_worker, agent_id, player_id, context)

    def _user_judge_worker(self, agent_id: str, player_id: str, context: dict[str, Any]) -> None:
        judge = self.relationship_judge
        if judge is None:
            return
        try:
            result = judge.judge_user(**context)
        except Exception:  # noqa: BLE001 - a dead judge must not stop the world
            logger.warning("user-chat judge failed for %s↔%s", agent_id, player_id, exc_info=True)
            return
        if result is None:
            return
        with self._lock:
            if self._stopped:
                return
            factor = self._damped_factor(agent_id, player_id)
            for as_id, target_id, delta, axes in (
                (agent_id, player_id, result.delta_a_to_b * factor, result.axes_a_to_b),
                (player_id, agent_id, result.delta_b_to_a * factor, result.axes_b_to_a),
            ):
                if abs(delta) < 0.01:
                    continue
                payload = {"kind": "sentiment_delta", "as": as_id, "target": target_id, "delta": delta}
                if axes:
                    payload["axes"] = {k: v * factor for k, v in axes.items()}
                self._record_and_deliver({
                    "type": "state_change",
                    "who": as_id,
                    "payload": payload,
                })

    def _start_journey(self, agent: Agent, action: ActionDescriptor) -> bool:
        """Begin a walk that takes time. False ⇒ caller should emit the old
        instant `location_join` (no map, or the two ends can't be measured)."""
        destination = action.params.get("location")
        origin = agent.blackboard.read("loc") or agent.location
        minutes = self._travel_minutes(origin, destination)
        if minutes is None:
            return False
        if minutes <= 0:
            self._transit.pop(agent.id, None)
            return False  # already there — land immediately

        mpt = DEFAULT_MINUTES_PER_TICK
        if self.config_store is not None:
            mpt = self.config_store.get("world.minutes_per_tick", default=mpt)
        ticks = max(1, math.ceil(minutes / max(1, int(mpt))))
        self._transit[agent.id] = {
            "from": origin, "to": destination, "arrive_at": self.clock + ticks,
        }
        self._record_and_deliver({
            "type": "travel",
            "who": agent.id,
            "payload": {
                "from": origin,
                "to": destination,
                "minutes": round(minutes, 1),
                "arrive_at": self.clock + ticks,
            },
        })
        return True

    def _is_colocated(self, agent: Agent, target: str | None) -> bool:
        """Two agents can only talk when they are in the same place.

        Without this, the log happily recorded 遥 (in the workshop) chatting 柔
        (at home) — and every relationship edge M4 derived from those chats was
        a fiction. An agent in transit is nowhere, so it cannot talk either.
        """
        if not target:
            return False
        other = self.agents.get(target)
        if other is None:
            return False
        if agent.id in self._transit or target in self._transit:
            return False
        here = agent.blackboard.read("loc") or agent.location
        there = other.agent.blackboard.read("loc") or other.agent.location
        return bool(here) and here == there

    def _generate_narrative(
        self, agent_id: str, action: dict[str, Any], blackboard: dict[str, Any]
    ) -> None:
        """Worker body: call the (possibly slow) provider, then record the event.

        Runs on the narrative pool, never on the tick thread. Any provider
        failure is swallowed — losing a line of flavor text must never take
        down the world.
        """
        provider = self.narrative_provider  # may be swapped out while we queue
        if provider is None:
            return
        try:
            text = provider.describe(action=action, agent_id=agent_id, blackboard=blackboard)
        except Exception:  # noqa: BLE001 - flavor text is never worth a crash
            logger.warning("narrative provider failed for %s", agent_id, exc_info=True)
            return
        with self._lock:
            if self._stopped or self.narrative_history is None:
                return
            self.narrative_history.append(text)
            narrative_ev = {
                "target_agent_id": agent_id,
                "who": agent_id,
                "type": "narrative",
                "payload": {"text": text, "speaker": agent_id},
            }
            self._deliver(narrative_ev)
            self._record_event(narrative_ev)

    @staticmethod
    def _blackboard_to_dict(blackboard: Blackboard) -> dict[str, Any]:
        """Snapshot blackboard for narrative context.

        A real copy, not a live reference (prompt-grounding code review #3):
        the narrative worker reads this seconds later off-thread, and `loc`
        mutates during simulation — a shared dict would let a slow LLM
        describe an action in the place the agent moved to AFTERWARD, plus
        an unlocked cross-thread read. The call site holds the scheduler
        lock, so this copy is consistent.
        """
        return {"location": blackboard.read("loc"), "raw": dict(blackboard._data)}

    # ── World seed editing (M6) ─────────────────────────────────────────────

    def update_agent_persona(self, agent_id: str, personality: str) -> None:
        """Atomically apply + persist a persona edit (design.md D4/D8 — no
        UI caller in this change; call directly against a running
        process). Raises KeyError for an unknown agent id.

        This still goes through the event log, unlike its sibling
        `update_location_description`, and the split is deliberate (nested-map
        D7): a persona is part of an *agent's state* — it is what the agent was
        when it acted, so replay must reconstruct it — whereas a location's
        description is *map configuration*, authored in an editor and never
        touched by the simulation. State is history; the map is scenery.
        """
        with self._lock:
            if agent_id not in self.agents:
                raise KeyError(f"unknown agent: {agent_id}")
            # One live-apply path for persona_update semantics (shared with
            # the beat director) — the projection merge covers replay, the
            # blackboard write covers the running world.
            self._apply_spec_to_blackboard(agent_id, {"personality": personality})
            self._record_event(
                {
                    "type": "state_change",
                    "who": agent_id,
                    "payload": {"kind": "persona_update", "spec": {"personality": personality}},
                }
            )

    def update_location_description(self, loc_id: str, description: str) -> None:
        """Apply a location description edit to the `locations` table.

        nested-map D7: the map is configuration, not history — no simulation
        logic ever changes a location's description, so this records no event
        and the table is the only source of truth. Raises KeyError for an
        unknown location id, RuntimeError when no `location_store` is wired
        (there is no event-only fallback to degrade to). The live internal
        projection is kept in step for the store-less read paths that still
        surface descriptions from it.
        """
        with self._lock:
            if self.location_store is None:
                raise RuntimeError("no location_store wired: the map lives in the DB (D7)")
            if self.location_store.get(loc_id) is None:
                raise KeyError(f"unknown location: {loc_id}")
            self.location_store.upsert(loc_id, description=description)
            location = self._memory_projection.locations.get(loc_id)
            if location is not None:
                location.description = description

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def stop(self, wait: bool = False) -> None:
        """Drain remaining queue then halt.

        wait=True (novel-benchmark-loop) lets in-flight AND already-queued
        narrative/planner tasks finish and record their events before the
        connection is torn down — used by `simulate`'s clean exit, where a
        cancelled future would silently drop an LLM result. wait=False
        (default) keeps serve's original fast-shutdown behavior: anything
        not yet started is cancelled.
        """
        # Null the references under the lock so a request thread mid-submit
        # (all submits happen while holding it) either sees a live pool or
        # None — never a pool being shut down. The shutdowns themselves must
        # run OUTSIDE the lock: with wait=True the workers need it to finish.
        pools = []
        with self._lock:
            for attr in ("_narrative_pool", "_planner_pool", "_judge_pool"):
                pool = getattr(self, attr, None)
                setattr(self, attr, None)
                if pool is not None:
                    pools.append(pool)
        for pool in pools:
            pool.shutdown(wait=wait, cancel_futures=not wait)
        with self._lock:
            if self._stopped:
                return
            # Drain pending
            while self._queue:
                event = self._queue.popleft()
                self._deliver(event)
            self._persist_all_needs()  # needs-v3: checkpoint curves at shutdown
            self._persist_reflection_watermarks()  # memory-2.0: same reason
            self._persist_clock()  # the quiet tail leaves no event to restore from
            self._stopped = True
