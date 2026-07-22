"""FastAPI web server for the anima_world.

An API, not a site: REST + SSE for observing and driving a running world.
Three audiences, three prefixes — `/api/*` for local inspection and settings,
`/internal/v1/*` for the site (service credential + membership claim), and
`/api/admin/v1/*` for the operator. No HTML is served from here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import queue
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.middleware.cors import CORSMiddleware

from anima_world.chat_service import ChatService
from anima_world.chat_session import ChatSessionManager
from anima_world.chat_store import ChatStore
from anima_world.config_store import mask_secret
from anima_world.db import open_db
from anima_world.llm_client import create_llm_client_from_config, create_llm_client_from_env
from anima_world.locations import DEFAULT_POINTS
from anima_world.narrative import MockNarrativeProvider, OpenAICompatibleNarrativeProvider
from anima_world.prompt_store import PromptRenderError
from anima_world.projection import project_events
from anima_world.world.auth import (
    MembershipClaim,
    MembershipClaimError,
    service_credential_matches,
    verify_membership_claim,
)
from anima_world.scheduler import MAX_TICKS_PER_SECOND, Scheduler
from anima_world.snapshot import create_snapshot, load_latest_snapshot, load_snapshot, save_snapshot
from anima_world.types import AgentState, Projection
from anima_world.world_time import DEFAULT_MINUTES_PER_TICK

# The idle reaper scans for stale conversations this often (wall seconds).
_REAP_INTERVAL = 30.0

# Map row fields sent to the frontend, which assembles the tree from them.
_LOCATION_KEYS = ("id", "name", "description", "kind", "parent", "x", "y", "w", "h")


# This runtime serves NO HTML, and owns no authoring code. Every UI belongs to
# somebody else: the admin console to the operator tool, the player experience
# to the site (which reaches worlds through /internal/v1), and world authoring
# to the studio — a separate program that manages engine versions, which it
# could not do if it lived inside one of them.


def _resolve_tick_rate(fallback: float, config_store: Any | None) -> float:
    """M5 §5: live tick rate — `scheduler.tick_rate` from `config_store` when
    wired, else the `tick_rate` the app was created with (no restart needed
    for an admin's `/api/config` edit to change the next loop interval)."""
    if config_store is None:
        return fallback
    return config_store.get("scheduler.tick_rate", default=fallback)


# ── Serve world wrapper ────────────────────────────────────────────────────


class _ServeWorld:
    """Wraps a running Scheduler with an up-to-date Projection and SSE fan-out.

    The background thread calls `tick()` on the scheduler; we observe
    scheduler.narrative_history + recent_events to update our projection
    and push to SSE subscribers.
    """

    def __init__(self, scheduler: Scheduler) -> None:
        self.scheduler = scheduler
        self._recovered_from_persistence = scheduler.event_log is not None
        self.projection = self._recover_projection()
        self._projection_lock = threading.Lock()
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        self._subscribers_lock = threading.Lock()
        self._last_narrative_seq = (
            self._latest_recovered_narrative_seq()
            if self._recovered_from_persistence
            else 0
        )
        self._last_fanout_seq = 0

        # Seed missing agents from scheduler, and hydrate runtime from recovered projection.
        for aid, brain in scheduler.agents.items():
            agent = brain.agent
            recovered = self.projection.agents.get(aid)
            if recovered is not None:
                if recovered.location:
                    agent.location = recovered.location
                    agent.blackboard.write("loc", recovered.location)
                for key, value in recovered.state.items():
                    agent.blackboard.write(f"state.{key}", value)
                continue
            self.projection.agents[aid] = AgentState(
                spec={"name": agent.name},
                state={},
                location=agent.location,
                joined_at=0,
                updated_at=0,
            )

    def _recover_projection(self) -> Projection:
        if self.scheduler.event_log is None:
            return Projection()
        conn = self.scheduler.event_log.conn
        snap = load_latest_snapshot(conn)
        if snap is None:
            return project_events(self.scheduler.event_log.replay())
        projection = load_snapshot(snap)
        tail = self.scheduler.event_log.replay(since_seq=int(snap.get("seq", 0) or 0))
        return project_events(tail, base=projection)

    def _latest_recovered_narrative_seq(self) -> int:
        with self.scheduler._lock:
            return max(
                (
                    int(ev.get("seq", 0) or 0)
                    for ev in self.scheduler.recent_events
                    if ev.get("type") == "narrative"
                ),
                default=0,
            )

    def _sync_projection_from_scheduler(self) -> None:
        """Mirror scheduler history and agent locations into the web projection."""
        with self.scheduler._lock:
            self._sync_projection_locked()

    def _sync_projection_locked(self) -> None:
        with self._projection_lock:
            # Apply new narrative events from the sequenced web event buffer.
            for ev in self.scheduler.recent_events:
                seq = int(ev.get("seq", 0) or 0)
                if ev.get("type") != "narrative" or seq <= self._last_narrative_seq:
                    continue
                agent_id = ev.get("who") or ev.get("target_agent_id") or "?"
                text = ev.get("text") or ev.get("payload", {}).get("text", "")
                self.projection.narrative_log.append({
                    "agent": agent_id,
                    "speaker": agent_id,
                    "text": text,
                    "ts": ev.get("ts", self.scheduler.clock),
                })
                self._last_narrative_seq = seq

            # Update agent locations from blackboard
            for aid, brain in self.scheduler.agents.items():
                loc = brain.agent.blackboard.read("loc") or brain.agent.location
                if aid in self.projection.agents:
                    self.projection.agents[aid].location = loc
                else:
                    self.projection.agents[aid] = AgentState(
                        spec={}, state={}, location=loc, joined_at=0, updated_at=0,
                    )

    def on_tick(self) -> None:
        """Called after scheduler.tick() to update projection + fan out."""
        self._sync_projection_from_scheduler()
        # Fan out new events to SSE subscribers
        self._fan_out()

    def _fan_out(self) -> None:
        """Push new events to all SSE subscriber queues."""
        with self.scheduler._lock:
            events = [
                ev for ev in self.scheduler.recent_events
                if int(ev.get("seq", 0) or 0) > self._last_fanout_seq
            ]
        if not events:
            return
        self._last_fanout_seq = max(int(ev.get("seq", 0) or 0) for ev in events)
        with self._subscribers_lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait({"type": "batch", "events": events})
                except queue.Full:
                    pass

    def catchup_events(self, since_seq: int | None = None) -> list[dict[str, Any]]:
        """Return buffered events with seq greater than since_seq."""
        with self.scheduler._lock:
            if since_seq is None:
                return list(self.scheduler.recent_events)
            return [
                ev for ev in self.scheduler.recent_events
                if int(ev.get("seq", 0) or 0) > since_seq
            ]

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        """Register a thread-safe subscriber queue and return it."""
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=64)
        with self._subscribers_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self._subscribers_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot for /api/state."""
        with self.scheduler._lock:
            self._sync_projection_locked()
            recent_events = list(self.scheduler.recent_events)
            with self._projection_lock:
                return self._snapshot_locked(recent_events)

    def save_persistent_snapshot(self) -> dict[str, Any] | None:
        """Persist a durable M1 snapshot for the current projected world."""
        if self.scheduler.event_log is None:
            return None
        with self.scheduler._lock:
            self._sync_projection_locked()
            seq = self._latest_event_seq()
            with self._projection_lock:
                snap = create_snapshot(self.projection, seq=seq)
        save_snapshot(self.scheduler.event_log.conn, snap)
        return snap

    def _latest_event_seq(self) -> int:
        if self.scheduler.event_log is None:
            return max(
                (int(ev.get("seq", 0) or 0) for ev in self.scheduler.recent_events),
                default=0,
            )
        row = self.scheduler.event_log.conn.execute("SELECT MAX(seq) FROM events").fetchone()
        return int(row[0] or 0)

    def _agent_activity(self, agent_id: str) -> dict[str, Any]:
        """What the agent is doing right now, and which BT leaf decided it.

        This is the frontend's window onto the tree: `node_id` is the leaf that
        fired this tick, and `source` says which band of the Selector it came
        from — a duty, the LLM's free-time plan, or the idle fallback. Read-only
        derivation from live state; nothing new is stored.
        """
        brain = self.scheduler.agents.get(agent_id)
        if brain is None:
            return {}
        bb = brain.agent.blackboard
        node_id = bb.read("_selected_action_id")
        action = self.scheduler._current_action.get(agent_id)
        trip = self.scheduler._transit.get(agent_id)

        if node_id == "follow_plan":
            source = "plan"
        elif node_id in (None, "idle_wander", "idle_social"):
            source = "idle"
        else:
            source = "duty"

        activity: dict[str, Any] = {
            "node_id": node_id,
            "source": source,
            "kind": action.kind if action else None,
            "params": dict(action.params) if action else {},
        }
        if trip is not None:
            mpt = DEFAULT_MINUTES_PER_TICK
            if self.scheduler.config_store is not None:
                mpt = self.scheduler.config_store.get("world.minutes_per_tick", default=mpt)
            remaining = max(0, int(trip["arrive_at"]) - self.scheduler.clock)
            activity["transit"] = {
                "from": trip["from"],
                "to": trip["to"],
                "eta_minutes": remaining * int(mpt),
            }

        plan = self.scheduler._plans.get(agent_id)
        if plan is not None:
            now = self.scheduler.world_time()
            upcoming = [s for s in plan.steps if s.start_min > now.minute_of_day]
            if upcoming:
                nxt = upcoming[0]
                activity["next"] = {
                    "start_min": nxt.start_min,
                    "kind": nxt.kind,
                    "params": dict(nxt.params),
                    "note": nxt.note,
                }
        return activity

    def _snapshot_locked(self, recent_events: list[dict[str, Any]]) -> dict[str, Any]:
        agents = {}
        for aid, a in self.projection.agents.items():
            brain = self.scheduler.agents.get(aid)
            agents[aid] = {
                "name": brain.agent.name if brain else a.spec.get("name", aid),
                "location": a.location,
                "state": dict(a.state),
                "activity": self._agent_activity(aid),
                # player-visitor D5: a departed agent (agent_leave) stays
                # visible as history but is flagged — the projection never
                # forgets it, the scheduler registry is the presence truth.
                "away": brain is None,
            }
        # Add any agents from scheduler not yet in projection
        for aid, brain in self.scheduler.agents.items():
            if aid not in agents:
                agents[aid] = {
                    "name": aid,
                    "location": brain.agent.location or "",
                    "state": {},
                    "activity": self._agent_activity(aid),
                    "away": False,
                }

        # The map: the `locations` table is its single source of truth
        # (nested-map D7 — the map is configuration, not history). Rows go out
        # flat (id/name/description/kind/parent/x/y/w/h) and the frontend
        # assembles the tree; `DEFAULT_POINTS` is the store-less fallback
        # (headless `story`, tests). The projection contributes nothing here.
        loc_store = getattr(self.scheduler, "location_store", None)
        locations: list[dict[str, Any]] = []
        if loc_store is not None:
            try:
                locations = [
                    {k: row.get(k) for k in _LOCATION_KEYS} for row in loc_store.all()
                ]
            except Exception:  # a broken map read must not take down /api/state
                locations = []
        if not locations:
            locations = [{k: p.get(k) for k in _LOCATION_KEYS} for p in DEFAULT_POINTS]

        now = self.scheduler.world_time()
        return {
            "agents": agents,
            "world_time": {
                "day": now.day,
                "hour": now.hour,
                "minute": now.minute,
                "minute_of_day": now.minute_of_day,
                "tick": self.scheduler.clock,
            },
            "locations": locations,
            "relations": {
                f"{a}|{b}": {"sentiment": r.sentiment}
                for (a, b), r in self.projection.relations.items()
            },
            "narrative_log": list(self.projection.narrative_log)[-200:],
            "recent_events": recent_events,
            "runtime": self._runtime_status(recent_events),
        }

    def _llm_degraded_reason(self) -> str | None:
        """Why this world is answering with the Mock LLM.

        A never-configured key and an unreadable one are indistinguishable to
        every consumer (both read as "unset"), and a degraded world looks
        perfectly healthy from the outside — same tick rate, same events, just
        template text forever. This is the one place that names the cause.
        """
        config_store = self.scheduler.config_store
        if config_store is None:
            return "no config store: this world has no LLM configuration (in-memory run)"
        undecryptable = getattr(config_store, "undecryptable_secrets", None)
        if callable(undecryptable) and "llm.api_key" in undecryptable():
            return "llm.api_key could not be decrypted — did <db>.key travel with the database?"
        if not (config_store.get("llm.api_key", default="") or ""):
            return "llm.api_key is not configured"
        return None  # a key is set; the provider is mock for some other reason

    def _runtime_status(self, recent_events: list[dict[str, Any]]) -> dict[str, Any]:
        conn = self.scheduler.event_log.conn if self.scheduler.event_log is not None else None
        if conn is not None:
            event_row = conn.execute("SELECT COUNT(*), MAX(seq) FROM events").fetchone()
            snap = load_latest_snapshot(conn)
            db_status = {
                "enabled": True,
                "path": self.scheduler.db_path,
            }
            events_status = {
                "count": int(event_row[0] or 0),
                "latest_seq": int(event_row[1] or 0),
                "buffered_count": len(recent_events),
            }
            snapshot_status = {
                "available": snap is not None,
                "seq": snap.get("seq") if snap else None,
                "ts": snap.get("ts") if snap else None,
            }
        else:
            db_status = {"enabled": False, "path": None}
            events_status = {
                "count": len(recent_events),
                "latest_seq": max((int(ev.get("seq", 0) or 0) for ev in recent_events), default=0),
                "buffered_count": len(recent_events),
            }
            snapshot_status = {"available": False, "seq": None, "ts": None}

        provider = self.scheduler.narrative_provider
        if isinstance(provider, OpenAICompatibleNarrativeProvider):
            llm_status = {
                "provider": "openai-compatible",
                "mock": False,
                "model": provider.model,
                "base_url": provider.base_url,
                "degraded_reason": None,
            }
        elif isinstance(provider, MockNarrativeProvider) or provider is None:
            llm_status = {
                "provider": "mock",
                "mock": True,
                "model": None,
                "base_url": None,
                "degraded_reason": self._llm_degraded_reason(),
            }
        else:
            llm_status = {
                "provider": provider.__class__.__name__,
                "mock": False,
                "model": getattr(provider, "model", None),
                "base_url": getattr(provider, "base_url", None),
                "degraded_reason": None,
            }

        return {
            "db": db_status,
            "events": events_status,
            "llm": llm_status,
            "snapshot": snapshot_status,
        }


# ── Factory ────────────────────────────────────────────────────────────────

# Version of the /internal/v1 wire contract this runtime speaks. The v1
# surface is PUBLISHED: endpoints, claim key set and header echo are frozen —
# additive JSON fields only. A breaking change means a new /internal/v2
# served alongside, and a bump here.
INTERNAL_PROTOCOL_VERSION = 1


def create_app(
    scheduler: Scheduler,
    tick_rate: float = 1.0,
    run_loop: bool = True,
    config_store: Any | None = None,
    prompt_store: Any | None = None,
    *,
    instance_id: str = "legacy",
    world_id: str = "legacy",
    world_name: str = "",
    admin_token: str | None = None,
    shutdown_callback: Callable[[], None] | None = None,
    cors_origins: tuple[str, ...] | list[str] = (),
    platform_service_credentials: tuple[str, ...] | list[str] = (),
    membership_claim_secret: str | None = None,
    require_local_api_auth: bool = False,
) -> FastAPI:
    """Build the FastAPI app bound to `scheduler`.

    Args:
        scheduler: a populated Scheduler (agents registered).
        tick_rate: ticks per second when run_loop is enabled (fallback when
            `config_store` has no `scheduler.tick_rate` row yet).
        run_loop: start the background tick thread (disabled in tests).
        config_store: optional M5 `ConfigStore` — when given, chat/LLM/tick
            settings are read live instead of the fixed args above.
        prompt_store: optional M5 `PromptStore` — when given, chat/narrative
            prompt text is read live instead of the hardcoded defaults.
    """
    if not instance_id.strip() or not world_id.strip():
        raise ValueError("instance_id and world_id cannot be empty")
    world_name = world_name.strip() or world_id
    allowed_origins = tuple(dict.fromkeys(origin.rstrip("/") for origin in cors_origins))
    accepted_service_credentials = tuple(
        dict.fromkeys(value for value in platform_service_credentials if value)
    )
    if bool(accepted_service_credentials) != bool(membership_claim_secret):
        raise ValueError(
            "platform service credentials and membership claim secret must be configured together"
        )
    if "*" in allowed_origins:
        raise ValueError("wildcard CORS origins are not allowed")
    if not math.isfinite(tick_rate) or tick_rate <= 0 or tick_rate > MAX_TICKS_PER_SECOND:
        raise ValueError(f"tick_rate must be > 0 and <= {MAX_TICKS_PER_SECOND}")
    # Fall back to whatever the scheduler was already wired with (M5 §10),
    # so callers don't have to pass config_store/prompt_store twice.
    if config_store is None:
        config_store = getattr(scheduler, "config_store", None)
    if prompt_store is None:
        prompt_store = getattr(scheduler, "prompt_store", None)

    serve = _ServeWorld(scheduler)
    _loop_running = {"value": False}
    _loop_thread: list[threading.Thread | None] = [None]
    _reaper_thread: list[threading.Thread | None] = [None]
    _simulation_paused = {"value": False}
    _tick_lock = threading.Lock()

    # Wire scheduler._record_event to also fan out to SSE subscribers
    _orig_record = scheduler._record_event

    def _record_and_fan(event: dict[str, Any]) -> None:
        _orig_record(event)
        serve._fan_out()

    scheduler._record_event = _record_and_fan  # type: ignore[method-assign]

    # ── Chat subsystem (M3.5): decoupled from the event core ────────────────
    # Shares the world.db connection + scheduler lock so the single SQLite
    # connection is only touched under one lock across threads. Session close
    # is the sole seam back to the event log (via the fan-out-wrapped record).
    chat_conn = scheduler.event_log.conn if scheduler.event_log is not None else open_db(":memory:")
    chat_store = ChatStore(chat_conn, lock=scheduler._lock)
    # World-owned delivery receipts make retried platform commands safe.  They
    # contain only provenance and a payload digest, never platform transcripts.
    with scheduler._lock:
        chat_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS world_command_receipts (
              command_id TEXT PRIMARY KEY,
              membership_id TEXT NOT NULL,
              world_id TEXT NOT NULL,
              instance_id TEXT NOT NULL,
              command_type TEXT NOT NULL,
              payload_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at INTEGER NOT NULL
            )
            """
        )
        chat_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS world_chat_evolution_receipts (
              delivery_id TEXT PRIMARY KEY,
              membership_id TEXT NOT NULL,
              world_id TEXT NOT NULL,
              instance_id TEXT NOT NULL,
              agent_id TEXT NOT NULL,
              payload_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              conversation_id INTEGER,
              created_at INTEGER NOT NULL
            )
            """
        )
        chat_conn.commit()
    chat_llm = (
        create_llm_client_from_config(config_store)
        if config_store is not None
        else create_llm_client_from_env()
    )

    def _persona(agent_id: str) -> dict[str, Any]:
        brain = scheduler.agents.get(agent_id)
        if brain is None:
            return {}
        agent = brain.agent
        return {
            "name": agent.name,
            "personality": agent.blackboard.read("personality") or "",
            "location": agent.location,
        }

    # chat-grounding: one snapshot of the agent's lived state per chat turn,
    # read under the scheduler lock (reads only — no LLM, no IO). Any failure
    # inside is caught by ChatService and degrades to the ungrounded prompt.
    _ACTIVITY_LABELS = {
        "sleep": "在睡觉", "work": "在工作", "chat": "在和人聊天",
        "walk": "正准备出门", "idle_wander": "闲着", "idle_social": "闲着",
    }

    def _world_context(agent_id: str, interlocutor_id: str) -> dict[str, Any]:
        from anima_world.memory_triggers import BAND_NAMES, band

        with scheduler._lock:
            brain = scheduler.agents.get(agent_id)
            if brain is None:
                return {}
            ctx: dict[str, Any] = {}
            if scheduler.memory_store is not None:
                try:
                    k = (
                        config_store.get("chat.recall_k", default=3)
                        if config_store is not None else 3
                    )
                    rows = scheduler.memory_store.query(agent_id=agent_id)
                    # Most IMPORTANT first, recency as tiebreak — plain
                    # tick-DESC surfaced machine-y genesis relation_shift rows
                    # over the anchored narrative memories (same tick 0,
                    # arbitrary order), and the chat block wants lived life.
                    rows.sort(key=lambda m: (-float(m.get("importance") or 0), -int(m.get("tick") or 0)))
                    ctx["memories"] = [m["summary"] for m in rows[: int(k)]]
                except Exception:  # noqa: BLE001 - memories are flavor, never fatal
                    pass
            now = scheduler.world_time()
            loc_id = brain.agent.blackboard.read("loc") or brain.agent.location or ""
            loc_name = loc_id
            if scheduler.location_store is not None and loc_id:
                row = scheduler.location_store.get(loc_id)
                if row is not None:
                    loc_name = row.get("name") or loc_id
            activity = serve._agent_activity(agent_id)
            if activity.get("transit"):
                label = f"正在去{activity['transit'].get('to', '别处')}的路上"
            else:
                label = _ACTIVITY_LABELS.get(activity.get("kind"), "闲着")
            others = [
                scheduler.agents[aid].agent.name
                for aid, loc in scheduler._agent_locations().items()
                if loc == loc_id and aid != agent_id
            ]
            ctx["presence"] = {
                "day": now.day, "hh": f"{now.hour:02d}", "mm": f"{now.minute:02d}",
                "location_id": loc_id, "location": loc_name, "activity": label,
                "others": "、".join(others),
            }
            rel = scheduler._memory_projection.relations.get((agent_id, interlocutor_id))
            if rel is not None:
                ctx["relation"] = {
                    "r_type": rel.r_type,
                    "band": BAND_NAMES[band(rel.sentiment)],
                }
            return ctx

    chat_service = ChatService(
        store=chat_store,
        llm=chat_llm,
        persona_provider=_persona,
        config_store=config_store,
        prompt_store=prompt_store,
        world_provider=_world_context,
    )
    session_manager = ChatSessionManager(
        store=chat_store,
        llm=chat_llm,
        emit_event=scheduler._record_event,
        config_store=config_store,
        prompt_store=prompt_store,
        # player-visitor: a closed player conversation gets a relationship
        # verdict from the real transcript (deltas only — the conversation
        # event above already mints the agent's memory).
        judge_hook=lambda info: scheduler.submit_user_chat_judgment(**info),
    )

    # player-visitor: who is visiting, and where they stand. Deliberately
    # in-memory (a restart is a fresh visit); the durable part of a player —
    # conversations, memories about them, relations — lives in the DB.
    # Keyed by membership id: the only identity a world accepts now that the
    # name-based legacy routes are gone.
    players: dict[str, dict[str, Any]] = {}

    def _advance_once() -> None:
        with _tick_lock:
            scheduler.tick()
            serve.on_tick()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_loop:
            _loop_running["value"] = True

            def _loop() -> None:
                while _loop_running["value"] and not scheduler._stopped:
                    if _simulation_paused["value"]:
                        time.sleep(0.1)
                        continue
                    _advance_once()
                    # Sleep in short slices, re-reading the configured rate each
                    # slice: at 1 tick / 5 min a single sleep(300) would pin a
                    # tick_rate edit (M5 hot-reload) and graceful shutdown to the
                    # old interval for up to five minutes.
                    slept = 0.0
                    while _loop_running["value"] and not scheduler._stopped:
                        interval = 1.0 / _resolve_tick_rate(tick_rate, config_store)
                        if slept >= interval or _simulation_paused["value"]:
                            break
                        step = min(0.5, interval - slept)
                        time.sleep(step)
                        slept += step

            def _reaper() -> None:
                # Own thread: closing a conversation summarizes via a network
                # LLM call, which must never pin the tick loop — the same rule
                # that put narrative/planner/judge on their pools. Sliced sleep
                # so shutdown isn't held for a full interval.
                while _loop_running["value"] and not scheduler._stopped:
                    slept = 0.0
                    while (
                        _loop_running["value"]
                        and not scheduler._stopped
                        and slept < _REAP_INTERVAL
                    ):
                        time.sleep(0.5)
                        slept += 0.5
                    if not _loop_running["value"] or scheduler._stopped:
                        return
                    try:
                        asyncio.run(session_manager.reap_idle())
                    except Exception:  # pragma: no cover - reaper is best-effort
                        pass

            t = threading.Thread(target=_loop, daemon=True)
            _loop_thread[0] = t
            t.start()
            reaper_thread = threading.Thread(target=_reaper, daemon=True)
            _reaper_thread[0] = reaper_thread
            reaper_thread.start()
        yield
        _loop_running["value"] = False
        loop_thread = _loop_thread[0]
        if loop_thread is not None:
            loop_thread.join(timeout=1.0)
        reaper = _reaper_thread[0]
        if reaper is not None:
            reaper.join(timeout=1.0)
        # The event log is committed per event, so a snapshot is only a
        # rebuild cache. Never let one in-flight daemon tick prevent the HTTP
        # server from honoring SIGTERM or an authenticated admin stop.
        if loop_thread is None or not loop_thread.is_alive():
            scheduler.stop(wait=False)
            serve.save_persistent_snapshot()

    app = FastAPI(title="Cyberworld Web", lifespan=lifespan)
    app.state.world_chat_store = chat_store
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            # Authorization is permitted so the operator-hosted admin console can
            # reach /api/admin/v1 cross-origin; origins stay gated to allowed_origins.
            allow_headers=["Content-Type", "Authorization"],
            expose_headers=["X-Cyberworld-Protocol-Version", "X-Cyberworld-Instance-Id"],
        )
    app.state.world_provider = _world_context  # 供测试与 change ② 复用

    if require_local_api_auth:
        # The unauthenticated /api/* group exists for loopback operation only:
        # it can write llm.api_key, rewrite every prompt, and pause the world.
        # On a non-loopback bind those become admin-token-gated — /internal/v1
        # and /api/admin/v1 are fail-closed, and this group must not be the one
        # door left open. OPTIONS passes through so CORS preflights still work.
        @app.middleware("http")
        async def _guard_local_api(request: Request, call_next):
            path = request.url.path
            if (
                request.method != "OPTIONS"
                and path.startswith("/api/")
                and not path.startswith("/api/admin/")
            ):
                authorization = request.headers.get("Authorization", "")
                scheme, _, supplied = authorization.partition(" ")
                if (
                    admin_token is None
                    or scheme.lower() != "bearer"
                    or not supplied
                    or not hmac.compare_digest(supplied, admin_token)
                ):
                    return JSONResponse(
                        status_code=401,
                        content={
                            "detail": "local API requires admin authorization on a non-loopback bind"
                        },
                    )
            return await call_next(request)

    def _trusted_claim(request: Request) -> MembershipClaim:
        """Authenticate one platform-to-runtime request and bind its audience."""
        if "player_name" in request.query_params or request.headers.get("X-Player-Name"):
            raise HTTPException(status_code=400, detail="free-form identity fields are forbidden")
        authorization = request.headers.get("Authorization", "")
        scheme, _, supplied_credential = authorization.partition(" ")
        if scheme.lower() != "bearer" or not service_credential_matches(
            supplied_credential, accepted_service_credentials
        ):
            raise HTTPException(status_code=401, detail="invalid platform service credential")
        token = request.headers.get("X-Cyberworld-Membership", "")
        if not token or membership_claim_secret is None:
            raise HTTPException(status_code=401, detail="membership claim is required")
        try:
            return verify_membership_claim(
                token,
                membership_claim_secret,
                world_id=world_id,
                instance_id=instance_id,
            )
        except MembershipClaimError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    async def _require_world_admin(request: Request) -> None:
        authorization = request.headers.get("Authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        if (
            admin_token is None
            or scheme.lower() != "bearer"
            or not supplied
            or not hmac.compare_digest(supplied, admin_token)
        ):
            raise HTTPException(status_code=401, detail="world admin authentication required")

    def _database_name() -> str:
        if scheduler.db_path == ":memory:":
            return ":memory:"
        return str(Path(scheduler.db_path).resolve())

    def _table_names() -> list[str]:
        with scheduler._lock:
            rows = chat_conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def _json_cell(value: Any) -> Any:
        if isinstance(value, bytes):
            return {
                "type": "blob",
                "base64": base64.b64encode(value).decode("ascii"),
            }
        return value

    def _membership_view(claim: MembershipClaim) -> dict[str, str]:
        return {
            "membership_id": claim.membership_id,
            "role": claim.role,
        }

    # ── Routes ──────────────────────────────────────────────────────────────

    @app.get("/")
    async def index():
        """No UI here, and none coming: the player surface belongs to the site
        and authoring belongs to the studio, a separate desktop program. Say so
        rather than 404 blankly at whoever opened this in a browser."""
        return JSONResponse(
            status_code=404,
            content={
                "detail": "这是世界引擎的 API,没有网页界面。"
                          "创作/编辑世界用创作工作台(anima-studio);玩家界面由网站提供。",
                "world_id": world_id,
                "instance_id": instance_id,
                "api": ["/api/v1/world", "/api/state", "/api/config", "/internal/v1/meta"],
            },
        )

    @app.get("/api/admin/v1/runtime")
    async def admin_runtime(
        request: Request,
        _: None = Depends(_require_world_admin),
    ):
        return {
            "world": {
                "name": world_name,
                "world_id": world_id,
                "instance_id": instance_id,
            },
            "runtime": {
                "endpoint": str(request.base_url).rstrip("/"),
                "database": _database_name(),
                "tick": scheduler.clock,
                "paused": _simulation_paused["value"],
                "tick_rate": _resolve_tick_rate(tick_rate, config_store),
            },
        }

    @app.post("/api/admin/v1/evolution/pause")
    async def admin_pause(_: None = Depends(_require_world_admin)):
        _simulation_paused["value"] = True
        return {"paused": True, "tick": scheduler.clock}

    @app.post("/api/admin/v1/evolution/resume")
    async def admin_resume(_: None = Depends(_require_world_admin)):
        _simulation_paused["value"] = False
        return {"paused": False, "tick": scheduler.clock}

    @app.post("/api/admin/v1/server/stop", status_code=202)
    async def admin_stop_server(
        background_tasks: BackgroundTasks,
        _: None = Depends(_require_world_admin),
    ):
        if shutdown_callback is None:
            raise HTTPException(status_code=503, detail="server shutdown is not configured")

        async def request_shutdown_after_response() -> None:
            await asyncio.sleep(0)
            shutdown_callback()

        background_tasks.add_task(request_shutdown_after_response)
        return JSONResponse(
            {"status": "stopping", "world_id": world_id},
            status_code=202,
        )

    @app.get("/api/admin/v1/database/tables")
    async def admin_database_tables(_: None = Depends(_require_world_admin)):
        tables = []
        with scheduler._lock:
            for name in _table_names():
                quoted = name.replace('"', '""')
                total = int(chat_conn.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0])
                tables.append({"name": name, "rows": total})
        return {"database": _database_name(), "tables": tables}

    @app.get("/api/admin/v1/database/tables/{table_name}")
    async def admin_database_table(
        table_name: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
        _: None = Depends(_require_world_admin),
    ):
        if table_name not in _table_names():
            raise HTTPException(status_code=404, detail="database table not found")
        quoted = table_name.replace('"', '""')
        with scheduler._lock:
            column_rows = chat_conn.execute(f'PRAGMA table_info("{quoted}")').fetchall()
            columns = [
                {
                    "position": int(row[0]),
                    "name": str(row[1]),
                    "type": str(row[2]),
                    "not_null": bool(row[3]),
                    "default": row[4],
                    "primary_key": bool(row[5]),
                }
                for row in column_rows
            ]
            total = int(chat_conn.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0])
            result_rows = chat_conn.execute(
                f'SELECT * FROM "{quoted}" LIMIT ? OFFSET ?', (limit, offset)
            ).fetchall()
        names = [column["name"] for column in columns]
        rows = [
            {name: _json_cell(row[index]) for index, name in enumerate(names)}
            for row in result_rows
        ]
        return {
            "table": table_name,
            "columns": columns,
            "rows": rows,
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    @app.get("/api/v1/world")
    async def get_world():
        return {
            "protocol_version": "1",
            "instance_id": instance_id,
            "world_id": world_id,
            "capabilities": ["state", "chat", "player_move", "stream"],
        }

    @app.get("/internal/v1/context/{agent_id}")
    async def internal_context(agent_id: str, request: Request):
        claim = _trusted_claim(request)
        brain = scheduler.agents.get(agent_id)
        if brain is None:
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        context = _world_context(agent_id, claim.membership_id)
        # Keep this adapter deliberately bounded.  It is grounding, not a
        # projection export or a backdoor to arbitrary world tables.
        bounded: dict[str, Any] = {}
        memories = context.get("memories")
        if isinstance(memories, list):
            bounded["memories"] = [str(item)[:500] for item in memories[:8]]
        for key in ("presence", "relation"):
            if isinstance(context.get(key), dict):
                bounded[key] = context[key]
        agent = brain.agent
        return {
            "protocol_version": "1",
            "world_id": world_id,
            "instance_id": instance_id,
            "membership": _membership_view(claim),
            "agent": {
                "id": agent_id,
                "name": agent.name,
                "location": agent.blackboard.read("loc") or agent.location,
            },
            "context": bounded,
        }

    @app.get("/internal/v1/meta")
    async def internal_meta():
        """Auth-free edition notice: which protocol/engine/db-format this
        runtime speaks. The platform health prober records it; nothing here
        is sensitive."""
        from anima_world.db import DB_FORMAT_VERSION
        from anima_world.world_package import _engine_version

        return {
            "protocol": INTERNAL_PROTOCOL_VERSION,
            "engine_version": _engine_version(),
            "db_format": DB_FORMAT_VERSION,
            "world_id": world_id,
            "instance_id": instance_id,
        }

    @app.get("/internal/v1/state")
    async def internal_state(request: Request):
        claim = _trusted_claim(request)
        state = serve.snapshot()
        # Recent logs are useful for immediate UI grounding but must not grow
        # an internal response without bound.
        for key in ("recent_events", "narrative_log"):
            if isinstance(state.get(key), list):
                state[key] = state[key][-100:]
        state.update({
            "protocol_version": "1",
            "world_id": world_id,
            "instance_id": instance_id,
            "membership": _membership_view(claim),
            "player": players.get(claim.membership_id),
        })
        return state

    @app.post("/internal/v1/chat")
    async def internal_chat(body: dict[str, Any], request: Request):
        claim = _trusted_claim(request)
        if {"player_name", "display_name", "role", "user_id", "membership_id"}.intersection(body):
            raise HTTPException(status_code=400, detail="free-form identity fields are forbidden")
        agent_id = body.get("agent_id")
        history = body.get("messages")
        if not isinstance(agent_id, str) or agent_id not in scheduler.agents:
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        if not isinstance(history, list) or not history or len(history) > 20:
            raise HTTPException(status_code=400, detail="messages must contain 1 to 20 items")
        messages: list[dict[str, str]] = []
        for item in history:
            if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                raise HTTPException(status_code=400, detail="invalid chat message")
            content = item.get("content")
            if not isinstance(content, str) or not content.strip() or len(content) > 4000:
                raise HTTPException(status_code=400, detail="invalid chat message content")
            messages.append({"role": item["role"], "content": content})
        if messages[-1]["role"] != "user":
            raise HTTPException(status_code=400, detail="last message must be from user")
        player = players.get(claim.membership_id, {})
        player_location = str(player.get("location") or "")
        player_location_name = player_location
        if scheduler.location_store is not None and player_location:
            location = scheduler.location_store.get(player_location)
            if location is not None:
                player_location_name = location.get("name") or player_location
        raw_display = str(body.get("player_display_name") or "").strip()
        display_name = raw_display if raw_display else f"player-{claim.membership_id[:8]}"
        return StreamingResponse(
            chat_service.respond(
                agent_id,
                messages,
                interlocutor_id=claim.membership_id,
                interlocutor={
                    "display_name": display_name,
                    "role": claim.role,
                    "location": player_location,
                    "location_name": player_location_name,
                },
            ),
            media_type="text/plain",
            headers={
                "X-Cyberworld-World-Id": world_id,
                "X-Cyberworld-Instance-Id": instance_id,
            },
        )

    @app.post("/internal/v1/chat-evolution")
    async def internal_chat_evolution(body: dict[str, Any], request: Request):
        claim = _trusted_claim(request)
        forbidden_identity = {"player_name", "display_name", "role", "user_id", "membership_id"}
        if forbidden_identity.intersection(body):
            raise HTTPException(status_code=400, detail="free-form identity fields are forbidden")
        delivery_id = body.get("delivery_id")
        agent_id = body.get("agent_id")
        raw_messages = body.get("messages")
        if not isinstance(delivery_id, str) or not delivery_id.strip() or len(delivery_id) > 128:
            raise HTTPException(status_code=400, detail="valid delivery_id is required")
        delivery_id = delivery_id.strip()
        if not isinstance(agent_id, str) or agent_id not in scheduler.agents:
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        if not isinstance(raw_messages, list) or len(raw_messages) != 2:
            raise HTTPException(status_code=400, detail="messages must contain one completed turn")
        messages: list[dict[str, str]] = []
        for index, item in enumerate(raw_messages):
            expected_role = "user" if index == 0 else "assistant"
            if not isinstance(item, dict) or item.get("role") != expected_role:
                raise HTTPException(status_code=400, detail="messages must be user then assistant")
            content = item.get("content")
            if not isinstance(content, str) or not content.strip() or len(content) > 4000:
                raise HTTPException(status_code=400, detail="invalid chat message content")
            messages.append({"role": expected_role, "content": content.strip()})

        canonical = json.dumps(
            {"agent_id": agent_id, "messages": messages},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        payload_hash = hashlib.sha256(canonical.encode()).hexdigest()
        provenance = (
            claim.membership_id, world_id, instance_id, agent_id, payload_hash
        )
        with scheduler._lock:
            existing = chat_conn.execute(
                """SELECT membership_id, world_id, instance_id, agent_id, payload_hash,
                          status, conversation_id
                   FROM world_chat_evolution_receipts WHERE delivery_id = ?""",
                (delivery_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing[:5]) != provenance:
                    raise HTTPException(status_code=409, detail="delivery_id provenance conflict")
                if existing[5] == "applied":
                    return {
                        "world_id": world_id,
                        "instance_id": instance_id,
                        "delivery_id": delivery_id,
                        "status": existing[5],
                        "conversation_id": existing[6],
                        "duplicate": True,
                    }
                # A receipt still 'processing' means an earlier attempt died
                # mid-flight (failures below delete their receipt; only a hard
                # crash leaves one). Treating it as a duplicate would turn the
                # idempotency table into a black hole where the first failure
                # swallows every retry — take the delivery over instead.
                chat_conn.execute(
                    "UPDATE world_chat_evolution_receipts SET created_at = ? WHERE delivery_id = ?",
                    (int(time.time()), delivery_id),
                )
            else:
                chat_conn.execute(
                    """INSERT INTO world_chat_evolution_receipts
                       (delivery_id, membership_id, world_id, instance_id, agent_id,
                        payload_hash, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'processing', ?)""",
                    (delivery_id, *provenance, int(time.time())),
                )
            chat_conn.commit()

        try:
            player = players.get(claim.membership_id, {})
            location = str(player.get("location") or "") or None
            participants = [
                {"id": claim.membership_id, "kind": "user"},
                {"id": agent_id, "kind": "agent"},
            ]
            conversation_id = chat_store.start_conversation(
                agent_id,
                int(time.time()),
                participants=participants,
                location=location,
                player_id=claim.membership_id,
            )
            for message in messages:
                chat_store.add_message(
                    conversation_id, message["role"], message["content"], int(time.time())
                )
            await session_manager.close_conversation(conversation_id)
        except BaseException:
            # Withdraw the receipt so the platform's retry gets a real attempt
            # rather than a 'processing' echo. A conversation started before
            # the failure lingers open and is swept by the idle reaper.
            with scheduler._lock:
                chat_conn.execute(
                    "DELETE FROM world_chat_evolution_receipts WHERE delivery_id = ?",
                    (delivery_id,),
                )
                chat_conn.commit()
            raise
        with scheduler._lock:
            chat_conn.execute(
                """UPDATE world_chat_evolution_receipts
                   SET status = 'applied', conversation_id = ? WHERE delivery_id = ?""",
                (conversation_id, delivery_id),
            )
            chat_conn.commit()
        return {
            "world_id": world_id,
            "instance_id": instance_id,
            "delivery_id": delivery_id,
            "status": "applied",
            "conversation_id": conversation_id,
            "duplicate": False,
        }

    @app.get("/internal/v1/stream")
    async def internal_stream(request: Request):
        claim = _trusted_claim(request)

        async def membership_stream():
            # Establish the trusted identity before subscribing; subsequent
            # frames contain only world events and the membership identifier.
            yield f"event: ready\ndata: {json.dumps({'membership_id': claim.membership_id})}\n\n"
            q = serve.subscribe()
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        item = await asyncio.to_thread(q.get, True, 1.0)
                    except queue.Empty:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            finally:
                serve.unsubscribe(q)

        return StreamingResponse(membership_stream(), media_type="text/event-stream")

    @app.post("/internal/v1/commands")
    async def internal_command(body: dict[str, Any], request: Request):
        claim = _trusted_claim(request)
        forbidden_identity = {"player_name", "display_name", "role", "user_id", "membership_id"}
        if forbidden_identity.intersection(body):
            raise HTTPException(status_code=400, detail="free-form identity fields are forbidden")
        command_id = body.get("command_id")
        command_type = body.get("type")
        payload = body.get("payload", {})
        if not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 128:
            raise HTTPException(status_code=400, detail="valid command_id is required")
        if command_type not in {"noop", "player_move", "player_action"}:
            raise HTTPException(status_code=400, detail="command type is not allowlisted")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="command payload must be an object")
        if forbidden_identity.intersection(payload):
            raise HTTPException(status_code=400, detail="free-form identity fields are forbidden")
        if command_type == "player_move":
            location = payload.get("location")
            if not isinstance(location, str) or not location.strip():
                raise HTTPException(status_code=400, detail="location is required")
            location = location.strip()
            if scheduler.location_store is not None:
                row = scheduler.location_store.get(location)
                if row is None or row.get("kind", "point") != "point":
                    raise HTTPException(status_code=404, detail=f"没有 {location} 这个地方")
        if command_type == "player_action":
            action = payload.get("action")
            details = payload.get("details", {})
            if not isinstance(action, str) or not action.strip():
                raise HTTPException(status_code=400, detail="action is required")
            if not isinstance(details, dict):
                raise HTTPException(status_code=400, detail="action details must be an object")
        canonical = json.dumps(
            {"type": command_type, "payload": payload},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        payload_hash = hashlib.sha256(canonical.encode()).hexdigest()
        with scheduler._lock:
            existing = chat_conn.execute(
                """SELECT membership_id, world_id, instance_id, command_type, payload_hash, status
                   FROM world_command_receipts WHERE command_id = ?""",
                (command_id,),
            ).fetchone()
            if existing is not None:
                expected = (
                    claim.membership_id, world_id, instance_id, command_type, payload_hash
                )
                if tuple(existing[:5]) != expected:
                    raise HTTPException(status_code=409, detail="command_id provenance conflict")
                return {"command_id": command_id, "status": existing[5], "duplicate": True}
            chat_conn.execute(
                """INSERT INTO world_command_receipts
                   (command_id, membership_id, world_id, instance_id, command_type,
                    payload_hash, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'accepted', ?)""",
                (
                    command_id,
                    claim.membership_id,
                    world_id,
                    instance_id,
                    command_type,
                    payload_hash,
                    int(time.time()),
                ),
            )
            chat_conn.commit()
            if command_type == "player_move":
                players[claim.membership_id] = {
                    "role": claim.role,
                    "location": location,
                }
            elif command_type == "player_action":
                player = players.get(claim.membership_id, {})
                scheduler._record_event({
                    "type": "player_action",
                    "who": f"membership:{claim.membership_id}",
                    "membership_id": claim.membership_id,
                    "role": claim.role,
                    "loc": player.get("location"),
                    "action": action.strip(),
                    "details": details,
                    "command_id": command_id,
                })
        return {"command_id": command_id, "status": "accepted", "duplicate": False}

    @app.get("/api/v1/state")
    @app.get("/api/state")
    async def get_state(request: Request):
        state = serve.snapshot()
        state["players"] = {pid: dict(p) for pid, p in players.items()}
        state["simulation"] = {
            "paused": _simulation_paused["value"],
            "tick_rate": _resolve_tick_rate(tick_rate, config_store),
        }
        if request.url.path.startswith("/api/v1/"):
            state.update({
                "protocol_version": "1",
                "instance_id": instance_id,
                "world_id": world_id,
            })
        return state

    @app.post("/api/simulation/toggle")
    async def toggle_simulation():
        _simulation_paused["value"] = not _simulation_paused["value"]
        return {
            "paused": _simulation_paused["value"],
            "tick": scheduler.clock,
        }

    @app.get("/api/memories/{agent_id}")
    async def get_memories(agent_id: str):
        memories = scheduler.memory_store.query(agent_id=agent_id) if scheduler.memory_store else []
        return {"agent_id": agent_id, "memories": memories}

    @app.get("/api/graph")
    async def get_graph(agent_id: str | None = None):
        if scheduler.knowledge_graph is None:
            return {"edges": []}
        if agent_id is not None:
            edges = scheduler.knowledge_graph.query(subject=agent_id)
        else:
            edges = scheduler.knowledge_graph.query()
        return {"edges": edges}

    @app.get("/api/config")
    async def get_config(category: str | None = None):
        if config_store is None:
            return {"config": []}
        rows = []
        for row in config_store.list(category=category):
            value = mask_secret(row["value"]) if row["is_secret"] and row["value"] else row["value"]
            rows.append({**row, "value": value})
        return {"config": rows}

    @app.put("/api/config/{key}")
    async def put_config(key: str, body: dict[str, Any]):
        if config_store is None or not config_store.has(key):
            raise HTTPException(status_code=404, detail=f"config key {key} not found")
        meta = config_store.meta(key)
        is_secret = meta["is_secret"]
        if "value" not in body:
            if is_secret:
                return {"key": key, "updated": False}
            raise HTTPException(status_code=400, detail="missing 'value'")
        value = body["value"]
        if is_secret and value == "":
            raise HTTPException(status_code=400, detail="secret value cannot be set to empty")
        value_type = meta["value_type"]
        try:
            if value_type == "int":
                value = int(value)
            elif value_type == "float":
                value = float(value)
            elif value_type == "bool":
                if isinstance(value, str):
                    if value.lower() not in ("true", "false", "1", "0"):
                        raise ValueError(f"invalid bool value: {value}")
                    value = value.lower() in ("true", "1")
                else:
                    value = bool(value)
            else:
                value = str(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"'{key}' expects a {value_type} value") from None
        if key == "scheduler.tick_rate" and (
            not math.isfinite(value) or value <= 0 or value > MAX_TICKS_PER_SECOND
        ):
            raise HTTPException(
                status_code=400,
                detail=f"'{key}' must be > 0 and <= {MAX_TICKS_PER_SECOND}",
            )
        config_store.set(key, value)
        return {"key": key, "updated": True}

    @app.get("/api/prompts")
    async def get_prompts():
        if prompt_store is None:
            return {"prompts": []}
        return {"prompts": prompt_store.list()}

    @app.put("/api/prompts/{name}")
    async def put_prompt(name: str, body: dict[str, Any]):
        if prompt_store is None or not prompt_store.has(name):
            raise HTTPException(status_code=404, detail=f"prompt {name} not found")
        template = body.get("template", "")
        try:
            prompt_store.set(name, template)
        except PromptRenderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"name": name, "updated": True}

    @app.get("/api/conversations/{agent_id}")
    async def list_conversations(agent_id: str):
        return {"agent_id": agent_id, "conversations": chat_store.list_conversations(agent_id)}

    @app.get("/api/conversations/{conversation_id}/messages")
    async def conversation_messages(conversation_id: int):
        return {
            "conversation_id": conversation_id,
            "messages": chat_store.messages_for(conversation_id),
        }

    @app.post("/api/conversations/{conversation_id}/close")
    async def close_conversation(conversation_id: int):
        emitted = await session_manager.close_conversation(conversation_id)
        conv = chat_store.get(conversation_id)
        return {
            "conversation_id": conversation_id,
            "closed": bool(conv and conv["status"] == "closed"),
            "event_emitted": emitted,
        }

    @app.get("/api/v1/stream")
    @app.get("/api/stream")
    async def stream(since_seq: int | None = None):
        async def event_generator():
            q = serve.subscribe()
            try:
                # First frame: send recent events as catchup
                recent = serve.catchup_events(since_seq)
                if recent:
                    yield f"event: catchup\ndata: {json.dumps(recent)}\n\n"

                while True:
                    try:
                        # Block on the thread-safe queue from the asyncio loop
                        msg = await asyncio.to_thread(q.get, timeout=5.0)
                        events = msg.get("events", [])
                        for ev in events:
                            yield f"event: world\ndata: {json.dumps(ev)}\n\n"
                    except queue.Empty:
                        # heartbeat to keep connection alive
                        yield "event: heartbeat\ndata: {\"ts\": 0}\n\n"
            except asyncio.CancelledError:
                pass
            except GeneratorExit:
                pass
            finally:
                serve.unsubscribe(q)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "X-Cyberworld-Protocol-Version": "1",
                "X-Cyberworld-Instance-Id": instance_id,
            },
        )

    return app


# ── Inline HTML (single-file frontend) ─────────────────────────────────────
