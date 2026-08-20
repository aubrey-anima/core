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
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Sequence, TypeVar

from anima_world import autonomy
from anima_world import contact
from anima_world import perception as perception_mod
from anima_world import together
from anima_world import tools as tools_mod
from anima_world.actions import ActionDescriptor
from anima_world import chat_service as chat_service_mod
from anima_world.chat_service import ChatService
from anima_world.beats import coerce_goals
from anima_world.chat_session import ChatSessionManager
from anima_world.chat_state import ChatStateStore
from anima_world.chat_store import ChatStore
from anima_world.character_card import (
    billing_of,
    card_errors,
    card_warnings,
    normalize_card,
)
from anima_world.config_store import coerce_to_declared_type, mask_secret
from anima_world.intent import FIRST_PERSON, Director, places_menu, read_self_introduction
from anima_world.llm_client import (
    create_background_llm_client_from_config,
    create_llm_client_from_config,
    create_llm_client_from_env,
)
from anima_world.locations import DEFAULT_POINTS
from anima_world.media import (
    LOCATION_IMAGE_KEYS,
    LOCATION_IMAGE_MAX_BYTES,
    media_uri_errors,
)
from anima_world.narrative import MockNarrativeProvider, OpenAICompatibleNarrativeProvider
from anima_world.redis_state import RedisErasureProgress, RedisPlayerPresence
from anima_world.scheduler import MAX_TICKS_PER_SECOND, Scheduler
from anima_world.types import AgentState, Projection
from anima_world.world_time import DEFAULT_MINUTES_PER_TICK, DEFAULT_SECONDS_PER_TICK

logger = logging.getLogger(__name__)

# 聊天两条路产出的东西不同型(`chat` 是文本块,`chat_burst` 是步骤 dict),
# 而 `_noting_chat_health` 对两条都一视同仁 —— 它只管转发和记账。
_T = TypeVar("_T")

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
# 两格图也在这里出去 —— **`state()` 是它们唯一的读出口**,而网站每几秒
# 问一次这道门。漏了这一行,作者写的图会一路存进 Redis 却永远到不了玩家眼前,
# 全程零报错;而那正是角色卡那一次的病本身。没写图的地点这两格是 `None`。
_LOCATION_KEYS = (
    "id", "name", "description", "kind", "parent", "x", "y", "w", "h",
    *LOCATION_IMAGE_KEYS,
)

# 动作 → 她读到的那句话。**这张表要盖住 `ActionTable.default()` 的每一种**,
# 漏一种的样子不是报错,是一个正在干活的人被写成「闲着」:`interact` 与 `eat`
# 从前都不在表里,于是排班让她去"照料那棵树"、去吃饭的那一个钟头,同屋的人和她
# 自己的自主上下文里读到的都是「闲着」—— 而「闲着」正是"可以打扰"的意思。
_ACTIVITY_LABELS = {
    "sleep": "在睡觉", "work": "在工作", "chat": "在和人聊天",
    "walk": "正准备出门", "eat": "在吃饭", "interact": "在忙手上的事",
    "idle_wander": "闲着", "idle_social": "闲着",
}
# 闲着不算"在做什么"。别人眼里的那一行只在她**真的**在做点什么时才多出一句 ——
# 给每个人都缀一句「闲着」是提示词噪音,而**没有那句话本身就是闲着**。
_IDLE_KINDS = frozenset({"idle_wander", "idle_social"})

# 他按下去的时候那份邀请已经有了结局 —— **四种各说各的话**。
# 从前只有一句"要么答过了,要么已经过期",而它恰好把 `cancelled`(她自己把话
# 收回去了)排除在外 —— 四种里唯一一种他什么也没做错的。
_INVITE_GONE_LABELS = {
    "accepted": "这件事已经做过了 —— 这份邀请答应过了",
    "declined": "这份邀请你回过了 —— 你当时说的是不去",
    "expired": "这份邀请已经过期了",
    "cancelled": "她不等了 —— 这句话是她自己收回去的,不是你没答",
}
# **说不上来就别猜**(结局掉出了 `SETTLED_INVITATIONS_KEPT` 那一段)。
# 从四种里挑两种说,和猜一个说出来是同一种谎。
_INVITE_GONE_UNKNOWN = "这份邀请已经不在了 —— 它有了结局,只是隔得太久,说不上来是哪一种"

# 世界压根没有他那一行时,人话里管他叫什么。**这是引擎写的一个称呼,不是名字** ——
# 所以 `_named()` 不给它套「」(框的只有数据里来的那一截,见 `Scheduler._named`)。
_PLAYER_FALLBACK_DISPLAY = "这位玩家"


def _where_unknown_line(name: str, here_name: str) -> str:
    """「世界这会儿不知道你在哪」那一句 —— **两扇门共用的那一份**。

    `World._invite_absence`(他按「好」那扇)和 `World._colocation_error`
    (他直接动手那扇)都会走到这一支,而同一个玩家两扇门都撞得上。

    ⚠️ **这个函数存在的理由是"逐字同一句"曾经被人手抄着维持,然后塌了**
    (2026-08-20 第六轮):第五轮在 `_colocation_error` 头上写下「措辞现在和
    `_invite_absence` 那一支逐字同一句」,而那时它俩已经不一样了 —— 一处拼的是
    人话地名、一处拼的是裸 id(`苏晚夏在咖啡店` vs `苏晚夏在 cafe`),还差一个
    空格。**抄两遍来维持"逐字相同",正是塌掉的那个机制**;句子收在这里之后,
    "两扇门说同一句话"这件事由调用点保证,不再由谁读没读全那段注释保证。

    ⚠️ **收敛到这儿的只有一支,别读成"两扇门整体统一了"**(第七轮 2026-08-20
    认账;上面那段话写下来之后,两个验收员各自独立读成了后者)。共用的**只有
    「世界这会儿不知道你在哪」这一支**,也就是他没位置那一种。另外两种情形两扇门
    仍是各写各的,**而且其中一句说的是她的出发地**:

    下表引的都是**句子里说位置的那一截**,前后还有别的字,别当整句抄走:

    | 情形 | `_invite_absence`(他按「好」那扇) | `_colocation_error`(他动手那扇) |
    |---|---|---|
    | 她在途、他在别处 | 「…这会儿在路上,还没落脚 —— 不是你不在」 | 「…你在后院,苏晚夏在咖啡店。…」← **咖啡店是她的出发地** |
    | 世界不知道**她**在哪 | 「世界这会儿不知道…在哪 —— 不是你没到场。…」 | 「…这会儿在路上,不在任何地方」 |

    **两扇门在这两种情形上说的话恰好对调了。** 病根在 `_colocation_error` 那边:
    它**从不问 `scheduler._transit`**,只按 `here == where` 猜她在不在赶路。
    敲得动的判据(第七轮当场敲过):

        awk '/^    def _colocation_error/,/^    def intend/' anima_world/api.py | grep -c _transit

    今天答 `0`。行首那两个 `^    ` 是承重的:去掉它,这段 docstring 自己就成了
    awk 的起点,判据当场自答 `1`(第七轮真敲出来过)。⚠️ 它**宁可误报也不漏报**:
    哪天答出非 0,先看命中落在代码里还是落在一段讲这件事的注释里 ——
    所以那个函数的 docstring 里有意一次都没写这个名字。
    **这不是第六轮引入的** —— `git diff fdd2408 26a204c -- anima_world/api.py`
    里那两支的分支条件逐位相同,第六轮改的只是地名印不印成人话;
    **但也正因为改成了人话,那句假话更像真的了**。
    修法(改去问 `_colocation_gate` 那四个枚举,别自己按 `here` 猜)**有意留在
    定版之后**,已进看板 —— 定版前动一条判断逻辑,换来的是没人复验过的新分支。

    `here_name` 收的是**过了 `Scheduler.place_name()` 的人话地名**,不是 id ——
    拼给人看的句子里出现地点变量,先问一句「过 `place_name()` 了吗」。
    """
    return (
        f"世界这会儿不知道你在哪 —— 你可能已经离开这个世界了,"
        f"也可能是这一程还没把落脚处告诉世界。{name}在{here_name}"
    )


class _PlayerRow(MutableMapping):
    """一个在场玩家的那一行 —— **写穿到 Redis**。

    存在的理由是"只加不改":`World.players` 是公开属性,宿主与一堆测试早就在写
    `players[pid]["display_name"] = …` / `.setdefault(...)` / `.pop("location")`。
    把底座换成 Redis 之后,那些写法必须逐字还成立,否则搬家就是一次破坏性变更。
    读走快照(一次 `HGETALL`),写立刻落键。
    """

    __slots__ = ("_store", "_pid", "_snap")

    def __init__(self, store: RedisPlayerPresence, player_id: str,
                 snapshot: dict[str, Any]) -> None:
        self._store = store
        self._pid = player_id
        self._snap = dict(snapshot)

    def __getitem__(self, key: str) -> Any:
        return self._snap[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._snap[key] = value
        self._store.update(self._pid, {key: value})

    def __delitem__(self, key: str) -> None:
        del self._snap[key]
        self._store.drop_field(self._pid, key)

    def __iter__(self):
        return iter(self._snap)

    def __len__(self) -> int:
        return len(self._snap)

    def update(self, *args: Any, **kwargs: Any) -> None:   # type: ignore[override]
        fields = dict(*args, **kwargs)
        if not fields:
            return
        self._snap.update(fields)
        self._store.update(self._pid, fields)

    def __repr__(self) -> str:
        return f"_PlayerRow({self._pid!r}, {self._snap!r})"


class _PlayerRoster(MutableMapping):
    """在场名册。`World.players` 就是它。

    **过期只有一条规则:那个人的键还在不在**(Redis 自己的 TTL)。此前
    `_present_roster` 另外按 `last_seen` 过滤过一遍 —— 两套规则迟早给出不同答案,
    而两边都不报错。
    """

    __slots__ = ("_store",)

    def __init__(self, store: RedisPlayerPresence) -> None:
        self._store = store

    def __getitem__(self, player_id: str) -> _PlayerRow:
        row = self._store.get(player_id)
        if row is None:
            raise KeyError(player_id)
        return _PlayerRow(self._store, player_id, row)

    def __setitem__(self, player_id: str, row: Any) -> None:
        self._store.create(player_id, dict(row))
        self._store.update(player_id, dict(row))

    def __delitem__(self, player_id: str) -> None:
        if player_id not in self._store:
            raise KeyError(player_id)
        self._store.forget(player_id)

    def __iter__(self):
        return iter(self._store.ids())

    def __len__(self) -> int:
        return len(self._store.ids())

    def __contains__(self, player_id: object) -> bool:
        return isinstance(player_id, str) and player_id in self._store

    def __repr__(self) -> str:
        return f"_PlayerRoster({sorted(self._store.ids())!r})"


class _PlayerTransit(MutableMapping):
    """在路上的玩家。住在在场那一行里的一个字段上,所以和在场同生共死。"""

    __slots__ = ("_store",)

    def __init__(self, store: RedisPlayerPresence) -> None:
        self._store = store

    def __getitem__(self, player_id: str) -> dict[str, Any]:
        trip = self._store.get_transit(player_id)
        if trip is None:
            raise KeyError(player_id)
        return trip

    def __setitem__(self, player_id: str, trip: dict[str, Any]) -> None:
        self._store.set_transit(player_id, dict(trip))

    def __delitem__(self, player_id: str) -> None:
        if self._store.get_transit(player_id) is None:
            raise KeyError(player_id)
        self._store.clear_transit(player_id)

    def __iter__(self):
        return iter(self._store.transit_ids())

    def __len__(self) -> int:
        return len(self._store.transit_ids())

    def __contains__(self, player_id: object) -> bool:
        return (
            isinstance(player_id, str)
            and self._store.get_transit(player_id) is not None
        )

    def __repr__(self) -> str:
        return f"_PlayerTransit({sorted(self._store.transit_ids())!r})"


def _resolve_tick_rate(fallback: float, config_store: Any | None) -> float:
    if config_store is None:
        return fallback
    return config_store.get("scheduler.tick_rate", default=fallback)


def _transit_view(scheduler: Any, trip: Mapping[str, Any]) -> dict[str, Any]:
    """一趟没走完的路,写给宿主的那几栏:从哪、去哪、还有多少**世界分钟**。

    她和他各有一份在途(`scheduler._transit` / `World._player_transit`),而快照那扇门
    要把两份都报出去。**换算只准有这一处** —— 两边各算一遍的话,迟早一处忘了
    `world.minutes_per_tick` 不是 1,于是同一段路在角色那栏是 15 分钟、在玩家那栏
    是 3 分钟,而两栏都不报错。
    """
    mpt = DEFAULT_MINUTES_PER_TICK
    if scheduler.config_store is not None:
        mpt = scheduler.config_store.get("world.minutes_per_tick", default=mpt)
    remaining = max(0, int(trip["arrive_at"]) - int(scheduler.clock))
    return {
        "from": trip["from"],
        "to": trip["to"],
        "eta_minutes": remaining * int(mpt),
    }


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
            activity["transit"] = _transit_view(self.scheduler, trip)

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

    def identity_rows_locked(self) -> dict[str, dict[str, Any]]:
        """每个人的**身份那几栏**:名字 / 此刻在哪 / 状态 / 在不在场 / 作者写的卡。

        **`state()` 和 `roster()` 共用这一份。** 位置那条规矩是有讲究的(在场读活
        黑板 —— 在途时只有黑板是真的;离场读投影),抄第二遍就是抄错第二遍:两扇门
        会对"她在哪"给出两个答案,而宿主读哪一扇全凭运气。名册那个洞
        (`mai：`)本身就是"没有一条读得回来的路"造成的,再多造一条平行的路只是
        把同一个病换个位置犯。

        `card` 从投影的 spec 里读(创世那条 `agent_join` 事件写进去的)——
        **不上黑板**:`tagline` 是写给玩家的广告词,进了黑板她就会照着念。
        """
        rows: dict[str, dict[str, Any]] = {}
        for aid, a in self.projection.agents.items():
            brain = self.scheduler.agents.get(aid)
            # 在场角色的位置读活黑板(在途时黑板才是真的),离场的读投影。
            # 以前这是靠每 tick 往第二份投影里回写一次维护的。
            location = (
                (brain.agent.blackboard.read("loc") or brain.agent.location)
                if brain is not None
                else a.location
            )
            rows[aid] = {
                "name": brain.agent.name if brain else a.spec.get("name", aid),
                "location": location,
                "state": dict(a.state),
                "away": brain is None,
                "card": normalize_card(a.spec.get("card"))
                if isinstance(a.spec, dict) else None,
            }
        for aid, brain in self.scheduler.agents.items():
            if aid not in rows:
                rows[aid] = {
                    "name": aid,
                    "location": brain.agent.location or "",
                    "state": {},
                    "away": False,
                    "card": None,
                }
        return rows

    def _snapshot_locked(self, recent_events: list[dict[str, Any]]) -> dict[str, Any]:
        agents = {}
        for aid, row in self.identity_rows_locked().items():
            # `card` 有意不进 `state()`:那扇门是"世界此刻的样子",而卡是写给
            # 玩家看的元数据,它的门是 `roster()`。
            agents[aid] = {
                "name": row["name"],
                "location": row["location"],
                "state": row["state"],
                "activity": self._agent_activity(aid),
                "away": row["away"],
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

# 引擎回执的备忘录最多留几句 —— 见 `World._remember_receipt`。
_RECEIPT_MEMO_MAX = 256

# 邀请判定带进提示词的转录轮数。取最后几条就够:判定器要知道的是"他俩这会儿
# 在说话、说到哪儿了",不是整场会话 —— 整场会话由记忆那条路负责,而提示词有上限。
_INVITE_TALK_TURNS = 6

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
    # 她开了口,他还没答。**不是错**,也不是拒绝 —— 她该等,或者去做别的;
    # 报成 `ToolCallError` 的话,一次成功的邀请在她眼里会变成一句报错。
    "invited",
})


# ── 法务抹除(`World.erase_player`)的纯文本部分 ─────────────────────────────
# 涉他事件里要抹空的原文字段。加了会带**玩家原文**的新事件字段,把键名登进来 ——
# 抹除认的是这张表,不认识的键只做名字替换,漏登等于给原文留一条静默的活路。
_ERASE_TEXT_KEYS = frozenset({
    "summary", "conversation_summary", "note", "text", "content", "transcript",
})
_ERASED_TEXT = "(已抹除)"
_ERASED_NAME = "(已注销)"
# id 键 → 名字键的配对:`player_id`→`player_name` 这类按 `_id`→`_name` 推,
# 判定事件的 `as`/`target` 与账本的 `from`/`to` 是仅有的四个不带后缀的。
_ERASE_ID_KEYS = frozenset({"as", "target", "from", "to"})
#: 回执里跨片累加的那几格(进度键里存的也是这几格)。
_ERASE_COUNT_KEYS = (
    "events", "conversations", "messages", "memories_dropped", "memories_redacted",
)
#: 改写多少条落一次水位。**按批,不按条** —— 每条一次 Redis 往返是这个仓库
#: 明说过的反面教材(`catch_up_projection` 那条)。和 `_iter_event_log` 的一页同宽。
_ERASE_CURSOR_EVERY = 500
#: 进度键活多久。**它装着正要被抹掉的那些名字**,所以必须会过期 —— 一个不过期的
#: 进度键就是把抹除的对象原样另存一份,还存在最不容易被想起来的地方。而它又必须
#: 远长于任何一趟抹除可能花的时间(晚潮实测一趟 188 秒),否则续跑读不到名字,
#: 那个不可逆的死角就回来了。24 小时是这两条之间的取值,不是一个精确的数。
_ERASURE_PROGRESS_TTL_SECONDS = 24 * 60 * 60
#: `phase`:**这个人在这个世界里的抹除处在哪一步**,不是"他被抹干净了没有"。
_ERASE_PHASE_NOT_STARTED = "not_started"
_ERASE_PHASE_PARTIAL = "partial"
_ERASE_PHASE_DONE = "done"


def _mentions_pid(value: Any, pid: str) -> bool:
    """载荷的任何深度上有没有一个字符串值**恰等于**这个 id。恰等于,不是包含 ——
    子串匹配会把 `aubrey-player` 认成 `aubrey`(`forget_player` 那条同款教训)。"""
    if isinstance(value, str):
        return value == pid
    if isinstance(value, Mapping):
        return any(_mentions_pid(v, pid) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_mentions_pid(v, pid) for v in value)
    return False


def _collect_player_names(value: Any, pid: str, out: set[str]) -> None:
    """从载荷里收他的显示名:某个 id 键的值是他,就收它配对的名字键。

    **配对收,不是见名就收**:hail 的载荷里 `player_id` 旁边坐着 `agent_name` ——
    把同一个 dict 里所有 `*name*` 都当成他的名字,就会把她的名字也抹成「已注销」。
    """
    if isinstance(value, Mapping):
        for key, v in value.items():
            if isinstance(v, str) and v == pid:
                name_key = None
                if isinstance(key, str) and key.endswith("_id"):
                    name_key = key[:-3] + "_name"
                elif key in _ERASE_ID_KEYS:
                    name_key = f"{key}_name"
                if name_key:
                    name = value.get(name_key)
                    if isinstance(name, str) and name.strip():
                        out.add(name.strip())
            _collect_player_names(v, pid, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _collect_player_names(v, pid, out)


def _erase_probe(value: Any, replacements: dict[str, str], *, blank: bool) -> bool:
    """"这条载荷抹起来会不会变"—— 只答是非,不造新值。

    `dry_run` 只用得着那个 bool(回执里的 `events` 就是它数出来的),而
    `_erase_scrub` 为了给出新值要把每一层 dict/list 重建一遍。两者必须给同一个
    答案,所以叶子那一层**照抄**下面那份的判断(替换是串起来的 —— 后一个 `old`
    看到的是前一个替完的样子,写成"某个 old 是子串"就是另一个函数了),
    省下的只有拷贝和**第一处命中就返回**(`_erase_scrub` 非得走完全程,
    因为它要交出完整的新值)。
    """
    if isinstance(value, str):
        fresh = value
        for old, new in replacements.items():
            fresh = fresh.replace(old, new)
        return fresh != value
    if isinstance(value, Mapping):
        for key, v in value.items():
            if (blank and key in _ERASE_TEXT_KEYS
                    and isinstance(v, str) and v and v != _ERASED_TEXT):
                return True
            if _erase_probe(v, replacements, blank=blank):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_erase_probe(v, replacements, blank=blank) for v in value)
    return False


def _erase_scrub(value: Any, replacements: dict[str, str], *,
                 blank: bool) -> tuple[Any, bool]:
    """一份载荷的抹除拷贝:名字全域替换;`blank=True`(这条事件涉他)时把
    `_ERASE_TEXT_KEYS` 里的原文字段整个抹空。返回 (新值, 改没改)。

    只想知道"会不会变"就用 `_erase_probe` —— 它不造那份拷贝。
    """
    if isinstance(value, str):
        fresh = value
        for old, new in replacements.items():
            fresh = fresh.replace(old, new)
        return fresh, fresh != value
    if isinstance(value, Mapping):
        changed = False
        out: dict[str, Any] = {}
        for key, v in value.items():
            if (blank and key in _ERASE_TEXT_KEYS
                    and isinstance(v, str) and v and v != _ERASED_TEXT):
                out[key] = _ERASED_TEXT
                changed = True
                continue
            fresh, hit = _erase_scrub(v, replacements, blank=blank)
            out[key] = fresh
            changed = changed or hit
        return (out, True) if changed else (dict(value), False)
    if isinstance(value, (list, tuple)):
        changed = False
        items = []
        for v in value:
            fresh, hit = _erase_scrub(v, replacements, blank=blank)
            items.append(fresh)
            changed = changed or hit
        return (items, changed)
    return value, False


def _iter_event_log(log: Any, *, since: int = 0, page_size: int = 500) -> Any:
    """流式过一遍事件日志 —— 抹除要看每一条,而日志没有上限,不整只端起来。

    ⚠️ 这只有在 `page(limit=)` **真的只读那么多行**时才是流式的。`RedisEventLog`
    从前是读完整条尾巴再切,于是这个循环整体 O(N²) —— 而它一页页翻的样子和线性
    一模一样,没有任何一处报错。见 `RedisEventLog._rows`。
    """
    while True:
        batch = log.page(since_seq=since, limit=page_size)
        if not batch:
            return
        yield from batch
        since = int(batch[-1].seq or 0)


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
        """**墙钟**分钟 → 世界 tick 数。

        换算走 `scheduler.tick_rate`(每真实秒几个 tick),**不走**
        `world.minutes_per_tick` —— 后者是世界时间每 tick 走多少分钟,和真实时间
        没有固定比例。两者只在引擎默认值下恰好相等(1 tick = 5 分钟世界时间,
        也正好 5 分钟真实时间),所以拿错了那一个在开发机和整套测试上一路是对的。

        线上那个 `tick_rate=0.2` 的世界里,「等我五分钟」按 `minutes_per_tick`
        折出来是 1 tick = **5 秒**:调度器五秒后就把那条回访兑现了,顺手
        `clear_quiet` 撤掉配对的那次静音 —— 她话音未落自己回来敲门,而玩家那一侧
        既没看见「她在忙」,也没等到那五分钟。

        为什么必须是墙钟:这个数唯一的用处是给 `delay_reply` 排那次回访,而回访和
        `set_quiet` 是**同一个承诺的两半**;`chat_state` 那侧早就写死了静音走墙钟
        (玩家的五分钟是真的五分钟)。两半用两个时钟,就是这个 bug 本身。
        """
        rate = _resolve_tick_rate(
            1.0 / DEFAULT_SECONDS_PER_TICK, self._world.scheduler.config_store
        )
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            rate = 1.0 / DEFAULT_SECONDS_PER_TICK
        if rate <= 0:
            rate = 1.0 / DEFAULT_SECONDS_PER_TICK
        return max(1, int(round(float(minutes) * 60.0 * rate)))

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

    def _resolve_party(
        self, raw: Sequence[str], *, player_id: str = "",
    ) -> tuple[list[str], str]:
        """玩家和模型嘴里的名字 → 世界里的 id。返回 `(名单, 认不出的那个)`。

        三种写法都要认得:角色 id、角色名字、以及**玩家**(`player:<id>`、
        玩家的显示名、或者一句「我」)。认不出来当场说,别静默丢掉一个人 ——
        丢掉之后人数就对不上 `participants.min`,而报出来的会是"人不够",
        于是真正的原因("我不认识白霜")永远说不出口。

        **「我」说的是这一轮的那个人**(`player_id`),不是名册里排第一的那个。
        从前是后者 —— 一个人的房间里两者恰好相同,所以一直没错过;而一屋子两个
        玩家时,点头的和被拉进来的会是两个人,回执上却只有一个名字。
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
            if who in FIRST_PERSON and player_id:
                out.append(f"player:{player_id}")
                continue
            matched = next(
                (f"player:{pid}" for pid, name in players.items()
                 if who in (pid, name)),
                "",
            )
            if matched:
                out.append(matched)
                continue
            return ([], who)
        return (list(dict.fromkeys(out)), "")

    def _consent(
        self, actor_id: str, target: str, verb: str, party: Sequence[str],
        *, player_id: str = "", accepted_ids: Sequence[str] = (),
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """挨个问过去。返回 `(答应了的, 没答应的, 还等着答的)`,三边都是
        `Consent.to_dict()`。

        **两段,次序是有意的**:先世界(`joint_gate` + 静音 + 玩家在不在跟前),
        再性格。一句笼统的"她没答应"会让玩家以为被拒绝的是这个人,而真正的原因
        可能只是他在赶路 —— 那种误解在世界里是**改不回来**的。

        **邀请人也可能是玩家。** 判定器读到的那个名字要照他的显示名写 ——
        给一个 `player:9f2c…` 过去,她收到的邀请是一个 uuid 发来的。

        ## 第三堆是新的:被点名的**玩家**不再当场被替着点头

        这里从前写着一句假话:「玩家过了在场那道闸就是答应 —— 他就是发起这次调用
        的那个人」。**在这条分支上他从来不是。** 玩家自己按按钮时他是 `actor_id`,
        走不到这里;走到这里的只有一种情形 —— **一个 NPC 点了他的名**(聊天里
        `interact(with=["我"])`,或者一轮 autonomy 里她自己决定的)。于是引擎替他
        答应了一件他根本没被问过的事,而这个模块的第一条红线就是**邀请必须能被
        拒绝**。同一条红线保护着虚构的人、取消着真人的意志,是这个引擎最不该有的
        那种不对称。

        现在这一支落一条 `agent_invites`,然后**等**。三条:

        - **`accepted_ids` 是唯一能替玩家点头的路**,而它只有引擎内部两条路传得
          进来(私有形参,公开的 `interact_with` 一个字没变):`answer_invitation`
          (他按下的那一下)与 `Director._together`(**他自己开的口** —— 那句
          「陪我听完这一面」就是他的同意,再给他发一封信问他要不要做他刚说的事,
          是这一层能犯的最荒唐的错)。伪造一次同意因此不是"别这么做",是**没有
          这个写法**。
        - **一次只算得进一个要被问的玩家**(见 `_interact_with` 那道形状闸)。
          两个人的头点在两次调用里,而一次 `answer_invitation` 只带得动一个 ——
          于是先点头的那个必然被记成"没做成",他按了"好"而世界一声不吭。
        - **它跳过的只有"问"这一步,不跳过闸。** 他点头那一刻人可能已经走开了 ——
          `_invitee` 的 `player_not_here` 照查(这就是验收里"答复那一刻重查"的落点)。
        - 世界关着这扇门(`social.joint.npc_may_invite_player`)、或者她今天已经
          问够了(`social.joint.invites_per_player_per_day`),都归**硬闸**那一堆:
          那是世界的状态,不是他的意思。**问够了不是错**,是她今天不再开口。
        """
        scheduler = self._world.scheduler
        judge = scheduler.relationship_judge
        judge_invite = getattr(judge, "judge_invite", None) if judge is not None else None
        min_willingness = float(self._world.config_get(
            "social.joint.min_willingness", together.DEFAULT_MIN_WILLINGNESS))
        stock_key = str(self._world.config_get(
            "social.joint.consent_stock", together.DEFAULT_CONSENT_STOCK) or "").strip()
        inviter = self._display(actor_id)
        verb_label, target_name = self._affordance_display(target, verb)

        accepted: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        to_invite: list[str] = []
        pre_ok = {str(a) for a in accepted_ids if str(a)}
        for who in party:
            invitee = self._invitee(
                actor_id, target, verb, who,
                player_id=player_id, stock_key=stock_key,
            )
            if invitee.gate:
                refused.append(together.decide_alone(
                    invitee, min_willingness=min_willingness).to_dict())
                continue
            if who in pre_ok:
                # 他**真的点过头了**(他按下的那一下、他自己开的那句口、或者她
                # 开口那一刻就已经答应过的人)。这一句在这几条路上是真的 ——
                # 从前它写在所有路上,那才是假的。
                accepted.append(together.Consent(
                    who=who, accepted=True, source="gate",
                    note="你自己点的头" if invitee.is_player else "她当时点的头",
                ).to_dict())
                continue
            if invitee.is_player:
                # **先记下,最后再开口**(见循环下面那一段)。
                to_invite.append(who)
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
                        # 人话,不是键名:判定器写出来的话进她的记忆,而
                        # `agent_location` 的契约是 id(公开 API,不动它)。
                        location=scheduler.place_name(self.agent_location(who)),
                        recent_talk=invitee.recent_talk,
                    )
                except Exception:  # noqa: BLE001 - 判定器挂了不该掀翻这次调用
                    logger.warning("邀请判定失败 %s ← %s", who, actor_id, exc_info=True)
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

        # **开口排在最后,而且只在别人都过得了的时候。** 一件已经办不成的事
        # (有人过不了闸、有人不肯)不该先在他手机上响一下再自己消失 —— 那份邀请
        # 会挂到 ttl 才过期,还白占掉她今天的额度。这是"一个人过不了闸,整件事就
        # 不发生"(红线 2)在开口这一侧的样子。
        #
        # 没被问到的人**不进 `consents`**:那张表记的是"问了谁、他说了什么",
        # 而他根本没被问 —— 塞一条进去,读的人分不出"他没答"和"没问他"。
        if to_invite and not refused:
            for who in to_invite:
                asked = self._invite(
                    actor_id, target, verb, who, party,
                    verb_label=verb_label, target_name=target_name,
                    inviter=inviter,
                    # **她开口那一刻,别人已经点过的头一起存进去。** 不存的话,
                    # 他点头那一刻得把每个人重新问一遍 —— 而"再问一次"读的是
                    # 模型,答案可以和上一次不同:于是他按了"好",却因为她这次
                    # 改了主意而被记成"没做成"。见 `answer_invitation`。
                    consented=[str(c.get("who") or "") for c in accepted],
                )
                (pending if asked.get("ok") else refused).append(asked["consent"])
        return (accepted, refused, pending)

    def _invite(
        self, actor_id: str, target: str, verb: str, who: str,
        party: Sequence[str], *, verb_label: str, target_name: str, inviter: str,
        consented: Sequence[str] = (),
    ) -> dict[str, Any]:
        """她对一个玩家开口。返回 `{"ok", "consent", ...}`。

        **句子在这里拼一次,存进事件**(`together.describe_invitation`)——
        和判定器读到的是同一句。读那扇门的时候现拼一遍的话,作者明天改了动词的
        label,他手机上那条昨天的邀请会跟着变成另一句话。
        """
        pid = who.split(":", 1)[1]
        others = [self._display(p) for p in party if p != who]
        text = together.describe_invitation(
            inviter=inviter, verb_label=verb_label,
            target_name=target_name, others=others,
        )
        asked = self._world.scheduler.invite_player(
            actor_id, pid, target=target, verb=verb, party=list(party), text=text,
            verb_label=verb_label, target_name=target_name, agent_name=inviter,
            consented=[c for c in consented if c],
        )
        if not asked.get("ok"):
            # 门关着 / 今天问够了。**归硬闸那一堆** —— 那是世界的状态,
            # 不是他的意思(`GATE_LABELS` 里那两条)。
            reason = str(asked.get("reason") or "")
            gate = "player_invites_off" if reason == "invites_off" else "invite_capped"
            return {"ok": False, "consent": together.Consent(
                who=who, accepted=False, reason=gate, source="gate",
                note=together.GATE_LABELS[gate],
            ).to_dict()}
        consent = together.Consent(
            who=who, accepted=False, reason=together.INVITE_PENDING,
            source="gate", note=text,
        ).to_dict()
        consent["invite_seq"] = asked["invite_seq"]
        consent["expires_tick"] = asked["expires_tick"]
        return {"ok": True, "consent": consent, **asked}

    def withdraw_invitations(
        self, agent_id: str, player_id: str = "", *, note: str = "",
    ) -> list[int]:
        """她走开时,把还等着回话的邀请一起收回去(委托
        `Scheduler.cancel_invitations`,理由写在那儿)。

        **不按玩家挑。** 她起身去了另一个地方,那份"要不要一起坐会儿"对**谁**
        都已经办不成了 —— 只收回跟她正说着话的那个人的话,别人手机上还亮着一份
        她此刻根本兑现不了的邀请。
        """
        return self._world.scheduler.cancel_invitations(
            agent_id, player_id, note=note)

    def _display(self, who: str) -> str:
        if who.startswith("player:"):
            pid = who.split(":", 1)[1]
            name = self.player_name(pid)
            # **裸 id 不进人话。** `player_name()` 找不到行时回落成 id —— 那是
            # 给调用方的兜底,而这里拼出来的句子是**念给玩家看的**:
            # 「「p1」不在她跟前」里那个 `p1` 是一个主键,不是任何人的名字。
            # 而这一支恰好只在"世界没有这个玩家的行"时才走到 —— 也就是他刚
            # `player_leave` 过、或者宿主从没登记过他,正是最该说实话的那次。
            return _PLAYER_FALLBACK_DISPLAY if name == pid else name
        return self.agent_names().get(who, who)

    def _named(self, who: str) -> str:
        """同一个名字,**紧贴着下一个字**印时的样子 —— 见
        `Scheduler._named`,那儿写全了理由。这一份的名字还多一个来路:
        玩家的昵称是**用户**填的,而用户填得出「老陈的猫」。

        ⚠️ **框的只有数据里来的那一截**(和 `Scheduler._named` 里玩家那个「你」
        逐字同一条):`_display()` 兜底的「这位玩家」是**引擎写的一个称呼**,
        不是谁的名字 —— 套上「」读起来像在念一个人的名字,而这一支恰好只在
        世界压根没有他那一行时走到,正是最不该假装知道他叫什么的那次。
        """
        shown = self._display(who)
        if shown == _PLAYER_FALLBACK_DISPLAY and who.startswith("player:"):
            return shown
        return f"「{shown}」"

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

    def _colocation_gate(self, actor_id: str, player_id: str) -> str:
        """他俩这会儿当不成面的话,**是哪一种**当不成面。当得成就是空串。

        `face_to_face()` 把四种原因折成同一个 `False`,而这一层要把它们分开说 ——
        理由和 `intent._colocation_refusal` 逐字同一条:「你不在她跟前」在一个
        宿主没落过 `player_move` 的世界里是**一句假话**,它把"世界不知道他在哪"
        说成了"他站错了地方",而后者他自己改得掉、前者他做什么都改不掉。

        次序是承重的:先问她在不在赶路,再问她的位置,最后才问他的 ——
        `agent_location()` 对在途的人仍报着**出发前那个地名**,先看地名的话,
        两处相同就会得出"他们在一起"这个和 `face_to_face()` 相反的结论。
        判定与 `face_to_face()` 必须逐位同构,两份判断分了岔就会出现"闸说得出
        理由、门却没拦"。⚠️ **这句"钉着"从前是假的**:3.6.0 第四轮写下它时,
        `tests/` 里一处都没提过这个函数名 —— 一句"有测试守着"比没有测试更坏,
        读的人会照着它省下自己那一次检查。3.6.0 第五轮(2026-08-20)才真的钉上:
        `tests/test_interaction_witness_and_invites.py::test_闸和面对面必须逐位同构`,
        按**状态 ×(她, 他)**铺开对,不是挑一个样本点。
        """
        scheduler = self._world.scheduler
        if actor_id in scheduler._transit:
            return "inviter_in_transit"
        here = self.agent_location(actor_id)
        if not here:
            return "inviter_where_unknown"
        where = self._world.player_location(player_id).strip()
        if not where:
            return "player_where_unknown"
        return "" if here == where else "player_not_here"

    def _invitee(
        self, actor_id: str, target: str, verb: str, who: str,
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
            else:
                gate = self._colocation_gate(actor_id, pid)
            return together.Invitee(
                id=who, name=self.player_name(pid), is_player=True, gate=gate,
            )
        gate = scheduler.joint_gate(actor_id, target, verb, who)
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
        inviter_name = self._display(actor_id)
        memories: tuple[str, ...] = ()
        if scheduler.memory_store is not None:
            try:
                store = scheduler.memory_store
                if hasattr(store, "retrieve"):
                    # **按相关性召回,不是按新鲜度。** 判定器要判的是「他跟这个
                    # 邀请人熟不熟」,而挑最近三条给出来的常常是关于**另外一个人**
                    # 的事 —— 于是模型读着三段与邀请人无关的记忆,得出"不认识"。
                    # 和聊天那侧 `world_context` 同一条路:拿对方的**名字**当 query。
                    rows = store.retrieve(
                        who, now_tick=scheduler.clock, query=inviter_name, k=3)
                else:
                    rows = store.query(agent_id=who)[:3]
                memories = tuple(str(m.get("summary") or "") for m in rows)
            except Exception:  # noqa: BLE001 - 读不到记忆不该挡住一次邀请
                logger.debug("读 %s 的记忆失败", who, exc_info=True)
        recent_talk = self._recent_talk(who, actor_id, player_id=player_id)
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
            # 邀请人是玩家时关系挂在**裸 pid** 上(`Scheduler._relation_id`
            # 那一课:库存带前缀、关系不带)。带着前缀查的话永远查不到,于是
            # 一个跟他熟得不能再熟的人被当成生人来判,而且一声不吭。
            relation=scheduler._memory_projection.relations.get(
                (who, scheduler._relation_id(actor_id))),
            agreeableness=agreeableness, stance=stance,
            personality=personality, memories=memories,
            recent_talk=recent_talk,
        )

    def _recent_talk(
        self, who: str, actor_id: str, *, player_id: str,
    ) -> tuple[str, ...]:
        """他和邀请人**这会儿正说着的话**,渲染成 `名字：内容` 的几行。

        ⚠️ **邀请正发生在会话中间,而记忆是会话关闭那一刻才落的。** 只给记忆的话,
        判定器判的是一个「我压根不记得跟这个人说过话」的处境 —— 而他们刚聊了两轮,
        玩家眼里的样子是"我跟她聊得好好的,一叫她就说不熟"。世界照跑,日志一行不错。

        只在**邀请人就是这场会话的那个玩家**时取:NPC 之间没有转录可读,而另一个
        玩家的会话不是这一次邀请的上下文。
        """
        if not player_id or not actor_id.startswith("player:"):
            return ()
        if actor_id.split(":", 1)[1] != player_id:
            return ()
        store = getattr(self._world, "chat_store", None)
        if store is None:
            return ()
        try:
            active = store.active_conversation(who, player_id=player_id)
            if active is None:
                return ()
            rows = store.recent_messages(int(active["id"]), _INVITE_TALK_TURNS)
        except Exception:  # noqa: BLE001 - 读不到转录不该挡住一次邀请
            logger.debug("读 %s 与 %s 的转录失败", who, player_id, exc_info=True)
            return ()
        inviter_name = self.player_name(player_id)
        agent_name = self.agent_names().get(who, who)
        out: list[str] = []
        for row in rows:
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            speaker = inviter_name if row.get("role") == "user" else agent_name
            out.append(f"{speaker}：{content}")
        return tuple(out)

    def interact_with(
        self, actor_id: str, target: str, verb: str,
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

        **施动者可以是玩家**(`player:<id>`,由 `ToolContext.world_actor_id` 拼)。
        这一整条从闸到扣账没有一处按"是不是角色"分支:窗她擦得了我也擦得了,
        擦完一样掉体力。给玩家关着的那阵子,同一个世界里是两套物理 —— 而她那套
        才是真的。`player_id` 是另一回事:它说的是这一轮跟她说话的人是谁,
        一次角色调用照样带着它。

        ⚠️ **被点名的玩家不在这条路上答应**(3.6.0):她点了他的名,落一条
        `agent_invites` 然后等 —— 回执是 `reason: "invited"`,不是 `ok: True`。
        替他点头的唯一入口是 `World.answer_invitation()`,而它走的是下面那条
        私有的 `_interact_with`。
        """
        return self._interact_with(
            actor_id, target, verb, participants, player_id,
        )

    def _interact_with(
        self, actor_id: str, target: str, verb: str,
        participants: Sequence[str] | None = None, player_id: str = "",
        *, accepted_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        """`interact_with` 的正文,外加一个**只有引擎内部给得出**的参数。

        `accepted_ids` 里的人**已经真的点过头了**。它私有,是因为它是这一层唯一
        能绕过"问一遍"的东西 —— 公开出去就等于给每个宿主一把替玩家点头的钥匙,
        而这个模块的第一条红线正是"邀请必须能被拒绝"。三条路传得进来,每一条都
        握着一次**真的**点头:

        - `World.answer_invitation()` —— 他按下的那一下。
        - `Director._together` —— **他自己开的口**。玩家在对话里说「陪我听完
          这一面」,那句话就是他的同意;再落一条 `agent_invites` 问他要不要做
          他刚说的事,是把一次表态换成一封给他自己的信。裁决里"玩家自己按按钮
          那条路一个字不变"这句话,只有在**聊天这条路也不变**时才是真的 ——
          按钮和聊天是同一个人的同一个意思,走的却是两条代码。
        - `_consent` 存进 `agent_invites` 的 `consented`(她开口那一刻已经答应
          过的人),由 `answer_invitation` 原样带回来。

        **它跳过的只有"问",不跳过闸**:`_consent` 里 `pre_ok` 那一支排在
        `invitee.gate` 之后,所以"他这会儿还在不在她跟前"照查。
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
            party, unknown = self._resolve_party(party_raw, player_id=player_id)
            if unknown:
                raise tools_mod.ToolCallError(
                    f"我不认识{unknown} —— 这个世界里现在只有"
                    f"{'、'.join(self.agent_names().values())}"
                )
            if actor_id in party:
                # 回执的口气跟着施动者走(`_display_name`:玩家印「你」)——
                # 玩家点一次"跟我一起",收到一句"她不能跟自己……",说的是别人。
                raise tools_mod.ToolCallError(
                    f"{scheduler._named(actor_id)}不能跟自己一起做一件事")
            # **一次只问得动一个玩家。** 两个人的头点在两次 `answer_invitation`
            # 里,而一次调用只带得动一个 pid —— 先点头的那个走完整条路,却在
            # `_invitee` 那道 `player_not_you` 上把另一个人判成"过不了闸",于是
            # 整件事记成 `expired`:**他按了「好」,而世界一声不吭**。这是这个
            # 仓库最忌的那类坏法(别摆一个必然失败的按钮),所以拦在开口之前 ——
            # 拦在后面的话,两份挂到过期的邀请已经发出去、额度也已经扣掉了。
            asked_players = [
                p for p in party
                if p.startswith(scheduler.PLAYER_PREFIX)
                and p not in {str(a) for a in accepted_ids if str(a)}
            ]
            if len(asked_players) > 1:
                raise tools_mod.ToolCallError(
                    "一起做一件事,一次只算得进一个还没点头的人:"
                    f"{'、'.join(self._named(p) for p in asked_players)}"
                    "都在名单里,而点头这件事替不了别人 —— 分两次约"
                )
            accepted, refused, pending = self._consent(
                actor_id, target, verb, party, player_id=player_id,
                accepted_ids=accepted_ids,
            )
            consents = [*accepted, *refused, *pending]
            if pending and not refused:
                # **她问了,他还没答。** 这不是"她被拒绝了",两者必须分得开 ——
                # 合成一句的话,她下一步该"换个人"还是该"等一会儿",谁也说不出。
                # 排在 `refused` 之后:一件已经办不成的事(有人过不了闸)不该被
                # 报成"等他回话",那会让调用方白等一个永远不会成真的答复。
                asked = pending[0]
                return {
                    "ok": False, "target": target, "verb": verb,
                    "reason": together.INVITE_PENDING,
                    "refusal": (
                        f"{self._named(str(asked.get('who') or ''))}"
                        f"{together.INVITE_PENDING_LABEL}"
                    ),
                    "invite_seq": asked.get("invite_seq"),
                    "expires_tick": asked.get("expires_tick"),
                    "consents": consents,
                }
            if refused:
                # **点名说出是谁、为什么。** 一句"没人答应"会让下一步无从谈起:
                # 该换个人、该等他睡醒、还是该死心,是三件完全不同的事。
                who = refused[0]
                name = self._display(str(who.get("who") or ""))
                # **闸的名字也交出去**(`gate`,只加不改):`reason` 那一格上
                # "闸拦下的"和"他自己说不去"都写成 `declined` —— 两件事在他
                # 手机上是两句完全不同的话,而只有这一格分得开。人话照旧在
                # `refusal` 里;枚举给机器读,句子给人读(那条纪律的两半)。
                gate_key = (
                    str(who.get("reason") or "")
                    if str(who.get("source") or "") == "gate" else ""
                )
                gate = together.GATE_LABELS.get(str(who.get("reason") or ""))
                if gate is not None:
                    # 世界那一段:回执是「名字 + 一句状态」。名字划边界(中文
                    # 不分词,而它是数据);冒号那一支不用划,冒号已经断开了。
                    refusal = f"{self._named(str(who.get('who') or ''))}{gate}"
                else:
                    # 她自己那一句 —— **原话进引号**。混在陈述句里的话,"白霜不想
                    # 凑在一起"读起来像引擎的判词,而那句话是她说的。
                    note = str(who.get("note") or "").strip()
                    refusal = (
                        f"{name}:「{note}」" if note
                        else f"{self._named(str(who.get('who') or ''))}"
                             f"{together.DECLINE_LABEL}"
                    )
                out = {
                    "ok": False, "target": target, "verb": verb,
                    "reason": "declined", "refusal": refusal,
                    "consents": consents,
                }
                if gate_key:
                    out["gate"] = gate_key
                return out
            party = [str(c["who"]) for c in accepted]
        with scheduler._lock:
            outcome = scheduler.perform_affordance(actor_id, target, verb, party)
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
        # 引擎最近自己说过的那几句回执(插入序 = 先进先出)。落转录时摘掉它们,
        # 见 `_strip_receipt`;闸装在 `chat_store.content_filter` 上。
        self._chat_receipts: dict[str, bool] = {}
        self.chat_store.content_filter = self._strip_receipt
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
            # 名册是她名字的**唯一**出处 —— 顺手抄一份到 `participants` 上就成了
            # 两份真相(玩家那一头必须抄,因为世界不从别处认识他;她不必)。
            agent_name=lambda aid: self._tool_runtime.agent_names().get(aid, aid),
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
        # 在场玩家。**3.2.0 之前这是一个进程内的普通 dict,注释里写着"刻意内存态"** ——
        # 那个"刻意"在单进程宿主里成立,在容器里不成立:一次重启把名册清空,
        # `player_location()` 返回空串,于是每一场对话静默退回"手机私聊"的措辞,
        # 而人就坐在她对面。没有异常、日志干净 —— 照跑,但给错东西。
        # 按分家判据它本来就该落 Redis:并发在场的人**有界**,而 `_present_roster`
        # **直接进她的决定上下文**。两条都指向 Redis。
        self.presence_store = RedisPlayerPresence(
            self.scheduler.redis, self.scheduler.world_id or "", _PLAYER_TTL_SECONDS
        )
        # 一趟没做完的法务抹除做到哪儿了(3.5.0)。**平时是空的** —— 键在就说明
        # 有一趟停在半路,而它记着的名字是那一趟唯一能把活干完的凭据。
        self.erasure_progress = RedisErasureProgress(
            self.scheduler.redis, self.scheduler.world_id or "",
            _ERASURE_PROGRESS_TTL_SECONDS,
        )
        # 公开属性照旧叫 `players`,照旧是个 mapping(只加不改):底下换成写穿到
        # Redis 的视图,`players["p1"]["display_name"] = …` 这类写法一个字不用改。
        self.players: _PlayerRoster = _PlayerRoster(self.presence_store)
        # 玩家的行程。**人走路和她走路一样要花时间** —— 同一份 `_travel_minutes`,
        # 同一条 `travel` 事件。和角色那份(`scheduler._transit`)分开放,因为落地
        # 的方式不同:她由 tick 循环 `_land_arrivals` 放下,人是**读的时候结算**
        # (下面 `player_location`)—— 玩家没有 tick 循环替他跑,而"到点了却没人
        # 把他放下"会让他永远停在路上。惰性结算是这个引擎里的现成套路:
        # `quiet_until` / `refused_topics` 也是读到就顺手清过期的。
        # 它和在场同住一个 hash(`__transit__` 字段):两个键就有两条过期规则,
        # 而"人走了、他的行程还在"会让 `player_in_transit` 说谎。
        self._player_transit: _PlayerTransit = _PlayerTransit(self.presence_store)
        # 他上一次开口是哪一 tick —— `player_doing` 的第三个来源(见那条 docstring)。
        # 只在 `_chat_prelude` 里写一处,过期靠比大小,所以没有要清的账。
        self._player_chat_tick: dict[str, int] = {}
        # 让世界看得见在场的玩家(issue #13,访客模型)。scheduler 不认识 World,
        # 只认识这个回调;在场以键还在不在为准,所以角色不会去敲断线三小时的人的门。
        self.scheduler._present_players = self._present_roster
        # 以及**他此刻在做什么** —— 规律层的 `action` 选择器读它(见 `player_doing`)。
        self.scheduler._players_doing = self._players_doing_now

        # 开机补完:会话只在 record_chat_turn 一次调用内开与关,所以此刻还 open
        # 的行**要么**是上次崩溃的遗留(补上总结与那一个 conversation 事件即可,
        # 崩溃从"丢总结"降级为"总结晚到"),**要么**是别的进程此刻正说着的话。
        # ⚠️ 这两件事此前不分:注释写的是"运行中的世界独占 db",而那句话随
        # world.db 一起过期了 —— 2.0 之后很多进程可以同时开同一个世界,于是
        # `map` / `prompt` / 运维脚本这些**只读的门**每开一次,就把玩家正说到
        # 一半的会话掐掉一次,还顺手在一个马上要退出的进程里发起关系判定:
        # 会话关了、总结有了、判定永远落不了地。玩家看到的是"她突然不记得刚才
        # 那段了",而全程零报错。**收尾归接得住它的进程**:关一次会话要调一次
        # LLM 生成总结、要把判定交给线程池,一个转身就退出的进程两样都做不到。
        # 读戳必须在盖戳之前 —— 盖完再读读到的是我自己。
        runner = self.scheduler.another_runner()
        # 盖一个"这个世界正被我跑着"的戳。CLAUDE.md 的第一条不变量此前没有任何
        # 标记去支撑 —— 谁也看不出一个 db 正被人跑着,而第二个写它的进程会让两边
        # 立刻分叉。是提示不是锁:进程崩掉标记就陈旧,拿陈旧标记拒绝操作,等于在
        # 真出事那天把人挡在门外。
        self.scheduler.claim_ownership()
        self._install_autonomy()   # 定时轮次挂到时钟上(开关关着时 hook 自己会退出)
        self._install_contact()    # "她想起你"挂到时钟上(同上)

        if runner is not None:
            logger.info(
                "%s 正跑着这个世界,开着的会话交给它收尾", runner,
            )
        else:
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
        `runtime.llm.degraded_reason` 常驻点名 Mock 降级的原因。

        **先补课再快照。** 关系、余额、随身物品、角色卡全是投影折出来的,而别的
        进程可能刚写过一条(维护容器改了张卡、另一个宿主收了笔钱)。跑着的世界会
        在下一次追加事件时自己追上(`_fold_gap_before`),但**只读门不该指望世界
        正好在动** —— 一个暂停的 / 空闲的世界一条事件都不发,那份快照就永远停在
        这个进程开机那一刻,而它看上去完全正常。
        """
        with self.scheduler._lock:
            self.scheduler.catch_up_projection()
        state = self._view.snapshot()
        # **走名册那条路,不要直接誊在场那几行。** 原样誊出来的话,一个正在赶路的人
        # 报的是他的**出发地**,而且一格标记都没有 —— 于是界面把他画在铁匠巷,同一秒
        # `player_options` 正拿一句「你在路上 —— 到了地方就能动手了」把他能干的事全挡
        # 了,一块屏幕的两半互相打脸。名册那条路顺带**先把到站的人放下**,只读门自己
        # 补课(和上面那句 `catch_up_projection` 同一条纪律:暂停的世界不会自愈)。
        state["players"] = self._present_roster()
        state["simulation"] = {
            "paused": self._paused,
            "tick_rate": _resolve_tick_rate(1.0, self.scheduler.config_store),
        }
        for aid, agent_state in state["agents"].items():
            need_values = self.needs(aid)
            if need_values:
                agent_state["needs"] = need_values
        return state

    def roster(self) -> dict[str, Any]:
        """**这个世界里有谁** —— 名字、一句话、立绘、主次、此刻在哪。玩家那一侧的读出口。

        由来是一次真人试玩:线上那个世界 21 个角色,4 个是作者写了几周的主角、
        17 个是背景 NPC,而玩家的通讯录里这 21 个人长得一模一样。作者写得进、
        校验放行、世界跑得动、包也导得出 —— **就是到不了玩家眼前,而全程零报错**。
        顺带补上一个已经在线上咬人的洞:**显示名此前没有读出口**(`map --json`
        的地点有 `name`,人只有 id),于是网站旁白里印的是 `mai:`、`yu:`。

        返回 `{"agents": [ … ]}`,每行**九栏固定**(线格式已冻,运维台
        `world_server.py` 的 `_ROSTER_FIELDS` 照它写):

            agent_id / name / tagline / portrait / billing /
            location / location_name / state / away

        外加第十栏 `card`:作者写的那张卡的**原样**(没写就是 `None`)。它存在是因为
        引擎**不理解**这几格,只原样带过去 —— 创作台已经预告了第四样(声线、主题色、
        CV),而只摊平引擎认得的三格等于在这道门上把它们悄悄扔了。

        三条要知道的:

        - **`billing` 缺省 `supporting`,不是 `lead`。** 猜错方向的代价不对称:
          把主角说成配角只是排版难看,把还没出场的人说成主角是**剧透**。
        - **`hidden` 的人照出。** 引擎是"这个世界里有谁"的权威,它得说得出;
          筛掉是宿主那一层的事(运维台的壳已经在 `/internal/v1/roster` 上做了,
          理由是泄露的边界在进程上、不在浏览器里)。
        - **顺序跟世界自己的名册走**(事件日志的顺序),不按字母重排 —— 重排的话
          同一个世界在两处会给出两种出场顺序。

        名字取不到就**原样给 id**:编一个出来的话,界面上写的是一个这个世界里
        不存在的人。地名同理(`location_name` 查不到就回落地点 id)。

        **先补课再读。** 卡住在投影里(`agent_join` / `persona_update` 折出来的),
        而写它的 `set_card` 常常来自另一个进程 —— 线上那一幕正是这样:维护容器写完
        四张卡、回执写着 `changed=True`,长驻世界里的玩家看到的还是 `card: null`。
        `state()` 那条注释写了为什么只读门自己得补,不能指望世界正好在动。
        """
        loc_names: dict[str, str] = {}
        loc_store = getattr(self.scheduler, "location_store", None)
        if loc_store is not None:
            try:
                for row in loc_store.all():
                    loc_names[str(row.get("id") or "")] = str(row.get("name") or "")
            except Exception:  # noqa: BLE001 - 没有地图不该让名册整个读不出来
                loc_names = {}

        with self.scheduler._lock:
            self.scheduler.catch_up_projection()
            rows = self._view.identity_rows_locked()

        agents: list[dict[str, Any]] = []
        for agent_id, row in rows.items():
            card = row.get("card") or {}
            location = str(row.get("location") or "")
            agents.append({
                "agent_id": agent_id,
                "name": str(row.get("name") or "").strip() or agent_id,
                "tagline": str(card.get("tagline") or ""),
                "portrait": str(card.get("portrait") or ""),
                "billing": billing_of(card),
                "location": location,
                "location_name": loc_names.get(location) or location,
                "state": dict(row.get("state") or {}),
                "away": bool(row.get("away")),
                "card": row.get("card"),
            })
        return {"agents": agents}

    def set_card(
        self,
        agent_id: str,
        card: dict[str, Any] | None = None,
        *,
        clear: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """改**一个已经在这个世界里的人**的角色卡。`roster()` 的写那一侧。

        存在的理由,和 `roster()` 是同一个故事的下半截:卡只在 `agent_join` 的
        `payload.spec` 上,而一个已经在册的角色**永远不会再 join** —— 作者层合并
        (`--world-file`)只给 `newcomers` 发 join,语义又是"只填缺不覆盖"。于是
        角色卡那一整轮改造**对唯一一个有真人的世界等于没做**:线上那 20 个角色
        (4 个是作者写了几周的主角)一张卡都装不进去,而作者写得进、校验放行、
        测试全绿、包也导得出。**照跑但给错东西**,全程零报错。

        四条判断,写在这里而不是散在 CLI 上:

        **一、这是一次明示的编辑,所以它覆盖。** 和作者层合并的"只填缺不覆盖"
        (`_join_authored_additions` / `__main__._join_spec`)**有意相反**,两边的
        docstring 互相点名。理由是两条路问的不是同一件事:那一条手里捏着一份**文件**
        (缺省还是内置橱窗),拿它去覆盖等于把这个世界跑出来的现在倒带回创世那一刻;
        这一条是一个人敲了一行命令指名道姓地说"这个人是主角",让它只填缺的话,
        一个已经写着 `supporting` 的角色**永远**改不成 `lead`。⚠️ **别把其中一条
        "修"成另一条** —— 它们语义相反是对的。

        **二、部分更新合并进现有的卡,不是替换整张卡。** 只给 `tagline` 不许把作者
        写了几周的 `billing` 和立绘顺手抹掉;而"抹掉"在这条路上不会报错,只会让那个
        人下一次刷新时从通讯录第一屏掉下去。要抹掉**某一格**就把它给成空串。
        引擎不认识的键(创作台预告的声线 / 主题色 / CV)照旧原样带过去。

        **三、`clear` 是单独一格,不是"把 billing 设回 supporting"。**
        "作者说他是背景"和"作者什么也没说"是两件事(`_join_spec` 与
        `normalize_card` 的 docstring 为同一个区别写过) —— 前者是一句声明,
        后者是收回声明。给了 `clear` 就不许再给值:两句互相矛盾的话,引擎挑哪句
        都是猜。

        **四、合并后逐字相同就一个字都不写。** 事件溯源里追加一条毫无差别的
        `persona_update` 只是给历史添噪音,而历史是这个世界唯一的真相 —— 噪音进去
        了就再也分不出"这一天作者真的改了主意"。

        校验用的是 `character_card` **那一份**判断(`card_errors`),在写之前,
        对**合并后**的卡跑一遍:另写一套的下场是同一张卡在装载期和这条路上得到
        两个答案。坏卡抛 `ValueError`(一次列全),不认识的人抛 `KeyError`
        **并把这个世界里有谁说出来** —— 编一个空结果出去的话,运维的人会以为改
        成功了,而那正是这一整轮要修的病。两种拒绝都**一个字都不写**。

        走的是 `persona_update` 那条现成的路(和 `repair_agent_goals` 同一形状),
        **不就地改历史**:创世那条 `agent_join` 说的仍然是创世那天的话,投影把新的
        那一条叠上去。**卡不上黑板** —— `tagline` 是写给玩家看的广告词,上了黑板
        她就会照着念。

        返回一份回执:`{agent_id, name, before, after, changed, cleared,
        dry_run, warnings}`。`dry_run=True` 一个字节都不写。
        """
        agent_id = str(agent_id or "").strip()
        if not agent_id:
            raise ValueError("agent_id is required")
        if clear and card:
            raise ValueError(
                "--clear 和 --billing/--tagline/--portrait 不能一起给:"
                "一句是「删掉这张卡」,一句是「这张卡写成这样」——引擎挑哪句都是猜"
            )
        if not clear and not card:
            raise ValueError(
                "什么都没给 —— --billing / --tagline / --portrait / --clear 至少给一个。"
                "一次什么也没改的「成功」读起来和改成功了一模一样"
            )
        if card is not None and not isinstance(card, dict):
            raise ValueError(f"card 必须是一个对象,收到 {type(card).__name__}")

        with self._guard():
            # 别的进程可能刚给同一个人写过一张卡 —— 不先折进来的话,这次合并的
            # 底稿是一份过期的现在,而结果会安静地把对方那一次覆盖掉。
            # **补课和读底稿在同一把 `_lock` 下**:分成两次拿锁的话,两次之间
            # tick 线程能挤进来,而补课本身是写(见 `catch_up_projection`)。
            with self.scheduler._lock:
                self.scheduler.catch_up_projection()
                rows = self._view.identity_rows_locked()
            row = rows.get(agent_id)
            if row is None:
                # **把这个世界里有谁说出来。** 抄错一个 id 最常见的原因是名字记岔了,
                # 而一句「没有这个人」不足以让人自己找回来。
                known = "、".join(list(rows)[:20]) or "(一个人都没有)"
                more = "…" if len(rows) > 20 else ""
                raise KeyError(
                    f"这个世界里没有叫 {agent_id!r} 的角色。有的是:{known}{more}"
                )

            before = normalize_card(row.get("card"))
            if clear:
                after = None
            else:
                merged = dict(before or {})
                merged.update(card or {})
                after = normalize_card(merged)
                # 校验**合并后**的那张卡,而且在写之前 —— 半张合法的卡落库之后,
                # 作者看到的是一个「改成功了」的回执和一个开不了机的世界。
                problems = card_errors(after, label=f"{agent_id} 的卡")
                if problems:
                    raise ValueError("\n".join(problems))

            receipt: dict[str, Any] = {
                "agent_id": agent_id,
                "name": str(row.get("name") or "").strip() or agent_id,
                "before": before,
                "after": after,
                "changed": after != before,
                "cleared": bool(clear),
                "dry_run": bool(dry_run),
                "warnings": card_warnings(after, label=f"{agent_id} 的卡"),
            }
            if not receipt["changed"] or dry_run:
                return receipt

            with self.scheduler._lock:
                # `spec` 里**只写 card 这一格**:`persona_update` 是 `spec.update(…)`,
                # 顺手带上 name/personality 等于拿此刻的黑板去覆盖作者写的人设。
                # 清掉时写 `None` 而不是省略这一格 —— dict.update 没有「删键」,
                # 而读的那一侧 `normalize_card(None)` 本来就读作「没有卡」。
                #
                # **进事件的是另一份拷贝。** `_apply_state_change` 会把这一格原样
                # `update` 进投影,于是事件的 payload 和投影共用同一个 dict;回执
                # 再共用第三次的话,调用方顺手改一下回执就**就地改写了历史在内存
                # 里的样子**(`projection.py` 为这个洞警告过)。
                self._record_and_fan({
                    "type": "state_change",
                    "who": agent_id,
                    "payload": {
                        "kind": "persona_update",
                        "spec": {"card": dict(after) if after else None},
                    },
                })
            self.scheduler.checkpoint()
        return receipt

    def set_location_image(
        self,
        location_id: str,
        images: dict[str, Any] | None = None,
        *,
        clear: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """改**一个已经在这个世界里的地点**的那两张图。`state()` 的写那一侧。

        存在的理由和 `set_card` 是同一个故事的地点版,而且这一次是**明着欠下的**:
        作者层的语义是"只填缺,不覆盖",而合并的粒度是**整个地点行** ——
        `LocationStore.seed_defaults(merge=True)` 按地点 id 整条跳过已有的地点
        (整行合并会把这个世界跑出来的名字和描述倒带回创世那天)。于是拿一份补了
        图的世界文件去编辑一个**已经跑起来的**世界,那两格一个都装不进去:作者
        写得进、`validate world` 放行、包也导得出,**就是到不了玩家眼前**。
        角色卡那一次的形状逐字重演(线上 20 个早就在册的角色一张卡都装不进去),
        区别只在这一次引擎自己先说了出来(`_warn_skipped_location_media`)——
        而一句只在服务器日志里的话不是一扇门。

        **它不发事件,而 `set_card` 发 —— 这条差别是有意的,别"统一"掉。**
        角色卡住在 `agent_join.payload.spec` 上,也就是说它的家本来就是事件日志,
        改它只能追加一条 `persona_update`;地图不是 —— `locations` 表是它**唯一**
        的权威(`projection.py` 为此退役了 `location_desc_update`:地图是配置,
        不是历史)。在这里再发一条事件,等于让"这个地点的图是什么"多出一个日志
        之外的答案,而两个答案分叉的那天没有一处会报错。

        四条判断:

        **一、这是一次明示的编辑,所以它覆盖。** 和作者层合并**有意相反**,理由
        和 `set_card` 逐字相同:那一条手里捏着一份文件(缺省还是内置橱窗),拿它
        覆盖等于把这个世界的现在倒带回创世那一刻;这一条是一个人指名道姓地说
        "这个地方的图换成这张"。

        **二、两格分开合并,给谁改谁。** 只给 `map_image` 不许把作者写了几周的
        `scene_image` 顺手抹掉。`clear=True` 是"两格都抹掉",单独一格,不许和值
        一起给。**"别动这一格"和"抹掉这一格"是两句不同的话,所以有三个答案:**

        | 你给的 | 意思 |
        |---|---|
        | **不给这个键** | 别动这一格 |
        | **空串 `""`** | 抹掉这一格(和 `set_card` 的 `--portrait ''` 同一条约定) |
        | **`None` / 全是空白的字符串** | **拒绝**(`ValueError`) |

        第三行是这一轮补的,理由是**代价不对称**:`None` 在宿主那儿最常见的来源是
        `row.get("img")` 没取到值,一段全空白的字符串最常见的来源是运维台模板里
        一个没展开的变量 —— 把它们读成"抹掉"就是一次**静默删图**,而回执上写着
        "改了"。反过来,拒一次的代价只是调用方补一个字。**"别动"已经有写法了
        (不给这个键),所以 `None` 不必再承担第二种含义。**
        ⚠️ **这条判断在 API 上,不在 CLI 上**:argv 那扇门和 `--*-file` 那扇门因此
        对同一个输入给同一个答案 —— 判断放在 CLI 的话,两扇门迟早分叉,而
        `--map-image '   '` 安静地删掉线上那张图正是它分叉的样子。

        **三、这扇门只写这两格,别的键当场拒绝。** 和角色卡"不认识的键原样带过去"
        **不一样**,而不一样是对的:那一格是作者写给玩家看的一张卡,创作台预告过
        第四样(声线 / 主题色);地点行的其余部分(名字、描述、几何)只有一个合法
        的写入者 —— 作者层。在这里开第二个,就是让"这张地图为什么变成这样"多出
        一个答案。

        **四、逐字相同就一个字都不写**(`changed: false`)。一次什么也没改的"成功"
        读起来和改成功了一模一样,而这条命令最常见的用法是运维照着单子敲一遍。

        校验走 `media.media_uri_errors` **那一份**判断(scheme + 每格 256 KiB,
        数按读出口定),对**合并后**的值、在**写之前**跑 —— 另写一套的下场是同一
        条 URI 在开机时和这条路上得到两个答案。坏值抛 `ValueError`(一次列全),
        不认识的地点抛 `KeyError` **并把这个世界里有哪些地点说出来**(编一个空回执
        出去的话,运维的人会以为改成功了);两种拒绝都**一个字都不写**。

        返回 `{location_id, name, before, after, changed, cleared, dry_run}`。
        `before` / `after` **两格永远都在**(没写的是 `None`)—— 形状和读出口
        `state()` 的 `locations[]` 行一致,于是回执和世界能直接对上;
        `dry_run=True` 一个字节都不写。
        """
        location_id = str(location_id or "").strip()
        if not location_id:
            raise ValueError("location_id is required")
        if clear and images:
            raise ValueError(
                "--clear 和 --map-image/--scene-image 不能一起给:"
                "一句是「这两格都抹掉」,一句是「这一格写成这样」——引擎挑哪句都是猜"
            )
        if not clear and not images:
            raise ValueError(
                "什么都没给 —— --map-image / --scene-image / --clear 至少给一个。"
                "一次什么也没改的「成功」读起来和改成功了一模一样"
            )
        if images is not None and not isinstance(images, dict):
            raise ValueError(f"images 必须是一个对象,收到 {type(images).__name__}")
        unknown = sorted(set(images or {}) - set(LOCATION_IMAGE_KEYS))
        if unknown:
            raise ValueError(
                f"这扇门只写地点的两格图({' / '.join(LOCATION_IMAGE_KEYS)}),"
                f"不认识:{'、'.join(unknown)}。地点的名字、描述、几何只有一个合法的"
                "写入者 —— 作者层;在这里开第二个,「这张地图为什么变成这样」就多出"
                "一个答案"
            )

        store = getattr(self.scheduler, "location_store", None)
        if store is None:
            raise RuntimeError("这个世界没有地图表 —— 没有地点可以配图")

        def _stored(value: Any) -> Any:
            """**库里**那一格读成什么。只归一化,不判对错。

            读的这一侧刻意比写的那一侧宽:历史上写坏的值该由写它的那条路负责,
            在这里翻脸只会让一次对**另一格**的合法编辑也做不成。
            """
            if isinstance(value, str):
                return value.strip() or None
            return value

        with self._guard():
            row = store.get(location_id)
            if row is None:
                # **把这个世界里有哪些地点说出来。** 抄错一个 id 最常见的原因是
                # 名字记岔了,而一句「没有这个地点」不足以让人自己找回来。
                ids = [str(r.get("id") or "") for r in store.all()]
                known = "、".join(ids[:20]) or "(一个地点都没有)"
                more = "…" if len(ids) > 20 else ""
                raise KeyError(
                    f"这个世界里没有叫 {location_id!r} 的地点。有的是:{known}{more}"
                )

            before = {key: _stored(row.get(key)) for key in LOCATION_IMAGE_KEYS}
            problems: list[str] = []
            if clear:
                after: dict[str, Any] = {key: None for key in LOCATION_IMAGE_KEYS}
            else:
                after = dict(before)
                for key, value in (images or {}).items():
                    # **三个输入三个答案**,理由见上面那张表:代价不对称,
                    # 猜错的那一半是安静地删掉线上那张图。
                    if value is None:
                        problems.append(
                            f"地点 {location_id!r}: {key} 给的是 None —— "
                            "「别动这一格」请**不要给这个键**,「抹掉这一格」请给空串。"
                            "None 在宿主那儿最常见的来源是「我这儿没取到值」,"
                            "读成「抹掉」就是一次静默删图"
                        )
                        continue
                    if isinstance(value, str) and value != "" and not value.strip():
                        problems.append(
                            f"地点 {location_id!r}: {key} 全是空白 —— 掐掉两头之后"
                            "什么都不剩。要抹掉这一格请明写空串:一个模板里没展开的"
                            "变量长得就是这样,而把它读成「抹掉」会安静地删掉线上那张图"
                        )
                        continue
                    after[key] = (value.strip() or None) if isinstance(value, str) else value
                for key in LOCATION_IMAGE_KEYS:
                    problems.extend(media_uri_errors(
                        after[key], label=f"地点 {location_id!r}", field=key,
                        max_bytes=LOCATION_IMAGE_MAX_BYTES,
                    ))
            if problems:
                raise ValueError("\n".join(problems))

            receipt: dict[str, Any] = {
                "location_id": location_id,
                "name": str(row.get("name") or "").strip() or location_id,
                "before": dict(before),
                "after": dict(after),
                "changed": after != before,
                "cleared": bool(clear),
                "dry_run": bool(dry_run),
            }
            if not receipt["changed"] or dry_run:
                return receipt

            # **只写变了的那几格。** 整行写回去等于把这一行的其余部分(名字、
            # 描述、几何)也当成这次编辑的内容 —— 而它们此刻可能是别的进程刚写的。
            store.upsert(location_id, **{
                key: after[key] for key in LOCATION_IMAGE_KEYS
                if after[key] != before[key]
            })
        return receipt

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
        role: str = "",   # 空 = 这一路不知道他是谁;字面量 "player" 会当身份写进世界
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
        role: str = "",   # 空 = 这一路不知道他是谁;字面量 "player" 会当身份写进世界
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
            # 身份和名字走同一条:宿主这一轮说了就记下,没说就不动世界记着的那个。
            # 从前这里是紧跟其后的一句 `players[pid].setdefault("role", role)`,而
            # `_touch_player` 建行时**总会**播一个 `role` —— 于是那个 key 永远在,
            # `setdefault` 永远是空操作:**聊天这条路一次都没写进过身份**,而它读起来
            # 像写了。后果不是少写一次,是修不回来:一个先走路、后聊天的玩家(走路
            # 那条路手上没有身份)行里躺着占位的 `player`,此后聊多少轮都改不掉它,
            # 而 `chat_service` 把 `player` 当占位身份整段丢掉 —— 他在这个世界里
            # 从此没有身份,一行日志都不会说。
            role=interlocutor["role"],
        )
        # 他开口了 —— `player_doing` 据此说他这会儿在说话(见那条 docstring)。
        # 落在静音闸**后面**:被拒之门外的那一句不算他在跟谁聊天。
        self._player_chat_tick[player_id] = int(self.scheduler.clock)
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
        `players[pid]["name"]` 的写点只有两个(`_chat_prelude` 与 `player_move`),
        两个都只写宿主亲口传过的 `display_name`;出处仍然是宿主,纪律 3 没有松。
        松掉的是"世界明明记得却装作不知道":宿主第一轮传了「林越」,第二轮没传,
        她当场又不认识他了。

        (`player_move` 是后加的第二个写点,加它的理由是**名字不该等他开口** ——
        见那条 docstring。两个写点都过这个函数正是它们不会分叉的原因。)
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
            outcome["detail"] = {"kind": kind, "value": value}
            # **规则记下了,但绝不顶掉她的话** —— 和正下方"指挥她本人"那条逐字同构。
            # 从前这里是 `handled=True` + 一句「（记下了:… —— …。）」,于是玩家开口
            # 教一条规则,换回来的是一张收条、她一个字都没说。而 `intent.py` 的
            # docstring 早就写明了这两种错的代价不对等:「该 dialogue 判成 narrative,
            # 你正说的话被吞掉,只回一句系统确认。后者更贵」—— 这条正是那个更贵的。
            # 线上现场更难看:灯塔湾那个世界的法律是"一律用英语回答",玩家用中文求
            # 「用中文回答我」,收条说「记下了」,两轮之后她照旧用英语并当面说明自己
            # 不打算照办 —— **收条替世界许了一个世界不会兑现的诺**。规则该不该压过
            # 世界的设定由她那一轮自己权衡(两样都在她的提示词里),不由一张收条替她答。
            outcome["handled"] = False
            outcome["grounding"] = (
                "【刚刚发生的事｜按这个事实说话】"
                f"对方刚刚教了你一条对话规则:{OVERRIDE_KINDS[kind]} —— {value}。"
                "这条**已经记下了**,往后每一轮都会摆在你眼前。"
            )
            return outcome
        if verdict.intent == "narrative_direction":
            directed = self._director.direct(
                agent_id=agent_id, params=verdict.params or {}, player_id=player_id,
            )
            if directed.underspecified:
                # 分类器没把自己的字段填全 —— 这一句回执说的是**我**没读懂,不是
                # 世界不答应,而玩家根本不知道有个分类器。和正上方 style_adjust
                # 少了 kind/value 那一手逐字同构:记一行日志,按对话处理。
                logger.info(
                    "导演指令少了参数(action=%s reason=%s)—— 这一轮退回按对话处理 agent=%s",
                    directed.detail.get("action") or "?",
                    directed.detail.get("reason") or "?",
                    agent_id,
                )
                outcome["intent"] = "dialogue"
                outcome["reason"] = (
                    f"导演指令的参数不全({directed.detail.get('reason')}),按对话处理"
                )
                return outcome
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
            # 指挥**别人**那条路走完了自己那一程:回一句确认,不再 in-character 生成。
            # 后果已经在世界里(她下一次读 world_context 会真的看到那个人在场),
            # 不是提示词里的一句想象。
            if handled:
                # 这一句是**引擎的记账,不是她的台词** —— 不记下来的话它会原样落进
                # 转录、进摘要、进她的长期记忆,和 `_strip_receipt` 里写的那场事故
                # 逐字同构。下面那条路早就登记了,这条一直是同一道闸上的一个洞。
                self._remember_receipt(handled)
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
            self._remember_receipt(receipt)
            yield receipt + "\n"
        topic_block = self.chat_service.refused_topic_block(agent_id, prelude["user_text"])
        if topic_block:
            extra_system.append(topic_block)
        async for token in self._noting_chat_health(self.chat_service.respond(
            agent_id,
            messages[-20:],
            interlocutor_id=player_id,
            interlocutor=prelude["interlocutor"],
            meta=sink,
            extra_system=extra_system,
        )):
            yield token

    async def _noting_chat_health(
        self, stream: AsyncIterator[_T]
    ) -> AsyncIterator[_T]:
        """把这一轮聊天成没成记进健康表 —— **记完照旧往外抛**。

        逮到这条的是一次真试玩:线上灯塔湾的主模型(本机 ollama)没起来,于是每一次
        开口都在 `httpx.ConnectError` 上断掉。而那个世界的 `World.state()` 报的是

            llm: {"mock": false, "degraded_reason": null}   subsystems: {}

        —— 「一切正常」。`_llm_degraded_reason` 答的其实是**配没配**这个问题
        (它只在 Mock 那一支算),而字段名叫 `degraded_reason`、放在三支里都有,
        运维台和宿主界面都当健康读。配好了却打不通的端点因此**报满分**,
        而它一句话都答不出来:照跑、报成功、日志一行不错。

        健康的家本来就在 `subsystems`(「`llm.degraded_reason` 说的是"现在";
        这里说的是"一路上怎么样"」),planner / narrative / relationship_judge 三个
        早就在里面,聊天一直缺席 —— 因为聊天子系统有意和事件核解耦,而
        `note_subsystem` 在 scheduler 上。所以记在这一层:`ChatService` 照旧不认识
        scheduler,而这条路是**所有**玩家聊天的必经之地。

        **不吞**:吞掉就得替她编一句道歉,那句会原样落进转录、进摘要、进她的长期
        记忆(`_strip_receipt` 里那场事故)。宿主接得住这个异常,引擎接不住 ——
        它不知道这个世界的玩家该看到什么。

        分不清"模型不通"和"引擎有 bug"是有意的:`respond()` 自己已经把世界读、
        stance 写、分类器都兜住了(各带一句 docstring),漏到这儿的**几乎只有那次
        LLM 往返**;而 reason 里记的是真实的异常类型与消息,所以万一真漏出个引擎
        bug,健康表上写的仍然是那个 bug 的名字,不是一句猜出来的"模型不通"。
        """
        try:
            async for token in stream:
                yield token
        except Exception as exc:  # noqa: BLE001 - 记一笔再原样抛出去
            self.scheduler.note_subsystem("chat", False, f"{type(exc).__name__}: {exc}")
            raise
        self.scheduler.note_subsystem("chat", True)

    def chat_burst(
        self,
        agent_id: str,
        messages: list[dict[str, str]],
        *,
        player_id: str,
        display_name: str | None = None,
        role: str = "",   # 空 = 这一路不知道他是谁;字面量 "player" 会当身份写进世界
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
        role: str = "",   # 空 = 这一路不知道他是谁;字面量 "player" 会当身份写进世界
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
            self._remember_receipt(receipt)
            yield {"kind": "message", "text": receipt, "meta": dict(sink)}
        topic_block = self.chat_service.refused_topic_block(agent_id, prelude["user_text"])
        if topic_block:
            extra_system.append(topic_block)
        # 连续输出这条路上同一道闸(`_noting_chat_health` 的 docstring):两条路各写
        # 一遍才是常态错法,而只钉一条的话,点亮 `chat.loop.enabled` 的世界模型一挂,
        # 健康表上照旧一片空白。
        async for step in self._noting_chat_health(self.chat_service.autonomous_loop(
            agent_id,
            messages[-20:],
            interlocutor_id=player_id,
            interlocutor=prelude["interlocutor"],
            extra_system=extra_system,
            interrupt_check=interrupt_check,
        )):
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

    def _remember_receipt(self, receipt: str) -> None:
        """记下这一轮**引擎自己说的那句**,好在落转录时把它摘掉(见 `_strip_receipt`)。

        有界:最多留 `_RECEIPT_MEMO_MAX` 句,先进先出 —— 玩家数不封顶,而这是纯内存
        的账。**不按 (她, 他) 索引**:落库那一层手上只有 `conversation_id`,再去反查
        是谁跟谁说话等于每条消息多一次 IO,而回执本身就是一句一模一样都难的长句。
        """
        memo = self._chat_receipts
        memo.pop(receipt, None)          # 重发同一句就挪到队尾,别占两格
        memo[receipt] = True
        while len(memo) > _RECEIPT_MEMO_MAX:
            memo.pop(next(iter(memo)))

    def _strip_receipt(self, role: str, content: str) -> str:
        """把引擎的回执从**她说的话**里摘出去。落转录前的最后一道。

        `chat()` 把「(没有 哈尔滨 这个地方……)」这类回执塞在她的回复前面,而宿主
        原样把整段流当成"她这一轮说的话"交回来 —— 于是那句话作为**她的台词**落进
        转录。线上现场:林迟那条消息的第一行是「(咖啡车的雨棚不能被「站」;它能被
        一起躲会儿雨、端详、补一补)」,下一轮它跟着转录进了邀请判定的提示词、进了
        会话摘要、再进她的长期记忆 —— 他还真照着演了一句「我刚被人纠正过,雨棚不能
        「站」」。**世界的记账不是她的台词。**

        摘的是**引擎自己刚发出去的那几句原文**,不猜形状:凭"括号开头"去删等于把
        她自己写的动作块也删掉。没记过那句就原样落库(跨进程的宿主、或者压根不经
        `chat()` 的调用方)—— 退回今天的行为,不会删错东西。
        """
        if role != "assistant" or not content:
            return content
        for receipt in self._chat_receipts:
            if content.startswith(receipt):
                return content[len(receipt):].lstrip()
        return content

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
                           label: str | None = None, bands: Any = None) -> None:
        """声明某类量角色感知得到哪一档:`self` / `here` / `public` / `hidden`。

        **没声明就是感知不到**(默认 `hidden`)—— 反过来的错不可挽回:一个"暗中的
        恨意"的量若默认公开,角色下一句就说出来了。声明本身就是这一层的开关。

        `bands` 是**可选**的一份 `[[阈值, 词], …]`(阈值升序):她读到的是
        `雨势 瓢泼大雨` 而不是 `雨势 0.8` —— 0.8 的雨算大算小,不给档词就得靠
        LLM 猜,而两个模型猜出两种雨。取**最后一个 `<=` 当前值**的那一档,两头
        封口。**不写就是不分档,行为逐位不变**;写错(不升序 / 空词 / 形状不对)
        **当场拒**。数字仍然原样进 `perception()`,分档只影响她怎么说。
        """
        store = self.scheduler.visibility_store
        if store is None:
            raise ValueError("world-rules needs a persistent world")
        store.declare(owner_kind, key, visibility, label, bands=bands)

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
                        # 分档过的量:她读到的是词,而界面上要显示"作者把这个量
                        # 翻成了什么"只有这里问得到。没分档就是空的。
                        "bands": [[t, w] for t, w in q.bands],
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
                        # 在场的人记不记得住这一下。`None` = 作者没写 = 这一层
                        # 整个缺席(不是 0):**没写**和**写了 0**是两句话,前者
                        # 是"我还没想过",后者是"我想过了,不值一提"。
                        "importance": a.importance,
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
           ⚠️ **同一个函数还不够 —— 喂给它的人也得是真的。** `player_id` 指的人不在
           这个世界里时(默认值、id 抄错了),`self.players.get()` 交回一个空 dict,
           于是这一份提示词照拼不误,而**身份/在场/关系三块全是拿一个陌生人算的**:
           她被告知对方没报过名字、不在她跟前、这是手机私聊,连真玩家都被列进"同场
           角色,不是正在和你说话的人"。它渲染得完美、字数像模像样,只是没有哪一轮
           真的长这样 —— 那正是这条 docstring 第 1 点要防的事,只是从另一扇门进来的。
           所以 `asker` 是**返回值的一部分**:这一份是拿谁算的、那个人世界认不认得。
           **绝不悄悄换一个真玩家顶上** —— 换了就是第二种撒谎。
        2. **它解释缺席**(`absent`)。少一块几乎总比多一块难查:世界照跑、她照说话,
           只是从来没提过那棵树。所以"哪块没出现、为什么"和"哪块出现了"一样是答案。
           缺席理由同样要连着 `asker` 读:陌生人身上「还没有关系行」是真话,而它
           解释的是一个不存在的人。
        3. **它不留副作用。** 不写 `players.last_seen`、不触发意图分类、不进 LLM、
           静音中的角色也照样交出提示词(而 `chat()` 会当场拒)。看,但不碰。

        返回 `{"blocks": [{"label","chars","text"}], "order", "absent", "asker",
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
            "asker": {
                "player_id": player_id,
                # 判据是**这个世界认不认得他**,不是"他有没有位置":没位置的真玩家
                # 只是没落脚,而世界压根不认得的那个人会让上面三块整个换一套算法。
                "known": player_id in self.players,
                "display_name": str(interlocutor.get("display_name") or ""),
                "location": where,
                "location_name": str(interlocutor.get("location_name") or ""),
            },
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
        # 开关管着的块**逐个都要在这儿有一行**。漏一个的下场正是这个视图存在的
        # 理由:那一块凭空消失、零解释,而这份视图的职责就是解释缺席
        # (`persona.anchor` 曾经漏在这张表外)。加了新的开关块就往这里加一行。
        for label, key in (("stance", "chat.stance.enabled"),
                           ("tools", "chat.tools.enabled"),
                           ("persona.anchor", "chat.persona_anchor.enabled")):
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

    def _self_activity_label(
        self, agent_id: str, activity: Mapping[str, Any],
        doing: Mapping[str, str],
    ) -> str:
        """**她读自己此刻在做什么** —— 只此一份措辞。

        `world_context`(她跟人说话时那份)和 `_autonomy_context`(没人跟她说话
        时那份)从前各拼各的:前者认得在途,后者不认 —— 于是同一个正在去后院
        路上的人,一份提示词说「正在去后院的路上」,另一份说「闲着」,而
        **「闲着」正是"可以打扰"的意思**。两处分支不会互相报错,只会让她做出两种
        不同的决定,取决于那一刻是谁在问她。

        ⚠️ 这一处此前是**潜伏的**,不是每天在犯:`_maybe_run_autonomy` 把在途的人
        整个排除在外(「在赶路的人不做别的事」),所以活路上走不到那一支。
        照样收:潜伏的分叉不是没有分叉,它只是在等一个改动把它放出来 ——
        而"唯一一份措辞"这条纪律的全部意义就是不留第二处。
        """
        if activity.get("transit"):
            to_name = self.scheduler.place_name(
                str((activity.get("transit") or {}).get("to") or "")
            )
            return f"正在去{to_name or '别处'}的路上"
        return doing.get(
            self.scheduler.stock_owner_of(agent_id),
            _ACTIVITY_LABELS.get(activity.get("kind"), "闲着"),
        )

    def _autonomy_context(self, agent_id: str, now: Any) -> Any:
        """锁内一次快照(只读、无 LLM、无 IO)。worker 之后只碰这个对象。

        ⚠️ **在场名单里从前只有玩家。** 于是一个站在三个同事中间的角色,提示词第一句
        写的是「这会儿你身边没有别人」,而下面 `interact` 的参数说明正让她用 `with`
        点名跟她一起做的人 —— 名单从来没给过她。晚潮那 11 个标着「得有人一起」的
        动词因此 238 天、161648 条事件里一次都没被发出去过(**2026-08-19 在那个
        世界上量的,是修这条时的实况,不是现状**):机制全在(同意、`joint_gate`、
        共同经历都写好了),缺的只是**她不知道身边有谁**。
        """
        scheduler = self.scheduler
        brain = scheduler.agents.get(agent_id)
        if brain is None:
            return None
        agent = brain.agent
        here = str(agent.blackboard.read("loc") or agent.location or "")
        activity = self._view._agent_activity(agent_id)
        doing = self._activities_now()
        # **她读自己那一句和读别人那一句,是同一句措辞。** 从前这一行走的是
        # `_ACTIVITY_LABELS`(排班那一层的动作名),而同屋的人走 `_activities_now()`
        # (它认得 `:engaged` 那件占着人的长过程)—— 于是同一份提示词里两句话
        # 互相打脸:她读到「你在回声唱片店,闲着」,同一个房间里的人读到
        # 「江晚(在一起听完一面)」,说的是同一个人的同一分钟。
        # **在途那一支也在里面了**(`_self_activity_label`):从前这一行不认得
        # 赶路,于是路上的她在这份上下文里"闲着"。
        label = self._self_activity_label(agent_id, activity, doing)

        def _person(person_id: str, actor: str, name: str, kind: str) -> dict[str, str]:
            """名单上的一个人。

            `person_id` 是她要写进 `with` / `player_id` 的那个 id(`_resolve_party`
            两种都收),`actor` 是同一个人在量那张表里的 id —— **一个人两个命名空间**
            是这个仓库既有的事实(`stock_owner_of`),不是这一波新造的分支。
            `kind` 让宿主与菜单分得清人和玩家;`doing` 那一句与 presence 块、感知块
            共用同一份措辞(`_activities_now`)。
            """
            row = {"id": person_id, "name": name, "kind": kind}
            said = doing.get(scheduler.stock_owner_of(actor))
            if said:
                row["doing"] = said
            return row

        # 同地的角色。`_agent_locations()` 已经跳过在途的人 —— 在途不算在场,只比
        # 地点的话一个正在赶路的人会被判成和她面对面。按 id 排,要的是**确定**。
        present = [
            _person(aid, aid, scheduler.agents[aid].agent.name or aid, "agent")
            for aid, loc in sorted(scheduler._agent_locations().items())
            if loc == here and aid != agent_id
        ]
        present += [
            _person(
                pid,
                f"{scheduler.PLAYER_PREFIX}{pid}",
                str((self.players.get(pid) or {}).get("display_name") or pid),
                "player",
            )
            for pid in self.who_is_present()
            if str((self.players.get(pid) or {}).get("location") or "") == here
        ]
        notes: list[str] = []
        mood = agent.blackboard.read("need.mood")
        if mood is not None:
            notes.append(f"你此刻的心气儿:{float(mood):.2f}(0~1)")
        for person in present:
            # 关系表的键**两边同形**(`_relation_id` 对玩家剥 `player:` 前缀,对角色
            # 是恒等)。走它而不是直接拿 `person["id"]`,是为了名单上哪天换成带前缀
            # 的 id 时这一行不会静默查空 —— 查空不报错,只是她忽然谁都不认识了。
            rel = scheduler._memory_projection.relations.get(
                (agent_id, scheduler._relation_id(person["id"]))
            )
            if rel is not None:
                notes.append(f"{person['name']} 在你眼中:{rel.r_type}")
        # 她**感知到**的世界的量也进决定 —— 否则"矿富了所以我去挖"这种事永远不会
        # 发生:她做决定时看不见世界的任何量,而那正是模拟层和角色层脱节的地方。
        perceived = self._perceive(agent_id, here)
        targets: list[str] = []
        if perceived is not None:
            # 她这儿**能被做点什么**的东西。判据取 `verbs` 而不是"在不在这儿" ——
            # 和 `describe_here` 露不露 id 是同一条:只能看不能碰的东西进不了
            # `interact` 的参数,摆上菜单只会诱她去调一个必然被拒的调用。
            targets = sorted(owner for owner in perceived.here if perceived.verbs.get(owner))
        if perceived is not None and not perceived.is_empty():
            # 三行都走 `Perception` 自己的渲染 —— 自主决定这一路要是另写一遍拼装,
            # 她做决定时看到的世界就和她说话时看到的不是同一个,而两边都能跑、
            # 都不报错。观察窗不许撒谎,这一条同样适用于她自己的决定上下文。
            # (分档就是这么发现的:`{value:g}` 那两行会绕开档词,于是她说"外面
            # 瓢泼大雨",转头按 0.8 做决定。)
            if perceived.own:
                notes.append(f"你自己:{perceived.describe_own()}")
            for owner in sorted(perceived.here):
                notes.append(f"这里的{perceived.describe_here(owner)}")
            if perceived.overflow:
                notes.append(f"这里还有 {perceived.overflow} 样别的东西,你没细看")
            if perceived.public:
                notes.append(f"人人都知道:{perceived.describe_public()}")
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
            targets=targets,
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

    @staticmethod
    def _autonomy_menu(ctx: Any) -> list[Any]:
        """这一轮摆给她的菜单 —— **去掉这会儿不可能成的那几样。**

        菜单原先是整轮算一次的常量,于是提示词刚说完「这会儿你身边没有别人」,
        下面还照样摆着 `reach_out`。线上量出来的样子:63 次问、0 次动作、5 次失败,
        五次全是「她身边没有人」。摆一个必然被拒的选项不只是浪费一次调用 ——
        她挑了、被拒了、而这次失败教不会她任何事(她当时**没有别的选择**)。

        两道闸都是声明出来的,这里不认识任何一个能力的名字:多一个自主能力时,
        它自己说它要什么,而不是回来改这个函数。

        ⚠️ **`requires_colocation` 数的是在场的玩家,不是在场的人。** 那格声明逐字写着
        「这个能力要**玩家**真的在她跟前」,而 AUTONOMY 面上唯一带它的 `reach_out`
        只收玩家 id(处理器上那句是 `present_player_ids`)。`present` 这一轮补进了同地
        的角色,若照单全收去数,一个身边只有三个同事的人就会被摆上 `reach_out` ——
        然后必然收到「这会儿她身边没有人」。那正是这段注释开头那个 bug 本身,只是
        换了个由头。**联合动词不靠这道闸**:`interact` 声明的是 `requires_target_entity`,
        它要的是"手边有东西",而同伴的名字现在从 `present` 里读得到了。
        """
        players_here = [p for p in ctx.present if p.get("kind", "player") == "player"]
        return [
            spec
            for spec in tools_mod.tools_for(ctx.agent_id, surface=tools_mod.AUTONOMY)
            if not (spec.requires_colocation and not players_here)
            and not (spec.requires_target_entity and not ctx.targets)
        ]

    async def _autonomy_round_body(self, contexts: list[Any], day: int) -> None:
        template = (
            self.scheduler.prompt_store.get("autonomy.decide", default=autonomy.DEFAULT_DECIDE_PROMPT)
            if self.scheduler.prompt_store is not None
            else autonomy.DEFAULT_DECIDE_PROMPT
        )
        for ctx in contexts:
            specs = self._autonomy_menu(ctx)
            if not specs:
                # 菜单空了 = 这一轮她怎么选都做不成。问她等于白花一次 LLM,
                # 而且退回额度 —— 上限是"主动几次",被问都算不上。
                self._autonomy_done[(ctx.agent_id, day)] = max(
                    0, self._autonomy_done.get((ctx.agent_id, day), 1) - 1
                )
                continue
            menu = "\n".join(spec.prompt_line() for spec in specs)
            allowed = [spec.id for spec in specs]
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
                    # 地点也是 id —— 「她在 cart 想起了你」和「bai 想起了你」同一种病。
                    "location_name": self.scheduler.place_name(item["location"] or ""),
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

        不给 `player_id` 就是全部(运维/调试用)。返回按 seq 升序。

        ⚠️ **要增量拉取请用 `contact_requests_page()`。** 拿这一条的最后一条 `seq`
        当下次的 `since_seq` 在热闹的世界里会**饿死**:一整窗都是别人的事件时你拿到
        空 list,没有"最后一条",游标一步都推不动,而他自己那条永远排在窗外。

        ⚠️ **引擎不负责送达。** 推送、红点、消息列表归宿主那一层 —— 这里给的是
        一条有据可查的世界事件,`payload.reasons` 里每条由头都带着它的出处。
        """
        return self.contact_requests_page(
            player_id, since_seq=since_seq, limit=limit
        )["events"]

    def contact_requests_page(
        self, player_id: str | None = None, *, since_seq: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        """`contact_requests()` 的**带游标**版本 —— 增量拉取用这一条。

        `{"events", "next_seq", "cursor", "scanned", "total"}`。

        **为什么是姊妹方法而不是给老方法加个关键字。** 返回类型随参数值变化的函数
        读起来是猜谜:调用点上看不出手里是 list 还是 dict,而两者都是真值。
        "只加不改"用加一个门就满足了,不必让老门变形。

        **`cursor` 永远是 int,而且和"这一页有没有他的事"无关** —— 它是这一窗
        **扫过的**最后一条的 seq(一条都没扫到就是传进来的 `since_seq`)。这正是
        修掉饿死的那一格:空页也推得动游标。`scanned` 是这一窗扫了多少条(过滤前),
        宿主照它就能看出"我在替别人翻页"。`next_seq` 仍是"后面还有没有",和
        `history()` 逐字同义。

        于是宿主一次调用拿全:不用为了推游标再查一次库(运维台此前正是这么绕的 ——
        空页时再 `history(kind=)` 一次,多一次全表扫,而且每个宿主都得重新发明)。
        """
        return self._filtered_page(
            kind="agent_wants_contact", player_id=player_id,
            since_seq=since_seq, limit=limit,
        )

    def _filtered_page(
        self, *, kind: str, player_id: str | None, since_seq: int, limit: int
    ) -> dict[str, Any]:
        """"按 kind 取一页、按 player_id 过滤、把游标交出去"。

        **三扇门共用这一份**(`contact_requests_page` / `inbox_page` /
        `invitations_page`):各写一遍的话它们迟早在"空页时游标怎么算"上分叉,
        而那正是这个洞本身。
        """
        page = self.history(since_seq=since_seq, limit=limit, kind=kind)
        scanned = page["events"]
        events = scanned
        if player_id is not None:
            events = [
                e for e in scanned
                if (e.get("payload") or {}).get("player_id") == player_id
            ]
        return {
            "events": events,
            "next_seq": page["next_seq"],
            # 扫过的最后一条 —— **不是过滤后的最后一条**。一条都没扫到就原样退回
            # 传进来的水位(而不是 0):倒退的游标会让宿主把已经拉过的段再拉一遍。
            "cursor": int(scanned[-1]["seq"]) if scanned else int(since_seq),
            "scanned": len(scanned),
            "total": page["total"],
        }

    # ── 有人在等你点头(邀请门)────────────────────────────────────────────
    #
    # **和上面两扇门是三件事,不是一件。** `inbox`(`agent_hail`)的语义写在它自己
    # 的 docstring 里:「敲门不是对话……玩家还没回话,世界里什么也没发生」——
    # **不回也没事**是它的定义。`contact_requests` 的定义是「**只在你不在跟前时
    # 成立**」。而一份邀请:不回她就站在那儿等着,而且一起做事**必须面对面**。
    # 三个前置条件两两互斥,合进一扇门等于把它们变成同一件事,于是玩家分不出
    # "她跟我打了个招呼"、"她隔着半张地图想起我"和"她在等我点头"。
    #
    # 但**翻页的规矩共用那一份**(`_filtered_page`,第三个消费者):各写一遍的话
    # 它们迟早在"空页时游标怎么算"上分叉,而那正是饿死玩家的那个洞。

    def invitations(
        self, player_id: str | None = None, *, since_seq: int = 0, limit: int = 50
    ) -> list[dict[str, Any]]:
        """**有人在等你点头** —— 她开口约过他的那些事件。

        每条多一格 `pending`:这份邀请**此刻**还等不等得到答复。事件本身是历史
        (她当时确实问了),而"还在等吗"是**现在** —— 两者在一条流里读得出,
        宿主才画得出"3 条待回应",而不是把三天前答复过的邀请再亮一次红点。

        ⚠️ **要增量拉取请用 `invitations_page()`**,理由和 `contact_requests()`
        逐字相同(热闹的世界里拿最后一条 seq 当游标会饿死)。

        ⚠️ **引擎不负责送达**,推送/红点归宿主。这里给的是有据可查的世界事件。
        """
        return self.invitations_page(
            player_id, since_seq=since_seq, limit=limit
        )["events"]

    def invitations_page(
        self, player_id: str | None = None, *, since_seq: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        """`invitations()` 的**带游标**版本 —— 增量拉取用这一条。

        `{"events", "next_seq", "cursor", "scanned", "total", "now_tick"}`,
        游标语义和 `contact_requests_page()` / `inbox_page()` 逐字相同
        (共用 `_filtered_page`)。

        **多出来的那一格是时间**:`now_tick` 是这一页取出来那一刻的世界时钟,
        每条事件另带一格 `expires_in`(还剩几个 tick,`pending` 为假时是 0)。
        没有它的话,宿主手里只有事件里那个 `expires_tick` —— 一个绝对刻度,
        要减去"现在"才是人看得懂的"还剩多久",而"现在"得再问一次
        `state()`。两次调用之间世界还在走,于是画出来的倒计时是拿**另一刻**的
        现在减这一刻的到期:平时差一两个 tick 没人看得出,而它偏偏在最后那几秒
        显示成"还有时间",玩家点下去才发现已经过期了。
        **过期按世界时钟判、前端不许自己算**这条契约,少了这一格就等于逼着
        前端自己算。

        🔴 **第二格是结局**(`outcome`,只加不改):不 pending 的行带上它**是怎么
        结束的** —— `INVITE_OUTCOMES` 里的一个,说不上来是空串。少了这一格,
        「她自己把话收回去了」和「你没来得及答」在这扇门上长得一模一样,而消费方
        只能去别处捡那条 `invitation_settled` 事件:那条路上没有游标(壳截最后
        100 条),离线久一点它就滑出去了,那一行于是永远显示成「错过了」——
        **把她做的事记在他头上**,而没有一处报错。这扇门本来就有游标,把结局挂在
        这里等于顺手把那扇门补上。
        ⚠️ 它跟着 `SETTLED_INVITATIONS_KEPT` 那一小段走:太老的行给空串,
        **说不上来就别猜**(消费方读到空串该照旧按 `pending` 显示,别当成某一种)。
        """
        # **只读门自己补课**(和 `state()` / `roster()` 同一条):`pending` 那一格
        # 从投影里读,而别的进程刚落的答复这个进程还没折过。跑着的世界会在下一次
        # 追加时自愈,暂停的不会 —— 而只读门不该指望世界正好在动。
        self.scheduler.catch_up_projection()
        waiting = self.scheduler._memory_projection.invitations
        settled = self.scheduler._memory_projection.settled_invitations
        now_tick = int(self.scheduler.clock)
        page = self._filtered_page(
            kind="agent_invites", player_id=player_id,
            since_seq=since_seq, limit=limit,
        )

        def _left(event: dict[str, Any], pending: bool) -> int:
            if not pending:
                return 0
            try:
                expires = int((event.get("payload") or {}).get("expires_tick") or 0)
            except (TypeError, ValueError):
                return 0
            return max(0, expires - now_tick)

        rows = []
        for e in page["events"]:
            pending = int(e.get("seq") or 0) in waiting
            # **拷一份,不改那条事件**:投影拷一份那条纪律在读侧的样子 ——
            # 就地写一格 `pending` 会让 `World.events()` 里那条事件从此长得和
            # 日志里那条不一样。
            rows.append({
                **e, "pending": pending, "expires_in": _left(e, pending),
                # 还等着的行没有结局(空串),而不是"结局是等着" —— 两件事。
                "outcome": "" if pending else str(
                    settled.get(int(e.get("seq") or 0)) or ""
                ),
            })
        page["events"] = rows
        page["now_tick"] = now_tick
        return page

    def answer_invitation(
        self, player_id: str, invite_seq: int, accept: bool,
    ) -> dict[str, Any]:
        """**他自己按下的那一下。** 这是引擎里唯一一处替玩家点头的入口 ——
        而它拿到的正是他点的那一下。

        `invite_seq` 就是那条 `agent_invites` 的 `seq`(`invitations()` 每行都带)。

        `accept=True`:**在这一刻重查一遍闸,再去做**。决定与执行之间世界还在
        跑 —— 他点头时人可能已经走开了、她可能睡了或者手上有了别的事。查过之后
        才落 `invitation_settled{accepted}`,做不成就落 `expired` 并把原因写进
        `note`:**不许在他不在场时把那件事做掉**。

        ⚠️ **重查的是闸,不是人心。** 同行的人在她开口那一刻已经被问过了
        (`agent_invites` 的 `consented`),这里把那份答复原样带回去,不再问
        第二遍。两个理由,都不是省一次网络:一是**他不该为了按一下"好"去等一次
        模型**;二是再问一遍读的是模型,同一个人同一件事的答案可以和上一次不同 ——
        于是他按了「好」,却因为**别人**这次改了主意被记成 `expired`,而他这辈子
        也不会知道自己那一下点得对不对。她当时点了头,那就是她点过头了。

        `accept=False`:落 `invitation_settled{declined}`,并且**只有这一支**会
        写在关系上、进她的记忆(纯算术,一次模型都不调 —— 红线 3)。他没答而挂到
        ttl 的那一支是**错过**,不是拒绝,一个字都不写。

        返回 `{"ok", "outcome", ...}`。**这扇门只吐得出四种**:`"gone"`(这份邀请
        已经不在了)/ `"declined"` / `"accepted"` / `"expired"`。⚠️ **不是
        `together.INVITE_OUTCOMES` 全集** —— 那个元组里的 `"cancelled"` 是
        **她自己收回**,只有 `Scheduler.cancel_invitations()` 产得出,这扇门一次
        都不会返回它(它照旧会出现在下面那格 `settled` 里:那一格答的是"这份邀请
        上一次是怎么结束的",而她收回正是其中一种)。这里从前写的是"`INVITE_OUTCOMES`
        里的一个",宣称五选一而门只吐四种 —— 下游照着写的那个 `match` 会有一支
        永远进不去,而它看上去像是在防守。**重复答复不报错**:两个设备同时点同一份
        邀请是常态,而第二下不该看到一次异常。

        🔴 **说不出是谁不在场,就是把她做的事记在他头上。** 三格是为这一条加的
        (都**只加不改**):

        | 格 | 什么时候有 | 说什么 |
        |---|---|---|
        | `settled` | `outcome == "gone"` | 那份邀请**真正的**结局(四种里的哪一种) |
        | `absent` | 这件事得当面而没当成 | `agent` / `player` / `both` / `unknown` |
        | `gate` | 闸拦下的 | 闸的名字(`player_not_here` …) |

        `gone` 从前只说得出"要么答过了,要么已经过期" —— 一句**恰好把她自己
        收回去(`cancelled`)排除在外**的话,而那正是四种里唯一不是他的责任的
        那种。`absent` 同理:她按作息表溜达开之后他再按「好」,从前拿到的报文
        和**他**走开时逐字相同(实测),两条路都写着「不在她跟前」。

        ⚠️ **`reason` 一格没动**:闸拦下的仍旧写 `declined`。它是 `act()` 那扇
        门上的既有枚举,改它的含义 = 改跨仓库契约(下游两仓已经按现在这份交过
        活)。要分辨读新加的 `absent` / `gate`。
        """
        pid = str(player_id or "").strip()
        if not pid:
            raise ValueError("player_id is required")
        scheduler = self.scheduler
        row = scheduler.pending_invitation(int(invite_seq))
        if row is None:
            return self._invite_gone(int(invite_seq))
        if str(row.get("player_id") or "") != pid:
            # 替**别人**答应,和引擎替他答应是同一件事的两种写法。
            raise tools_mod.ToolCallError("这份邀请不是给你的")
        if not accept:
            # 关系与记忆焊在 `settle_invitation` 里,不在这儿补 —— 见它的 docstring。
            settled = scheduler.settle_invitation(int(invite_seq), "declined")
            if settled is None:
                # 上面那行和 `pending_invitation()` 之间它有了结局(她收回去了、
                # 或者过了期)。**同一件事只能有一句话**:这里从前另写了一句
                # 光秃秃的"这份邀请已经不在了",于是同一个「没赶上」在两条路上
                # 报出两种形状 —— 一条带 `settled` 说得出是哪一种,一条什么都
                # 不说,而下游没办法知道自己碰上的是哪一条。
                return self._invite_gone(int(invite_seq))
            return {"ok": True, "outcome": "declined",
                    "agent_id": row.get("agent_id"), "text": row.get("text")}
        try:
            outcome = self._tool_runtime._interact_with(
                str(row.get("agent_id") or ""),
                str(row.get("target") or ""), str(row.get("verb") or ""),
                [str(p) for p in (row.get("party") or [])],
                player_id=pid,
                accepted_ids=[
                    f"{scheduler.PLAYER_PREFIX}{pid}",
                    # 她开口那一刻就点过头的人。**闸照查**(`pre_ok` 排在
                    # `invitee.gate` 之后),重查的只是"问"这一步。
                    *[str(c) for c in (row.get("consented") or []) if str(c)],
                ],
            )
        except tools_mod.ToolCallError as exc:
            # 那样东西没了、动词没了 —— 世界在这中间变了。**这不是他的错,
            # 也不是她的拒绝**,所以走"错过"那一支:不落记忆、不动关系。
            scheduler.settle_invitation(int(invite_seq), "expired", note=str(exc))
            return {"ok": False, "outcome": "expired", "reason": "gone",
                    "refusal": str(exc)}
        if outcome.get("ok"):
            scheduler.settle_invitation(int(invite_seq), "accepted")
            return {"ok": True, "outcome": "accepted", **outcome}
        refusal = str(outcome.get("refusal") or "")
        absent = ""
        if str(outcome.get("gate") or "") in together.COLOCATION_GATES:
            # **这一句从前是怪玩家的**:她按作息表溜达开、他一步没动,报文照样
            # 写「你不在她跟前」。人话与枚举一起换掉,两头都点名。
            # 按**整族**闸分支,不按单个闸名 —— 见 `together.COLOCATION_GATES`。
            absent, refusal = self._invite_absence(row, pid)
        scheduler.settle_invitation(int(invite_seq), "expired", note=refusal)
        out = {
            "ok": False, "outcome": "expired",
            "reason": str(outcome.get("reason") or ""), "refusal": refusal,
            **{k: v for k, v in outcome.items()
               if k not in {"ok", "reason", "refusal"}},
        }
        if absent:
            out["absent"] = absent
        return out

    def _invite_gone(self, invite_seq: int) -> dict[str, Any]:
        """他按下去的时候这份邀请已经有了结局 —— **一句话,一种形状**。

        两条路走到这儿(按之前就结了、按的过程中被结了),而它们从前各写各的
        报文。合成一个的理由和 `_INVITE_GONE_LABELS` 同一条:说得出是哪一种
        结局,他手机上那句话才不至于四种情形共用一句。
        """
        settled = self.scheduler.settled_invitation(int(invite_seq))
        out: dict[str, Any] = {
            "ok": False, "outcome": "gone",
            "refusal": _INVITE_GONE_LABELS.get(settled, _INVITE_GONE_UNKNOWN),
        }
        if settled:
            out["settled"] = settled
        return out

    def _invite_absence(self, row: Mapping[str, Any], pid: str) -> tuple[str, str]:
        """他按了「好」而这件事得当面 —— **到底是谁不在场**。回(枚举, 人话)。

        判据是**她开口那一刻在哪**(`row["loc"]`,投影从事件顶层抄下来的那一格)。
        只知道两个人此刻各在哪的话,一句"你们不在一处"说不出是谁动的 —— 而
        「她走开了」和「你走开了」在他手机上是两句完全不同的话:前一句里他什么
        也没做错。和 `_colocation_error` 那张三分表同一条纪律,只是那边分的是
        「怎么办」,这边分的是「怪谁」。

        `unknown` 是**说不上来**,不是"都怪你":她开口那一刻的地点没记下来
        (老事件)、或者世界这会儿不知道他俩里某一个在哪。猜一个出来会让那句话
        读起来完全正常而恰好是反的。

        ⚠️ **"世界不知道他在哪"不等于"宿主没接 `player_move`"**。它至少有三种
        来路:他 `player_leave` 过、在场记录过了 `_PLAYER_TTL_SECONDS`(15 分钟)
        没续上、宿主确实没落过位置。把三种写成一种,就等于对着一个接得好好的
        宿主说它没接 —— 而那句话现在是假的(站点 2026-08-13 前后已接上)。
        """
        runtime = self._tool_runtime
        scheduler = self.scheduler
        agent_id = str(row.get("agent_id") or "")
        name = runtime.agent_names().get(agent_id, agent_id) or agent_id
        here = runtime.agent_location(agent_id)
        where = runtime.player_location(pid)
        here_name = scheduler.place_name(here) or "别处"
        where_name = scheduler.place_name(where) or "别处"
        if agent_id in scheduler._transit:
            # 她在赶路 = 不在任何地方(`_where_is` 同一条)。照 `agent_location`
            # 那份直说会写出"你在 cafe,她在 cafe —— 这件事得当面",一句谎。
            return ("agent", f"{name}这会儿在路上,还没落脚 —— 不是你不在")
        if not here:
            # 世界不知道**她**在哪。落到下面几支的话会写成"她已经离开咖啡店了 ——
            # 她这会儿在别处",一句把"查不到"说成"她走了"的话。
            return ("unknown",
                    f"世界这会儿不知道{name}在哪 —— 不是你没到场。"
                    f"等她落了脚再问一次")
        if not where:
            return ("unknown", _where_unknown_line(name, here_name))
        asked = str(row.get("loc") or "")
        if not asked:
            return ("unknown",
                    f"你们不在一处 —— {name}在{here_name},你在{where_name}")
        asked_name = scheduler.place_name(asked) or "别处"
        if here != asked and where == asked:
            return ("agent",
                    f"{name}已经离开{asked_name}了 —— 她这会儿在{here_name},"
                    f"而你还在{where_name}。是她走开了,不是你没到场")
        if here == asked and where != asked:
            return ("player",
                    f"你已经离开{asked_name}了 —— {name}还在{here_name},"
                    f"而你这会儿在{where_name}。一起做事得当面")
        return ("both",
                f"你们俩都不在{asked_name}了 —— {name}在{here_name},"
                f"你在{where_name}。一起做事得当面")

    def contact_stats(self) -> dict[str, Any]:
        """"她想起你"这条链跑没跑、发没发(contact)。

        和 `autonomy_stats()` / `rule_stats()` 同一个理由:这条路最容易的坏法是
        **看着都对、其实一次没算**。`checked` 是 0 说明 hook 没挂上或者压根没有
        候选;`checked` 不为 0 而 `fired` 是 0,配上 `last` 那句话,就能分清是
        "还不够近"、"没有由头"、还是"她在睡觉"。

        ⚠️ **本次运行内的计数,不是历史**(冷却与次数才落库)。
        """
        return dict(self._contact_stats)

    # ── 一段关系的人话 ─────────────────────────────────────────────────────

    def relationship_summary(self, agent_id: str, other_id: str) -> dict[str, Any]:
        """**她这会儿把这个人当什么** —— 一句话、一个档、和一条出处。

        `state()["relations"]` 给的是四个 -1~1 的浮点数。宿主只有两条路,而两条
        都是坏的:显示出来 = 把一段关系变成一根进度条,而**刷分是恋爱陪伴产品最
        不该长出来的东西**;不显示,玩家聊了两个小时不知道有没有发生过任何事 ——
        而世界里其实发生了。

        所以这一层是**加**出来的:数字一个字不动(还在 `axes` 里,宿主要画什么是
        宿主的事),再给一句人话、一个粗档、和**上一次改变它的是哪一件事**。

        最后那半句是这一层的分量所在。一句"你们更亲近了"如果说不出出处,和一根
        进度条没有区别 —— 玩家学不到"我做了什么让它变的"。所以 `last_change` 带着
        那条事件的 `seq` / `tick` / `delta`,以及(玩家对话判出来的那种)是哪一场
        对话、那场讲了什么。**查不到就明说查不到**(`conversation_id: None`,
        `summary: ""`),不编。

        档走 `memory_triggers.band()` / `BAND_NAMES` —— 和引擎自己认的是**同一个
        函数**。另写一份阈值表的下场是同一段关系在两个地方显示成两档。

        返回 `{agent_id, other_id, agent_name, other_name, exists, met, band,
        band_name, summary, axes:{sentiment,trust,affection,respect},
        last_change}`。`exists` 是 False 时四个轴都是 0.0 —— 那是**没有来往**,
        不是敌意(报成"交恶"的话,一个刚进来的新玩家开局就被讨厌)。

        **`met` 和 `exists` 是两件事,别合并**:`met` 是"这两个人说过话",
        `exists` 是"判定落地了"。判定跑在对话关闭时,所以
        `met=True, exists=False` 是一个真实且常见的中间态 —— 玩家刚聊完那一屏
        看到的就是它。合并的下场见 `relationship_summaries()`。
        """
        return self._relationship_row(agent_id, other_id)

    def _relationship_row(
        self, agent_id: str, other_id: str,
        contact_names: dict[str, str] | None = None,
        met: bool | None = None,
    ) -> dict[str, Any]:
        """一行。`contact_names` 是**一整份名单那条路上传下来的名字表** ——
        逐行去问联系态等于对同一个 hash 做 N 次 HGETALL。`met` 同理:一整份名单
        那条路上一次算完,`None` 就自己去问这一对。"""
        from anima_world.memory_triggers import BAND_NAMES, band as _band

        agent_id, other_id = str(agent_id or ""), str(other_id or "")
        rel = self.scheduler._memory_projection.relations.get((agent_id, other_id))
        if met is None:
            met = self._has_contact(agent_id, other_id)
        met = bool(met) or rel is not None
        last = dict(rel.last_change) if (rel is not None and rel.last_change) else None
        axes = {
            "sentiment": rel.sentiment if rel else 0.0,
            "trust": rel.trust if rel else 0.0,
            "affection": rel.affection if rel else 0.0,
            "respect": rel.respect if rel else 0.0,
        }
        index = _band(axes["sentiment"])
        agent_name = self._party_name(agent_id, (last or {}).get("as_name", ""), contact_names)
        other_name = self._party_name(other_id, (last or {}).get("target_name", ""), contact_names)
        if last is not None:  # 名字是给这一层用的,不往回泄进出处那一格
            last.pop("as_name", None)
            last.pop("target_name", None)
        return {
            "agent_id": agent_id,
            "other_id": other_id,
            "agent_name": agent_name,
            "other_name": other_name,
            "exists": rel is not None,
            "met": met,
            # **没结算的一对不落在档表上。** 四个轴全是 0.0 时 `_band` 给的是
            # 「不远不近」——于是同一行一边说 `exists: False`,一边递出一个档名,而
            # 宿主照着 `band_name` 渲染正是这一格的用途:一个刚进门的新玩家开局
            # 就被每个角色贴上一个档名。空白不是一个档位,它是**还没有值**,所以这儿
            # 给 `None` / `""` 而不是挑一个看上去中性的档。同一条纪律的另一个
            # 落点在 `relationship_judge._closeness_phrase`(0 不念作形同陌路)——
            # 那一处是让**模型**读的,这一处是让**宿主**读的,两边都不能替
            # "还没有来往"编一个值出来。
            "band": index if rel is not None else None,
            "band_name": BAND_NAMES[index] if rel is not None else "",
            "summary": self._relationship_sentence(
                agent_name, other_name, index, rel is not None, last,
                r_type=(rel.r_type if rel else ""), met=met,
            ),
            "axes": axes,
            "last_change": last,
        }

    def relationship_summaries(
        self, *, agent_id: str = "", other_id: str = ""
    ) -> list[dict[str, Any]]:
        """一整份名单 —— "她们几个都怎么看我" / "他跟谁都是什么关系"。

        一次一个地问等于让宿主自己攒,而攒的那一步它得先知道有哪些对 —— 那份名单
        只有引擎有。两个过滤都不给就是全世界的每一对(运维/调试用)。

        每一行的形状和 `relationship_summary()` 逐字相同,按 sentiment 从高到低。

        ⚠️ **名单不只是"已判定的那些对"。** 关系判定跑在**对话关闭**的时候(默认
        静默 600 秒才算关),而玩家聊完就去点关系那一屏。只列已折叠的对,三种状态
        就压成了两种:

            从没来往过    → 没这一行   ← 对
            结算过        → 有一行     ← 对
            来往过没结算  → 没这一行   ← **错,和"从没来往过"长得一模一样**

        他刚跟她聊完一整场,那一屏是空的,于是他学到的是"聊天没有用" —— 而这是
        恋爱陪伴产品里最要紧的一屏。所以联系态里说过话的那些对也进名单,带着
        `met=True, exists=False`,由 `summary` 诚实地说"还没落定"。
        **提前判不是修法**:判定要花一次 LLM 调用,而一场没关的对话本来就还没讲完。
        """
        names, contacted = self._contact_snapshot()
        folded = list(self.scheduler._memory_projection.relations)
        seen = set(folded)
        pairs = folded + [p for p in contacted if p not in seen]
        rows = [
            self._relationship_row(a, b, names, met=(a, b) in contacted)
            for a, b in pairs
            if (not agent_id or a == agent_id) and (not other_id or b == other_id)
        ]
        # 次序按 agent_id 断:没结算的那几行 sentiment 全是 0.0,只按分数排的话
        # 同一个世界每次刷新给出的顺序都不一样 —— 名单跳来跳去和排错了一样难查。
        rows.sort(key=lambda r: (-r["axes"]["sentiment"], r["agent_id"]))
        return rows

    def _has_contact(self, agent_id: str, player_id: str) -> bool:
        """这一对说过话没有 —— 一整份名单那条路走 `_contact_snapshot()`。"""
        store = getattr(self.scheduler, "contact_store", None)
        if store is None:
            return False
        row = store.get(agent_id, player_id)
        return isinstance(row, dict) and row.get("last_contact_tick") is not None

    def _contact_snapshot(self) -> tuple[dict[str, str], dict[tuple[str, str], int]]:
        """联系态读一遍,取两样:`player_id → 名字`,以及**说过话的每一对**。

        **一个人在这张表上有好几行**(跟他来往过的每个角色一行),而人是会改名的 ——
        所以挑的是**最近一次来往**那行上的名字。从前折叠用 `setdefault`,底下是
        `HGETALL`:线上于是出现同一个玩家在「江晚怎么看你」那行叫 player-5688afd1、
        在别处叫刘俊康,而哪个露面全看这次的哈希顺序。带 uuid 的人话和一个浮点数
        一样不能给人看 —— 这条本来就写在 `_party_name` 的 docstring 里。

        判据是 **`last_contact_tick` 在不在**,不是"这一行存不存在":`note_fired`
        (她想起他了)也在这张表上写行,而那种行没有 `player_name` —— 拿它当
        "说过话"会让一串 uuid 出现在玩家的关系屏上,正是上一段修掉的那个病。
        """
        store = getattr(self.scheduler, "contact_store", None)
        if store is None:
            return {}, {}
        # 同一 tick 的两行按 agent_id 断,只为**同一个世界每次给同一个答案**:
        # 一个随读取顺序变的名字和一个错的名字一样难查。
        rows = sorted(
            store.all(),
            key=lambda row: (int(row.get("last_contact_tick") or 0),
                             str(row.get("agent_id") or "")),
        )
        names: dict[str, str] = {}
        contacted: dict[tuple[str, str], int] = {}
        for row in rows:
            aid, pid = row.get("agent_id"), row.get("player_id")
            name, tick = row.get("player_name"), row.get("last_contact_tick")
            if pid and name:
                names[str(pid)] = str(name)
            if aid and pid and tick is not None:
                contacted[(str(aid), str(pid))] = int(tick)
        return names, contacted

    def _party_name(self, party_id: str, recorded: str = "",
                    contact_names: dict[str, str] | None = None) -> str:
        """一段关系的一头叫什么。**带 uuid 的人话和一个浮点数一样不能给人看。**

        名册 → 在场玩家/联系态 → 事件上抄下来的那个名字 → id。事件那一格排在
        后面是有意的:它是**那时候**的名字,而人是会改名的。
        """
        scheduler = self.scheduler
        if party_id in getattr(scheduler, "agents", {}):
            return scheduler.agent_display_name(party_id)
        if contact_names is not None:
            live = str((self.players.get(party_id) or {}).get("display_name") or "").strip()
            name = live or contact_names.get(party_id, "")
        else:
            name = self._departed_player_name(party_id)
        if name and name != party_id:
            return name
        return str(recorded or "").strip() or party_id

    # 每档一句,主语是她。**档变了话没变的话,这一层等于把同一句话印给每一段
    # 关系** —— 而那和一根进度条的区别只剩下它不显示数字。
    _BAND_SENTENCES = (
        "{a}把{b}当仇人",
        "{a}对{b}存着芥蒂",
        "{a}和{b}还谈不上什么交情",
        "{a}把{b}当认识的人",
        "{a}拿{b}当亲近的人",
        "{a}把{b}当最要紧的那几个人之一",
    )

    def _relationship_sentence(
        self, agent_name: str, other_name: str, index: int,
        exists: bool, last: dict[str, Any] | None, *, r_type: str = "",
        met: bool = False,
    ) -> str:
        """人话本身。**一个字的数字都不出现。**"""
        if not exists:
            if met:
                # 不写"刚":判定可能几十个世界日都没跑过,而"刚"是个时间断言。
                return f"{agent_name}和{other_name}说过话了 —— 这一趟来往还没在她心里落定。"
            return f"{agent_name}还不认识{other_name} —— 两个人之间还没有来往。"
        sentence = self._BAND_SENTENCES[index].format(a=agent_name, b=other_name)
        # 作者/判定器给的关系名(「一起长大的邻居」)比引擎的六个档具体得多,
        # 有就带上;`acquaintance` 是 `Relation` 的默认值,不是谁说过的话。
        label = str(r_type or "").strip()
        if label and label != "acquaintance":
            sentence += f"(「{label}」)"
        if not last:
            return sentence + "。"
        moved = {"up": "更近了一步", "down": "又远了一点"}.get(str(last.get("direction")))
        if moved is None:
            return sentence + "。"
        tail = f";上一次两个人的来往让它{moved}"
        if last.get("summary"):
            tail += f" —— 那回是「{last['summary']}」"
        return sentence + tail + "。"

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

        **只有 `act()` 走这一句**(⚠️ 这里从前写着「`act()` 和 `intend()` 共用」,
        第七轮 2026-08-20 自查逮到:`git grep -nE 'self[.]_colocation_error[(]' -- anima_world/`
        只答得出**一行**(⚠️ 用 `[.]` `[(]` 而不是反斜杠转义,两个理由:这行自己
        因此不被数进去;docstring 不是 raw 串,反斜杠转义写在这儿会让**整个包在
        导入时报 SyntaxWarning** —— 这条注意事项本身就是第七轮当场踩出来的,
        全量跑那一行末尾多出来的「1 warning」就是它)。
        `intend()` 那半边是另一件事、另一句话 —— 它在排队时
        就把这种动词整个挡回去(「排不进打算」),而且是**抛 `ValueError`,不是回执**。
        把两条路写成一条,读的人会去 `intend()` 里找一句根本不存在的话)。
        **回执要说得出是三种里的哪一种**:

        | 原因 | 玩家该干什么 |
        |---|---|
        | 你在别处 | 走过去(或者只跟她说话) |
        | 世界不知道你在哪 | 再进一次世界(或者等宿主把落脚处报上来) |
        | 她在赶路 | 等她落脚 |

        合成一句"你不在她跟前"的话,第二种会看起来像是玩家自己站错了地方,
        而他做什么都改不了 —— 那是这个仓库最怕的那种"技术上没错、读起来是谎"。

        ⚠️ **第三行是猜出来的,不是问出来的**(第七轮 2026-08-20 认账,**有意没修**)。
        这一支的条件是 `not here or here == where`,而这个函数从头到尾**没有一处去问
        她在不在赶路** —— 隔壁 `_colocation_gate()` 第一句问的就是那个,这里第一句问的
        是有没有 `player_id`。于是它**猜错的时候会说出世界支持不了的话**:她在
        「咖啡店 → 工作室」的路上而他在别处时,走的是最后那一支,印出来是
        「你在后院,苏晚夏在咖啡店」—— 咖啡店是她的**出发地**
        (`agent_location()` 对在途的人仍报着出发前那个地名,`_colocation_gate` 的
        docstring 里写着同一句)。**他在别处时这句话读起来完全正常,所以没人逮到。**
        逐支的对照表、敲得动的判据、以及"这不是第六轮引入的"那笔账,写在
        `_where_unknown_line()` 的 docstring 里 —— 那是两扇门唯一真的合流的地方,
        账记在合流点上才不会只有一半人读到。修法(改去问 `_colocation_gate` 那四个
        枚举)**有意留到定版之后**,已进看板。

        ⚠️ **第二行从前把原因写死成「宿主没调过 `player_move`」**(3.6.0 第五轮
        改掉,2026-08-20):那一支至少有三种来路 —— 他 `player_leave` 过、在场记录
        过了 `_PLAYER_TTL_SECONDS`(15 分钟)没续上、宿主确实没落过位置。**前两种
        里宿主刚刚才调过**,而那句话对着一个接得好好的宿主说它没接(站点 2026-08-13
        前后已接上)。那一支的句子和 `_invite_absence` 收在**同一个函数**里
        (`_where_unknown_line`):同一件事在两扇门上不该有两种说法,而这两扇门玩家
        都会撞上。

        ⚠️ **第五轮在这儿写下的"逐字同一句"是假的,而且假在两处**(第六轮
        2026-08-20 修):裸 id vs 人话地名(`在 cafe` / `在咖啡店`),外加一个空格。
        **这个病治过一次** —— CHANGELOG **3.0.0** 有一条专门的
        `### Fixed —— 她读到的地名一半是人话、一半是 id`,而它在这个函数里原样长
        回来了,因为改这个函数的人没读全。它不报错、测试也不红,只是出戏,所以专挑
        「改了这个函数但没读全」的轮次复发。**长期判据:凡是拼给玩家/角色看的句子
        里出现地点变量,一律问一句「过 `scheduler.place_name()` 了吗」。**

        ⚠️ **这四句里不出现动词名**(第七轮 2026-08-20 改)。上一轮把 Python 的 `!r`
        换成「」是对的一半 —— 记号对了,而框里那几个字母还是**函数名**:玩家刚刚动手做了
        一件事,回敬他一句「「reach_out」要当面才办得到」。最便宜的正解是这句话根本
        不点动词名:全仓声明 `requires_colocation` 的能力**有且只有一个**(判据:
        `git grep -c 'requires_colocation=True' -- anima_world/tools/` → `social.py:1`),
        所以"是哪件事"这一格里没有歧义可消;将来加了第二个,「这件事」照样对。
        **动词名一个字都没丢**:`act()` 把它原样放在返回值的 `"tool"` 那一格里,
        日志那一行也带着(`logger.info("act(%s, %s) 被拒:…")`),宿主排障照样
        查得到 —— 去掉的只是**玩家读到的那份**。
        也不给它套「」:框的只有**数据里来的那一截**(和 `_PLAYER_FALLBACK_DISPLAY`
        同一条),「这件事」是引擎自己的话。宿主/作者面那几处**一处没动**,判据:

            git grep -cE 'verb.r[}]' -- anima_world/

        今天答 `api.py:4` + `ontology.py:2`。**pattern 拐这个弯是有意的** ——
        写成没拐弯的字面量,这一行自己就会被数进去,判据当场从 4 变 5
        (第七轮真踩过,而且是同一天里第二次);拐弯用 `[}]` 而不是反斜杠,
        理由见 `_colocation_error` 那条同样的注意事项(docstring 不是 raw 串)。
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
            return "这件事要当面才办得到,而这次调用没说是替哪个玩家"
        if self._tool_runtime.face_to_face(agent_id, player_id):
            return ""
        here = self._tool_runtime.agent_location(agent_id)
        where = self._tool_runtime.player_location(player_id)
        name = self._tool_runtime.agent_names().get(agent_id, agent_id)
        # **地名一律过 `place_name()`**:玩家读到的是「咖啡店」,不是 `cafe`。
        here_name = self.scheduler.place_name(here) or "别处"
        if not where:
            # **不指认宿主**(和 `_invite_absence` 那一支共用 `_where_unknown_line`):
            # 这里走到的三种来路里,有两种宿主刚刚才调过 `player_move`。
            return "这件事要当面才办得到,而" + _where_unknown_line(name, here_name)
        if not here or here == where:
            # 两处地名一样却不是面对面 —— 只可能是她在赶路(`face_to_face` 与
            # `_where_is` 同一条:在途即不在任何地方)。照 `agent_location` 那份
            # 直说会写出"你在 cafe,她在 cafe —— 这件事得当面",一句读起来是谎的话。
            # ⚠️ 这句话是**猜**出来的,猜错的样子见 docstring 第三段。
            return f"这件事要当面才办得到,而{name}这会儿在路上,不在任何地方"
        # `where_name` 算在这儿而不是上面:上面两支一个都用不着它(第七轮的顺手项)。
        where_name = self.scheduler.place_name(where) or "别处"
        return (
            f"这件事要当面才办得到 —— 你在{where_name},{name}在{here_name}。"
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
                # 这几项**不在 `act()` 那条调用栈上落地**(判定/叙事跑在别的线程上)。
                # 只说"改哪儿"不说"什么时候",宿主会在返回的那一瞬间去查,
                # 然后随机地读到一个还没变的世界 —— 而且不报错。
                "writes_late": list(spec.writes_late),
                # 它要不要玩家真的在她跟前。界面上这一格决定按钮什么时候可点 ——
                # 点下去才发现的话,那是一次没有任何人预告过的失败。
                "requires_colocation": spec.requires_colocation,
                # 同一件事的另一半:手边得有一样能动的东西。
                "requires_target_entity": spec.requires_target_entity,
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

        ⚠️ **3.2.0 之前玩家的位置是进程内的**,这一段曾经写着"多进程 + 开这道闸的
        部署要先把位置搬进共享存储"。**现在搬完了**(`RedisPlayerPresence`):位置与
        在场落 `anima:{world_id}:player:{pid}`,带 TTL,重启与换进程都还在。
        `location_source` 因此是 `"redis"` —— 那一格从前是**警告**(闸依赖的东西活不过
        一次重启),现在只是元数据。

        名单仍然从**落库的那份**(`contact` 表,她记下过"他上次出现")补齐,因为
        在场是带 TTL 的,而"这个世界跟他打过交道"没有 TTL。两者分开报,而且分得开:

        - `seen_before` 为真、`known` 为假 = **这个世界跟他打过交道,而他这会儿
          不在场**(走了,或者 TTL 过了)。
        """
        runtime = self._tool_runtime
        agents = {aid: runtime.agent_location(aid) for aid in self.scheduler.agents}
        present = set(self.who_is_present())
        # 落库的那份名单:她记下过"他上次出现"的每一个人。在场那份带 TTL,只回答
        # "此刻谁在";打过交道的人不会因为下线就从体检里消失。
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
            # 3.2.0 起真的落库了。这一格留着是因为镜像端在读它 —— 换值比删格好:
            # 删了对面读到缺键,而缺键在 JS 里是 undefined,不报错。
            "location_source": "redis",
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
                # 两格图**写了才出现**(和 `w`/`h` 同一个安排):一张没有图的地图
                # 的 `--json` 因此和从前逐字节相同,而画图的人拿到的是"有没有这一格",
                # 不是一堆 `null`。谁是谁见 `media.LOCATION_IMAGE_GLOSS`。
                for key in LOCATION_IMAGE_KEYS:
                    if row.get(key):
                        entry[key] = str(row[key])
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

    def player_move(
        self, player_id: str, location: str, *, role: str = "",
        display_name: str | None = None,
    ) -> None:
        """玩家移动到某个 point 地点。未知地点抛 KeyError。

        **名字走这条路,不必等他开口。** 此前 `players[pid]["name"]` 只有
        `_chat_prelude` 一个写点,于是一个落了脚还没说话的玩家在世界眼里没有
        名字 —— 而宿主在这一步手上就有它(网站入会表单填的就是它)。后果是
        看得见的:照 `state()["players"]` 渲染的界面上他叫「路人」,同屋的另一个
        玩家看到的是一个身份而不是人名,一直到他说出第一句话为止。

        **和聊天那条路共用 `_interlocutor_for`** —— 名字与称呼怎么落、空着怎么
        回落,两条路各拼一遍就会分叉,而分叉的那一天没有任何地方会报错。
        """
        location = location.strip()
        if not location:
            raise ValueError("location is required")
        if self.scheduler.location_store is not None:
            row = self.scheduler.location_store.get(location)
            if row is None or row.get("kind", "point") != "point":
                raise KeyError(f"没有 {location} 这个地方")
        # 更新而不是整条替换:CLI 每聊一轮都先调一次 player_move,而它手上常常
        # 没有名字 —— 整条替换会把名字冲掉,于是检索又退回不透明 id。
        who = self._interlocutor_for(player_id, display_name, role)
        self._touch_player(
            player_id,
            name=who["display_name"],     # 宿主报过的名字,没报就是空
            display_name=who["address"],  # 给人和模型看的称呼,永不是 id
            role=who["role"],
            location=location,
        )
        self.presence_store.clear_transit(player_id)  # 宿主把他放到哪就是哪,行程作废

    def player_location(self, player_id: str) -> str:
        """玩家这会儿在哪。**在路上就还算在出发地**,到点了当场落地。

        惰性结算:没有哪个循环替玩家跑 tick(角色由 `_land_arrivals` 放下),所以
        "他到了没有"在每次读的时候算。到达的那一次补发 `location_join` ——
        和角色落地发的是同一种事件,宿主不用为人另写一套。

        没调过 `player_move` / `player_walk` 就是空串 = 不在场,引擎不猜
        (和 `face_to_face` 同一条规矩)。
        """
        trip = self.presence_store.get_transit(player_id)
        if trip is not None and int(self.scheduler.clock) >= int(trip["arrive_at"]):
            self.presence_store.clear_transit(player_id)
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
        return str((self.presence_store.get(player_id) or {}).get("location") or "")

    def player_in_transit(self, player_id: str) -> bool:
        """他还在路上吗。先结算一次 —— 否则一个已经到了的人会被报成还在赶路。"""
        self.player_location(player_id)
        return player_id in self._player_transit

    def _player_here(self, player_id: str) -> str:
        """他此刻**站在**哪 —— 在路上就是空串,不是"还在出发地"。

        和 `player_location()` 分工:那一条答的是"他属于哪儿"(`player_walk` 拿它
        算路费、`_present_roster` 拿它排版,两处都**要**在途仍算出发地);这一条
        答的是"他这会儿够得着什么"。`Scheduler._where_is` 是同一个答案的角色那一半,
        它的注释早就把这条写死了 —— 「两处各写一遍的话,迟早一处认为在路上还算在
        原地」。

        而那件事真的发生了:能力那条路问的是 `_where_is`(在途 = 不在),买卖两条
        路问的是 `player_location`(在途 = 还在店里),于是同一个人在同一时刻,
        一扇门说他不在,另一扇门把货卖给了他 —— 他起步之后可以原地刷完全镇的货架,
        而"走过去"那段路费正是这个世界让他掂量的东西。所以玩家这半边的答案也只准
        有这一句,新开的门问它,别在门上再写一遍。
        """
        return "" if self.player_in_transit(player_id) else self.player_location(player_id)

    def player_walk(self, player_id: str, location: str, *, role: str = "") -> dict[str, Any]:
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
            self.presence_store.clear_transit(player_id)
            return {"in_transit": False, "location": location}
        minutes = self.scheduler._travel_minutes(origin, location)
        if minutes is None or minutes <= 0:
            # 量不出来的两点(没有地图)照旧瞬移 —— 与 `_start_journey` 同一条退路
            self._touch_player(player_id, role=role, location=location)
            self.presence_store.clear_transit(player_id)
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

    def player_tools(self, player_id: str = "") -> list[dict[str, Any]]:
        """人在网页上点得动的那些。**和她那份出自同一个注册表** —— 宿主照这个
        画按钮,不用自己维护一份会和引擎分叉的清单。

        两个前置条件都报出来:`requires_colocation` 是"得跟人在一块儿",
        `requires_target_entity` 是"这儿得有样能动的东西"。少报一条,宿主就会
        在一个空屋子里画一个点下去必然失败的按钮 —— 而失败的原因写在引擎里,
        它那侧看不见。

        **说明文字走 `player_description`**(声明里没写就回落 `description`)。
        同一份声明有两个读者:她和一个人。写给她的那一半会指路指到「你此刻感觉到
        的」那个提示词块上,而玩家那一侧根本没有那个东西 —— 于是那句话对一个人
        等于没说。

        给了 `player_id`,`interact` 的 `target` / `verb` 两个参数上会**多出一栏
        `options`**(`{"value","label","available","reason","refusal",…}`),
        来自 `player_options()`。不给的时候**一个字都不变** —— 老调用方照旧,
        而那份也确实没有"此时此地"可言。
        """
        options: dict[str, dict[str, list[dict[str, Any]]]] = {}
        if str(player_id or "").strip():
            options["interact"] = self._interact_options(player_id)
        rows: list[dict[str, Any]] = []
        for spec in tools_mod.tools_for("*", tools_mod.PLAYER):
            schema = spec.params_schema
            extra = options.get(spec.id)
            if extra:
                schema = {
                    name: ({**meta, "options": extra[name]}
                           if name in extra and isinstance(meta, dict) else meta)
                    for name, meta in schema.items()
                }
            rows.append({
                "id": spec.id,
                "kind": spec.kind,
                "description": spec.player_description or spec.description,
                "params_schema": schema,
                "requires_colocation": spec.requires_colocation,
                "requires_target_entity": spec.requires_target_entity,
            })
        return rows

    def _interact_options(self, player_id: str) -> dict[str, list[dict[str, Any]]]:
        """`interact` 那两个参数的选项。**摊平自 `player_options()`**,不另算一遍。

        `verb` 那一栏按动词去重(同一个动词可能挂在这儿的好几样东西上),每条带
        `targets` 说清它对哪几样东西成立 —— 少了这一格,宿主会把一个只对窗成立的
        动词画在一棵树的旁边。
        """
        menu = self.player_options(player_id)
        targets: list[dict[str, Any]] = []
        verbs: dict[str, dict[str, Any]] = {}
        for row in menu["targets"]:
            usable = [v for v in row["verbs"] if v["available"]]
            targets.append({
                "value": row["id"], "label": row["name"], "gloss": row["gloss"],
                "available": bool(usable),
                "verbs": [v["verb"] for v in row["verbs"]],
            })
            for verb in row["verbs"]:
                slot = verbs.setdefault(verb["verb"], {
                    "value": verb["verb"], "label": verb["label"],
                    "available": False, "reason": verb["reason"],
                    "refusal": verb["refusal"], "targets": [],
                    **({"participants": verb["participants"]}
                       if "participants" in verb else {}),
                })
                slot["targets"].append(row["id"])
                if verb["available"]:
                    slot.update(available=True, reason="", refusal="")
        return {"target": targets, "verb": list(verbs.values())}

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
        永久告别是另一件事,见 `forget_player`。
        """
        self.presence_store.forget(player_id)

    def forget_player(
        self, player_id: str, *, reason: str = "", dry_run: bool = False
    ) -> dict[str, Any]:
        """**这个人离开了这个世界。** 和 `player_leave`(只是下线)不是一回事。

        为什么需要它:线上跑了几周的世界里躺着一批早就注销的试玩账号 —— 而她们
        和其中几个"有关系"。那些关系**占着她的联系配额和社交需求**:她会想起一个
        永远不会再出现的人,而这一层每天能想起的人数是有上限的,占掉的那一格是从
        别人那里挪走的。

        **为什么这是一条事件而不是一次删除。** 关系不是一张表,是
        `state_change/sentiment_delta` 折出来的投影;联系态、姿态、静音是世界自己
        写的演化态。**手改一行演化态 = 伪造历史 = 投影和日志对不上,而且没有任何
        地方会报错** —— 而这里更具体:直接删掉投影里那一行,下一次重放(换个进程、
        重启一次、`catch_up_projection` 一次)会原样把它折回来,世界照跑、日志干净,
        "她还惦记着一个不存在的人"这件事一天之内自己长回来。所以做法是往日志里
        **追加一条事实**(`player_departed`),折叠端认它,于是"对账即重放"仍然成立。

        **它不改历史。** 事件、记忆、转录、账本一个字不动 —— 她记得这个人来过。
        走的是**朝前看**的那一半:关系、联系冷却、姿态、静音、回头找你的约、在场。

        **关系有两份记法,两份都要走。** 投影里的 `relations` 是可变的数值,
        `player_departed` 一折就没了;关系图上的 `edges` 是"这两人是朋友"这个
        **事实**,住在自己的表里,而且**只增不减**(`add` 是 INSERT OR IGNORE)。
        折叠端碰不到它 —— 于是告别之后那条 `friendship` 原样挂着,`compute_cliques`
        照着它把一个已经清干净的幽灵算进她的小团体,`World.cliques()` 报得出一个
        不存在的人,没有任何一处会报错。这里显式 `drop()`(那个原语本来就是为撤销
        存在的,关系反转时一直在用),而这么做是安全的:边只在事件**当场落库**那条
        路上写(`_apply_memory_trigger` → `_on_relation_shift`),重放不重建它,
        所以撤掉就是撤掉,不会隔一次重启自己长回来。

        返回一份回执(`{player_id, reason, relations, edges, contact, chat_state,
        dry_run, seq}`)。`dry_run=True` 只数不写。幂等:第二次调返回全 0。
        """
        player_id = str(player_id or "").strip()
        if not player_id:
            raise ValueError("player_id is required")

        scheduler = self.scheduler
        # 先补上别的进程写的 —— 不补的话预览数出来的关系条数和真跑那次对不上,
        # 而"先看一眼"正是这个参数存在的全部理由。
        #
        # **补课在 `scheduler._lock` 下**(`state()` / `act()` 同款,理由写在
        # `catch_up_projection` 的 docstring 里):它是"读水位 → replay → 折 →
        # 写水位"四拍,不在锁下就和 tick 线程折同一段。今天这个窗口是毫秒级的
        # (两条线程抢一台机器),所以一直没被观测到 —— 但宿主一旦把这条路挪进
        # 线程池,窗口就是整整一次 replay 那么长。
        with scheduler._lock:
            scheduler.catch_up_projection()
            relations = sum(
                1 for key in scheduler._memory_projection.relations if player_id in key
            )
        graph = getattr(scheduler, "knowledge_graph", None)
        # **按整个节点比,别按子串。** `aubrey` 是 `aubrey-player` 的子串 ——
        # 子串匹配会把另一个人的边一起撤掉,而两个人的名字长得像是常态。
        node = f"agent:{player_id}"
        edge_rows: list[dict[str, Any]] = []
        if graph is not None:
            edge_rows = [
                row for row in graph.query(include_invalid=True)
                if row.get("subject") == node or row.get("object") == node
            ]
        contact_store = getattr(scheduler, "contact_store", None)
        contact_rows = 0
        if contact_store is not None:
            contact_rows = sum(
                1 for row in contact_store.all() if row.get("player_id") == player_id
            )
        chat_state = getattr(scheduler, "chat_state_store", None) or getattr(
            self, "chat_state", None
        )

        receipt: dict[str, Any] = {
            "player_id": player_id,
            "reason": reason,
            "relations": relations,
            "edges": len(edge_rows),
            "contact": contact_rows,
            "chat_state": 0,
            "dry_run": bool(dry_run),
            "seq": None,
        }
        if dry_run:
            # 数一遍 chat_state 也要不写 —— 而那一层没有"只数"的原语,所以这里
            # 给的是 None(说不出来就别猜一个数字出来)。
            receipt["chat_state"] = None
            return receipt

        # `_guard()` 是**跨进程**那把 RedisLock,挡的是别的进程;`scheduler._lock`
        # 是**进程内**那把 RLock,挡的是自己的 tick 线程。两把各挡一半,谁都不是
        # 谁的替代 —— 这里此前只有前一把。顺序和 `act()` 逐字相同(先 guard 后
        # lock),两处不同序就是一个死锁。
        with self._guard(), scheduler._lock:
            scheduler.catch_up_projection()
            # **先记事实,再清演化态。** 反过来的话,清到一半崩掉就留下一个
            # 没有任何解释的世界 —— 而"为什么变成这样"必须在日志里查得到。
            event = scheduler._record_and_deliver({
                "type": "player_departed",
                "who": None,
                "payload": {
                    "player_id": player_id,
                    "player_name": self._departed_player_name(player_id),
                    "reason": reason,
                },
            })
            receipt["seq"] = (event or {}).get("seq")
            if graph is not None:
                # 拿锁之后重数一遍 —— 上面那次是给 dry_run 用的,而两次之间
                # 别的进程可能又立了一条边。
                dropped = 0
                for row in graph.query(include_invalid=True):
                    if row.get("subject") == node or row.get("object") == node:
                        # **hard=True**:R3 之后 `drop()` 默认是"作废"(写 invalid_at,
                        # 保留历史)—— 那对"他俩绝交了"是对的,对"这个人离开了这个
                        # 世界"是错的。留一行作废的边等于 `cliques` 看不见他、而
                        # `query(as_of=…)` 和导出仍然报得出他,告别就没告干净。
                        if graph.drop(row["subject"], row["predicate"], row["object"],
                                      hard=True):
                            dropped += 1
                receipt["edges"] = dropped
            if contact_store is not None:
                receipt["contact"] = contact_store.forget_player(player_id)
            if chat_state is not None:
                try:
                    receipt["chat_state"] = chat_state.forget_player(player_id)
                except NotImplementedError:
                    receipt["chat_state"] = None
            self.presence_store.forget(player_id)   # 在场与行程同住一行,一起走
        return receipt

    def _departed_player_name(self, player_id: str) -> str:
        """他叫什么 —— 在场名册没了就问联系态那一层(它就是为这个记的名字)。"""
        info = self.players.get(player_id) or {}
        name = str(info.get("display_name") or "").strip()
        if name:
            return name
        store = getattr(self.scheduler, "contact_store", None)
        if store is not None:
            for row in store.all():
                if row.get("player_id") == player_id and row.get("player_name"):
                    return str(row["player_name"])
        return player_id

    def erase_player(
        self, player_id: str, *, reason: str = "", dry_run: bool = False,
        since_seq: int | None = None, limit: int | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        """**法务抹除:把这个人的交互数据从世界里抹掉。** 和 `forget_player`(告别,
        历史一个字不动)是两个动作 —— 这个是用户行使删除权时宿主要调的那一个,
        《拟人化互动办法》第十六条的引擎侧出口。

        内部先走一遍 `forget_player`(朝前看的先断:关系、回访、在场),再动历史:

        - **转录整场删**(两后端,`ChatStore.erase_player`):会话行、消息行、
          逐轮观测量一起走。
        - **由他而起的记忆删行**(`event_seq` 指向涉他事件的);**旁及他的记忆只
          换名字**(别人的反思提了他一句,删整行等于把别人的记忆也抹掉一角)。
        - **事件不删行,原地改写**:`seq` 在 Redis 后端是列表下标,删一行后面全错位,
          「对账即重放」当场碎掉。改写做两件事:他的显示名全域换成「(已注销)」,
          涉他事件的原文字段(`_ERASE_TEXT_KEYS`)抹成「(已抹除)」。
        - **不透明 id 保留,不换假名。** 换假名曾是第一版设计,被跨进程折叠否决:
          落后的进程折了真名 delta、再折到假名 `player_departed`,真名关系成了
          没人清得掉的幽灵 —— 而假名映射一旦落库(哪怕落在事件里)就等于没抹。
          保留 id 则折叠语义零改动、无竞态;id 与人的关联在宿主的账号表里,
          账号一删它就只是一串指向虚空的字符。**宿主应以不透明 id 作 player_id**,
          拿邮箱/昵称当 id 的宿主要自己承担这一条的后果。
        - **账本不动**(钱、物品):守恒不许破,那是世界的账,不是他的话。

        三条边界,都是明说的:**名字太短(单字)或与某个角色重名的不替换**(替换
        会把她们的名字和世界的文本一起绞碎,回执里的 `names_skipped` 数着);
        **别的进程的内存事件窗口(≤200 条)不归这里管**,重启或滑出后消失;
        **抹除后落库的新事件不在扫描范围里** —— 先让宿主停掉他的会话再抹,
        或者过后再跑一次(**幂等**:第二次只会数出 0)。

        ⚠️ **`dry_run=True` 不是"数一下",它是 O(全量事件) 的两遍全表扫描 ——
        绝不许在 event loop / tick 线程上同步调。** 名字要先收齐(第一遍),才知道
        该拿什么去比对(第二遍),而后一遍必须看每一条:他的名字可能出现在任何一条
        事件的正文里。这个世界有多少条事件,它就读多少条,**没有上限**,也没有
        `limit` 可以给 —— "抹干净"和"只看前一页"是互斥的。一个跑了两天的世界就是
        十几万条,一个月两百万条;宿主把它挂在一个请求上、挂在时钟那条线程上,
        世界就停在那儿,而它不会报错,只会像是卡住了。(和 `player_engagement`
        那句 ⚠️ 同一族:**那一条是一次 SELECT,这一条是整本账。**)真跑另外还有
        一遍写。⚠️ 这条曲线 2026-08-19 从平方掰回了线性(`RedisEventLog.page`
        从前不把 `limit` 交给 `LRANGE`),**但线性不是常数** —— 上面那句一个字
        都没松,别拿那个倍数去换掉宿主那侧的墙。

        **这条曲线的长期解是给事件按 `player_id` 建一条索引**,让抹除只碰涉他的
        那几条;它动 `storage` 契约、要给老世界回填、镜像端要跟,所以不在这一轮里。
        在那之前,宿主的做法只有一个:**把它放到请求之外去跑**(一次性子进程 /
        后台作业),别指望引擎这一侧把它变便宜——常数项砍得再多,它还是 O(全量事件)。

        ## 可续与分片(3.5.0):`since_seq` / `limit` / `resume`

        ⚠️ **先说它不做什么,免得照着一个假承诺去设计宿主**:分片分的是**改写
        那一遍**,收名字那一遍(`_erase_survey`)永远 O(全量事件),而且
        `World.open` 那次重放本来就是这条路上最贵的一段。所以**分片不会让每一发
        请求变快** —— 让抹除门答得出话的是宿主那侧的异步作业,不是这几个参数。

        它买到的是另外两样,而**第一样是正确性,不是性能**:

        1. **一趟被杀在半路之后还能续,而且名字不丢。** 3.4.0 及更早有一个不可逆的
           死角:改写从低 seq 往高 seq 走,而名字的来源之一就是日志自己
           (`*_id`/`*_name` 配对)。半路被杀之后低 seq 那半的配对已经是
           「(已注销)」,`forget_player` 又早把在场与联系态清了 —— 重跑的第一遍
           **收不到他的名字**,于是尾巴上那些只在自由文本里提过他的句子
           **再也抹不掉**,一处不报错。3.5.0 把解析好的名字与水位在动日志的第一个
           字节**之前**落进一个带 TTL 的进度键(`anima:{world_id}:erasure:{pid}`,
           `contract --json` 的 `storage.volatile_keys` 里有它,**打包时跳过** ——
           它装着正要被抹掉的那些名字),续跑一律读它,绝不重新推断。
        2. **一次调用的写入量有上限。** `limit` 封住这一趟看多少条,被杀最多丢一片。

        - `limit=K` —— 这一趟最多看 K 条事件(**数的是条数,不是 seq 跨度**)。
        - `since_seq=N` —— 从 seq > N 处接着看。真跑时**不许越过已完成的水位**
          (越过去就是在日志里留一个洞,而下一趟从更高的水位接着做,没有一处
          会报错)—— 越了当场 `ValueError`。预演不受这条管:它不写,造不出洞。
        - `resume=True` —— **只把没做完的那趟做完**;没有未完成的就什么都不做
          (回执各格全 0、`phase="not_started"`),**绝不顺手开一趟新的**。
          不带它的普通重跑照旧会自动续上(进度键在就接着做),所以宿主的重试
          循环什么都不用改。

        回执多两格,**它们答的是两个不同的问题**:

        - `phase` —— **这个人在这个世界里的抹除处在哪一步**:`not_started`(没有
          未完成的)/ `partial`(有一趟停在半路)/ `done`(这一趟走到了日志尽头,
          审计事件已写、进度键已删)。⚠️ 它**不是**"他被抹干净了没有" —— 后者看
          计数。一次**预演**也答得出 `partial`,而那正是宿主今天问不出来、只能把
          "被墙挡在门外"和"抹到一半"混成同一个 503 的那一格。
        - `resume_seq` —— 还没看完时下一趟从哪儿接着看;看到头了是 `None`。

        返回回执 `{player_id, reason, forget, events, conversations, messages,
        memories_dropped, memories_redacted, names, names_skipped, dry_run, seq,
        resume_seq, phase}`;`seq` 有值 = 审计事件写下了 = 这趟走到了尽头。
        `dry_run=True` 一个字节都不写(所以数不出 `forget.chat_state`,并且比真跑
        少数一条 —— 真跑会把 `forget` 刚追加的那条 `player_departed` 里的名字也抹掉);
        预演**读**进度键但不写它。真跑的计数跨片累加,预演只数这一趟看过的窗口。
        转录与记忆**在第一趟就删掉,而且早于那个长循环**(它们便宜、有界,又是这条
        链上最私密的一份);续跑不重做它们,计数从进度键里带过来 —— 所以**续跑那次
        回执里的 `forget` 是第一片那次的原件**,不是重新数出来的。
        CLI 出口:`anima-world player erase`(不带 `--yes` 只数)。
        """
        pid = str(player_id or "").strip()
        if not pid:
            raise ValueError("player_id is required")
        if limit is not None and int(limit) < 1:
            raise ValueError("limit 至少是 1")
        if since_seq is not None and int(since_seq) < 0:
            raise ValueError("since_seq 不能是负数")
        scheduler = self.scheduler
        log = scheduler.event_log
        progress = self.erasure_progress
        carried = progress.load(pid)

        # ── 上一趟没做完:名字与水位从**进度键**来,不从日志来 ─────────────────
        # 这是那个键存在的全部理由,而它防的不是慢,是一个不可逆的死角:改写从低
        # seq 往高 seq 走,而名字的来源之一就是日志自己(`*_id`/`*_name` 配对);
        # 被杀在半路之后低 seq 那半的配对已经是「(已注销)」,重跑的第一遍**收不到
        # 他的名字**,于是尾巴上那些只在自由文本里提过他的句子再也抹不掉,
        # 而且一处不报错。所以续跑绝不重新推断名字。
        # **拒绝必须零副作用,所以水位校验排在任何一次写之前。**
        # 这一行曾经排在 `forget_player` **后面**:一条被拒的命令 rc=2、stdout 零字节,
        # 而世界已经被改了(在场与联系态清掉、日志多一条 `player_departed`)——
        # 连敲三次 `max_seq` 54→57。一次拒绝在调用方那儿的意思是"什么都没发生",
        # 这是这条路上最容易被信以为真的一句话。校验读进度键(只读),不写。
        cursor = int((carried or {}).get("cursor") or 0)
        if not dry_run and since_seq is not None and int(since_seq) > cursor:
            raise ValueError(
                f"since_seq={int(since_seq)} 越过了这趟抹除已完成的水位 {cursor}:"
                "中间那一段再也不会有人回来抹,而且一处不报错"
            )

        if carried is not None:
            names = {str(n) for n in (carried.get("names") or [])}
            skipped = [str(n) for n in (carried.get("skipped") or [])]
            his_seqs = {int(s) for s in (carried.get("seqs") or [])}
            scanned_through = int(carried.get("scanned_through") or 0)
            carried_counts = dict(carried.get("counts") or {})
            forget_receipt = carried.get("forget")
            reason = reason or str(carried.get("reason") or "")
        elif resume:
            # `--resume` 是"只把没做完的那趟做完"。没有未完成的就什么都不做,
            # **绝不顺手开一趟新的** —— 那正是操作者以为自己在续、其实在从头抹
            # 的样子,而从头抹会重跑一遍 O(全量) 并再写一条审计事件。
            return {
                "player_id": pid, "reason": reason, "forget": None,
                "events": 0, "conversations": 0, "messages": 0,
                "memories_dropped": 0, "memories_redacted": 0,
                "names": 0, "names_skipped": 0,
                "dry_run": bool(dry_run), "seq": None,
                "resume_seq": None, "phase": _ERASE_PHASE_NOT_STARTED,
            }
        else:
            names, skipped, his_seqs, scanned_through = self._erase_survey(pid)
            carried_counts = {}
            forget_receipt = self.forget_player(pid, reason=reason, dry_run=dry_run)

        # 长名先换:「小明哥」比「小明」先替,免得替完短的把长的拆成两截。
        replacements = {n: _ERASED_NAME for n in sorted(names, key=len, reverse=True)}

        # 真跑的计数**跨片累加**(回执要给出整趟活的总数);预演只数这一趟看过的
        # 那个窗口 —— 把已完成的计数加进一次预演,给出的数既不是"还剩多少"
        # 也不是"已经抹了多少"。
        base = {} if dry_run else carried_counts
        receipt: dict[str, Any] = {
            "player_id": pid, "reason": reason,
            "forget": forget_receipt,
            "events": int(base.get("events") or 0),
            "conversations": int(base.get("conversations") or 0),
            "messages": int(base.get("messages") or 0),
            "memories_dropped": int(base.get("memories_dropped") or 0),
            "memories_redacted": int(base.get("memories_redacted") or 0),
            "names": len(names), "names_skipped": len(skipped),
            "dry_run": bool(dry_run), "seq": None,
            "resume_seq": None, "phase": _ERASE_PHASE_NOT_STARTED,
        }

        def _counts() -> dict[str, int]:
            return {k: receipt[k] for k in _ERASE_COUNT_KEYS}

        # ── 历史:改写,不删行 ─────────────────────────────────────────────
        # 两遍扫描是语义上必需的:名字要先收齐(第一遍)才知道拿什么去比对,
        # 而名字可能出现在**任何一条**事件的正文里,包括收到它之前的那些 ——
        # 所以合不成一遍。能省的只有常数:
        # ① 第一遍已经对 seq ≤ `scanned_through` 的每一条判过 `about` 了,
        #    直接查 `his_seqs`(载荷这期间没人动:改写它的只有抹除自己);
        #    比它新的照旧现判 —— 那是 `forget_player` 刚追加的那条、以及别的进程
        #    在这两遍之间写下的,漏判它们等于把抹除的边界往回缩。
        # ② `dry_run` 只要那个 bool,别为它把每一层 dict/list 重建一遍。
        # ③ 什么都改不动时连日志都不用翻(没有名字要替、也没有一条涉他)——
        #    只有比第一遍更新的那几条还得看。
        nothing_to_change = not replacements and not his_seqs
        floor = scanned_through if nothing_to_change else 0
        start_at = max(cursor if since_seq is None else int(since_seq), floor)

        def _full_progress_row(at: int) -> dict[str, Any]:
            return {
                "names": sorted(names), "skipped": list(skipped),
                "seqs": sorted(his_seqs), "scanned_through": scanned_through,
                "cursor": int(at), "counts": _counts(), "forget": forget_receipt,
                "reason": reason, "started_at": time.time(),
            }

        # **名字先落盘,再动日志的第一个字节。** 顺序反过来就是上面那个死角。
        live = not dry_run and not nothing_to_change
        if live and carried is None:
            progress.save(pid, _full_progress_row(0))

        # ── 转录与记忆:**第一趟就做完,而且早于那个长循环** ────────────────────
        # 它们便宜、有界,而且是这条链上最私密的那一份。放在改写循环**后面**的话,
        # 第一片被杀在循环里就留下"转录一条没动";放在前面,被杀留下的是"最要紧的
        # 已经没了,剩下的是改写"。代价不对称,所以顺序不对称。
        # 续跑不重做(计数从进度键里带过来),否则同一批会被数第二遍。
        if dry_run or carried is None:
            chat = getattr(self, "chat_store", None)
            if chat is not None and hasattr(chat, "erase_player"):
                wiped = chat.erase_player(pid, dry_run=dry_run)
                receipt["conversations"] = wiped["conversations"]
                receipt["messages"] = wiped["messages"]
            memory = scheduler.memory_store
            if memory is not None and hasattr(memory, "erase_for_event_seqs"):
                receipt["memories_dropped"] = memory.erase_for_event_seqs(
                    his_seqs, dry_run=dry_run)
                receipt["memories_redacted"] = memory.redact_summaries(
                    replacements, dry_run=dry_run)

        examined = 0
        resume_from = start_at
        exhausted = True
        if log is not None:
            for e in _iter_event_log(log, since=start_at):
                if limit is not None and examined >= int(limit):
                    exhausted = False
                    break
                examined += 1
                seq = int(e.seq or 0)
                if seq <= scanned_through:
                    about = seq in his_seqs
                else:
                    about = e.who == pid or _mentions_pid(e.payload, pid)
                if about or replacements:
                    if dry_run:
                        if _erase_probe(e.payload, replacements, blank=about):
                            receipt["events"] += 1
                    else:
                        payload, changed = _erase_scrub(
                            e.payload, replacements, blank=about)
                        if changed:
                            receipt["events"] += 1
                            log.rewrite(seq, {
                                "ts": e.ts, "type": e.type, "who": e.who,
                                "loc": e.loc, "payload": payload,
                            })
                resume_from = seq
                # **先改写、后挪水位**,和 `_projection_seq` 同一条纪律:任何一处
                # 把水位推过没做过的事件,那几条再也补不回来。落盘按批 —— 每条一次
                # Redis 往返是这个仓库明说过的反面教材。
                if live and examined % _ERASE_CURSOR_EVERY == 0:
                    progress.save(pid, {"cursor": resume_from, "counts": _counts()})

        # `phase` 说的是**这个人在这个世界里的抹除处在哪一步**,不是"这一趟做了
        # 什么",也不是"他被抹干净了没有"(后者看计数)。所以一次预演也答得出
        # 「上一趟死在半路了」—— 而那正是宿主最需要、今天却问不出来的一格。
        #
        # ⚠️ **两条硬不变量,宿主按 `phase` 分支时靠它们**:
        # ① `partial` ⇔ 进度键在(**唯一的判据只有一个**,不是两处各判各的);
        # ② `not_started` ⇒ 这一趟一个字都没写,而且 `resume_seq` 必然是 `None`。
        # 从前不是这样:一趟真跑在"没什么可抹 + `--limit` 截断"时会报
        # `{"phase": "not_started", "resume_seq": 55}` —— 而那一趟已经跑过
        # `forget_player` 了。宿主照着 `not_started` 判"还没开始",照着 `resume_seq`
        # 去续,而 `--resume` 又回它"没有未完成的":三句话互相打架,没有一句是对的。
        if dry_run:
            # 预演不写,所以它报的永远是**世界**的状态,和这一趟翻到哪儿无关。
            receipt["phase"] = (
                _ERASE_PHASE_PARTIAL if carried is not None else _ERASE_PHASE_NOT_STARTED
            )
            if not exhausted and carried is not None:
                receipt["resume_seq"] = resume_from
            return receipt

        if not exhausted:
            # 这一片做完了,日志还没到头:**不写审计事件**(它的意思是"抹完了"),
            # 进度键留着,回执告诉宿主从哪儿接着做。
            #
            # **真跑截断了就一定留下一个进度键**,哪怕这一趟什么都不用改
            # (`nothing_to_change`,于是循环前那次没建)。不建的话 `--resume`
            # 会回"没有未完成的",而 `phase` 说 `partial` —— 上面那条不变量①
            # 正是为了不许出现这种两处各判各的。
            progress.save(pid, (
                {"cursor": resume_from, "counts": _counts()} if live
                else _full_progress_row(resume_from)
            ))
            receipt["resume_seq"] = resume_from
            receipt["phase"] = _ERASE_PHASE_PARTIAL
            self._erase_recent_window(pid, replacements)
            return receipt

        # ── 记下这件事本身(审计):载荷里只有 id 和数目,没有任何名字 ─────────
        # 两把锁都要(理由同 `forget_player`):`_guard()` 挡别的进程,
        # `scheduler._lock` 挡自己的 tick 线程。
        with self._guard(), scheduler._lock:
            scheduler.catch_up_projection()
            event = scheduler._record_and_deliver({
                "type": "player_erased",
                "who": None,
                "payload": {
                    "player_id": pid, "reason": reason,
                    "events": receipt["events"],
                    "conversations": receipt["conversations"],
                    "messages": receipt["messages"],
                    "memories_dropped": receipt["memories_dropped"],
                    "memories_redacted": receipt["memories_redacted"],
                },
            })
            receipt["seq"] = (event or {}).get("seq")
        # 审计写成了才删进度键:反过来的话,审计那一步炸掉就再也没人知道这趟
        # 抹到哪儿了,而名字也跟着没了。
        progress.clear(pid)
        receipt["phase"] = _ERASE_PHASE_DONE

        self._erase_recent_window(pid, replacements)
        return receipt

    def _erase_survey(
        self, pid: str
    ) -> tuple[set[str], list[str], set[int], int]:
        """抹除的**第一遍**:他叫过什么、哪些事件涉他、扫到了哪一条。

        ⚠️ **它永远是 O(全量事件),分片一格都分不动它。** 判据是保守面:他的名字
        可能出现在**任何一条**事件的自由文本里,而那种句子不带他的 id —— 任何
        按 `player_id` 建的索引都覆盖不到它们(那正是 D10 不是完整解的原因)。
        想让这一遍变便宜,只有放弃"自由文本里的名字也抹"这条,而那是法务范围的
        判断,不是引擎的判断。

        **必须早于 `forget_player`**:联系态与在场是名字的两个来源,forget 会清掉
        它们。这个顺序踩过一次,写在这里免得下一个人把两句挪反。
        """
        scheduler = self.scheduler
        log = scheduler.event_log
        names: set[str] = set()
        info = self.players.get(pid) or {}
        display = str(info.get("display_name") or "").strip()
        if display:
            names.add(display)
        contact_store = getattr(scheduler, "contact_store", None)
        if contact_store is not None:
            for row in contact_store.all():
                if row.get("player_id") == pid and row.get("player_name"):
                    names.add(str(row["player_name"]).strip())
        his_seqs: set[int] = set()
        scanned_through = 0
        if log is not None:
            for e in _iter_event_log(log):
                seq = int(e.seq or 0)
                if seq > scanned_through:
                    scanned_through = seq
                if e.who == pid or _mentions_pid(e.payload, pid):
                    his_seqs.add(seq)
                    _collect_player_names(e.payload, pid, names)
        # 占位符不是名字 —— 不滤掉的话第二次跑会把「(已注销)」当成他的又一个
        # 显示名收进来,幂等性从回执上看就破了(names 永远数出 1)。
        # **他的 id 同样不是名字**,而且这一条要连着 `names_skipped` 一起滤:
        # `forget_player` 写下的那条 `player_departed` 在联系态被清掉之后把
        # `player_name` 兜底成 id(名字问不出来了),下一轮扫描于是把 id 当成他的
        # 一个显示名收进来 —— 上一版只把它滤出 `names`、却让它落进 `skipped`,
        # 于是第三次起 `names_skipped` 永远是 1。**数据是对的,坏的是回执**,
        # 而宿主拿"再跑一遍各格全 0"写合规断言(文档承诺过)会当场红。
        # id 本来也绝不该被替换:它是不透明 id,替它等于把载荷里的 `player_id`
        # 一起绞碎(§2.9.10.1「不换假名」那一条)。
        names = {n for n in names if n and n != _ERASED_NAME and n != pid}
        agent_names = {b.agent.name for b in scheduler.agents.values()}
        skipped = sorted(n for n in names if len(n) < 2 or n in agent_names)
        names -= set(skipped)
        return names, skipped, his_seqs, scanned_through

    def _erase_recent_window(self, pid: str, replacements: dict[str, str]) -> None:
        """本进程的内存事件窗口跟着改 —— 只读门不该还端着抹掉之前的原文。"""
        scheduler = self.scheduler
        with scheduler._lock:
            window = scheduler.recent_events
            for i, ev in enumerate(window):
                about = ev.get("who") == pid or _mentions_pid(ev, pid)
                fresh, changed = _erase_scrub(dict(ev), replacements, blank=about)
                if changed:
                    window[i] = fresh

    def player_engagement(self, player_id: str) -> dict[str, Any]:
        """**他跟这个世界处得有多深** —— 依赖预警要的那笔账(E2)。

        《人工智能拟人化互动服务管理暂行办法》第十条要求服务方具备"过度依赖风险
        预警、情感边界引导"的能力。判断和干预是宿主的事(引擎不触达用户),
        但**判断要的原始数据在世界里**,而宿主今天只能自己去 join 三张表:
        会话在转录里、关系在投影里、"上次想起他"在联系态里。

        所以这一层是**聚合出口,不是评分**。它给数,不给结论 ——
        `relationship_summary` 那条纪律在这里同样成立:一个"依赖指数"会被产品做成
        进度条,而**刷分是这类产品最不该长出来的东西**。宿主要什么阈值、怎么提示,
        是宿主的判断。

        返回 `{player_id, conversations, messages, agents, first_seen, last_seen,
        span_days, relationships:[…], closest, contacts}`:

        - `conversations` / `messages` —— 他和这个世界说过多少话(墙钟,按秒记)
        - `agents` —— 他和几个角色说过话。**1 和 5 是两种依赖**,合成一个数就分不开了
        - `span_days` —— 从第一次到最近一次跨了多少天(墙钟)
        - `relationships` —— 每个角色对他的那一行(`relationship_summary` 的形状)
        - `closest` —— 其中最亲的那一段的 `sentiment`,方便宿主一眼看
        - `contacts` —— 世界**主动**想起他的次数(她找他,不是他找她)

        ⚠️ **转录归 MySQL 的世界里这是一次 SELECT**,别放进 tick 循环里调。
        """
        pid = str(player_id or "").strip()
        if not pid:
            raise ValueError("player_id is required")
        scheduler = self.scheduler
        # 补课在锁下(理由见 `catch_up_projection` 的 docstring):这条路是只读的,
        # 但**补课本身是写**(它折进投影、挪水位),而这一层恰恰是宿主最可能挪到
        # 慢通道上去的那一条(下面那句 ⚠️ 说的就是它有多贵)。
        with scheduler._lock:
            scheduler.catch_up_projection()

        convs: list[dict[str, Any]] = []
        for agent_id in scheduler.agents:
            for row in self.chat_store.list_conversations(agent_id):
                if (row.get("player_id") or "user") == pid:
                    convs.append(row)
        stamps = [int(r.get("started_at") or 0) for r in convs if r.get("started_at")]
        last_stamps = [
            int(r.get("last_activity_at") or r.get("started_at") or 0) for r in convs
        ]
        first_seen = min(stamps) if stamps else None
        last_seen = max(last_stamps) if last_stamps else None
        # 墙钟,不是 tick:这条账是给**合规**看的,而第十八条那类义务(连续使用
        # 时长)按真实时间算 —— 世界 tick 和真实时间没有固定比率。
        span_days = (
            round((last_seen - first_seen) / 86400.0, 2)
            if first_seen and last_seen else 0.0
        )

        rows = []
        for agent_id in scheduler.agents:
            row = self._relationship_row(agent_id, pid)
            if row["exists"] or row["met"]:
                rows.append(row)
        closest = max((r["axes"]["sentiment"] for r in rows), default=0.0)

        contacts = 0
        store = getattr(scheduler, "contact_store", None)
        if store is not None:
            for row in store.all():
                if row.get("player_id") == pid:
                    contacts += int(row.get("fired_today") or 0)

        return {
            "player_id": pid,
            "conversations": len(convs),
            "messages": sum(int(r.get("message_count") or 0) for r in convs),
            "agents": len({r.get("agent_id") for r in convs if r.get("agent_id")}),
            "first_seen": first_seen,
            "last_seen": last_seen,
            "span_days": span_days,
            "relationships": rows,
            "closest": round(float(closest), 4),
            "contacts": contacts,
        }

    def persona_drift(self, agent_id: str, *, baseline_n: int | None = None,
                      player_id: str | None = None) -> dict[str, Any]:
        """**她还是不是她** —— 人设漂移的尺子(R2)。

        长对话里人设会漂,这是可测的:人设块坐在提示词开头,而注意力随窗口填满而
        衰减,八轮内就测得出显著偏移(arxiv 2402.10962)。这一层把她说过的话按时间
        排开,拿**她自己最早那几条**当基线,后面的每一条走 CUSUM ——
        单条消息的抖动没有意义,而 CUSUM 是专门在噪声里认持续小偏移的。

        **不调模型**(纯计数),所以同一段转录跑一百遍给同一个答案:它能进 CI,
        也能当一条体检。代价是它测的是**文风**不是人格 —— 拿它当报警器,别当结论。
        判据与七个特征见 `anima_world.drift`。

        `player_id` 只看跟这个人的对话(不同的人会把她带向不同的样子,混在一起
        算等于让两段关系互相稀释);`baseline_n` 改基线取几条。

        返回见 `drift.analyze()`。**样本不够时 `ok=False` 并说出为什么** ——
        在 5 条消息上宣布"人设很稳"比不测更坏。里面单独有一格 `sycophancy`:
        它同时是合规项(第八条五:不得过度迎合用户)。

        CLI 出口:`anima-world drift --agent 夏`。
        """
        from anima_world import drift as drift_mod

        agent_id = str(agent_id or "").strip()
        if agent_id not in self.scheduler.agents:
            raise KeyError(f"agent {agent_id} not found")
        said: list[tuple[int, int, int, str]] = []
        for conv in self.chat_store.list_conversations(agent_id):
            if player_id is not None and (conv.get("player_id") or "user") != player_id:
                continue
            conv_id = int(conv["id"])
            for seat, msg in enumerate(self.chat_store.messages_for(conv_id)):
                if str(msg.get("role") or "") in drift_mod.HER_ROLES:
                    said.append((
                        int(msg.get("created_at") or 0), conv_id, seat,
                        str(msg.get("content") or ""),
                    ))
        # 按时间正序 —— 漂移问的是"后来的和当初的比",次序错了**整个结论就反了**。
        #
        # ⚠️ **`created_at` 是墙钟的秒,不足以定序。** 只拿它当键的话,同一秒里的
        # 消息保持**取出来的次序**(稳定排序),而 `list_conversations` 两个后端
        # 都是**倒序**给的(Redis 版 `rows.reverse()`、MySQL 版 `ORDER BY id DESC`)——
        # 于是一段"先不迎合、后极度迎合"的转录被读成"先迎合、后不迎合",
        # `rising` 报成 False,退出码照样 0,日志干净。而 CI 里喂一段转录进去
        # **正是同秒批量落库这个形状**(REFERENCE 承诺它能进 CI),所以这不是边角。
        # 补上的两个键都是单调的:会话 id 按开场次序发,座次是同一场里的追加次序。
        said.sort(key=lambda row: row[:3])
        report = drift_mod.analyze(
            [text for *_, text in said],
            baseline_n=drift_mod.BASELINE_N if baseline_n is None else int(baseline_n),
        )
        report["agent_id"] = agent_id
        report["agent_name"] = self.scheduler.agent_display_name(agent_id)
        report["player_id"] = player_id
        return report

    def inbox(self, player_id: str, *, since_seq: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        """有谁来找过你 —— 角色主动搭话的收件箱(issue #13)。

        **敲门不是对话**:一条 `agent_hail` 不产生记忆、不动关系、不开会话。玩家还没
        回话,世界里什么也没发生;真正的对话仍然由 `World.chat` 发起,走原来那条完整
        的链。这条边界是有意的 —— 否则你会看到"她来找过我",转头问她却毫无印象。

        返回按 seq 升序。

        ⚠️ **要增量拉取请用 `inbox_page()`。** 拿这一条的最后一条 `seq` 当下次的
        `since_seq` 在热闹的世界里会**饿死**:一整窗都是别人的敲门时你拿到空 list,
        没有"最后一条",游标一步都推不动,而他自己那条「她想你了」永远排在窗外。
        """
        return self.inbox_page(player_id, since_seq=since_seq, limit=limit)["events"]

    def inbox_page(
        self, player_id: str | None, *, since_seq: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        """`inbox()` 的**带游标**版本 —— 增量拉取用这一条。

        `{"events", "next_seq", "cursor", "scanned", "total"}`,每一格的语义与
        `contact_requests_page()` 逐字相同(两扇门共用 `_filtered_page`)。
        `player_id` 给 `None` 就是不过滤(运维/调试用)。
        """
        return self._filtered_page(
            kind="agent_hail", player_id=player_id,
            since_seq=since_seq, limit=limit,
        )

    @property
    def player_ttl_seconds(self) -> float:
        """一个玩家多久没动静就当他走了(墙钟秒)。**这就是 Redis 键上的 TTL。**

        不要求宿主维护心跳(那会把契约面弄脏),任何一次交互都算"我还在"。
        改它会当场重挂在场者的过期时间 —— 否则调小了对已经在场的人无效。
        """
        return self.presence_store.ttl_seconds

    @player_ttl_seconds.setter
    def player_ttl_seconds(self, value: float) -> None:
        self.presence_store.ttl_seconds = float(value)

    def who_is_present(self) -> list[str]:
        """此刻真的在场的玩家 id(已过 TTL 的当作走了)。

        **判据只有一条:他那个键还在不在。** 过期由 Redis 自己做,这里不扫表、
        不比 `last_seen` —— 两套过期规则迟早给出不同答案,而两边都不报错。
        """
        return self.presence_store.ids()

    def _touch_player(self, player_id: str, **fields: Any) -> dict[str, Any]:
        """记一次"这个玩家还在",顺便更新几个字段。所有玩家入口都过这里。

        **`role=""` 是"这一路不知道他是谁",不是"他叫 player"** —— 和读那一侧的
        `_interlocutor_for` 逐字同构(`str(role or known.get("role") or "")`),
        只是这里是写。从前几个入口的默认值都是字面量 `"player"` 并且无条件写下去,
        于是"我知道他的身份是 player"和"我这条路上压根拿不到身份"变成同一件事:
        玩家在门口填的「刚搬来的人」,走一步就被 `player_do_action` 的 walk 分支
        (它没有 role 可传)冲成 `player`。而 `chat_service` 会把 `player` 当占位
        身份**整段丢掉**,于是身份块里那句「（身份：刚搬来的人）」就此消失 ——
        **占位身份的过滤器反过来把这次损坏藏住了**,线上照跑、日志一行不错。
        """
        # **空 = "这一路不知道",不是"他改叫空字符串了"** —— `name` 和 `role` 同一
        # 条,理由也同一条:有些入口手上根本没有这一格(点一下"走"、世界重启后的
        # 复位),而写下去就是把世界记着的那个冲掉。落在这儿而不是各个入口里:
        # 这是所有玩家入口的**唯一窄口**,挨个加等于给未来的第 N 个入口留一个洞。
        for blank_means_unknown in ("role", "name", "display_name"):
            if not str(fields.get(blank_means_unknown) or "").strip():
                fields.pop(blank_means_unknown, None)
        # 建行时那一格是**空的**,不是 `"player"` —— 建行的常常正是那些手上没有身份
        # 的路(点一下"走"),而播一个字面量等于替它们答了"他叫 player"。它还有个
        # 更实的后果:那一格一旦有值,聊天那条路从前的 `setdefault` 就永远不生效。
        fresh = self.presence_store.create(
            player_id, {"role": "", "location": None}
        )
        if fresh:
            # 他身上声明过的量在这儿落地 —— 和角色走同一份(`register` 那条路),
            # 只填缺不覆盖。这里是玩家侧唯一的窄口,和 `register` 的地位一样。
            # 少了这一步,`requires: ["me_体力 >= 4"]` 对他恒不成立:世界里每一件
            # 要力气的事他都做不了,而回执只说"你做不了",一个字不提原因。
            self.scheduler.seed_actor_quantities(f"player:{player_id}")
            self._grant_player_allowance(player_id)
        self.presence_store.update(
            player_id, {**fields, "last_seen": time.time()}
        )
        return self.players[player_id]

    def _grant_player_allowance(self, player_id: str) -> None:
        """他兜里的第一笔钱 —— 一辈子一次。

        落成账本上一笔 `payment`,和别的钱走同一条路(账本是投影,对账 = 重放)。
        **窄口只有一个**:补在"每次露面"上等于把余额补满,而补满了的钱包不构成
        代价 —— 他永远不必掂量买哪一样,于是货架又成了摆设,只是换了个方向。

        ⚠️ **那个"一次"记在账本上,不记在"在场"上。** 从前的判据是
        `presence_store.create` 报的 `fresh`,而 3.2.0 把在场从进程内存搬进了带
        TTL 的 Redis 键 —— 于是那句话的含义悄悄从"他这辈子头一回露面"变成了
        "他这一刻钟里头一回露面":挂机十五分钟再回来就是又一笔,没有上限,而
        账本、日志、屏幕上没有一处会说它不对。线上真的这样(晚潮的
        `dogfood-2e7fbb4` 领了四次 60 块)。所以判据换成
        `Projection.allowances` —— 那是全量重放折出来的,过不了期。

        **要先补课再问**:别的进程刚发过的那一笔,不折过来就看不见,而这一层
        正是多进程共用一个世界的地方。

        给不出钱不该掀翻一次露面(没接经济层的世界、没有事件日志的世界):
        这是一份见面礼,不是一道闸。
        """
        from anima_world import economy

        store = self.scheduler.config_store
        if store is None or self.scheduler.event_log is None:
            return
        try:
            amount = float(store.get("economy.player_allowance", default=0.0) or 0.0)
        except (TypeError, ValueError):
            return
        if amount <= 0:
            return
        holder = f"player:{player_id}"
        # **补课和那一问在同一把锁下** —— 这条路发的是 `payment`,而
        # `payment` 正是"折两遍 = 账翻倍"的那一类;判据("他领过没有")读的又
        # 恰好是投影。分两次拿锁等于给 tick 线程留一个挤进来的位置。
        with self.scheduler._lock:
            self.scheduler.catch_up_projection()
            if holder in self.scheduler._memory_projection.allowances:
                return
        self._record_and_fan({
            "type": "payment", "who": holder,
            "payload": {"from": economy.TOWN, "to": holder,
                        "amount": amount, "reason": "allowance"},
        })

    def _present_roster(self) -> dict[str, dict[str, Any]]:
        """在场玩家名册 —— 世界那一侧问"人在哪"时读的就是这一份。

        **先把到站的人放下。** 玩家没有 `_land_arrivals` 替他落地(`player_location`
        是读到才算),而这份名册回答的正是"他这会儿在哪":不结算的话,一个早就到了
        的人在世界眼里还站在出发地,一直站到他下次自己开口。

        `in_transit` 单列一栏而不是把位置抹成空串:调用方要分得开"他在路上"和
        "世界不知道他在哪"—— 拒绝那句话里说的就是这个差别。

        在路上时**还带一格 `transit`**(去哪、还有多少世界分钟),和角色那一半的
        `activity.transit` 同形同源(`_transit_view`)。只给布尔的话,界面说得出
        「在路上」却说不出去哪、还有多久 —— 而时间是第三种代价,只有看得见才咬得
        住人;何况这两半本来就该长一样,`state()` 会把它们并排摆出去。
        """
        roster: dict[str, dict[str, Any]] = {}
        for pid in self.who_is_present():
            location = self.player_location(pid)   # 到站就落地,顺带补 location_join
            trip = self._player_transit.get(pid)
            row = {
                **(self.presence_store.get(pid) or {}),
                "location": location,
                "in_transit": trip is not None,
            }
            if trip is not None:
                row["transit"] = _transit_view(self.scheduler, trip)
            roster[pid] = row
        return roster

    # 一个世界日里"一小时"是多少 tick —— `player_doing` 的说话窗口(见那条 docstring)。
    _CHAT_IS_STILL_ON_MINUTES = 60

    # 空菜单的那三个原因,写给人看的那一份(`player_options` 的 `blocked_text`)。
    # **每加一个枚举就得在这儿加一句**,`test_每一个挡住的理由都配了一句话` 对着
    # 这张表逐格点名 —— 忘了配话的那天没有任何一处会报错,只有玩家屏幕上多出一个
    # 英文标识符。每一句都要说得出**他下一步该干什么**:「你做不了」教不会他任何事。
    _BLOCKED_WORDS = {
        "unknown_player_location": "你还没落个脚 —— 先挑个地方站过去,才谈得上做什么",
        "in_transit": "你在路上 —— 到了地方就能动手了",
        "no_ontology": "这个世界还没摆出什么摸得着的东西 —— 先找个人说说话吧",
    }

    # 货架为什么空,写给人看的那一份(`player_shop` 的 `note`)。和上面那张表
    # 同一条纪律:**每加一个枚举就得在这儿加一句**,`test_货架空着的每个理由都配了
    # 一句话` 逐格点名。三个理由长得一模一样(`shelf: []`),而玩家的下一步完全不同。
    _SHOP_WORDS = {
        "unknown_player_location": "你还没落个脚 —— 先挑个地方站过去,才看得到人家卖什么",
        "in_transit": "你还在路上 —— 到了再看",
        "no_shop_here": "这儿没有卖东西的 —— 换个地方逛逛",
    }

    def player_doing(self, player_id: str) -> str:
        """他此刻在做的那件事(`walk` / `interact` / `chat`),什么也没做就是 `""`。

        **这是"人也是世界里的人"的最后一格。** 她身上的量由行为树驱动:
        `scheduler._current_action` 记着她此刻在做什么,世界的规律里
        `{"action": "chat"}` 这类选择器读的就是它。而人没有行为树 —— 于是这张表里
        **从来没有过一个人**,`{"action": …}` 那半边规律对他整个缺席:线上那个世界
        21 个角色的「随和」「手艺」「嗓子」每 tick 都在动,而每一个玩家的这三个量
        停在他进世界那一 tick 的默认值上,一动不动。日志干净、面板照画 ——
        照跑,但给错东西。反过来 `{"not_action": …}` 却算得到他(它是"所有角色"减去
        "正在做这件事的"),所以本该互补的两半对人是**单边**的:他只吃得到往下拖的
        那一条,吃不到往上走的那一条。

        **派生,不存储。** 三个来源都是真的、当下的状态,所以没有第二份真相要维护、
        也没有会过期的账:占着他的那件长过程(`:engaged`)、他在不在路上、以及他
        上一次开口离现在多久。存一份"他上次说他在做什么"的话,一个关掉浏览器的人
        会在世界里永远地走下去。

        优先级是**约束由强到弱**:占用 > 赶路 > 说话 —— 和拒绝那三类的排法同一条。
        赶路时他仍然说得上话(`_PLAYER_TRANSIT_OK`),但那会儿他主要在赶路,
        不是坐下来聊天。
        """
        pid = str(player_id or "").strip()
        if not pid:
            return ""
        prefix = self.scheduler.PLAYER_PREFIX
        if self.scheduler._occupying(f"{prefix}{pid}") is not None:
            return "interact"
        if self.player_in_transit(pid):
            return "walk"
        spoke = self._player_chat_tick.get(pid)
        if spoke is None:
            return ""
        minutes = max(1, int(self.scheduler._minutes_per_tick()))
        window = max(1, round(self._CHAT_IS_STILL_ON_MINUTES / minutes))
        return "chat" if int(self.scheduler.clock) - int(spoke) <= window else ""

    def _players_doing_now(self) -> dict[str, str]:
        """在场的人此刻各自在做什么 —— scheduler 每 tick 问一次的那份。

        只报做着事的人:空串在 `_agents_doing` 那侧没有意义,而一个"什么也没做"
        的条目会让 `not_action` 那半边多绕一次。
        """
        doing: dict[str, str] = {}
        for pid in self.who_is_present():
            kind = self.player_doing(pid)
            if kind:
                doing[pid] = kind
        return doing

    def player_action(
        self,
        player_id: str,
        action: str,
        details: dict[str, Any] | None = None,
        *,
        role: str = "",
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
                # 这一路没给身份就写世界记着的那个,别把事件里的 role 记成空 ——
                # 事件是不可改的历史,写空了以后谁都补不回来。
                "role": role or str(player.get("role") or ""),
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

    @staticmethod
    def _money(amount: float) -> str:
        return f"¥{amount:.2f}".rstrip("0").rstrip(".")

    def _too_poor(self, wallet: float, price: float) -> str:
        """钱不够那句话。**说得出还差多少** —— 「买不起」教不会他任何事,
        「还差 ¥0.85」他知道自己要先去挣或者先去卖点什么。

        灰按钮上印的那句和真按下去被拒的那句**共用这一个函数**:另写一遍的话,
        两句迟早分叉,而分叉的方向必然是屏幕上那句更好看。
        """
        return (f"你带的钱不够:要{self._money(price)},"
                f"你有{self._money(wallet)} —— 还差{self._money(price - wallet)}")

    def player_buy(self, player_id: str, location_id: str, item_id: str) -> dict[str, Any]:
        """玩家买货:钱包扣款、货架减一,payment + item_transfer 事件入账本。

        **人得站在那儿。** 和能力调用上那道 `absent` 闸是同一条:一次交易是
        一个人、一个地方、一个瞬间。放开的话玩家在渡口就能刷光全镇的货架,
        而"走过去"本身是这个世界里的一段代价。拒绝**两头都说** —— 只说
        "它在铁匠巷"会读成一句谎,真正的原因可能是世界压根不知道他在哪。

        ⚠️ **玩家读得到的拒绝,不许坐 `KeyError` 这班车。** `KeyError.__str__` 是
        `repr(args[0])` —— 全 Python 独此一家。世界壳照规矩 `str(exc)` 原样传、
        一个字没改,而玩家屏幕上出现的是 `'小念咖啡车没有一杯拿铁了'`,**带着
        一对单引号**;线上真的这样。所以这条路上只有两种车:走不通
        (`LookupError` —— 你不在这儿 / 这儿没有 / 世界还不认识你)与钱不够
        (`ValueError`),两种的下一步不一样,而两种都说人话。
        """
        from anima_world import economy

        if self.scheduler.event_log is None:
            raise ValueError("economy needs a persistent world")
        player = self.players.get(player_id)
        if player is None:
            raise LookupError("世界还不认识你 —— 先在一个地方落个脚")
        store = self.scheduler.economy_store
        if store is None:
            raise ValueError("economy needs a persistent world")
        if self.player_in_transit(player_id):
            raise LookupError("你还在路上 —— 等走到了再买")
        here = self._player_here(player_id)
        if not here:
            raise LookupError("世界还不知道你在哪 —— 先走到卖它的地方去")
        if here != location_id:
            raise LookupError(
                f"你不在「{self._location_display_name(location_id)}」 —— "
                f"你在「{self._location_display_name(here)}」"
            )
        with self.scheduler._lock:
            shelf = next(
                (r for r in store.shelves()
                 if r["location_id"] == location_id and r["item_id"] == item_id),
                None,
            )
            sold_out = (
                f"「{self._location_display_name(location_id)}」没有"
                f"「{self.scheduler.item_name_of(item_id) or item_id}」了"
            )
            if shelf is None or int(shelf.get("quantity") or 0) <= 0:
                raise LookupError(sold_out)
            price = float(shelf["price"])
            holder = f"player:{player_id}"
            # 门禁读账本,不读内存 —— 那两个数此前会分叉(见 player_topup)。
            wallet = float(self.scheduler._memory_projection.balances.get(holder, 0.0))
            if wallet < price:
                raise ValueError(self._too_poor(wallet, price))
            if not store.take_stock(location_id, item_id):
                raise LookupError(sold_out)
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

    def player_shop(self, player_id: str) -> dict[str, Any]:
        """他脚下这地儿卖什么、他有多少钱、他带着什么 —— **一屏**。

        `shop()` / `balance()` / `inventory()` 三个门早就都在,而上一轮真人试玩里
        整套经济仍然对玩家隐形:世界壳一条也没接。三个门拼一屏这件事每个宿主都要
        做一遍,而做漏的样子是安静的 —— 拼不出"这个按钮为什么是灰的",宿主就干脆
        不画按钮。所以拼装归引擎(和 `player_options` 同一个理由、同一个形状)。

        每一行的判据和 `player_options` **逐字同构**:`available` + 分得开的
        `reason` + 一句引擎已经写成人话的 `refusal`。两类拒绝一个都不许合并 ——
        `broke` 是"再去挣点"、`sold_out` 是"改天再来",而合成一句"现在不能",
        玩家会挨样点过去,每一样都告诉他同一句废话。

        名字一律人话:`item_id` / `location_id` 印给玩家是这个仓库反复修的那类
        bug(引擎的词汇漏进他读到的那句话)。id 照旧带着 —— 那是他点下去要回传的
        东西,不是给他看的。

        世界不知道他在哪就没有货架(`location: ""`),不是一个空店铺:那两件事
        玩家的下一步完全不同。**在路上也没有货架** —— 位置从 `_player_here` 问,
        不从 `player_location`(后者在途仍答出发地);从前这一屏会一边写着
        `in_transit: true`、一边把他刚走开的那家店的货**整架摆出来还标着可买**,
        一屏之内自相矛盾,而 `player_buy` 那头真的卖给他。

        **空货架为什么空,由引擎说成人话**(`empty` 枚举 + `note`),和
        `player_options` 的 `blocked` / `blocked_text` 逐字同构。三件事在数据上
        长得一模一样(`shelf: []`),而玩家的下一步完全不同:在路上要等、没落脚
        要先站过去、这儿本来就不卖东西要换个地方。从前这三句归宿主自己拼 ——
        拼漏的那个宿主给玩家的就是一块什么也没写的空板子,而**世界照跑、日志干净**。
        """
        self._touch_player(player_id)
        holder = f"player:{player_id}"
        in_transit = self.player_in_transit(player_id)   # 顺带结算到达
        here = self._player_here(player_id)
        wallet = self.balance(holder)
        store = self.scheduler.economy_store

        shelf: list[dict[str, Any]] = []
        for row in (self.shop(here) if here else []):
            price, quantity = float(row["price"]), int(row["quantity"])
            if quantity <= 0:
                reason, refusal = "sold_out", "卖完了,改天再来"
            elif wallet < price:
                reason, refusal = "broke", self._too_poor(wallet, price)
            else:
                reason, refusal = "", ""
            shelf.append({
                **row,
                "name": row.get("name") or row["item_id"],
                "available": not reason,
                "reason": reason,
                "refusal": refusal,
            })

        names: dict[str, str] = {}
        if store is not None:
            names = {str(r["id"]): str(r.get("name") or r["id"]) for r in store.items()}
        carrying = [
            {"item_id": item_id, "name": names.get(item_id, item_id), "quantity": int(qty)}
            for item_id, qty in sorted(self.inventory(holder).items())
            if int(qty) > 0
        ]
        if shelf:
            empty = ""
        elif in_transit:
            empty = "in_transit"
        elif not here:
            empty = "unknown_player_location"
        else:
            empty = "no_shop_here"
        return {
            "location": here,
            "location_name": self._location_display_name(here) if here else "",
            # 「他在路上」和「世界不知道他在哪」分得开 —— 两边 `location` 都是空的,
            # 而前者是"再等等",后者是"你还没走过路"。
            "in_transit": in_transit,
            "balance": wallet,
            "shelf": shelf,
            "carrying": carrying,
            "empty": empty,
            "note": self._SHOP_WORDS.get(empty, ""),
        }

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

    def _players_here(
        self, location: str, *, exclude: str = "", doing: dict[str, str] | None = None
    ) -> list[str]:
        """和这个地方同处一室的**别的玩家**该怎么称呼。

        在场块的 `others` 从前只从 `scheduler.agents` 里拼,于是一个三个人的房间
        在她眼里只有一个人。这不是少了行装饰:她会当着另外两位的面说只该对你说的
        话,而世界里明明写着他们在场 —— 照跑,给错东西。

        两条边界:
        - **排除正在跟她说话的那一位**。身份块已经单独讲过他(「X 和你都在……,
          因此这是面对面交谈」),再进 `others` 就会被补上一句"他只是同场角色,
          不是正在和你说话的人",同一段提示词里自相矛盾。
        - **在途不算在场**,和角色那半同一条规矩(`_agent_locations` 跳 `_transit`)。

        没报过名字的用 `访客`,不是他的 id —— 她读到什么就说什么(`_place_name`
        那一课),把一个 uuid 放进提示词就是让她把 uuid 念出口。

        `doing` 是 `_activities_now()` 那一份(可选)。给了就在名字后面缀上他此刻在
        做的事 —— **和同屋角色那一半走的是同一份措辞、同一个括号**:少了这一句,
        提示词里的他就是一个"站在那儿什么也没做的人",而世界里他刚花了体力干完活。
        """
        out: list[str] = []
        doing = doing or {}
        for pid, info in self._present_roster().items():
            if pid == exclude or info.get("in_transit"):
                continue
            if str(info.get("location") or "") != location:
                continue
            name = str(info.get("display_name") or chat_service_mod.DEFAULT_ADDRESS)
            said = doing.get(
                self.scheduler.stock_owner_of(f"{self.scheduler.PLAYER_PREFIX}{pid}")
            )
            out.append(f"{name}({said})" if said else name)
        return out

    def world_context(self, agent_id: str, interlocutor_id: str) -> dict[str, Any]:
        """chat-grounding:锁内一次快照角色的 lived state(只读,无 LLM 无 IO)。"""
        from anima_world.memory_triggers import BAND_NAMES, band

        scheduler = self.scheduler
        config_store = scheduler.config_store
        # 在场玩家的行程先结算 —— **在快照之前**,因为结算会补发 `location_join`,
        # 而下面那一段说好了是只读的(`_present_roster` 的长注释)。
        self._present_roster()
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
            # 同屋的人各带一句"此刻在做什么"。措辞取自 `_activities_now()`,和自主
            # 上下文的在场名单、感知块那一行是同一份 —— 各拼一遍必然分叉,而分叉
            # 那天不报错:一边说他在擦窗,另一边把他写成闲着,两句话进同一份提示词。
            # 齐老板在线上对一个正在做事的玩家说「我没见你动过手」,病就在这儿:
            # 世界里那个人一直在动,只是提示词里从没写过。
            doing = self._activities_now()
            # **她自己那一句走同一份措辞。** 从前它单独走 `_ACTIVITY_LABELS`,
            # 于是一个正被长过程占着的人读到「你在回声唱片店,闲着」,而同屋的人
            # 在同一份提示词的下一行读到「江晚(在一起听完一面)」—— 一处分支
            # 换来两句互相打脸的话。在途那一支留着:它答的是「你在哪儿」,
            # 比"此刻在做什么"多一格终点,而路上的人本来就不在做别的事。
            # **整句挪进了 `_self_activity_label`**,自主上下文读的是同一份。
            label = self._self_activity_label(agent_id, activity, doing)

            def _busy(actor: str, name: str) -> str:
                said = doing.get(scheduler.stock_owner_of(actor))
                return f"{name}({said})" if said else name

            others = [
                _busy(aid, scheduler.agents[aid].agent.name)
                for aid, loc in scheduler._agent_locations().items()
                if loc == loc_id and aid != agent_id
            ]
            others += self._players_here(loc_id, exclude=interlocutor_id, doing=doing)
            # **她去得了哪儿,得由世界告诉她。** `walk` 的 `location` 是必填,而在这
            # 之前整份提示词里没有一处列过这个世界有哪些地方 —— 她于是只能编一个
            # (线上现场:「回声后面有个小阁楼」,而世界里没有这个地方),然后整件事
            # 退回散文,连一次被拒绝的记录都不留。给了必填参数却不给取值范围,是这一轮
            # 反复撞见的那条缝的又一处。
            #
            # ⚠️ 这个洞被**引擎自带的那份 world.setting** 盖了很久:它手写着"街区只有
            # 三个地方——咖啡店(cafe)、建筑工作室(workshop)、以及一间用来画画的家
            # (home)",于是演示世界和整套测试都读得到清单。作者一换掉 setting(真世界
            # 都会换)清单就没了。而那句手写的话本身也是一颗雷:谁给这个世界加第四个
            # 地点,它就当场变成一句谎,且不报错。
            #
            # 名字够了,不带 id:`walk` / `walk_away` 现在都收人话(`resolve_location`)。
            # 这一行按世界的规模封顶,不随时间涨 —— 和有界性那条对得上。走
            # `point_names()` 而不是自己遍历地图:她读到的清单和工具认的那份必须是
            # 同一份,各写一遍就迟早只有一半跟着代码走(#20 那条判据的另一面)。
            names = self._tool_runtime.point_names()
            places = places_menu(names, with_ids=False) if names else ""
            ctx["presence"] = {
                "day": now.day, "hh": f"{now.hour:02d}", "mm": f"{now.minute:02d}",
                "location_id": loc_id, "location": loc_name, "activity": label,
                "others": "、".join(others),
                "places": places,
                # 在途不算在场。黑板的 `loc` 要落地才改写,途中读出来仍是出发地
                # —— `_agent_locations()` 跳过 `_transit` 就是在补这个洞,只比
                # 地点的话,一个正在赶路的人会被判成和你面对面。
                "in_transit": agent_id in scheduler._transit,
                # 她最近一次走完的那段路是从哪儿出发的(没走过就是空)。**这一层只
                # 给事实,"要不要说出来"归 chat_service** —— 它那边才知道对方此刻
                # 在哪,而这句话只有在"他还站在你刚离开的那个地方"时才成立。
                "came_from": str(
                    (scheduler._last_arrival.get(agent_id) or {}).get("from") or ""),
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
                            visibility=visibility, ontology=self.scheduler.ontology,
                            activities=self._activities_now())
        except Exception:  # noqa: BLE001 - 读不到感知不该让聊天告吹
            logger.warning("读 perception 失败", exc_info=True)
            return None

    def _activities_now(self) -> dict[str, str]:
        """此刻每个人在做的那件事,渲染成一句人话 —— `{"agent:齐": "在陪一次夜播"}`。

        **这个仓库里"某某此刻在做什么"只有这一句措辞。** 三个读者共用它:聊天提示词的
        presence 块(`presence.others`)、自主上下文的在场名单(`AutonomyContext.present`)、
        以及感知块里那一行(`Perception.activities` → `describe_here`)。各写一遍的话
        它们必然分叉,而分叉那天不报错:一边说她在工作,另一边把她写成闲着,两句话
        进同一份提示词。

        **人和玩家一处分支都没有。** 两个来源都是全场一问:`actions_now()`(她的来自
        行为树、他的来自每 tick 的玩家动作快照,合并点只有那一处)与
        `occupations_now()`(谁被一件长过程占着,以及那件事叫什么)。两边的 id 都经
        `stock_owner_of` 落进同一个命名空间,所以"她看得见他擦窗"和"他看得见她擦窗"
        是同一段代码。

        **长过程盖过动作名**:它更具体(「在陪一次夜播」而不是「在忙手上的事」),
        而且它是这两条路唯一都答得出的那一格 —— 一个用 `World.act` 起了长过程的
        角色根本不经过行为树,只有 `:engaged` 记着她。

        闲着的人不在这份表里(见 `_IDLE_KINDS`)。
        """
        scheduler = self.scheduler
        doing: dict[str, str] = {}
        for owner, kind in scheduler.actions_now().items():
            if kind in _IDLE_KINDS:
                continue
            label = _ACTIVITY_LABELS.get(kind)
            if label:
                doing[owner] = label
        for owner, said in scheduler.occupations_now().items():
            doing[owner] = f"在{said}"
        return doing

    def perception(self, agent_id: str) -> dict[str, Any]:
        """这个角色此刻**感知到**什么(不是世界有什么)。

        存在的理由是可查:可见性是声明出来的,而"我以为她知道/其实她不知道"是这一层
        最容易的错。这个函数就是那个对照面。
        """
        perceived = self._perceive(agent_id, self._tool_runtime.agent_location(agent_id))
        return {} if perceived is None else perceived.to_dict()

    def player_perception(self, player_id: str) -> dict[str, Any]:
        """人此刻感知到什么 —— **和她那份走的是同一个函数、同一套声明**。

        这是 `interact` 开给玩家之后的另一半:给了动词却不给"这儿有什么、它能被
        怎么做",宿主只能自己拿 `entities()` 和 `kinds()` 去拼一份能力表 ——
        而拼错了不报错,按钮点下去才发现世界不认(`ontology --check` 那一课)。
        `verbs` 里那几个词就是 `player_tool("interact", …)` 的 `verb` 收的东西。

        他身上的量也照同一份可见性声明来:`self` 只有他自己看得见、`here` 同屋的
        人都看得见 —— 玩家不是第二套可见性,是同一套里的另一个人。

        在路上 = 不在任何地方(`_player_here`),于是 `here` 那一档什么也照不见,
        `self` 那一档照旧 —— 赶路的人仍然知道自己累不累,但看不见他已经走开的
        那间屋里有什么。
        """
        perceived = self._perceive(
            f"{Scheduler.PLAYER_PREFIX}{player_id}", self._player_here(player_id)
        )
        return {} if perceived is None else perceived.to_dict()

    def player_options(self, player_id: str) -> dict[str, Any]:
        """**这个人此时此地点得动什么** —— 一份可以直接渲染成按钮的菜单。

        `player_perception()` 答的是"这儿有什么、它能被怎么做";这一条再往前走
        一步,答"**这会儿点得动吗、点不动是为什么**"。缺了后半句,宿主只能把每个
        动词都画成一个按钮,让人一个个点过去试 —— 而每一次失败的原因都写在引擎里。

        三条,每条都对着一种"能跑但给错东西":

        1. **不新造第二套真相。** 这儿有什么走 `player_perception`(所以可见性、
           budget、`overflow` 一条都不绕过),能被怎么做走 `Ontology`,成不成走
           `Scheduler.perform_affordance(dry_run=True)` —— **和真点下去那一次是
           同一个函数**。另写一份"看上去差不多"的判定,下场是菜单说得动、点下去
           世界不认,两边都不报错。
        2. **四类拒绝一个都不合并**(`reason` 逐字来自那条真路):`conditions`
           该等一会儿、`incapable` 该先去补足、`busy` 该等手上这件做完、剩下那摞
           是讲不通的调用。合成一句"现在不能",一个累坏了的人会挨扇窗点过去。
        3. **一个字节都不写**。它每一帧都要被渲染一次。
        4. **代价说在按下去之前**(`cost`)。四类拒绝管的是"点不动的时候为什么",
           而一个把人锁住一小时的按钮**是点得动的** —— 它和一个瞬间完成的按钮
           长得一模一样,玩家点完才在下一次拒绝里知道自己被占住了。

        5. **量也要翻。** 这一屏上其余每一个名字都翻过了(东西的 `name`、那行
           `gloss`、动词的 `label`、拒绝、代价),只有 `quantities` 把内部键和裸
           数字原样递出去 —— 而宿主没有别的东西可印,于是屏上写着「今日短语
           phrase_age 3」,作者明明写了 `label` 和 `unit`。`bands` 更要紧:她读到
           「雨势 瓢泼大雨」而玩家读到 `雨势 0.82`,同一个世界的同一个量,两个人
           看见两种东西。措辞走 `perception.readouts` —— **和她那行提示词同一个
           函数**,各写一遍必然分叉而且不报错。

        形状:`{"player_id", "location", "location_name", "blocked", "overflow",
        "own": {"quantities", "readouts"},
        "targets": [{"id","name","gloss","kind","quantities","readouts",
        "verbs":[{"verb","label","available","reason","refusal","cost",
        "participants","candidates"}]}]}`,
        `readouts` 每行是 `{"key","label","value","word","unit","text"}` ——
        **`quantities` 那份键与数字仍是契约,`readouts` 是加上去的**(宿主要自己
        排版就拿分开的几格,不想排版就印 `text`)。

        `blocked` 是**空菜单的原因**,不是一句沉默:`unknown_player_location`
        (宿主没调过 `player_move` —— 玩家做什么都改不了)、`in_transit`
        (他在路上)、`no_ontology`(这个世界没声明过东西)。空表加一句沉默读起来
        像"这儿什么也没有",而那是三件完全不同的事(和 `presence` 那条同一课)。
        **`blocked_text` 是同一件事写给人看的那一份**,和每个动词那对
        `reason`(枚举)+ `refusal`(人话)逐字同构 —— 理由也是同一条:这句话没有
        主人的话,每个宿主自己译一遍,而译漏的那个会把 `in_transit` 四个字母原样
        印在玩家脸上。线上真的这样,还是在两块并排的面板上:货架那块自己写了
        「你还在路上 —— 到了再看」,能力那块印的是 `in_transit`。
        """
        pid = str(player_id or "").strip()
        blank: dict[str, Any] = {
            "player_id": pid, "location": "", "location_name": "",
            "blocked": "", "blocked_text": "", "overflow": 0, "targets": [],
            # 三条 `blocked` 的早退路径也带上这一格:形状随情况变的话,宿主要么
            # 每处写一遍 `?.`,要么在"他还在路上"那一帧崩掉。
            "own": {"quantities": {}, "readouts": []},
        }

        def blocked(reason: str) -> dict[str, Any]:
            blank["blocked"] = reason
            blank["blocked_text"] = self._BLOCKED_WORDS[reason]
            return blank

        if not pid:
            return blocked("unknown_player_location")
        if self.player_in_transit(pid):
            # 在途不是"还在出发地":算成出发地的话,他一边在路上一边擦着咖啡店
            # 的窗 —— 和 `perform_affordance` 的 `_where_is` 同一条。
            return blocked("in_transit")
        here = self._player_here(pid)
        if not here:
            return blocked("unknown_player_location")
        blank["location"] = here
        blank["location_name"] = self.scheduler.place_name(here) or here

        ontology = self.scheduler.ontology
        if ontology is None:
            return blocked("no_ontology")

        seen = self.player_perception(pid)
        blank["overflow"] = int(seen.get("overflow") or 0)
        actor = f"{Scheduler.PLAYER_PREFIX}{pid}"
        # 一起做事的候选人:这儿站着的角色。**说不出"跟谁"的话,那个按钮点下去
        # 必然失败**,而失败的原因(得有人一起)本来在声明里写着。
        candidates = [
            {"id": aid, "name": self.scheduler.agent_display_name(aid)}
            for aid in sorted(self.scheduler.agents)
            if self.scheduler._where_is(aid) == here
        ]

        targets: list[dict[str, Any]] = []
        for owner, values in sorted((seen.get("here") or {}).items()):
            entity = ontology.entities.get(owner)
            if entity is None:
                continue   # 角色、货架之类不是"能被做点什么"的东西
            kind = ontology.kinds.get(entity.kind)
            if kind is None or not kind.affordances:
                continue
            rows: list[dict[str, Any]] = []
            for affordance in kind.affordances.values():
                out = self.scheduler.perform_affordance(
                    actor, owner, affordance.verb, dry_run=True
                )
                row: dict[str, Any] = {
                    "verb": affordance.verb,
                    "label": affordance.label or affordance.verb,
                    "available": bool(out.get("ok")),
                    "reason": "" if out.get("ok") else str(out.get("reason") or ""),
                    "refusal": "" if out.get("ok") else str(out.get("refusal") or ""),
                    "cost": self._affordance_cost(affordance),
                }
                if affordance.participants is not None:
                    row["participants"] = {
                        "min": affordance.participants.minimum,
                        "max": affordance.participants.maximum,
                    }
                    row["candidates"] = list(candidates)
                rows.append(row)
            targets.append({
                "id": owner,
                "name": (seen.get("names") or {}).get(owner) or entity.name or owner,
                "gloss": (seen.get("glosses") or {}).get(owner, ""),
                "kind": entity.kind,
                "quantities": dict(values),
                "readouts": perception_mod.readouts(
                    values,
                    ((seen.get("words") or {}).get("here") or {}).get(owner, {}),
                    (seen.get("units") or {}).get(owner, {}),
                    ((seen.get("labels") or {}).get("here") or {}).get(owner, {}),
                ),
                "verbs": rows,
            })
        blank["targets"] = targets
        own_values = dict(seen.get("own") or {})
        blank["own"] = {
            "quantities": own_values,
            "readouts": perception_mod.readouts(
                own_values,
                (seen.get("words") or {}).get("own") or {},
                seen.get("own_units") or {},
                (seen.get("labels") or {}).get("own") or {},
            ),
        }
        return blank

    def _affordance_cost(self, affordance: Any) -> str:
        """**按下去之前**要知道的那句话:这件事要花多久、期间还能不能干别的。
        一下子就完的事回空串。

        试玩现场:玩家点了「重描一遍」,按钮亮着、点下去成了,接下来一小时的世界
        时间里他点什么都只得到「你手上还有一件事没做完」。那句拒绝本身没错(第四类
        `busy`),错的是**它来晚了** —— 一个把人锁住一小时的按钮,在按下去之前和一个
        瞬间完成的按钮长得一模一样。`duration` 那一格自己的注释里已经写着同一条理由:
        付了十个月再被拒掉,她没有任何办法预防,而预防不了的代价教不会任何人任何事。

        **只说时间。** 量和材料的代价不写在这儿,因为它们已经有一条更好的路:
        不够的时候 `incapable` 会当场点名差什么;而时间不一样 —— 时间**总是**够,
        于是永远不会有人拦住他。

        写在引擎是因为 `duration` / `occupies` 住在本体声明里,而宿主从来看不见
        本体。措辞归引擎、宿主原样显示,和四类拒绝那条同一条纪律。
        """
        span = self.scheduler.human_span(int(getattr(affordance, "duration", 0) or 0))
        if not span:
            return ""
        if getattr(affordance, "occupies", True):
            return f"要花 {span},这期间做不了别的"
        return f"要花 {span}"


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
