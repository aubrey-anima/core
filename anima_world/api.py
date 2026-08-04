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
import contextlib
import logging
import queue
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from anima_world import autonomy
from anima_world import tools as tools_mod
from anima_world.actions import ActionDescriptor
from anima_world.chat_service import ChatService
from anima_world.chat_session import ChatSessionManager
from anima_world.chat_state import ChatStateStore
from anima_world.chat_store import ChatStore
from anima_world.config_store import coerce_to_declared_type, mask_secret
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
        return self.scheduler.event_log.max_seq()

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
        log = self.scheduler.event_log
        if log is not None:
            db_status = {"enabled": True, "path": self.scheduler.db_path}
            events_status = {
                "count": log.count(),
                "latest_seq": log.max_seq(),
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

    def present_player_ids(self, agent_id: str | None = None) -> list[str]:
        """在场的玩家(按 TTL)—— `reach_out` 的在场闸。

        给了 `agent_id` 就**按同地过滤**,和 issue #13 那条 `_maybe_hail_player`
        同一条规矩:不然一个在工作室的角色能"主动去找"一个在咖啡店的玩家,
        隔着半个地图打招呼,而 `reach_out` 的整个意义就是"她走过来跟你说话"。
        不给 `agent_id`(旧调用点)时退回世界范围的在场名单。
        """
        present = self._world.who_is_present()
        if agent_id is None:
            return present
        here = self.agent_location(agent_id)
        return [
            pid for pid in present
            if str((self._world.players.get(pid) or {}).get("location") or "") == here
        ]

    def player_name(self, player_id: str) -> str:
        info = self._world.players.get(player_id) or {}
        return str(info.get("display_name") or player_id)

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

    def do_action(self, agent_id: str, kind: str, params: dict[str, Any]) -> bool:
        """过日子的动作 —— **委托行为树走的那条路**(`Scheduler.emit_action`)。

        于是"排班让她走"和"她自己决定走"在世界里是同一件事:一样发 travel /
        location_join、一样花时间、一样在途中不可打断。另写一份"外部版本的走路"
        迟早和行为树那份分叉,而分叉的那天没人会发现。

        返回 `False` 是**世界说"还不行"**(她在半路上、要找的人不在这儿),不是失败。
        """
        from anima_world.actions import ActionDescriptor

        brain = self._world.scheduler.agents.get(agent_id)
        if brain is None:
            raise tools_mod.ToolCallError(f"没有 {agent_id} 这个人")
        return bool(
            self._world.scheduler.emit_action(brain.agent, ActionDescriptor(kind, dict(params)))
        )

    def close_conversation(self, agent_id: str, player_id: str) -> bool:
        active = self._world.chat_store.active_conversation(agent_id, player_id=player_id)
        if active is None:
            return False
        return self._world.close_conversation(int(active["id"]))


# 搬完之后要清掉的表。**搬家一直是复制,不是移动** —— 不清的话那个 .db 既不是完整
# 的世界,也不是干净的空壳,而是**一份过时的副本**,而我们刚在它上面盖了"这里没数据"
# 的戳。那个组合最危险:戳没撒谎(数据确实以 Redis 为准),但文件里躺着一份看起来
# 很像真世界的旧数据,谁手滑读一下都会读出一个几小时前的世界。
#
# 不清的两张:`config`(还没搬,而且按 DB-SPLIT.md 它该搬**出**世界)与
# `conversations` / `messages`(转录,同理)。`db_meta` 当然留 —— 戳就在那儿。
_MIGRATED_TABLES = (
    "events", "memories", "reflection_state", "edges", "stocks", "world_rules",
    "stock_visibility", "stock_places", "agent_needs", "cliques",
    "item_defs", "shop_stock", "locations", "bt_nodes", "bt_actions",
    "prompt_templates", "agent_stance", "agent_mutes", "agent_refused_topics",
    "agent_followups", "persona_overrides",
)


def _goes_to_mysql(table: str, mysql: Any) -> bool:
    """这张表会不会被 MySQL 接手 —— **接手的话就别往 Redis 搬。**

    搬进去再被换掉,留下的是一份**冻在创世的旧拷贝**:引擎自己不读它(store 已经
    换成 MySQL 版),但 Redis 的全部意义是"另一个只有 Redis 连接的进程读得到这个
    世界" —— 那个进程读到的会是一个从创世起什么都没发生过的世界。实测:MySQL 289
    条事件,Redis 那份停在 50 条,而且永远不再长。

    两份真相里有一份不会更新,是这个仓库最怕的坏法:两边都读得出来,只是一边是错的。
    """
    from anima_world.mysql_state import GROWS_FOREVER

    return mysql is not None and table in GROWS_FOREVER


def _drop_stale_redis_copies(redis: Any, world_id: str) -> list[str]:
    """删掉上一次"只有 Redis"时留下的那份拷贝。

    一个世界可能先只用 Redis 跑过,后来才接上 MySQL —— 那时 Redis 里已经有
    events / memories 了。不删的话它会一直躺在那儿冒充这个世界的历史。
    删之前 MySQL 那边已经从 SQLite 补齐了全量,所以这不是丢数据。
    """
    from anima_world.redis_state import KEY_PREFIX, events_key

    doomed = [
        events_key(world_id),
        f"{KEY_PREFIX}:{world_id}:memories",
        f"{KEY_PREFIX}:{world_id}:memories:id",
    ]
    dropped = [key for key in doomed if redis.delete(key)]
    if dropped:
        logger.info("删掉 Redis 里冻住的旧拷贝(这些表归 MySQL 了):%s", dropped)
    return dropped


def _rebind_chat_store(world: Any, store: Any) -> None:
    """把转录存储换掉,**并且换掉每一个攥着旧引用的人**。

    这是踩出来的:换后端时只改 `world.chat_state` 那一个字段,而 `ChatService`
    在构造时把它存进了自己的 `_state`,于是聊天照跑、照写 —— 写进了旧后端。
    **测试全绿,因为每个组件单看都对。** 转录这边攥着旧引用的有四处,少改一处
    就是一半的聊天记录落在 SQLite、一半落在 MySQL。

    `tests/test_mysql_state.py::test_swapping_the_transcript_rebinds_every_holder`
    是这条的闸:它枚举持有者,漏一个就红。
    """
    world.chat_store = store
    world.chat_service._store = store
    world.session_manager._store = store
    # chat_state 的 `annotate_message` 经 transcript 转发到 `messages` 表。
    world.chat_state._transcript = store
    # 会话关闭事件带的那份 meta 也是从转录里算的。
    world.session_manager._meta_provider = world.chat_state.conversation_meta


def _shed_migrated_rows(conn: Any) -> None:
    """把已经搬进 Redis 的表清空,让这个文件成为一个**诚实的空壳**(schema + 戳)。"""
    import sqlite3

    for table in _MIGRATED_TABLES:
        try:
            conn.execute(f"DELETE FROM {table}")
        except sqlite3.Error:  # noqa: PERF203 - 少一张表不该让开机失败
            logger.debug("清空 %s 时跳过(这个世界没有这张表)", table)
    conn.commit()


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
        conn = scheduler.conn
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
        # autonomy:(角色, 世界日) -> 今天主动过几次;以及那四个"通没通"的计数。
        # 都是内存态:重启即清 —— 上限是"别把玩家的收件箱刷满",不是需要审计的账。
        self._autonomy_done: dict[tuple[str, int], int] = {}
        self._autonomy_stats: dict[str, Any] = {
            "asked": 0, "acted": 0, "quiet": 0, "failed": 0, "last": None,
        }
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
        self._install_autonomy()   # 定时轮次挂到时钟上(开关关着时 hook 自己会退出)

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
        redis: Any = None,
        mysql: Any = None,
        world_id: str = "world",
    ) -> "World":
        """打开(或创建)一个世界。

        空库首启时从 seed 播种(缺省用内置种子);已有库的 seed 会被忽略并
        警告。坏 beats 脚本在这里当场抛 BeatScriptError,不流到运行期。

        `redis` 给了的话,**每个角色的黑板搬进 Redis**,这个进程不再持有它们
        (`anima_world.redis_state`)。那 20 个键 —— 她在哪、在干嘛、饿不饿、打算做
        什么 —— 此前是纯内存,于是两个进程各开同一个世界文件会读到同一份历史、
        然后在各自内存里跑出**两个不同的世界**。搬走之后那一半不再是进程私有的。

        `world_id` 进 Redis 键名:一个 Redis 实例上跑十个世界是常态,键撞车的后果是
        两个世界的角色共用一个脑子。

        ⚠️ 这是"全部状态进 Redis"的**第一步,不是全部**。时钟、在途集合、当前动作、
        规划、记忆投影仍在进程里 —— 见 `docs/AGENT-RUNTIME.md`。
        """
        from anima_world.__main__ import build_serve_scheduler  # 延迟导入避免环

        scheduler = build_serve_scheduler(
            agents,
            db_path=db_path,
            seed_path=seed_path,
            beats_path=beats_path,
            force_mock_llm=force_mock_llm,
        )
        world = cls(scheduler)
        if redis is not None:
            from anima_world.redis_state import (
                RedisBlackboard, RedisClock, RedisDict, RedisLock,
                agent_key, clock_key, current_action_key, decode_action,
                encode_action, lock_key, plans_key, transit_key, RedisStockStore,
                RedisMemoryStore, RedisPromptStore, RedisVisibilityStore,
                RedisBTStore, RedisLocationStore, RedisChatStateStore,
                RedisEventLog, events_key, RedisKnowledgeGraph, RedisNeedsStore,
                RedisCliqueStore, RedisReflectionStore, RedisEconomyStore,
            )

            # 搬家而不是清空:黑板上此刻的内容(创世写进去的性格、目标、位置)必须
            # 跟过去,否则第一个 tick 她会以为自己没有性格。
            for aid, brain in scheduler.agents.items():
                board = RedisBlackboard(redis, agent_key(world_id, aid))
                # **只填缺的,不覆盖。** 手里这份是从 SQLite 读出来的创世快照;
                # 接上一个已经在跑的世界时写回去,等于把她按回原点。
                board.seed_missing(brain.agent.blackboard.snapshot())
                brain.agent.blackboard = board
            # 时钟同理:先把 db 里恢复出来的那个值带过去,再交给 Redis 管。
            scheduler._clock_store = RedisClock(
                redis, clock_key(world_id), initial=scheduler.clock
            )
            # 谁在路上、谁在干嘛。此前是纯内存的 dict,后果很具体:另一个进程不知道
            # 她**正在赶路**,于是会让她"走开"、让她跟一个还没走到的人搭话 —— 而这些
            # 判断恰恰是引擎用来把约束变成等待、把等待变成相遇的。
            transit = RedisDict(redis, transit_key(world_id))
            for agent_id, trip in list(scheduler._transit.items()):
                transit[agent_id] = trip
            scheduler._transit = transit

            doing = RedisDict(
                redis, current_action_key(world_id),
                encode=encode_action, decode=decode_action,
            )
            for agent_id, action in list(scheduler._current_action.items()):
                doing[agent_id] = action
            scheduler._current_action = doing

            # 规划:同样是真状态,同样搬走。它的值是 JSON 原生的(计划步骤列表),
            # 不需要编解码。
            plans = RedisDict(redis, plans_key(world_id))
            for agent_id, plan in list(scheduler._plans.items()):
                plans[agent_id] = plan
            scheduler._plans = plans

            # 世界的量。**搬家要把已有的量带过去** —— 创世播下的树高、季节、
            # 钱都在 SQLite 里,不带过去世界会从一张白纸重新开始。
            if scheduler.stock_store is not None:
                shelf = RedisStockStore(redis, world_id)
                for owner in scheduler.stock_store.owners():
                    snap = scheduler.stock_store.snapshot(owner)
                    for key, (value, tick) in snap.items():
                        shelf.set(owner, key, value, tick)
                scheduler.stock_store = shelf

            # 记忆 / 提示词模板 / 可见性声明。三样都**先把已有内容搬过去** ——
            # 创世播下的记忆、内置的十几个模板、种子里的可见性声明,不带过去
            # 世界会从一张白纸重开。
            if scheduler.memory_store is not None and not _goes_to_mysql("memories", mysql):
                fresh_mem = RedisMemoryStore(redis, world_id, scheduler.config_store)
                for aid in scheduler.agents:
                    for row in scheduler.memory_store.query(aid):
                        fresh_mem.add(
                            aid, tick=int(row.get("tick") or 0), kind=str(row.get("kind") or ""),
                            summary=str(row.get("summary") or ""),
                            importance=float(row.get("importance") or 0.5),
                            anchor=bool(row.get("anchor")),
                            event_seq=row.get("event_seq"),
                            created_at=row.get("created_at"),
                        )
                scheduler.memory_store = fresh_mem

            if scheduler.prompt_store is not None:
                fresh_prompts = RedisPromptStore(redis, world_id)
                # **只搬作者动过的。** `list()` 返回的是合并视图(引擎声明的 31 条
                # 加上世界里多出来的),整份搬过去等于把刚从 SQLite 拆掉的默认值
                # 快照在 Redis 里原样重建 —— 改进过的模板从此又到不了这个世界,
                # 而且照样无声(DB-SPLIT.md 移动 1 要拆的正是这个)。
                for row in scheduler.prompt_store.list():
                    if row.get("source") != "世界文件":
                        continue
                    fresh_prompts.set(
                        str(row["name"]), str(row.get("template") or ""),
                        row.get("description"),
                    )
                scheduler.prompt_store = fresh_prompts

            if scheduler.visibility_store is not None:
                fresh_vis = RedisVisibilityStore(redis, world_id)
                for row in scheduler.visibility_store.declarations():
                    fresh_vis.declare(
                        str(row["kind"]), str(row["key"]), str(row["visibility"]),
                        row.get("label"),
                    )
                for owner, label in scheduler.visibility_store.labels().items():
                    where = scheduler.visibility_store.place_of(owner)
                    if where:
                        fresh_vis.place(owner, where, label)
                scheduler.visibility_store = fresh_vis

            # 地图与行为树。两样都是创世时写好、之后基本不动的配置,但**只要还有
            # 一张表留在 SQLite,你就仍然需要那个文件** —— 而这一整件事的目的正是让
            # 世界不再是一个文件。完整性比冷热重要。
            if scheduler.location_store is not None:
                fresh_loc = RedisLocationStore(redis, world_id)
                for row in scheduler.location_store.all():
                    fresh_loc.upsert(str(row["id"]), **{
                        k: v for k, v in row.items() if k != "id"
                    })
                scheduler.location_store = fresh_loc

            if scheduler.bt_store is not None:
                fresh_bt = RedisBTStore(redis, world_id)
                for row in scheduler.bt_store.actions():
                    fresh_bt.set_action(
                        str(row["node_id"]), str(row["kind"]), dict(row.get("params") or {})
                    )
                for tree in {"default", *scheduler.agents}:
                    for node in scheduler.bt_store._tree_rows(tree):
                        fresh_bt.add_node(
                            tree, str(node["node_id"]), str(node["type"]),
                            node.get("parent"), int(node.get("sort") or 0),
                            dict(node.get("params") or {}),
                        )
                scheduler.bt_store = fresh_bt

            # 她和某个人之间的状态:意图 / 静音 / 拒谈 / 回头找你 / 玩家教的规则。
            # 这五样是**真世界状态**(她真的在不理你、真的拒绝谈那件事),搬。
            # 转录**不搬进 Redis**(它随时间无限增长,见 mysql_state.py);
            # 但这一层要拿着它,否则 `annotate_message` 没有落脚的地方。
            fresh_chat = RedisChatStateStore(redis, world_id, transcript=world.chat_store)
            old_chat = world.chat_state
            for agent_id in scheduler.agents:
                for row in old_chat.stances(agent_id):
                    fresh_chat.set_stance(
                        agent_id, str(row["target"]), str(row["stance"]),
                        declared=bool(row["declared"]), tick=int(row["updated_tick"]),
                    )
                for row in old_chat.refused_topics(agent_id):
                    fresh_chat.refuse_topic(agent_id, str(row["keyword"]))
            for row in old_chat.mutes():
                fresh_chat.set_quiet(
                    str(row["agent_id"]), str(row["player_id"]), kind=str(row["kind"]),
                    minutes=max(0.0, row["seconds_left"] / 60.0), reason=row.get("reason"),
                )
            for row in old_chat.pending_followups():
                fresh_chat.add_followup(
                    str(row["agent_id"]), str(row["player_id"]),
                    due_tick=int(row["due_tick"]), kind=str(row["kind"]),
                    reason=row.get("reason"),
                )
            world.chat_state = fresh_chat
            scheduler.chat_state = fresh_chat
            # **聊天服务在构造时就拿走了旧的那个引用**,只换 world/scheduler 上的
            # 属性它看不见 —— 于是 stance / 静音 / 拒谈会继续写进 SQLite,而这个
            # 世界的别的东西全在 Redis。一半在这儿一半在那儿,而且不报错。
            if world.chat_service is not None:
                world.chat_service._state = fresh_chat

            # 最后六张:关系图 / 身体 / 小团体 / 反思水位 / 物品 / 货架。
            if scheduler.knowledge_graph is not None:
                fresh_graph = RedisKnowledgeGraph(redis, world_id)
                for edge in scheduler.knowledge_graph.query():
                    fresh_graph.add(
                        str(edge["subject"]), str(edge["predicate"]), str(edge["object"]),
                        edge.get("source_event_seq"), int(edge.get("created_at") or 0),
                    )
                scheduler.knowledge_graph = fresh_graph

            if scheduler.needs_store is not None:
                fresh_needs = RedisNeedsStore(redis, world_id)
                for agent_id in scheduler.agents:
                    fresh_needs.persist(agent_id, scheduler.needs_store.load(agent_id), 0)
                scheduler.needs_store = fresh_needs

            if scheduler.clique_store is not None:
                fresh_cliques = RedisCliqueStore(redis, world_id)
                rows = scheduler.clique_store.load()
                if rows:
                    fresh_cliques.store(
                        [list(r["member_ids"]) for r in rows],
                        int(rows[0].get("computed_tick") or 0),
                    )
                scheduler.clique_store = fresh_cliques

            if scheduler.reflection_store is not None:
                fresh_reflect = RedisReflectionStore(redis, world_id)
                for agent_id in scheduler.agents:
                    fresh_reflect.set(agent_id, scheduler.reflection_store.get(agent_id))
                scheduler.reflection_store = fresh_reflect

            if scheduler.economy_store is not None:
                fresh_econ = RedisEconomyStore(redis, world_id)
                for item in scheduler.economy_store.items():
                    fresh_econ.put_item(
                        str(item["id"]), str(item["name"]), str(item["kind"]),
                        float(item["base_price"]), item.get("restores"),
                    )
                for shelf in scheduler.economy_store.shelves():
                    fresh_econ.put_shelf(
                        str(shelf["location_id"]), str(shelf["item_id"]),
                        int(shelf["quantity"]), float(shelf["price"]),
                    )
                scheduler.economy_store = fresh_econ

            # **事件日志 —— 唯一真相那张表。** 放在最后搬:上面那些 store 的搬家
            # 都不发事件,而一旦换成 Redis 版,之后的每一条都进 Redis。
            # 已有的历史要带过去,否则重放会重建出一个从头开始的世界。
            if scheduler.event_log is not None and not _goes_to_mysql("events", mysql):
                fresh_log = RedisEventLog(redis, events_key(world_id))
                if not fresh_log.max_seq():
                    for e in scheduler.event_log.replay():
                        fresh_log.append({
                            "ts": e.ts, "type": e.type, "who": e.who,
                            "loc": e.loc, "payload": e.payload,
                        })
                sqlite_log = scheduler.event_log
                scheduler.event_log = fresh_log
                world._sqlite_log = sqlite_log   # 盖戳还要用它那条连接

            # **在世界文件上盖个戳**:数据不在这儿了。
            # 离线命令(doctor / events export / report / 打包)是直接开这个文件的,
            # 读到的会是一张空表 —— 报"0 条事件",然后一切照跑。盖了戳它们就当场
            # 停下并说清去哪儿看,而不是给一个错的答案。
            from anima_world.db import stamp_storage

            if getattr(world, "_sqlite_log", None) is not None:
                _shed_migrated_rows(world._sqlite_log.conn)
                stamp_storage(world._sqlite_log.conn, "redis", world_id)

            # ── 随时间无限增长的那几样搬去 MySQL ──────────────────────────
            #
            # 实测:一个三人世界跑 20 天,Redis 内存增量的**九成**是 `events` 与
            # `memories`。一年 4.7 MB/世界,一千个世界跑一年 4.6 GB **常驻**,
            # 而且永远不回落。别的东西随世界规模有界,不随时间涨。
            #
            # **分界线是增长性,不是冷热** —— 内存装得下一个热但有界的东西,
            # 装不下一个冷但无限的东西。
            if mysql is not None:
                from anima_world.mysql_state import (
                    MySQLChatStore, MySQLEventLog, MySQLMemoryStore, as_connection,
                    ensure_schema,
                )

                # 工厂 → 每线程一条;裸连接 → 能用但当场点名(见 `as_connection`)。
                mysql = as_connection(mysql)

                prefix = f"{world_id}_"
                ensure_schema(mysql, prefix)
                if redis is not None:
                    _drop_stale_redis_copies(redis, world_id)

                fresh_log = MySQLEventLog(mysql, prefix)
                if not fresh_log.max_seq():
                    for e in scheduler.event_log.replay():
                        fresh_log.append({
                            "ts": e.ts, "type": e.type, "who": e.who,
                            "loc": e.loc, "payload": e.payload,
                        })
                scheduler.event_log = fresh_log

                fresh_mem = MySQLMemoryStore(mysql, prefix, scheduler.config_store)
                if not any(fresh_mem.query(a) for a in scheduler.agents):
                    for agent_id in scheduler.agents:
                        for row in scheduler.memory_store.query(agent_id):
                            fresh_mem.add(
                                agent_id, tick=int(row.get("tick") or 0),
                                kind=str(row.get("kind") or ""),
                                summary=str(row.get("summary") or ""),
                                importance=float(row.get("importance") or 0.5),
                                anchor=bool(row.get("anchor")),
                                event_seq=row.get("event_seq"),
                                created_at=row.get("created_at"),
                            )
                scheduler.memory_store = fresh_mem

                # 转录 —— **用户点名要在 MySQL 的那一样。**
                # 它是所有表里最该离开内存的:一条消息几百字,只增不减,而世界
                # 只在会话关闭时收一个摘要事件。放 Redis 等于用最贵的存储装最冷的数据。
                fresh_chat_store = MySQLChatStore(mysql, prefix, lock=scheduler._lock)
                for agent_id in scheduler.agents:
                    if fresh_chat_store.list_conversations(agent_id):
                        continue
                    for row in world.chat_store.list_conversations(agent_id):
                        new_id = fresh_chat_store.start_conversation(
                            agent_id, int(row.get("started_at") or 0),
                            participants=row.get("participants"),
                            location=row.get("location"),
                            player_id=row.get("player_id"),
                        )
                        for msg in world.chat_store.messages_for(int(row["id"])):
                            fresh_chat_store.add_message(
                                new_id, str(msg["role"]), str(msg["content"]),
                                int(msg.get("created_at") or 0))
                        if row.get("status") == "closed":
                            fresh_chat_store.close(
                                new_id, row.get("summary") or "",
                                int(row.get("closed_at") or 0))
                _rebind_chat_store(world, fresh_chat_store)

            # 跨进程的世界锁。**在调度器那把 RLock 之外,不是替代它** ——
            # 那把还被 threading.Condition 用着(等规划落地),而 Condition 要真线程锁。
            world._world_lock = RedisLock(redis, lock_key(world_id))

            # **开机点名:这个 Redis 会不会把世界忘掉。**
            # Redis 主要活在内存里,持久化是配置选项,而默认的 redis.conf 里 AOF
            # 是关的。忘掉的样子不是报错,是"世界悄悄退回创世那一刻然后接着跑"。
            from anima_world.redis_state import durability_warning

            warning = durability_warning(redis)
            if warning:
                logger.warning("世界跑在 Redis 上,但 %s", warning)
                world._durability_warning = warning
        return world

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
                    scheduler.conn.backup(target)
                    target.execute("DELETE FROM config WHERE is_secret=1")
                    target.commit()
                finally:
                    target.close()
                genesis_row = scheduler.conn.execute(
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

        with self.scheduler._lock:
            return self.scheduler.clique_store.load()

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
        log = self.scheduler.event_log
        with self.scheduler._lock:
            total = log.count(who=who, kind=kind)
            # +1 = "还有没有下一页"
            rows = log.page(since_seq=int(since_seq), limit=limit + 1, who=who, kind=kind)

        page = rows[:limit]
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

    # ── world-rules:世界的规律与存量 ───────────────────────────────────────

    def stock(self, owner: str, key: str, default: float = 0.0) -> float:
        """读一个量。owner 是任意字符串,前缀即种类(`tree:oak_01` / `world`)。"""
        store = self.scheduler.stock_store
        return default if store is None else store.get(owner, key, default)

    def stocks(self, owner: str) -> dict[str, float]:
        """这个 owner 身上所有的量 —— 一个"实体"就是共用一个 owner 的一组量。"""
        store = self.scheduler.stock_store
        return {} if store is None else store.of(owner)

    def set_stock(self, owner: str, key: str, value: float) -> None:
        """写一个量(种一棵树、埋一个矿、给谁加点功力)。

        写进来的 `updated_tick` 是**此刻** —— 所以刚种下的树不会按世界年龄
        一次性长成参天大树。
        """
        store = self.scheduler.stock_store
        if store is None:
            raise ValueError("world-rules needs a persistent world")
        store.set(owner, key, float(value), tick=self.scheduler.clock)

    def set_stocks(self, owner: str, values: dict[str, float]) -> None:
        store = self.scheduler.stock_store
        if store is None:
            raise ValueError("world-rules needs a persistent world")
        store.set_many(owner, {k: float(v) for k, v in values.items()},
                       tick=self.scheduler.clock)

    def stock_owners(self, kind: str | None = None) -> list[str]:
        """世界里有哪些量的主人;给了 kind 就只看那一类(`tree` / `agent` / …)。"""
        store = self.scheduler.stock_store
        return [] if store is None else store.owners(kind)

    def place_stock(self, owner: str, location: str, label: str | None = None) -> None:
        """这个东西在哪(一棵树在咖啡店)。`here` 档的可见性靠它才成立。

        `label` 是给角色看的名字 —— 提示词里"这里的老橡树"比"这里的 tree:oak_01"
        像人话。
        """
        store = self.scheduler.visibility_store
        if store is None:
            raise ValueError("world-rules needs a persistent world")
        store.place(owner, location, label)

    def declare_visibility(self, owner_kind: str, key: str, visibility: str,
                           label: str | None = None) -> None:
        """声明某类量角色感知得到哪一档:`self` / `here` / `public` / `hidden`。

        **没声明就是感知不到**(默认 `hidden`)—— 反过来的错不可挽回:一个"暗中的
        恨意"的量若默认公开,角色下一句就说出来了。声明本身就是这一层的开关。
        """
        store = self.scheduler.visibility_store
        if store is None:
            raise ValueError("world-rules needs a persistent world")
        store.declare(owner_kind, key, visibility, label)

    def visibility_rules(self) -> list[dict[str, Any]]:
        store = self.scheduler.visibility_store
        return [] if store is None else store.declarations()

    # ---- 看一眼她收到了什么 ----------------------------------------------

    def debug_prompt(
        self,
        agent_id: str,
        *,
        player_id: str = "p1",
        message: str = "在吗",
        display_name: str | None = None,
        role: str = "player",
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """把这一刻这个角色**会收到的提示词**原样交出来,逐块带来源标签。

        为什么要有这个:提示词是这套系统里最不可见、又最容易出错的一层。1.3 开发期
        四个 bug 有三个在这儿(stance 声明率 2/6、能力一次没用、定时轮次 18 轮 0 动作),
        每一个的诊断都需要同一件事 —— **她到底收到了什么**;而当时唯一的办法是往
        `chat_service` 的私有属性上塞一个假 LLM 去偷看。宿主和世界作者一个都没有。

        三件事按重要性排:

        1. **它不会撒谎。** 块从 `ChatService.prompt_blocks` 来 —— 和真聊天**同一个
           函数**。调试视图另写一遍拼装,迟早和真提示词分叉,那时你会照着它去改一个
           不存在的问题。
        2. **它解释缺席**(`absent`)。少一块几乎总比多一块难查:世界照跑、她照说话,
           只是从来没提过那棵树。所以"哪块没出现、为什么"和"哪块出现了"一样是答案。
        3. **它不留副作用。** 不写 `players.last_seen`、不触发意图分类、不进 LLM、
           静音中的角色也照样交出提示词(而 `chat()` 会当场拒)。看,但不碰。

        返回 `{"blocks": [{"label","chars","text"}], "order", "absent",
        "system", "system_chars", "history"}`。
        """
        if agent_id not in self.scheduler.agents:
            raise KeyError(f"agent {agent_id} not found")
        turns = list(history or []) + [{"role": "user", "content": message}]
        known = self.players.get(player_id) or {}
        interlocutor: dict[str, str] = {
            "display_name": display_name or known.get("display_name")
            or f"player-{player_id[:8]}",
            "role": role or str(known.get("role") or "player"),
        }
        where = str(known.get("location") or "").strip()
        if where:
            interlocutor["location"] = where
            interlocutor["location_name"] = self._location_display_name(where)
        extra = self.chat_service.refused_topic_block(agent_id, message)
        blocks = self.chat_service.prompt_blocks(
            agent_id,
            interlocutor_id=player_id,
            interlocutor=interlocutor,
            extra_system=[extra] if extra else None,
        )
        system = "\n\n".join(block.text for block in blocks)
        return {
            "agent_id": agent_id,
            "player_id": player_id,
            "blocks": [
                {"label": block.label, "chars": len(block.text), "text": block.text}
                for block in blocks
            ],
            "order": [block.label for block in blocks],
            "absent": self._absent_prompt_blocks(agent_id, player_id, blocks),
            "system": system,
            "system_chars": len(system),
            "history": turns[-20:],
        }

    def _absent_prompt_blocks(
        self, agent_id: str, player_id: str, blocks: list[Any]
    ) -> dict[str, str]:
        """哪些块没出现,以及**为什么** —— 一句人话,照着它就能让那块出现。

        缺席比多余难查得多:世界照跑、她照说话,只是从来没提那棵树,而你不知道该去
        改可见性声明、开关、还是模板。所以这里报的是原因,不是一句"missing"。
        """
        present = {block.label for block in blocks}
        why: dict[str, str] = {}

        def check(label: str, reason: str) -> None:
            if label not in present:
                why[label] = reason

        check("world.setting", "prompt_templates 里的 world.setting 是空的")
        check("memories", "这一刻检索不到记忆(世界刚开,或这个角色还没记住什么)")
        check("presence", "world_provider 没给出在场快照 —— 通常是世界没在跑")
        check("relation", "她和这个玩家之间还没有关系行(说过话之后才有)")
        if "perception" not in present:
            store = self.scheduler.visibility_store
            declared = [] if store is None else store.declarations()
            if not declared:
                why["perception"] = (
                    "没有任何量声明过可见性 —— 用 declare_visibility() 声明,"
                    "否则默认 hidden(谁都感知不到)"
                )
            else:
                why["perception"] = (
                    f"已声明 {len(declared)} 条可见性,但这一刻这个角色一个都感知不到:"
                    "要么对应的量还没有值,要么都是 self/here 而她身边没有那些东西"
                )
        check("overrides", "这个玩家还没教过她对话规则(set_persona_override)")
        # identity 不在这儿:没传 display_name 时**真聊天也会兜底**成 `player-xxxx`,
        # 所以它永远在场。给它写一条"缺席理由"就是一段假装解释的死代码 —— 而调试
        # 视图里的死代码最坏:你会照着一句永远不会出现的话去找原因。
        check("extra", "本轮没有临时插入的块(拒谈话题/loop 提示)")
        for label, key in (("stance", "chat.stance.enabled"), ("tools", "chat.tools.enabled")):
            if label in present:
                continue
            why[label] = (
                f"{key} 是关着的"
                if not self.config_get(key)
                else f"{key} 开着但这一刻渲染出来是空的(模板被改空了?)"
            )
        return why

    def rules(self) -> list[dict[str, Any]]:
        """这个世界的规律(编译过的,只读视图)。"""
        return [
            {
                "id": rule.id,
                "every_ticks": rule.interval_ticks,
                "for_each": {rule.selector_kind: rule.selector_value},
                "set": {key: str(expression) for key, expression in rule.outputs.items()},
                "when": [str(condition) for condition in rule.conditions],
                "emit": [{"when": str(e.when), "type": e.type} for e in rule.emits],
                "reads": sorted(rule.reads()),
            }
            for rule in self.scheduler.world_rules
        ]

    def rule_stats(self) -> dict[str, Any]:
        """规律引擎跑得怎么样:算了几次、写了几个量、发了几条门槛事件、跳过几次。

        和 `autonomy_stats()` 同一个理由:这条链最容易的坏法是"看着都对、其实
        一次没算" —— 而一个手滑的公式会被逐条跳过并只留一条日志。

        ⚠️ **本次运行内的计数,不是历史**:内存态,重开世界即清零(它是诊断,不是
        账)。刚打开一个世界就看到全零是正常的,那不代表规律没跑过 —— 存量的
        `updated_tick` 才是"这条规律确实算过"的凭据。
        """
        return dict(self.scheduler._rule_stats)

    # ── autonomy:没人跟她说话时的定时轮次 ──────────────────────────────────

    def _autonomy_enabled(self) -> bool:
        return bool(self.config_get("autonomy.enabled", False)) and bool(
            self.config_get("chat.tools.enabled", False)
        )

    def _install_autonomy(self) -> None:
        """把定时轮次挂到时钟上(每次读配置,所以热改开关立即生效)。"""
        interval = int(self.config_get("autonomy.interval_ticks", autonomy.DEFAULT_INTERVAL_TICKS) or 0)
        self.scheduler._autonomy_interval = max(1, interval)
        self.scheduler._autonomy_hook = self._on_autonomy_due

    def _on_autonomy_due(self, agent_ids: list[str], now: Any) -> None:
        """时钟喊到点了。**在这里只做快照与投递,然后立刻返回。**

        调用它的是 tick 线程,而这一轮要打 LLM —— 引擎最老的一条不变量是"时钟永远
        不等网络"。快照在锁内取(调用方已经持锁),决定与执行丢到世界自己那条事件
        循环上跑。
        """
        if not self._autonomy_enabled() or self._closed:
            return
        day = int(getattr(now, "day", 0))
        contexts = []
        for agent_id in agent_ids:
            if self._autonomy_done.get((agent_id, day), 0) >= self._autonomy_cap():
                continue   # 今天她已经主动过够多次了
            snapshot = self._autonomy_context(agent_id, now)
            if snapshot is not None:
                contexts.append(snapshot)
        if not contexts:
            return
        for ctx in contexts:
            self._autonomy_done[(ctx.agent_id, day)] = (
                self._autonomy_done.get((ctx.agent_id, day), 0) + 1
            )
        # fire-and-forget,但**不是丢了不管**:`run_coroutine_threadsafe` 返回一个
        # concurrent.futures.Future,没人读它的话一次异常就无声无息地消失
        # (最多是 GC 时一句 "exception was never retrieved",没人会去翻日志找它)。
        # 这个包最忌讳的就是"照跑但给错东西"——所以挂一个回调,把异常喂回
        # `autonomy_stats()`,让"这条链是不是通的"始终有据可查。
        future = asyncio.run_coroutine_threadsafe(
            self._autonomy_round(contexts, day), self._bridge._loop
        )
        future.add_done_callback(self._on_autonomy_round_done)

    def _on_autonomy_round_done(self, future: Any) -> None:
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001 - 记录下来,不许无声
            logger.warning("autonomy round crashed", exc_info=True)
            self._autonomy_stats["last"] = f"自主轮次崩溃({type(exc).__name__}:{exc})"

    def _autonomy_cap(self) -> int:
        return max(0, int(self.config_get("autonomy.max_per_day", autonomy.DEFAULT_MAX_PER_DAY) or 0))

    def _autonomy_context(self, agent_id: str, now: Any) -> Any:
        """锁内一次快照(只读、无 LLM、无 IO)。worker 之后只碰这个对象。"""
        brain = self.scheduler.agents.get(agent_id)
        if brain is None:
            return None
        agent = brain.agent
        here = str(agent.blackboard.read("loc") or agent.location or "")
        activity = self._view._agent_activity(agent_id)
        label = _ACTIVITY_LABELS.get(activity.get("kind"), "闲着")
        present = [
            {"id": pid, "name": str((self.players.get(pid) or {}).get("display_name") or pid)}
            for pid in self.who_is_present()
            if str((self.players.get(pid) or {}).get("location") or "") == here
        ]
        notes: list[str] = []
        mood = agent.blackboard.read("need.mood")
        if mood is not None:
            notes.append(f"你此刻的心气儿:{float(mood):.2f}(0~1)")
        for person in present:
            rel = self.scheduler._memory_projection.relations.get((agent_id, person["id"]))
            if rel is not None:
                notes.append(f"{person['name']} 在你眼中:{rel.r_type}")
        # 她**感知到**的世界的量也进决定 —— 否则"矿富了所以我去挖"这种事永远不会
        # 发生:她做决定时看不见世界的任何量,而那正是模拟层和角色层脱节的地方。
        perceived = self._perceive(agent_id, here)
        if perceived is not None and not perceived.is_empty():
            if perceived.own:
                notes.append("你自己:" + "、".join(
                    f"{key} {value:g}" for key, value in sorted(perceived.own.items())))
            for owner, values in sorted(perceived.here.items()):
                name = perceived.labels.get(owner) or owner
                notes.append(f"这里的{name}:" + "、".join(
                    f"{key} {value:g}" for key, value in sorted(values.items())))
            if perceived.public:
                notes.append("人人都知道:" + "、".join(
                    f"{key} {value:g}" for key, value in sorted(perceived.public.items())))
        return autonomy.AutonomyContext(
            agent_id=agent_id,
            name=agent.name or agent_id,
            personality=str(agent.blackboard.read("personality") or ""),
            day=int(getattr(now, "day", 0)),
            hour=int(getattr(now, "hour", 0)),
            minute=int(getattr(now, "minute", 0)),
            location=self._location_display_name(here),
            activity=label,
            present=present,
            notes=notes,
        )

    async def _autonomy_round(self, contexts: list[Any], day: int) -> None:
        """一轮:问每个角色要不要做点什么,把她挑的那一个执行掉。

        跑在世界自己那条事件循环上(不是 tick 线程)。任何一个角色出错只影响她自己。
        """
        specs = tools_mod.tools_for("*", surface=tools_mod.AUTONOMY)
        menu = "\n".join(spec.prompt_line() for spec in specs)
        allowed = [spec.id for spec in specs]
        template = (
            self.scheduler.prompt_store.get("autonomy.decide", default=autonomy.DEFAULT_DECIDE_PROMPT)
            if self.scheduler.prompt_store is not None
            else autonomy.DEFAULT_DECIDE_PROMPT
        )
        for ctx in contexts:
            self._autonomy_stats["asked"] += 1
            try:
                messages = autonomy.build_messages(template, ctx, menu)
            except (KeyError, IndexError, ValueError):
                logger.warning("autonomy.decide 渲染失败,这轮跳过")
                self._autonomy_stats["last"] = "提示词渲染失败"
                continue
            try:
                reply = await self.chat_service._background_llm.complete(messages)
            except Exception as exc:  # noqa: BLE001 - 一次调用失败不该影响别人
                logger.warning("自主轮次的 LLM 调用失败:%s", exc)
                self._autonomy_stats["last"] = f"LLM 调用失败({type(exc).__name__})"
                continue
            decision = autonomy.parse_decision(reply, allowed)
            if not decision.get("acted"):
                # 什么都不做是常态,所以**不发事件**:一条"她想了想,没做"的事件
                # 每六小时一条,会把日志灌满而不带一点信息。
                self._autonomy_stats["quiet"] += 1
                self._autonomy_stats["last"] = f"{ctx.agent_id}:{decision.get('reason', '')}"
                # 没做就把额度退回去 —— 上限是"主动几次",不是"被问几次"。
                self._autonomy_done[(ctx.agent_id, day)] = max(
                    0, self._autonomy_done.get((ctx.agent_id, day), 1) - 1
                )
                continue
            target = str((decision.get("params") or {}).get("player_id") or "")
            result = tools_mod.call(
                tools_mod.ToolContext(
                    agent_id=ctx.agent_id, player_id=target,
                    runtime=self._tool_runtime, agent_name=ctx.name,
                ),
                decision["tool"], decision.get("params") or {},
            )
            if result.ok:
                self._autonomy_stats["acted"] += 1
                self._autonomy_stats["last"] = f"{ctx.agent_id}:{decision['tool']}"
                logger.info("%s 自己决定了:%s %s", ctx.agent_id, decision["tool"], result.detail)
            else:
                self._autonomy_stats["failed"] += 1
                self._autonomy_stats["last"] = f"{ctx.agent_id}:{decision['tool']} 没成 —— {result.error}"

    _world_lock: Any = None
    _sqlite_log: Any = None
    _durability_warning: str | None = None

    def _guard(self) -> Any:
        """跨进程的世界锁;没配 Redis 时是个不做事的上下文。

        **一个动作原子**这条要求跨进程也成立才有意义:两个 agent 进程同时提交动作,
        必须一个做完另一个才开始,否则 world-rules 的双缓冲、三源仲裁、`events.seq`
        的折叠顺序全都失去依据。
        """
        return self._world_lock if self._world_lock is not None else contextlib.nullcontext()

    def act(
        self,
        agent_id: str,
        verb: str,
        params: dict[str, Any] | None = None,
        *,
        player_id: str = "",
        surface: str = tools_mod.AUTONOMY,
    ) -> dict[str, Any]:
        """**以某个角色的身份做一件事。** 外面的进程改变这个世界的唯一入口。

        存在的理由:此前"她做了什么"只能由引擎内部触发 —— 聊天那一轮、定时轮次、
        节拍脚本。一个住在别的进程里、由 LLM 驱动的角色**碰不到任何动词**,
        于是"很多进程操作同一个世界"这件事在引擎这一侧是断的。这个方法就是那扇门。

        四条性质,每条都有理由:

        1. **一个动作是原子的。** 整个执行期持有世界那把唯一的锁(RLock,所以工具
           内部再拿一次是安全的)。这不是为了性能 —— 锁每次只持 62 微秒,而一次 LLM
           往返是 6.5 秒。是为了 world-rules 的双缓冲、三源仲裁、`events.seq` 的折叠
           顺序:这三件事都要求"一个动作期间世界不会从下面被换掉"。
        2. **在执行时校验,不在决定时。** 她想了 6.5 秒,决定送达时世界早就变了 ——
           所以"她还在不在场""走不走得掉"由动词自己在执行的那一刻查
           (`walk_away` 隔着手机降级成挂断就是这个模式)。**别在这里预先校验**,
           那会变成第二份判断,迟早和动词里那份分叉。
        3. **面是硬的。** `walk_away` / `end_conversation` 这些需要"对面有个人",
           在没人说话的场合是空动作。默认面是 `autonomy`(她自己决定做点什么),
           要聊天里那批就显式传 `surface="chat"` 并给 `player_id`。
        4. **坏调用不掀翻世界,但也不静默。** 未知动词 / 不在这个面上 / 工具自己
           失败,一律返回 `ok=False` 并说明原因(而不是抛异常)—— 一个 agent 进程
           挑错了动词,不该让世界跟着崩。未知角色仍然抛 `KeyError`:那是调用方
           搞错了对象,不是一次失败的尝试。

        返回 `{"tool", "params", "ok", ...}`,形状和聊天里那批工具调用**逐字相同**
        (共用 `ToolResult.to_dict`)—— 两条路的结果不该长得不一样。

        ⚠️ **它不推进世界的时间。** `docs/AGENT-RUNTIME.md` 里"时间是动作的副产品"
        那一半还没实施,现在仍然由调用方决定什么时候 `tick()`。这个方法只负责
        "做这件事并记一笔"。
        """
        if agent_id not in self.scheduler.agents:
            raise KeyError(f"agent {agent_id} not found")
        allowed = {spec.id for spec in tools_mod.tools_for(agent_id, surface)}
        if verb not in allowed:
            known = {spec.id for spec in tools_mod.tools_for(agent_id)}
            reason = (
                f"{verb!r} 在 {surface!r} 这个面上没有;它在 "
                f"{sorted(s for s in tools_mod.SURFACES if verb in {x.id for x in tools_mod.tools_for(agent_id, s)})} 上"
                if verb in known
                else f"没有 {verb!r} 这个动词;这个面上有:{sorted(allowed)}"
            )
            logger.warning("act(%s, %s) 被拒:%s", agent_id, verb, reason)
            return {"tool": verb, "params": dict(params or {}), "ok": False, "error": reason}

        name = ""
        brain = self.scheduler.agents.get(agent_id)
        if brain is not None:
            name = getattr(brain.agent, "name", "") or ""
        context = tools_mod.ToolContext(
            agent_id=agent_id, player_id=player_id,
            runtime=self._tool_runtime, agent_name=name,
        )
        with self._guard(), self.scheduler._lock:
            # 先把别的进程写进日志、这个进程还没折进来的事件补上 —— 否则会拿着一份
            # 过时的投影去判断"她买得起吗""他们认识吗"。
            self.scheduler.catch_up_projection()
            result = tools_mod.call(context, verb, dict(params or {}))
        if not result.ok:
            logger.info("act(%s, %s) 没成:%s", agent_id, verb, result.error)
        return result.to_dict(verb, dict(params or {}))

    def intend(
        self, agent_id: str, steps: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        """**告诉她接下来打算做什么** —— 一串动作,世界替她走完脚步。

        `act()` 是"现在做这一件事",`intend()` 是"接下来这几步"。区别不是语法糖:
        一个 LLM 驱动的进程**不该一步一次网络往返地编排走路** —— 那又贵又编得烂。
        她该说"先走到咖啡店,然后干活",剩下的交给世界。

        **调用即设定意图,不是执行到底。** 立刻返回;之后每个 tick 由仲裁器在
        [身体 → 她刚决定的 → 排班 → 空闲规划 → 兜底] 之间挑。所以:

        - **饿到一定程度她会先去吃**,吃完再回来接着走 —— 紧急带没被架空
        - **可被打断是特性**:路上被人叫住、被需求压倒,都该发生
        - 一步真的生效了队列才往前走一格(在途时会被重挑很多次,挑一次弹一次
          会把后面几步一起吃掉)

        `steps` 是 `[{"verb": "walk", "params": {...}}, {"verb": "work"}]`,
        动词必须在 `body` 面上(过日子的动作)。传 `None` 或空列表 = **取消**她
        当前的打算。返回 `{"agent_id", "queued", "steps"}`。
        """
        if agent_id not in self.scheduler.agents:
            raise KeyError(f"agent {agent_id} not found")
        allowed = {spec.id: spec for spec in tools_mod.tools_for(agent_id, tools_mod.BODY)}
        queue: list[dict[str, Any]] = []
        for index, step in enumerate(steps or []):
            verb = str((step or {}).get("verb") or "").strip()
            spec = allowed.get(verb)
            if spec is None:
                raise ValueError(
                    f"第 {index + 1} 步的 {verb!r} 不是过日子的动作;"
                    f"可用的是 {sorted(allowed)}"
                )
            queue.append({"kind": spec.kind, "params": dict((step or {}).get("params") or {}),
                          "verb": verb})
        with self._guard(), self.scheduler._lock:
            brain = self.scheduler.agents[agent_id]
            brain.agent.blackboard.write("intent.queue", queue)
        return {"agent_id": agent_id, "queued": len(queue), "steps": list(queue)}

    def intent(self, agent_id: str) -> list[dict[str, Any]]:
        """她此刻还打算做的事(队首是下一步)。做完一步就少一条。"""
        if agent_id not in self.scheduler.agents:
            raise KeyError(f"agent {agent_id} not found")
        with self.scheduler._lock:
            queue = self.scheduler.agents[agent_id].agent.blackboard.read("intent.queue")
        return [dict(step) for step in (queue or [])]

    def durability_warning(self) -> str | None:
        """这个世界的存储会不会在重启后忘掉它。不会就是 None。

        存在的理由是**降级不许无声**:Redis 主要活在内存里,而持久化是配置选项。
        忘掉的样子不是报错,是世界悄悄退回创世那一刻然后接着跑。
        """
        return self._durability_warning

    def verbs(self, agent_id: str = "*", surface: str | None = None) -> list[dict[str, Any]]:
        """这个角色在某个面上能做什么 —— `act()` 的配套目录。

        `surface=None` 给全部并逐条标出它在哪些面上。外面的 agent 进程要先知道
        自己能做什么,才谈得上选一个 —— **给了能力却不给目录,等于没给**。
        """
        specs = tools_mod.tools_for(agent_id, surface)
        return [
            {
                "id": spec.id,
                "kind": spec.kind,
                "description": spec.description,
                "params": {
                    key: dict(meta) if isinstance(meta, dict) else {"type": "string"}
                    for key, meta in spec.params_schema.items()
                },
                "surfaces": list(spec.surfaces),
                # 它把世界改在哪儿 —— 外面的进程不该靠猜
                "writes": list(spec.writes),
            }
            for spec in specs
        ]

    def autonomy_stats(self) -> dict[str, Any]:
        """定时轮次到底跑没跑、做没做。

        存在的理由是这条路**最容易静默地不工作**:开关点亮了、时钟在走,而她一次也
        没主动过 —— 那可能是"她确实没什么想做的"(正常),也可能是 hook 没挂上、
        LLM 一直失败、或者额度早就用完了。这四个数把它们分开。
        """
        return dict(self._autonomy_stats)

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
            rows = self.scheduler.conn.execute(
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
            conn = self.scheduler.conn
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
        """按声明类型强转后写入,立即生效。未知键抛 KeyError。

        **`llm.*` 写进这台机器,不写进世界**(见 `machine_config`)—— 和
        `anima-world config set` 同一条路由。两条门各走各的,只会让"我明明设了"
        变成一个取决于你走哪扇门的问题。
        """
        from anima_world import machine_config

        store = self.scheduler.config_store
        if store is None or not store.has(key):
            raise KeyError(f"config key {key} not found")
        meta = store.meta(key)
        if meta["is_secret"] and value == "":
            raise ValueError("secret value cannot be set to empty")
        value = coerce_to_declared_type(value, meta["value_type"])
        if key == "scheduler.tick_rate" and not (0 < float(value) <= MAX_TICKS_PER_SECOND):
            raise ValueError(f"'{key}' must be > 0 and <= {MAX_TICKS_PER_SECOND}")
        if machine_config.is_machine_key(key):
            machine_config.set_value(key, value)
            return
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
            # perception:世界的量里她感知得到的那些(没声明过可见性 = 什么也没有,
            # 这一层就不进提示词)。客观存在 ≠ 她知道 —— 混成一层就是无所不知的角色。
            perceived = self._perceive(agent_id, loc_id)
            if perceived is not None and not perceived.is_empty():
                ctx["perception"] = perceived
            return ctx

    def _perceive(self, agent_id: str, here: str) -> Any:
        """她此刻感知到的量。没有存量/没有声明就返回 None。"""
        store = self.scheduler.stock_store
        visibility = self.scheduler.visibility_store
        if store is None or visibility is None:
            return None
        from anima_world.perception import perceive

        try:
            return perceive(agent_id=agent_id, here=here, stock_store=store,
                            visibility=visibility)
        except Exception:  # noqa: BLE001 - 读不到感知不该让聊天告吹
            logger.warning("读 perception 失败", exc_info=True)
            return None

    def perception(self, agent_id: str) -> dict[str, Any]:
        """这个角色此刻**感知到**什么(不是世界有什么)。

        存在的理由是可查:可见性是声明出来的,而"我以为她知道/其实她不知道"是这一层
        最容易的错。这个函数就是那个对照面。
        """
        perceived = self._perceive(agent_id, self._tool_runtime.agent_location(agent_id))
        return {} if perceived is None else perceived.to_dict()


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
