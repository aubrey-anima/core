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
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Sequence

from anima_world import autonomy
from anima_world import contact
from anima_world import together
from anima_world import tools as tools_mod
from anima_world.actions import ActionDescriptor
from anima_world import chat_service as chat_service_mod
from anima_world.chat_service import ChatService
from anima_world.beats import coerce_goals
from anima_world.chat_session import ChatSessionManager
from anima_world.chat_state import ChatStateStore
from anima_world.chat_store import ChatStore
from anima_world.config_store import coerce_to_declared_type, mask_secret
from anima_world.intent import FIRST_PERSON, Director, read_self_introduction
from anima_world.llm_client import (
    create_background_llm_client_from_config,
    create_llm_client_from_config,
    create_llm_client_from_env,
)
from anima_world.locations import DEFAULT_POINTS
from anima_world.narrative import MockNarrativeProvider, OpenAICompatibleNarrativeProvider
from anima_world.scheduler import MAX_TICKS_PER_SECOND, Scheduler
from anima_world.types import AgentState, Projection
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
                # **投影只填缺,不覆盖。** 投影是从事件重折出来的过去,黑板上可能
                # 已经躺着别的进程写下的现在(她真的走到了新地方,只是那一步不发
                # 事件)。拿过去盖掉现在,就是"第二个 World.open 把她挪回 cafe"。
                if recovered.location:
                    agent.location = recovered.location
                    if agent.blackboard.read("loc") is None:
                        agent.blackboard.write("loc", recovered.location)
                for key, value in recovered.state.items():
                    if agent.blackboard.read(f"state.{key}") is None:
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

        # 她手上要花时间的事。**和 `transit` 并列不是巧合** —— 两者是同一种东西:
        # 一段她已经起了头、还没走完的时间。少了这一格,一个埋头做了十个月椅子的人
        # 在 `state()` 里看上去和闲着一模一样,而宿主的界面只认这里。
        engaged = [
            {
                "target": str(r.get("target") or ""),
                "verb": str(r.get("verb") or ""),
                "label": str(r.get("label") or r.get("verb") or ""),
                "remaining": max(0, int(r.get("ends", 0)) - int(self.scheduler.clock)),
                "occupies": bool(r.get("occupies")),
            }
            for _, r in self.scheduler._engaged.items()
            if r.get("agent") == agent_id
        ]
        if engaged:
            activity["engaged"] = sorted(engaged, key=lambda r: (r["remaining"], r["verb"]))

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
        """这个世界为什么在用 Mock 回答 —— "从没配过"和"读不回来"分得开的那一处。

        **是给人看的一句话**(`start` / `chat` / `play` / `run` 四个横幅、doctor
        的报告、宿主的界面都直接把它显示出来),所以说中文;键名 `llm.api_key`
        照旧原样,因为那正是他要去敲的那个东西。
        """
        config_store = self.scheduler.config_store
        if config_store is None:
            return "这个世界没有配置存储,也就没有 LLM 配置(纯内存跑)"
        undecryptable = getattr(config_store, "undecryptable_secrets", None)
        if callable(undecryptable) and "llm.api_key" in undecryptable():
            return "llm.api_key 读不回来"
        if not (config_store.get("llm.api_key", default="") or ""):
            return "llm.api_key 还没配"
        return None

    def _runtime_status(self, recent_events: list[dict[str, Any]]) -> dict[str, Any]:
        log = self.scheduler.event_log
        if log is not None:
            db_status = {"enabled": True, "world_id": self.scheduler.world_id}
            events_status = {
                "count": log.count(),
                "latest_seq": log.max_seq(),
                "buffered_count": len(recent_events),
            }
        else:
            db_status = {"enabled": False, "world_id": None}
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


# 赶路时仍然允许的动作:**路上可以说话**(老大 2026-08-08 定的)。
# 挡住的是手上的活 —— 没人能在半路上把觉睡了、把活干了。
_PLAYER_TRANSIT_OK = frozenset({"chat"})

# 一次能力调用被拒,分两种:**世界说不行**(下面这些)和**这次调用讲不通**
# (`unknown_entity` / `unknown_verb` / `no_ontology` / `error`)。只有后者才是
# `ToolCallError`。
#
# 分界为什么重要:`tools.call` 捕获 `ToolCallError` 时只留一句话
# (`ToolResult(ok=False, error=…)`,**没有 detail**),于是 `reason` 那个词在
# `World.act()` 的出口上就没了。而这一层的全部意义正是让这几类分得开 ——
# `conditions` 是"等一会儿"、`incapable` 是"她做不了,去歇着或者去变强"、
# `busy` 是"先把手上这件做完"、`absent` 是"走过去"、`declined`/`participant_gate`
# 是"换个人或者晚点再约"。合成一句话之后,拿 `act()` 驱动角色的宿主(它存在的
# 全部理由就是让别的进程里的角色够得着动词)只能去正则匹配中文散文;
# 一个累坏了的人于是挨棵树轮着试过去,每一棵都回她"再等等"。
#
# 她读到的那句话一直是对的 —— 坏的是**程序读到的那一份**。
_WORLD_SAID_NO = frozenset({
    "conditions", "participant_gate", "incapable", "busy", "absent", "declined",
})


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
            if self._world.player_location(pid) == here
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
        where = self._world.player_location(player_id).strip()
        return bool(here) and bool(where) and here == where

    def claim_hail(self, agent_id: str, player_id: str) -> str:
        """`reach_out` 的"她今天开过口了吗"那道闸 —— 和闲着时的搭话共用一个水位。
        能搭话返回空串并当场记下,不能就返回一句人话的理由(见
        `Scheduler.claim_hail`)。"""
        return self._world.scheduler.claim_hail(agent_id, player_id)

    def point_ids(self) -> list[str]:
        store = self._world.scheduler.location_store
        if store is None:
            return sorted(str(row["id"]) for row in DEFAULT_POINTS)
        return sorted(
            str(row["id"]) for row in store.all()
            if (row or {}).get("kind", "point") == "point"
        )

    def point_names(self) -> dict[str, str]:
        """地点 id → 人话名。**玩家嘴里的地名永远是这一份,不是 id 那一份**:
        他说的是「哈尔滨」,世界里躺着的是 `harbin-icecity` / 哈尔滨·冰雪大世界。
        没有名字的地点回落成 id,所以调用方拿到的永远是一份满的表。"""
        store = self._world.scheduler.location_store
        if store is None:
            return {str(row["id"]): str(row.get("name") or row["id"]) for row in DEFAULT_POINTS}
        return {
            str(row["id"]): str((row or {}).get("name") or row["id"])
            for row in store.all()
            if (row or {}).get("kind", "point") == "point"
        }

    def player_location(self, player_id: str) -> str:
        """玩家这会儿在哪。宿主没调过 `player_move` / `player_walk` 就是没告诉
        世界 —— 返回空串,引擎不猜(和 `face_to_face` 同一条规矩)。

        走 `World.player_location`:到点落地这件事只在那**一处**结算,读的人
        不用各自记得先跑一次。"""
        return self._world.player_location(player_id)

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

    def entity_names(self) -> dict[str, str]:
        """东西的 id → 人话名。**给玩家嘴里那个名字用的。**

        和 `point_names` 逐字同一个用途:玩家说「天鹅冰雕」,世界里那个东西叫
        `icesculpture:swan`、名字是「半成的天鹅冰雕」。不认名字的话,一句
        「你去雕那座天鹅冰雕」的下场是"这儿没有 天鹅冰雕 这个东西" —— 一句
        技术上没错、而玩家读起来是谎的回执。
        """
        ontology = self._world.scheduler.ontology
        if ontology is None:
            return {}
        return {e.id: (e.name or e.id) for e in ontology.entities.values()}

    def give_item(self, player_id: str, agent_id: str, wanted: str) -> dict[str, Any]:
        """玩家把随身的一样东西交给一个角色。**只走账本,不凭空造物。**

        `wanted` 是玩家嘴里的说法(「红围巾」),不是 id —— 所以先在**他手上有的
        那些**里面认名字,认不出来就抛 `LookupError`。反过来做(先认全世界的物品
        定义、再查有没有)会让"你没有这个"和"世界里没这个"给出同一句回执,而玩家
        的下一步完全不同。

        为什么不加一条"凭空给她一件"的路:库存是 `item_transfer` 的投影,不记账
        的东西下一次重放就没了;而记了账却没有来源,等于一句话就能印钱。要给她
        一件她没有的东西,先让玩家自己有(`player_buy` / `player_topup`)。
        """
        scheduler = self._world.scheduler
        if agent_id not in scheduler.agents:
            raise tools_mod.ToolCallError(f"没有 {agent_id} 这个人")
        holder = f"player:{player_id}"
        with scheduler._lock:
            held = dict(scheduler._memory_projection.inventories.get(holder, {}))
        if not held:
            raise LookupError("你手上什么也没有")
        store = scheduler.economy_store
        names = {}
        if store is not None:
            names = {str(r["id"]): str(r.get("name") or r["id"]) for r in store.items()}
        item_id = self._match_item(wanted, held, names)
        if item_id is None:
            readable = "、".join(names.get(i, i) for i in held)
            raise LookupError(f"你手上没有{wanted} —— 你带着的是 {readable}")
        loc = str((self._world.players.get(player_id) or {}).get("location") or "") or None
        # 名字**随事件走**,不靠事后回查。她记住的那句话是"阿檀把速写本给了我",而
        # 玩家的显示名住在 `World.players` 这个刻意的内存态里、物品名住在经济表里 ——
        # 两样都可能在重放那一刻不在手上,于是同一条事件重放出来会变成
        # "8f3c-… 把 sketchbook 给了我"。名字是**那一刻的事实**,和事件一起存才对得上。
        with scheduler._lock:
            self._world._record_and_fan({
                "type": "item_transfer", "who": holder, "loc": loc,
                "payload": {"from": holder, "to": agent_id, "item_id": item_id, "qty": 1,
                            "from_name": self.player_name(player_id),
                            "item_name": names.get(item_id, item_id)},
            })
            scheduler.checkpoint()  # 交互即检查点
        return {"item_id": item_id, "item_name": names.get(item_id, item_id),
                "to": agent_id, "qty": 1}

    @staticmethod
    def _match_item(wanted: str, held: dict[str, int], names: dict[str, str]) -> str | None:
        """玩家嘴里的东西 → 他手上那件的 id。由准到松,第一层命中就收手。"""
        text = str(wanted or "").strip()
        if not text:
            return None
        if text in held:
            return text
        folded = text.casefold()
        for candidates in (
            [i for i in held if str(names.get(i, "")).casefold() == folded],
            [i for i in held if i.casefold() == folded],
            [i for i in held if folded in str(names.get(i, "")).casefold()
             or str(names.get(i, "")).casefold() in folded],
        ):
            if len(candidates) == 1:
                return candidates[0]
            if candidates:
                return None  # 说得不够准,别替他挑
        return None

    # ── 一起做事:同意那一段 ────────────────────────────────────────────────
    #
    # **同意在这里问,不在调度器里问** —— 因为读人设的判定要打网络,而调度器跑在
    # 世界的锁里、也跑在 tick 线程上(时钟永不等网络,和叙事 / 规划 / 关系判定
    # 同一条)。这一层问完,把点过头的名单交进 `perform_affordance`,那里再把
    # 世界那一段的闸重查一遍(`joint_gate`)—— 决定与执行之间世界还在跑。

    def _resolve_party(self, agent_id: str, raw: Sequence[str]) -> tuple[list[str], str]:
        """玩家和模型嘴里的名字 → 世界里的 id。返回 `(名单, 认不出的那个)`。

        三种写法都要认得:角色 id、角色名字、以及**玩家**(`player:<id>`、
        玩家的显示名、或者一句「我」)。认不出来当场说,别静默丢掉一个人 ——
        丢掉之后人数就对不上 `participants.min`,而报出来的会是"人不够",
        于是真正的原因("我不认识白霜")永远说不出口。
        """
        names = self.agent_names()
        by_name = {str(v).strip(): k for k, v in names.items()}
        players = {
            pid: str((self._world.players.get(pid) or {}).get("display_name") or pid)
            for pid in self._world.players
        }
        out: list[str] = []
        for raw_one in raw:
            who = str(raw_one or "").strip()
            if not who:
                continue
            if who.startswith("player:"):
                out.append(who)
                continue
            if who in names:
                out.append(who)
                continue
            if who in by_name:
                out.append(by_name[who])
                continue
            matched = next(
                (f"player:{pid}" for pid, name in players.items()
                 if who in (pid, name) or who in FIRST_PERSON),
                "",
            )
            if matched:
                out.append(matched)
                continue
            return ([], who)
        return (list(dict.fromkeys(out)), "")

    def _consent(
        self, agent_id: str, target: str, verb: str, party: Sequence[str],
        *, player_id: str = "",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """挨个问过去。返回 `(答应了的, 没答应的)`,两边都是 `Consent.to_dict()`。

        **两段,次序是有意的**:先世界(`joint_gate` + 静音 + 玩家在不在跟前),
        再性格。一句笼统的"她没答应"会让玩家以为被拒绝的是这个人,而真正的原因
        可能只是他在赶路 —— 那种误解在世界里是**改不回来**的。
        """
        scheduler = self._world.scheduler
        judge = scheduler.relationship_judge
        judge_invite = getattr(judge, "judge_invite", None) if judge is not None else None
        min_willingness = float(self._world.config_get(
            "social.joint.min_willingness", together.DEFAULT_MIN_WILLINGNESS))
        stock_key = str(self._world.config_get(
            "social.joint.consent_stock", together.DEFAULT_CONSENT_STOCK) or "").strip()
        inviter = self.agent_names().get(agent_id, agent_id)
        verb_label, target_name = self._affordance_display(target, verb)

        accepted: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        for who in party:
            invitee = self._invitee(
                agent_id, target, verb, who,
                player_id=player_id, stock_key=stock_key,
            )
            if invitee.gate:
                refused.append(together.decide_alone(
                    invitee, min_willingness=min_willingness).to_dict())
                continue
            if invitee.is_player:
                # 玩家过了在场那道闸就是答应 —— **他就是发起这次调用的那个人**。
                # 替他去问一个 LLM 是荒谬的:他刚刚按下了那个按钮。
                accepted.append(together.Consent(
                    who=who, accepted=True, source="gate", note="你自己点的头",
                ).to_dict())
                continue
            verdict = None
            if judge_invite is not None:
                others = [
                    self._display(p) for p in party if p != who
                ]
                try:
                    verdict = judge_invite(
                        a={"name": invitee.name, "personality": invitee.personality},
                        inviter=inviter,
                        invitation=together.describe_invitation(
                            inviter=inviter, verb_label=verb_label,
                            target_name=target_name, others=others,
                        ),
                        relation={"a_to_b": together.closeness(invitee.relation)},
                        memories=list(invitee.memories),
                        location=self.agent_location(who),
                    )
                except Exception:  # noqa: BLE001 - 判定器挂了不该掀翻这次调用
                    logger.warning("邀请判定失败 %s ← %s", who, agent_id, exc_info=True)
                    verdict = None
            # **降级不许无声。** 没有判定器(或者它这次没给出可用的回包)就退回
            # 关系与作者声明的量 —— 那仍然是一个真实的判断(理由见 together.py),
            # 但它和"读了人设的判定"不是同一件事,所以要点名。
            scheduler.note_subsystem(
                "joint_consent", verdict is not None,
                "" if verdict is not None else "没有可用的邀请判定(多半是没配 key)",
            )
            if verdict is None:
                decided = together.decide_alone(invitee, min_willingness=min_willingness)
            else:
                decided = together.Consent(
                    who=who, accepted=bool(verdict.accept),
                    reason="" if verdict.accept else together.DECLINE_REASON,
                    source="judge", note=verdict.reason,
                )
            (accepted if decided.accepted else refused).append(decided.to_dict())
        return (accepted, refused)

    def _display(self, who: str) -> str:
        if who.startswith("player:"):
            return self.player_name(who.split(":", 1)[1])
        return self.agent_names().get(who, who)

    def _affordance_display(self, target: str, verb: str) -> tuple[str, str]:
        """动词与东西的**人话**。判定器、回执、她的提示词读到的必须是同一个词 ——
        给一个 `tend` 过去,判定器判的是一件她根本没听说过的事。"""
        ontology = self._world.scheduler.ontology
        if ontology is None:
            return (verb, target)
        affordance = ontology.affordance_of(target, verb)
        entity = ontology.entities.get(target)
        return (
            (affordance.label or affordance.verb) if affordance is not None else verb,
            (entity.name or target) if entity is not None else target,
        )

    def _invitee(
        self, agent_id: str, target: str, verb: str, who: str,
        *, player_id: str, stock_key: str,
    ) -> together.Invitee:
        """把判断一个人要用到的东西收齐。**闸在这里合流**:调度器那几条
        (同地 / 赶路 / 睡着 / 手上有事 / 做不了)加上只有这一层知道的两条
        (她把他静音了、玩家在不在她跟前)。"""
        scheduler = self._world.scheduler
        if who.startswith("player:"):
            pid = who.split(":", 1)[1]
            gate = ""
            if player_id and pid != player_id:
                # 一次调用只替**一个**玩家说话。替别人点头等于把他的意志也取消掉,
                # 而那正是这一层要挡的东西 —— 只是换成了玩家。
                gate = "player_not_you"
            elif not self.face_to_face(agent_id, pid):
                gate = "player_not_here"
            return together.Invitee(
                id=who, name=self.player_name(pid), is_player=True, gate=gate,
            )
        gate = scheduler.joint_gate(agent_id, target, verb, who)
        if not gate and player_id and self._world.chat_state is not None:
            if self._world.chat_state.quiet_until(who, player_id) is not None:
                # 他刚把这个玩家静音 —— 这一层再把他拉进来,等于引擎撤销他的选择。
                gate = "muted"
        brain = scheduler.agents.get(who)
        personality = ""
        if brain is not None:
            personality = str(brain.agent.blackboard.read("personality") or "")
        agreeableness = 1.0
        if stock_key and scheduler.stock_store is not None:
            raw = scheduler.stock_store.of(f"agent:{who}").get(stock_key)
            if raw is not None:
                # **没声明 = 1.0**,和 `contact.initiative_stock` 逐字同构:
                # 声明本身就是开关,没写这个量的世界这一格整个不存在。
                try:
                    agreeableness = float(raw)
                except (TypeError, ValueError):
                    agreeableness = 1.0
        memories: tuple[str, ...] = ()
        if scheduler.memory_store is not None:
            try:
                memories = tuple(
                    str(m.get("summary") or "")
                    for m in scheduler.memory_store.query(agent_id=who)[:3]
                )
            except Exception:  # noqa: BLE001 - 读不到记忆不该挡住一次邀请
                logger.debug("读 %s 的记忆失败", who, exc_info=True)
        stance = ""
        # stance 关着的世界这一格是 1.0(`together.STANCE_FACTORS["neutral"]`),
        # 所以**行为逐位不变** —— 和 contact 那一层逐字同一条。
        if (
            self._world.chat_state is not None and player_id
            and self._world.config_get("chat.stance.enabled", False)
        ):
            try:
                row = self._world.chat_state.stance(who, player_id)
                stance = str((row or {}).get("stance") or "")
            except Exception:  # noqa: BLE001 - 读不到姿态不该挡住一次邀请
                logger.debug("读 %s 对 %s 的 stance 失败", who, player_id, exc_info=True)
        return together.Invitee(
            id=who, name=self.agent_names().get(who, who),
            gate=gate,
            relation=scheduler._memory_projection.relations.get((who, agent_id)),
            agreeableness=agreeableness, stance=stance,
            personality=personality, memories=memories,
        )

    def interact_with(
        self, agent_id: str, target: str, verb: str,
        participants: Sequence[str] | None = None, player_id: str = "",
    ) -> dict[str, Any]:
        """对一样东西做一件事 —— 把本体声明的能力真的兑现在世界的量上。

        这一条补的是本体层最后那半步:她的提示词里写着"可以照料",而在这之前
        **没有任何路径让她照料**。`tools/base.py` 开头那句"声明了却没人兑现的能力,
        比没有更坏"说的就是它。

        四道闸,每道都对着一种"能跑但给错东西":没有本体的世界(这一层本就没开)、
        不认识的东西、不认识的动词、以及**东西不在她这儿** —— 最后一条是在场语义,
        和 `walk` 拒绝不存在的地名同一条规矩:隔着半个地图照料一棵树,世界的量会
        真的变,而没有一行日志说这不对劲。

        **实现委托 `Scheduler.perform_affordance`**,和排班里那个 `interact` 动作
        走同一条(理由见 `do_action`:另写一份迟早分叉)。这一层只做一件事 ——
        把"讲不通的调用"翻成 `ToolCallError`,把"世界说这会儿不行"原样交出去。

        `participants` 是**一起做这件事的人**(名字或 id 都认;`player:<id>` 或者
        一句「我」指玩家)。同意在这一层问完(**锁外**,因为读人设的判定要打网络),
        点过头的名单才交进调度器 —— 那里再把世界那一段重查一遍。
        """
        scheduler = self._world.scheduler
        party_raw = list(participants or ())
        party: list[str] = []
        consents: list[dict[str, Any]] = []
        if party_raw:
            # **先问"这个调用讲不讲得通",再去问人。** 反过来的话,一个"这件事根本
            # 不用别人一起做"的调用会先把人问一遍,而回执写的是"沈遥他不想" ——
            # 于是调用方去改名单,而错的是动词。
            bad_shape, refusal = scheduler.joint_precheck(
                target, verb, len(party_raw)
            )
            if bad_shape:
                raise tools_mod.ToolCallError(refusal)
            party, unknown = self._resolve_party(agent_id, party_raw)
            if unknown:
                raise tools_mod.ToolCallError(
                    f"我不认识{unknown} —— 这个世界里现在只有"
                    f"{'、'.join(self.agent_names().values())}"
                )
            if agent_id in party:
                raise tools_mod.ToolCallError("她不能跟自己一起做一件事")
            accepted, refused = self._consent(
                agent_id, target, verb, party, player_id=player_id
            )
            consents = [*accepted, *refused]
            if refused:
                # **点名说出是谁、为什么。** 一句"没人答应"会让下一步无从谈起:
                # 该换个人、该等他睡醒、还是该死心,是三件完全不同的事。
                who = refused[0]
                name = self._display(str(who.get("who") or ""))
                gate = together.GATE_LABELS.get(str(who.get("reason") or ""))
                if gate is not None:
                    # 世界那一段:回执是「名字 + 一句状态」。
                    refusal = f"{name}{gate}"
                else:
                    # 她自己那一句 —— **原话进引号**。混在陈述句里的话,"白霜不想
                    # 凑在一起"读起来像引擎的判词,而那句话是她说的。
                    note = str(who.get("note") or "").strip()
                    refusal = (
                        f"{name}:「{note}」" if note
                        else f"{name}{together.DECLINE_LABEL}"
                    )
                return {
                    "ok": False, "target": target, "verb": verb,
                    "reason": "declined", "refusal": refusal,
                    "consents": consents,
                }
            party = [str(c["who"]) for c in accepted]
        with scheduler._lock:
            outcome = scheduler.perform_affordance(agent_id, target, verb, party)
            if outcome.get("ok"):
                self._world._view._fan_out()
                scheduler.checkpoint()  # 交互即检查点(RLock,可重入)
        if consents:
            outcome = {**outcome, "consents": consents}
        if not outcome.get("ok") and outcome.get("reason") not in _WORLD_SAID_NO:
            raise tools_mod.ToolCallError(str(outcome.get("refusal")))
        return outcome

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

    def player_do_action(self, player_id: str, kind: str, params: dict[str, Any]) -> bool:
        """人做的那一下。**动词和后果与她共用一份,执行器不共用** —— 她有行为树,
        人没有:`emit_action` 每一步都拿着一个 `Agent`。

        返回 `False` 同样是"世界这会儿不接",不是异常。
        """
        world = self._world
        if kind == "walk":
            where = str((params or {}).get("location") or "").strip()
            try:
                world.player_walk(player_id, where)
            except (KeyError, ValueError):
                return False
            return True
        # 「路上可以说话」——赶路挡住的是**手上的活**,不是嘴。所以 chat 放行,
        # 干活/吃饭/睡觉不放:一个人没法在半路上把觉睡了。
        if world.player_in_transit(player_id) and kind not in _PLAYER_TRANSIT_OK:
            return False
        here = world.player_location(player_id)
        if kind == "chat":
            target = str((params or {}).get("target") or "").strip()
            if not target or target not in world.scheduler.agents:
                return False
            # 同地才搭得上话 —— 和 `Scheduler._is_colocated` 逐字同一条规矩
            if not here or self.agent_location(target) != here:
                return False
        world.player_action(player_id, kind, dict(params or {}))
        return True

    def close_conversation(self, agent_id: str, player_id: str) -> bool:
        active = self._world.chat_store.active_conversation(agent_id, player_id=player_id)
        if active is None:
            return False
        return self._world.close_conversation(int(active["id"]))


class World:
    """一个打开的世界:时钟、状态、聊天、玩家、配置,全部函数化。

    用 `World.open(world_id, redis=…)` 打开,用完 `close()`(或 with 语句)。
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

        # 聊天子系统:转录跟着"无限增长归 MySQL"走,没给 MySQL 就住 Redis。
        if scheduler.event_log is None:
            raise ValueError("World requires a persistent scheduler (event_log wired)")
        redis_client = getattr(scheduler, "redis", None)
        if redis_client is None:
            raise ValueError(
                "World requires a Redis-backed scheduler (build_serve_scheduler)"
            )
        # 同步门面的异步工作全跑在这一条循环上(见 `_BridgeLoop`:一个跨循环复用的
        # HTTP 连接池是"每轮泄一条连接,某天忽然全炸"那种坏)。
        self._bridge = _BridgeLoop()
        mysql_conn = getattr(scheduler, "mysql_conn", None)
        if mysql_conn is not None:
            from anima_world.mysql_state import MySQLChatStore

            self.chat_store = MySQLChatStore(
                mysql_conn, getattr(scheduler, "mysql_prefix", ""), lock=scheduler._lock
            )
        else:
            from anima_world.redis_state import RedisChatStore

            self.chat_store = RedisChatStore(
                redis_client, scheduler.world_id, lock=scheduler._lock
            )
        # chat-agent(1.3.0):一轮聊天要读写的当前值(stance / 静音 / 拒谈话题 /
        # 玩家教的规则)。转录不归它,只经 transcript 转发逐轮观测量。
        from anima_world.redis_state import RedisChatStateStore

        self.chat_state = RedisChatStateStore(
            redis_client, scheduler.world_id, transcript=self.chat_store
        )
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
            place_name=self._location_display_name,
        )
        # autonomy:(角色, 世界日) -> 今天主动过几次。**内存态是对的** ——
        # 上限是"别把玩家的收件箱刷满",不是需要审计的账,重启即清可以接受。
        self._autonomy_done: dict[tuple[str, int], int] = {}
        # 那四个"这条链通没通"的计数**不一样,它要给别的进程看**:一个人开着
        # `anima-world run`,想知道"她到底主动过没有"只能另开一个进程问,而
        # 内存态的答案永远是全 0 —— 那正是这四个数存在的理由(分开"她不想做"和
        # "根本没跑")在进程外反过来给了错答案。所以这里只是**写缓冲**,
        # 真相发布在 `:meta` 上,`autonomy_stats()` 一律读那一份。
        self._autonomy_stats: dict[str, Any] = {
            "asked": 0, "acted": 0, "quiet": 0, "failed": 0,
            "last": None, "last_failure": None,
        }
        # contact:那条链"通没通"的计数。**冷却与次数不在这里** —— 那两样落库
        # (`RedisContactStore`),因为重启不该把所有人的冷却一起清零。这里只是诊断。
        self._contact_stats: dict[str, Any] = {
            "checked": 0, "fired": 0, "blocked": 0, "composed": 0,
            "compose_failed": 0, "last": None,
        }
        # 在场玩家(刻意内存态:重启即新访;持久的部分——会话/记忆/关系——在 db 里)
        self.players: dict[str, dict[str, Any]] = {}
        self.player_ttl_seconds: float = _PLAYER_TTL_SECONDS
        # 玩家的行程。**人走路和她走路一样要花时间** —— 同一份 `_travel_minutes`,
        # 同一条 `travel` 事件。和角色那份(`scheduler._transit`)分开放,因为落地
        # 的方式不同:她由 tick 循环 `_land_arrivals` 放下,人是**读的时候结算**
        # (下面 `player_location`)—— 玩家没有 tick 循环替他跑,而"到点了却没人
        # 把他放下"会让他永远停在路上。惰性结算是这个引擎里的现成套路:
        # `quiet_until` / `refused_topics` 也是读到就顺手清过期的。
        self._player_transit: dict[str, dict[str, Any]] = {}
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
        self._install_contact()    # "她想起你"挂到时钟上(同上)

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
        world_id: str,
        *,
        redis: Any,
        mysql: Any = None,
        world_file: str | None = None,
        beats_path: str | None = None,
        agents: int | None = None,
        force_mock_llm: bool = False,
    ) -> "World":
        """打开(或创建)一个世界。**世界住在 Redis 里,`world_id` 是它的名字。**

**创世与还原是同一个动作**:往这个前缀里装一个 `world_file`(缺省是内置的
        演示世界)。文件里的**状态层**直接落键,**作者层**走编译(`duties` → 行为树、
        `money` → `payment` 事件);已有世界的作者层会被忽略并警告 —— 它只会被读进
        一个空世界。坏 beats 脚本在这里当场抛 BeatScriptError,不流到运行期。

        创世与重连共用一条纪律:**只填缺,不覆盖**(黑板 seed_missing、时钟
        setnx、每个播种函数空 store 才播)—— 接一个已经在跑的世界不许把她
        按回原点。`world_id` 进 Redis 键名:一个 Redis 实例上跑十个世界是常态,
        键撞车的后果是两个世界的角色共用一个脑子。

        `mysql` 给了的话,随时间无限增长的四样(events / memories /
        conversations / messages)归 MySQL —— 判据是"她带不带得进上下文"。
        **传工厂,别传裸连接**(`mysql=lambda: pymysql.connect(...)`)。
        """
        from anima_world.__main__ import build_serve_scheduler  # 延迟导入避免环
        from anima_world.redis_state import RedisLock, durability_warning, lock_key

        scheduler = build_serve_scheduler(
            world_id,
            redis,
            mysql=mysql,
            n_agents=agents,
            world_file=world_file,
            beats_path=beats_path,
            force_mock_llm=force_mock_llm,
        )
        world = cls(scheduler)
        # 跨进程的世界锁。**在调度器那把 RLock 之外,不是替代它** ——
        # 那把还被 threading.Condition 用着(等规划落地),而 Condition 要真线程锁。
        world._world_lock = RedisLock(redis, lock_key(world_id))

        # **开机点名:这个 Redis 会不会把世界忘掉。**
        # Redis 主要活在内存里,持久化是配置选项,而默认的 redis.conf 里 AOF
        # 是关的。忘掉的样子不是报错,是"世界悄悄退回创世那一刻然后接着跑"。
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
        beats_path: str | Path | None = None,
        summary: str = "",
        genre: str = "",
        setting: str = "",
        theme: str = "default",
    ) -> Any:
        """活体导出:世界不停,当场打出一个 `.cyberworld`(v3,gzip JSONL)。

        先刷检查点(needs / 反思水位),再**持世界锁流式 dump** —— 锁只挡 dump
        那一段,压缩与落盘在锁外。导出的是**状态层**:一个跑过的世界没有作者层,
        它的"本来是什么样"已经被它后来的样子取代了。

        分发纪律不变,而且它们是安全条款不是格式细节:包里**零 secret**
        (`is_secret` 的配置行在 dump 时剥除)、不带 `lock`(JSON 存不了 TTL,
        装回去就是一把死锁)、不带 `owner_pid`/`owner_host`(装进新世界等于让一个
        还没人跑过的世界自称"有人在跑")。
        """
        from datetime import datetime, timezone

        import anima_world
        from anima_world.world_file import WorldFileManifest, write_world_file
        from anima_world.world_package import dump_world_records

        if self._closed:
            raise RuntimeError("world is closed")
        scheduler = self.scheduler
        scheduler.checkpoint()
        world_lock = getattr(self, "_world_lock", None)
        mysql_conn = getattr(scheduler, "mysql_conn", None)
        manifest = WorldFileManifest(
            world_id=world_id, name=name, summary=summary, genre=genre,
            setting=setting, theme=theme,
            engine_min=anima_world.__version__,
            source_engine_version=anima_world.__version__,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        if world_lock is not None:
            world_lock.acquire()
        try:
            with scheduler._lock:
                write_world_file(output_path, manifest, dump_world_records(
                    redis=scheduler.redis, world_id=scheduler.world_id, mysql=mysql_conn,
                ))
        finally:
            if world_lock is not None:
                world_lock.release()
        return manifest

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

    def repair_memory_ticks(self, *, dry_run: bool = False) -> dict[str, Any]:
        """把老世界里盖了墙钟的记忆 `tick` 折回世界时钟。**幂等**。

        修的是什么:2.0 之前 `chat_session.close_conversation` 给 `conversation`
        事件盖的是 `int(time.time())`,`TriggerEngine` 照抄进记忆的 `tick`。
        `MemoryStore.query` 按 `(tick, id)` DESC 排序,于是那几条跟玩家的对话
        **把每个角色的召回列表整个占满** —— 实测一个世界 382 条记忆里它们占 20 条
        (5%),而前 20 条 100% 是它们。喂给 planner、反思源、八卦源、叙事的都是
        这个列表。源头已经在 `chat_session` 修掉了,这个函数管**已经写下的那些**。

        为什么是就地换算,不是标记、也不是留着:

        - **留着**不行 —— 那批行不会自己过期,`tick` 是排序键,这个世界的角色
          从此永远只想得起跟玩家说过的话。
        - **标记**不行 —— 加一个"这条的 tick 不算数"的字段,等于要求每一个读
          `tick` 的地方都记得看它。`WALL_CLOCK_FLOOR` 那道闸就是这么来的,而它
          恰恰漏了记忆这一路:补闸的代价是你得**记得每一个消费方**。
        - **就地换算**能算准,所以选它:事件按 seq 单调,世界时钟单调不减,于是
          `event_seq` 之前最近的那条正常事件的 ts,就是这场对话关掉的那一刻。
          这不是估计。折法与 `TriggerEngine._tick_of` 是同一条 —— 两处不一致的
          话,修完再 `rebuild` 一次就又变回去了。

        **事件日志一个字不动。** 日志记的是"发生过什么",那条 ts 确实是当时写下
        的;记忆是能重折出来的派生数据,改它不改历史。每个按 tick 做算术的事件
        消费方(时钟恢复、运行摘要)本来就已经有 `WALL_CLOCK_FLOOR` 那道闸。

        返回 `{"scanned", "repaired", "unresolved", "rows"}`;`unresolved` 是查不到
        出处的行(没有 `event_seq`,或日志里找不到它)—— **这些一律不动**:
        编一个 tick 出来比留着更坏,因为它从此看不出来了。
        `dry_run=True` 只报不改。
        """
        from anima_world.world_time import WALL_CLOCK_FLOOR

        store = self.scheduler.memory_store
        log = self.scheduler.event_log
        if store is None:
            return {"scanned": 0, "repaired": 0, "unresolved": 0, "rows": []}

        # seq → 那一刻的世界时钟。和 `_tick_of` 同一条:上一条正常事件的 ts。
        resolved_at: dict[int, int] = {}
        watermark = 0
        if log is not None:
            for persisted in log.replay():
                ts = int(persisted.ts)
                if ts < WALL_CLOCK_FLOOR:
                    watermark = max(watermark, ts)
                resolved_at[int(persisted.seq)] = watermark

        rows: list[dict[str, Any]] = []
        repaired = unresolved = scanned = 0
        for agent_id in self.scheduler.agents:
            for row in store.query(agent_id=agent_id):
                if int(row.get("tick") or 0) < WALL_CLOCK_FLOOR:
                    continue
                scanned += 1
                seq = row.get("event_seq")
                fixed = resolved_at.get(int(seq)) if seq is not None else None
                if fixed is None:
                    unresolved += 1
                    rows.append({
                        "id": row["id"], "agent_id": agent_id, "kind": row.get("kind"),
                        "tick": row.get("tick"), "event_seq": seq, "repaired_to": None,
                    })
                    continue
                if not dry_run:
                    store.retick(int(row["id"]), fixed)
                repaired += 1
                rows.append({
                    "id": row["id"], "agent_id": agent_id, "kind": row.get("kind"),
                    "tick": row.get("tick"), "event_seq": seq, "repaired_to": fixed,
                })
        rows.sort(key=lambda r: int(r["id"]))
        return {
            "scanned": scanned, "repaired": repaired,
            "unresolved": unresolved, "rows": rows,
        }

    def repair_agent_goals(self, *, dry_run: bool = False) -> dict[str, Any]:
        """把被按**字**拆开的 `goals` 拼回来。**幂等**。

        修的是什么:创作台的世界生成器一度把模型回的一整行目标
        (`"摆脱母亲的控制；重新定义自己的人生"`)做成列表推导,于是每个角色背着
        十几个单字目标进了世界。它是 `list[str]`,形状合法,`{goals}` 照样逐条渲染
        进 planner 的提示词 —— 于是那个世界的九个人,每天都在照着一列没有意义的
        单字排一天的日子。产出侧已经堵住(创作台 `concept.py` 的 `_short_lines`),
        引擎这一头也堵住了(`beats.coerce_goals`),这个函数管**已经写下的那些**。

        **不必单独跑也能好**:开机时 goals 是从投影的 spec 经 `_coerce_goals` 重新
        折出来的,所以换上这版引擎重启一次,黑板上那份自己就修好了。这个命令存在
        是为了另外两件事 —— `--dry-run` 让人**在重启之前**看清会改成什么,以及
        把修好的那份**落进事件日志**,于是投影里的 spec 也跟着对。只修黑板不发
        事件的话,那份坏 spec 会一直躺在世界里,而下一个读它的人(换个引擎版本、
        或者任何一次 `events export`)又会拿到单字。

        走的是 `persona_update` 那条现成的路,不是就地改历史:日志记的是"发生过
        什么",而"目标被订正过"本身就是一件发生过的事。
        """
        rows: list[dict[str, Any]] = []
        for agent_id, brain in self.scheduler.agents.items():
            before = brain.agent.blackboard.read("goals") or []
            after = coerce_goals(before)
            if list(before) == list(after):
                continue
            rows.append({
                "agent_id": agent_id,
                "name": brain.agent.name,
                "before": list(before),
                "after": list(after),
            })
            if dry_run:
                continue
            with self.scheduler._lock:
                self.scheduler._apply_spec_to_blackboard(agent_id, {"goals": after})
                self._record_and_fan({
                    "type": "state_change",
                    "who": agent_id,
                    "payload": {"kind": "persona_update", "spec": {"goals": after}},
                })
        if rows and not dry_run:
            self.scheduler.checkpoint()
        return {"scanned": len(self.scheduler.agents), "repaired": len(rows), "rows": rows}

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
        # 他自报家门的话,在这儿就落库 —— **要早于 `_interlocutor_for`**,这一轮的
        # 身份块才读得到"他刚说了他叫什么"。记完照旧往下走:这一轮仍然是对话。
        self._note_self_introduction(agent_id, player_id, str(messages[-1].get("content") or ""))
        interlocutor = self._interlocutor_for(player_id, display_name, role)
        # 记住这个玩家叫什么。记忆文本里写的是名字,不是 id —— 检索 query 用得上
        # (见 world_context)。身份即参数,所以世界只在被告知时才知道。
        self._touch_player(
            player_id,
            name=interlocutor["display_name"],      # 他报过的名字,没报就是空
            display_name=interlocutor["address"],   # 给人和模型看的称呼,永不是 id
        )
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
        # "他上次出现"的水位(contact)。落在这里而不是 `_touch_player`:那一个是
        # 玩家侧的入口(不知道是跟谁),而"久别"是**她**和他之间的事。
        self._note_player_contact(agent_id, player_id, interlocutor["address"])
        return {
            "interlocutor": interlocutor,
            "user_text": str(messages[-1].get("content") or ""),
        }

    def _note_self_introduction(self, agent_id: str, player_id: str, user_text: str) -> None:
        """「我叫林越,你叫我小林就行」—— 记下来,然后**放行**(见
        `intent.read_self_introduction`)。

        兑现的是身份块自己许下的那个诺:它两支结尾都写着「他要是告诉了你名字,这一轮
        之后就照那个名字认他」,而在这之前没有一行代码做这件事。玩家报了名字,她当场
        叫得出来(那一轮的原文还在上下文里),下一场开局身份块又以"最高优先级事实"的
        口气说「他没有告诉过你他叫什么名字」。

        **只填空,不覆盖**:他改口("以后叫我老林")该走 `style_adjust` 那条明路,
        而不是被一句偶然命中的正则悄悄改掉 —— 和创世那条纪律同一个理由。
        """
        found = read_self_introduction(user_text)
        if not found:
            return
        try:
            known = {
                str(rule.get("kind")) for rule in self.chat_state.overrides(agent_id, player_id)
            }
            for kind, value in found.items():
                if kind not in known:
                    self.chat_state.set_override(agent_id, player_id, kind, value)
        except Exception:  # noqa: BLE001 - 记不下名字不该让这一轮聊天告吹
            logger.warning("记玩家自报的名字失败 agent=%s player=%s",
                           agent_id, player_id, exc_info=True)

    def _interlocutor_for(
        self, player_id: str, display_name: str | None, role: str
    ) -> dict[str, str]:
        """"对面那个人"是谁 —— **真聊天(`_chat_prelude`)和调试视图共用这一份**。

        两条路各拼一遍就会分叉,而这份 dict 正是身份块的全部输入;调试视图撒起谎来
        比没有调试视图更坏。(它已经撒过一次:`debug_prompt` 的 `role` 默认值
        `"player"` 盖过了世界里真正的身份,于是同一个世界,真聊天里她读到的是
        「身份是旅人」,调试视图里是「身份是player」。)

        **`display_name` 空着就让它空着 —— 不许兜底成一个假名字。** 从前这里兜底成
        `player-3f9a2c`,而身份块紧接着命令她「始终用这个名字称呼对方」:一条必然
        执行不了的命令。她于是自己编一个,而编出来的那个会进转录、进会话摘要、进她
        0.8 重要度的长期记忆。线上 `night-tide` 就是这么让玩家改名叫「旅人」的 ——
        照跑、报成功、日志一行不错。

        名字与称呼分成两格之后,"他还没说过名字"才是一件她说得出口的事。

        **宿主这一轮没给,回落到世界自己记着的那一格** —— 这不是上面禁的那件事。
        `players[pid]["name"]` 只有一个写点(`_chat_prelude`),写进去的只有宿主亲口
        传过的 `display_name`;出处仍然是宿主,纪律 3 没有松。松掉的是"世界明明记得
        却装作不知道":宿主第一轮传了「林越」,第二轮没传,她当场又不认识他了。
        (玩家**自己说**「我叫林越」是另一格 —— `player_name` override,由
        `chat_service.told_name` 读,身份块对这两种出处说的是两句不同的话。)

        **空串必须和 `None` 走同一支**:CLI 不给 `--name` 时传的就是 `""`,而回落只认
        `None` 的话,那个空串还会顺着 `_touch_player(name=…)` 把记着的名字**冲掉** ——
        第一轮认得他、从第二轮起永远不认识,日志一行不错。
        """
        known = self.players.get(player_id) or {}
        name = str(display_name or "").strip() or str(known.get("name") or "").strip()
        resolved_role = str(role or known.get("role") or "").strip()
        return {
            "display_name": name,     # 他报过的名字;空 = 他还没说过
            "role": resolved_role,
            "address": name or chat_service_mod.address_for(resolved_role),
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
            user_text,
            present=self._present_names(agent_id),
            recent=history[-5:],
            # 地点清单进分类器 —— 不给它,「你去哈尔滨」只能靠它猜一个地名出来,
            # 而猜出来的那个多半和世界里的 `harbin-icecity` 对不上。
            places=sorted(self._tool_runtime.point_names().items()),
            # 谁在被玩家称作「你」。不给的话分类器只能把 target 填成字符串"你",
            # 而那正是真模型第一次实测的样子(见 `Director._resolve`)。
            speaker=self._tool_runtime.agent_names().get(agent_id, agent_id),
        )
        outcome.update(verdict.to_dict())
        if verdict.intent == "style_adjust" and read_self_introduction(user_text):
            # 「我叫林越,你叫我小林就行」—— 这一轮**已经**在 `_note_self_introduction`
            # 里落库了,分类器给出的那份是同一件事的第二个答案,而它带来的不是重复,
            # 是**吞掉**:style_adjust 那条路以一句系统回执收尾,于是玩家开口说的
            # 第一句话她一个字都没答。真世界实测判成 `style_adjust(0.95)`,她回
            # 「（记下了:玩家的昵称 —— 小林。）」,**自我介绍换来一张收条**。
            # (`read_self_introduction` 的 docstring 写的就是"记下来之后必须让这一轮
            # 继续走对话" —— 那句诺在这儿之前没人守。)
            # 只挡 style_adjust:「我叫林越,你过来一下」里的导演那半照旧要兑现。
            logger.info(
                "这一轮是自报家门,已经记下了 —— 不按 style_adjust 收条处理 agent=%s player=%s",
                agent_id, player_id,
            )
            outcome["intent"] = "dialogue"
            outcome["reason"] = "自报家门已由世界记下,这一轮照旧是对话"
            return outcome
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
            directed = self._director.direct(
                agent_id=agent_id, params=verdict.params or {}, player_id=player_id,
            )
            outcome["detail"] = dict(directed.detail)
            outcome["ok"] = directed.ok
            if not directed.self_directed:
                # 指挥别人:照旧由这一句系统回执收尾,不再 in-character 生成。
                outcome["handled"] = True
                outcome["text"] = directed.text
                return outcome
            # 指挥她本人 —— **指令兑现,但绝不顶掉她的话**。她一边答应一边真的走,
            # 靠的是把"刚刚真发生了什么"塞进这一轮的提示词,而不是替她说一句系统确认。
            # 兑现不了时那句回执照旧要露出来(玩家得知道世界里没有"哈尔滨"),
            # 但它是**加在她的回话前面**,不是代替它。
            outcome["handled"] = False
            outcome["grounding"] = directed.grounding
            outcome["receipt"] = "" if directed.ok else directed.text
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
        # 指挥她本人那条路:指令已经兑现,但这一轮**还要继续走 in-character 生成**。
        # 两样东西因此得交给下游 —— 塞进提示词的那句事实,以及兑现不了时那句回执。
        if verdict.get("grounding"):
            sink["intent_grounding"] = verdict["grounding"]
        if verdict.get("receipt"):
            sink["intent_receipt"] = verdict["receipt"]
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
        # 指挥她本人:指令已经在世界里兑现了,这一轮照常由她自己开口 —— 只是她读到的
        # 提示词里多了一句"刚刚真发生了什么"。**顺序要紧**:回执在她的话前面,
        # 因为它说的是"这件事没能兑现",而她接下来那句是在这个前提下说的。
        grounding = str(sink.get("intent_grounding") or "")
        if grounding:
            extra_system.append(grounding)
        receipt = str(sink.get("intent_receipt") or "")
        if receipt:
            yield receipt + "\n"
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
        # 指挥她本人 —— 和 `_chat_agen` 逐条同构。两条路各写一遍的话,点亮
        # `chat.loop.enabled` 的世界里「你去哈尔滨」会静默地不接 grounding:
        # 她照走,却完全不知道自己已经动身了。
        grounding = str(sink.get("intent_grounding") or "")
        if grounding:
            extra_system.append(grounding)
        receipt = str(sink.get("intent_receipt") or "")
        if receipt:
            yield {"kind": "message", "text": receipt, "meta": dict(sink)}
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
        # 他刚跟她说过话(contact 的"久别"水位)。**这条门也要挂** —— 只走
        # `record_chat_turn` 的宿主(网站后端正是这个用法)不经 `_chat_prelude`。
        self._note_player_contact(agent_id, player_id, str(player.get("display_name") or ""))
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

    # ── 本体:世界里有哪些种类的东西,以及能对它们做什么 ─────────────────────

    def kinds(self) -> list[dict[str, Any]]:
        """这个世界声明过的种类。**能力表是这里唯一的权威。**

        宿主要画一个"她能对这棵树做什么"的界面,只有这一个地方问得到 ——
        `stocks()` 只给得出数字,而数字不告诉你 `tend` 这个词存不存在。
        猜一份动词表出来是这一层最容易犯的错:猜错了不报错,按钮点下去
        才发现世界不认。

        每行 `{"id","gloss","builtin","budget","quantities":[{"key","default",
        "visibility","label","unit"}],"affordances":[{"verb","label","duration",
        "occupies","changes_world","needs_actor","conditions":[…],"sets":[…],
        "requires":[…],"costs":[…],"consumes":{item_id: 几个}}]}`。

        `duration > 0` 的能力是**长过程**:调用它只是起个头(代价当场付),效果
        要到 `duration` 个 tick 之后才落。`occupies` 说这段时间占不占用她 ——
        占用的那种一次只能有一件,期间别的能力一律拒绝(`reason == "busy"`)。
        `label` 是她提示词里读到的那几个字,也是宿主界面上该印的那几个字。
        条件与效果给的是**源表达式的字符串**,照着作者写的样子 —— 宿主要显示
        "照料:树高 < 最大树高 时可用"。

        `participants` 是**得有人一起做**的那一格(`{"min","max"}`,`None` = 单人)。
        它决定 `act(…, "interact", {…, "with": [...]})` 要不要带一份名单 ——
        宿主界面上这一格就是"叫谁一起"那个选人框该不该出现。

        `requires` / `costs` / `consumes` 是关于**施动者**的那一半(`me_*` 读她身上
        的量,`have_*` 读她随身带着几个某样东西),和 `conditions` / `sets` 分开列
        而不是拼在一起:界面上"这棵树还没长好"和"你没力气了 / 你没带剪子"要能显示成
        两句不同的话,因为她该做的事不一样。
        她身上有哪些量,看 `id == "agent"` 那一行的 `quantities`。
        `consumes` 自带一道"你得有"的门,不会另外出现在 `requires` 里。
        """
        ontology = self.scheduler.ontology
        if ontology is None:
            return []
        return [
            {
                "id": kind.id,
                "gloss": kind.gloss,
                "builtin": kind.builtin,
                "budget": ontology.budget_of(kind.id),
                "quantities": [
                    {
                        "key": q.key, "default": q.default, "visibility": q.visibility,
                        "label": q.render_label(), "unit": q.unit,
                    }
                    for q in kind.quantities.values()
                ],
                "affordances": [
                    {
                        "verb": a.verb,
                        # 她读到的那几个字。宿主的按钮上该印这个,不是 `harvest` ——
                        # 动词放开之后引擎手上也没有别的地方查得到它。
                        "label": a.label or a.verb,
                        "duration": a.duration,
                        "occupies": a.occupies if a.duration > 0 else False,
                        "changes_world": a.changes_world,
                        "needs_actor": a.needs_actor,
                        "conditions": [str(c) for c in a.conditions],
                        "sets": [f"{k} = {v}" for k, v in a.outputs.items()],
                        "requires": [str(r) for r in a.requires],
                        "costs": [f"{k} = {v}" for k, v in a.costs.items()],
                        "consumes": dict(a.consumes),
                        # 生与灭。`spawn` 是 `{"kind","name","gloss","location",
                        # "quantities"}`(`location` 空 = 生在这件事发生的地方)。
                        "spawn": (
                            {
                                "kind": a.spawn.kind, "name": a.spawn.name,
                                "gloss": a.spawn.gloss, "location": a.spawn.location,
                                "quantities": dict(a.spawn.quantities),
                            }
                            if a.spawn is not None else None
                        ),
                        "destroys_target": a.destroys_target,
                        # 得有人一起做的事。`None` = 单人的老样子。宿主界面上这
                        # 一格决定按钮该不该弹一个"叫谁一起"的选人框 —— 不给出来
                        # 的话,它只会在点下去之后收到一句"这件事得有人一起做"。
                        "participants": (
                            {"min": a.participants.minimum, "max": a.participants.maximum}
                            if a.participants is not None else None
                        ),
                    }
                    for a in kind.affordances.values()
                ],
            }
            for kind in sorted(ontology.kinds.values(), key=lambda k: k.id)
        ]

    def entities(self, kind: str | None = None) -> list[dict[str, Any]]:
        """世界里的实例;给了 `kind` 就只看那一类。

        带上此刻的量(`values`)是有意的:这两样分开问的话,宿主得先 `entities()`
        再逐个 `stocks()`,而两次之间世界还在跑。`gloss` 是**这一个**的补充描述,
        空的时候回落到种类的那一行 —— 提示词里也是这么落的。

        每行 `{"id","kind","name","gloss","location","values"}`。
        """
        ontology = self.scheduler.ontology
        if ontology is None:
            return []
        store = self.scheduler.stock_store
        rows = (
            ontology.entities_of(kind) if kind is not None
            else sorted(ontology.entities.values(), key=lambda e: e.id)
        )
        return [
            {
                "id": e.id,
                "kind": e.kind,
                "name": e.name,
                "gloss": e.gloss or (ontology.kinds[e.kind].gloss
                                     if e.kind in ontology.kinds else ""),
                "location": e.location,
                "values": {} if store is None else store.of(e.id),
            }
            for e in rows
        ]

    def check_entity(self, entity_id: str | None = None) -> list[dict[str, Any]]:
        """**这些东西活得了吗** —— 逐个跑一遍出生自检,报每个身上讲不通的地方。

        不给 `entity_id` 就查全部。每行 `{"entity","kind","ok","problems":[…]}`,
        `ok` 为真时 `problems` 是空的。

        引擎在**每一次出生时自己跑这一套**(不通过就整个撤回并发 `entity_stillborn`),
        这个方法是同一套检查的手动入口。两个用处:

        - **写声明的时候**问一句"我这么写,生出来的东西活得了吗" —— 不必先让世界
          真的生一个出来看看;
        - **查一个已经在世界里的东西**为什么不动弹。它查的正是那类不报错的病:
          量没落地(读到 0,于是条件和规律都安静地不生效)、能力的表达式算不出来、
          声明成看得见却不在任何地方(存在,而没有任何人碰得到)。

        查的是**能不能算出一个叫得出名字的结论**,不是"能不能成功":`conditions`
        (果子还没熟)和 `incapable`(她做不了)都算过关 —— 那是世界在正常说话。
        """
        from anima_world.ontology import check_entity as _check

        ontology = self.scheduler.ontology
        if ontology is None:
            return []
        store = self.scheduler.stock_store
        visibility = self.scheduler.visibility_store
        ids = [entity_id] if entity_id is not None else sorted(ontology.entities)
        out: list[dict[str, Any]] = []
        for eid in ids:
            entity = ontology.entities.get(eid)
            problems = _check(
                ontology, eid,
                values={} if store is None else store.of(eid),
                world_values=None if store is None else store.of("world"),
                place=(
                    None if visibility is None
                    else (visibility.place_of(eid) or "")
                ),
            )
            out.append({
                "entity": eid,
                "kind": entity.kind if entity is not None else "",
                "ok": not problems,
                "problems": problems,
            })
        return out

    def engagements(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        """**谁正在做一件要花时间的事**;给了 `agent_id` 就只看她。

        为什么这个必须有出口:一个长过程在别处一点痕迹都没有 —— `state()` 里她
        看上去闲着,`stocks()` 上那个东西一动不动,而她其实已经埋头干了十个月。
        少了这一个方法,"她在忙"和"她没事干"在宿主眼里长得一模一样,而这一层
        存在的全部意义就是让这两者不一样。

        每行 `{"agent","target","verb","label","started_tick","ends_tick",
        "remaining","occupies"}`。`remaining` 是**此刻**还剩几个 tick,拿它显示
        进度条;`occupies` 为真表示这期间她腾不出手,别的能力一律 `busy`。
        """
        now = int(self.scheduler.clock)
        rows = [
            dict(record) for _, record in self.scheduler._engaged.items()
            if agent_id is None or record.get("agent") == agent_id
        ]
        return sorted(
            (
                {
                    "agent": str(r.get("agent") or ""),
                    "target": str(r.get("target") or ""),
                    "verb": str(r.get("verb") or ""),
                    "label": str(r.get("label") or r.get("verb") or ""),
                    "started_tick": int(r.get("started", 0)),
                    "ends_tick": int(r.get("ends", 0)),
                    "remaining": max(0, int(r.get("ends", 0)) - now),
                    "occupies": bool(r.get("occupies")),
                }
                for r in rows
            ),
            key=lambda r: (r["ends_tick"], r["agent"], r["target"], r["verb"]),
        )

    # ---- 看一眼她收到了什么 ----------------------------------------------

    def debug_prompt(
        self,
        agent_id: str,
        *,
        player_id: str = "p1",
        message: str = "在吗",
        display_name: str | None = None,
        role: str = "",
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
        interlocutor = self._interlocutor_for(player_id, display_name, role)
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
            self._publish_autonomy_stats()

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
            # 和聊天那条路共用 `describe_here` —— 自主决定这一路要是另写一遍拼装,
            # 她做决定时看到的世界就和她说话时看到的不是同一个,而两边都能跑、
            # 都不报错。观察窗不许撒谎,这一条同样适用于她自己的决定上下文。
            for owner in sorted(perceived.here):
                notes.append(f"这里的{perceived.describe_here(owner)}")
            if perceived.overflow:
                notes.append(f"这里还有 {perceived.overflow} 样别的东西,你没细看")
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
        try:
            await self._autonomy_round_body(contexts, day)
        finally:
            # 崩了也要发布:一轮半路炸掉留下的 `asked` 与 `last` 正是排查要看的
            # 那两行 —— 只在成功时发布,等于这条链坏得最厉害的时候最看不见。
            self._publish_autonomy_stats()

    async def _autonomy_round_body(self, contexts: list[Any], day: int) -> None:
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
                said = f"{ctx.agent_id}:{decision['tool']} 没成 —— {result.error}"
                self._autonomy_stats["last"] = said
                # **失败单独留一格。** `last` 每轮都被改写,而"什么都不做"是这一层
                # 的常态 —— 一次失败后面跟上两轮沉默,那句理由就没了,只剩计数器上
                # 一个 `failed: 1`:你知道有一次没成,永远不知道是什么没成。而这一层
                # 的全部意义就是把失败的方式分开。真世界上撞见的:一个跑了 18 天的
                # 世界报 `failed: 1`,而库里、日志里都找不到那一次是什么。
                self._autonomy_stats["last_failure"] = said

    # ── contact:她想起一个不在跟前的玩家 ──────────────────────────────────

    def _contact_enabled(self) -> bool:
        """**不搭 `chat.tools.enabled`。**

        autonomy 要那个开关是因为它给的是一份能力菜单,菜单空着的轮次是一次白花
        的 LLM 调用。这一层不挑动词:它只发一条事件,而且没有 LLM 也成立。
        """
        return bool(self.config_get("contact.enabled", False)) and not self._closed

    def _install_contact(self) -> None:
        interval = int(self.config_get("contact.interval_ticks", contact.DEFAULT_INTERVAL_TICKS) or 0)
        self.scheduler._contact_interval = max(1, interval)
        self.scheduler._contact_hook = self._on_contact_due

    def _note_player_contact(self, agent_id: str, player_id: str, name: str = "") -> None:
        """他刚跟她说了话 —— 记一笔世界时钟。「很久没出现」的那个"上次"就是它。

        挂在 `_chat_prelude`(`chat` / `chat_burst` 共用的那一道)与
        `record_chat_turn` 上,**两条门都要挂**:只挂一条的话,一个只用
        `record_chat_turn` 的宿主(那正是网站后端的用法)会让她永远觉得你
        从没来过,于是"久别"这条由头对他一个字都不成立 —— 而且一声不吭。
        """
        store = getattr(self.scheduler, "contact_store", None)
        if store is None:
            return
        try:
            store.note_contact(agent_id, player_id, self.scheduler.clock, name)
        except Exception:  # noqa: BLE001 - 记一笔失败不该挡住一轮对话
            logger.warning("记 contact 水位失败 agent=%s player=%s", agent_id, player_id, exc_info=True)

    def _contact_blockers(self, agent_id: str, player_id: str) -> list[str]:
        """她这会儿为什么不该想起谁 —— 五条硬闸,**一条都不是打折**。

        `face_to_face` 在这里不是"多此一举":他就在她跟前时该发生的是她直接开口
        (`reach_out` / `agent_hail`),而不是一条"她想找你"的通知。两条路各管一半,
        重叠的那一块必须由一边让出来,否则玩家会在跟她面对面聊天的同时收到
        "她想联系你"的推送。
        """
        blockers: list[str] = []
        activity = self._view._agent_activity(agent_id)
        kind = activity.get("kind")
        if kind == "sleep":
            blockers.append("sleep")
        if kind == "chat":
            blockers.append("chat")
        if activity.get("transit") or agent_id in self.scheduler._transit:
            blockers.append("transit")
        # **只有占着她的那种长过程算数。** 做椅子占用她,怀胎不占用 —— 两者都
        # 花十个月,而"这期间她还能不能干别的"正是代价的真实形状(见本体层
        # `occupies`)。照"有没有长过程"判的话,一个怀着孕的人十个月想不起你。
        if any(row.get("occupies") for row in (activity.get("engaged") or [])):
            blockers.append("engaged")
        try:
            if self._tool_runtime.face_to_face(agent_id, player_id):
                blockers.append("face_to_face")
        except Exception:  # noqa: BLE001 - 读不到位置就当不在跟前
            logger.debug("face_to_face 读不到,按不在跟前处理", exc_info=True)
        if self.chat_state is not None and self.chat_state.quiet_until(agent_id, player_id) is not None:
            # 她自己刚把他静音 —— 这一层再去"想起"他,等于引擎把她的选择撤销掉。
            blockers.append("muted")
        return blockers

    def _conversation_player(self, event_seq: Any) -> str:
        """那条 `user_conversation` 记忆到底是**跟谁**的对话。

        ⚠️ **不许拿名字去记忆摘要里找。** 真模型实测(gemma4:26b)两条记忆:

            白霜:「面对阿檀对离别的感伤,白霜表现出怀疑与试探……」   ← 提到了
            零  :「面对即将到来的离别,对话充满了依依不舍的感伤与温情。」← 没提

        同一场对话、同一个玩家,摘要提不提名字全看那一次模型怎么写。照名字匹配的话,
        「零」拿不到这条由头而「白霜」拿得到 —— 而这个差别和两个人的性格毫无关系,
        纯粹是措辞的偶然。**照跑,不报错,而且看上去像是性格起了作用**,这是这个仓库
        最怕的那种坏法。

        `conversation` 事件的 `participants` 里写着他是谁,那才是事实。事件按 seq
        连续存放,而这里查的记忆一定在保鲜期内(离表尾很近),所以这一次读很便宜。
        """
        try:
            seq = int(event_seq)
        except (TypeError, ValueError):
            return ""
        if seq <= 0:
            return ""
        page = self.history(since_seq=seq - 1, limit=1, kind="conversation")
        for event in page["events"]:
            if int(event.get("seq") or 0) != seq:
                continue
            for person in (event.get("payload") or {}).get("participants") or []:
                if (person or {}).get("kind") == "user":
                    return str(person.get("id") or "")
        return ""

    def _contact_reasons(
        self, agent_id: str, player_id: str, player_name: str, *, now_tick: int,
        last_contact_tick: int | None,
    ) -> list[contact.Reason]:
        """由头,一条都不许是凭空的 —— 每条都带着它出处的引用。

        四条里三条从**她的记忆**里长出来(那是这个引擎里"她知道一件事"的表示),
        第四条从"上次他跟她说话是哪一 tick"长出来。没有第五条:她不会因为闲着
        就想起一个人。
        """
        reasons: list[contact.Reason] = []
        recent = max(1, int(self.config_get("contact.recent_ticks", contact.DEFAULT_RECENT_TICKS) or 1))
        absence_ticks = max(1, int(self.config_get("contact.absence_ticks", contact.DEFAULT_ABSENCE_TICKS) or 1))

        # 久别。`None` = 他从没跟她说过话 —— 那不是"很久没出现",那是没出现过,
        # 而**引擎不替一个没发生过的过去编一个时长**。
        #
        # ⚠️ **哨兵是 `None`,不是 0。** 拿 0 当"从没有过"和世界的创世 tick 撞车:
        # 一个开机就跟她说了话的玩家(CLI 试聊、真世界的第一个访客,都是这个形状)
        # 记下的正是 `last_contact_tick = 0`,于是"久别"这条由头对他**永远**不成立。
        # 真模型实测时就是这么撞上的:两个人跑满两个世界日,一条都没触发。
        if last_contact_tick is not None:
            idle = max(0, now_tick - int(last_contact_tick))
            weight = contact.absence_weight(idle, absence_ticks)
            if weight > 0:
                reasons.append(contact.Reason(
                    kind="absence", weight=weight,
                    note=f"上次说话是在 tick {last_contact_tick},到现在过去了 {idle} tick",
                    ref={"last_contact_tick": int(last_contact_tick), "idle_ticks": idle},
                ))

        store = self.scheduler.memory_store
        if store is None:
            return reasons
        try:
            rows = store.query(agent_id=agent_id)
        except Exception:  # noqa: BLE001 - 读不到记忆就只剩久别那一条
            logger.warning("读记忆失败 agent=%s", agent_id, exc_info=True)
            return reasons

        # 名字**和 id 一起当针**:宿主没告诉世界他叫什么的时候,记忆里写的就是 id。
        needles = {n for n in (player_name, player_id) if n}
        best: dict[str, contact.Reason] = {}
        conversation_checked = False
        for row in rows:
            tick = int(row.get("tick") or 0)
            if now_tick - tick > recent:
                continue
            kind = str(row.get("kind") or "")
            summary = str(row.get("summary") or "")
            if kind == "user_conversation":
                # 跟玩家的对话**按事实认人,不按摘要措辞**(见 `_conversation_player`)。
                # 一次查一条(最新的那条已经够 —— 同一类只留最重的一条),免得一个
                # 攒了三十条对话的世界每轮都去翻三十次事件。
                if conversation_checked:
                    continue
                conversation_checked = True
                if self._conversation_player(row.get("event_seq")) != player_id:
                    continue
            elif not any(needle in summary for needle in needles):
                continue
            importance = float(row.get("importance") or 0.0)
            ref = {"memory_id": row.get("id"), "memory_kind": kind, "tick": tick,
                   "event_seq": row.get("event_seq")}
            if kind == "directive":
                candidate = contact.Reason(
                    kind="errand",
                    weight=contact.REASON_WEIGHTS["errand"] * max(0.3, min(1.0, importance / 0.7)),
                    note=summary, ref=ref,
                )
            elif kind.startswith("hearsay"):
                candidate = contact.Reason(
                    kind="gossip",
                    weight=contact.REASON_WEIGHTS["gossip"] * max(0.3, min(1.0, importance / 0.5)),
                    note=summary, ref=ref,
                )
            elif importance >= 0.6:
                candidate = contact.Reason(
                    kind="strong_memory",
                    weight=contact.REASON_WEIGHTS["strong_memory"] * min(1.0, importance),
                    note=summary, ref=ref,
                )
            else:
                continue
            # 每类只留最重的一条。不这么做的话,二十条八卦会把 `1-Π(1-w)` 顶到
            # 1.0 —— 于是"由头"退化成"记忆条数",而那和拍脑袋只差一个名字。
            if candidate.weight > best.get(candidate.kind, contact.Reason(candidate.kind, -1.0)).weight:
                best[candidate.kind] = candidate
        reasons.extend(best.values())
        return reasons

    def _contact_targets(self, agent_id: str) -> list[str]:
        """她跟哪些**玩家**有过来往。

        ⚠️ **判据不是"关系投影里不是角色的那些 id"。** 那条差点放行了一个很坏的
        错:一个用 `agents=1` 打开的世界(或任何角色在这个进程里没注册全的世界)
        里,`遥` 和 `柔` 就成了"玩家" —— 她会对着两个同事算亲密度、写一句想说的话,
        然后发一条谁也收不到的 `agent_wants_contact`。世界照跑,日志干净。

        判据是**这个 id 走过玩家那扇门**:`contact` 表里有他的行(他跟她说过话,
        `_note_player_contact` 写的),或者他此刻正登记在场。前者落库,所以重启
        之后这一层照旧成立 —— 而 `World.players` 是刻意的内存态,只靠它的话
        一个刚重启的世界里她谁都想不起来,失效的样子和"她这会儿没想起谁"一模一样。
        """
        store = getattr(self.scheduler, "contact_store", None)
        known: set[str] = set(self.players)
        if store is not None:
            known.update(
                str(row.get("player_id") or "")
                for row in store.all()
                if row.get("agent_id") == agent_id
            )
        agents = self.scheduler.agents
        return sorted(pid for pid in known if pid and pid not in agents)

    def _contact_evaluate(self, agent_id: str, now: Any) -> list[dict[str, Any]]:
        """她这会儿对每个玩家算出来是多少 —— **只读,没有副作用**。

        真轮次(`_contact_candidates`)和调试视图(`contact_forecast`)共用这一份。
        另写一遍拼装就会撒谎:调阈值的人看到的分数和世界真用的那个分数是两条
        代码路径,而两边都能跑、都不报错(`debug_prompt` 那一课)。
        """
        brain = self.scheduler.agents.get(agent_id)
        store = getattr(self.scheduler, "contact_store", None)
        if brain is None or store is None:
            return []
        agent = brain.agent
        now_tick = int(self.scheduler.clock)
        day = int(getattr(now, "day", 0))
        cooldown = max(0, int(self.config_get("contact.cooldown_ticks", contact.DEFAULT_COOLDOWN_TICKS) or 0))
        cap = max(0, int(self.config_get("contact.max_per_day", contact.DEFAULT_MAX_PER_DAY) or 0))
        mood = agent.blackboard.read("need.mood")
        initiative = 1.0
        if self.scheduler.stock_store is not None:
            key = str(self.config_get("contact.initiative_stock", contact.DEFAULT_INITIATIVE_STOCK) or "")
            if key:
                # **没声明 = 1.0**:`get` 的 default 就是那个语义,和本体层
                # "声明本身就是开关"逐字同构 —— 不写这个量的世界行为逐位不变。
                initiative = float(self.scheduler.stock_store.get(f"agent:{agent_id}", key, 1.0))
        here = str(agent.blackboard.read("loc") or agent.location or "")

        out: list[dict[str, Any]] = []
        for player_id in self._contact_targets(agent_id):
            row = store.get(agent_id, player_id)
            fired_today = store.fired_today(agent_id, player_id, day)
            # `None` = 从来没触发过。**不能拿 0 当哨兵** —— 创世那一 tick 触发过
            # 的话,`0` 会被读成"从来没有",冷却整个失效(和 `last_contact_tick`
            # 同一个坑,那个是真模型实测撞出来的)。
            last_fired = row.get("last_fired_tick")
            # 额度与冷却**先算出来当成一个字段**,而不是当场 `continue`:
            # 调试视图要说得出"她本来会想起你,是冷却挡住的" —— 提前退出的话
            # 那种情形和"她没想起你"在产物上一模一样。
            quota = ""
            if cap and fired_today >= cap:
                quota = "capped"
            elif last_fired is not None and now_tick - int(last_fired) < cooldown:
                quota = "cooling"
            player_name = str(
                (self.players.get(player_id) or {}).get("display_name")
                or row.get("player_name") or player_id
            )
            last_contact = row.get("last_contact_tick")
            reasons = self._contact_reasons(
                agent_id, player_id, player_name,
                now_tick=now_tick, last_contact_tick=last_contact,
            )
            stance_row = None
            if self.chat_state is not None and self.config_get("chat.stance.enabled", False):
                stance_row = self.chat_state.stance(agent_id, player_id)
            decision = contact.decide(
                relation=self.scheduler._memory_projection.relations.get((agent_id, player_id)),
                reasons=reasons,
                blockers=self._contact_blockers(agent_id, player_id),
                mood=None if mood is None else float(mood),
                initiative=initiative,
                stance=(stance_row or {}).get("stance"),
                min_closeness=float(self.config_get("contact.min_closeness", contact.DEFAULT_MIN_CLOSENESS)),
                base_threshold=float(self.config_get("contact.threshold", contact.DEFAULT_THRESHOLD)),
                fired_today=fired_today,
                fatigue=float(self.config_get("contact.fatigue", contact.DEFAULT_FATIGUE)),
            )
            out.append({
                "agent_id": agent_id,
                "agent_name": agent.name or agent_id,
                "personality": str(agent.blackboard.read("personality") or ""),
                "player_id": player_id,
                "player_name": player_name,
                "location": here,
                "location_name": self._location_display_name(here),
                "day": day,
                "hour": int(getattr(now, "hour", 0)),
                "minute": int(getattr(now, "minute", 0)),
                "tick": now_tick,
                "mood": None if mood is None else float(mood),
                "fired_today": fired_today,
                "quota": quota,
                "decision": decision,
            })
        return out

    def _contact_candidates(self, agent_id: str, now: Any) -> list[dict[str, Any]]:
        """锁内一次快照 + 判定(只读、无 LLM、无 IO),外加占掉额度。

        **判定在这里做完**,worker 只负责写那句线索并落事件 —— 判定要能复现,而
        一个跑在 worker 上、读着已经变了的世界的判定,查起来永远差一口气。
        """
        store = getattr(self.scheduler, "contact_store", None)
        if store is None:
            return []
        out: list[dict[str, Any]] = []
        for item in self._contact_evaluate(agent_id, now):
            self._contact_stats["checked"] += 1
            if item["quota"]:
                continue   # 额度用完 / 还在冷却。不是"被挡下",所以不记进 blocked。
            decision: contact.Decision = item["decision"]
            if not decision.fire:
                if decision.blocked_by:
                    self._contact_stats["blocked"] += 1
                self._contact_stats["last"] = (
                    f"{agent_id}→{item['player_id']}:{decision.explain()}"
                )
                continue
            # **额度当场记掉**(还在锁里)。判定和落事件之间隔着一次可能的 LLM
            # 往返,而下一个 tick 不会等它 —— 不在这儿占位的话,一次慢调用能让
            # 同一条由头连发好几遍。
            store.note_fired(agent_id, item["player_id"], tick=item["tick"], day=item["day"])
            out.append(item)
        return out

    def contact_forecast(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        """**此刻**每个角色对每个玩家算出来是多少 —— 调阈值的那扇窗(contact)。

        `contact_requests()` 给的是已经发生的;这一个给的是"为什么没发生"。作者
        调 `contact.threshold` / `min_closeness` 时要看的正是后者 —— 只看已发生
        的那一份,一个永远不触发的配置和一个刚好不触发的配置长得一模一样。

        **和真轮次共用同一个判定函数**(`_contact_evaluate`),所以它不会撒谎。
        只读:不占额度、不写冷却、不发事件。
        """
        now = self.scheduler.world_time()
        ids = [agent_id] if agent_id else list(self.scheduler.agents)
        rows: list[dict[str, Any]] = []
        with self.scheduler._lock:
            for aid in ids:
                if aid not in self.scheduler.agents:
                    raise KeyError(f"agent {aid} not found")
                for item in self._contact_evaluate(aid, now):
                    decision: contact.Decision = item["decision"]
                    rows.append({
                        "agent_id": item["agent_id"],
                        "agent_name": item["agent_name"],
                        "player_id": item["player_id"],
                        "player_name": item["player_name"],
                        "would_fire": bool(decision.fire) and not item["quota"],
                        "quota": item["quota"],
                        "fired_today": item["fired_today"],
                        "components": decision.components(),
                        "blocked_by": decision.blocked_by,
                        "explain": (
                            {"capped": "今天的额度用完了", "cooling": "还在冷却期内"}[item["quota"]]
                            if item["quota"] else decision.explain()
                        ),
                        "reasons": [r.to_dict() for r in decision.reasons],
                    })
        return rows

    def _on_contact_due(self, agent_ids: list[str], now: Any) -> None:
        """时钟喊到点了。**快照 + 判定,然后立刻返回** —— 时钟永远不等网络。"""
        if not self._contact_enabled():
            return
        candidates: list[dict[str, Any]] = []
        for agent_id in agent_ids:
            try:
                candidates.extend(self._contact_candidates(agent_id, now))
            except Exception:  # noqa: BLE001 - 一个角色算错不该拖垮别人
                logger.warning("contact 判定失败 agent=%s", agent_id, exc_info=True)
        if not candidates:
            return
        future = asyncio.run_coroutine_threadsafe(
            self._contact_round(candidates), self._bridge._loop
        )
        future.add_done_callback(self._on_contact_round_done)

    def _on_contact_round_done(self, future: Any) -> None:
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001 - 记录下来,不许无声
            logger.warning("contact round crashed", exc_info=True)
            self._contact_stats["last"] = f"想起谁那一轮崩溃({type(exc).__name__}:{exc})"

    async def _contact_round(self, candidates: list[dict[str, Any]]) -> None:
        """给每条已经定下来的念头写一句线索,然后落成 `agent_wants_contact`。

        **判定不在这儿。** 这一轮只做两件事:写那句线索(可省)、发事件。所以
        LLM 挂了、没配 key、模板渲染失败,结果都只是线索退回由头原文 —— 事件照发。
        一个"模型没回话所以她就不想你了"的机制,和没有这个机制是一回事。
        """
        compose = bool(self.config_get("contact.compose.enabled", True))
        template = contact.DEFAULT_COMPOSE_PROMPT
        if self.scheduler.prompt_store is not None:
            template = self.scheduler.prompt_store.get(
                "contact.compose", default=contact.DEFAULT_COMPOSE_PROMPT
            )
        for item in candidates:
            decision: contact.Decision = item["decision"]
            topic, source = contact.fallback_topic(decision.reasons), "reason"
            if compose:
                notes = []
                if item.get("mood") is not None:
                    notes.append(f"你此刻的心气儿:{item['mood']:.2f}(0~1)")
                try:
                    messages = contact.build_compose_messages(
                        template,
                        name=item["agent_name"], personality=item["personality"],
                        day=item["day"], hour=item["hour"], minute=item["minute"],
                        location=item["location_name"], player=item["player_name"],
                        reasons=decision.reasons, notes=notes,
                    )
                    written = contact.parse_topic(
                        await self.chat_service._background_llm.complete(messages)
                    )
                    if written:
                        topic, source = written, "llm"
                        self._contact_stats["composed"] += 1
                    else:
                        self._contact_stats["compose_failed"] += 1
                except Exception as exc:  # noqa: BLE001 - 线索写不出来不该吞掉念头
                    self._contact_stats["compose_failed"] += 1
                    logger.info("contact 线索没写成(%s),退回由头原文", type(exc).__name__)
            head = decision.top_reason
            self._tool_runtime.emit({
                "type": "agent_wants_contact",
                "who": item["agent_id"],
                "loc": item["location"] or None,
                "payload": {
                    "agent_id": item["agent_id"],
                    "agent_name": item["agent_name"],
                    "player_id": item["player_id"],
                    "player_name": item["player_name"],
                    # 主由头 + 全部由头。单数那个是给"显示成一行"用的,复数那个
                    # 才是账 —— 只给单数的话,一条四个由头叠出来的念头会被读成
                    # 只有一个理由。
                    "reason": (head.kind if head else ""),
                    "reasons": [r.to_dict() for r in decision.reasons],
                    "topic": topic,
                    "topic_source": source,
                    "components": decision.components(),
                    "explain": decision.explain(),
                    "location": item["location"],
                    "day": item["day"],
                    "at": f"{item['hour']:02d}:{item['minute']:02d}",
                },
            })
            self._contact_stats["fired"] += 1
            self._contact_stats["last"] = (
                f"{item['agent_id']}→{item['player_id']}:{decision.explain()}"
            )
            logger.info(
                "%s 想起了 %s(%s,%.2f):%s",
                item["agent_id"], item["player_id"],
                head.kind if head else "—", decision.score, topic,
            )

    def contact_requests(
        self, player_id: str | None = None, *, since_seq: int = 0, limit: int = 50
    ) -> list[dict[str, Any]]:
        """**她想起过你** —— 上层拿走这一层产物的那扇门(contact)。

        和 `inbox()` 分开是有理由的:那一条(`agent_hail`)的语义是"她已经在你
        面前开口了",所以它只在你在场时成立;这一条正相反,**只在你不在跟前时
        成立**。合成一个的话,宿主没法把"她来打招呼"和"她想联系你"显示成两件
        事 —— 而对玩家来说那是完全不同的两件事。

        不给 `player_id` 就是全部(运维/调试用)。返回按 seq 升序;拿最后一条的
        `seq` 当下次的 `since_seq` 就是增量拉取,和 `inbox()` 同一个用法。

        ⚠️ **引擎不负责送达。** 推送、红点、消息列表归宿主那一层 —— 这里给的是
        一条有据可查的世界事件,`payload.reasons` 里每条由头都带着它的出处。
        """
        page = self.history(since_seq=since_seq, limit=limit, kind="agent_wants_contact")
        events = page["events"]
        if player_id is None:
            return events
        return [
            event for event in events
            if (event.get("payload") or {}).get("player_id") == player_id
        ]

    def contact_stats(self) -> dict[str, Any]:
        """"她想起你"这条链跑没跑、发没发(contact)。

        和 `autonomy_stats()` / `rule_stats()` 同一个理由:这条路最容易的坏法是
        **看着都对、其实一次没算**。`checked` 是 0 说明 hook 没挂上或者压根没有
        候选;`checked` 不为 0 而 `fired` 是 0,配上 `last` 那句话,就能分清是
        "还不够近"、"没有由头"、还是"她在睡觉"。

        ⚠️ **本次运行内的计数,不是历史**(冷却与次数才落库)。
        """
        return dict(self._contact_stats)

    _world_lock: Any = None
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

        # **异地就只能打电话。** 声明了 `requires_colocation` 的能力,玩家不在她
        # 跟前时办不到 —— 而在这之前玩家是个幽灵,位置这个维度等于白设计了。
        # 默认关(`presence.enforce_colocation`):引擎侧收紧会当场打断线上世界,
        # 迁移的次序只能是"先让宿主调 `player_move`,再开这个开关"。
        gate = self._colocation_error(verb, agent_id, player_id)
        if gate:
            logger.info("act(%s, %s) 被拒:%s", agent_id, verb, gate)
            return {"tool": verb, "params": dict(params or {}), "ok": False, "error": gate}

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

    def _colocation_error(self, verb: str, agent_id: str, player_id: str) -> str:
        """这个能力要不要玩家真的在她跟前 —— 办不到就返回那句回执,办得到是空串。

        `act()` 和 `intend()` 共用这一句。**回执要说得出是三种里的哪一种**:

        | 原因 | 玩家该干什么 |
        |---|---|
        | 你在别处 | 走过去(或者只跟她说话) |
        | 世界不知道你在哪 | **宿主的事** —— 他没调过 `player_move` |
        | 她在赶路 | 等她落脚 |

        合成一句"你不在她跟前"的话,第二种会看起来像是玩家自己站错了地方,
        而他做什么都改不了 —— 那是这个仓库最怕的那种"技术上没错、读起来是谎"。
        """
        if not self.config_get("presence.enforce_colocation", False):
            return ""
        try:
            spec = tools_mod.get(verb)
        except tools_mod.ToolCallError:
            return ""
        if not spec.requires_colocation:
            return ""
        if not player_id:
            return f"{verb!r} 要当面才办得到,而这次调用没说是替哪个玩家"
        if self._tool_runtime.face_to_face(agent_id, player_id):
            return ""
        here = self._tool_runtime.agent_location(agent_id)
        where = self._tool_runtime.player_location(player_id)
        name = self._tool_runtime.agent_names().get(agent_id, agent_id)
        if not where:
            return (
                f"{verb!r} 要当面才办得到,而世界不知道你这会儿在哪 —— "
                f"宿主没调过 player_move。{name}在 {here or '别处'}"
            )
        if not here or here == where:
            # 两处地名一样却不是面对面 —— 只可能是她在赶路(`face_to_face` 与
            # `_where_is` 同一条:在途即不在任何地方)。照 `agent_location` 那份
            # 直说会写出"你在 cafe,她在 cafe —— 这件事得当面",一句读起来是谎的话。
            return f"{verb!r} 要当面才办得到,而{name}这会儿在路上,不在任何地方"
        return (
            f"{verb!r} 要当面才办得到 —— 你在 {where},{name}在 {here}。"
            f"隔着这么远,你只能跟她说话"
        )

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

        声明了 `requires_colocation` 的能力**排不进打算**(开着那道闸时)——
        它要"有个玩家在她跟前",而一份打算是给未来几个 tick 的,那时候谁在她跟前
        没有人知道。放进去的话,它会在某个说不清的时刻静默地做不成。
        """
        if agent_id not in self.scheduler.agents:
            raise KeyError(f"agent {agent_id} not found")
        allowed = {spec.id: spec for spec in tools_mod.tools_for(agent_id, tools_mod.BODY)}
        enforced = bool(self.config_get("presence.enforce_colocation", False))
        queue: list[dict[str, Any]] = []
        for index, step in enumerate(steps or []):
            verb = str((step or {}).get("verb") or "").strip()
            spec = allowed.get(verb)
            if spec is None:
                raise ValueError(
                    f"第 {index + 1} 步的 {verb!r} 不是过日子的动作;"
                    f"可用的是 {sorted(allowed)}"
                )
            if enforced and spec.requires_colocation:
                raise ValueError(
                    f"第 {index + 1} 步的 {verb!r} 要有个玩家在她跟前,"
                    f"排不进一份给未来几个 tick 的打算 —— 要做就当场 act()"
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
                # 它要不要玩家真的在她跟前。界面上这一格决定按钮什么时候可点 ——
                # 点下去才发现的话,那是一次没有任何人预告过的失败。
                "requires_colocation": spec.requires_colocation,
            }
            for spec in specs
        ]

    def presence(self, player_id: str | None = None) -> dict[str, Any]:
        """**谁在谁跟前** —— 玩家的位置从哪来、有没有人维护、此刻和谁面对面。

        这一条存在的理由是**迁移**,不是好看。`presence.enforce_colocation` 一开,
        声明了 `requires_colocation` 的能力会开始拒绝所有不在场的调用 —— 而
        `player_move` 是宿主的**可选**调用,没人调过的世界里"异地"是每一次调用的
        默认值。于是那道闸打开的当天,`give`、一起做事全线开始拒绝,而看上去像是
        玩家自己站错了地方。

        所以这一条要先答得出四个问题(`known` 那一格就是第四个):

        | 问题 | 答案在哪 |
        |---|---|
        | 玩家的位置从哪来 | **只有 `World.player_move()`** —— 引擎不猜,也没有第二个入口 |
        | 谁维护它 | **宿主**。CLI 的 `chat` 每轮调一次,网站后端要自己调 |
        | 默认值是什么 | **没有默认值**。没调过就是空串 = 不在场 = 异地 |
        | 现在有人维护吗 | 这一条的 `known` / `unplaced` |

        返回 `{"enforced", "location_source", "agents": {id: 地点}, "players":
        [{"player_id","name","location","known","present","seen_before",
        "face_to_face": [角色 id]}], "unplaced"}`。`unplaced` 是**没有位置的玩家数**:
        它大于 0 而 `enforced` 为真,就是那道闸正在拒绝一批谁也帮不了的调用。

        ⚠️ **玩家的位置是进程内的**(`World.players` 是刻意的内存态),而这一条要
        当体检用 —— 所以名单从**落库的那份**(`contact` 表,她记下过"他上次出现")
        补齐,位置从这个进程手上读。两者分开报,而且分得开:

        - `seen_before` 为真、`known` 为假 = **这个世界跟他打过交道,而这个进程
          手上没有他的位置**。CLI 里这几乎一定就是全部 —— 一个新开的进程当然不知道
          任何人在哪。
        - 于是它同时暴露了一件必须知道的事:`presence.enforce_colocation` 依赖的是
          **进程内**的位置。多进程宿主里 A 进程调了 `player_move`,B 进程照样认为
          他不在场。多进程 + 开这道闸的部署要先把位置搬进共享存储 —— 这条写在
          REFERENCE §2.9.9,不该由踩到的人自己发现。
        """
        runtime = self._tool_runtime
        agents = {aid: runtime.agent_location(aid) for aid in self.scheduler.agents}
        present = set(self.who_is_present())
        # 落库的那份名单:她记下过"他上次出现"的每一个人。**光看内存那份会撒谎** ——
        # 一个新开的进程手上一个玩家都没有,而 CLI 恰恰永远是新开的进程。
        seen: dict[str, str] = {}
        store = getattr(self.scheduler, "contact_store", None)
        if store is not None:
            try:
                for row in store.all():
                    pid = str(row.get("player_id") or "")
                    if pid:
                        seen.setdefault(pid, str(row.get("player_name") or ""))
            except Exception:  # noqa: BLE001 - 读不到落库名单不该让体检告吹
                logger.warning("读 contact 名单失败,只报这个进程知道的玩家", exc_info=True)
        rows: list[dict[str, Any]] = []
        for pid in sorted({*self.players, *seen}):
            if player_id is not None and pid != player_id:
                continue
            where = runtime.player_location(pid)
            rows.append({
                "player_id": pid,
                "name": (
                    runtime.player_name(pid) if pid in self.players
                    else (seen.get(pid) or pid)
                ),
                "location": where,
                # **"不知道他在哪"和"他在一个没有角色的地方"是两件事。**
                # 合成一个的话,一个宿主根本没接 `player_move` 的世界,看起来会像
                # 是玩家都碰巧站在没人的地方 —— 而那是改不回来的误诊。
                "known": bool(where),
                "present": pid in present,
                "seen_before": pid in seen,
                "face_to_face": sorted(
                    aid for aid, here in agents.items()
                    if here and where and here == where
                    and aid not in self.scheduler._transit
                ),
            })
        return {
            "enforced": bool(self.config_get("presence.enforce_colocation", False)),
            # 这一格是**警告,不是元数据**:那道闸依赖的东西活不过一次重启,
            # 也跨不过第二个进程。
            "location_source": "process-memory",
            "agents": agents,
            "players": rows,
            "unplaced": sum(1 for row in rows if not row["known"]),
        }

    def map_data(
        self,
        *,
        from_tick: int | None = None,
        to_tick: int | None = None,
        agents: list[str] | None = None,
    ) -> dict[str, Any]:
        """地图 + 此刻谁在哪 + 这段时间里谁去了哪儿。

        `anima-world map` 与任何宿主渲染器**共用这一份** —— 观察窗另写一遍拼装就会
        撒谎(这条在提示词那一层踩过,`debug_prompt` 与真聊天共用 `prompt_blocks`
        是同一个理由)。

        几何是**绝对**画布坐标(0~1),已经换算好:库里存的是相对父级的
        (`w=0.55` 是父级宽度的 55%),照原始值画出来的图每个东西都在错的地方,
        而且什么都不会报错。换算在 `LocationStore.absolute_xy` / `absolute_box`。

        - `places`:`id` / `name` / `kind` / `x` / `y`(+ region 的 `w` / `h`)
        - `standing`:`{place_id: [角色…]}` —— 此刻站在那儿的人
        - `travelling`:此刻在路上的人(`from` / `to` / `arrive_at`)。**路上的人
          不站在任何地方**,漏了这一层会让她在图上凭空消失半段路。
        - `tracks`:`[{agent, points: [{tick, place}]}]`,只认**到达**
          (`location_join`);起程不算 —— 她可能走到一半被打断。
        - `clock`:此刻第几 tick

        不给 tick 范围就是整段历史。给了 `agents` 就只算这几个人的轨迹。
        """
        from anima_world.mapview import tracks_from_events

        scheduler = self.scheduler
        store = scheduler.location_store
        places: list[dict[str, Any]] = []
        if store is not None:
            for row in store.all():
                loc_id = str(row["id"])
                origin = store.absolute_xy(loc_id)
                if origin is None:
                    continue          # 放不下的地点画不出来,但不该让整张图挂掉
                entry: dict[str, Any] = {
                    "id": loc_id,
                    "name": str(row.get("name") or loc_id),
                    "kind": str(row.get("kind") or "point"),
                    "x": origin[0],
                    "y": origin[1],
                }
                box = store.absolute_box(loc_id)
                if box is not None:
                    entry["w"], entry["h"] = box[2], box[3]
                places.append(entry)

        standing: dict[str, list[str]] = {}
        travelling: list[dict[str, Any]] = []
        for agent_id in sorted(scheduler.agents):
            if agents is not None and agent_id not in agents:
                continue
            trip = scheduler._transit.get(agent_id)
            if trip:
                travelling.append({
                    "agent": agent_id, "from": trip.get("from"), "to": trip.get("to"),
                    "arrive_at": int(trip.get("arrive_at") or 0),
                })
                continue
            board = scheduler.agents[agent_id].agent.blackboard
            here = board.read("loc")
            if here:
                standing.setdefault(str(here), []).append(agent_id)

        all_events = scheduler.event_log.replay() if scheduler.event_log is not None else []
        events = list(all_events)
        if from_tick is not None:
            events = [e for e in events if int(e.ts) >= from_tick]
        if to_tick is not None:
            events = [e for e in events if int(e.ts) <= to_tick]

        # **窗口之前她在哪,得带进来。** 只取窗口内的点,那么起点在窗口之前的人
        # 就只剩一个孤点 —— 画不出线,看上去像"她这天没动"。而 `--day N` 恰恰是
        # 最常用的看法:实测第 2 天,三个人里两个的起点在第 1 天。
        # 锚点标 `before`,图例说"自 X"而不是假装那也是这天的一次位移。
        anchors: dict[str, tuple[int, str]] = {}
        if from_tick is not None:
            for track in tracks_from_events(
                [e for e in all_events if int(e.ts) < from_tick], agents=agents
            ):
                if track.points:
                    anchors[track.agent] = track.points[-1]

        tracks = []
        for track in tracks_from_events(events, agents=agents):
            points = [
                {"tick": tick, "place": place} for tick, place in track.points
            ]
            anchor = anchors.pop(track.agent, None)
            if anchor is not None:
                points.insert(0, {"tick": anchor[0], "place": anchor[1], "before": True})
            tracks.append({"agent": track.agent, "points": points})
        # 窗口内一步没动的人也要有条目(带着她窗口之前的位置)—— 否则"她这天
        # 待在家里"看起来像"没有这个人"
        for agent_id, anchor in sorted(anchors.items()):
            tracks.append({
                "agent": agent_id,
                "points": [{"tick": anchor[0], "place": anchor[1], "before": True}],
            })

        return {
            "clock": scheduler.clock,
            "places": places,
            "standing": standing,
            "travelling": travelling,
            "tracks": tracks,
        }

    def autonomy_stats(self) -> dict[str, Any]:
        """定时轮次到底跑没跑、做没做。

        存在的理由是这条路**最容易静默地不工作**:开关点亮了、时钟在走,而她一次也
        没主动过 —— 那可能是"她确实没什么想做的"(正常),也可能是 hook 没挂上、
        LLM 一直失败、或者额度早就用完了。这四个数把它们分开。

        **读的是 `:meta` 上发布的那一份,不是这个进程的内存。** 驱动世界的是
        `anima-world run` 那个进程,而问这句话的人多半在另一个进程里(CLI、
        运维台、宿主的健康检查)。读内存的话他永远拿到全 0 —— 一个"这条链从没
        跑过"的答案,而那正是这四个数要用来排除的那种情况。**诊断本身给出假阴性,
        比没有诊断更坏。**
        """
        published = self._published_autonomy_stats()
        return published if published is not None else dict(self._autonomy_stats)

    def _published_autonomy_stats(self) -> dict[str, Any] | None:
        store = getattr(self.scheduler, "meta_store", None)
        if store is None:
            return None
        try:
            row = store.get("autonomy_stats")
        except Exception:  # noqa: BLE001 - 读不到诊断不该掀翻调用方
            logger.warning("读 autonomy_stats 失败", exc_info=True)
            return None
        return dict(row) if isinstance(row, dict) else None

    def _publish_autonomy_stats(self) -> None:
        """把这一轮的计数发布到 `:meta` —— 别的进程只看得见这一份。

        一轮结束发一次(而不是每次自增发一次):一轮是每 `interval_ticks` 一次,
        本来就稀疏,而"这一轮最后发生了什么"正是 `last` 要说的那句话。
        """
        store = getattr(self.scheduler, "meta_store", None)
        if store is None:
            return
        row = dict(self._autonomy_stats)
        # **上一轮发生在第几 tick** —— 四个计数只说"本次开机以来",而重启之后
        # 库里躺着的还是上一次开机那一行,读的人分不出"刚重启,新的一轮还没到"
        # 和"这条链死了"。而那正好又是这四个数要分开的那两件事,只是换了个地方
        # 犯。有了它,判据变成"离上一轮过去多久了",重启不影响这句话。
        row["last_tick"] = int(self.scheduler.clock)
        try:
            store.put("autonomy_stats", row)
        except Exception:  # noqa: BLE001 - 发布失败不该让这一轮跟着炸
            logger.warning("写 autonomy_stats 失败", exc_info=True)

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
        self._player_transit.pop(player_id, None)  # 宿主把他放到哪就是哪,行程作废

    def player_location(self, player_id: str) -> str:
        """玩家这会儿在哪。**在路上就还算在出发地**,到点了当场落地。

        惰性结算:没有哪个循环替玩家跑 tick(角色由 `_land_arrivals` 放下),所以
        "他到了没有"在每次读的时候算。到达的那一次补发 `location_join` ——
        和角色落地发的是同一种事件,宿主不用为人另写一套。

        没调过 `player_move` / `player_walk` 就是空串 = 不在场,引擎不猜
        (和 `face_to_face` 同一条规矩)。
        """
        trip = self._player_transit.get(player_id)
        if trip is not None and int(self.scheduler.clock) >= int(trip["arrive_at"]):
            self._player_transit.pop(player_id, None)
            self._touch_player(player_id, location=trip["to"])
            with self.scheduler._lock:
                self._record_and_fan({
                    "type": "state_change",
                    "who": f"player:{player_id}",
                    "loc": trip["to"],
                    "payload": {
                        "kind": "location_join",
                        "location": trip["to"],
                        "player_id": player_id,
                    },
                })
        return str((self.players.get(player_id) or {}).get("location") or "")

    def player_in_transit(self, player_id: str) -> bool:
        """他还在路上吗。先结算一次 —— 否则一个已经到了的人会被报成还在赶路。"""
        self.player_location(player_id)
        return player_id in self._player_transit

    def player_walk(self, player_id: str, location: str, *, role: str = "player") -> dict[str, Any]:
        """人走过去 —— **和她走同一段路花一样的时间**。

        `player_move` 留着不动:那是宿主"把他放在这儿"(进世界、换场景),瞬时;
        这一条是**他自己走**,要花时间、要发 `travel`、途中不能干活。两件事
        共用一份 `Scheduler._travel_minutes`,所以地图改了两边一起改。

        第一次进世界(还没有位置)不收路费:没有出发地的话"走过去"没有意义,
        直接落地 —— 否则新玩家的第一步永远卡在一段量不出来的路上。
        """
        location = location.strip()
        if not location:
            raise ValueError("location is required")
        store = self.scheduler.location_store
        if store is not None:
            row = store.get(location)
            if row is None or row.get("kind", "point") != "point":
                raise KeyError(f"没有 {location} 这个地方")
        origin = self.player_location(player_id)
        if not origin or origin == location:
            self._touch_player(player_id, role=role, location=location)
            self._player_transit.pop(player_id, None)
            return {"in_transit": False, "location": location}
        minutes = self.scheduler._travel_minutes(origin, location)
        if minutes is None or minutes <= 0:
            # 量不出来的两点(没有地图)照旧瞬移 —— 与 `_start_journey` 同一条退路
            self._touch_player(player_id, role=role, location=location)
            self._player_transit.pop(player_id, None)
            return {"in_transit": False, "location": location}
        mpt = max(1, int(self.scheduler._minutes_per_tick()))
        ticks = max(1, int(-(-minutes // mpt)))  # ceil,不引入 math 依赖
        arrive_at = int(self.scheduler.clock) + ticks
        self._player_transit[player_id] = {
            "from": origin, "to": location, "arrive_at": arrive_at,
        }
        self._touch_player(player_id, role=role)
        with self.scheduler._lock:
            self._record_and_fan({
                "type": "travel",
                "who": f"player:{player_id}",
                "loc": origin,
                "payload": {
                    "from": origin, "to": location,
                    "minutes": round(float(minutes), 1),
                    "arrive_at": arrive_at,
                    "player_id": player_id,
                },
            })
        return {
            "in_transit": True, "arrive_at": arrive_at,
            "minutes": round(float(minutes), 1), "from": origin, "to": location,
        }

    def player_tools(self) -> list[dict[str, Any]]:
        """人在网页上点得动的那些。**和她那份出自同一个注册表** —— 宿主照这个
        画按钮,不用自己维护一份会和引擎分叉的清单。"""
        return [
            {
                "id": spec.id,
                "kind": spec.kind,
                "description": spec.description,
                "params_schema": spec.params_schema,
                "requires_colocation": spec.requires_colocation,
            }
            for spec in tools_mod.tools_for("*", tools_mod.PLAYER)
        ]

    def player_tool(
        self, player_id: str, tool_id: str,
        params: dict[str, Any] | None = None, *, agent_id: str = "",
    ) -> dict[str, Any]:
        """人点了一下能力。

        **和她挑同一个能力走的是同一条路**:同一份 `ToolSpec`、同一套参数校验、
        同一个 handler、同一批副作用。区别只有 `actor` 那一个字段。这正是
        `player_action` 欠下的那笔账 —— 那条只落一行日志,点"走到哈尔滨"
        世界里什么也没发生。

        `agent_id` 是**这一轮对着谁**(`talk_to` 的目标、聊天里的那个她),
        不影响施动者是人这件事。不在 `PLAYER` 面上的能力一律拒绝,不静默降级。
        """
        spec = tools_mod.get(tool_id)  # 没有这个能力就抛,不假装成功
        if tools_mod.PLAYER not in spec.surfaces:
            raise tools_mod.ToolCallError(f"{tool_id} 不是玩家能用的能力")
        self._touch_player(player_id)
        ctx = tools_mod.ToolContext(
            agent_id=agent_id or "",
            player_id=player_id,
            runtime=self._tool_runtime,
            actor=tools_mod.PLAYER_ACTOR,
        )
        args = dict(params or {})
        return tools_mod.call(ctx, tool_id, args).to_dict(tool_id, args)

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
        store = self.scheduler.economy_store
        if store is None:
            return []
        with self.scheduler._lock:
            items = {str(r["id"]): r for r in store.items()}
            shelves = [r for r in store.shelves() if r["location_id"] == location_id]
        return [
            {
                "item_id": r["item_id"],
                "name": items.get(str(r["item_id"]), {}).get("name"),
                "kind": items.get(str(r["item_id"]), {}).get("kind"),
                "price": float(r["price"]),
                "quantity": int(r["quantity"]),
            }
            for r in shelves
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
        store = self.scheduler.economy_store
        if store is None:
            raise ValueError("economy needs a persistent world")
        with self.scheduler._lock:
            shelf = next(
                (r for r in store.shelves()
                 if r["location_id"] == location_id and r["item_id"] == item_id),
                None,
            )
            if shelf is None or int(shelf.get("quantity") or 0) <= 0:
                raise KeyError(f"{location_id} 没有 {item_id} 的货")
            price = float(shelf["price"])
            holder = f"player:{player_id}"
            # 门禁读账本,不读内存 —— 那两个数此前会分叉(见 player_topup)。
            wallet = float(self.scheduler._memory_projection.balances.get(holder, 0.0))
            if wallet < price:
                raise ValueError(f"钱包不够:{wallet} < {price}")
            if not store.take_stock(location_id, item_id):
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
            def _place_name(point_id: str) -> str:
                """地点 id → 人话名。**进提示词的一律走它。**

                她读到什么就说什么:漏掉一处的样子是「你在建筑工作室，正在去cafe
                的路上」—— 同一句话里一个地方用人话、另一个用 id,而她会照着把
                `cafe` 念出口。不报错,只是出戏。
                """
                if not point_id or scheduler.location_store is None:
                    return point_id
                row = scheduler.location_store.get(point_id)
                return str((row or {}).get("name") or point_id)

            loc_id = brain.agent.blackboard.read("loc") or brain.agent.location or ""
            loc_name = _place_name(loc_id)
            activity = self._view._agent_activity(agent_id)
            if activity.get("transit"):
                to_name = _place_name(str(activity["transit"].get("to") or ""))
                label = f"正在去{to_name or '别处'}的路上"
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
                            visibility=visibility, ontology=self.scheduler.ontology)
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
