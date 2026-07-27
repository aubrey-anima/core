"""anima_world.api — 引擎的官方函数门面(纯库接口)。

引擎是一个库,不是一个服务:任何要用世界的模块 import 本包,通过 `World`
的函数操作 `world.db`。没有 HTTP、没有进程边界 —— 世界活在调用方进程里。

三条使用纪律(调用方必须遵守,门面内部无法替你兜住):

1. **一个运行中的世界独占它的 world.db。** 世界的真相一半在内存里(时钟、
   投影、锁、线程池),绕过 World 直接写同一个 db 文件会让两边立刻分叉。
   离线处置(打包、快进)走 CLI 或在世界关闭后进行。
2. **一个进程一个引擎版本。** 世界文件钉死在生成它的引擎版本上;要同时
   操作不同版本的世界,按版本拆进程(anima-studio 的隔离 venv + 子进程
   就是这个模式)。
3. **信任边界是进程边界。** import 了本包就拥有世界的一切;玩家身份
   (player_id)只是参数,验证调用者是谁是宿主应用自己的责任。

最短用法::

    from anima_world.api import World

    with World.open("saves/world.db") as world:
        world.start_clock()                  # 后台走时钟(或手动 world.tick())
        print(world.state()["world_time"])
        for chunk in world.chat("夏", [{"role": "user", "content": "你好"}],
                                player_id="p1", display_name="阿宇"):
            print(chunk, end="")
"""

from __future__ import annotations

import asyncio
import logging
import queue
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from anima_world.chat_service import ChatService
from anima_world.chat_session import ChatSessionManager
from anima_world.chat_store import ChatStore
from anima_world.config_store import mask_secret
from anima_world.llm_client import create_llm_client_from_config, create_llm_client_from_env
from anima_world.locations import DEFAULT_POINTS
from anima_world.narrative import MockNarrativeProvider, OpenAICompatibleNarrativeProvider
from anima_world.scheduler import MAX_TICKS_PER_SECOND, Scheduler
from anima_world.types import AgentState, Projection
from anima_world.world_package import WorldPackageManifest, export_world_package
from anima_world.world_time import DEFAULT_MINUTES_PER_TICK

logger = logging.getLogger(__name__)

# The idle reaper scans for stale conversations this often (wall seconds).
_REAP_INTERVAL = 30.0

# Map row fields exposed in state(); consumers assemble the tree from them.
_LOCATION_KEYS = ("id", "name", "description", "kind", "parent", "x", "y", "w", "h")

_ACTIVITY_LABELS = {
    "sleep": "在睡觉", "work": "在工作", "chat": "在和人聊天",
    "walk": "正准备出门", "idle_wander": "闲着", "idle_social": "闲着",
}


def _resolve_tick_rate(fallback: float, config_store: Any | None) -> float:
    if config_store is None:
        return fallback
    return config_store.get("scheduler.tick_rate", default=fallback)


class _WorldView:
    """Wraps a running Scheduler with an up-to-date Projection and event fan-out.

    (原 web 层的 _ServeWorld,原样平移 —— 投影同步、状态快照、订阅队列
    与 HTTP 无关,是库门面的核心。)
    """

    def __init__(self, scheduler: Scheduler) -> None:
        self.scheduler = scheduler
        self._recovered_from_persistence = scheduler.event_log is not None
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        self._subscribers_lock = threading.Lock()
        self._last_fanout_seq = 0

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

    @property
    def projection(self) -> Projection:
        """系统里只有一份投影 —— scheduler 的那份。

        这里曾经维护第二份:开机全量重放建起来,此后只同步叙事日志和角色位置,
        经济与关系变化一概不折叠。于是它在运行中停在开机状态,而
        `scheduler._memory_projection` 才是每条事件都折叠、始终正确的那份
        (`_apply_memory_trigger` 无条件 `project_events`)。两份投影既是重复
        开销(开世界要重放两遍),也是陷阱 —— 从这份读余额或关系会读到旧值。
        已删除的 `snapshots` 表正是把这份陈旧投影写回库里,才留下会累积的错账。
        """
        return self.scheduler._memory_projection

    def on_tick(self) -> None:
        self._fan_out()

    def _fan_out(self) -> None:
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
        with self.scheduler._lock:
            if since_seq is None:
                return list(self.scheduler.recent_events)
            return [
                ev for ev in self.scheduler.recent_events
                if int(ev.get("seq", 0) or 0) > since_seq
            ]

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=64)
        with self._subscribers_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self._subscribers_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def snapshot(self) -> dict[str, Any]:
        # scheduler 的 RLock 是系统唯一的锁,投影既然是它的,读也走它 ——
        # 这里曾经还有一把 _projection_lock 守着第二份投影。
        with self.scheduler._lock:
            recent_events = list(self.scheduler.recent_events)
            return self._snapshot_locked(recent_events)

    def _latest_event_seq(self) -> int:
        if self.scheduler.event_log is None:
            return max(
                (int(ev.get("seq", 0) or 0) for ev in self.scheduler.recent_events),
                default=0,
            )
        row = self.scheduler.event_log.conn.execute("SELECT MAX(seq) FROM events").fetchone()
        return int(row[0] or 0)

    def _agent_activity(self, agent_id: str) -> dict[str, Any]:
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
            # 在场角色的位置读活黑板(在途时黑板才是真的),离场的读投影。
            # 以前这是靠每 tick 往第二份投影里回写一次维护的。
            location = (
                (brain.agent.blackboard.read("loc") or brain.agent.location)
                if brain is not None
                else a.location
            )
            agents[aid] = {
                "name": brain.agent.name if brain else a.spec.get("name", aid),
                "location": location,
                "state": dict(a.state),
                "activity": self._agent_activity(aid),
                "away": brain is None,
            }
        for aid, brain in self.scheduler.agents.items():
            if aid not in agents:
                agents[aid] = {
                    "name": aid,
                    "location": brain.agent.location or "",
                    "state": {},
                    "activity": self._agent_activity(aid),
                    "away": False,
                }

        loc_store = getattr(self.scheduler, "location_store", None)
        locations: list[dict[str, Any]] = []
        if loc_store is not None:
            try:
                locations = [
                    {k: row.get(k) for k in _LOCATION_KEYS} for row in loc_store.all()
                ]
            except Exception:  # a broken map read must not take down state()
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
                f"{a}|{b}": {
                    "sentiment": r.sentiment,
                    "trust": r.trust,
                    "affection": r.affection,
                    "respect": r.respect,
                }
                for (a, b), r in self.projection.relations.items()
            },
            "narrative_log": list(self.projection.narrative_log)[-200:],
            "recent_events": recent_events,
            "runtime": self._runtime_status(recent_events),
        }

    def _llm_degraded_reason(self) -> str | None:
        """Why this world is answering with the Mock LLM — the one place that
        distinguishes a never-configured key from an unreadable one."""
        config_store = self.scheduler.config_store
        if config_store is None:
            return "no config store: this world has no LLM configuration (in-memory run)"
        undecryptable = getattr(config_store, "undecryptable_secrets", None)
        if callable(undecryptable) and "llm.api_key" in undecryptable():
            return "llm.api_key could not be decrypted — did <db>.key travel with the database?"
        if not (config_store.get("llm.api_key", default="") or ""):
            return "llm.api_key is not configured"
        return None

    def _runtime_status(self, recent_events: list[dict[str, Any]]) -> dict[str, Any]:
        conn = self.scheduler.event_log.conn if self.scheduler.event_log is not None else None
        if conn is not None:
            event_row = conn.execute("SELECT COUNT(*), MAX(seq) FROM events").fetchone()
            db_status = {"enabled": True, "path": self.scheduler.db_path}
            events_status = {
                "count": int(event_row[0] or 0),
                "latest_seq": int(event_row[1] or 0),
                "buffered_count": len(recent_events),
            }
        else:
            db_status = {"enabled": False, "path": None}
            events_status = {
                "count": len(recent_events),
                "latest_seq": max((int(ev.get("seq", 0) or 0) for ev in recent_events), default=0),
                "buffered_count": len(recent_events),
            }

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
        }


class World:
    """一个打开的世界:时钟、状态、聊天、玩家、配置,全部函数化。

    用 `World.open(db_path)` 打开,用完 `close()`(或 with 语句)。
    所有方法线程安全 —— 内部沿用调度器的单锁纪律。
    """

    def __init__(self, scheduler: Scheduler) -> None:
        """Wrap an already-built scheduler. 常规入口是 `World.open`。"""
        self.scheduler = scheduler
        self._view = _WorldView(scheduler)
        self._paused = False
        self._clock_running = False
        self._clock_thread: threading.Thread | None = None
        self._reaper_thread: threading.Thread | None = None
        self._tick_lock = threading.Lock()
        self._closed = False

        # 聊天子系统:共享世界的 SQLite 连接与唯一锁。
        if scheduler.event_log is None:
            raise ValueError("World requires a persistent scheduler (event_log wired)")
        conn = scheduler.event_log.conn
        self.chat_store = ChatStore(conn, lock=scheduler._lock)
        config_store = scheduler.config_store
        chat_llm = (
            create_llm_client_from_config(config_store)
            if config_store is not None
            else create_llm_client_from_env()
        )
        self.chat_service = ChatService(
            store=self.chat_store,
            llm=chat_llm,
            persona_provider=self._persona,
            config_store=config_store,
            prompt_store=scheduler.prompt_store,
            world_provider=self.world_context,
        )
        self.session_manager = ChatSessionManager(
            store=self.chat_store,
            llm=chat_llm,
            emit_event=self._record_and_fan,
            config_store=config_store,
            prompt_store=scheduler.prompt_store,
            judge_hook=lambda info: scheduler.submit_user_chat_judgment(**info),
        )
        # 在场玩家(刻意内存态:重启即新访;持久的部分——会话/记忆/关系——在 db 里)
        self.players: dict[str, dict[str, Any]] = {}

        # 开机补完:会话只在 record_chat_turn 一次调用内开与关,且运行中的
        # 世界独占 db,所以此刻还 open 的行只能是上次崩溃的遗留。消息早已
        # 逐条落盘,补上总结与那一个 conversation 事件即可 —— 崩溃从
        # "丢总结"降级为"总结晚到"。
        try:
            orphans = asyncio.run(self.session_manager.reap_orphans())
            if orphans:
                logger.info("closed %d orphaned conversation(s) from a previous run", len(orphans))
        except Exception:  # noqa: BLE001 - recovery must never block open
            logger.warning("orphan conversation recovery failed", exc_info=True)

    # ── 生命周期 ────────────────────────────────────────────────────────────

    @classmethod
    def open(
        cls,
        db_path: str,
        *,
        seed_path: str | None = None,
        beats_path: str | None = None,
        agents: int | None = None,
        force_mock_llm: bool = False,
    ) -> "World":
        """打开(或创建)一个世界。

        空库首启时从 seed 播种(缺省用内置种子);已有库的 seed 会被忽略并
        警告。坏 beats 脚本在这里当场抛 BeatScriptError,不流到运行期。
        """
        from anima_world.__main__ import build_serve_scheduler  # 延迟导入避免环

        scheduler = build_serve_scheduler(
            agents,
            db_path=db_path,
            seed_path=seed_path,
            beats_path=beats_path,
            force_mock_llm=force_mock_llm,
        )
        return cls(scheduler)

    def close(self, *, wait: bool = True) -> None:
        """停时钟、排干 LLM 线程池、存快照。幂等。"""
        if self._closed:
            return
        self._closed = True
        self.stop_clock()
        self.scheduler.stop(wait=wait)

    def __enter__(self) -> "World":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def export_snapshot(
        self,
        output_path: str | Path,
        *,
        world_id: str,
        name: str,
        seed_path: str | Path | None = None,
        beats_path: str | Path | None = None,
        summary: str = "",
        genre: str = "",
        setting: str = "",
        theme: str = "default",
    ) -> WorldPackageManifest:
        """活体导出:世界不停,当场打出一个完整的 snapshot 包。

        先刷检查点(needs/反思水位/时钟),再持世界锁用 SQLite backup API
        拷一致副本 —— 锁只挡拷贝那一瞬,打包在锁外进行。种子按
        显式 seed_path → 建库时存进 db_meta 的出生种子 → 内置种子(记警告)
        解析。分发纪律不变:密文(is_secret=1)在副本落地时即剥除。
        """
        if self._closed:
            raise RuntimeError("world is closed; use export_world_package offline instead")
        scheduler = self.scheduler
        with tempfile.TemporaryDirectory(prefix="anima_world-live-export-") as temp_dir:
            temp_db = Path(temp_dir) / "world.db"
            scheduler.checkpoint()
            with scheduler._lock:
                target = sqlite3.connect(temp_db)
                try:
                    scheduler.event_log.conn.backup(target)
                    target.execute("DELETE FROM config WHERE is_secret=1")
                    target.commit()
                finally:
                    target.close()
                genesis_row = scheduler.event_log.conn.execute(
                    "SELECT value FROM db_meta WHERE key='world_seed'"
                ).fetchone()
            if seed_path is not None:
                resolved_seed = Path(seed_path)
            elif genesis_row is not None:
                resolved_seed = Path(temp_dir) / "world_seed.json"
                resolved_seed.write_text(genesis_row[0], encoding="utf-8")
            else:
                import anima_world

                resolved_seed = Path(anima_world.__file__).parent / "world_seed.json"
                logger.warning(
                    "this database predates genesis-seed provenance (1.0.2); the exported "
                    "package carries the BUNDLED seed — pass seed_path to override"
                )
            return export_world_package(
                seed_path=resolved_seed,
                output_path=output_path,
                world_id=world_id,
                name=name,
                mode="snapshot",
                db_path=temp_db,
                beats_path=beats_path,
                summary=summary,
                genre=genre,
                setting=setting,
                theme=theme,
            )

    # ── 时钟 ────────────────────────────────────────────────────────────────

    def tick(self, n: int = 1) -> int:
        """手动推进 n 个 tick,返回当前时钟。适合测试与自定义宿主循环。"""
        for _ in range(n):
            with self._tick_lock:
                self.scheduler.tick()
                self._view.on_tick()
        return self.scheduler.clock

    def start_clock(self, fallback_tick_rate: float = 1.0) -> None:
        """后台线程按 `scheduler.tick_rate` 走时钟(热更新生效),并启动
        会话收割线程。已在走则 no-op。"""
        if self._clock_running:
            return
        if not (0 < fallback_tick_rate <= MAX_TICKS_PER_SECOND):
            raise ValueError(f"tick_rate must be > 0 and <= {MAX_TICKS_PER_SECOND}")
        self._clock_running = True
        config_store = self.scheduler.config_store

        def _loop() -> None:
            while self._clock_running and not self.scheduler._stopped:
                if self._paused:
                    time.sleep(0.1)
                    continue
                self.tick()
                # 睡眠切成 ≤0.5s 的片、每片重读速率:1 tick/5min 下一次
                # sleep(300) 会把热更新和优雅停机钉住五分钟。
                slept = 0.0
                while self._clock_running and not self.scheduler._stopped:
                    interval = 1.0 / _resolve_tick_rate(fallback_tick_rate, config_store)
                    if slept >= interval or self._paused:
                        break
                    step = min(0.5, interval - slept)
                    time.sleep(step)
                    slept += step

        def _reaper() -> None:
            # 独立线程:关会话要调 LLM 生成摘要,绝不许钉住时钟线程。
            while self._clock_running and not self.scheduler._stopped:
                slept = 0.0
                while (
                    self._clock_running
                    and not self.scheduler._stopped
                    and slept < _REAP_INTERVAL
                ):
                    time.sleep(0.5)
                    slept += 0.5
                if not self._clock_running or self.scheduler._stopped:
                    return
                try:
                    asyncio.run(self.session_manager.reap_idle())
                except Exception:  # reaper is best-effort
                    pass

        self._clock_thread = threading.Thread(target=_loop, daemon=True)
        self._clock_thread.start()
        self._reaper_thread = threading.Thread(target=_reaper, daemon=True)
        self._reaper_thread.start()

    def stop_clock(self) -> None:
        self._clock_running = False
        for thread in (self._clock_thread, self._reaper_thread):
            if thread is not None:
                thread.join(timeout=1.0)
        self._clock_thread = None
        self._reaper_thread = None

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    # ── 读世界 ──────────────────────────────────────────────────────────────

    def state(self) -> dict[str, Any]:
        """完整世界快照(角色/活动/地图/关系/叙事/runtime 诊断),
        `runtime.llm.degraded_reason` 常驻点名 Mock 降级的原因。"""
        state = self._view.snapshot()
        state["players"] = {pid: dict(p) for pid, p in self.players.items()}
        state["simulation"] = {
            "paused": self._paused,
            "tick_rate": _resolve_tick_rate(1.0, self.scheduler.config_store),
        }
        for aid, agent_state in state["agents"].items():
            need_values = self.needs(aid)
            if need_values:
                agent_state["needs"] = need_values
        return state

    def world_time(self) -> Any:
        return self.scheduler.world_time()

    def memories(self, agent_id: str) -> list[dict[str, Any]]:
        if self.scheduler.memory_store is None:
            return []
        return self.scheduler.memory_store.query(agent_id=agent_id)

    def retrieve_memories(
        self, agent_id: str, query: str | None = None, k: int = 5
    ) -> list[dict[str, Any]]:
        """memory-2.0 三因子检索(时近×重要×相关),命中即加固遗忘曲线。"""
        store = self.scheduler.memory_store
        if store is None:
            return []
        return store.retrieve(
            agent_id, now_tick=self.scheduler.clock, query=query, k=k,
            ticks_per_day=max(1, 1440 // int(self.config_get("world.minutes_per_tick", 5))),
        )

    def reflections(self, agent_id: str) -> list[dict[str, Any]]:
        """角色的反思(由记忆归纳出的洞察)。"""
        store = self.scheduler.memory_store
        if store is None:
            return []
        return store.query(agent_id=agent_id, kind="reflection")

    def needs(self, agent_id: str) -> dict[str, float]:
        """needs-v3:当前需求水平(energy/hunger/social/mood)。未点亮或
        首 tick 前返回空 dict。"""
        brain = self.scheduler.agents.get(agent_id)
        if brain is None:
            return {}
        bb = brain.agent.blackboard
        if bb.read("need.energy") is None:
            return {}
        return {
            key: bb.read(f"need.{key}")
            for key in ("energy", "hunger", "social", "mood")
        }

    def graph(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        """关系图谱三元组;`agent_id` 只看这个角色出发的边。

        边的 subject/object 带 `agent:` 前缀(图谱里也可能有别的实体),而这个
        参数收的是**裸角色 id**。此前它把裸 id 直接当 subject 查,于是永远查不
        到任何东西 —— 而且是返回空列表,宿主读成"这个角色没有任何关系",不是
        报错。前缀在这里补齐;已经带前缀的照单全收。
        """
        if self.scheduler.knowledge_graph is None:
            return []
        if agent_id is None:
            return self.scheduler.knowledge_graph.query()
        subject = agent_id if agent_id.startswith("agent:") else f"agent:{agent_id}"
        return self.scheduler.knowledge_graph.query(subject=subject)

    def cliques(self) -> list[dict[str, Any]]:
        """social-v5:小团体(friendship 连通分量,日切重算的派生缓存)。"""
        if self.scheduler.event_log is None:
            return []
        from anima_world.cliques import load_cliques

        with self.scheduler._lock:
            return load_cliques(self.scheduler.event_log.conn)

    def events(self, since_seq: int | None = None) -> list[dict[str, Any]]:
        """内存事件缓冲(近期);全量历史请离线读 events 表。"""
        return self._view.catchup_events(since_seq)

    def subscribe(self) -> "queue.Queue[dict[str, Any]]":
        """订阅事件推送(线程安全队列,批量帧 {type:'batch', events:[…]})。"""
        return self._view.subscribe()

    def unsubscribe(self, q: "queue.Queue[dict[str, Any]]") -> None:
        self._view.unsubscribe(q)

    def agent_context(self, agent_id: str, interlocutor_id: str) -> dict[str, Any]:
        """有界 grounding:记忆若干条 + 在场 + 关系。"""
        return self.world_context(agent_id, interlocutor_id)

    # ── 聊天与玩家 ──────────────────────────────────────────────────────────

    def chat(
        self,
        agent_id: str,
        messages: list[dict[str, str]],
        *,
        player_id: str,
        display_name: str | None = None,
        role: str = "player",
    ) -> Iterator[str]:
        """代玩家和角色聊一轮,流式产出回复文本块。

        `messages` 是调用方持有的近期对话(≤20 条,末条须 user);世界不落
        完整转录 —— 完整历史归宿主应用管。身份即参数(纪律 3)。
        """
        if agent_id not in self.scheduler.agents:
            raise KeyError(f"agent {agent_id} not found")
        if not messages or messages[-1].get("role") != "user":
            raise ValueError("messages must end with a user turn")
        agen = self.chat_service.respond(
            agent_id,
            messages[-20:],
            interlocutor_id=player_id,
            interlocutor={
                "display_name": display_name or f"player-{player_id[:8]}",
                "role": role,
            },
        )
        yield from _iterate_sync(agen)

    def chat_reply(self, *args: Any, **kwargs: Any) -> str:
        """chat() 的非流式便捷版,直接返回整段回复。"""
        return "".join(self.chat(*args, **kwargs))

    def record_chat_turn(
        self,
        agent_id: str,
        player_id: str,
        messages: list[dict[str, str]],
    ) -> int:
        """把一个已完成回合(user→assistant 恰好两条)记入世界并立即关闭:
        生成摘要、发一个 conversation 事件、触发关系判定。返回会话 id。

        与旧 chat-evolution 不同,这里没有投递回执 —— 进程内调用失败即异常,
        重试与否是调用方一行代码的事。
        """
        if agent_id not in self.scheduler.agents:
            raise KeyError(f"agent {agent_id} not found")
        if len(messages) != 2 or [m.get("role") for m in messages] != ["user", "assistant"]:
            raise ValueError("messages must be exactly one user turn then one assistant turn")
        player = self.players.get(player_id, {})
        location = str(player.get("location") or "") or None
        conversation_id = self.chat_store.start_conversation(
            agent_id,
            int(time.time()),
            participants=[
                {"id": player_id, "kind": "user"},
                {"id": agent_id, "kind": "agent"},
            ],
            location=location,
            player_id=player_id,
        )
        for message in messages:
            content = str(message.get("content") or "").strip()
            if not content:
                raise ValueError("chat message content cannot be empty")
            self.chat_store.add_message(
                conversation_id, message["role"], content, int(time.time())
            )
        asyncio.run(self.session_manager.close_conversation(conversation_id))
        # 交互即检查点:说完这句话的瞬间,db 就是完整的(可打包、可崩)。
        self.scheduler.checkpoint()
        return conversation_id

    def conversations(self, agent_id: str) -> list[dict[str, Any]]:
        return self.chat_store.list_conversations(agent_id)

    def conversation_messages(self, conversation_id: int) -> list[dict[str, Any]]:
        return self.chat_store.messages_for(conversation_id)

    def close_conversation(self, conversation_id: int) -> bool:
        """手动关闭会话(摘要 + 事件 + 判定);返回是否发了世界事件。"""
        emitted = asyncio.run(self.session_manager.close_conversation(conversation_id))
        self.scheduler.checkpoint()  # 交互即检查点
        return emitted

    def player_move(self, player_id: str, location: str, *, role: str = "player") -> None:
        """玩家移动到某个 point 地点。未知地点抛 KeyError。"""
        location = location.strip()
        if not location:
            raise ValueError("location is required")
        if self.scheduler.location_store is not None:
            row = self.scheduler.location_store.get(location)
            if row is None or row.get("kind", "point") != "point":
                raise KeyError(f"没有 {location} 这个地方")
        self.players[player_id] = {"role": role, "location": location}

    def player_action(
        self,
        player_id: str,
        action: str,
        details: dict[str, Any] | None = None,
        *,
        role: str = "player",
    ) -> None:
        """玩家动作,落一条 player_action 事件。"""
        action = action.strip()
        if not action:
            raise ValueError("action is required")
        player = self.players.get(player_id, {})
        with self.scheduler._lock:
            self._record_and_fan({
                "type": "player_action",
                "who": f"player:{player_id}",
                "player_id": player_id,
                "role": role,
                "loc": player.get("location"),
                "action": action,
                "details": dict(details or {}),
            })
            self.scheduler.checkpoint()  # 交互即检查点(RLock,可重入)

    # ── 经济(economy-v4) ──────────────────────────────────────────────────

    def balance(self, holder: str) -> float:
        """余额(事件账本的投影)。holder 可以是角色 id、`player:<id>`、`__town__`。"""
        with self.scheduler._lock:
            return float(self.scheduler._memory_projection.balances.get(holder, 0.0))

    def inventory(self, holder: str) -> dict[str, int]:
        with self.scheduler._lock:
            return dict(self.scheduler._memory_projection.inventories.get(holder, {}))

    def shop(self, location_id: str) -> list[dict[str, Any]]:
        """某地货架:物品、现价、库存。"""
        if self.scheduler.event_log is None:
            return []
        with self.scheduler._lock:
            rows = self.scheduler.event_log.conn.execute(
                "SELECT s.item_id, d.name, d.kind, s.price, s.quantity FROM shop_stock s"
                " JOIN item_defs d ON d.id = s.item_id WHERE s.location_id = ?"
                " ORDER BY s.item_id",
                (location_id,),
            ).fetchall()
        return [
            {"item_id": r[0], "name": r[1], "kind": r[2], "price": float(r[3]), "quantity": int(r[4])}
            for r in rows
        ]

    def player_topup(self, player_id: str, amount: float) -> float:
        """宿主给玩家钱包充值(玩家钱包是在场状态,持久化到 v7 才入库)。"""
        if amount <= 0:
            raise ValueError("amount must be positive")
        player = self.players.setdefault(player_id, {"role": "player", "location": None})
        player["wallet"] = float(player.get("wallet", 0.0)) + float(amount)
        return player["wallet"]

    def player_buy(self, player_id: str, location_id: str, item_id: str) -> dict[str, Any]:
        """玩家买货:钱包扣款、货架减一,payment + item_transfer 事件入账本。"""
        from anima_world import economy

        if self.scheduler.event_log is None:
            raise ValueError("economy needs a persistent world")
        player = self.players.get(player_id)
        if player is None:
            raise KeyError(f"player {player_id} not present")
        with self.scheduler._lock:
            conn = self.scheduler.event_log.conn
            row = conn.execute(
                "SELECT price, quantity FROM shop_stock WHERE location_id = ? AND item_id = ?",
                (location_id, item_id),
            ).fetchone()
            if row is None or int(row[1]) <= 0:
                raise KeyError(f"{location_id} 没有 {item_id} 的货")
            price = float(row[0])
            wallet = float(player.get("wallet", 0.0))
            if wallet < price:
                raise ValueError(f"钱包不够:{wallet} < {price}")
            if not economy.take_stock(conn, location_id, item_id):
                raise KeyError(f"{location_id} 没有 {item_id} 的货")
            player["wallet"] = wallet - price
            self.scheduler._shop_sales[(location_id, item_id)] = (
                self.scheduler._shop_sales.get((location_id, item_id), 0) + 1
            )
            holder = f"player:{player_id}"
            self._record_and_fan({
                "type": "payment", "who": holder, "loc": location_id,
                "payload": {"from": holder, "to": economy.TOWN, "amount": price,
                            "reason": f"purchase:{item_id}"},
            })
            self._record_and_fan({
                "type": "item_transfer", "who": holder, "loc": location_id,
                "payload": {"from": f"shop:{location_id}", "to": holder,
                            "item_id": item_id, "qty": 1},
            })
            self.scheduler.checkpoint()  # 交互即检查点
        return {"item_id": item_id, "price": price, "wallet": player["wallet"]}

    # ── 配置与提示词 ────────────────────────────────────────────────────────

    def config_list(self, category: str | None = None, *, mask: bool = True) -> list[dict[str, Any]]:
        store = self.scheduler.config_store
        if store is None:
            return []
        rows = []
        for row in store.list(category=category):
            value = row["value"]
            if mask and row["is_secret"] and value:
                value = mask_secret(value)
            rows.append({**row, "value": value})
        return rows

    def config_get(self, key: str, default: Any = None) -> Any:
        store = self.scheduler.config_store
        return default if store is None else store.get(key, default=default)

    def config_set(self, key: str, value: Any) -> None:
        """按声明类型强转后写入,立即生效。未知键抛 KeyError。"""
        store = self.scheduler.config_store
        if store is None or not store.has(key):
            raise KeyError(f"config key {key} not found")
        meta = store.meta(key)
        if meta["is_secret"] and value == "":
            raise ValueError("secret value cannot be set to empty")
        value_type = meta["value_type"]
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
        if key == "scheduler.tick_rate" and not (0 < float(value) <= MAX_TICKS_PER_SECOND):
            raise ValueError(f"'{key}' must be > 0 and <= {MAX_TICKS_PER_SECOND}")
        store.set(key, value)

    def prompt_list(self) -> list[dict[str, Any]]:
        store = self.scheduler.prompt_store
        return [] if store is None else store.list()

    def prompt_set(self, name: str, template: str) -> None:
        """改提示词模板,保存前试渲染(占位符错误抛 PromptRenderError)。"""
        store = self.scheduler.prompt_store
        if store is None or not store.has(name):
            raise KeyError(f"prompt {name} not found")
        store.set(name, template)

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _record_and_fan(self, event: dict[str, Any]) -> None:
        self.scheduler._record_event(event)
        self._view._fan_out()

    def _persona(self, agent_id: str) -> dict[str, Any]:
        brain = self.scheduler.agents.get(agent_id)
        if brain is None:
            return {}
        agent = brain.agent
        return {
            "name": agent.name,
            "personality": agent.blackboard.read("personality") or "",
            "location": agent.location,
        }

    def world_context(self, agent_id: str, interlocutor_id: str) -> dict[str, Any]:
        """chat-grounding:锁内一次快照角色的 lived state(只读,无 LLM 无 IO)。"""
        from anima_world.memory_triggers import BAND_NAMES, band

        scheduler = self.scheduler
        config_store = scheduler.config_store
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
                    store = scheduler.memory_store
                    if hasattr(store, "retrieve"):
                        # memory-2.0: three-factor retrieval; the interlocutor's
                        # name is the query, so "who I'm talking to" pulls the
                        # memories about them. Retrieval reinforces — chatting
                        # about something keeps it remembered.
                        rows = store.retrieve(
                            agent_id, now_tick=scheduler.clock,
                            query=interlocutor_id, k=int(k),
                        )
                    else:
                        rows = store.query(agent_id=agent_id)
                        rows.sort(key=lambda m: (-float(m.get("importance") or 0), -int(m.get("tick") or 0)))
                        rows = rows[: int(k)]
                    ctx["memories"] = [m["summary"] for m in rows]
                except Exception:  # noqa: BLE001 - memories are flavor, never fatal
                    pass
            now = scheduler.world_time()
            loc_id = brain.agent.blackboard.read("loc") or brain.agent.location or ""
            loc_name = loc_id
            if scheduler.location_store is not None and loc_id:
                row = scheduler.location_store.get(loc_id)
                if row is not None:
                    loc_name = row.get("name") or loc_id
            activity = self._view._agent_activity(agent_id)
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


def _iterate_sync(agen: Any) -> Iterator[str]:
    """把 async 生成器桥成同步迭代器(库门面是同步世界,聊天流式在
    调用方线程上消费;私有事件循环,不与宿主的 asyncio 纠缠)。"""
    loop = asyncio.new_event_loop()
    try:
        while True:
            try:
                yield loop.run_until_complete(agen.__anext__())
            except StopAsyncIteration:
                return
    finally:
        try:
            loop.run_until_complete(agen.aclose())
        except Exception:  # noqa: BLE001 - closing an exhausted generator is best-effort
            pass
        loop.close()
