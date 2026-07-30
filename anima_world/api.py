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
from typing import Any, AsyncIterator, Iterator

from anima_world import tools as tools_mod
from anima_world.actions import ActionDescriptor
from anima_world.chat_service import ChatService
from anima_world.chat_session import ChatSessionManager
from anima_world.chat_state import ChatStateStore
from anima_world.chat_store import ChatStore
from anima_world.config_store import mask_secret
from anima_world.intent import Director
from anima_world.llm_client import (
    create_background_llm_client_from_config,
    create_llm_client_from_config,
    create_llm_client_from_env,
)
from anima_world.locations import DEFAULT_POINTS
from anima_world.narrative import MockNarrativeProvider, OpenAICompatibleNarrativeProvider
from anima_world.scheduler import MAX_TICKS_PER_SECOND, Scheduler
from anima_world.types import AgentState, Projection
from anima_world.world_package import WorldPackageManifest, export_world_package
from anima_world.world_time import DEFAULT_MINUTES_PER_TICK

logger = logging.getLogger(__name__)

# The idle reaper scans for stale conversations this often (wall seconds).
_REAP_INTERVAL = 30.0

# `history()` 一页的硬上限。存在的理由不是"省内存",是**不让一次调用把整个世界的
# 历史拉进一个 list 里同时还在锁内** —— 事件日志只增不减,一个跑了一年的世界那样
# 做会把宿主卡死。要全量就分页,分页是可见的。
_HISTORY_MAX_PAGE = 5000

# 一个玩家多久没动静就当他走了(墙钟秒)。**不是心跳契约**:任何一次交互都算
# "我还在",宿主什么也不用维护。它只是那道兜底闸 —— 宿主忘了调 `player_leave`
# 时,世界不该留下一个永久站在咖啡店里的幽灵。
_PLAYER_TTL_SECONDS = 15 * 60

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
            # `llm.degraded_reason` 说的是"现在";这里说的是"一路上怎么样"。
            # 一个整整三天没有 planner 的世界,和一个角色确实无所事事的世界,产物
            # 看起来一模一样 —— 只有计数能把它们分开。档位切换还会落一条
            # `subsystem_health` 事件,所以它同时也是可查的历史,不是一行会滚掉的日志。
            "subsystems": self.scheduler.subsystem_health(),
        }


class _BridgeLoop:
    """世界自己的一条事件循环线程:同步门面上的所有异步工作都跑在它上面。

    为什么必须是同一条循环:LLM 客户端底层是一个 `httpx.AsyncClient`,连接池绑在
    创建它的那个循环上。原来的桥每调一次就 `asyncio.run()` 开一个新循环再关掉,
    而客户端是被缓存复用的 —— 于是**一个属于已关闭循环的连接池被后面每一次调用
    继续用**。2026-07-29 用真模型跑一局时,每一轮都在刷
    `Task was destroyed but it is pending` 与 `aclose was never awaited`;真正的
    危险是它某天会变成 `Event loop is closed`,而那时表现是"聊天忽然全炸"。

    顺带一个真实的好处:连接与 TLS 握手能复用。那一局里第一轮 31 秒、之后 7~20 秒,
    差的那一截里有一部分就是每轮重连。

    线程是 daemon,`World.close()` 会收掉它。语义与原来的桥逐字相同:调用方阻塞到
    结果出来,流式仍然一段一段地回。
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="anima-chat-loop"
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro: Any) -> Any:
        """跑一个协程,阻塞到它跑完。"""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def iterate(self, agen: Any) -> Iterator[Any]:
        """同步地迭代一个 async 生成器 —— 一次取一个,所以流式不退化成"等全部"。"""
        try:
            while True:
                try:
                    yield asyncio.run_coroutine_threadsafe(
                        agen.__anext__(), self._loop
                    ).result()
                except StopAsyncIteration:
                    return
        finally:
            # 调用方提前不要了(break / 异常)时,生成器要在**它自己的循环上**关掉,
            # 否则那条 HTTP 流会挂在那里,等 GC 来抱怨。
            try:
                asyncio.run_coroutine_threadsafe(agen.aclose(), self._loop).result(5)
            except Exception:  # noqa: BLE001 - 关一个已经结束的生成器是 best-effort
                pass

    def close(self) -> None:
        if self._loop.is_closed():
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._loop.shutdown_asyncgens(), self._loop
            )
            fut.result(5)
        except Exception:  # noqa: BLE001
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        if not self._thread.is_alive():
            self._loop.close()


class AgentUnavailable(RuntimeError):
    """她这会儿不理这个人(软静音 / 等会儿再说 —— issue #15)。

    抛而不是"返回一句空回复":静默的空回复在宿主那边和"LLM 挂了"长得一模一样,
    而这两件事该给玩家看到完全不同的东西(「她五分钟后再理你」对「服务出错了」)。
    `kind` / `seconds_left` / `reason` 都带在异常上,UI 直接用。
    """

    def __init__(self, agent_id: str, player_id: str, quiet: dict[str, Any]) -> None:
        self.agent_id = agent_id
        self.player_id = player_id
        self.kind = quiet.get("kind", "mute")
        self.expires_at = int(quiet.get("expires_at", 0))
        self.seconds_left = int(quiet.get("seconds_left", 0))
        self.reason = quiet.get("reason")
        minutes = max(1, round(self.seconds_left / 60))
        label = "不想理你" if self.kind == "mute" else "说了等会儿再聊"
        super().__init__(f"{agent_id} 现在{label}(还有约 {minutes} 分钟)")


class _ToolRuntime:
    """工具与 director 能碰到的世界(`tools.base.ToolRuntime` 的实现)。

    存在的理由是**方向**:聊天子系统不认识调度器,只认识这个对象;世界也不认识
    聊天,只被这个对象调用。工具因此能真改世界(走开、广播)而不用把 `World`
    整个传进 chat_service。
    """

    def __init__(self, world: "World") -> None:
        self._world = world

    @property
    def state(self) -> ChatStateStore:
        return self._world.chat_state

    def tick(self) -> int:
        return int(self._world.scheduler.clock)

    def now(self) -> int:
        return int(time.time())

    def ticks_for_minutes(self, minutes: float) -> int:
        """墙钟分钟 → 世界 tick 数。演示速度与实时速度下都成立。"""
        mpt = DEFAULT_MINUTES_PER_TICK
        config_store = self._world.scheduler.config_store
        if config_store is not None:
            mpt = config_store.get("world.minutes_per_tick", default=mpt)
        return max(1, int(round(float(minutes) / max(1, int(mpt)))))

    def config(self, key: str, default: Any = None) -> Any:
        config_store = self._world.scheduler.config_store
        if config_store is None:
            return default
        value = config_store.get(key, default=default)
        return default if value is None else value

    def emit(self, event: dict[str, Any]) -> None:
        with self._world.scheduler._lock:
            self._world._record_and_fan(event)
            self._world.scheduler.checkpoint()  # 交互即检查点(RLock,可重入)

    def agent_ids(self) -> list[str]:
        return list(self._world.scheduler.agents)

    def agent_names(self) -> dict[str, str]:
        return {
            aid: brain.agent.name or aid
            for aid, brain in self._world.scheduler.agents.items()
        }

    def agent_location(self, agent_id: str) -> str:
        brain = self._world.scheduler.agents.get(agent_id)
        if brain is None:
            return ""
        return str(brain.agent.blackboard.read("loc") or brain.agent.location or "")

    def face_to_face(self, agent_id: str, player_id: str) -> bool:
        """她和这个玩家此刻是面对面,还是隔着手机?

        判定与身份声明那一段共用同一条规矩(`chat_service.respond`):同地、且她不在
        途中。宿主没调过 `player_move` 就是没告诉世界他在哪 —— 一律按不在场,引擎不猜。
        """
        if agent_id in self._world.scheduler._transit:
            return False  # 在途不算在场,与 `_colocated_agents` 同一条规矩
        here = self.agent_location(agent_id)
        where = str((self._world.players.get(player_id) or {}).get("location") or "").strip()
        return bool(here) and bool(where) and here == where

    def point_ids(self) -> list[str]:
        store = self._world.scheduler.location_store
        if store is None:
            return sorted(str(row["id"]) for row in DEFAULT_POINTS)
        return sorted(
            str(row["id"]) for row in store.all()
            if (row or {}).get("kind", "point") == "point"
        )

    def move_agent(self, agent_id: str, location: str) -> dict[str, Any]:
        """让一个角色真的动起来 —— 走的是 BT 走的那条路。

        因此"她走了"和"她自己决定走了"在世界里是**同一件事**:一样发 travel /
        location_join 事件,一样要走路花时间。提示词里塞一句"她走了"是另一回事,
        那种"她"下一 tick 还站在原地。
        """
        scheduler = self._world.scheduler
        brain = scheduler.agents.get(agent_id)
        if brain is None:
            raise tools_mod.ToolCallError(f"没有 {agent_id} 这个人")
        store = scheduler.location_store
        if store is not None:
            row = store.get(location)
            if row is None or row.get("kind", "point") != "point":
                raise tools_mod.ToolCallError(f"没有 {location} 这个地方")
        with scheduler._lock:
            scheduler.emit_action(brain.agent, ActionDescriptor("walk", {"location": location}))
            trip = scheduler._transit.get(agent_id)
            scheduler.checkpoint()
        if trip is not None:
            return {"in_transit": True, "arrive_at": int(trip.get("arrive_at", 0))}
        return {"in_transit": False, "location": self.agent_location(agent_id)}

    def close_conversation(self, agent_id: str, player_id: str) -> bool:
        active = self._world.chat_store.active_conversation(agent_id, player_id=player_id)
        if active is None:
            return False
        return self._world.close_conversation(int(active["id"]))


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
        # 同步门面的异步工作全跑在这一条循环上(见 `_BridgeLoop`:一个跨循环复用的
        # HTTP 连接池是"每轮泄一条连接,某天忽然全炸"那种坏)。
        self._bridge = _BridgeLoop()
        self.chat_store = ChatStore(conn, lock=scheduler._lock)
        # chat-agent(1.3.0):一轮聊天要读写的当前值(stance / 静音 / 拒谈话题 /
        # 玩家教的规则)。和 ChatStore 共用同一个连接与同一把锁。
        self.chat_state = ChatStateStore(conn, lock=scheduler._lock)
        self._tool_runtime = _ToolRuntime(self)
        self._director = Director(self._tool_runtime)
        scheduler.chat_state = self.chat_state
        config_store = scheduler.config_store
        chat_llm = (
            create_llm_client_from_config(config_store)
            if config_store is not None
            else create_llm_client_from_env()
        )
        background_llm = (
            create_background_llm_client_from_config(config_store)
            if config_store is not None
            else chat_llm
        )
        self.chat_service = ChatService(
            store=self.chat_store,
            llm=chat_llm,
            persona_provider=self._persona,
            config_store=config_store,
            prompt_store=scheduler.prompt_store,
            world_provider=self.world_context,
            state_store=self.chat_state,
            tool_runtime=self._tool_runtime,
            background_llm=background_llm,
        )
        self.session_manager = ChatSessionManager(
            store=self.chat_store,
            llm=chat_llm,
            emit_event=self._record_and_fan,
            config_store=config_store,
            prompt_store=scheduler.prompt_store,
            judge_hook=lambda info: scheduler.submit_user_chat_judgment(**info),
            meta_provider=self.chat_state.conversation_meta,
        )
        # 在场玩家(刻意内存态:重启即新访;持久的部分——会话/记忆/关系——在 db 里)
        self.players: dict[str, dict[str, Any]] = {}
        self.player_ttl_seconds: float = _PLAYER_TTL_SECONDS
        # 让世界看得见在场的玩家(issue #13,访客模型)。scheduler 不认识 World,
        # 只认识这个回调;回调按 TTL 过滤,所以角色不会去敲断线三小时的人的门。
        self.scheduler._present_players = lambda: {
            pid: self.players[pid] for pid in self.who_is_present()
        }

        # 开机补完:会话只在 record_chat_turn 一次调用内开与关,且运行中的
        # 世界独占 db,所以此刻还 open 的行只能是上次崩溃的遗留。消息早已
        # 逐条落盘,补上总结与那一个 conversation 事件即可 —— 崩溃从
        # "丢总结"降级为"总结晚到"。
        # 盖一个"这个世界正被我跑着"的戳。CLAUDE.md 的第一条不变量此前没有任何
        # 标记去支撑 —— 谁也看不出一个 db 正被人跑着,而第二个写它的进程会让两边
        # 立刻分叉。是提示不是锁:进程崩掉标记就陈旧,拿陈旧标记拒绝操作,等于在
        # 真出事那天把人挡在门外。
        self.scheduler.claim_ownership()

        try:
            orphans = self._bridge.run(self.session_manager.reap_orphans())
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
        self.scheduler.release_ownership()   # 撤戳要在 stop 之前:stop 之后连接可能已经关了
        self.scheduler.stop(wait=wait)
        self._bridge.close()   # 收掉那条循环线程(以及挂在它上面的 HTTP 连接)

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

    def fast_forward(self, ticks: int, *, plan_wait_cap: float | None = None) -> dict[str, Any]:
        """无头快进,并在每个世界日等在途的规划落地。

        与 `tick(n)` 的区别只有"等规划"这一条:快进会在一次 LLM 调用的时间里烧掉
        几千个 tick,不等的话第 D 天要的计划装回来时第 D 天早过去了。

        返回 `{"ticks", "clock", "planner_gave_up", "exhausted_days"}` 而不是一个 int
        —— **`planner_gave_up` 才是宿主真正需要的那个字**:一个安静的世界和一个规划
        全程没跟上的世界,产物看起来一模一样。`plan_wait_cap<=0` 是"不等",不是判死。

        这条路径与 `anima-world simulate` 共用同一份实现。
        """
        with self._tick_lock:
            outcome = self.scheduler.fast_forward(ticks, plan_wait_cap=plan_wait_cap)
            self._view.on_tick()
        return outcome

    def report(self, *, ticks: int | None = None) -> dict[str, Any]:
        """把这个世界跑出来的历史读成一份运行摘要(与 `simulate --report` 同一口径)。

        纯读:事件日志是唯一输入,`sim_report` 是纯函数。`ticks` 缺省用当前时钟。

        ⚠️ 叙事与关系判定跑在线程池上,**没排干之前尾部是缺的** —— 一份刚跑完就取的
        摘要会少掉最后几条叙事与判定,而那正是"三日试炼"最关心的尾巴。要一份完整的,
        先 `close()` 再离线算,或者先 `wait_planning_idle()`。
        """
        from anima_world.sim_report import build_run_report
        from anima_world.world_time import DEFAULT_MINUTES_PER_TICK

        mpt = DEFAULT_MINUTES_PER_TICK
        if self.scheduler.config_store is not None:
            mpt = self.scheduler.config_store.get("world.minutes_per_tick", default=mpt)
        with self.scheduler._lock:
            events = (
                self.scheduler.event_log.replay()
                if self.scheduler.event_log is not None else []
            )
        return build_run_report(
            events,
            ticks=self.scheduler.clock if ticks is None else int(ticks),
            minutes_per_tick=int(mpt),
        )

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
                    self._bridge.run(self.session_manager.reap_idle())
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
        """内存事件缓冲(**近期 200 条的窗口,不是历史**);全量历史用 `history()`。

        窗口滑过 `since_seq` 时会打一条 warning —— 那正是调用方即将拿到一段有洞的
        历史、却以为自己追上了的时刻。返回值是个普通 list,看不出中间少了什么。
        """
        if since_seq is not None:
            with self.scheduler._lock:
                window = self.scheduler.recent_events
                oldest = int(window[0].get("seq", 0) or 0) if window else 0
            if oldest and int(since_seq) < oldest - 1:
                logger.warning(
                    "events(since_seq=%s):内存窗口只剩 seq >= %s,中间 %s 条已经滑出去了 "
                    "—— 这次返回的是一段有洞的历史。要全量请用 World.history()。",
                    since_seq, oldest, oldest - 1 - int(since_seq),
                )
        return self._view.catchup_events(since_seq)

    def history(
        self,
        *,
        since_seq: int = 0,
        limit: int = 1000,
        who: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """全量事件历史,**分页**。事件形状与 `events()` 完全一致。

        `{"events": [...], "next_seq": int | None, "total": int}` —— `next_seq` 不是
        None 就代表后面还有,把它当 `since_seq` 再要一页。

        为什么是分页而不是"给你前 N 条":一份少了一截的历史看起来和完整的一模一样,
        宿主拿它做统计不会有任何报错。截断必须结构性地可见。`total` 是**满足过滤条件
        的全部条数**(不受 `since_seq` 影响),让调用方一眼看出这趟要拉多少。

        `who` / `kind` 是可选过滤。注意 `events` 表今天只有 `ts` / `type` 索引,
        按 `who` 过滤是全表扫 —— 分页上限就是它的护栏。

        ⚠️ **活着的世界是移动目标**:叙事跑在线程池上,分页期间还会有新事件落库。
        分页保证 seq 有序不重不漏,但"开始分页时的 total"和"读完时的条数"本就可能
        对不上。要一份静止的历史,先 `close()`。
        """
        from anima_world.events import Event

        limit = max(1, min(int(limit), _HISTORY_MAX_PAGE))
        filters: list[str] = []
        filter_params: list[Any] = []
        if who:
            filters.append("who = ?")
            filter_params.append(who)
        if kind:
            filters.append("type = ?")
            filter_params.append(kind)
        filter_clause = (" WHERE " + " AND ".join(filters)) if filters else ""
        page_clause = " WHERE " + " AND ".join(["seq > ?", *filters])

        conn = self.scheduler.event_log.conn
        with self.scheduler._lock:
            total = conn.execute(
                f"SELECT COUNT(*) FROM events{filter_clause}", filter_params
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT seq, ts, type, who, loc, payload FROM events"
                f"{page_clause} ORDER BY seq ASC LIMIT ?",
                (int(since_seq), *filter_params, limit + 1),  # +1 = "还有没有下一页"
            ).fetchall()

        page = [Event.from_row(row) for row in rows[:limit]]
        events = [
            self.scheduler._stream_event({
                "seq": e.seq, "ts": e.ts, "type": e.type,
                "who": e.who, "loc": e.loc, "payload": e.payload,
            })
            for e in page
        ]
        return {
            "events": events,
            "next_seq": events[-1]["seq"] if len(rows) > limit and events else None,
            "total": int(total),
        }

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
        meta: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        """代玩家和角色聊一轮,流式产出回复文本块。

        `messages` 是调用方持有的近期对话(≤20 条,末条须 user);世界不落
        完整转录 —— 完整历史归宿主应用管。身份即参数(纪律 3)。

        `meta` 是可选的收件盘(chat-agent):流耗尽后里面是这一轮的
        `stance` / `intent` / `tool_calls` / `end_conversation`,可以原样交回
        `record_chat_turn(..., meta=…)` 落到消息行上。不给就丢弃。

        她这会儿不理这个人时抛 `AgentUnavailable` —— 静默的空回复在宿主那边和
        "LLM 挂了"长得一样,而这两件事该让玩家看到完全不同的东西。
        """
        yield from self._bridge.iterate(
            self._chat_agen(agent_id, messages, player_id=player_id,
                            display_name=display_name, role=role, meta=meta)
        )

    async def achat(
        self,
        agent_id: str,
        messages: list[dict[str, str]],
        *,
        player_id: str,
        display_name: str | None = None,
        role: str = "player",
        meta: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """`chat()` 的原生 async 版本 —— 直接跑在宿主自己的事件循环上。

        同步的 `chat()` 在 async 宿主里也能用(桥会换个线程跑),但流式转发本来就是
        async 的形状,包一层再拆开只是白绕。参数与 `chat()` 逐字相同。

        注意别把它当成"整个门面都有 async 版":`record_chat_turn` 等要抢
        scheduler 那把系统唯一的 RLock,把等锁搬上宿主的事件循环会在 tick 持锁期间
        卡死整个宿主。要非阻塞就用 `asyncio.to_thread(world.record_chat_turn, …)`。
        """
        async for token in self._chat_agen(
            agent_id, messages, player_id=player_id,
            display_name=display_name, role=role, meta=meta,
        ):
            yield token

    def _chat_prelude(
        self,
        agent_id: str,
        messages: list[dict[str, str]],
        *,
        player_id: str,
        display_name: str | None,
        role: str,
    ) -> dict[str, Any]:
        """一轮聊天开口之前要办的事:校验、静音闸、身份、在场。

        返回 `{"interlocutor": …, "user_text": …}`。`chat` / `chat_burst` 共用 ——
        两条路上的静音与身份判定必须是同一份,不然一条路上守住的边界在另一条上漏。
        """
        if agent_id not in self.scheduler.agents:
            raise KeyError(f"agent {agent_id} not found")
        if not messages or messages[-1].get("role") != "user":
            raise ValueError("messages must end with a user turn")
        # 软静音(#15):世界当场拒,消息不进 LLM。硬静音(锁输入框)由宿主按
        # `mute_started` 事件自己决定 —— 引擎不替 UI 做主。
        quiet = self.chat_state.quiet_until(agent_id, player_id)
        if quiet is not None:
            raise AgentUnavailable(agent_id, player_id, quiet)
        interlocutor: dict[str, str] = {
            "display_name": display_name or f"player-{player_id[:8]}",
            "role": role,
        }
        # 记住这个玩家叫什么。记忆文本里写的是名字,不是 id —— 检索 query 用得上
        # (见 world_context)。身份即参数,所以世界只在被告知时才知道。
        self._touch_player(player_id, display_name=interlocutor["display_name"])
        self.players[player_id].setdefault("role", role)
        # 玩家在哪,决定角色是"看见你"还是"收到你的消息":`chat_service.respond`
        # 按这个字段在面对面/手机私聊两段身份声明里选一段。这里曾经不传,于是
        # 面对面那一支经门面永远不可达 —— CLI 明明先把你走到她跟前(player_move),
        # 提示词照样告诉她你不在场,并禁止她描写看见你。
        # 宿主没调过 `player_move` 就是没告诉世界你在哪:不猜,维持手机私聊。
        where = str((self.players.get(player_id) or {}).get("location") or "").strip()
        if where:
            interlocutor["location"] = where
            interlocutor["location_name"] = self._location_display_name(where)
        return {
            "interlocutor": interlocutor,
            "user_text": str(messages[-1].get("content") or ""),
        }

    def _present_names(self, agent_id: str) -> list[str]:
        """这场对话里角色看得见的人 —— 分类器要知道"林素在不在场"。"""
        here = self._tool_runtime.agent_location(agent_id)
        names = self._tool_runtime.agent_names()
        return [
            name for aid, name in names.items()
            if aid != agent_id and self._tool_runtime.agent_location(aid) == here
        ]

    async def _dispatch_intent(
        self, agent_id: str, player_id: str, user_text: str, history: list[dict[str, str]]
    ) -> dict[str, Any]:
        """意图分类 + 三条 handler 的分派(issue #16)。

        返回 `{"intent", "confidence", "handled", "text", "detail"}`。`handled=True`
        表示这条消息已经被 style / narrative 那条路处理掉了,不该再走 in-character
        生成;`text` 是那条路要回给玩家的一句。

        分类器不通、置信度不够、handler 拒绝 —— 一律退回 dialogue,并把原因带上。
        """
        from anima_world.chat_state import OVERRIDE_KINDS

        outcome: dict[str, Any] = {"handled": False, "text": ""}
        verdict = await self.chat_service.classify(
            user_text, present=self._present_names(agent_id), recent=history[-5:],
        )
        outcome.update(verdict.to_dict())
        if verdict.intent == "style_adjust":
            kind = str((verdict.params or {}).get("kind") or "").strip()
            value = str((verdict.params or {}).get("value") or "").strip()
            if kind not in OVERRIDE_KINDS or not value:
                logger.warning(
                    "style_adjust 的参数不完整(kind=%r value=%r)—— 这条退回按对话处理",
                    kind, value,
                )
                outcome["intent"] = "dialogue"
                outcome["reason"] = "style_adjust 少了 kind/value,按对话处理"
                return outcome
            self.chat_state.set_override(agent_id, player_id, kind, value)
            outcome["handled"] = True
            # 轻确认,不 in-character:这一句是"规则已记下",不是她在说话。
            outcome["text"] = f"（记下了:{OVERRIDE_KINDS[kind]} —— {value}。）"
            outcome["detail"] = {"kind": kind, "value": value}
            return outcome
        if verdict.intent == "narrative_direction":
            directed = self._director.direct(agent_id=agent_id, params=verdict.params or {})
            outcome["handled"] = True
            outcome["text"] = directed.text
            outcome["detail"] = dict(directed.detail)
            outcome["ok"] = directed.ok
            return outcome
        return outcome

    async def _intent_prelude(
        self,
        agent_id: str,
        player_id: str,
        user_text: str,
        messages: list[dict[str, str]],
        sink: dict[str, Any],
    ) -> str | None:
        """分类 + 分派,把结果写进 `sink`。返回一句"这条已经处理掉了"的回话,或 None。

        `chat` 与 `chat_burst` **共用这一份**:两条路上的分派必须是同一份,否则点亮了
        `chat.intent.enabled` 之后,走连续输出那条路的世界会静默地不分类 —— 玩家教的
        "以后叫我霜霜" 永远不落库,而表面上一切照跑。

        是 async 的,因为分类是一次真的 LLM 往返:在 `achat` 那条路上同步阻塞地等它,
        会把宿主的事件循环按住好几秒(FastAPI 的处理函数就是 async def,而 README 把
        "嵌入到应用里"写成主要用法)。
        """
        if not self.chat_service.intent_enabled():
            return None
        verdict = await self._dispatch_intent(agent_id, player_id, user_text, list(messages))
        sink["intent"] = verdict.get("intent")
        sink["intent_confidence"] = verdict.get("confidence")
        if verdict.get("reason"):
            sink["intent_reason"] = verdict["reason"]
        if verdict.get("detail"):
            sink["intent_detail"] = verdict["detail"]
        if not verdict.get("handled"):
            return None
        sink["handled_by"] = verdict.get("intent")
        return str(verdict.get("text") or "")

    async def _chat_agen(
        self,
        agent_id: str,
        messages: list[dict[str, str]],
        *,
        player_id: str,
        display_name: str | None,
        role: str,
        meta: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """`chat` / `achat` 共用的那一份:开口前的那一套,然后交给 chat_service。"""
        prelude = self._chat_prelude(
            agent_id, messages, player_id=player_id, display_name=display_name, role=role,
        )
        sink: dict[str, Any] = meta if meta is not None else {}
        extra_system: list[str] = []
        handled = await self._intent_prelude(
            agent_id, player_id, prelude["user_text"], messages, sink
        )
        if handled is not None:
            # style / narrative 走完了自己那条路:回一句确认,不再 in-character
            # 生成。narrative 的后果已经在世界里(她下一次读 world_context 会真的
            # 看到那个人在场),不是提示词里的一句想象。
            if handled:
                yield handled
            return
        topic_block = self.chat_service.refused_topic_block(agent_id, prelude["user_text"])
        if topic_block:
            extra_system.append(topic_block)
        async for token in self.chat_service.respond(
            agent_id,
            messages[-20:],
            interlocutor_id=player_id,
            interlocutor=prelude["interlocutor"],
            meta=sink,
            extra_system=extra_system,
        ):
            yield token

    def chat_burst(
        self,
        agent_id: str,
        messages: list[dict[str, str]],
        *,
        player_id: str,
        display_name: str | None = None,
        role: str = "player",
        interrupt_check: Any | None = None,
    ) -> Iterator[dict[str, Any]]:
        """连着说到她自己想停(issue #17)。产出结构化的步骤,不是一整段文本。

        `chat()` 是回合制:你一句 → 她一段 → 停。这条路是 agent 那个形状:她可以
        一口气说三条、中间去做件事、问你一句然后停下等你。每一步产出一个 dict
        (`kind` 为 `budget` / `text` / `message` / `stance` / `tool_call` / `stop`),
        宿主可以逐条弹出来,也可以只取 `message`。

        `chat.loop.enabled` 关着时它只跑一步 —— 形状仍是这个形状,所以宿主不用写
        两套消费代码。
        """
        yield from self._bridge.iterate(
            self._chat_burst_agen(
                agent_id, messages, player_id=player_id, display_name=display_name,
                role=role, interrupt_check=interrupt_check,
            )
        )

    async def achat_burst(
        self,
        agent_id: str,
        messages: list[dict[str, str]],
        *,
        player_id: str,
        display_name: str | None = None,
        role: str = "player",
        interrupt_check: Any | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """`chat_burst()` 的原生 async 版本。"""
        async for step in self._chat_burst_agen(
            agent_id, messages, player_id=player_id, display_name=display_name,
            role=role, interrupt_check=interrupt_check,
        ):
            yield step

    async def _chat_burst_agen(
        self,
        agent_id: str,
        messages: list[dict[str, str]],
        *,
        player_id: str,
        display_name: str | None,
        role: str,
        interrupt_check: Any | None,
    ) -> AsyncIterator[dict[str, Any]]:
        prelude = self._chat_prelude(
            agent_id, messages, player_id=player_id, display_name=display_name, role=role,
        )
        sink: dict[str, Any] = {}
        handled = await self._intent_prelude(
            agent_id, player_id, prelude["user_text"], messages, sink
        )
        if sink:
            # 判过了就得让宿主看得见 —— #16 要的就是"这条为什么被那样处理"可解释。
            # 只写进一个内部 dict 然后丢掉,等于分类了但没人知道。
            if handled is not None:
                yield {"kind": "budget", "budget": 1, "effective": 1,
                       "reasons": ["意图分派已经处理掉这条"], "traits": {}}
            yield _intent_step(sink)
        if handled is not None:
            if handled:
                yield {"kind": "message", "text": handled, "meta": dict(sink)}
            yield {"kind": "stop", "reason": "handled_by_intent",
                   "messages": 1 if handled else 0, "tool_calls": 0, "budget": 1}
            return
        extra_system: list[str] = []
        topic_block = self.chat_service.refused_topic_block(agent_id, prelude["user_text"])
        if topic_block:
            extra_system.append(topic_block)
        async for step in self.chat_service.autonomous_loop(
            agent_id,
            messages[-20:],
            interlocutor_id=player_id,
            interlocutor=prelude["interlocutor"],
            extra_system=extra_system,
            interrupt_check=interrupt_check,
        ):
            yield step

    def _location_display_name(self, location_id: str) -> str:
        """地点 id → 给角色看的名字。查不到就用 id,聊天不该因此告吹。"""
        store = self.scheduler.location_store
        if store is None or not location_id:
            return location_id
        row = store.get(location_id)
        return (row or {}).get("name") or location_id

    def chat_reply(self, *args: Any, **kwargs: Any) -> str:
        """chat() 的非流式便捷版,直接返回整段回复。"""
        return "".join(self.chat(*args, **kwargs))

    def record_chat_turn(
        self,
        agent_id: str,
        player_id: str,
        messages: list[dict[str, str]],
        *,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """把一个已完成回合(user→assistant 恰好两条)记入世界并立即关闭:
        生成摘要、发一个 conversation 事件、触发关系判定。返回会话 id。

        与旧 chat-evolution 不同,这里没有投递回执 —— 进程内调用失败即异常,
        重试与否是调用方一行代码的事。

        `meta` 收 `chat()` 那一轮填出来的观测量(stance / intent / tool_calls):
        它们落到 assistant 那一行上,并汇总进关闭时那一个 `conversation` 事件。
        不给就只是少了那份观测量,链路照旧。
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
        message_ids: dict[str, int] = {}
        for message in messages:
            content = str(message.get("content") or "").strip()
            if not content:
                raise ValueError("chat message content cannot be empty")
            message_ids[message["role"]] = self.chat_store.add_message(
                conversation_id, message["role"], content, int(time.time())
            )
        if meta:
            # 意图落在**用户那一行**(它是对那条消息的判定),stance 与 tool_call
            # 落在她的回复那一行。分开写,否则运维台上的 tag 会挂错气泡。
            if message_ids.get("user") and meta.get("intent"):
                self.chat_state.annotate_message(
                    message_ids["user"], intent=meta.get("intent"),
                    intent_confidence=meta.get("intent_confidence"),
                )
            if message_ids.get("assistant"):
                self.chat_state.annotate_message(
                    message_ids["assistant"],
                    # 只记她真的选了的那一格 —— 见 ChatService.annotate 的同一条理由。
                    stance=meta.get("stance") if meta.get("stance_declared") else None,
                    tool_calls=meta.get("tool_calls") or None,
                )
        self._bridge.run(self.session_manager.close_conversation(conversation_id))
        # 交互即检查点:说完这句话的瞬间,db 就是完整的(可打包、可崩)。
        self.scheduler.checkpoint()
        return conversation_id

    def conversations(self, agent_id: str) -> list[dict[str, Any]]:
        return self.chat_store.list_conversations(agent_id)

    def conversation_messages(self, conversation_id: int) -> list[dict[str, Any]]:
        return self.chat_store.messages_for(conversation_id)

    def close_conversation(self, conversation_id: int) -> bool:
        """手动关闭会话(摘要 + 事件 + 判定);返回是否发了世界事件。"""
        emitted = self._bridge.run(self.session_manager.close_conversation(conversation_id))
        self.scheduler.checkpoint()  # 交互即检查点
        return emitted

    # ── chat-agent(1.3.0):stance / 能力 / 玩家教的规则 ─────────────────────

    def tools(self) -> list[dict[str, Any]]:
        """她在聊天里能调的能力清单(声明在代码里,`@tool` 登记)。"""
        return [
            {"id": spec.id, "kind": spec.kind, "description": spec.description,
             "params_schema": dict(spec.params_schema)}
            for spec in tools_mod.tools_for("*")
        ]

    def stance(self, agent_id: str, target_id: str) -> dict[str, Any] | None:
        """她此刻对某人的关系性意图(#18)。没聊过就是 None。

        `declared=False` 的意思是"这是兜的底,不是她选的" —— 两者在文本上一模一样,
        只有这个字段能分开它们。
        """
        return self.chat_state.stance(agent_id, target_id)

    def stances(self, agent_id: str) -> list[dict[str, Any]]:
        return self.chat_state.stances(agent_id)

    def mutes(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        """还没过期的静音 / "等会儿再说"。"""
        return self.chat_state.mutes(agent_id)

    def is_muted(self, agent_id: str, player_id: str) -> dict[str, Any] | None:
        """她这会儿理这个人吗?返回 None 表示理。宿主可以拿它先探一下再开口。"""
        return self.chat_state.quiet_until(agent_id, player_id)

    def unmute(self, agent_id: str, player_id: str) -> None:
        """作者/运维的手动解除(角色自己不会调这个 —— 那是她的决定)。"""
        self.chat_state.clear_quiet(agent_id, player_id)

    def refused_topics(self, agent_id: str) -> list[dict[str, Any]]:
        return self.chat_state.refused_topics(agent_id)

    def followups(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        """还没到点的"回头找你" —— delay_reply 的兑现队列。"""
        return self.chat_state.pending_followups(agent_id)

    def persona_overrides(self, agent_id: str, player_id: str) -> list[dict[str, Any]]:
        """这个玩家教给这个角色的对话规则(#16,跨会话永久)。"""
        return self.chat_state.overrides(agent_id, player_id)

    def set_persona_override(
        self, agent_id: str, player_id: str, kind: str, value: str
    ) -> None:
        """直接写一条规则(宿主自己做 UI 时用,不必经过分类器)。"""
        if agent_id not in self.scheduler.agents:
            raise KeyError(f"agent {agent_id} not found")
        self.chat_state.set_override(agent_id, player_id, kind, value)

    def clear_persona_override(self, agent_id: str, player_id: str, kind: str) -> bool:
        return self.chat_state.clear_override(agent_id, player_id, kind)

    def broadcasts(self, *, since_seq: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        """她公开说过的话(`broadcast` 能力的产物)。"""
        page = self.history(since_seq=since_seq, limit=limit, kind="agent_broadcast")
        return page["events"]

    def player_move(self, player_id: str, location: str, *, role: str = "player") -> None:
        """玩家移动到某个 point 地点。未知地点抛 KeyError。"""
        location = location.strip()
        if not location:
            raise ValueError("location is required")
        if self.scheduler.location_store is not None:
            row = self.scheduler.location_store.get(location)
            if row is None or row.get("kind", "point") != "point":
                raise KeyError(f"没有 {location} 这个地方")
        # 更新而不是整条替换:`display_name` 是 `chat()` 记进来的,而 CLI 每聊一轮
        # 都先调一次 player_move —— 整条替换会把名字冲掉,于是检索又退回不透明 id。
        self._touch_player(player_id, role=role, location=location)

    def player_leave(self, player_id: str) -> None:
        """玩家离场。幂等 —— 宿主的断线回调可能重入。

        访客模型的另一半。`world.players` 此前**只有写、没有删**,而 CLI 每聊一轮都
        调一次 `player_move`;长跑的宿主里会攒下一屋子早就下线的幽灵访客。今天这还
        无所谓(没人读那份名单),但一旦让角色看得见在场的玩家,它就变成可见的错:
        NPC 会走去找一个断线三小时的人,并把一场没有人在的对话写进事件日志。

        他造成的**后果**(记忆、关系、图谱边、账本)留在世界里 —— 走的只是在场。
        """
        self.players.pop(player_id, None)

    def inbox(self, player_id: str, *, since_seq: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        """有谁来找过你 —— 角色主动搭话的收件箱(issue #13)。

        **敲门不是对话**:一条 `agent_hail` 不产生记忆、不动关系、不开会话。玩家还没
        回话,世界里什么也没发生;真正的对话仍然由 `World.chat` 发起,走原来那条完整
        的链。这条边界是有意的 —— 否则你会看到"她来找过我",转头问她却毫无印象。

        返回按 seq 升序;拿最后一条的 `seq` 当下次的 `since_seq` 就是增量拉取。
        """
        page = self.history(since_seq=since_seq, limit=limit, kind="agent_hail")
        return [
            event for event in page["events"]
            if (event.get("payload") or {}).get("player_id") == player_id
        ]

    def who_is_present(self) -> list[str]:
        """此刻真的在场的玩家 id(已过 TTL 的当作走了)。

        `player_ttl_seconds` 是那道兜底闸:不要求宿主维护心跳(那会把契约面弄脏),
        任何一次交互都算"我还在"。宿主没有显式 `player_leave` 也不会留下永久幽灵。
        """
        cutoff = time.time() - self.player_ttl_seconds
        return sorted(
            pid for pid, info in self.players.items()
            if float(info.get("last_seen", 0.0)) >= cutoff
        )

    def _touch_player(self, player_id: str, **fields: Any) -> dict[str, Any]:
        """记一次"这个玩家还在",顺便更新几个字段。所有玩家入口都过这里。"""
        info = self.players.setdefault(player_id, {"role": "player", "location": None})
        info.update(fields)
        info["last_seen"] = time.time()
        return info

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
        player = self._touch_player(player_id, role=role)
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
        """宿主给玩家钱包充值 —— 落成账本上的一笔 `payment`,返回充值后的余额。

        这里曾经只改内存里的 `players[pid]["wallet"]`,**不发任何事件**,而
        `player_buy` 拿那个内存数做门禁、却把花费发成 payment。于是同一个玩家有两个
        余额:内存里是"充值 − 花费",账本投影里是"**负的花费**",`World.balance()`
        读投影,所以一个刚充过钱的玩家在那里显示为负数。两个数谁也不知道对方存在。

        经济的第一条设计是"账本是投影,对账 = 重放"。钱包站在那条规矩外面就没有
        道理 —— 何况内存那份重启即失效,而钱是世界的一部分,不是会话的一部分。
        """
        from anima_world import economy

        if amount <= 0:
            raise ValueError("amount must be positive")
        if self.scheduler.event_log is None:
            raise ValueError("economy needs a persistent world")
        holder = f"player:{player_id}"
        self._touch_player(player_id)
        with self.scheduler._lock:
            self._record_and_fan({
                "type": "payment", "who": holder,
                "payload": {"from": economy.TOWN, "to": holder,
                            "amount": float(amount), "reason": "topup"},
            })
            self.scheduler.checkpoint()  # 交互即检查点
        return self.balance(holder)

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
            holder = f"player:{player_id}"
            # 门禁读账本,不读内存 —— 那两个数此前会分叉(见 player_topup)。
            wallet = float(self.scheduler._memory_projection.balances.get(holder, 0.0))
            if wallet < price:
                raise ValueError(f"钱包不够:{wallet} < {price}")
            if not economy.take_stock(conn, location_id, item_id):
                raise KeyError(f"{location_id} 没有 {item_id} 的货")
            self.scheduler._shop_sales[(location_id, item_id)] = (
                self.scheduler._shop_sales.get((location_id, item_id), 0) + 1
            )
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
        return {"item_id": item_id, "price": price, "wallet": self.balance(holder)}

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

    def _recall_query(self, interlocutor_id: str) -> str:
        """检索用的 query:对方**叫什么**,不是宿主给的不透明 id。

        记忆文本里写的是名字(「阿檀说他在找一把旧伞」),而 `interlocutor_id` 常常
        是 `p1` 或一个 uuid —— 字符二元组交集恒空,relevance 恒 0,三因子检索静默
        退化成 recency+importance。于是角色确实记得你,却永远召回不到关于你的那几条。

        NPC 之间不受影响:那边的 id 就是名字,取不到 display_name 时原样退回。
        """
        name = str((self.players.get(interlocutor_id) or {}).get("display_name") or "").strip()
        if not name:
            return interlocutor_id
        if len(name) < 2:
            # bigram 对单字返回 {整串},而记忆侧全是 2 字二元组,交集恒空 —— 检索
            # 又退回 recency+importance。降级不许无声(与 llm 降级同一条纪律)。
            logger.warning(
                "显示名 %r 只有一个字,记忆检索匹配不到它 —— 这次召回退回"
                "「最近 + 最重要」,与对方是谁无关", name,
            )
        return name

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
                            query=self._recall_query(interlocutor_id), k=int(k),
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
                # 在途不算在场。黑板的 `loc` 要落地才改写,途中读出来仍是出发地
                # —— `_agent_locations()` 跳过 `_transit` 就是在补这个洞,只比
                # 地点的话,一个正在赶路的人会被判成和你面对面。
                "in_transit": agent_id in scheduler._transit,
            }
            rel = scheduler._memory_projection.relations.get((agent_id, interlocutor_id))
            if rel is not None:
                ctx["relation"] = {
                    "r_type": rel.r_type,
                    "band": BAND_NAMES[band(rel.sentiment)],
                }
            # #17 的预算要读心情。needs 没点亮时读不到 —— 那时预算就少一项依据,
            # 而不是拿 0.5 假装读到了(那会让"她累了"和"没装需求系统"混成一件事)。
            mood = brain.agent.blackboard.read("need.mood")
            if mood is not None:
                ctx["mood"] = float(mood)
            return ctx


def _intent_step(meta: dict[str, Any]) -> dict[str, Any]:
    """分类结果作为 `chat_burst` 的一个步骤 —— 宿主的消费代码只有一套。"""
    return {
        "kind": "intent",
        "intent": meta.get("intent"),
        "confidence": meta.get("intent_confidence"),
        "reason": meta.get("intent_reason"),
        "detail": meta.get("intent_detail"),
        "handled": bool(meta.get("handled_by")),
    }


def _host_loop_is_running() -> bool:
    """调用线程上已经有一个在跑的事件循环吗?

    留着是为了把那条纪律说清楚:**"同步门面"不等于"只能从非 async 代码里调用"**
    —— FastAPI / aiohttp 的处理函数就是 `async def`,而 README 把"嵌入到应用里"写成
    主要用法。1.3.0 起门面不再靠"检测到有循环就换个线程 `asyncio.run`"来兜(那条路
    每次开一个新循环,而 HTTP 连接池是被缓存复用的 —— 见 `_BridgeLoop`),而是一律交给
    世界自己那条循环。两种宿主因此走同一条路,`achat` 那条原生 async 的门也照旧。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True
