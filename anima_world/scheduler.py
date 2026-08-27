"""Event-drivenScheduler: dispatches events, drives world clock, handles idle + action emission."""

from __future__ import annotations

import json
import logging
import math
import os
import socket
import threading
import time
import uuid
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

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
from anima_world.bt_nodes import Blackboard, StockCondition
from anima_world.chat_service import DEFAULT_ADDRESS
from anima_world.events import EventLog
from anima_world.expressions import ExpressionError
from anima_world import memory_store as memory_store_mod
from anima_world.narrative import NarrativeProvider
from anima_world.perception import why_not_perceivable
from anima_world import together as together_mod
from anima_world.projection import project_events
from anima_world.stocks import clock_names
from anima_world.types import Event
from anima_world.world_time import (
    DEFAULT_MINUTES_PER_TICK,
    WALL_CLOCK_FLOOR,
    WorldTime,
    world_time,
)


logger = logging.getLogger(__name__)


def _event_numbers(event: dict[str, Any]) -> dict[str, float]:
    """一条事件里**读得出数的那几格** → 触发器命名空间里的 `event.<格>`。

    ⚠️ **只收数,不收别的。** 表达式那一层只做算术与比较,一个字符串进去的下场是
    运行期 `TypeError`,而那的样子是"这条触发器安静地跳过了"。所以在**进命名空间
    之前**就把非数的滤掉:读不到的名字会当场报"读了一个不存在的量",作者看得懂;
    一个悄悄跳过的触发器他看不懂。

    嵌套一层也收(`changed` / `me_delta` 那种「量名 → 数」的表),写成
    `event.<格>` 读不到,得写 `event.<格>` 的子键 —— 而两层点号这一版不收,
    所以嵌套那一层**摊平成 `<格>_<子键>`**:`event.changed_树高`。
    """
    out: dict[str, float] = {}
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return out
    for key, value in payload.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            out[f"event.{key}"] = float(value)
        elif isinstance(value, dict):
            for sub, inner in value.items():
                if isinstance(inner, bool) or not isinstance(inner, (int, float)):
                    continue
                out[f"event.{key}_{sub}"] = float(inner)
    return out

# The `events.ts` column carries two different time bases — see
# `world_time.WALL_CLOCK_FLOOR` for the full statement of the rule. Restoring
# the clock skips wall-clock stamps; so does the run report (`sim_report`).
_WALL_CLOCK_FLOOR = WALL_CLOCK_FLOOR

# Performance guardrail — a default, not a law. The operator owns this number:
# `config set scheduler.max_agents N` (0 = unlimited). Hardcoding it here was
# the tool deciding for the operator; the constant remains only as the declared
# default in config_store._DEFAULTS' absence.
MAX_AGENTS = 100


class _BeatWorldReader:
    """节拍谓词读世界用的窄口子。

    `beats.py` 是纯数据 + 求值,不 import 存储层;但 `need` / `memory` 两个谓词要的
    东西不在投影里(需求在黑板上,记忆在 MemoryStore)。所以给它一个只读适配器,
    而不是把 scheduler 整个递进去 —— 谓词能看到什么,这个类就是那份清单。
    """

    __slots__ = ("_scheduler",)

    def __init__(self, scheduler: "Scheduler") -> None:
        self._scheduler = scheduler

    def need(self, agent_id: str, need: str) -> float | None:
        return self._scheduler._beat_needs(agent_id, need)

    def memories(self, agent_id: str) -> list[str]:
        return self._scheduler._beat_memories(agent_id)

# llm-relationship-judge: minimum ticks between verdicts for the same
# (unordered) agent pair — both sides landing chats at each other within
# one window is one conversation, not two. 6 ticks = 30 world minutes at
# the default 5 min/tick.
JUDGE_PAIR_COOLDOWN_TICKS = 6
MAX_TICKS_PER_SECOND = 1000
MAX_IDLE_LOOP_DEPTH = 5
# `_actor_placed` 里表示"她此刻不在任何地方"(在路上)。地点 id 永远非空,
# 所以空串不会和真地点撞车,而它与 `None`(这个进程还没管过她)不是一回事。
_NOWHERE = ""

# 规律 `emit` 出来的事件变成在场者的记忆时,记在这个 kind 下(witness-memory)。
# 独立的 kind 而不是复用 `seed`:一条"她亲眼看见江水漫过台阶"和一条创世注入的
# 背景设定,来路完全不同 —— 混成一个之后,任何按来路筛记忆的地方(反思源、
# 八卦源、`repair_memory_ticks` 这类维护)都再也分不开它们。
WITNESS_MEMORY_KIND = "witness"

# 一次判定小到可以当作没发生的门槛。**三轴落地之后,这道闸不能只看 headline** ——
# 判定器现在会说"这次没多喜欢他,但信了他一点"(sentiment 0.004 / trust 0.15),
# 而只看 headline 的闸会把**整行连轴一起**丢掉,一声不吭。于是"守约"这类只推
# 信任的互动在世界里等于没发生过,而日志干净得像什么都没漏。
RELATION_NOISE_FLOOR = 0.01


def _is_noise(delta: float, axes: dict[str, float] | None) -> bool:
    """headline 和三轴**都**小到可以忽略,这一行才算噪声。"""
    if abs(delta) >= RELATION_NOISE_FLOOR:
        return False
    return not any(abs(value) >= RELATION_NOISE_FLOOR for value in (axes or {}).values())

# 规律的节流水位在 `:meta` 里的行名(`redis_state.meta_rows`)。
#
# **为什么不另开一个 `:rule_marks` 键**:水位是这个世界的元数据,和 `:meta` 里
# 已经住着的 `autonomy_stats`(同样是"上一轮是什么时候"的水位)是同一类东西;
# 而新开一个 Redis 键是**跨仓库存储契约的变更**,镜像端要跟着对齐。为一份
# 每条规律一个整数的水位付那个代价不值。行的形状:
# `{"marks": {规律 id: tick}, "updated_tick": 写下这一行时的世界时钟}`。
RULE_MARKS_ROW = "rule_marks"


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
        world_id: str | None = None,
        meta_store: Any | None = None,
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
        # 时钟住在一个可替换的盒子里。默认在进程内(行为逐字不变);给了 Redis
        # 之后,**"现在是第几 tick"只有一个答案** —— 两个进程各推各的时钟,世界就
        # 分叉了,而分叉之后两边都还在正常跑,只是不再是同一个世界。
        from anima_world.redis_state import ClockStore

        self._clock_store = ClockStore(0)
        self._stopped: bool = False
        self.narrative_provider = narrative_provider
        self.event_log = event_log
        # 这个世界叫什么(Redis 键前缀里的那一段)。世界文件退役后它就是世界的名字。
        self.world_id = world_id
        # 世界的元数据行(`:meta`,redis_state.meta_rows):创世出生证明、占用标记。
        self.meta_store = meta_store
        # 需求 / 小团体 / 反思水位 / 经济:由 build_serve_scheduler 注入 Redis 版;
        # 裸 Scheduler()(单元测试)没有它们,和以前的无 db 路径同一个形状。
        # ⚠️ **3.8.0 起没有人写它了。** 需求的值搬进了量表(和树高、灵力同一张表),
        # 而 `:needs` 那张老检查点表是"内存态每天落一次盘"的产物 —— 内存态没有了。
        # 这一格留着是因为**老世界里那张表还在**:它是那个世界当时的账,删它是抹
        # 历史不是升级。谁都不读它,也谁都不再写它。
        self.needs_store: Any | None = None
        self.clique_store: Any | None = None
        self.reflection_store: Any | None = None
        self.economy_store: Any | None = None
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
        # 叙事器在 scheduler 之前就造好了(它是构造参数),而名册与地图要等世界起来
        # 才有 —— 所以名字是后绑的。不绑的话它写出来的那行字里是键名,而**那行字就是
        # 玩家在世界动态里读到的正文**(线上原文「chi在studio忙着」)。
        bind_names = getattr(narrative_provider, "bind_names", None)
        if bind_names is not None:
            bind_names(self.agent_display_name, self.place_name)
        # M6: seeded from persisted history immediately when an event_log is
        # given, so update_agent_persona/update_location_description's
        # "known entity" checks are correct for ANY Scheduler built against a
        # populated log — not just one that a caller (build_serve_scheduler)
        # separately replayed into this projection after construction.
        boot_events = event_log.replay() if event_log is not None else []
        self._memory_projection = project_events(boot_events)
        # 投影折到哪一条了。**投影是派生的,不是原始状态** —— 所以它不进 Redis:
        # 存一份派生数据的唯一后果,是多出一种"它和日志不一致"的坏法。正确的做法是
        # 让它跟上日志(`catch_up_projection`),而日志本来就是共享的。
        self._projection_seq = max((int(e.seq or 0) for e in boot_events), default=0)
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
        # 这一个 World 的身份(占用戳用)。见 `another_runner`:pid 认不出
        # "本进程里另一个 World",而那正好是要分开的两种情况之一。
        self._owner_token = uuid.uuid4().hex
        # 这一趟有没有盖过"从哪条事件之后开始跑"那个戳(见 `RUN_SINCE_SEQ`)。
        self._run_marked = False
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
        # 每个人**最近一次走完的路**:agent -> {from, to, tick}。派生数据、只在内存里,
        # 和 `_transit` 同一个性质(重启丢掉正好:那时没有进行中的对话)。
        # 用途只有一个 —— 聊天那层要说得出"你刚从哪儿走到哪儿",见 `_land_arrivals`。
        self._last_arrival: dict[str, dict[str, Any]] = {}
        # 子系统档位:name -> {ok, degraded, status, reason}。切换时发 subsystem_health 事件。
        self._subsystem_health: dict[str, dict[str, Any]] = {}
        # 在场玩家名单的来源(World 注入)。scheduler 不认识 World,只认识这个回调。
        self._present_players: Any | None = None
        # 在场玩家**此刻在做什么**的来源(World 注入,见 `World.player_doing`)。
        # 每 tick 取一次快照,规律层的 `action` 选择器读那份快照 —— 逐条规律去问
        # 的话,一个有十几条 `{"action": …}` 的世界每 tick 要多问十几趟在场名册。
        self._players_doing: Any | None = None
        self._player_action_now: dict[str, str] = {}
        # chat-agent:聊天的当前值存储(World 注入)。只为一件事:到点把
        # "等会儿再说"兑现成一次敲门。没有它世界照跑,那条约就永远不到期。
        self.chat_state: Any | None = None
        # autonomy:定时轮次的回调(World 注入)。`hook(agent_ids, world_time)`,
        # 必须立刻返回 —— 决定与执行都在世界自己那条事件循环上跑。
        self._autonomy_hook: Any | None = None
        self._autonomy_interval: int = 0   # 0 = 关着
        # contact:"她想起某个不在跟前的玩家"的回调(World 注入),和 autonomy
        # 同一个形状、**另一条节流**。为什么不合成一条见 `contact.py` 的模块说明:
        # 候选集互补、额度两本账、判定的主语不同、节奏不同。
        self._contact_hook: Any | None = None
        self._contact_interval: int = 0    # 0 = 关着
        self.contact_store: Any | None = None
        # world-rules:世界的规律(数据)与它们作用的存量。规律是纯算术,跑在 tick 上。
        self.stock_store: Any | None = None
        self.visibility_store: Any | None = None   # 可见性声明 + 东西在哪(perception)
        self.world_rules: list[Any] = []
        # 插件(3.8.0)。`plugins` 是**这一次开机装着的那几个**(声明的权威是世界
        # 文件,库里那份 `:plugins` 只记"装的是哪一版、有哪几个事实名")。
        self.plugin_store: Any | None = None
        self.edge_store: Any | None = None
        #: 边类型名(带命名空间)→ 它的声明。`link` 那一刻查约束靠它。
        self.edge_types: dict[str, Any] = {}
        #: `(种类 id, 动词) → 这个动词的边效果`(`link`/`unlink`/`transfer`)。
        #: 挂在这儿而不是挂在 affordance 上,理由见 `__main__._install_plugins`。
        self.verb_edge_effects: dict[tuple[str, str], tuple[dict[str, Any], ...]] = {}
        #: 声明了 `mode:"projected"` 的事实:`存储键 → 那条 delta 事件的 type`。
        #: 空的时候这一层整个不花钱 —— 和"声明本身就是开关"逐字同构。
        self.projected_facts: dict[str, str] = {}
        self.plugins: list[Any] = []
        # 事件类型 → 订它的触发器。**建一次,tick 上只做一次字典查** ——
        # 每 tick 遍历全部触发器去比 type,是这一层最容易写成 O(触发器×事件) 的地方。
        self._triggers_by_event: dict[str, list[Any]] = {}
        # 哪几条规律是插件带来的。**`world_rules` 里两种混在一起跑是对的**
        # (同一个引擎、同一份双缓冲、同一条水位),而**读出口要分得开** ——
        # `World.rules()` 是作者问"我这个世界写了哪些规律"的地方,把插件那七条
        # 混进去,他会去找七条他从没写过的规律。插件那一份问 `plugin list`。
        self.plugin_rule_ids: set[str] = set()
        # 🔴 **队列在 tick 开头快照、drain 一遍;触发器自己 emit 的落进下一 tick。**
        # 这条是这一层的"双缓冲",理由和规律那一层逐字相同:同轮递归让"A 先跑还是
        # B 先跑"变成隐藏的语义,而两个互相 emit 的触发器会当场把 tick 线程转死 ——
        # 而时钟卡住的样子是整个世界停了,没有一处报错。
        self._trigger_queue: list[dict[str, Any]] = []
        # 本体:这个世界里能有什么东西。`None` = 作者没声明过种类,这一层不启用
        # (声明本身就是开关,和认知层同构)。
        self.ontology: Any | None = None
        self.ontology_store: Any | None = None
        # 她正在做的**长过程**:field = `agent|target|verb`,值是一条记录
        # (`_engagement_key` / `_engage`)。真状态,所以在 `__main__` 里换成 RedisDict ——
        # 十月怀胎横跨的重启次数不由引擎决定,内存态等于每次重启都流产一次。
        self._engaged: Any = {}
        # 手里这份实例表是哪个版本的(见 `_sync_entities`)。别的进程种下的树,
        # 这个进程也得看得见 —— 实例可以运行期长出来,而种类是冻的。
        self._entities_rev: int = 0
        # 规律上次算是什么时候。**落库**(`:meta` 的 `rule_marks` 行),不是内存态。
        #
        # 它曾经是内存态,理由写在 `stocks.evaluate_due` 的 docstring 上:"只决定
        # 要不要现在算,不影响结果 —— 结果由 dt 定,重启清空最多多算一次,值不会错"。
        # **那句话只对 dt 化的表达式成立。** 一条常数步长的规律(`雨天数 + 1`、
        # `江水位 + 雨势*0.9`)每多算一次就真的多走一步,而"多算一次"的次数由**运维
        # 重启了几次**决定 —— 世界的物理法则从此挂在部署节奏上。
        #
        # 线上那个雨季世界就是这么坏的:调试期滚动重启,每次重启多烧一整天的雨,
        # 实测雨天数 56 而按时钟应为 ~50;洪水本该第 3.2 天、实际发生在第 1 天,
        # 此后水位顶死 clamp 上限 17 个世界日 —— 唯一的叙事高潮烧在了没人看的第一天,
        # 而**日志一条错都没有**。
        #
        # 水位只对 `every > 1` 的规律有意义(每 tick 都算的规律没有节流可丢),
        # 所以落库的也只有那些 —— 见 `_persist_rule_marks`。
        self._rule_last_run: dict[str, int] = {}
        self._rule_marks_loaded: bool = False       # 这个进程 hydrate 过了吗
        self._rule_marks_saved: dict[str, int] = {} # 我们相信 Redis 里躺着的那份
        self._rule_stats: dict[str, Any] = {
            "evaluated": 0, "written": 0, "emitted": 0, "skipped": 0, "last_error": None,
        }
        # 她的树问到了哪些量(见 `_stock_watches`):agent_id -> (树对象, 量的列表)。
        # 树换了(`build_tree` 重建)`id()` 就变,缓存自然失效。
        self._stock_watch_cache: dict[str, tuple[Any, tuple[tuple[str, str], ...]]] = {}
        self._stock_watch_warned: set[tuple[str, str, str, str]] = set()
        # 她身上有没有别人看得见的量(本体加载完就定死),以及她上次被放在哪 ——
        # 位置没变时不必再写一次(见 `_settle_actor_place`)。上路的人记
        # `_NOWHERE`,那也是一个"写过了"的状态,不是"没写过"。
        # 玩家那一半记的是 `(地点, 称呼)`:名字也会变,而只比地点的话
        # perception 会一直叫他上一次落地时的那个称呼(`_settle_player_places`)。
        self._actor_visible_cache: bool | None = None
        self._actor_placed: dict[str, str | tuple[str, str]] = {}
        # 上一次扫幽灵时的在场名册。`None` = 这个进程还没扫过 —— 开机那一次
        # 非扫不可,要还的正是上一个进程落下的账。
        self._swept_roster: set[str] | None = None
        # (角色, 玩家) -> 上次打招呼的世界日。一天一次,不是每 tick 一次。
        self._hailed: dict[tuple[str, str], int] = {}
        # 本世界日各人上了多久班(tick)。日切结算工资时清空。
        self._worked_ticks: dict[str, int] = {}
        # economy-v4: per-day sales counter feeding the price drift. Memory
        # only — prices in shop_stock are the durable part.
        self._shop_sales: dict[tuple[str, str], int] = {}
        # social-v5: one gossip roll per (speaker, listener) per world day.
        # Memory only — resets at rollover and on restart.
        self._gossip_rolled: set[tuple[str, str, int]] = set()
        # (她, 他, 世界日) → 她今天已经开口约过他几次。**这是「像个人」和「像推送」
        # 的分界**:一个不设上限的邀请者在玩家眼里就是一条推送。
        #
        # **进程态,和 `_gossip_rolled` 逐字同一条**:上限是礼貌,不是账 ——
        # 重启一次让她今天多问一句,没有任何人受损;而为了它去开一个 Redis 键,
        # 换来的是一个要跨进程一致、要进 `.cyberworld`、要跟着法务抹除走的新状态。
        # 键里带着世界日,所以它也自己过期(日切那一下顺手清)。
        self._invited_today: dict[tuple[str, str, int], int] = {}
        # memory-2.0: reflection watermark, hydrated from reflection_state on
        # first touch. Kept in memory so the per-memory path stays db-free;
        # `_reflection_dirty` is what still needs a checkpoint.
        self._reflection_watermark: dict[str, float] = {}
        self._reflection_dirty: set[str] = set()
        self.planner = planner
        self._plans: dict[str, Any] = {}          # agent_id → Plan (cache of the `plan` event)
        self._planning: set[str] = set()          # agents with a replan in flight
        self._plan_attempts: dict[str, int] = {}  # agent_id → 已经替哪一天试过了
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
            cap = MAX_AGENTS
            if self.config_store is not None:
                cap = int(self.config_store.get("scheduler.max_agents", default=cap))
            if cap > 0 and len(self.agents) >= cap:
                raise RuntimeError(
                    f"agent cap reached ({cap}) — raise it with "
                    f"`config set scheduler.max_agents N` (0 = unlimited)")
            self.agents[brain.agent.id] = brain
            self.seed_actor_quantities(brain.agent.id)

    @staticmethod
    def stock_owner_of(actor_id: str) -> str:
        """施动者 id → 他身上那些量的 owner key。

        角色是 `agent:夏`,玩家是 `agent:player:p1` —— **同一个命名空间**,因为
        `me_*` 读的是"一个人身上的量",而这件事对两种人是同一件。分两个前缀的话
        `requires` / `costs` / 感知三处都要各判一次"这是谁",而漏掉任何一处的
        样子都是安静的:门永远开着,或者他的体力永远不掉。
        """
        return f"agent:{actor_id}"

    def item_name_of(self, item_id: str) -> str:
        """一样东西的人话名字,查不到就回空 —— 拒绝那句话拿它翻译 id。

        没接经济层的世界不该因此掀翻一次能力调用:查不到只是少一句人话,
        而 `apply_affordance` 已经会退回 id。
        """
        store = self.economy_store
        if store is None:
            return ""
        try:
            return store.name_of(item_id)
        except Exception:  # noqa: BLE001 - 一句人话不值一次崩溃
            logger.warning("could not read item name for %r", item_id, exc_info=True)
            return ""

    def quantity_label_for(
        self, target: str, actor_owner: str
    ) -> Callable[[str, str], str]:
        """一条表达式里的量该被念成什么 —— 交给 `apply_affordance` 拼拒绝语。

        三个作用域各查各的主人:裸名字查**这个东西**,`me_` 查**这个人**,
        `world_` 查世界那一份。合成一张全世界的表会踩本体层早就踩过的坑 ——
        两个种类各有一个「新鲜度」时,全局表只留得下一个。

        ⚠️ **查的是可见性表,不是本体** —— 屏幕上那行字(`perception` 的
        `labels_map`)念的就是它。本体那份是**上游**:`_apply_ontology` 装载时把
        种类声明播进可见性表,所以可见性表是并集,而并集里多出来的两样恰好是最
        容易撞见的 —— 世界自己那一份量(作者写在 `stock_visibility` 里,从来不属于
        任何种类,查本体永远是空),以及事后 `declare_visibility` 改过的量。
        照本体查的话这两样都会退回内部键:玩家在菜单上读到「江水位(米)」,点下去
        被拒绝成「世界的江水位」—— 同一个量两个名字,而这正是这条规矩要挡的事。
        """
        from anima_world.ontology import owner_kind

        owners = {"": target, "me": actor_owner, "world": "world"}

        store = self.visibility_store
        rules = getattr(store, "labels_map", None) if store is not None else None
        table = rules() if callable(rules) else {}

        def label(scope: str, key: str) -> str:
            owner = owners.get(scope)
            if owner is None:
                return ""
            return table.get((owner_kind(owner), key), "")

        return label

    def quantity_bands_for(
        self, target: str, actor_owner: str
    ) -> Callable[[str, str], Any]:
        """一条表达式里的阈值该被念成哪一档 —— 和 `quantity_label_for` 同一份声明、
        同一张表(可见性表的 `bands_map`),只是取另一格。作用域的分法逐字相同。

        分成两个函数而不是让一个返回 `(label, bands)`,是因为拒绝语那边**两样各管
        一处**:名字换在名字上,档词换在比较另一头的那个数上(`speak_expression`)。
        """
        from anima_world.ontology import owner_kind

        owners = {"": target, "me": actor_owner, "world": "world"}

        store = self.visibility_store
        rules = getattr(store, "bands_map", None) if store is not None else None
        table = rules() if callable(rules) else {}

        def bands(scope: str, key: str) -> Any:
            owner = owners.get(scope)
            if owner is None:
                return None
            return table.get((owner_kind(owner), key))

        return bands

    def seed_actor_quantities(self, actor_id: str) -> None:
        """这个人身上声明过的量在这儿落地。**只填缺,不覆盖**(锁内)。

        为什么落在 `register` 而不是开机那一段:角色不是 `ontology.entities` 的成员
        (她的元数据归 Brain / 黑板),而**她可以在世界跑起来之后才出现** —— 节拍
        导演的 `agent_join`、重启后的中途加入,都不经过开机那条路。`register` 是
        这几条路唯一共同的窄口,所以那条不变量("一个实体存在,它声明过的量就存在")
        钉在这里才真的成立。

        **玩家走的是另一个窄口**(`World._touch_player`),但用的是这一份:他也要
        有力气才擦得动窗。少了这一步的样子不是"他被拒绝",是 `me_体力` 恒为 0 ——
        世界里每一件要力气的事他都做不了,而回执只说"你做不了",一个字不提原因。

        整份写回会把跑了三十天的人倒带回创世体力(创世那条纪律踩过两次)。
        """
        # **插件那几格先种,而且不受本体那道早退管**(3.8.0):一个没写 `kinds` 的
        # 世界 `ontology` 是 None —— 本体层整个缺席是对的,而插件层和它无关。
        # 把这一句放在早退后面的下场是安静的:`me_qi.灵力` 恒为 0,他做什么都被拒,
        # 而回执只说"你做不了"。
        self.seed_actor_plugin_facts(actor_id)
        ontology = self.ontology
        store = self.stock_store
        if ontology is None or store is None:
            return
        from anima_world.ontology import actor_quantities

        declared = actor_quantities(ontology)
        if not declared:
            return
        owner = self.stock_owner_of(actor_id)
        have = store.of(owner)
        # 逐个量填,不是逐个人填 —— 加了一个新属性的世界重启时,老角色只会补上新的
        # 那一个,而不是被跳过(跳过的话 `me_手艺` 恒为 0,门永远关着)。
        missing = {k: v for k, v in declared.items() if k not in have}
        if missing:
            store.set_many(owner, missing, tick=int(self.clock))

    def seed_actor_plugin_facts(self, actor_id: str) -> None:
        """挂在 `agent` 身上的**插件事实**在这儿落地。只填缺,不覆盖(锁内)。

        为什么和本体那一半挤在同一个窄口:**理由逐字相同**。角色可以在世界跑起来
        之后才出现(节拍 `agent_join`、重启中途加入),玩家走的是另一条窄口
        (`World._touch_player`)而用的是这同一份 —— 装载那一遍只装得到"已经在册的",
        后来的人一个都盖不着。少了这一步的样子不是"他被拒绝",是 `me_qi.灵力`
        恒为 0:世界里每一件要灵力的事他都做不了,而回执只说"你做不了"。
        """
        store = self.stock_store
        if store is None or not self.plugins:
            return
        owner = self.stock_owner_of(actor_id)
        have = store.of(owner)
        # `actor` = 角色 + 玩家(今天的语义);`player` = 只玩家。**分它们的是那个
        # 前缀,不是两张表** —— 两种人的量住在同一个命名空间里。
        is_player = actor_id.startswith(self.PLAYER_PREFIX)
        wanted = {"actor", "player"} if is_player else {"actor"}
        missing = {
            fact.qualified: fact.default
            for plugin in self.plugins
            for fact in plugin.facts.values()
            if fact.bearer in wanted and fact.qualified not in have
        }
        if missing:
            store.set_many(owner, missing, tick=int(self.clock))

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

    def _record_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Append to bounded recent_events buffer and signal listeners.

        返回**落库之后**的那一份(带 `seq`)。调用方传进来的 dict 不会被改写 ——
        这里拷了一份,而那正是"投影拷一份"同一条纪律在发射端的样子。
        """
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
            # **插件的触发器在这里入队** —— 落库那一处,也就是"发生过"在这个引擎里
            # 的定义。挂在两个包装函数中的一个上,等于让"订得到吗"取决于发它的人
            # 碰巧调了哪一个(第 1 期就是这么让五种事件死掉的,见
            # `_enqueue_for_triggers` 的说明)。
            self._enqueue_for_triggers(event)
            self.recent_events.append(self._stream_event(event))
            self._event_signal.set()
            self._event_signal.clear()
            return event

    def _apply_memory_trigger(self, event: dict[str, Any]) -> None:
        """M4: promote memory-worthy events, then fold the event into the
        internal projection kept for computing future trigger deltas.

        A no-op unless both `trigger_engine` and `memory_store` are wired up.
        """
        # **先补空档,再动这条。** 别的进程可能刚往同一条日志里写过一批,而这一整个
        # 方法(触发器的 delta、`_apply_item_restores`、关系跃迁)都是拿
        # `_memory_projection` 当"现在"来算的 —— 补在后面等于这一轮对着一份过期的
        # 现在做判断。理由的另一半见 `_fold_gap_before`。
        self._fold_gap_before(int(event.get("seq") or 0))
        # llm-relationship-judge: memory_seed is an EXPLICIT memory
        # declaration (judge chat summaries at runtime, seed injection at
        # genesis) — folded directly, bypassing TriggerEngine, with the same
        # branch _rebuild_memories' closure uses. Live path and rebuild path
        # are symmetric from here on (rich-injection only covered rebuild).
        if event.get("type") == "item_consume":
            self._apply_item_restores(event)
        if event.get("type") == "memory_seed" and self.memory_store is not None:
            payload = event.get("payload") or {}
            agent_id = payload.get("agent_id")
            if not agent_id:
                logger.warning("memory_seed event has no agent_id; skipping")
            else:
                kind = payload.get("kind", "seed")
                summary = payload.get("summary", "")
                importance = float(payload.get("importance", 0.5))
                anchor = bool(payload.get("anchor", False))
                # R3:她是怎么知道这件事的。写事件的一方可以显式说,**没说就交给
                # store 按 kind 判**(连转成字符串一起)—— 两个后端的 `add()` 都
                # 已经是 `str(provenance or provenance_of(kind))`,这里再兜一次就是
                # 同一条规则在两处各写一遍,而分叉的那一天没有任何一处会报错。
                # (下面 `TriggerEngine` 那一支同一条,理由写在那儿。)
                provenance = payload.get("provenance")
                # **`memory_seed` 不过准入闸(R4)。** 它是一条**显式声明** ——
                # 世界(作者的创世记忆、关系判定、八卦反应、反思)明说"记住这件事",
                # 而它本来就绕开触发器,理由见上面那段注释。准入闸是对**引擎自己的
                # 推断**的一次再判断("这类事件值得记" → "这一条值得吗");一条声明
                # 上面没有可以再判的推断。
                #
                # 更要紧的是后果:世界明说要记而引擎悄悄不记,日志里有 `memory_seed`
                # 事件、记忆表里没有那一行、没有任何一处报错 —— 正是这个仓库最怕的
                # 那种坏法。`test_verb_writes` 当场逮到了它(`talk_to` 声明会写记忆,
                # 而两条 seed 都被拒了)。重复的八卦要治,治在它的源头(`_maybe_gossip`
                # 本来就有每日一次的闸),不是在落库这一下把一条声明吞掉。
                self.memory_store.add(
                    agent_id=agent_id,
                    tick=int(event.get("ts") or 0),
                    kind=kind,
                    summary=summary,
                    importance=importance,
                    anchor=anchor,
                    event_seq=event.get("seq"),
                    source_ids=payload.get("source_ids"),
                    provenance=provenance,
                )
                self._note_memory_written(agent_id, importance, kind)
            return
        if self.trigger_engine is not None and self.memory_store is not None:
            descriptor = self.trigger_engine.process(event, self._memory_projection)
            if descriptor is not None:
                admitted = self._admit_memory(
                    descriptor.agent_id, descriptor.summary, descriptor.kind,
                    float(descriptor.importance), descriptor.anchor,
                )
                if admitted:
                    self.memory_store.add(
                        agent_id=descriptor.agent_id,
                        tick=descriptor.tick,
                        kind=descriptor.kind,
                        summary=descriptor.summary,
                        importance=descriptor.importance,
                        anchor=descriptor.anchor,
                        event_seq=descriptor.event_seq,
                        # **触发器没说就交给 store 按 kind 判**,这里不再自己兜
                        # 一次:同一条规则在两处各写一遍,就是给它们分叉留位置。
                        # (原先这里写着 `or self._provenance_of(...)`,而
                        # `MemoryDescriptor.provenance` 那时默认 `"experienced"`
                        # —— 一个真值,于是那个 `or` 一次都没有生效过。)
                        provenance=descriptor.provenance,
                    )
                    self._note_memory_written(
                        descriptor.agent_id, float(descriptor.importance), descriptor.kind
                    )
                # **关系跃迁不受准入闸管。** 准入管的是"记不记得住",而这一步动的是
                # 关系图和 r_type —— 那是世界的状态,不是她的记忆。拒了一条摘要就
                # 顺手把关系也拒掉,是把两件事绑在一起,而绑错的那一次没有任何报错。
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
        self._projection_seq = max(self._projection_seq, int(ev.seq or 0))

    def _fold_gap_before(self, seq: int) -> None:
        """把水位和 `seq` 之间**别的进程写的**那一段补折进来。

        **水位前进的条件是"折过了",不是"我写了一条"。** `_projection_seq` 的含义
        是「≤ 它的事件我都折过了」,而这个进程自己追加一条时,日志里可能已经躺着
        别的进程写的一批。直接把水位挪到自己那条的 seq,等于替那一批签了字;而
        `catch_up_projection` 只往前看(`replay(since_seq=水位)`),于是它们**再也
        不会**被折进来。

        这不是竞态,是必然:一个跑着的世界每 tick 都在追加事件。线上 `night-tide`
        就是这么坏的 —— 从维护容器给四个角色写了角色卡,事件确实进了日志、回执写着
        `changed=True`,而那个长驻进程里的玩家永远看到 `card: null`。更贵的是
        `player forget`:**关系就是投影**(`_apply_player_departed`),共享 Redis 里
        的联系态清掉了而内存里那个幽灵留着,她继续惦记一个不存在的人、占着社交位。
        两处都零报错 —— 投影"少折了一条"和"那件事没发生"长得一模一样。

        和 `reset_projection` 那条("两个字段各写各的就是那个洞本身")是同一族的
        第二个洞:那一次是折了却不挪水位(事件折两遍),这一次是挪了水位却没折。

        两个落点上的判断:

        - **只在真有空档时才多跑一次 replay**(`seq > 水位 + 1` 这个比较是免费的)。
          无条件 replay 就是每条事件一次 Redis 往返 —— 修法退化成"每条事件都全量
          对账",比原来的 bug 更难查(它只是慢)。
        - **补空档时按 `seq` 截断,不折自己这一条**。它虽然已经在日志里了,但调用方
          随后还要拿它走触发器那条路并单独折一次;这里顺手带上就是折两遍(而
          `payment` / `item_transfer` 折两遍 = 账翻倍,同样不报错)。

        `seq <= 0` 的路径(没有日志、或调用方自带 seq 的合成事件)一概不碰:
        算不出空档就别猜一个出来。
        """
        if seq <= 0 or self.event_log is None or seq <= self._projection_seq + 1:
            return
        gap = [
            e for e in self.event_log.replay(since_seq=self._projection_seq)
            if 0 < int(e.seq or 0) < seq
        ]
        if not gap:
            return
        project_events(gap, base=self._memory_projection)
        self._projection_seq = max(int(e.seq or 0) for e in gap)

    # ── Reflection (memory-2.0) ────────────────────────────────────────────

    REFLECTION_KIND = "reflection"

    def note_subsystem(
        self, subsystem: str, ok: bool, reason: str = "", *, sticky: bool = False,
    ) -> None:
        """记一次子系统的成功/降级,并在**档位切换的那一刻**发一个事件。

        今天降级只在 stderr 刷一行 warning。日志会滚掉,而"这个世界当时跑在什么档位
        上"是解释它为什么长成这样的关键 —— 一个整整三天没有 planner 的世界,和一个
        角色确实无所事事的世界,产物看起来一模一样。

        只在**切换**时发事件,不是每次都发:一个持续降级的子系统会每 tick 触发一次,
        那样事件日志会被自己的健康报告淹掉(needs 那个教训)。计数照常累加。

        ⚠️ **`sticky=True` 的子系统统计的是"出过没出过",不是"此刻好不好"。**
        分界是**下一次成功还算不算平反**:planner 掉一次线、下一次通了,那盏灯就该
        灭 —— 它报的是"这会儿能不能用"。而"她起了个头就被带走了"是一件**已经发生
        过、赔不回来的事**(代价不退),下一件事顺利做完并不能把它抵消掉。
        不分开的话有两个坏法,实测都在:档位跟着每一件事来回翻,五件事发五个
        `subsystem_health` 事件(而这个函数存在的全部理由就是不淹日志);而且每次
        成功都把上一次掉线的 `reason` 抹成空串 —— `state()` 上于是留下一盏
        `status: "ok"` 而 `degraded: 3`、`reason: ""` 的灯:数字说出过三次事,
        而**是哪三件永远查不回来了**。粘住之后:红了就红着(至多一个事件),
        `reason` 留着最近那一件的名字;成功那一半只加 `ok`,**但第一次成功照旧
        把灯点成 `"ok"`** —— 见下面那一段。
        """
        health = self._subsystem_health.setdefault(
            subsystem, {"ok": 0, "degraded": 0, "status": None, "reason": ""}
        )
        health["ok" if ok else "degraded"] += 1
        if sticky and ok:
            if health["status"] is None:
                # **粘住的意思是"红了不许自己变绿",不是"绿这一档不存在"。**
                # 这里从前在设 `status` 之前就早退,于是一个从没出过事的世界这一格
                # 永远是 `null` —— 而 `null` 在 `state()` 上和"这个子系统压根没跑过"
                # 逐字相同,读的人分不出"没事"和"没跑"。更贵的是它是**一个既有字段
                # 的取值悄悄变了**(baseline 上是 `"ok"`):加一格下游看不见,改一格
                # 下游的判断当场错,而两者都不报错。
                health["status"] = "ok"
            return
        status = "ok" if ok else "degraded"
        if health["status"] == status:
            if sticky and not ok:
                # 灯已经红着,再掉一件只更新"最近是哪一件"—— 不再发第二个事件。
                health["reason"] = reason
            return
        previous, health["status"], health["reason"] = health["status"], status, reason
        if previous is None and ok:
            return  # 开机第一次成功不值一个事件
        self._record_event({
            "type": "subsystem_health",
            "who": None,
            "payload": {"subsystem": subsystem, "status": status,
                        "reason": reason, "previous": previous},
        })

    def subsystem_health(self) -> dict[str, dict[str, Any]]:
        """各子系统的当前档位与累计计数(给 `World.state()` 用)。"""
        return {name: dict(data) for name, data in self._subsystem_health.items()}

    # ── R3 记忆分型 / R4 准入闸 / R5 夜间固化 ─────────────────────────────

    #: kind → 她是怎么知道这件事的。**这里只是引用**:表住在
    #: `memory_store.PROVENANCE_BY_KIND`,因为两个后端的读侧也要拿它给老行补出处,
    #: 而 store 不认识 scheduler(抄一份过去 = 同一条记忆两种出处)。
    PROVENANCE_BY_KIND = memory_store_mod.PROVENANCE_BY_KIND
    #: ⚠️ 这里曾经还有一个 `_provenance_of()` 转发方法,两条写侧各调它兜一次
    #: 默认值 —— 而两个 store 的 `add()` 早就在做同一件事。留着一个"想兜就兜得到"
    #: 的转发口,等于让"没说就按 kind 判"这条规则随时可以变成两处实现,
    #: 而它们分叉的那一天不会有任何一处报错。要判就问 `memory_store.provenance_of()`。

    def _admit_memory(self, agent_id: str, summary: str, kind: str,
                      importance: float, anchor: bool) -> bool:
        """这一条该不该进她的记忆(R4)。**开关关着时一律放行** —— 逐位退回从前。

        闸在这里而不在触发器里:触发器答的是"这类事件配不配",这一层答的是
        "**这一条**配不配"(第七次「在吗」不配)。拒了要**说出来**:一条静默丢掉的
        记忆查不了,作者只会看到"她怎么不记得这件事",而日志里什么都没有。

        ⚠️ **只管引擎自己推断出来的那条路**(`TriggerEngine`),不管 `memory_seed` ——
        那是世界的**显式声明**,理由见 `_apply_memory_trigger` 里那一段。
        """
        if self.memory_store is None:
            return False
        if self.config_store is None or not self.config_store.get(
            "memory.admission.enabled", default=False
        ):
            return True
        from anima_world import memory_admission

        threshold = float(self.config_store.get(
            "memory.admission.threshold",
            default=memory_admission.DEFAULT_THRESHOLD,
        ))
        try:
            existing = self.memory_store.query(agent_id=agent_id)
        except Exception:  # noqa: BLE001 - 读不到已有记忆不该让这条丢掉
            logger.warning("准入闸读不到 %s 的已有记忆,这一条放行", agent_id, exc_info=True)
            return True
        verdict = memory_admission.judge(
            summary, kind=kind, importance=importance, anchor=anchor,
            existing=existing, threshold=threshold,
        )
        self.note_subsystem("memory_admission", True, "")
        if not verdict.admit:
            logger.info(
                "记忆准入拒了 %s 的一条 %s(%.3f):%s —— %r",
                agent_id, kind, verdict.score, verdict.reason, summary[:40],
            )
            # **拒了几条要数得出来。** 档位仍是 `ok` —— 闸拦下一条复读是它在正常
            # 工作,报 degraded 等于给 `state()` 点一盏假的红灯(而 `note_subsystem`
            # 只在切档时发事件,那盏灯还会来回闪)。所以另加两格:拒过多少条、
            # 最后一条是为什么被拒的。此前两支写的是同一行,于是 `state()` 上
            # 看不出这道闸到底拦过没有 —— 而"开了闸但一条都没拦"和"拦掉了半个
            # 世界"在读数上必须分得开。
            health = self._subsystem_health["memory_admission"]
            health["refused"] = int(health.get("refused") or 0) + 1
            health["last_refusal"] = verdict.reason
        return verdict.admit

    def consolidate_memories(self, *, now_tick: int | None = None) -> dict[str, Any]:
        """夜间固化(R5):**趁她睡着的时候整理记忆。**

        做三件事,每个角色各做一遍:衰减一轮(该淡的淡下去)、把弱到看不见的行清掉、
        然后让攒够的那些去反思。这三样引擎本来就有,缺的只是**一个该做这件事的时刻** ——
        而这个世界自己有夜晚,那正是它。

        为什么挂在夜里而不是每 tick 做:遗忘曲线是按世界日算的,每 tick 跑一遍
        既贵又没有意义;更要紧的是**反思是一次 LLM 调用**,跟着白天的对话一起跑会
        和她正在说的话抢线程。离线固化(sleep-time compute)这条在外面已经被反复
        验证,而这个引擎的好处是它不必假装有"夜晚"——世界里真的有。

        **默认关**(`memory.consolidation.enabled`):它会改变记忆的留存,是行为变更。
        开着的时候它**接管**两样东西(`_on_day_rollover` 是 if/else,不是两段都跑):

        - **日切的衰减** —— `decay_pass` 不幂等,跑两遍是平方,理由见那里。
        - **反思的时刻** —— 白天那条路(`_note_memory_written`)在开着固化的世界里
          只攒不发,越过阈值的那一次留到夜里。这正是 R5 存在的理由本身(反思是一次
          LLM 调用,跟白天的对话一起跑会和她正在说的话抢线程),所以"挪到夜里"是
          它的内容,不是副作用。

        ⚠️ **门是 `memory.reflection_threshold`,不是"水位大于 0"。** 后者的意思是
        "今天写过任何一条记忆的人今晚都反思一次" —— 每人每世界日一次 LLM 调用,而且
        它把没攒够的水位**清成 0**,于是那条阈值路在开着固化的世界里等于被停掉:
        两个机制,一个安静地废掉另一个,读数上还都对。攒不够的留着攒,明天接着攒。
        返回 `{agents, decayed, pruned, reflections}`,零成本可观测(和
        `rule_stats()` / `autonomy_stats()` 同一条:最容易的坏法是看着都对、其实一次没跑)。
        """
        out = {"agents": 0, "decayed": 0, "pruned": 0, "reflections": 0}
        if self.memory_store is None:
            return out
        tick = int(self.clock if now_tick is None else now_tick)
        ticks_per_day = max(1, 1440 // self._minutes_per_tick())
        floor = 0.0
        if self.config_store is not None:
            floor = float(self.config_store.get(
                "memory.consolidation.prune_below", default=0.0))
        threshold = self._reflection_threshold()
        # 缺 `decay_pass` 的 store 上这一轮只做清扫与反思。**判断在循环外** ——
        # 放在里面的话每个角色都会记一条 warning,而缺的是同一个方法;更坏的是
        # 老代码里那个 `hasattr` 一并管着 `continue`,于是清扫和反思跟着一起被跳过。
        can_decay = hasattr(self.memory_store, "decay_pass")
        if not can_decay:
            logger.warning("固化:这个记忆后端没有 decay_pass,这一轮只清扫与反思")
        for agent_id in list(self.agents):
            out["agents"] += 1
            if can_decay:
                try:
                    self.memory_store.decay_pass(agent_id, tick, ticks_per_day)
                    out["decayed"] += 1
                except Exception:  # noqa: BLE001 - 一个角色的固化失败不该停掉整轮
                    logger.warning("固化:%s 的衰减失败", agent_id, exc_info=True)
                    continue
            if floor > 0.0:
                # **锚定的永不清** —— 创世记忆是她是谁的一部分(和淘汰同一条纪律)。
                for row in self.memory_store.query(agent_id=agent_id):
                    if row.get("anchor"):
                        continue
                    if float(row.get("strength") or 1.0) < floor:
                        if self.memory_store.forget_memory(int(row["id"])):
                            out["pruned"] += 1
            if self._watermark_of(agent_id) < threshold:
                continue                # 还没攒够 —— 留着,明天接着攒
            self._reflection_watermark[agent_id] = 0.0
            self._reflection_dirty.discard(agent_id)
            if self.reflection_store is not None:
                self.reflection_store.reset(agent_id, tick)
            # **只在真的交出去之后才计数**:`_submit_reflection` 在没有 reflector /
            # 线程池 / 已停机时是空转,而报一个"反思了 3 次"的读数,正是
            # `rule_stats()` 那条纪律要防的坏法 —— 看着都对、其实一次没跑。
            if self._submit_reflection(agent_id):
                out["reflections"] += 1
        self.note_subsystem("memory_consolidation", True, "")
        return out

    def _reflection_threshold(self) -> float:
        """攒到多少重要度才值一次反思。**白天与夜里读同一格** —— 两个阈值迟早
        给出两种答案,而"她为什么这时候反思"就没有一个答案了。"""
        threshold = 3.0
        if self.config_store is not None:
            threshold = float(self.config_store.get(
                "memory.reflection_threshold", default=threshold))
        return threshold

    def _consolidation_enabled(self) -> bool:
        return bool(
            self.config_store is not None
            and self.config_store.get("memory.consolidation.enabled", default=False)
        )

    def _watermark_of(self, agent_id: str) -> float:
        """她攒到哪儿了。**没读过就先从库里取一次** —— 水位落过盘(重启接着攒),
        而只看进程内存的版本会让一次重启把攒了半天的水位读成 0。"""
        if agent_id not in self._reflection_watermark and self.reflection_store is not None:
            self._reflection_watermark[agent_id] = self.reflection_store.get(agent_id)
        return float(self._reflection_watermark.get(agent_id, 0.0))

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

        ⚠️ **开了夜间固化的世界只攒不发**(R5):越过阈值的那一次留到日切,由
        `consolidate_memories()` 交出去。这是 R5 的内容本身 —— 它存在的理由就是
        "反思是一次 LLM 调用,别跟她正在说的话抢线程"。阈值一个字没变,变的只是
        它在哪一刻兑现。
        """
        if (
            kind == self.REFLECTION_KIND
            or self.reflector is None
            or self._judge_pool is None
            or self.event_log is None
        ):
            return
        threshold = self._reflection_threshold()
        if agent_id not in self._reflection_watermark:
            self._reflection_watermark[agent_id] = self.reflection_store.get(agent_id)
        total = self._reflection_watermark[agent_id] + max(0.0, importance)
        if total < threshold or self._consolidation_enabled():
            self._reflection_watermark[agent_id] = total
            self._reflection_dirty.add(agent_id)
            return
        self._reflection_watermark[agent_id] = 0.0
        self._reflection_dirty.discard(agent_id)
        self.reflection_store.reset(agent_id, self.clock)
        self._submit_reflection(agent_id)

    def reset_projection(self, events: list[Any]) -> None:
        """从头重折一遍投影,**并且把水位一起挪到位**。

        水位(`_projection_seq`)是"我已经折到第几条了"。重折而不挪水位,投影里
        已经有了那些事件、水位却还停在 0 —— 于是下一次 `catch_up_projection` 会把
        它们**再折一遍**。这不是理论上的:创世那条路正是这么走的(建 Scheduler 时
        日志还空着 → 写创世事件 → 重折),于是一个刚建好的世界在第一次
        `World.act()` 时,每个人的钱和随身物品**当场翻倍**,一次,而且只在创建它的
        那个进程里。日志没错、重开一次就正常,所以从账面上永远看不出来。

        两个字段各写各的是那个洞本身,所以这里把它们焊死在一个方法里。
        """
        self._memory_projection = project_events(events)
        self._projection_seq = max((int(e.seq or 0) for e in events), default=0)

    def catch_up_projection(self) -> int:
        """把别的进程写进日志、而这个进程还没折进来的事件补上。

        多进程下这一步是必需的:进程 A 记了一条 `payment`,进程 B 的投影里那笔钱
        还没动 —— 而 B 正是靠投影判断"她买得起吗"。**投影不进 Redis**(见
        `_projection_seq` 那条注释):派生数据存两份只会多一种不一致的坏法,
        而事件日志本身已经是共享的,重折是廉价且必然正确的。

        ⚠️ **水位前进的条件是"折过了",不是"我写了一条"。** 这一步只往前看
        (`since_seq=_projection_seq`),所以任何一处把水位推过没折过的事件,那些
        事件就永远补不回来了 —— 自己追加时的那个洞见 `_fold_gap_before`。往
        `_projection_seq` 上写值的地方只该有三处:开机(`__init__`)、整份重折
        (`reset_projection`)、以及这两条真的折过之后。

        ⚠️ **而水位只许往前,不许往回。** 这一整步是四拍("读水位 → replay → 折 →
        写水位"),**要在 `_lock` 下调**(`state()` / `act()` 就是这么写的)。不在
        锁下的话,tick 线程可能在第二拍和第四拍之间自己追加并折了一条 —— 于是这里
        拿着一批**已经被折过**的 `fresh`,把水位从 tick 线程刚推到的位置**按回去**。
        被按回去的那一段下一次 `catch_up_projection` 会再折一遍,而
        `payment` / `item_transfer` 折两遍 = **账翻倍,零报错**。

        所以这里写的是 `max(旧水位, …)`(和 `_apply_memory_trigger` 尾部同一形状)。
        少了这个 `max`,漏掉一处锁就不只是"这一次多折一遍",而是把水位**永久**留在
        一个偏低的位置,后面每一条都跟着再折一遍 —— 一处漏锁变成一族回拉。
        锁是纪律,`max` 是那道兜底的闸:**它单点挡住整族**。
        (往回倒带只有一条合法的路:`reset_projection`,它是连投影一起从头重建的。)

        返回补进来了多少条。没有新事件时是纯读一次 db。
        """
        if self.event_log is None:
            return 0
        fresh = self.event_log.replay(since_seq=self._projection_seq)
        if not fresh:
            return 0
        project_events(fresh, base=self._memory_projection)
        self._projection_seq = max(
            self._projection_seq, max(int(e.seq or 0) for e in fresh)
        )
        return len(fresh)

    def _clock_box(self) -> Any:
        """时钟那个盒子,没有就现造一个。

        **属性必须是全函数**:测试里会 `Scheduler.__new__(Scheduler)` 绕过 `__init__`
        再设 `clock`(`_gossip_seed` 的跨进程稳定性就是这么验的)。此前 `clock` 是个
        普通字段,那样用没问题;换成属性之后不兜底就会 AttributeError ——
        换实现不该让"绕过 __init__"这种正当用法炸掉。
        """
        from anima_world.redis_state import ClockStore

        box = self.__dict__.get("_clock_store")
        if box is None:
            box = ClockStore(0)
            self.__dict__["_clock_store"] = box
        return box

    @property
    def clock(self) -> int:
        return self._clock_box().get()

    @clock.setter
    def clock(self, value: int) -> None:
        self._clock_box().set(int(value))

    def _persist_clock(self) -> None:
        """时钟的持久化归 `RedisClock` 自己:每次 set 就是一次落盘。

        world.db 时代这里有一个 db_meta 检查点(安静的夜晚不留事件,时钟只能
        从检查点恢复)。RedisClock 每写即持久,检查点与恢复整个不再需要 ——
        方法留空是因为 `checkpoint()` 的清单还点到它。
        """

    def claim_ownership(self) -> None:
        """在世界的 `:meta` 里盖一个"这个世界正被我跑着"的戳。

        **这是给人看的提示,不是锁**(跨进程互斥归 RedisLock)。进程崩掉标记就会
        陈旧,而拿陈旧标记去拒绝操作,等于在真出事那天把人挡在门外。
        """
        self._write_meta("owner_pid", str(os.getpid()))
        self._write_meta("owner_host", socket.gethostname())
        # **认的是这一个 World,不是这个进程。** 一个进程可以同时开两个 World
        # (宿主应用、测试、运维脚本都会),光比 pid 的话"另一个我"和"就是我"
        # 分不开 —— 而这两个的下场正好相反(该让它收尾 / 该自己收尾)。
        self._write_meta("owner_token", self._owner_token)

    def another_runner(self) -> str | None:
        """别人此刻正跑着这个世界吗?是的话给一句人话,否则 None。

        **只用来决定"要不要替它收尾",绝不用来拒绝操作** —— 上面那条纪律没变。
        判据是占用戳 + 活性:戳是我自己盖的 → 没别人;别的主机上的戳查不了活性,
        **当成有人在跑**(保守边是"晚一个 idle_timeout 收尾",另一边是"把玩家
        说到一半的话掐了",两者不对称);同一台机器上按 pid 探活性。正常关闭
        撤过戳,所以真的崩溃遗留在这儿看得出来。
        """
        store = self.meta_store
        if store is None:
            return None
        try:
            with self._lock:
                pid = store.get("owner_pid")
                host = store.get("owner_host")
                token = store.get("owner_token")
        except Exception:  # noqa: BLE001 - 读不到戳就当没人跑,照旧收尾
            logger.warning("could not read the world ownership marker", exc_info=True)
            return None
        if not pid:
            return None
        if token and str(token) == self._owner_token:
            return None                      # 就是我自己盖的戳
        if host and host != socket.gethostname():
            return f"{host} 上的进程 {pid}"
        if str(pid) == str(os.getpid()):
            return f"本进程里另一个 World({pid})"
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError, TypeError):
            return None                      # 进程没了 —— 戳陈旧,这个世界归我收尾
        return f"本机进程 {pid}"

    def release_ownership(self) -> None:
        """撤掉占用标记。正常关闭必须撤,否则每个关过的世界都变成"有人在跑"。"""
        if self.meta_store is None:
            return
        try:
            with self._lock:
                self.meta_store.drop("owner_pid")
                self.meta_store.drop("owner_host")
                self.meta_store.drop("owner_token")
        except Exception:  # noqa: BLE001 - 撤不掉标记不该拦住关停
            logger.warning("could not release the world ownership marker", exc_info=True)

    def _write_meta(self, key: str, value: str) -> None:
        if self.meta_store is None:
            return
        try:
            with self._lock:
                self.meta_store.put(key, value)
        except Exception:  # noqa: BLE001 - 一个提示标记永远不该是致命的
            logger.warning("could not write world meta %r", key, exc_info=True)

    def _persist_reflection_watermarks(self) -> None:
        """Checkpoint the in-memory watermarks (lock held). Best-effort: losing
        one only means a reflection arrives a little later."""
        if self.event_log is None or not self._reflection_dirty:
            return
        try:
            for agent_id in self._reflection_dirty:
                self.reflection_store.set(
                    agent_id, float(self._reflection_watermark.get(agent_id, 0.0))
                )
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

    def _submit_reflection(self, agent_id: str) -> bool:
        """Snapshot context under the lock, reflect on the judge pool.

        返回**真的交出去了没有** —— 调用方拿它计数(见 `consolidate_memories`):
        没有线程池、没有这个人、已经停机时这里是空转,而一个"反思了 3 次"的假读数
        比没有读数更坏。
        """
        brain = self.agents.get(agent_id)
        if brain is None or self.memory_store is None:
            return False
        recent = self.memory_store.query(agent_id=agent_id)[:10]
        context = {
            "name": brain.agent.name,
            "personality": brain.agent.blackboard.read("personality") or "",
            "memories": [(int(m["id"]), str(m["summary"])) for m in recent],
        }
        pool = self._judge_pool
        if pool is None or self._stopped:
            return False
        pool.submit(self._reflection_worker, agent_id, context)
        return True

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
                        # R3:反思是她**自己想出来的**,不是发生过的事。
                        "provenance": "believed",
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
                # 立起新边之前先撤掉**相反**的那一条。边只增不减的话,一对从
                # 「亲近」跌进「交恶」的人身上会同时挂着 friendship 与 rivalry,
                # 而 `compute_cliques` 只看 friendship —— 那个小团体里于是坐着
                # 两个此刻互相看不顺眼的人,而没有任何一处会报错。
                # **只撤相反的那一条,不撤中间那一档**:淡下来(落进中性带)
                # 不等于反目,把它也算作撤销会让边随着数字的小幅摆动来回闪。
                opposite = "rivalry" if predicate == "friendship" else "friendship"
                for a, b in ((agent_id, target_id), (target_id, agent_id)):
                    # **作废也要说得出哪一刻**(和下面 `created_at=` 同一条)。不传的
                    # 话默认写 0 —— 于是 `invalid_at` 早于 `valid_from`,而
                    # `query(as_of=他俩正是朋友的那一刻)` 会答"从来没有过",
                    # 恰好和这一层存在的理由相反,并且一条日志都不报错。
                    if self.knowledge_graph.drop(f"agent:{a}", opposite, f"agent:{b}",
                                                 at=int(descriptor.tick)):
                        logger.info(
                            "关系反转:撤掉 %s -%s-> %s,换上 %s", a, opposite, b, predicate
                        )
                for a, b in ((agent_id, target_id), (target_id, agent_id)):
                    self.knowledge_graph.add(
                        f"agent:{a}", predicate, f"agent:{b}",
                        source_event_seq=descriptor.event_seq,
                        # 出处得说得出"哪一刻"。此前这里不传,默认值 0 让每条边
                        # 都自称生于创世 —— 一个从来不报错、也从来对不上的答案。
                        created_at=int(descriptor.tick),
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
        # **改标签失败绝不许掀翻这条事件。** 这一处是从 `_record_event` 里面被调到的
        # (跨档 → `_apply_memory_trigger` → 这里),而 `_record_event` 的后半截才是
        # 把事件折进投影、写进记忆的地方。从这儿抛出去的话:事件已经发出去了,投影
        # 却没折 —— 日志和世界当场分叉,而且一声不吭。
        #
        # 一个抛得出来的形状是现成的:判定池正在关(`shutdown` 之后再 submit 就是
        # `RuntimeError`)。`stop()` 走的是"在锁里把池置空"那条路所以碰不到,但宿主
        # 自己关池、以及任何一条新的 submit 路径都碰得到 —— 而这一条本来就写着
        # "标签只是跨档之上的点缀,失败了保持旧文本"。
        pool = self._judge_pool
        if pool is None:
            return
        try:
            pool.submit(self._relabel_worker, agent_id, target_id, context)
        except RuntimeError:
            logger.warning("relabel not scheduled for %s→%s (judge pool is closing)",
                           agent_id, target_id)

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
            # tail is safe because RedisClock persists every advance itself.

    def _stream_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Return a web/SSE-friendly copy of a raw scheduler event.

        ⚠️ 曾经是 `@staticmethod`。改成实例方法是为了它能问名册要一个人话名 ——
        老库里的 `narrative` 事件没有 `speaker_name`,而重放走的正是这里。
        """
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
            # 实时流和状态记录是同一条内容的两种形状 —— 一边有名字一边没有,
            # 宿主按哪一边渲染就成了运气。老事件没有这个字段时退回 who/speaker。
            who = payload.get("speaker") or stream.get("who") or ""
            stream["speaker_name"] = (
                payload.get("speaker_name")
                or (self.agent_display_name(who) if who else "")
                or who
            )
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

    RUN_SINCE_SEQ = "run_since_seq"
    """`:meta` 里那一格:**这一趟是从哪条事件之后开始跑的**。

    存在的理由是 `doctor` 是**另一个进程**(一次性 CLI),而"本次开机以来"这句话
    在进程内存里是答得出的、在进程外是答不出的 —— 于是那条命令只好按**全量日志**
    判,结果是**一个世界历史上出过一次事,这条命令从此永远退 1**(看板 D25),
    而 `CLAUDE.md` 同时写着它能进 CI:一条永远红的 CI 检查等于没有这条检查。

    ⚠️ **盖戳的时机是"这一趟第一次推动世界时钟",不是 `World.open`。**
    只读的门(`map` / `prompt` / 运维脚本)每开一次世界也走 `open`,拿它当"开机"
    的话,一次 10 秒的 `anima-world map` 会把水位推到最新 —— 紧接着的 `doctor`
    报一句「本次开机以来 0 件」的**绿勾**,而那句话什么也没度量。
    **一行绿勾说出一句什么也没度量的话,比一行红字更贵。**
    """

    def _mark_run_start(self) -> None:
        """这一趟的水位:**当前日志的末条 seq**(1 起连续,所以 `count()` 就是它)。

        每个进程只写一次(`_run_marked`)。写在推第一 tick 之前,所以这一 tick
        产生的事件都算在这一趟里。写不进去就算了 —— 一个体检用的水位,不该有
        任何一条路因为它掀翻世界。
        """
        self._run_marked = True
        if self.meta_store is None or self.event_log is None:
            return
        try:
            self._write_meta(self.RUN_SINCE_SEQ, str(int(self.event_log.count())))
        except Exception:  # noqa: BLE001 - 体检水位写不进去不该拦住世界跑
            logger.debug("写不进本次开机水位", exc_info=True)

    def tick(self) -> None:
        """Advance world clock by tick_delta and process one frame."""
        with self._lock:
            if self._stopped:
                return
            if not self._run_marked:
                # **这一趟真的开始跑世界了** —— 盖一个 `doctor` 在别的进程里读得到
                # 的水位。见 `RUN_SINCE_SEQ` 的 docstring:时机是第一 tick,不是 open。
                self._mark_run_start()

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

            # 3.5 到点的"等会儿再说":她真的回来敲一次门(chat-agent)。
            self._fire_due_followups()

            # 3.55 在场的人此刻各自在做什么。**必须早于规律** —— 规律里
            #      `{"action": "chat"}` 那半边读的就是这份快照,晚一步就等于
            #      拿上一 tick 的名单去算这一 tick 的量。
            self._settle_player_actions()

            # 3.6 世界的规律:树在长、矿在枯(world-rules)。**跑在 tick 线程上**
            #     —— 它是纯算术 + SQL,没有 LLM,和 needs/economy 同一类。
            #     (autonomy 正相反:那条要打网络,必须丢到别的线程去。)
            self._evaluate_world_rules()

            # 3.61 边上的规律(`for_each: {"edge": …}`)。**和量的规律分开跑**:
            #      它们写的是两种存储(量表 / 边的那份 JSON),而 `evaluate_due`
            #      的双缓冲与一次性写回是按量表那张表写的。共用一个函数的话,
            #      要么边跟着量走一遍无谓的快照,要么量跟着边逐条往返 —— 两种都比
            #      分开写贵。**双缓冲那条纪律照抄**:这一轮读的是这一轮开始前的值。
            self._evaluate_edge_rules()

            # 3.62 插件的触发器:因一件事而变。**紧跟规律** —— 它和规律同一类
            #      (纯算术,写的都是量);排在规律之后是因为规律读的是这一轮开始前
            #      的快照,而触发器写下的量要等下一轮才被规律看见 —— 和双缓冲那条
            #      纪律是同一句话。
            self._drain_plugin_triggers()

            # 3.65 到点的长过程:椅子做好了、孩子生下来了。**必须早于行为树** ——
            #      占用她的那件事在这里解除,不然她这一 tick 还被当成在忙,
            #      于是每个长过程都白白多占一 tick(而且没人看得出来)。
            self._settle_engagements()

            # 3.67 到点没人答的邀请。**按世界时钟数,不按墙钟** —— 墙钟会让同一份
            #      日志重放出两份历史,而两边都不报错。纯算术,和上面同一类。
            self._settle_invitations()

            # 3.7 定时轮次:问问她此刻要不要自己做点什么(autonomy)。
            self._maybe_run_autonomy(now)

            # 3.75 她会不会想起一个**不在跟前**的玩家(contact)。和 3.7 并列而不是
            #      合并:那一条问的是"身边有什么可做",这一条问的是"有没有人我该
            #      找一下" —— 候选集正好互补(见 `contact.py`)。
            self._maybe_run_contact(now)

            # 3.8 在场玩家站在哪 → 可见性表。角色那一半在下面的循环里
            #     (`_settle_actor_place`),人没有 brain 所以单走一趟。
            self._settle_player_places()

            # 4. World clock → every agent's blackboard, then run its BT.
            #    bt-duties D1: the tree is driven by TIME, not by boredom. The
            #    old loop only reached the BT through the idle watchdog, so a
            #    duty that starts at 08:00 could never fire.
            needs_enabled = self._needs_enabled()
            # 🔴 **一整批一次取回来,不是每个人一次。** 需求的值 3.8.0 起住在量表里,
            # 而下面那个循环要逐个把它折上黑板 —— 在循环**里面**读的话就是
            # "每个人每 tick 一次 HGETALL",正是 `RedisStockStore` 说明里点名的
            # 那个 72ms/tick 的形状(`tests/test_world_rules.py` 那把计数尺子
            # 当场逮到过:3 个角色 120 tick = 370 次 `of`)。
            needs_values = (
                self.stock_store.snapshot_kind("agent")
                if needs_enabled and self.stock_store is not None else {}
            )
            for brain in list(self.agents.values()):
                bb = brain.agent.blackboard
                bb.write("time.day", now.day)
                bb.write("time.hour", now.hour)
                bb.write("time.minute", now.minute)
                bb.write("time.minute_of_day", now.minute_of_day)
                if needs_enabled:
                    self._settle_agent_needs(brain, needs_values)
                # 世界的量 → 黑板(只放她感知得到的)。树里没有按量分支的叶子时,
                # 这里是一次字典判空。
                self._settle_stock_watches(brain)
                # 她此刻站在哪 → 可见性表。声明成 `here` 的**她身上的量**靠它才
                # 有人看得见(没声明就是一次布尔判断)。
                self._settle_actor_place(brain)
                # 工资按真的上过多久班发(见日切结算)。这里只数,不判断。
                current = self._current_action.get(brain.agent.id)
                if current is not None and current.kind == "work":
                    self._worked_ticks[brain.agent.id] = (
                        self._worked_ticks.get(brain.agent.id, 0) + 1
                    )

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
        # R5 夜间固化。**挂在日切,不是每 tick** —— 遗忘曲线按世界日算,而反思是
        # 一次 LLM 调用,跟着白天的对话跑会和她正在说的话抢线程。
        #
        # ⚠️ **固化接管衰减,不是叠加在它上面。** 这是实装当天用真世界演出来的:
        # 固化自己会跑一遍 `decay_pass`,而下面那段日切的衰减照旧也跑一遍 ——
        # 于是**开了固化的世界,记忆一天衰减两遍**。而 `decay_pass` 不是幂等的:
        # 它按"距上次回想的完整空档"从**当前**强度再衰减一次,所以跑两遍是平方,
        # 不是多跑一点。实测一天之后强度 0.125(该是 0.35),配上
        # `prune_below` 就成了"她一觉醒来把昨天忘干净了" —— 而日志一条不错、
        # 每条记忆单看都合法。所以这里是 if/else,不是两段都跑。
        # 固化同时接管**反思的时刻**(白天只攒不发,见 `_note_memory_written`)——
        # 所以这一格的开关值必须两处读同一个函数。
        consolidating = self._consolidation_enabled()
        if consolidating:
            try:
                self.consolidate_memories(now_tick=now_tick)
            except Exception:  # noqa: BLE001 - 固化失败不该停掉日切的其余部分
                logger.warning("夜间固化失败", exc_info=True)
        elif self.memory_store is not None and hasattr(self.memory_store, "decay_pass"):
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
        self._invited_today.clear()
        if self._social_enabled() and self.knowledge_graph is not None and self.event_log is not None:
            from anima_world import cliques as cliques_mod

            try:
                groups = cliques_mod.compute_cliques(self.knowledge_graph.query())
                self.clique_store.store(groups, now_tick)
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
        meal = self.economy_store.cheapest_meal(loc)
        if meal is None:
            return
        balance = self._memory_projection.balances.get(agent.id, 0.0)
        if balance < meal["price"]:
            return
        if not self.economy_store.take_stock(loc, meal["item_id"]):
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
            # 工资按**真的上过多久班**发,不是每天无条件一份。此前一个整天睡觉的人
            # 和一个开了十小时店的人到手一样多 —— 那"经济"就只是个每天加数的计数器
            # (ARCHITECTURE:324 说的"行为树会去打工"欠的正是这一半)。
            full_day = max(1, 1440 // self._minutes_per_tick())
            for agent_id in list(self.agents):
                worked = self._worked_ticks.pop(agent_id, 0)
                if worked <= 0:
                    continue
                earned = round(wage * min(1.0, worked / full_day), 2)
                if earned <= 0:
                    continue
                self._record_event({
                    "type": "payment", "who": agent_id,
                    "payload": {"from": economy.TOWN, "to": agent_id,
                                "amount": earned, "reason": "daily_wage",
                                "worked_ticks": worked},
                })
        try:
            self.economy_store.daily_price_pass(self._shop_sales)
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
            # 听到之后有没有反应 —— 那是**她的**事,所以走判定,不走规则。
            self._submit_hearsay_judgment(listener_id, str(picked.get("summary") or ""))

    # ── 听到之后 ────────────────────────────────────────────────────────────

    def _hearsay_reaction_enabled(self) -> bool:
        return bool(
            self.config_store is not None
            and self.config_store.get("social.hearsay_reaction.enabled", default=False)
        )

    def _hearsay_roster(self, listener_id: str) -> tuple[dict[str, float], dict[str, str]]:
        """她认识谁 / 这个世界里真有谁。返回 (名字 → 好感度, 名字 → id)。

        名字是这一层唯一的货币:提示词里给的是名字,回包里认的也是名字,而 id
        由这张表翻回来 —— 判定那一头永远碰不到 id,也就永远编不出一个。

        ⚠️ **两份名单不是同一份。** `weights` 是**她已经认识的人**(给模型看的
        那一半:名字配好感度);`ids` 是**这个世界里真有这么个人**(闸的那一半)。
        合成一份的话,一段关系永远不可能**从一句闲话里长出来** —— 她听到一句
        关于林迟的话,而她跟林迟还没来往过,于是整条反应被丢掉,日志上写着
        「林迟不在名单上」,**而林迟就在这个世界里站着**。线上真踩过。
        闸一个字没松:编出来的名字照旧翻不回 id,照旧当场丢掉。

        ⚠️ **玩家的名字要从落库的那份读,不能只读在场名单。** 这一层最要紧的一句
        闲话恰恰是"他跟别人走得近",而"他"多半**不在线** —— 只读 `_present_players`
        的话,玩家在她下线期间从名单上消失,于是她永远吃不了关于他的醋,
        而世界照跑、日志干净。`contact` 表在他每次跟她说话时就把名字记下来了
        (`note_contact` 的第二个用途),正是为这种"他不在场时也要叫得出他名字"
        的场合准备的。

        同名的两个人只留一个,并且吭一声 —— 悄悄覆盖的话,她吃的醋会记到另一个
        人头上。
        """
        weights: dict[str, float] = {}
        ids: dict[str, str] = {}
        present: dict[str, Any] = {}
        if self._present_players is not None:
            try:
                present = self._present_players() or {}
            except Exception:  # noqa: BLE001 - 读不到在场名单只是名字难看一点
                logger.warning("could not read present players for hearsay roster", exc_info=True)
        known_players: dict[str, str] = {}
        store = getattr(self, "contact_store", None)
        if store is not None:
            try:
                known_players = {
                    str(row.get("player_id") or ""): str(row.get("player_name") or "")
                    for row in store.all()
                    if row.get("agent_id") == listener_id
                }
            except Exception:  # noqa: BLE001 - 读不到就退回在场名单
                logger.warning("could not read the contact table for the hearsay roster",
                               exc_info=True)
        def remember(name: str, target_id: str) -> bool:
            if not name or target_id == listener_id:
                return False
            if name in ids:
                if ids[name] != target_id:
                    logger.warning(
                        "%s 那儿有两个都叫 %r 的人(%s / %s)—— 这一轮只算前一个",
                        listener_id, name, ids[name], target_id,
                    )
                return False
            ids[name] = target_id
            return True

        for (as_id, target_id), relation in self._memory_projection.relations.items():
            if as_id != listener_id or target_id == listener_id:
                continue
            brain = self.agents.get(target_id)
            if brain is not None:
                name = brain.agent.name or target_id
            else:
                name = (
                    str((present.get(target_id) or {}).get("display_name") or "").strip()
                    or known_players.get(target_id, "").strip()
                )
                if not name:
                    continue   # 认不出名字的对象不进名单:进去了模型只能照 id 读
            if not remember(name, target_id):
                continue
            weights[name] = round(float(relation.sentiment or 0.0), 3)
        # 这个世界里还有谁 —— 只进 `ids`,不进 `weights`:她还没跟他来往过,
        # 说不出好感度,但他确确实实站在这儿,别人说起他不是编的。
        for agent_id, brain in self.agents.items():
            remember(brain.agent.name or agent_id, agent_id)
        for player_id, row in present.items():
            remember(str((row or {}).get("display_name") or "").strip(), player_id)
        for player_id, player_name in known_players.items():
            remember(player_name.strip(), player_id)
        return weights, ids

    def _submit_hearsay_judgment(self, listener_id: str, rumor: str) -> Any:
        """把"她听到了这句话"丢给判定池(lock held)。

        **绝不在 tick 线程上调模型** —— 和其余三个判定同一条。名单在锁里快照,
        判定在池上跑,回来的结果再进锁写事件。

        返回那个 Future,没提交时返回 `None`。**这是有意露出来的**:一条只在
        别处冒出来的异步副作用没法验 —— 而"判定悄悄没跑"和"她听了不在乎"在
        产物上完全一样,正是这一层最该分得开的两件事。
        """
        if not rumor or not self._hearsay_reaction_enabled():
            return None
        if self.relationship_judge is None or self._judge_pool is None:
            return None
        brain = self.agents.get(listener_id)
        if brain is None:
            return None
        roster, ids = self._hearsay_roster(listener_id)
        if not roster:
            return None   # 谁都不认识的人听不出弦外之音,也就没有可写的落点
        try:
            memories = [
                str(m.get("summary") or "")
                for m in (self.memory_store.query(agent_id=listener_id)[:5]
                          if self.memory_store is not None else [])
            ]
        except Exception:  # noqa: BLE001
            memories = []
        context = {
            "a": {
                "name": brain.agent.name or listener_id,
                "personality": brain.agent.blackboard.read("personality") or "",
            },
            "rumor": rumor,
            "roster": roster,
            "memories": memories,
            "location": self.place_name(
                brain.agent.blackboard.read("loc") or brain.agent.location or ""
            ),
        }
        pool = self._judge_pool
        if pool is None or self._stopped:
            return None
        return pool.submit(self._hearsay_judge_worker, listener_id, context, ids)

    def _hearsay_judge_worker(
        self, listener_id: str, context: dict[str, Any], ids: dict[str, str]
    ) -> None:
        """判定回来了:她的反应落成真的关系变化 + 一条她自己的记忆。

        **落点必须是既有的那套机制**(`sentiment_delta`),不是一张新表:关系跨档、
        `relation_shift` 记忆、图谱边、planner 读到的那份东西全都挂在它上面。
        另起一条路的话,她吃的醋就只存在于一个没人读的字段里 —— 而那正是这几轮
        在治的病。
        """
        judge = self.relationship_judge
        if judge is None:
            return
        judge_hearsay = getattr(judge, "judge_hearsay", None)
        if judge_hearsay is None:
            return
        try:
            verdict = judge_hearsay(
                a=context["a"], rumor=context["rumor"], roster=context["roster"],
                memories=context["memories"], location=context["location"],
                known=list(ids),
            )
        except Exception:  # noqa: BLE001 - a dead judge must not stop the world
            logger.warning("hearsay judge failed for %s", listener_id, exc_info=True)
            self.note_subsystem("hearsay_reaction", False, "judge raised")
            return
        # **两件事要分得开**:`None` = 这次判定没产出可用的东西(要吭声);
        # 空的 `reactions` = 她听了没往心里去(正常世界里最常见的结果)。
        self.note_subsystem(
            "hearsay_reaction", verdict is not None,
            "" if verdict is not None else "没有可用的判定(多半是没配 key)",
        )
        if verdict is None or not verdict.reactions:
            return

        listener_name = context["a"]["name"]
        with self._lock:
            if self._stopped:
                return
            for reaction in verdict.reactions:
                target_id = ids.get(reaction.about)
                if target_id is None:
                    continue   # 判定那一层已经拦过一次,这里是第二道
                payload: dict[str, Any] = {
                    "kind": "sentiment_delta", "as": listener_id, "target": target_id,
                    "delta": float(reaction.delta),
                    "as_name": listener_name, "target_name": reaction.about,
                    "cause": "hearsay",
                }
                if reaction.axes:
                    payload["axes"] = dict(reaction.axes)
                self._record_and_deliver({
                    "type": "state_change", "who": listener_id, "payload": payload,
                })
            # 她自己也要记得这件事,否则下一轮聊天里她"莫名其妙地冷淡"——
            # 数字动了而她说不出为什么,是这一层最容易长成的假。
            self._record_and_deliver({
                "type": "memory_seed",
                "who": listener_id,
                "payload": {
                    "agent_id": listener_id,
                    # `reaction` 是**内心活动**,和 `reflection` 一样不外传
                    # (见 `gossip.pick_gossip`)。不挡的话:她的反应又成为一条
                    # 新八卦,传出去再引发一次判定,而转手数从 0 重新开始 ——
                    # 一句闲话可以就这么永远活下去。
                    "kind": "reaction",
                    # R3:这条是**听来的**。她转述时该带着"听说"的分量,而不是
                    # 说得像自己在场 —— 八卦每传一手就多一层失真,出处丢了之后
                    # 再高的检索精度也救不回来。
                    "provenance": "heard",
                    "summary": verdict.summary,
                    "importance": round(
                        min(0.9, 0.5 + max(abs(r.delta) for r in verdict.reactions)), 3
                    ),
                    "anchor": False,
                },
            })

    def _settle_agent_needs(
        self, brain: BrainLike, batch: dict[str, dict[str, Any]] | None = None
    ) -> None:
        """needs-v3: advance one agent's need curves by tick_delta (lock held).

        Pure arithmetic on the blackboard; the agent_needs table is only a
        checkpoint (day rollover / shutdown). First touch hydrates from it."""
        from anima_world import needs as needs_mod

        bb = brain.agent.blackboard
        agent_id = brain.agent.id
        store = self.stock_store
        if store is None:
            return
        # 🔴 **3.8.0:值住在量表里,这一段只把它折成黑板上那几格。**
        # 推进(衰减 + 恢复)是 `needs` 出厂插件的六条规律干的,跑在 3.6 那一步;
        # 这一步在 3.4(每个角色的循环里),读到的就是这一 tick 刚算完的那份。
        # **行为树读的仍然是黑板** —— 它是"她做决定时看到的世界",而那一层
        # 一个字都不该知道量表长什么样。
        owner = self.stock_owner_of(agent_id)
        if batch is None:
            values = store.of(owner)          # 单独调它的那几处(测试、节拍)
        else:
            values = {k: v for k, (v, _t) in (batch.get(owner) or {}).items()}
        settled: dict[str, float] = {
            need: float(values.get(f"{needs_mod.PLUGIN_ID}.{need}", 1.0))
            for need in needs_mod.NEEDS
        }
        settled["mood"] = needs_mod.mood_of(settled)
        action = self._current_action.get(agent_id)
        kind = action.kind if action else None
        # 世界自己声明的那笔债(熬夜攒的睡眠债是标准用法)把心气儿往下拖。
        # **只改 mood,不改三条需求** —— mood 是派生值、从不落库,所以拖它不会
        # 在存储里留下第二份真相;拖 energy 则会被下一次 `settle` 当成"她真的
        # 睡了一觉",债就悄悄变成了精力。
        penalty_key = ""
        if self.config_store is not None:
            penalty_key = str(
                self.config_store.get("needs.mood_penalty_stock", default="") or ""
            ).strip()
        if penalty_key:
            # 上面那次 `of()` 已经把这个人的整张量表读回来了 —— 债也在里面。
            debt = values.get(penalty_key)
            if debt is not None:
                settled["mood"] = needs_mod.drag_mood(settled["mood"], debt)
        for key, value in settled.items():
            bb.write(f"need.{key}", value)
        # 迟滞的判据(见 NeedAction):当前动作正在补哪几条需求。写成派生值而不是
        # 一份新状态 —— 它就是 settle 刚用过的那个 kind,两处不可能对不上。
        bb.write("need._restoring", tuple(sorted(needs_mod.restores(kind))))

    # ── 按量分支(StockCondition)──────────────────────────────────────────────

    def _stock_watches(self, agent: Any) -> tuple[tuple[str, str], ...]:
        """她这棵树问到了哪些量。**从建好的树上读**,不回存储查。

        和 `duty_windows()` 从 `time_window` 行上读时间窗同一条:一棵树要什么,
        树自己是唯一权威。另存一份"她关心哪些量"迟早和树分叉,而分叉那天没人会发现。
        按树对象缓存 —— 树在 `build_tree` 时才重建,重建了 `id()` 就变了。
        """
        root = agent.bt_root
        cached = self._stock_watch_cache.get(agent.id)
        if cached is not None and cached[0] is root:
            return cached[1]
        found: list[tuple[str, str]] = []
        stack = [root]
        seen: set[int] = set()
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            if isinstance(node, StockCondition):
                found.append((node.owner, node.key))
            stack.extend(getattr(node, "children", ()) or ())
        watches = tuple(sorted(set(found)))
        self._stock_watch_cache[agent.id] = (root, watches)
        return watches

    def _settle_stock_watches(self, brain: BrainLike) -> None:
        """把她的树问到的量放到黑板上 —— **只放她感知得到的**(锁内)。

        没有这种叶子的树在这里是一次字典判空,所以常开不花钱。感知不到的量写成
        `None` 而不是跳过:黑板上留着的旧值会让她**按上一次路过时看到的数字**做决定,
        而那正是这一层要防的"知道得太多"的另一副面孔(知道得太旧)。
        """
        watches = self._stock_watches(brain.agent)
        if not watches:
            return
        bb = brain.agent.blackboard
        agent_id = brain.agent.id
        if self.stock_store is None or self.visibility_store is None:
            for owner, key in watches:
                bb.write(f"stock.{owner}.{key}", None)
            return
        rules = self.visibility_store.rules_map()
        here = bb.read("loc") or brain.agent.location or ""
        values: dict[str, dict[str, float]] = {}
        for owner, key in watches:
            resolved = f"agent:{agent_id}" if owner == "self" else owner
            reason = why_not_perceivable(
                rules, agent_id=agent_id, here=here, owner=resolved, key=key,
                place_of=self.visibility_store.place_of,
            )
            if reason:
                if reason != "elsewhere":
                    # 静态的坏:世界怎么跑这条分支都永不触发。吼一次,别每 tick 刷屏。
                    mark = (agent_id, owner, key, reason)
                    if mark not in self._stock_watch_warned:
                        self._stock_watch_warned.add(mark)
                        logger.warning(
                            "%s 的行为树按 %s 的「%s」分支,而她感知不到它(%s)—— "
                            "这条分支永远不会触发。去 stock_visibility 里给它声明一档。",
                            agent_id, resolved, key, reason,
                        )
                bb.write(f"stock.{owner}.{key}", None)
                continue
            if resolved not in values:
                values[resolved] = self.stock_store.of(resolved)
            bb.write(f"stock.{owner}.{key}", values[resolved].get(key))

    def _actor_is_visible_to_others(self) -> bool:
        """她身上有没有**别人看得见**的量。没有的话下面那步整个不必做。

        `self` 档不算:那种量只有她自己知道,谁站在哪儿都不改变这件事。
        """
        cached = self._actor_visible_cache
        if cached is None:
            ontology = self.ontology
            kind = ontology.kinds.get("agent") if ontology is not None else None
            cached = bool(kind) and any(
                q.visibility not in ("hidden", "self") for q in kind.quantities.values()
            )
            self._actor_visible_cache = cached
        return cached

    def _settle_actor_place(self, brain: BrainLike) -> None:
        """把"她此刻在哪"写进可见性表(锁内)。

        为什么要有这一步:`here` 档问的是 `visibility_store.place_of(owner)`,而
        角色的位置一直住在黑板上 —— 两边不通的话,一个声明成 `here` 的
        「手艺」**谁也看不见**,而且不报错。那是这一层最怕的那种坏法:作者写了
        一句"别人看得出她手艺好",世界照跑,只是那句话从来没有发生过。

        为什么落在 tick 循环里而不是每个写 `loc` 的地方:`loc` 有五处写点
        (创世、重连、到站、节拍、事件回放),挨个加等于给未来的第六处留一个
        静默的洞。这里是它们唯一的汇合处,而**汇合处只有一个**才守得住。
        """
        if self.visibility_store is None or not self._actor_is_visible_to_others():
            return
        agent_id = brain.agent.id
        if agent_id in self._transit:
            # 在路上:不在任何地方,也就不该被任何地方看见。
            #
            # **光是"不写新的"不够**:上一次落进表里的地点会原样留着,于是同一份
            # 提示词里两块打架 —— presence 走 `_agent_locations()`(在途的人被
            # 排除)说「同在这里的还有:没有别人」,perception 走这张表说「这里的
            # 陆知遥」。LLM 挑一边编,而且无声:她要么当自己一个人待着,要么对着
            # 一个走了半天的人说话。`unplace` 的注释早就写了同一条理由。
            #
            # 拿 `_actor_placed` 当哨子,所以一段路只删一次而不是每 tick 一次;
            # 记的是 `_NOWHERE` 而不是 `pop` —— 进程中途重启时这张缓存是空的,
            # `pop` 会读成"本来就没落过地"而把那行陈的留在库里(而在途是持久的)。
            self._unplace_actor(agent_id)
            return
        here = brain.agent.blackboard.read("loc") or brain.agent.location or ""
        if not here or self._actor_placed.get(agent_id) == here:
            return
        self.visibility_store.place(self.stock_owner_of(agent_id), here, brain.agent.name)
        self._actor_placed[agent_id] = here

    def _unplace_actor(self, agent_id: str) -> None:
        """她此刻不在任何地方 —— 把可见性表上那一行撤掉(锁内)。

        **两个调用点,不是一个。** `_start_journey` 是起点(上路的那一下当场松手),
        `_settle_actor_place` 是兜底(进程中途重启、或者别处把人塞进 `_transit`)。
        只钉后者的话会留下**一个 tick 的窗口**:一趟路在这一 tick 的
        `_settle_actor_place` 跑完之后才开始,要等下一 tick 才撤 —— 而那一 tick 里
        presence 说"这儿没别人"、perception 说"这里的陆知遥",正是要修的那句矛盾,
        只是窄了一点。真跑一遍 39 次在途取样,漏的 6 次全在上路那一 tick 上。
        """
        if self.visibility_store is None or not self._actor_is_visible_to_others():
            return
        if self._actor_placed.get(agent_id) == _NOWHERE:
            return   # 已经撤过了,一段路只写一次
        self.visibility_store.unplace(self.stock_owner_of(agent_id))
        self._actor_placed[agent_id] = _NOWHERE

    def _settle_player_actions(self) -> None:
        """在场玩家此刻在做什么 → 这一 tick 的快照(锁内)。角色那一半是
        `_current_action`,由行为树按跃迁写。

        两半分开写的理由和位置那一对一样:人没有行为树。而**要守的不变量只有
        一条** —— 世界的规律说"正在说话的人会变随和"时,说的是这个世界里的人,
        不分她和他。漏掉人这一半的样子是他的「随和」停在进世界那一 tick 的默认值
        上,一辈子不动,而面板每次都把它当成活的画出来。
        """
        if self._players_doing is None:
            self._player_action_now = {}
            return
        try:
            rows = self._players_doing() or {}
        except Exception:  # noqa: BLE001 - 读不到在场玩家在做什么不该掀翻 tick
            logger.warning("读在场玩家的当前动作失败", exc_info=True)
            self._player_action_now = {}
            return
        self._player_action_now = {
            str(pid): str(kind) for pid, kind in rows.items() if str(kind or "")
        }

    def _settle_player_places(self) -> None:
        """在场玩家此刻站在哪 → 可见性表(锁内)。角色那一半是 `_settle_actor_place`。

        两半分开写是因为人没有 brain:访客不落库,位置活在宿主进程里。但**要守的
        不变量只有一条** —— 一个人身上声明成 `here` 的量,和他同处一地的人看得见。
        漏掉人这一半的样子是作者写下"别人看得出他手忙脚乱",而那句话从来没发生过。

        走了的人要从表上撤下来:在场是会话状态,可见性表是持久的 —— 不撤的话一个
        下线三小时的访客还挂在咖啡店里被人"看见"。而**该问谁走了,得问表,不问
        这个进程的缓存**:`_actor_placed` 是进程内的,世界一重启它就是空的,于是
        上一个进程落下的行没人认领,永远留着。线上 `night-tide` 攒了六个 ——
        在场名册是空的,四个地方却各站着几天前就走了的人,每一条都占着 perception
        那一格的预算,把真东西挤出去。在场自 3.2.0 起住 Redis(带 TTL),所以
        名册是**全世界**的真相,拿它扫别的进程落下的行是安全的。
        """
        if self.visibility_store is None or not self._actor_is_visible_to_others():
            return
        if self._present_players is None:
            return
        try:
            roster = self._present_players() or {}
        except Exception:  # noqa: BLE001 - 读不到名册不该掀翻 tick
            logger.warning("读在场玩家名册失败", exc_info=True)
            return
        here_now: set[str] = set()
        for pid, info in roster.items():
            actor = f"{self.PLAYER_PREFIX}{pid}"
            here_now.add(actor)
            row = info or {}
            here = "" if row.get("in_transit") else str(row.get("location") or "")
            if not here:
                self._unplace_actor(actor)      # 在路上就不在任何地方
                continue
            # 没报过名字的退泛称,**绝不退他的 id**:这个标签会被 perception 原样
            # 念成「这里的 3f86be36-…」,而那一块的落款写着"这些是你确实知道的事,
            # 可以自然地提到"—— 于是她把一个 uuid 当人名说出口。同一条纪律在
            # `_players_here` 和摘要那一头都写着,只有这一处漏了。
            #
            # 名字进缓存的键,所以**他报过名字之后这一行会跟着改口**:只比地点的话,
            # "落地时还没报名、聊过之后才有名字"这条最常见的路上,perception 一直
            # 叫他访客而 presence 已经叫他阿檀,同一个人两个称呼。
            label = str(row.get("display_name") or "").strip() or DEFAULT_ADDRESS
            if self._actor_placed.get(actor) == (here, label):
                continue
            self.visibility_store.place(self.stock_owner_of(actor), here, label)
            self._actor_placed[actor] = (here, label)
        self._sweep_ghost_players(here_now)

    def _sweep_ghost_players(self, here_now: set[str]) -> None:
        """把不在名册上的玩家从可见性表上扫掉(锁内)。

        整表只在**名册变过**的时候读一次:一行会变成幽灵,前提是那个人从名册上
        掉下来过,而那正是这里要等的信号;开机时 `_swept_roster` 是 `None`,
        所以上一个进程落下的账开机第一 tick 就还上。
        """
        if self._swept_roster == here_now:
            return
        owner_prefix = f"agent:{self.PLAYER_PREFIX}"
        for owner in list(self.visibility_store.labels()):
            if not owner.startswith(owner_prefix):
                continue
            actor = owner[len("agent:"):]
            if actor in here_now:
                continue
            self.visibility_store.unplace(owner)
            self._actor_placed.pop(actor, None)
        self._swept_roster = set(here_now)

    # ── 一起做事 ────────────────────────────────────────────────────────────
    #
    # `interact` 一直是单人的。这一段补的是"两个人站在同一个地方却只能各干各的"
    # 那个洞。三条红线的落点分别在:
    #   ① 别人凭什么答应 → `joint_gate`(世界那一段)+ api 层的同意判定(性格那一段)
    #   ② 顺序不许有意义 → `_joint_outcomes`:先把所有人算完,一个字都不写
    #   ③ 关系变化是经历的效果 → `_settle_joint_experience`,纯算术,不调 LLM
    # 全文见 `together.py` 的模块说明。

    PLAYER_PREFIX = "player:"

    def joint_precheck(self, target: str, verb: str, count: int) -> tuple[str, str]:
        """这个调用**讲不讲得通** —— 返回 `(reason, 那句话)`,讲得通就是 `("", "")`。

        只问名单的形状,不问任何一个人肯不肯。**次序是这一段存在的全部理由**:
        决定那一层(api)要先问过它再去挨个征求同意 —— 不然一个"这件事根本不用别人
        一起做"的调用会先把人问一遍,而回执上写的是"沈遥他不想",于是调用方去改的是
        名单,而错的是动词。

        `perform_affordance` 在执行时**再问一遍同一个函数**(决定与执行之间世界还在
        跑,而且排班那条路根本不经过 api)。两处共用一份判断,所以不会分叉。
        """
        ontology = self.ontology
        affordance = (
            ontology.affordance_of(target, verb) if ontology is not None else None
        )
        if affordance is None:
            return ("", "")   # 不认识的东西 / 动词,归 `perform_affordance` 报
        spec = affordance.participants
        # 动词的人话是**作者写的**,所以拼进句子时要划得出边界 —— 理由与
        # `_named` 同一条,那儿写全了。
        said = f"「{affordance.label or affordance.verb}」"
        if spec is None:
            if count:
                return (
                    "not_joint",
                    f"{said}是一个人做的事 —— "
                    f"本体里没给它声明 participants,叫不上别人",
                )
            return ("", "")
        if not spec.accepts(count):
            want = (
                f"{spec.minimum} 个"
                if spec.minimum == spec.maximum
                else f"{spec.minimum}~{spec.maximum} 个"
            )
            return (
                "participants_missing",
                f"{said}得有人一起 —— "
                f"除你之外还要 {want},这次给的是 {count} 个",
            )
        return ("", "")

    def joint_gate(self, initiator: str, target: str, verb: str, who: str) -> str:
        """这个人过不过得了「一起做这件事」的**世界那一段**。

        返回 `together.GATE_LABELS` 里的一个键,过得了就是空串。**这几条不是她的
        意思,是世界的状态** —— 所以它们判在性格之前:一句笼统的"她没答应"会让
        玩家(和她自己)以为被拒绝的是这个人,而真正的原因可能只是他在赶路。

        决定那一层(api)拿它去**问得像话一点**,执行这一层拿它去**保证正确** ——
        两处调的是同一个函数,所以不会出现"问的时候行、做的时候不行"这种只在
        真世界里才现形的分叉。

        **被邀请的**玩家不在这里判:那一段还牵着静音、姿态、"替不替得了他点头",
        全是只有 api 层知道的事(`_invitee`)。**发起人**是玩家则照常判 ——
        他站在哪由 `_where_is` 问出去,和角色同一句话。
        """
        if not who or who == initiator:
            return "self"
        if who.startswith(self.PLAYER_PREFIX):
            return ""      # 玩家那一半归 api 层
        if who not in self.agents:
            return "unknown"
        if who in self._transit:
            return "in_transit"
        here = self._where_is(initiator)
        if not here or self._where_is(who) != here:
            return "elsewhere"
        action = self._current_action.get(who)
        if action is not None and getattr(action, "kind", "") == "sleep":
            return "asleep"
        if self._occupying(who) is not None:
            return "busy"
        # 这件事他做得了吗(力气够不够、带没带工具)。`apply_affordance` 本来就
        # **只算不写**,所以问这一句几乎免费 —— 而不问的代价是:他答应了,然后
        # 整件事因为他没力气而告吹,于是"他不肯"和"他做不到"在回执上长得一样。
        ontology, store = self.ontology, self.stock_store
        if ontology is None or store is None:
            return ""
        affordance = ontology.affordance_of(target, verb)
        if affordance is None or not affordance.needs_actor:
            return ""
        from anima_world.ontology import apply_affordance

        outcome = apply_affordance(
            affordance, values=store.of(target), world_values=store.of("world"),
            me_values=store.of(self.stock_owner_of(who)),
            held=self._memory_projection.inventories.get(who),
            item_name=self.item_name_of,
            quantity_label=self.quantity_label_for(target, self.stock_owner_of(who)),
            quantity_bands=self.quantity_bands_for(target, self.stock_owner_of(who)),
            now=int(self.clock), minutes_per_tick=self._minutes_per_tick(),
        )
        if outcome.reason == "incapable":
            return "incapable"
        return ""

    def _joint_outcomes(
        self, party: Sequence[str], target: str, verb: str, affordance: Any
    ) -> tuple[dict[str, Any], str, str]:
        """把**所有人**的账先算完 —— 返回 `(每个人的 outcome, 谁没过, 那句话)`。

        红线 ②:`participants: ["柔", "白霜"]` 和 `["白霜", "柔"]` 必须给出逐位
        相同的世界。做法和规律那一层的双缓冲同源 —— **在快照上逐个算,全过了才
        动手写**。边算边写的话,第一个人扣掉的体力会成为第二个人 `requires` 的
        输入,于是名单顺序决定了谁做得成,而那是一条没有任何人写下过的规则。

        **一个人过不了,整件事就不发生。** 三个人吃饭,一个人没钱,不该变成两个人
        吃饭 —— 那是引擎替他们改了计划(和 `拒绝时一个字都不写` 同一条)。

        **玩家和角色在这一轮里一样算。** 从前他被跳过,理由是"他身上没有量" ——
        那条理由随"玩家也有量"一起没了(`seed_actor_quantities`)。留着跳过的话,
        一场三个人的活里有一个人白干:他不掉体力、不烧材料,而账面上看不出这顿饭
        是两个人付的钱。
        """
        from anima_world.ontology import apply_affordance

        store = self.stock_store
        values = store.of(target)
        world_values = store.of("world")
        now, mpt = int(self.clock), self._minutes_per_tick()
        outcomes: dict[str, Any] = {}
        for who in party:
            outcome = apply_affordance(
                affordance, values=values, world_values=world_values,
                me_values=store.of(self.stock_owner_of(who)) if affordance.needs_actor else None,
                held=self._memory_projection.inventories.get(who),
                item_name=self.item_name_of,
                quantity_label=self.quantity_label_for(target, self.stock_owner_of(who)),
                quantity_bands=self.quantity_bands_for(target, self.stock_owner_of(who)),
                now=now, minutes_per_tick=mpt,
            )
            if not outcome.ok:
                return ({}, who, outcome.refusal or "这会儿不行")
            outcomes[who] = outcome
        return (outcomes, "", "")

    def perform_affordance(
        self,
        agent_id: str,
        target: str,
        verb: str,
        participants: Sequence[str] = (),
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """对一样东西做一件事 —— 本体声明的能力,真的兑现在世界的量上。

        **聊天里的 `interact` 动词和排班里的 `interact` 动作走的是这同一条**。
        另写一份"行为树版本的照料"迟早和工具那份分叉,而分叉的那天没人会发现
        (和 `_ToolRuntime.do_action` 委托 `emit_action` 同一条纪律)。

        返回 `{"ok": False, "refusal": ..., "reason": ...}` 是没成。`reason` 分**三**类,
        因为她接下来该做的事不一样:

        | `reason` | 意思 | 她该干什么 |
        |---|---|---|
        | `conditions` | 世界说"这会儿不行"(果子还没熟) | 等,或者换一棵 |
        | `incapable` | 她做不了(没力气、手艺不够) | 换件别的事,或先去补足 |
        | `no_ontology` / `unknown_entity` / `unknown_verb` / `absent` | 这个调用本身讲不通 | 作者/模型的事 |

        聊天那条路把最后一摞报成错、前两摞报成一句话;行为树那条路都只是"下一 tick
        再试"—— 但**第二摞下一 tick 再试也一样**,所以它必须能被认出来。混成一个,
        一个累坏了的人就会挨棵树轮着试过去,每一棵都回她"再等等"。

        `participants` 是**一起做这件事的人**(角色 id,或 `player:<id>`)。
        本体没声明 `participants` 的能力给了名单一律拒绝(`not_joint`);声明了的
        没给名单也一律拒绝(`participants_missing`)—— 两条都不许静默降级成单人,
        因为降级之后世界照跑,而作者写下的"这件事一个人做不成"一声不响地没了。

        **同意不在这一层。** 这一层只判世界那一段(`joint_gate`);"他肯不肯"由
        api 层在**锁外**问过之后,把点过头的那份名单交进来。理由是那一步要打网络
        (读人设的判定),而这一层跑在世界的锁里、也跑在 tick 线程上 ——
        时钟永不等网络。

        `dry_run=True` **只走闸,不写一个字节**:上面那几道全查一遍、`set`/`costs`
        也照算(表达式坏掉就在这里报),然后在落库之前掉头返回。它存在的理由是
        菜单 —— 玩家那一侧要在按钮上写出"这会儿点不点得动、点不动是为什么",而
        **另写一份"看上去差不多"的判定是这一层最容易犯的错**:菜单说得动、点下去
        世界不认,两边谁也不报错。所以预览和真调用共用这一个函数,四类拒绝逐字相同。
        """
        from anima_world.ontology import apply_affordance

        party_in = [str(p).strip() for p in (participants or ()) if str(p).strip()]

        def no(reason: str, refusal: str) -> dict[str, Any]:
            return {"ok": False, "target": target, "verb": verb,
                    "reason": reason, "refusal": refusal}

        with self._lock:
            # 别的进程刚种下的东西,这个进程也得看得见 —— 否则她面前那样东西
            # 会被回一句"这儿没有它"。
            self._sync_entities()
            ontology = self.ontology
            store = self.stock_store
            if ontology is None or store is None:
                return no("no_ontology", "这个世界没有声明过任何东西,没什么可交互的")
            entity = ontology.entities.get(target)
            if entity is None:
                nearby = self._resolve_here(agent_id, target)
                if nearby is not None:
                    target, entity = nearby, ontology.entities[nearby]
            if entity is None:
                menu = self._here_menu(agent_id)
                return no(
                    "unknown_entity",
                    f"这儿没有「{target}」这个东西"
                    + (f";你手边的是{menu}" if menu else ""),
                )
            affordance = ontology.affordance_of(target, verb)
            if affordance is None:
                # 人话也报出来:她读到的是"照料",报一句"它能被 ['tend']" 等于让她
                # 再猜一次,而那几个字本来就是引擎写给她的。
                known = sorted(
                    a.label or a.verb
                    for a in ontology.kinds[entity.kind].affordances.values()
                )
                return no(
                    "unknown_verb",
                    f"{entity.name or target}不能被「{verb}」"
                    + (f";它能被{'、'.join(known)}" if known else ",它什么也做不了"),
                )
            # 人话进来时把它归一成动词 id —— 事件与在做的事的键都用 id,
            # 不然同一件事按她那一轮说的是"照料"还是 tend 记成两条。
            verb = affordance.verb

            # 起头的在场检查和长过程收尾的那一次**共用这两句** —— 各写一遍的话,
            # 迟早一处认为"在路上还算在原地",于是同一个人在同一时刻,一条路上
            # "在",另一条路上"不在"。
            here = self._where_is(agent_id)
            where = self._place_of(target)
            if not here or where != here:
                # 两头都说出来。只说"它在 cafe"会读成一句谎:她也可能就在 cafe,
                # 而真正的原因是引擎不知道**她**在哪(在路上就是这样)。
                # 两个地名都过 `place_name` —— 这句话印给玩家看、也当
                # `ToolResult.error` 递给她,而 `cart`/`noodle` 是键名不是地名
                # (`place_name` 的 docstring 里那条教训,这一处当时漏了)。
                return no(
                    "absent",
                    f"{entity.name or target} 不在你这儿 —— "
                    f"它在 {self.place_name(where) or '别处'},"
                    f"你在 {self.place_name(here) or '别处'}",
                )

            if (affordance.spawn is not None or affordance.destroys_target) \
                    and self.ontology_store is None:
                # 引擎这一头没接上,不是世界的错。**拦在收费之前** —— 收了钱再
                # 发现生不出来,她付的那一次在世界里什么也没换到。
                return no(
                    "no_ontology",
                    "这个引擎没接本体存储,生不出新东西、也抹不掉旧的",
                )

            busy = self._busy_refusal(agent_id, target, verb, affordance)
            if busy is not None:
                return no("busy", busy)

            # ── 跟谁一起 ────────────────────────────────────────────────────
            #
            # 两条都不许静默降级成单人:降级之后世界照跑,而作者写下的"这件事
            # 一个人做不成"、或者调用方点名的那几个人,一声不响地没了。
            spec = affordance.participants
            bad_shape, shape_refusal = self.joint_precheck(target, verb, len(party_in))
            if bad_shape:
                return no(bad_shape, shape_refusal)
            if spec is not None:
                for who in party_in:
                    gate = self.joint_gate(agent_id, target, verb, who)
                    if gate:
                        from anima_world import together as together_mod

                        return {
                            "ok": False, "target": target, "verb": verb,
                            "reason": "participant_gate", "who": who, "gate": gate,
                            "refusal": f"{self._named(who)}"
                                       f"{together_mod.GATE_LABELS.get(gate, gate)}",
                        }

            me = self.stock_owner_of(agent_id)
            party = [agent_id, *party_in]
            if spec is not None:
                outcomes, blocked, refusal = self._joint_outcomes(
                    party, target, verb, affordance
                )
                if blocked:
                    # **一个人过不了,整件事就不发生。** 三个人吃饭,一个人没钱,
                    # 不该变成两个人吃饭 —— 那是引擎替他们改了计划。
                    return {
                        "ok": False, "target": target, "verb": verb,
                        "reason": "participant_gate", "who": blocked,
                        "gate": "incapable",
                        "refusal": f"{self._named(blocked)}这会儿做不了:{refusal}",
                    }
                if dry_run:
                    return {"ok": True, "target": target, "verb": verb,
                            "dry_run": True, "party": list(party)}
                return self._perform_joint(
                    agent_id, target, verb, affordance, outcomes, party,
                    here=here, spec=spec,
                )

            outcome = apply_affordance(
                affordance, values=store.of(target),
                world_values=store.of("world"),
                me_values=store.of(me) if affordance.needs_actor else None,
                held=self._memory_projection.inventories.get(agent_id),
                item_name=self.item_name_of,
                quantity_label=self.quantity_label_for(target, me),
                quantity_bands=self.quantity_bands_for(target, me),
                now=int(self.clock),
                minutes_per_tick=self._minutes_per_tick(),
            )
            if not outcome.ok:
                # `incapable`(她做不了)和 `conditions`(这会儿不行)必须分开传上去 ——
                # 合成一个,一个累坏了的人会挨棵树轮着试,每棵都回她"再等等"。
                return no(outcome.reason or "conditions", outcome.refusal)

            if dry_run:
                # 闸全过了。**在这里掉头** —— 下面每一行都在写世界。
                return {
                    "ok": True, "target": target, "verb": verb, "dry_run": True,
                    "changed": dict(outcome.updates), **self._spent(outcome),
                    "consumed": dict(outcome.consumed),
                }

            if affordance.is_process:
                # 长过程:**代价当场付,效果到点才落**。
                #
                # 代价付在起头是有意的:付在收尾的话,起个头再放弃就是免费的,而
                # 一个可以随时反悔且不留痕的承诺不是承诺。她真的少了一把力气、
                # 真的烧掉了两壶油,然后这段时间才开始走。
                #
                # `outcome.updates` 这里**扔掉** —— 它是拿起头那一刻的值算的。
                # 但让它算一遍不是白算:`set` 里的表达式坏掉会在这里就报成拒绝,
                # 而不是等十个月之后收尾时才发现。
                return self._engage(
                    agent_id, target, verb, affordance, outcome, here=here, me=me
                )

            if outcome.updates:
                store.set_many(target, outcome.updates, tick=int(self.clock))
            self._charge(agent_id, me, target, verb, outcome, here)
            self._record_event({
                "type": "entity_interaction",
                "who": agent_id,
                "loc": here,
                "payload": {
                    "target": target, "verb": verb, "changed": dict(outcome.updates),
                    # 她身上少了什么也进事件:代价不留痕的话,一个人干了一天活之后
                    # 账上只有"树高了",没有"她累了" —— 而后者才是她下一步的依据。
                    **self._spent(outcome),
                    "consumed": dict(outcome.consumed),
                },
            })
            # 事件先进历史,再变成在场者的经历 —— 顺序承重(见 `_emit_rule_event`):
            # 事件是**事实**,记忆是从事实里长出来的**经历**,反过来的话她"记得"
            # 的那件事在日志里还没发生。
            self._after_interaction(agent_id, target, verb, affordance, here)
            # 生与灭排在交互事件之后:那条事件记的是"她做了这件事",而生灭是它的
            # 后果 —— 历史里先有因再有果,重放的人才读得出是哪一下造出了那样东西。
            born = self._settle_birth_and_death(agent_id, target, verb, affordance, here)
            return {
                "ok": True, "target": target, "verb": verb,
                "changed": dict(outcome.updates), **self._spent(outcome),
                "consumed": dict(outcome.consumed),
                **({"spawned": born} if born else {}),
                **({"destroyed": target} if affordance.destroys_target else {}),
            }

    def _display_name(self, who: str) -> str:
        """**回执**里印的那个名字。玩家印「你」—— 拒绝的那句话是说给他听的。"""
        if who.startswith(self.PLAYER_PREFIX):
            return "你"
        brain = self.agents.get(who)
        return (brain.agent.name or who) if brain is not None else who

    def _named(self, who: str) -> str:
        """同一个名字,**贴着下一个字**时怎么印。

        中文不分词,而名字是作者(或用户)写的:`沈遥在赶路` 还看得懂,
        `Dr. Eleanor Finch在赶路` 断不开词(线上原文),`老陈的猫不在这儿`
        更糟 —— 会被读成"老陈的、猫不在这儿"。拒绝那句话是玩家唯一能读到的
        解释,读岔了他去改错东西。

        **划边界的只有数据里来的那一截**:玩家那个「你」是引擎写的代词,
        套一层框读起来像在念一个人的名字。用「」不用反引号 —— 反引号是
        markdown,玩家屏幕上就是两个撇号(`_current_action` 那句忙碌拒绝的旧账)。
        """
        if who.startswith(self.PLAYER_PREFIX):
            return self._display_name(who)
        return f"「{self._display_name(who)}」"

    @staticmethod
    def _relation_id(who: str) -> str:
        """参与者 id → **关系表里的那个 id**。玩家要脱掉 `player:` 前缀。

        ⚠️ 这一句是踩出来的。两张表对同一个人用的是**两套 id**,而且都对:

        - 库存的 holder 是 `player:{pid}`(`item_transfer` / `_is_person`)——
          那一层要在角色、玩家、货架、金库之间分门别类。
        - 关系的 target 是**裸 `pid`**(`_user_judge_worker` / `contact` 的
          `closeness` / `_hearsay_roster`)—— 那一层里"人"只有两种,不需要命名空间。

        不脱前缀的话,一起做事长出来的关系会挂在 `player:p1` 上,而聊天判定长出来的
        挂在 `p1` 上 —— **同一个人在关系表里有两行**,两边都在动、谁也不完整,而且
        `contact` / 提示词只读得到其中一行。世界照跑,日志一行不错。
        """
        return who[len(Scheduler.PLAYER_PREFIX):] \
            if who.startswith(Scheduler.PLAYER_PREFIX) else who

    def _relation_name(self, who: str) -> str:
        """**事件与记忆**里写的那个名字 —— 和回执那份不是同一件事。

        名字随事件走,不靠事后回查(和 `give_item` 逐字同一条):这条 `sentiment_delta`
        会长成一条 `relation_shift` 记忆,而那条记忆会被八卦原样转述给别人 ——
        "她和「你」一起坐了会儿"传出去没有一个人看得懂。
        """
        if not who.startswith(self.PLAYER_PREFIX):
            brain = self.agents.get(who)
            return (brain.agent.name or who) if brain is not None else who
        pid = self._relation_id(who)
        if self._present_players is not None:
            try:
                row = (self._present_players() or {}).get(pid) or {}
                name = str(row.get("display_name") or "").strip()
                if name:
                    return name
            except Exception:  # noqa: BLE001 - 读不到在场名单不该挡住结算
                logger.debug("读在场玩家名字失败", exc_info=True)
        store = getattr(self, "contact_store", None)
        if store is not None:
            try:
                for row in store.all():
                    if str(row.get("player_id") or "") == pid:
                        name = str(row.get("player_name") or "").strip()
                        if name:
                            return name
            except Exception:  # noqa: BLE001
                logger.debug("读 contact 里的玩家名字失败", exc_info=True)
        return pid

    def _perform_joint(
        self, agent_id: str, target: str, verb: str, affordance: Any,
        outcomes: Mapping[str, Any], party: Sequence[str], *,
        here: str, spec: Any,
    ) -> dict[str, Any]:
        """一起把这件事做了。**到这里为止一个字都还没写。**

        瞬间的事和长过程分两支,和单人那条逐字同构:
        - `duration == 0`:效果落一次(目标身上的量只有一份,不是一人一份)、
          代价每人各扣一次、然后当场结算共同经历。
        - `duration > 0`:代价每人各扣一次,每个人各记一条在做的事;效果与共同
          经历都等到**收尾那一刻**(`_settle_engagements`)。

        目标身上的 `set` 用**发起人**那一份算。这不是顺序:发起人是一个有名有姓
        的角色(是谁叫的这顿饭),而参与者名单的次序才是那个"没有任何人写下过的
        规则"。`set` 里读 `me_*` 的世界要知道这一条 —— 写在 REFERENCE 里。

        **玩家和角色在这里一样扣、一样记。** 从前名单先被滤掉玩家那一半,于是他
        白干:不掉体力、不烧材料、也不留一条"他那一小时在做这件事"的记录。
        """
        store = self.stock_store
        head = outcomes[agent_id]

        if affordance.is_process:
            for who in party:
                self._charge(
                    who, self.stock_owner_of(who), target, verb, outcomes[who], here
                )
            started, ends = int(self.clock), int(self.clock) + int(affordance.duration)
            for who in party:
                record = {
                    "agent": who, "target": target, "verb": verb,
                    "label": affordance.label or verb,
                    "started": started, "ends": ends,
                    "occupies": bool(affordance.occupies),
                    "loc": here,
                    # 名单跟着**每一条**记录走,不只跟着发起人那条:收尾时要靠它
                    # 算共同经历,而一条只有发起人知道名单的记录,在发起人中途走开
                    # 之后就再也说不出这顿饭有谁在。
                    "party": list(party),
                    "joint_role": (
                        "initiator" if who == agent_id else "participant"
                    ),
                    "initiator": agent_id,
                }
                self._engaged[self._engagement_key(who, target, verb)] = record
                self._record_event({
                    "type": "entity_engage", "who": who, "loc": here,
                    "payload": {
                        "target": target, "verb": verb,
                        "duration": int(affordance.duration), "ends_tick": ends,
                        "occupies": bool(affordance.occupies),
                        **self._spent(outcomes[who]),
                        "consumed": dict(outcomes[who].consumed),
                        # 每个人自己那条事件上都带全名单:重放时"她那一小时在跟
                        # 谁待着"要问得出来,而只有发起人知道等于问不出来。
                        "party": list(party),
                        "joint_role": record["joint_role"],
                    },
                })
            return {
                "ok": True, "target": target, "verb": verb, "started": True,
                "duration": int(affordance.duration), "ends_tick": ends,
                "occupies": bool(affordance.occupies), "party": list(party),
                "changed": {}, **self._spent(head),
                "consumed": dict(head.consumed),
            }

        if head.updates:
            store.set_many(target, head.updates, tick=int(self.clock))
        for who in party:
            self._charge(who, self.stock_owner_of(who), target, verb, outcomes[who], here)
        for who in party:
            self._record_event({
                "type": "entity_interaction", "who": who, "loc": here,
                "payload": {
                    "target": target, "verb": verb,
                    # 目标身上的量只变了一次,所以只有发起人那条带 `changed` ——
                    # 每条都带的话,按事件重算"这棵树长了多少"会得到人数倍。
                    "changed": dict(head.updates) if who == agent_id else {},
                    **self._spent(outcomes[who]),
                    "consumed": dict(outcomes[who].consumed),
                    "party": list(party),
                    "joint_role": "initiator" if who == agent_id else "participant",
                },
            })
        # 见证记忆与旁白**一场只发一轮**,不是一人一轮:一起做的事会为每个人各发
        # 一条 `entity_interaction`(每条各带自己那份代价),照着每条都种一次的话,
        # 三个人吃一顿饭,屋里每个人会记得吃了三顿、旁白里也会写三顿。
        self._after_interaction(agent_id, target, verb, affordance, here)
        born = self._settle_birth_and_death(agent_id, target, verb, affordance, here)
        self._settle_joint_experience(
            party, target=target, verb=verb, affordance=affordance,
            duration_ticks=0, here=here,
        )
        return {
            "ok": True, "target": target, "verb": verb, "party": list(party),
            "changed": dict(head.updates), **self._spent(head),
            "consumed": dict(head.consumed),
            **({"spawned": born} if born else {}),
            **({"destroyed": target} if affordance.destroys_target else {}),
        }

    # ── 共同经历的效果 ──────────────────────────────────────────────────────
    #
    # 红线 ③:**关系变化是这段经历的效果,不是再调一次判定。** 再调一次的话,
    # "一起过了一夜"和"多聊了两句"又回到同一个入口上,而这一层存在的全部理由
    # 就是它们不该一样。所以这里**一次模型都不调** —— 纯算术,跑得起在 tick
    # 线程上,而且同样的经历永远给出同样的账(世界的可重放性不能靠随机数)。

    def _joint_stayed(self, record: Mapping[str, Any], target: str) -> list[str]:
        """这场共同经历里,谁真的从头待到了尾。

        **按位置判,不按还剩没剩下一条 `_engaged` 记录判。** 后者要依赖结算的次序
        (发起人那条恰好排在参与者前面),而 `dict` 的插入序是一个没有人写下过的
        约定 —— 哪天有人改了 `_engaged` 的实现,这里就会静默地变成"谁都没留下"。

        玩家不查:引擎不模拟他的身体,他的在场在起头那一刻由 `face_to_face` 验过
        (api 层),此后世界对他一无所知。**照实说比假装查过好。**
        """
        party = [str(p) for p in (record.get("party") or []) if str(p)]
        if not party:
            return []
        if not record.get("occupies"):
            # 不占用的那种不要求她守在原地(和单人那一条逐字同构:怀胎不占用)。
            return party
        place = self._place_of(target)
        stayed: list[str] = []
        for who in party:
            if who.startswith(self.PLAYER_PREFIX):
                stayed.append(who)
                continue
            here = self._where_is(who)
            if here and here == place:
                stayed.append(who)
        return stayed

    def _joint_config(self) -> tuple[float, int]:
        step = together_mod.DEFAULT_RELATION_STEP
        full = together_mod.DEFAULT_FULL_DURATION_TICKS
        if self.config_store is not None:
            try:
                step = float(self.config_store.get(
                    "social.joint.relation_step", default=step))
                full = int(self.config_store.get(
                    "social.joint.full_duration_ticks", default=full))
            except (TypeError, ValueError):
                logger.warning("social.joint.* 配置读不成数,按默认刻度算", exc_info=True)
        return (step, max(1, full))

    def _settle_joint_experience(
        self, party: Sequence[str], *, target: str, verb: str, affordance: Any,
        duration_ticks: int, here: str,
    ) -> None:
        """一场共同经历落进世界:每一对有序对一条关系变化 + 每个人一条记忆。

        **落点必须是既有的那套机制**(`sentiment_delta`),不是一张新表 —— 关系
        跨档、`relation_shift` 记忆、图谱边、planner 读到的那份东西全都挂在它上面
        (和吃醋那条逐字同一条理由)。另起一条路的话,他们一起过的那一晚就只存在
        于一个没人读的字段里。

        记忆是**另一半**:数字动了而她说不出为什么,是这一层最容易长成的假。
        """
        people = [p for p in dict.fromkeys(party) if p]
        if len(people) < 2:
            return
        step, full = self._joint_config()
        # 关系表用的是**裸 id**(玩家没有 `player:` 前缀)—— 见 `_relation_id`。
        ids = {who: self._relation_id(who) for who in people}
        keys = [ids[who] for who in people]
        current = {
            (a, b): (
                rel.sentiment
                if (rel := self._memory_projection.relations.get((a, b))) is not None
                else 0.0
            )
            for a in keys for b in keys if a != b
        }
        deltas = together_mod.pair_deltas(
            keys, current, duration_ticks=duration_ticks, step=step, full_duration=full,
        )
        names = {ids[who]: self._relation_name(who) for who in people}
        for pair in deltas:
            self._record_and_deliver({
                "type": "state_change", "who": pair.who, "loc": here,
                "payload": {
                    "kind": "sentiment_delta", "as": pair.who, "target": pair.about,
                    "delta": float(pair.delta), "axes": dict(pair.axes),
                    "as_name": names.get(pair.who, pair.who),
                    "target_name": names.get(pair.about, pair.about),
                    # 由头写进事件。"她对他的好感涨了 0.04"回头查得出是哪一顿饭 ——
                    # 查不出的话,关系又变回一个说不出来路的数字。
                    "cause": "joint_activity",
                },
            })
        label = affordance.label or verb
        entity = self.ontology.entities.get(target) if self.ontology is not None else None
        what = (entity.name if entity is not None and entity.name else target)
        for who in people:
            if who.startswith(self.PLAYER_PREFIX):
                continue
            others = "、".join(names[ids[p]] for p in people if p != who)
            self._record_and_deliver({
                "type": "memory_seed", "who": who, "loc": here,
                "payload": {
                    "agent_id": who,
                    "kind": "shared_experience",
                    # **动词的人话自己可能就带着「一起」**(作者写的 label 是
                    # 「一起吃顿饭」这种),再拼一个就成了"我和零一起一起吃顿饭"。
                    # 这条摘要会进她的提示词、也会被八卦转述出去,读起来必须像人话。
                    "summary": (
                        f"我和{others}{'' if label.startswith('一起') else '一起'}"
                        f"{label},在{what}"
                    ),
                    # 比一句寒暄重、比创世锚点轻。**一起做过的事该比说过的话更
                    # 容易被想起来** —— 这一层的全部主张就是这一句。
                    "importance": 0.65,
                },
            })
        logger.info(
            "%s 一起 %s 了 %s(%s 条关系变化)", "、".join(names.values()), label, target,
            len(deltas),
        )

    # ── 邀请:她开口问了,他还没答 ─────────────────────────────────────────
    #
    # 这一族补的是红线 1 在**玩家**这一侧的缺口。角色被邀请时会被真的问一次
    # (`judge_invite` 读她的人设),而玩家被点名时,引擎此前**替他点了头**
    # ——「你自己点的头」那句话写在代码里,而他从来没被问过。同一条红线
    # (「邀请必须能被拒绝」)对角色成立、对人不成立,是这个引擎最不该有的那种
    # 不对称:它保护的是虚构的人,取消的是真人的意志。
    #
    # 四条,每条都决定了它长什么样:
    #
    # - **邀请是事件,不是易失态。** 它落 `agent_invites`,折进投影;不开新的
    #   Redis 键、不进 `volatile_keys` —— 存储契约一格不动。于是它免费得到
    #   跨进程一致(`catch_up_projection`)和可重放。
    # - **它的 id 就是那条事件的 `seq`。** 另发一个号等于凭空多一个 id 命名空间。
    # - **过期按世界时钟判,不按墙钟。** 墙钟会让同一份日志重放出两份历史,
    #   而两边都不报错。
    # - **「拒绝」和「过期」必须分得开,而且只有前者留痕。** 他按了"不"是一个
    #   人做出的决定(进记忆、进关系);他没答是**错过** —— 手机上的人放下手机
    #   去吃了顿饭,回来发现她问过他一句。把这记成"他拒绝了我",是引擎替他说话,
    #   而且是说反话。所以 expired 这一支**一个字都不写在他头上**。

    def _invite_config(self) -> tuple[bool, int, int]:
        """(她开不开得了口, 一份邀请等几个 tick, 同一个人一天最多几次)。"""
        may = True
        ttl = together_mod.DEFAULT_INVITE_TTL_TICKS
        cap = together_mod.DEFAULT_INVITES_PER_PLAYER_PER_DAY
        if self.config_store is not None:
            try:
                may = bool(self.config_store.get(
                    "social.joint.npc_may_invite_player", default=may))
                ttl = int(self.config_store.get(
                    "social.joint.invite_ttl_ticks", default=ttl))
                cap = int(self.config_store.get(
                    "social.joint.invites_per_player_per_day", default=cap))
            except (TypeError, ValueError):
                logger.warning("social.joint.invite* 配置读不成数,按默认刻度算", exc_info=True)
        return (may, max(1, ttl), max(0, cap))

    def invite_player(
        self, agent_id: str, player_id: str, *,
        target: str, verb: str, party: Sequence[str], text: str,
        verb_label: str = "", target_name: str = "", agent_name: str = "",
        consented: Sequence[str] = (),
    ) -> dict[str, Any]:
        """她开口约一个玩家。**落一条事件,然后等** —— 不替他答应。

        `player_id` 收**裸 pid**(关系表与 `_filtered_page` 的过滤键都是它;
        见 `_relation_id` 那一课)。

        `consented` 是**她开口那一刻已经点过头的人**。存下来的理由和 `text`
        逐字同一条:它属于那一刻。不存的话,他点头时得把同行的人重新问一遍,
        而"再问一次"读的是模型 —— 答案可以和上一次不同,于是他按了「好」,
        却因为**别人**这次改了主意被记成"没做成"。

        返回 `{"ok": True, "invite_seq", "expires_tick", "round"}`,或者
        `{"ok": False, "reason": "invites_off" | "invite_capped"}`。

        **上限用完不是错。** 她今天不再开口而已 —— 一个不设上限的邀请者在玩家
        眼里和一条消息推送没有区别,而她本该是一个人。调用方要把这一条报成
        "她这会儿没再开口",不是报成一次失败。
        """
        may, ttl, cap = self._invite_config()
        if not may:
            return {"ok": False, "reason": "invites_off"}
        pid = self._relation_id(str(player_id))
        with self._lock:
            now = int(self.clock)
            day = self.world_time(now).day
            key = (agent_id, pid, day)
            asked = self._invited_today.get(key, 0)
            if cap and asked >= cap:
                logger.info(
                    "%s 今天已经约过 %s %d 次了,不再开口(上限 %d)",
                    agent_id, pid, asked, cap,
                )
                return {"ok": False, "reason": "invite_capped", "asked": asked}
            here = self._where_is(agent_id)
            event = self._record_and_deliver({
                "type": "agent_invites", "who": agent_id, "loc": here,
                "payload": {
                    "agent_id": agent_id,
                    "agent_name": agent_name or self._relation_name(agent_id),
                    # 裸 pid:读那扇门按它过滤(`_filtered_page`),关系表也用它。
                    "player_id": pid,
                    "target": target, "verb": verb,
                    "verb_label": verb_label or verb,
                    "target_name": target_name or target,
                    # 名单里带着玩家自己(`player:<pid>`)—— 答应之后照原样交回
                    # `perform_affordance`,别在答复那一刻重新拼一遍名单。
                    "party": [str(p) for p in party],
                    # 她开口那一刻已经点过头的人 —— 和 `text` 同一个理由:
                    # 它属于那一刻,不在答复那一刻重新问一遍。
                    "consented": [str(p) for p in consented],
                    # 一句人话。**存下来,不在读的时候现拼** —— 她开口时说的那句
                    # 话属于那一刻(动词的 label 明天可能被作者改掉)。
                    "text": text,
                    "created_tick": now,
                    "expires_tick": now + ttl,
                    "round": asked + 1,
                },
            })
            self._invited_today[key] = asked + 1
            return {
                "ok": True, "invite_seq": int(event.get("seq") or 0),
                "expires_tick": now + ttl, "round": asked + 1,
            }

    def pending_invitation(self, invite_seq: int) -> dict[str, Any] | None:
        """还等着人回答的那一份(按 seq)。**先补课再答** —— 别的进程刚落的邀请,
        这个进程也得看得见(只读门自己补课,和 `state()` / `roster()` 同一条)。"""
        self.catch_up_projection()
        row = self._memory_projection.invitations.get(int(invite_seq))
        return dict(row) if row is not None else None

    def settled_invitation(self, invite_seq: int) -> str:
        """这份邀请**是怎么结束的**(`INVITE_OUTCOMES` 里的一个);说不上来是空串。

        `pending_invitation()` 回 `None` 只说明"它不在等人了",而那有四种意思,
        其中一种(`cancelled`,她把话收回去了)**不是他的责任**。答复那扇门要靠
        这一格才说得出是哪一种 —— 一句"要么答过了、要么已经过期"恰好把那一种
        排除在外,而它是四种里最需要说清楚的一种。

        只留最近一小段(`SETTLED_INVITATIONS_KEPT`),掉出去的回空串:**说不出来
        就别猜**。同样先补课(和 `pending_invitation` 逐字同一条)。
        """
        self.catch_up_projection()
        return str(
            self._memory_projection.settled_invitations.get(int(invite_seq)) or ""
        )

    def settle_invitation(
        self, invite_seq: int, outcome: str, *, note: str = "",
    ) -> dict[str, Any] | None:
        """给一份邀请一个结局,并把它从"还等着"里拿掉。

        **四种结局都只落同一种事件**(`invitation_settled`,`outcome` 分辨),
        因为"他拒绝了"和"他没看见"必须在账本上分得开、在清单上一样地消失。

        **关系与记忆焊在这里,只焊在 `declined` 那一支上。** 让调用方在事件之外
        再补一句的话,总有一条路只落了事件 —— 于是"他拒绝了"这件事在账本上有、
        在她心里没有,而两边都不报错。纯算术,一次模型都不调(红线 3)。

        邀请已经不在了(重复答复 / 已过期)返回 `None`,不报错:两个进程同时
        答同一份邀请是常态,而第二个不该看到一次异常。

        ⚠️ **`outcome` 必须是 `INVITE_OUTCOMES` 里的一个,写错当场报错。**
        不校验的话,一个拼错的结局会安安静静地落进日志、落进那扇门,而读的人
        (宿主的红点、运维台的账)对着一个它不认识的词只能当成"别的",
        于是那份邀请在清单上消失、在账上不存在 —— 两边都不报错。
        """
        if str(outcome) not in together_mod.INVITE_OUTCOMES:
            raise ValueError(
                f"不认识的邀请结局 {outcome!r};只有 "
                f"{'、'.join(together_mod.INVITE_OUTCOMES)}"
            )
        with self._lock:
            row = self._memory_projection.invitations.get(int(invite_seq))
            if row is None:
                return None
            settled = self._settle_invitation_event(invite_seq, outcome, row, note)
            if str(outcome) == together_mod.INVITE_OUTCOMES[1]:   # "declined"
                self._settle_invitation_declined(row)
            return settled

    def cancel_invitations(
        self, agent_id: str, player_id: str = "", *, note: str = "",
    ) -> list[int]:
        """**她自己把话收回去** —— 她还等着回话,人却已经走开了。返回撤掉的 seq。

        `INVITE_OUTCOMES` 里那个第四种结局(`cancelled`)的唯一来源。它和另外
        三种分得很清:`accepted`/`declined` 是**他**说的话,`expired` 是**没人**
        说话,而这一条是**她**改了处境 —— 三者在他手机上是三句不同的话
        (「你们一起做了」「你说了不去」「你没来得及答」「她已经走开了」)。

        **挂在她的动作上,不挂在 tick 的计时器上。** 拿"她此刻不在原地"每 tick
        判一次的话,一次游荡到隔壁也会把一份还等得到答复的邀请撤掉 —— 而"她走了"
        和"她还在等你"从此分不开。她真的走开是一个**决定**(`walk_away` 工具、
        她的选择必须在世界里兑现),所以撤回也是一个决定。

        和 `declined` 那一支的分界照旧:**这一条一个字都不写在他头上** ——
        不落记忆、不动关系。他什么也没做错。
        """
        pid = self._relation_id(str(player_id)) if player_id else ""
        out: list[int] = []
        with self._lock:
            rows = sorted(list(self._memory_projection.invitations.items()))
            for invite_seq, row in rows:
                if str(row.get("agent_id") or "") != agent_id:
                    continue
                if pid and str(row.get("player_id") or "") != pid:
                    continue
                if self.settle_invitation(
                    invite_seq, "cancelled", note=note,
                ) is not None:
                    out.append(int(invite_seq))
        if out:
            logger.info("%s 走开了,收回 %d 份还等着回话的邀请", agent_id, len(out))
        return out

    def _settle_invitation_event(
        self, invite_seq: int, outcome: str, row: Mapping[str, Any], note: str,
    ) -> dict[str, Any]:
        """结局那一条事件的正文。分出来只为了让上面读得清楚:**事件先落,
        后果后落** —— 后果是从事件里长出来的,反过来的话她"记得"的那件事在
        日志里还没发生(和 `_emit_rule_event` 的顺序逐字同一条)。"""
        payload: dict[str, Any] = {
            "invite_seq": int(invite_seq),
            "outcome": str(outcome),
            "agent_id": row.get("agent_id"),
            "agent_name": row.get("agent_name"),
            "player_id": row.get("player_id"),
            "target": row.get("target"), "verb": row.get("verb"),
            "verb_label": row.get("verb_label"),
            "target_name": row.get("target_name"),
            "created_tick": row.get("created_tick"),
            "expires_tick": row.get("expires_tick"),
        }
        if note:
            payload["note"] = note
        agent_id = str(row.get("agent_id") or "")
        return self._record_and_deliver({
            "type": "invitation_settled",
            "who": agent_id,
            "loc": self._where_is(agent_id),
            "payload": payload,
        })

    def _settle_invitation_declined(self, row: Mapping[str, Any]) -> None:
        """他按了"不"。**纯算术,一次模型都不调**(红线 3 的另一半)。

        落两样,缺一不可:一条 `sentiment_delta`(关系跨档、图谱边、planner 读的
        那份东西全挂在它上面),和她的一条记忆 —— 数字动了而她说不出为什么,
        是这一层最容易长成的假。

        记忆复用现成的 `relation_shift`(**不新增种类**)。它走 `memory_seed`,
        而 `memory_seed` 本来就绕开触发器与准入闸,所以不会顺带触发
        `_on_relation_shift` —— 那一条是给触发器推断出来的跃迁用的。
        """
        agent_id = str(row.get("agent_id") or "")
        pid = str(row.get("player_id") or "")
        if not agent_id or not pid:
            return
        step, _full = self._joint_config()
        rel = self._memory_projection.relations.get((agent_id, pid))
        current = float(getattr(rel, "sentiment", 0.0) or 0.0)
        delta = together_mod.decline_delta(current, step=step)
        here = self._where_is(agent_id)
        player_name = self._relation_name(f"{self.PLAYER_PREFIX}{pid}")
        agent_name = str(row.get("agent_name") or self._relation_name(agent_id))
        label = str(row.get("verb_label") or row.get("verb") or "")
        what = str(row.get("target_name") or row.get("target") or "")
        # 🔴 **先说发生了什么,再由内核去写四轴**(2026-08-26 第 2 期 2c,
        # 落的是老板拍的 D40 ③)。这一句从前是直接发那条 `state_change` 的 ——
        # 而 `together` 这一整块马上要搬成出厂插件,插件**写不进四轴**。
        # 搬完再改的话,中间那一版是一个明着违反自己刚立的边界的出厂插件,
        # 而作者会照着它写。
        self._record_and_deliver({
            "type": together_mod.INVITATION_DECLINED, "who": agent_id, "loc": here,
            "payload": {
                "agent_id": agent_id, "player_id": pid,
                "agent_name": agent_name, "player_name": player_name,
                "target": str(row.get("target") or ""),
                "verb": str(row.get("verb") or ""),
                "verb_label": label, "target_name": what,
            },
        })
        if abs(delta) >= 0.005:
            # 判了个 0 等于没判,别为它发一条事件(和 `pair_deltas` 的 `minimum`
            # 同一条)。**只发她 → 他这一条**:他推掉一次邀请,不该顺手改写
            # 他自己对她的感觉 —— 那是他的事,而世界不替他写。
            self._react_to_semantic_event(
                together_mod.INVITATION_DECLINED,
                {"as": agent_id, "target": pid, "delta": float(delta),
                 "axes": together_mod.decline_axes(delta),
                 "as_name": agent_name, "target_name": player_name},
                loc=here,
            )
        self._record_and_deliver({
            "type": "memory_seed", "who": agent_id, "loc": here,
            "payload": {
                "agent_id": agent_id,
                "kind": "relation_shift",
                "summary": (
                    f"我叫{player_name}{'' if label.startswith('一起') else '一起'}"
                    f"{label},在{what},{player_name}说不去"
                ),
                # 比一起做过的事(0.65)轻,比一句寒暄重:被当面推掉记得住,
                # 但它不该压过真的一起做过的那几件。
                "importance": 0.5,
            },
        })

    #: 语义事件 → 内核替它写四轴时盖在 `cause` 上的那个词。
    #:
    #: 🔴 **这张表是 D40 ③ 的落点**(老板 2026-08-26 拍):插件读得到、emit 得出
    #: 内置关系四轴,**写不进** —— 四轴是 `state_change{kind:"sentiment_delta"}` 的
    #: 投影,直写等于把关系从「可重放」变成「直接写」。于是每一件"该动关系"的事
    #: 分两半:**发生了什么**(语义事件,将来归插件)与**她因此怎么想**
    #: (这一条,永远归内核)。
    #:
    #: ⚠️ **`cause` 的取值一个字都不许改**:`test_没人答_到点就过期` 那一族按
    #: `cause == "invitation_declined"` 挑事件,改名之后那几条断言会变成**永远
    #: 成立**,于是"过期不记"这条老板拍的纪律从此没人守着,而它照绿。
    KERNEL_RELATION_CAUSES: dict[str, str] = {
        together_mod.INVITATION_DECLINED: "invitation_declined",
    }

    def _react_to_semantic_event(
        self, semantic_type: str, axes_payload: Mapping[str, Any], *, loc: str = "",
    ) -> bool:
        """一件语义事件 → 那条写四轴的 `state_change`。**内核保留的唯一写路。**

        为什么它是**同步**的、就跟在语义事件后面,而不是走插件那条触发器队列:
        队列在 tick 开头快照、drain 一遍,自己 emit 的落进**下一** tick ——
        对递归那是对的,对这一件不是。这一下的因果是同一个瞬间的两半
        (他回掉了 / 她因此冷了一点),隔一个 tick 的话,中间任何一次
        `state()` 都会看到一个"他已经拒绝了而她还没反应"的世界,
        **而那个世界是真的存在过的**(它会被写进日志、进重放、进提示词)。
        """
        cause = self.KERNEL_RELATION_CAUSES.get(semantic_type)
        if cause is None:
            return False
        self._record_and_deliver({
            "type": "state_change", "who": str(axes_payload.get("as") or ""),
            "loc": loc,
            "payload": {"kind": "sentiment_delta", **dict(axes_payload),
                        "cause": cause},
        })
        return True

    def _settle_invitations(self) -> None:
        """到点没人答的邀请,过期掉。**跑在 tick 线程上** —— 纯算术。

        **按世界时钟数**(`expires_tick <= clock`),不按墙钟:墙钟会让同一份
        日志重放出两份历史,而两边都不报错。

        **这一支一个字都不写在他头上**:不落 memory_seed、不发 sentiment_delta。
        没答不是拒绝,是错过。
        """
        pending = self._memory_projection.invitations
        if not pending:
            return
        now = int(self.clock)
        for invite_seq, row in sorted(list(pending.items())):
            try:
                expires = int(row.get("expires_tick") or 0)
            except (TypeError, ValueError):
                expires = 0
            if expires > now:
                continue
            self.settle_invitation(invite_seq, "expired")
            logger.info(
                "%s 约 %s 的那一句没等到回话,过期了(第 %s 轮)",
                row.get("agent_id"), row.get("player_id"), row.get("round"),
            )

    # ── 长过程:做一件事要花的那段时间 ──────────────────────────────────────
    #
    # 时间是这个引擎此前完全说不出的那种代价。`costs` 扣的是量、`consumes` 扣的是
    # 东西,两样都能靠"睡一觉就回来"绕开;而一段时间过不去就是过不去。所以它是
    # 唯一挡得住"生成新东西"的那道闸 —— 十月怀胎拦得住,不是因为贵,是因为长。
    #
    # 三条:
    # - **代价当场付,效果到点落。** 付在收尾的话起个头再放弃就是免费的。
    # - **关口只在起头查。** 见 `ontology.finish_affordance` 的长注释。
    # - **占用是一件事的属性,不是她的状态。** 做椅子占用她,怀胎不占用 ——
    #   两者都要花十个月,而"这期间她还能不能干别的"才是代价的真实形状。

    @staticmethod
    def _engagement_key(agent_id: str, target: str, verb: str) -> str:
        return f"{agent_id}|{target}|{verb}"

    def _where_is(self, actor_id: str) -> str:
        """他此刻在哪。**在路上就是"不知道"**(空串)—— 而不是"还在出发地"。

        `perform_affordance` 的 `absent` 与长过程的在场检查共用这一句:两处各写一遍
        的话,迟早一处认为在路上还算在原地,于是同一个人在同一时刻,一条路上"在",
        另一条路上"不在"。

        玩家住的是另一份名册(访客不落库,位置活在宿主进程里),但**这个问题只有一个
        答案** —— 所以两种人都从这一句问出去。少了玩家那一半的样子是:他站在窗前
        点"擦一擦",世界回一句"它不在你这儿 —— 它在 noodle,你在别处"。
        """
        if actor_id.startswith(self.PLAYER_PREFIX):
            return self._where_is_player(self._relation_id(actor_id))
        brain = self.agents.get(actor_id)
        if brain is None or actor_id in self._transit:
            return ""
        return brain.agent.blackboard.read("loc") or brain.agent.location or ""

    def agent_display_name(self, agent_id: str) -> str:
        """角色 id → 人话名。名册里没有就退回 id。

        **玩家也答得出。** 这个函数绑给了叙事器(`bind_names`),而旁白从今天起
        也会讲玩家做的事 —— 只认名册的话,玩家读到的那行正文里是
        「player:9f2c…忙活」,一个 uuid 印在他自己的世界动态里。玩家那一半
        走 `_relation_name`(在场名单 → contact 表 → 裸 pid),和记忆、关系事件
        里印的名字**是同一个** —— 各查各的迟早给出两个名字。
        """
        if agent_id.startswith(self.PLAYER_PREFIX):
            return self._relation_name(agent_id)
        brain = self.agents.get(agent_id)
        if brain is None:
            return agent_id
        return brain.agent.name or agent_id

    def place_name(self, location_id: str) -> str:
        """地点 id → 人话名。查不到就退回 id —— 少个名字不该让判定告吹。

        ⚠️ **判定器写出来的那句摘要会原样进她的长期记忆**,所以喂进去的地点必须
        是人话。线上读到的原文:「舒白回江渡录电台选题,两人在 cart 碰面」——
        `cart` 是键名,而她从此记得自己在一个叫 cart 的地方见过人,八卦还会把这
        句话转述出去。和 `chat_session` 的 `place_name=`、`api` 那侧的
        `_location_display_name` 是同一件事;判定器这条路当时漏了。
        """
        store = self.location_store
        if store is None or not location_id:
            return location_id
        try:
            row = store.get(location_id)
        except Exception:  # noqa: BLE001 - 查不到名字不该掀翻一次判定
            logger.warning("地点 %s 翻不成人话", location_id, exc_info=True)
            return location_id
        return (row or {}).get("name") or location_id

    def _where_is_player(self, player_id: str) -> str:
        """在场名册里那个人站在哪。没接名册 / 不在场 / 在路上,都是空串。"""
        if self._present_players is None:
            return ""
        try:
            row = (self._present_players() or {}).get(player_id) or {}
        except Exception:  # noqa: BLE001 - 读不到名册就当他不在,绝不掀翻这次调用
            logger.warning("读在场玩家位置失败", exc_info=True)
            return ""
        if row.get("in_transit"):
            return ""
        return str(row.get("location") or "")

    def _place_of(self, target: str) -> str:
        if self.visibility_store is not None:
            return self.visibility_store.place_of(target) or ""
        entity = self.ontology.entities.get(target) if self.ontology is not None else None
        return (entity.location if entity else "") or ""

    def _here_menu(self, agent_id: str, limit: int = 8) -> str:
        """她手边有哪些东西 —— 「名字(id)」,给人读的一句话。

        ⚠️ 这句话会**原样进玩家的聊天窗**(`intent._interact` 把 `ToolCallError`
        的原文括起来就发出去)。此前给的是 `sorted(ontology.entities)[:10]`:
        整个世界按字母序的前十个 id、一串 Python list 的样子,跟她面前有什么
        毫无关系 —— 玩家读到的是 `['awning:cart', 'bench:barber', …]`。

        空串是"不知道",不是"什么都没有":她在路上时也是空的,而断言一句
        "你这儿什么都没有"就成了谎(`absent` 那条两头都说,同一个道理)。
        """
        ontology = self.ontology
        if ontology is None:
            return ""
        here = self._where_is(agent_id)
        if not here:
            return ""
        names = [
            f"{e.name}({eid})" if e.name and e.name != eid else eid
            for eid, e in sorted(ontology.entities.items())
            if self._place_of(eid) == here
        ]
        if not names:
            return ""
        shown = "、".join(names[:limit])
        return shown if len(names) <= limit else f"{shown}…"

    def _resolve_here(self, agent_id: str, target: str) -> str | None:
        """她说的那样东西是她手边的哪一个 —— 认不准就 `None`。

        感知那一行印的是 `黑子[cat:hei]`,线上她写回来的是 `hei`;另一行印的是
        `剃头铺墙上那口[clockwall:barber]`,她写回来的是 `clockwall`。同一种复合 id
        被从冒号处劈开,取哪一半还没准 —— 而两次都换来一句"这儿没有它",一轮自主
        白费。

        **这不是把闸放松了**,和 `planner._unlabel` 是同一条:候选集就是**她此刻
        够得着的那几样**(`_here_menu` 印的正是这一份),对不上、或者对上不止一个,
        照旧拒绝。`bench` 在剃头铺、诊所、渡口各有一条,那种时候猜哪一条都是替她编。
        """
        ontology = self.ontology
        if ontology is None:
            return None
        target = target.strip()
        if not target:
            return None
        here = self._where_is(agent_id)
        if not here:
            return None
        hits = [
            eid
            for eid, e in ontology.entities.items()
            if self._place_of(eid) == here
            and (target == e.name or target in eid.split(":"))
        ]
        return hits[0] if len(hits) == 1 else None

    def _engagements_of(self, agent_id: str) -> list[tuple[str, dict[str, Any]]]:
        prefix = f"{agent_id}|"
        return [(k, v) for k, v in self._engaged.items() if k.startswith(prefix)]

    def _occupying(self, agent_id: str) -> dict[str, Any] | None:
        """她此刻被哪件长过程占着 —— 没有就是 `None`。占用的一次只能有一件。"""
        for _, record in self._engagements_of(agent_id):
            if record.get("occupies"):
                return record
        return None

    def occupations_now(self) -> dict[str, str]:
        """此刻被长过程占着的每个人 → **那件事叫什么**(`{"agent:齐": "陪一次夜播"}`)。

        `_occupying` 是一次一个人的问法,这是一次问全场的问法 —— 提示词里"这屋里
        谁在忙什么"要一次答完,挨个去问等于把 `:engaged` 扫 N 遍。

        **人和玩家一处分支都没有**:记录上的 `agent` 两边同形(`齐` / `player:p1`),
        `stock_owner_of` 把它们送进同一个命名空间。少了这一条的话,一个用
        `World.act` 起了长过程的角色会被写成「闲着」(她的 `_current_action` 是行为树
        写的,而这条路根本不经过行为树),而做同一件事的玩家却是「在陪一次夜播」——
        同一件事在两个人身上有两种说法,且不报错。
        """
        out: dict[str, str] = {}
        for _, record in self._engaged.items():
            if not record.get("occupies"):
                continue
            who = str(record.get("agent") or "")
            said = str(record.get("label") or record.get("verb") or "")
            if who and said:
                out[self.stock_owner_of(who)] = said
        return out

    def _busy_refusal(
        self, agent_id: str, target: str, verb: str, affordance: Any
    ) -> str | None:
        """她这会儿腾不出手 —— 返回那句话,腾得出就是 `None`。

        这是**第四类**拒绝,不是硬塞进前三类里的一种。判据一直是"她接下来该干什么"
        不一样:`conditions` 该换一棵树,`incapable` 该去补足,而 `busy` 两样都不该 ——
        她该等自己手上这件做完。塞进 `conditions` 的话,一个正在做椅子的人会挨棵树
        问过去,每棵都回她"这会儿不行",而真正的原因跟树一点关系没有。
        """
        key = self._engagement_key(agent_id, target, verb)
        mine = self._engaged.get(key)
        if mine is not None:
            left = max(0, int(mine.get("ends", 0)) - int(self.clock))
            return f"你已经在做这件事了 —— {self._human_wait(left)}"
        held = self._occupying(agent_id)
        if held is not None:
            left = max(0, int(held.get("ends", 0)) - int(self.clock))
            what = held.get("label") or held.get("verb")
            # 名字括起来:动词和它作用的那样东西直接拼在一起,读的人分不出边界在哪
            # (「重描一遍咖啡车上贴的节目单」)。而动词是作者写的,英文标签拼过来
            # 更糟。用「」不用反引号 —— 那是 markdown,玩家屏幕上就是两个撇号。
            return (
                f"你手上还有一件事没做完:{what}"
                f"「{self._display_name_of(str(held.get('target') or ''))}」 —— "
                f"{self._human_wait(left)}"
            )
        return None

    def _display_name_of(self, target: str) -> str:
        """一样东西该被叫什么。查不到就退回 id —— **只在这一处退**。"""
        entity = self.ontology.entities.get(target) if self.ontology is not None else None
        if entity is not None and entity.name:
            return entity.name
        return target

    def human_span(self, ticks: int) -> str:
        """一段时长说成人话 —— 用**世界时间**,不用 tick。空串 = 一下子的事。

        `tick` 是引擎的词。玩家问的是多久,而"3 个 tick"回答不了那个问题:
        他要么去查文档,要么放弃。而世界自己有答案(`world.minutes_per_tick`),
        同一个 3 tick 在两个世界里本来就是两段不同的时间。

        **格式化只此一处**:「还要多久」(拒绝那一句)和「要花多久」(菜单那一句)
        共用它。各写一份的下场是同一段时间在两块屏幕上写成两种说法。
        """
        minutes = max(0, int(ticks)) * max(1, int(self._minutes_per_tick()))
        if minutes <= 0:
            return ""
        if minutes < 60:
            return f"{minutes} 分钟"
        if minutes < 1440:
            hours, rest = divmod(minutes, 60)
            return f"{hours} 小时" + (f" {rest} 分钟" if rest else "")
        days, rest = divmod(minutes, 1440)
        hours = rest // 60
        return f"{days} 天" + (f" {hours} 小时" if hours else "")

    def _human_wait(self, ticks: int) -> str:
        span = self.human_span(ticks)
        return f"还要 {span}" if span else "马上就好"

    @staticmethod
    def _spent(outcome: Any) -> dict[str, Any]:
        """一次调用在**她身上**留下的账。两栏,因为读的人要问的是两个问题:

        - `me_changed` 她身上的量现在是多少 —— 和目标那边的 `changed` 同一种读法
        - `me_delta`   这一次让它变了多少(带符号)

        从前这里只有一栏,而且叫 `cost`。**那个名字是假的**:`costs` 里写的表达式
        算出来的是新值(`me_体力 - 4` → 96),于是一次擦窗记着 `cost: {体力: 96}`,
        她其实只花了 4。宿主照它画一句"这次花了 96 体力" —— 数字是真的、不报错、
        看着还合理,而它说的是另一件事。
        """
        return {"me_changed": dict(outcome.me_updates), "me_delta": outcome.me_deltas}

    def _charge(
        self, agent_id: str, me: str, target: str, verb: str, outcome: Any, here: str
    ) -> None:
        """把一次调用的代价落进世界:她身上的量 + 花掉的东西。

        瞬间的事和长过程共用这一段 —— 两条路各写一遍的话,迟早有一条忘了发
        `item_consume`,而那一条上的材料会**凭空回来**,账上还看不出来。
        """
        if outcome.me_updates and self.stock_store is not None:
            self.stock_store.set_many(me, outcome.me_updates, tick=int(self.clock))
        for item_id, quantity in sorted(outcome.consumed.items()):
            # 库存只有事件日志一个来源 —— 这里发事件,投影自己去减。直接改投影
            # 会让"重启一次她的肥料就回来了"成为可能,而账上什么也看不出来。
            self._record_event({
                "type": "item_consume", "who": agent_id, "loc": here,
                "payload": {"who": agent_id, "item_id": item_id, "qty": quantity,
                            "source": f"{verb}:{target}"},
            })

    def _engage(
        self, agent_id: str, target: str, verb: str, affordance: Any, outcome: Any,
        *, here: str, me: str,
    ) -> dict[str, Any]:
        """起头一件要花时间的事:代价当场付,记一条在做的事,到点由 tick 收尾。"""
        self._charge(agent_id, me, target, verb, outcome, here)
        started = int(self.clock)
        ends = started + int(affordance.duration)
        record = {
            "agent": agent_id, "target": target, "verb": verb,
            "label": affordance.label or verb,
            "started": started, "ends": ends,
            "occupies": bool(affordance.occupies),
            "loc": here,
        }
        self._engaged[self._engagement_key(agent_id, target, verb)] = record
        self._record_event({
            "type": "entity_engage", "who": agent_id, "loc": here,
            "payload": {
                "target": target, "verb": verb, "duration": int(affordance.duration),
                "ends_tick": ends, "occupies": bool(affordance.occupies),
                # 起头就把代价记进历史。到点才记的话,一件做了十个月的事在账上
                # 前十个月完全不存在 —— 而她那十个月的力气确实是这时候没的。
                **self._spent(outcome), "consumed": dict(outcome.consumed),
            },
        })
        return {
            "ok": True, "target": target, "verb": verb, "started": True,
            "duration": int(affordance.duration), "ends_tick": ends,
            "occupies": bool(affordance.occupies),
            "changed": {}, **self._spent(outcome),
            "consumed": dict(outcome.consumed),
        }

    def _settle_engagements(self) -> None:
        """到点的长过程结算掉。**跑在 tick 线程上** —— 纯算术,和规律同一类。"""
        if not self._engaged:
            return
        from anima_world.ontology import finish_affordance

        now_tick = int(self.clock)
        for key, record in list(self._engaged.items()):
            if int(record.get("ends", 0)) > now_tick:
                continue
            agent_id = str(record.get("agent") or "")
            target = str(record.get("target") or "")
            verb = str(record.get("verb") or "")
            self._engaged.pop(key, None)
            # 占着她的那件事解除了 —— 当前动作跟着清掉,不然行为树下一 tick
            # 挑到同一个动作会被"和当前相同"挡住,她就再也不动了。
            if record.get("occupies"):
                self._current_action.pop(agent_id, None)
            if record.get("joint_role") == "participant":
                # 一起做的事**只结算一次**,由发起人那条记录做。参与者这条到点
                # 只做一件事:把人放开。让每个人各结算一遍的话,目标身上的量会被
                # 写上人数遍,而 `set: {树高: 树高 + 0.05}` 里的"上一轮"每次都不同 ——
                # 于是一顿三个人的饭把那棵树长高了三次,账上完全看不出来。
                continue
            # 实例查一次,能力再查一次。**只查能力不够** —— `affordance_of` 查不到
            # 实例时会回落到种类,于是一棵被砍掉的树照样"能被照料",这件事会安静地
            # 收尾在一个不存在的东西上,量还真的写回去了。
            ontology = self.ontology
            affordance = (
                ontology.affordance_of(target, verb)
                if ontology is not None and target in ontology.entities else None
            )
            store = self.stock_store
            if affordance is None or store is None:
                # 东西没了(或者本体整个换了)。**代价不退** —— 她那十个月确实
                # 花掉了,退回去等于让"世界变了"成为一次免费的重来。
                self._record_event({
                    "type": "entity_disengage", "who": agent_id,
                    "loc": str(record.get("loc") or ""),
                    "payload": {"target": target, "verb": verb, "reason": "gone"},
                })
                logger.info("%s 的 %s %s 收不了尾:那个东西不在了", agent_id, verb, target)
                continue
            # **占用她的那种要求她一直在场。** 起头时查过一次同处一地
            # (`perform_affordance` 的 `absent`),而一件占着她身体的事,她走开
            # 之后当然就不在做了 —— 不查的话,她可以起个头就动身去别的镇子,
            # 那棵树照样在十二个 tick 之后被嫁接完,世界一声不吭。
            # 不占用的那种正相反(怀胎不该要求她守在原地),所以只查这一半。
            if record.get("occupies"):
                here = self._where_is(agent_id)
                if not here or here != self._place_of(target):
                    self._record_event({
                        "type": "entity_disengage", "who": agent_id,
                        "loc": here or str(record.get("loc") or ""),
                        "payload": {"target": target, "verb": verb, "reason": "left",
                                    "started_at": str(record.get("loc") or "")},
                    })
                    logger.info("%s 中途走开了,%s %s 没做完", agent_id, verb, target)
                    # ⚠️ **这一条今天还有一半是引擎自己造成的,而它此前一声不吭。**
                    # `occupies` 的含义是"这期间她还能不能干别的",可 `emit_action`
                    # 的 walk 那一支从不查 `_occupying` —— 于是排班在下一 tick 就能
                    # 把一个刚起头的长过程带走:代价照付、效果一格不落、一起做的那
                    # 几个人连关系都不变。这条边界比联合动词老,只是从前发不出联合
                    # 动词所以够不着;3.6.0 把它抬成了头号路径。
                    #
                    # **这一波不改行为**(BT 与 `_occupying` 的总账不只是 walk 一支,
                    # 见 FOR-STUDIO 那一节的丑话),但**降级不许无声**:落进
                    # `subsystem_health`,`World.state()` 里数得出来,作者才有可能
                    # 发现"她那件事一次都没做完"。
                    #
                    # **`sticky=True`**:这一件已经发生了、代价已经付了,下一件顺利
                    # 做完并不能把它抵消 —— 这盏灯报的是"这个世界出过没出过这种事",
                    # 不是"此刻好不好"(见 `note_subsystem` 的丑话)。
                    self.note_subsystem(
                        "engagement_kept", False,
                        f"{agent_id}: {verb} {target} 起了头就被带走了(代价不退)",
                        sticky=True,
                    )
                    continue
            outcome = finish_affordance(
                affordance, values=store.of(target),
                world_values=store.of("world"),
                me_values=store.of(self.stock_owner_of(agent_id)) if affordance.needs_actor else None,
                now=now_tick,
                minutes_per_tick=self._minutes_per_tick(),
            )
            if not outcome.ok:
                self._record_event({
                    "type": "entity_disengage", "who": agent_id,
                    "loc": str(record.get("loc") or ""),
                    "payload": {"target": target, "verb": verb, "reason": outcome.reason,
                                "refusal": outcome.refusal},
                })
                continue
            if outcome.updates:
                store.set_many(target, outcome.updates, tick=now_tick)
            if record.get("occupies"):
                # 健康的那一半。**只数坏的那一半读不出比例** —— "六次里有一次
                # 被带走"和"一千次里有一次"是两个完全不同的结论,而
                # `subsystem_health` 的 `ok` 计数正是那个分母。
                # 只加分母、不熄灯(`sticky=True`,和上面那半同一个开关)。
                self.note_subsystem("engagement_kept", True, sticky=True)
            here = str(record.get("loc") or "")
            spent = now_tick - int(record.get("started", now_tick))
            # **谁真的从头待到尾。** 起头时每个人都过了同处一地那道闸,而这段时间
            # 里谁都可能走开 —— 照名单发关系变化的话,一个开席就离场的人也算"一起
            # 吃过饭了",而世界里根本没有那顿饭。
            stayed = self._joint_stayed(record, target)
            self._record_event({
                "type": "entity_interaction", "who": agent_id,
                "loc": here,
                "payload": {
                    "target": target, "verb": verb, "changed": dict(outcome.updates),
                    # 代价在起头那条 `entity_engage` 上,这里不重复记 —— 记两遍的话
                    # 按事件重算"她今天花了多少力气"会得到两倍。
                    "me_changed": {}, "me_delta": {}, "consumed": {},
                    "duration": spent,
                    **({"party": stayed} if record.get("party") else {}),
                },
            })
            # 长过程的见证者是**收尾那一刻**在场的人,不是起头那一刻的:一件做了
            # 十个 tick 的事,记得它的该是看见它做成的人。
            self._after_interaction(agent_id, target, verb, affordance, here)
            # 十月怀胎落在这一行:孩子生在**收尾那一刻**,不是起头那一刻。
            self._settle_birth_and_death(agent_id, target, verb, affordance, here)
            if record.get("party"):
                self._settle_joint_experience(
                    stayed, target=target, verb=verb, affordance=affordance,
                    duration_ticks=spent, here=here,
                )

    # ── 生与灭 ──────────────────────────────────────────────────────────────
    #
    # 生成必须要代价,而代价由**作者**写(`costs` / `consumes` / `duration`),不由
    # 引擎发配额。配额是引擎的天花板:撞上去时她收到的拒绝在世界里没有意义,
    # "这个世界最多一百棵树"不是她能理解、能应对的东西。代价是世界的理由。
    #
    # 生和灭同一轮加,因为代价只封得住**速率**,封不住**存量**:体力天天回满的
    # 世界里,一百天就是一百个孩子。真实世界靠的是会生的东西都会死。

    def _sync_entities(self) -> None:
        """别的进程种下的树,这个进程也得看得见。

        实例表住 Redis(数据是共享的),而每个进程手里的 `Ontology` 是一份缓存 ——
        不同步的话,A 进程生下来的东西在 B 进程眼里根本不存在:B 的 `interact` 回
        一句"这儿没有这个东西",而那东西就在她面前。**两份真相里有一份不更新**,
        正是这个仓库最怕的坏法。

        用版本号而不是行数:一生一灭净变化是 0,而那正是最常见的一对。
        重编译只重编译实例(种类是冻的),所以这一步是一次 GET 加一次小解析。
        """
        store = self.ontology_store
        if store is None or self.ontology is None or not hasattr(store, "revision"):
            return
        try:
            remote = int(store.revision())
        except Exception:  # noqa: BLE001 - 同步失败不该掀翻这次调用
            logger.warning("实例表版本读不到", exc_info=True)
            return
        if remote == self._entities_rev:
            return
        try:
            entities = store.parse_entities_now(self.ontology.kinds)
        except Exception:  # noqa: BLE001 - 别的进程写坏了,不该让这个进程崩
            logger.warning("实例表重读失败,继续用手里这份", exc_info=True)
            return
        self.ontology = replace(self.ontology, entities=entities)
        self._entities_rev = remote

    def _settle_birth_and_death(
        self, agent_id: str, target: str, verb: str, affordance: Any, here: str,
    ) -> dict[str, Any]:
        """一次调用真的做成之后的生与灭。瞬间的事和长过程共用这一段。

        顺序是**先生后灭**:反过来的话,一条"砍倒这棵树,得到几根木料"里,新木料的
        默认位置(这件事发生的地方)要从一个已经被抹掉的东西身上问出来。
        """
        born: dict[str, Any] = {}
        if affordance.spawn is not None:
            born = self._spawn_entity(agent_id, target, verb, affordance.spawn, here)
        # 边效果排在**生之后、灭之前**:`sect.found` 那种「生出一个门派、再把自己
        # 连上去」要拿到新生的 id;而灭那一步会把挂在这个东西身上的边一并断掉,
        # 排在它后面的 `link` 会在一条刚被抹掉的边上重建一条指向坟墓的边。
        self._apply_verb_edges(agent_id, target, verb, here, born)
        if affordance.destroys_target:
            self._destroy_entity(agent_id, target, verb, here)
        return born

    def _apply_verb_edges(
        self, agent_id: str, target: str, verb: str, here: str,
        born: dict[str, Any] | None = None,
    ) -> None:
        """插件动词声明的 `link` / `unlink` / `transfer`。

        **和触发器那条路共用 `apply_edge_effect`** —— 各写一份的话,同一条 `link`
        写在触发器里查约束、写在动词里不查,而"没查"的样子是安静的:两条
        `member_of` 同时挂着,提示词里她同时是两个门派的人。
        """
        if not self.verb_edge_effects:
            return
        kind_id = target.split(":", 1)[0] if ":" in target else target
        specs = self.verb_edge_effects.get((kind_id, verb))
        if not specs:
            return
        namespace = {
            "target": target,
            "spawned": str((born or {}).get("id") or ""),
        }
        actor = self.stock_owner_of(agent_id)
        for spec in specs:
            self.apply_edge_effect(spec, namespace, {"who": actor, "loc": here},
                                   owner=actor)

    def _spawn_entity(
        self, agent_id: str, target: str, verb: str, spawn: Any, here: str,
    ) -> dict[str, Any]:
        """世界里多出一个东西 —— **而且当场验它活不活得了**。

        出生自检不是事后的工具,是出生的一部分。运行期生出来的东西走的不是创世
        那条路,而创世那条路上的闸(一次列全、当场开不了机)在这里一条都不在。
        不验的话,一个新生的东西可以是:`entities` 里看着好好的,量却一个都没落地,
        于是它的能力条件对着 0 求值、规律算不动,而两件事都只是安静地不发生。

        没通过就**整个撤回**(实例、量、位置三样一起),并且吭声 —— 留一个半死不活
        的东西在世界里,比根本没生出来更难查。**代价不退**:她确实付过了,而这一次
        失败是作者的声明坏了,退给她只会让那个 bug 从账面上也消失。
        """
        from anima_world.ontology import check_entity, seed_quantities

        store = self.ontology_store
        stocks = self.stock_store
        if store is None or stocks is None or self.ontology is None:
            return {}

        where = spawn.location or (
            self._place_of(target) or here
        )
        # **不写 `kind`**:实例的种类是 id 的前缀,不是一个字段(`tree:oak` 里那个
        # `tree` 就是它)。两处各存一份的话,迟早有一份说的是另一件事。
        entity_id = store.mint_id(spawn.kind)
        entry: dict[str, Any] = {"id": entity_id}
        if spawn.name:
            entry["name"] = spawn.name
        if spawn.gloss:
            entry["gloss"] = spawn.gloss
        if where:
            entry["location"] = where

        stamp = datetime.now(timezone.utc).isoformat()
        try:
            entity = store.add_entity(entry, stamp)
        except Exception as exc:  # noqa: BLE001 - 坏声明不该掀翻这一 tick
            logger.error("%s 生不出 %s:%s", agent_id, spawn.kind, exc)
            self._record_event({
                "type": "entity_stillborn", "who": agent_id, "loc": here,
                "payload": {"kind": spawn.kind, "verb": verb, "problems": [str(exc)]},
            })
            return {}
        self.ontology.add_entity(entity)
        self._entities_rev = store.revision()

        # **逐个量填,不是逐个实体填**(创世那边踩过):作者在 spawn 里写了一个量,
        # 不该让这个种类声明过的其余量一个都不落地。
        values = {**seed_quantities(self.ontology, entity), **dict(spawn.quantities)}
        if values:
            stocks.set_many(entity_id, values, tick=int(self.clock))
        if where and self.visibility_store is not None:
            self.visibility_store.place(entity_id, where, entity.name)

        problems = check_entity(
            self.ontology, entity_id,
            values=stocks.of(entity_id),
            world_values=stocks.of("world"),
            place=where,
        )
        if problems:
            self._unmake(entity_id)
            logger.error("%s 生下来的 %s 活不了:%s", agent_id, entity_id, "; ".join(problems))
            self._record_event({
                "type": "entity_stillborn", "who": agent_id, "loc": here,
                "payload": {"entity": entity_id, "kind": spawn.kind, "verb": verb,
                            "problems": problems},
            })
            return {}

        self._record_event({
            "type": "entity_spawn", "who": agent_id, "loc": here,
            "payload": {"entity": entity_id, "kind": spawn.kind, "name": entity.name,
                        "location": where, "from": target, "verb": verb,
                        "values": dict(stocks.of(entity_id))},
        })
        return {"entity": entity_id, "kind": spawn.kind, "name": entity.name,
                "location": where}

    def _destroy_entity(self, agent_id: str, target: str, verb: str, here: str) -> None:
        """这个东西没了。**四样一起走**:实例、量、位置、还在它身上的长过程。

        少收一样的下场各不相同,但都安静:量留着 → 一个不存在的东西还有高度,
        而下一个同名的实例会捡到它(id 不复用正是为了这个,但量的 owner 不会自己
        清);位置留着 → `at()` 一直把它报进那儿的在场名单,她的提示词里有一样
        走过去也摸不到的东西;长过程留着 → 她被一件永远收不了尾的事占着,直到
        原定的结束 tick 才解脱,而这中间她什么也做不了、也说不出为什么。
        """
        store = self.ontology_store
        if store is None or self.ontology is None:
            return
        name = ""
        entity = self.ontology.entities.get(target)
        if entity is not None:
            name = entity.name
        self._unmake(target)
        for key, record in list(self._engaged.items()):
            if record.get("target") != target:
                continue
            self._engaged.pop(key, None)
            if record.get("occupies"):
                self._current_action.pop(str(record.get("agent") or ""), None)
            if record.get("joint_role") == "participant":
                # 和 `_settle_engagements` 里那条 `continue` 同一条理由:一起做的事
                # **在日志上只记一次**,由发起人那条记录记。人照样放开(上面两行已经
                # 做完了),只是不再各发一条 `entity_disengage`。
                # 修在源头而不是 doctor 侧,是因为 `entity_disengage` 的载荷里
                # 根本没有 `joint_role` —— 让 doctor 认出参与者就得往一条事件的
                # 载荷里加字段(跨仓库契约),而"起了几件"和"收不了尾几件"本来就
                # 该按同一个单位数:一件事一条。两边不同单位的下场是 doctor 算出
                # 一个负数,再被 `max(0, …)` 抹成 0,屏幕上是一行绿勾。
                continue
            self._record_event({
                "type": "entity_disengage", "who": str(record.get("agent") or ""),
                "loc": str(record.get("loc") or ""),
                "payload": {"target": target, "verb": str(record.get("verb") or ""),
                            "reason": "gone"},
            })
        self._record_event({
            "type": "entity_destroy", "who": agent_id, "loc": here,
            "payload": {"entity": target, "name": name, "verb": verb},
        })

    def _unmake(self, entity_id: str) -> None:
        """把一个实体从**四**张表上一起抹掉。撤回一次失败的出生走的也是这一条。

        🆕 3.8.0 第 2 期加的是第四张:**挂在它身上的边**。留着的下场和上面那三样
        一样安静 —— `for_each: {"edge": …}` 每一轮都在一条指向坟墓的边上求值,
        两端有一端读不到量,于是这条规律**跳过**,而 `rule_stats()` 报的是
        skipped:专门用来回答"这层跑通了吗"的仪表说的是"没什么可算的"。
        提示词那一侧更直白:`connected` 那一档会把一个不存在的门派的门规
        继续念给她听。
        """
        store = getattr(self, "edge_store", None)
        if store is not None:
            for edge_type, src, dst in store.touching(entity_id):
                store.unlink(edge_type, src, dst)
        if self.ontology_store is not None:
            self.ontology_store.drop_entity(entity_id)
            self._entities_rev = self.ontology_store.revision()
        if self.ontology is not None:
            self.ontology.drop_entity(entity_id)
        if self.stock_store is not None:
            self.stock_store.delete(entity_id)
        if self.visibility_store is not None and hasattr(self.visibility_store, "unplace"):
            self.visibility_store.unplace(entity_id)

    def _persist_all_needs(self) -> None:
        """**3.8.0 起什么都不做,而这一格有意留着一个空函数。**

        需求的值搬进量表之后,它**本来就是持久的**(和树高、灵力同一张表、同一条
        写回路)—— 从前那份 `:needs` 检查点是"内存态每天落一次盘"的产物,而内存态
        没有了。留着这个名字是因为它有三个调用点(日切、关闭、`checkpoint()`),
        而把一件"不再需要做的事"从三处删掉,比留一个说得清为什么的空壳更容易漏。
        ⚠️ **`:needs` 那个老键不动**:一个从 3.7.0 升上来的世界里它还在,而它是
        那个世界当时的账 —— 删它是抹历史,不是升级。新值一律走量表。
        """
        return

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
        reader = _BeatWorldReader(self)
        for beat in self.beat_director.due_beats(
            now, self._memory_projection, agent_locs, reader
        ):
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

    def _apply_item_restores(self, event: dict[str, Any]) -> None:
        """吃下去的东西按 `item_defs.restores` 补需求。

        这一列 schema 里有、创世时写进去,而**在这个函数接上之前从来没有人读过**
        (病历,不是现状:读它的正是下面这段)—— 那会儿 `RESTORE_PER_TICK` 里的
        `eat` 是个跟吃什么无关的常数,于是作者认真写的"这碗面很顶饱"在世界里
        没有任何差别。经济和需求各有机制,中间一直缺这个闭环。

        需求没点亮时整条路惰性(与每个开关的既有承诺一致);查不到定义、解析不出
        JSON 都当作"不回血",绝不掀翻 tick。
        """
        if not self._needs_enabled() or self.event_log is None:
            return
        payload = event.get("payload") or {}
        who = payload.get("who") or event.get("who")
        item_id = payload.get("item_id")
        brain = self.agents.get(who) if who else None
        if brain is None or not item_id:
            return
        try:
            restores = self.economy_store.restores_of(item_id)
        except Exception:  # noqa: BLE001 - 一顿饭不值一次崩溃
            logger.warning("could not read item_defs for %r", item_id, exc_info=True)
            return
        if not restores:
            return
        # 🔴 **写量表,再顺手把黑板刷一下 —— 反过来那一半会被下一 tick 盖掉。**
        #
        # 3.8.0 起需求的**真值住在量表里**,黑板那几格是每 tick 折一次的**派生值**
        # (`_settle_agent_needs`)。第一版这里只写黑板,于是一碗面吃下去、
        # 下一 tick 就没了 —— 2026-08-26 验收 A 在同一个世界上量出来的:
        # 旧路吃完 0.6、走一 tick 还有 0.5954;新路吃完 0.6、走一 tick **0.2454**。
        # 而吃完那一刻**黑板 0.6、量表 0.1**:同一件事两个答案,两边都不报错 ——
        # 正是 `needs.py` 自己点名的「第二真相源」。
        # ⚠️ `tests/test_work_and_food.py` 当时照绿,是因为它在事件落库之后
        # **一个 tick 都不走**就断言;那条用例同轮补了一次 `tick(1)`。
        from anima_world import needs as needs_mod

        store = self.stock_store
        if store is None:
            return
        owner = self.stock_owner_of(who)
        have = store.of(owner)
        bb = brain.agent.blackboard
        fresh: dict[str, float] = {}
        for need, amount in restores.items():
            key = f"{needs_mod.PLUGIN_ID}.{need}"
            current = have.get(key)
            if not isinstance(current, (int, float)) or not isinstance(amount, (int, float)):
                continue
            value = max(0.0, min(1.0, float(current) + float(amount)))
            fresh[key] = value
            # 黑板同一刻也刷 —— 这一 tick 之内还有人读它(行为树的迟滞),
            # 而它下一 tick 会从量表折回同一个数。两处写的是同一个值,不是两份真相。
            bb.write(f"need.{need}", value)
        if fresh:
            store.set_many(owner, fresh, tick=int(self.clock))

    def _beat_needs(self, agent_id: str, need: str) -> float | None:
        brain = self.agents.get(agent_id)
        if brain is None or not self._needs_enabled():
            return None
        value = brain.agent.blackboard.read(f"need.{need}")
        return float(value) if isinstance(value, (int, float)) else None

    def _beat_memories(self, agent_id: str) -> list[str]:
        if self.memory_store is None:
            return []
        try:
            # 纯读:不加固。谓词每 tick 都在求值,让它顺手改遗忘曲线等于让"有没有
            # 写这条节拍"改变角色记得什么 —— 观察不该改变被观察的东西。
            rows = self.memory_store.query(agent_id=agent_id)
        except Exception:  # noqa: BLE001 - 谓词读不到就是"未满足",不能掀翻世界
            logger.warning("beat memory predicate could not read %r", agent_id, exc_info=True)
            return []
        return [str(row.get("summary") or "") for row in rows]

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
        from anima_world.character_card import normalize_card

        # 角色卡跟着一起进日志 —— 中途入场的人一样要出现在玩家的通讯录里,而
        # 名册的唯一权威是 `agent_join`(`World.roster()` 从投影里读它)。
        # 没写就一个字都不写:凭空补一张卡等于让宿主分不出"作者说他是背景"和
        # "作者什么也没说"。
        spec: dict[str, Any] = {
            "name": bundle.get("name", agent_id),
            "personality": bundle.get("personality", ""),
        }
        card = normalize_card(bundle.get("card"))
        if card:
            spec["card"] = card
        events: list[dict[str, Any]] = [{
            "type": "agent_join",
            "who": agent_id,
            "loc": bundle.get("location"),
            "payload": {
                "spec": spec,
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
            # **她刚从哪儿来的。** 聊天那一层要拿它说一句人话:一场面对面的对话
            # 说到一半,排班可以把她挪走(时钟不等人,那是对的),而此前这件事
            # **对谁都不留一个字** —— 玩家读到「走吧，你跟紧点」然后她就没影了;
            # 她自己下一轮的提示词只静静地翻成「手机文字私聊」,于是她照着转录的
            # 惯性接着写在咖啡车边擦台子的动作,而人已经在两条街外。
            # 只留最近一次,进程内:重启之后没有"进行中的对话"这回事。
            self._last_arrival[agent_id] = {
                "from": trip.get("from") or "", "to": trip["to"], "tick": self.clock,
            }
            self._record_and_deliver({
                "type": "state_change",
                "who": agent_id,
                "payload": {"kind": "location_join", "location": trip["to"]},
            })

    def _record_and_deliver(self, event: dict[str, Any]) -> dict[str, Any]:
        self._deliver(event)
        return self._record_event(event)

    def _enqueue_for_triggers(self, event: dict[str, Any]) -> None:
        """有人订这种事件吗 —— 有就排进队,**这一 tick 不处理**(见 `_trigger_queue`)。

        没有插件的世界这里是一次字典判空:和规律那一层同一条,常开也不花钱。

        🔴 **它挂在 `_record_event` 上,而这一格是第 1 期的一个真 bug**
        (2026-08-26 验收 C 实测):第一版挂在 `_record_and_deliver` 上,
        而**十种可订事件里有五种根本不走那条路** —— `entity_interaction`、
        `entity_spawn`、`entity_destroy`、`item_consume`、`payment` 直接调
        `_record_event`,`state_change` 一半一半。于是订 `entity_interaction` 的
        触发器在 288 tick 里:事件真发了 4 次、触发 0 次、值一格没动、
        **一处不报错**,而 `plugin list` 照旧印着「触发器 1」。
        **那正是 FOR-STUDIO §3.37 与 REFERENCE §10.1 唯一的例子。**

        挂在**落库**那一处是唯一站得住的地方:白名单说的是"这个世界里发生了什么",
        而"发生了"在这个引擎里的定义就是**进了日志**。挂在两个包装函数中的一个上,
        等于让"订得到吗"取决于发它的人碰巧调了哪一个 —— 而那件事没有任何一处写着。
        判据 `tests/test_plugins.py::test_白名单里每一种事件都真的到得了触发器`:
        它**逐条**走完 `SUBSCRIBABLE_EVENTS`,每加一种新事件都必须跟着补一条。
        """
        if not self._triggers_by_event:
            return
        if str(event.get("type") or "") in self._triggers_by_event:
            self._trigger_queue.append(dict(event))

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

    def world_time(self, tick: int | None = None) -> WorldTime:
        """The world calendar, derived from `clock` — never stored (D3).

        给了 `tick` 就折算那一刻(比如某个落库的水位是"哪一天")。
        """
        mpt = DEFAULT_MINUTES_PER_TICK
        if self.config_store is not None:
            mpt = self.config_store.get("world.minutes_per_tick", default=mpt)
        return world_time(self.clock if tick is None else int(tick), int(mpt))

    def _write_plan_step(self, agent: Agent, now: WorldTime) -> None:
        """Put the current plan step on the blackboard (clearing it when the
        agent has none) so the `follow_plan` leaf can read it."""
        plan = self._plans.get(agent.id)
        step = plan.step_at(now.minute_of_day) if plan is not None and plan.day == now.day else None
        agent.blackboard.write("plan.kind", step.kind if step else None)
        agent.blackboard.write("plan.params", dict(step.params) if step else None)

    def _request_replan_if_needed(self, agent_id: str, now: WorldTime) -> None:
        """Enqueue a replan when the agent has no plan for today. Returns
        immediately — the LLM call happens on the planner pool, never here.

        ⚠️ **一天一次是按"试过了"算的,不是按"排出来了"算的。** 从前的闸只问
        「她今天有计划吗」,而失败**什么都不留下** —— 于是每一个排不出计划的人
        每一 tick 都被重排一次。线上晚潮的样子:一个进程里 61 tick 攒了 1071 次
        规划,健康表刷成 `ok 0`,`subsystem_health` 事件被自己的健康报告淹掉
        (那正是 `note_subsystem` 的 docstring 承诺不会发生的事)。而它今天只是
        白转,是因为那 17 个人在调 LLM **之前**就返回了;真正 LLM 挂掉的那天,
        这就是一条对着付费接口每 tick 一次、没有任何退避的重试风暴。
        代价这边写在模块头上:没有计划就退回 `idle_wander`,和没有规划器的世界
        一样 —— 一次抽风顶多让她照旧过一天,而不是让世界去锤接口。
        """
        if self._planner_pool is None or agent_id in self._planning:
            return
        plan = self._plans.get(agent_id)
        if plan is not None and plan.day == now.day:
            return
        if self._plan_attempts.get(agent_id) == now.day:
            return
        self._plan_attempts[agent_id] = now.day
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

    def default_plan_wait_cap(self) -> float:
        """一个世界日肯为规划等多久。

        N 个角色排在 2 个 worker 上,一天的规划最坏是 ceil(N/2) 次串行 LLM 调用 ——
        固定 2× 超时会把一个"只是慢"的 planner 在 15 人世界的第一天就判死。
        """
        planner_timeout = 30.0
        if self.config_store is not None:
            planner_timeout = self.config_store.get("planner.timeout", default=planner_timeout)
        batches = max(2, -(-len(self.agents) // 2))  # ceil(N/2),下限 2
        return float(planner_timeout) * batches

    def fast_forward(self, ticks: int, *, plan_wait_cap: float | None = None) -> dict[str, Any]:
        """无头快进 `ticks` 个 tick,并在每个世界日等在途的规划落地。

        与裸的 `tick()` 循环的区别只有一条:**等规划**。快进会在一次 LLM 调用的
        时间里烧掉几千个 tick,于是第 D 天要的计划装回来时第 D 天早过去了(实测
        28 份计划全是 day=0,一份都没被消费)。所以每天给规划一份等待预算;
        连续两天用光就判定 planner 已死、此后不再等 —— 最坏是 2×cap 的死时间,
        永远不会挂住。

        返回 `{"ticks", "clock", "planner_gave_up", "exhausted_days"}`。
        **`planner_gave_up` 是这个返回值存在的理由**:一趟快进跑完只给一个 int,
        调用方没有任何办法区分"世界安静"和"规划全程没跟上",而两者的产物看起来
        一模一样。`plan_wait_cap<=0` 是显式的"不等",不是判死。
        """
        cap = self.default_plan_wait_cap() if plan_wait_cap is None else float(plan_wait_cap)
        no_wait = cap <= 0

        current_day: int | None = None
        day_budget = cap
        day_exhausted = False
        exhausted_streak = 0
        exhausted_days = 0
        planner_gave_up = False

        for _ in range(max(0, int(ticks))):
            self.tick()
            if no_wait or planner_gave_up:
                continue
            day = self.world_time().day
            if day != current_day:
                current_day = day
                if not day_exhausted:
                    exhausted_streak = 0
                day_budget = cap
                day_exhausted = False
            if day_budget > 0:
                started = time.monotonic()
                idle = self.wait_planning_idle(timeout=day_budget)
                day_budget -= time.monotonic() - started
                if not idle and not day_exhausted:
                    day_exhausted = True
                    exhausted_streak += 1
                    exhausted_days += 1
                    logger.warning(
                        "plan wait budget (%.1fs) exhausted on day %d; continuing planless",
                        cap, day,
                    )
                    if exhausted_streak >= 2:
                        planner_gave_up = True
                        logger.warning(
                            "planner declared dead after %d exhausted days; no further waits",
                            exhausted_streak,
                        )

        return {
            "ticks": int(ticks),
            "clock": self.clock,
            "planner_gave_up": planner_gave_up,
            "exhausted_days": exhausted_days,
        }

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

        **一个日程排满的人不是一次失败。** 所以先问"今天有什么可排",没有就
        一个字都不记 —— 记了的话健康表上「planner 挂了」和「这个世界的人都很忙」
        长得一模一样,而分开这两种正是它唯一的用处。
        """
        plan = None
        try:
            # 别在这儿 `return`:下面那把锁要负责把她从 `_planning` 里拿掉并叫醒
            # `wait_planning_idle`,提前走等于让快进永远等一个不会回来的人。
            windows, space = self.planner.plan_inputs(agent_id)
            if windows and space:
                plan = self.planner.make_plan(
                    agent_id, day, windows=windows, space=space
                )
                self.note_subsystem(
                    "planner", plan is not None,
                    "" if plan is not None else "planner returned no plan",
                )
        except Exception as exc:  # noqa: BLE001 - a dead planner must not stop the world
            logger.warning("planner failed for %s", agent_id, exc_info=True)
            plan = None
            self.note_subsystem("planner", False, f"{type(exc).__name__}: {exc}")
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

    def _advance_intent(self, agent: Any) -> None:
        """这一步真的生效了,才把她的意图队列往前走一格。

        **不在挑选时弹**:一步在途中会被重挑很多次(`emit_action` 在途时返回 False),
        挑一次弹一次会把后面几步一起吃掉,而她只走了第一步。生效才算数 —— 和
        `_current_action` 只在世界放行时才记录是同一条规矩。
        """
        blackboard = agent.blackboard
        if blackboard.read("_selected_action_id") != "follow_intent":
            return
        queue = list(blackboard.read("intent.queue") or [])
        if queue:
            queue.pop(0)
        blackboard.write("intent.queue", queue)

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
            self._advance_intent(agent)
            if action.kind == "interact" and self._occupying(agent.id) is None:
                # **一次交互是一下子的事,不是一个状态。** 记成"当前动作"的话,
                # 树下一 tick 重挑同一个动作 → 与当前相同 → 不再发生;于是
                # "树矮就浇水"这条排班一辈子只浇一次,而闸门还开着、日志还干净。
                # `walk`/`work`/`sleep` 正相反,它们本来就是持续的状态。
                #
                # **除非它起了一件占着她的长过程** —— 那时它就**是**一个状态,而且
                # 正是这条让"30 分钟的排班窗口把同一件事干 6 遍"停下来:给它一个
                # 覆盖那段窗口的 duration,她就只做一遍,做满整段时间。声明时长的
                # 世界才变,不声明的逐位如旧。
                self._current_action.pop(agent.id, None)
                return
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

            if action.kind == "interact":
                # 排班里的"照料那棵树"和聊天里的 `interact` 动词走同一条
                # (`perform_affordance`)—— 否则"她自己决定去照料"和"排班让她照料"
                # 会在世界里变成两件不一样的事。
                outcome = self.perform_affordance(
                    agent.id,
                    str(action.params.get("target") or ""),
                    str(action.params.get("verb") or ""),
                )
                if not outcome.get("ok"):
                    if outcome.get("reason") == "participants_missing":
                        # **行为树发不出「一起做」的事,而这件事从前一声不吭。**
                        # 排班里写了一个标着 participants 的动词,树每 tick 都去
                        # 试一次、每次都被同一句话拒回来,`logger.debug` 一行,
                        # 世界的历史上一个字都没有 —— 作者看到的是"这个动词从来
                        # 没发生过",看不到"她一直在试"。
                        #
                        # **不替她挑同伴。** 找同伴要先征得同意,征同意要走网络,
                        # 而这里跑在 tick 线程的锁里 —— 时钟永不等网络。她要叫人
                        # 一起做,走自主轮次那条路(在场名单现在给得出名字了)。
                        # 这里只负责让这件事**说得出来**。
                        #
                        # ⚠️ 只在**档位切换**那一刻刷日志,理由和 `note_subsystem`
                        # 自己那条一样:树每 tick 都会再试一次,每次都 info 的话,
                        # 这条通知会把日志淹掉,而淹掉等于又看不见了。持续的那一半
                        # 由计数器与 `World.state()` 的 `subsystem_health` 承担。
                        was = (self._subsystem_health.get("joint_from_tree") or {}).get(
                            "status"
                        )
                        self.note_subsystem(
                            "joint_from_tree", False,
                            f"{agent.id}: {outcome.get('refusal') or 'participants_missing'}",
                        )
                        if was != "degraded":
                            logger.info(
                                "%s 的排班让她去 %s %s,而那是件得有人一起做的事 —— "
                                "行为树叫不上人(叫人要先征得同意,征同意要走网络,"
                                "而这里是时钟线程)。她要一起做,得走自主轮次那条路。%s",
                                agent.id, action.params.get("verb"),
                                action.params.get("target"), outcome.get("refusal"),
                            )
                        return False
                    # 和 chat 找不到人同一条:不是失败,是世界说"还不行"。不记成
                    # 当前动作,下一 tick 再试;条件一满足它自己就发生了。
                    logger.debug(
                        "%s 想 %s %s,世界说不行:%s", agent.id,
                        action.params.get("verb"), action.params.get("target"),
                        outcome.get("refusal"),
                    )
                    return False
                # `entity_interaction` 已经把这件事记进世界的历史(还带着量变了多少),
                # 再发一条 `agent_action` 等于同一件事记两遍。
                events: list[dict[str, Any]] = []
            else:
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
            if action.kind in ("idle_social", "idle_wander"):
                # 「闲着 + 旁边站着一个人」正是一个人会开口打招呼的时刻。挂在闲置
                # 动作上而不是只挂 idle_social:一个世界完全可能一条 idle_social
                # 作息都没有(演示世界现在就是),那样这条路就永远走不到。
                self._maybe_hail_player(agent)
            return True

    def _evaluate_edge_rules(self) -> None:
        """作用在边上的规律。**双缓冲、一次写回,和量那一层逐字同一条纪律。**

        namespace 三个前缀:`edge.<事实>`(这条边自己的)· `src.<量>` / `dst.<量>`
        (两端节点身上的量,插件的事实写成 `src.<插件>.<事实>`)。
        ⚠️ **`set` 只写得到边自己的事实** —— 写两端节点身上的量,和 `bad_output_name`
        挡的那件事逐字同一种:双缓冲下扇入没有意义(一条作用在一百条边上的规律,
        每条读到的都是同一份旧值,于是"每条 +1"的结果是 +1 而不是 +100)。
        """
        store = self.edge_store
        if store is None or not self.world_rules:
            return
        due = [r for r in self.world_rules if r.selector_kind == "edge"]
        if not due:
            return
        from anima_world.stocks import clock_names

        clock = clock_names(self.clock, self._minutes_per_tick())
        pending: list[tuple[str, str, str, dict[str, Any]]] = []
        for rule in due:
            since = self._rule_last_run.get(rule.id)
            if since is not None and self.clock - since < rule.interval_ticks:
                continue
            self._rule_last_run[rule.id] = self.clock
            edge_type = rule.selector_value
            for src, dst, facts in store.all(edge_type):
                namespace: dict[str, Any] = {
                    **{f"edge.{k}": v for k, v in facts.items()
                       if isinstance(v, (int, float)) and not isinstance(v, bool)},
                    **{f"src.{k}": v for k, v in self.stock_store.of(src).items()},
                    **{f"dst.{k}": v for k, v in self.stock_store.of(dst).items()},
                    **clock, "dt": 0, "now": self.clock,
                }
                try:
                    if not all(c.evaluate(namespace) for c in rule.conditions):
                        continue
                    updates = {key: float(expr.evaluate(namespace))
                               for key, expr in rule.outputs.items()}
                except ExpressionError as exc:
                    logger.warning("边规律 %s 在 %s→%s 上算不出来:%s",
                                   rule.id, src, dst, exc)
                    continue
                if updates:
                    pending.append((edge_type, src, dst, updates))
        for edge_type, src, dst, updates in pending:
            facts = store.get(edge_type, src, dst)
            if facts is None:
                continue          # 这一轮里被别人断掉了 —— 不复活它
            facts.update(updates)
            store.link(edge_type, src, dst, facts)

    # ── 投影式事实(第 2 期 2b,设计稿 §9.3)──────────────────────────────

    def record_fact_delta(self, owner: str, fact: str, delta: float,
                          *, cause: str = "", loc: str = "") -> None:
        """一次变化落成一条 `<插件>.<事实>.delta`。**这才是真相。**

        量表里那个数只是物化视图 —— 抹掉它、换个进程、重开一次,这一串折一遍就
        回来了。`cause` 说的是"哪一条规律/触发器/动词让它变的";没有它,一串
        delta 只是一串数字,而这一格正是玩家屏上那句「你为什么只剩三块钱」的下半句。

        **0 不发。** 一条什么都没变的 delta 只会让那串账变长,而读它的人要在里面
        找"到底哪几下动了钱"。
        """
        if not delta:
            return
        event_type = self.projected_facts.get(fact)
        if not event_type:
            return
        self._record_and_deliver({
            "type": event_type, "who": owner, "loc": loc,
            "payload": {"owner": owner, "fact": fact,
                        "delta": round(float(delta), 6), "cause": cause},
        })

    def _record_projected_writes(
        self, pending: Mapping[str, Mapping[str, float]],
        before: Mapping[str, Mapping[str, Any]],
        causes: Mapping[tuple[str, str], str] | None = None,
        cause: str = "",
    ) -> None:
        """一轮写入里,哪几格是投影式的 —— 给它们各补一条 delta。

        ⚠️ **它跟在写入后面,不是替代写入**:量表照写(那是视图),日志多一条
        (那是真相)。两件事**必须在同一个地方做完** —— 分开的话,一个只写了
        视图的路径会让"重开一次"把这个数悄悄倒带,而倒带的那一刻没有一处报错。
        """
        if not self.projected_facts:
            return
        for owner, updates in pending.items():
            prior = before.get(owner) or {}
            for key, value in updates.items():
                if key not in self.projected_facts:
                    continue
                was = prior.get(key)
                if isinstance(was, tuple):        # 快照是 (值, tick)
                    was = was[0]
                self.record_fact_delta(
                    owner, key, float(value) - float(was or 0.0),
                    cause=(causes or {}).get((owner, key), cause))

    def _materialize_projected_facts(self) -> int:
        """把折出来的那份值写回量表。**开机走一趟,之后运行期自己维持。**

        它是"物化视图"这三个字的落点:折叠端是真相,这里只是把真相摆到读得到的
        地方(感知、表达式、`me_*` 全读量表)。⚠️ **只写声明过 `projected` 的那几个键**
        —— 多写一格就等于让一个不再是投影的量被一串陈年 delta 倒带。
        """
        store = self.stock_store
        if store is None or not self.projected_facts:
            return 0
        folded = getattr(self._memory_projection, "plugin_facts", None) or {}
        written = 0
        for owner, row in folded.items():
            updates = {key: float(value) for key, value in row.items()
                       if key in self.projected_facts}
            if not updates:
                continue
            store.set_many(owner, updates, tick=int(self.clock))
            written += len(updates)
        # 折出来一个字都没有的 owner 上,那几格该是 0 而不是"上一次留下的数"——
        # `forget_player` 折掉他那一行之后走的正是这一支。
        for owner in self._owners_with_projected_facts():
            if owner in folded:
                continue
            stale = {key: 0.0 for key in self.projected_facts
                     if key in (store.of(owner) or {})}
            if stale:
                store.set_many(owner, stale, tick=int(self.clock))
                written += len(stale)
        return written

    def _owners_with_projected_facts(self) -> list[str]:
        """量表里此刻有投影式事实的那些 owner。**只扫 `stock_owners` 那张索引。**"""
        store = self.stock_store
        owners = getattr(store, "owners", None)
        if not callable(owners):
            return []
        try:
            return list(owners())
        except Exception:  # noqa: BLE001 - 索引读不动不该掀翻开机
            logger.warning("读 stock_owners 索引失败,跳过投影式事实的清零那一趟",
                           exc_info=True)
            return []

    def _drain_plugin_triggers(self) -> None:
        """把上一 tick 攒下的那批事件交给订它们的触发器,**drain 一遍就停**。

        🔴 **快照 + 一遍,是这一层唯一的结构性决定。** 触发器自己 `emit` 出来的事件
        进的是**下一批** —— 于是"两个互相 emit 的触发器"各跑一轮就停下来,而不是
        把 tick 线程转死。同轮递归的下场不是算错,是**世界停了**,而停住的世界
        没有一处会报错(时钟不动、日志不长、健康检查照旧说 ok)。
        代价是滞后一轮,和规律那一层的双缓冲逐字同一笔账。

        **写入攒到最后一次性落库**,和 `stocks.evaluate_due` 同一条:一个 owner 一次
        往返是这个仓库明说过的反面教材。
        """
        if not self._trigger_queue or self.stock_store is None:
            return
        batch, self._trigger_queue = self._trigger_queue, []
        pending: dict[str, dict[str, float]] = {}
        emitted: list[dict[str, Any]] = []
        causes: dict[tuple[str, str], str] = {}
        for event in batch:
            for trigger in self._triggers_by_event.get(str(event.get("type") or ""), ()):
                try:
                    self._fire_trigger(trigger, event, pending, emitted, causes)
                except ExpressionError as exc:
                    # 运行期降级:一条算不出来的触发器不该掀翻 tick(规律那条纪律)。
                    # 但绝不无声。
                    logger.warning("触发器 %s.%s 算不出来:%s",
                                   trigger.plugin, trigger.id, exc)
        if pending:
            before = (self.stock_store.snapshot_many(sorted(pending))
                      if self.projected_facts else {})
            self.stock_store.write_round(pending, tick=self.clock)
            self._record_projected_writes(pending, before, causes=causes)
        for event in emitted:
            self._record_and_deliver(event)

    def _fire_trigger(
        self, trigger: Any, event: dict[str, Any],
        pending: dict[str, dict[str, float]], emitted: list[dict[str, Any]],
        causes: dict[tuple[str, str], str] | None = None,
    ) -> None:
        """一个触发器对一条事件。**当事人从事件上取,不从这一刻的世界上猜。**

        白名单(`events.SUBSCRIBABLE_EVENTS`)每条都标了 `parties` —— 那正是这一格
        存在的理由:一条事件落在谁头上,只有它自己说得出。拿"此刻在场的人"去猜的话,
        一件三分钟前发生的事会算在刚走进来的人头上,而没有一处会报错。
        """
        causes = {} if causes is None else causes
        owner = self._trigger_bearer(trigger, event)
        if owner is None:
            return
        values = {k: v for k, (v, _t) in self.stock_store.snapshot(owner).items()}
        if not values and trigger.bearer == "agent":
            return          # 这个人身上一个量都没有 = 插件还没种到他头上
        namespace: dict[str, Any] = {
            **values,
            **clock_names(self.clock, self._minutes_per_tick()),
            "dt": 0, "now": self.clock,
            **_event_numbers(event),
        }
        for condition in trigger.conditions:
            if not condition.evaluate(namespace):
                return
        for name, expression in trigger.sets:
            pending.setdefault(owner, {})[name] = float(expression.evaluate(namespace))
            # 谁让它变的 —— 投影式事实那条 delta 的 `cause`。**在这儿记**:
            # `pending` 是一整轮攒起来的,到写入那一处已经分不出是哪条触发器了。
            causes[(owner, name)] = f"{trigger.plugin}.{trigger.id}"
        for spec in trigger.links:
            self.apply_edge_effect(spec, namespace, event, owner)
        for spec in trigger.emits:
            emitted.append({
                "type": spec["type"], "who": event.get("who"), "loc": event.get("loc"),
                "payload": {**spec["payload"], "plugin": trigger.plugin,
                            "trigger": trigger.id, "because": event.get("type"),
                            **({"text": spec["text"]} if spec.get("text") else {})},
            })

    def apply_edge_effect(
        self, spec: dict[str, Any], namespace: dict[str, Any],
        event: dict[str, Any] | None = None, owner: str | None = None,
    ) -> bool:
        """`link` / `unlink` / `transfer` —— **内核执行,插件只是组合它们**。

        约束在这里查,不在声明里劝:`exclusive`(起点唯一)/ `exclusive_to`
        (终点唯一)。放行的样子是安静的 —— 两条 `member_of` 同时挂着,
        `plugin list` 看不出来,而提示词里她同时是两个门派的人。

        返回**这次到底动了没有**。⚠️ 它是承重的:一个"什么都没做"的 `link`
        和一个"建成了"的 `link` 在日志上长得一样,而调用方要拿它决定发不发事件。
        """
        store = self.edge_store
        if store is None:
            return False
        kind = spec.get("op")
        edge_type = str(spec.get("type") or "")
        declared = self.edge_types.get(edge_type)
        src = self._resolve_node(spec.get("from"), namespace, event, owner)
        dst = self._resolve_node(spec.get("to"), namespace, event, owner)
        if not edge_type or (kind != "unlink" and (not src or not dst)):
            return False
        if kind == "unlink":
            if src and dst:
                return bool(store.unlink(edge_type, src, dst))
            # 只给了一端 = 把这一端上这个类型的边全断掉(`退出师门`那种写法)。
            rows = store.of_src(edge_type, src) if src else store.of_dst(edge_type, dst)
            for a, b, _facts in rows:
                store.unlink(edge_type, a, b)
            return bool(rows)
        if kind == "transfer":
            rows = store.of_dst(edge_type, dst) if spec.get("by_dst") else                 store.of_src(edge_type, src)
            moved = False
            for a, b, facts in rows:
                store.unlink(edge_type, a, b)
                store.link(edge_type, src, dst, facts)   # 事实**跟着走**
                moved = True
            return moved
        # link
        if declared is not None:
            if declared.exclusive and store.of_src(edge_type, src):
                logger.warning(
                    "`%s` 是 exclusive 的:%s 已经有一条了,这次 link 不算数",
                    edge_type, src)
                return False
            if declared.exclusive_to and store.of_dst(edge_type, dst):
                logger.warning(
                    "`%s` 是 exclusive_to 的:%s 那一端已经有一条了", edge_type, dst)
                return False
        facts = {
            f"{declared.plugin}.{key}": fact.text_default if fact.shape == "text"
            else fact.default
            for key, fact in (declared.facts if declared is not None else {}).items()
        }
        facts.update(dict(spec.get("facts") or {}))
        store.link(edge_type, src, dst, facts)
        if declared is not None and declared.symmetric:
            # `symmetric` = 两个方向共一份事实。**建两条,不是建一条然后到处记得
            # 反着也查一遍** —— 后者要每个读的地方都记得,而漏掉一处不报错。
            store.link(edge_type, dst, src, dict(facts))
        return True

    @staticmethod
    def _resolve_node(
        raw: Any, namespace: dict[str, Any], event: dict[str, Any] | None,
        owner: str | None,
    ) -> str:
        """效果里那个 `"from"` / `"to"` 指的是哪个节点。**认不出就是空串,不猜。**

        ⚠️ **`target` / `spawned` 只在动词那条路上有值**(`_apply_verb_edges` 往
        namespace 里放),触发器那条路上它们**不存在** —— 而"不存在"在这里读成
        空串,于是 `apply_edge_effect` 当场返回 False。这是有意的:一个指着
        `target` 的触发器不该悄悄连到别的什么东西上。
        """
        name = str(raw or "")
        if name == "self":
            return str(owner or "")
        if name == "event.who":
            return str((event or {}).get("who") or "")
        if name in ("target", "spawned"):
            return str(namespace.get(name) or "")
        return name

    def _trigger_bearer(self, trigger: Any, event: dict[str, Any]) -> str | None:
        """这条事件落在哪个 owner 身上。答不出就是 `None` —— **不猜**。"""
        if trigger.bearer == "world":
            return "world"
        if trigger.bearer == "location":
            loc = str(event.get("loc") or "")
            return f"location:{loc}" if loc else None
        who = str(event.get("who") or "")
        if trigger.bearer == "agent":
            return self.stock_owner_of(who) if who else None
        # `entity:<kind>` —— 事件载荷里那个"对象"。
        kind = trigger.bearer.split(":", 1)[1]
        payload = event.get("payload") or {}
        for key in ("target", "entity"):
            target = str(payload.get(key) or "")
            if target.split(":", 1)[0] == kind:
                return target
        return None

    def _evaluate_world_rules(self) -> None:
        """把到点的规律跑一遍(world-rules)。

        没有规律的世界这里是一次字典判空,所以常开也不花钱。任何一条规律算不出来
        只跳过它自己并留下警告 —— 一个手滑的公式不该让整个世界停摆(和节拍脚本
        同一条运行期降级纪律)。
        """
        if not self.world_rules or self.stock_store is None:
            return
        from anima_world.stocks import evaluate_due

        # 🔴 **边上的规律不进这一趟,而这不是"顺手分个类"** —— 两条路共用
        # `_rule_last_run` 那张水位表:`evaluate_due` 会替每一条到点的规律盖上戳
        # (包括它自己一条都算不动的边规律),于是紧跟其后的 `_evaluate_edge_rules`
        # 每一轮都读到"这一 tick 已经跑过了"而整个跳过。**下场是边上的规律一辈子
        # 不跑,而 `rule_stats()` 每一轮都在涨** —— 那个数是 `evaluate_due` 数的。
        due = [r for r in self.world_rules if r.selector_kind != "edge"]
        if not due:
            return

        self._hydrate_rule_marks()
        try:
            report = evaluate_due(
                self.stock_store,
                due,
                self.clock,
                last_run=self._rule_last_run,
                action_owners=self._agents_doing,
                emit=self._emit_rule_event,
                minutes_per_tick=self._minutes_per_tick(),
                # `rand()` 的第一个坐标(`expressions.world_dice`)。缺省是空串,
                # 而那意味着**两个世界摇同一副骰子** —— 同名规律、同名 owner、
                # 同一 tick 下同一个数。世界的名字本来就是这里世界的身份。
                world_id=self.world_id or "",
                # 投影式事实那一半:落库**之前**记下每一格的差值(设计 §9.3)。
                # 空表时这个回调一次都不会被调用 —— 声明本身就是开关。
                on_round=(self._record_projected_writes
                          if self.projected_facts else None),
            )
        except Exception:  # noqa: BLE001 - 规律引擎自己挂了也不许掀翻 tick
            logger.warning("world-rules evaluation failed", exc_info=True)
            return
        finally:
            # 水位先落库,再统计。**放在 finally 里**:一条规律算炸了不该让这一轮
            # 已经跑过的那几条把水位丢掉 —— 丢掉的下场正是这个函数要修的那个
            # (下次开机多烧一轮)。
            self._persist_rule_marks()
        self._rule_stats["evaluated"] += report["evaluated"]
        self._rule_stats["written"] += report["written"]
        self._rule_stats["emitted"] += report["emitted"]
        if report["skipped"]:
            self._rule_stats["skipped"] += len(report["skipped"])
            self._rule_stats["last_error"] = report["skipped"][-1]

    # ── 规律的节流水位:落库(world-rules) ─────────────────────────────────

    def _hydrate_rule_marks(self) -> None:
        """从 `:meta` 把水位捞回来。**一个进程一次**(锁内,由 tick 线程调用)。

        冷启动语义有两半,两半都不许出事:

        - **老世界没有这一行**(升级路径)—— 当作空水位,每条规律在开机后第一次
          求值时跑一次。那就是今天的行为,一次,不是"把停机期间的账补烧一遍":
          补烧才是把这个 bug 反着犯一遍。
        - **水位比世界时钟还新** —— 世界被导进一个新前缀、或时钟被人挪过。留着它
          会让这几条规律一直等到时钟追上来,而那是一段**没人看得见的静默**
          (日志干净、规律不算)。所以当作没有水位,并且说一句。
        """
        if self._rule_marks_loaded:
            return
        self._rule_marks_loaded = True          # 读失败也不再试:重试只会每 tick 打一次 Redis
        store = self.meta_store
        if store is None:
            return
        try:
            row = store.get(RULE_MARKS_ROW)
        except Exception:  # noqa: BLE001 - 读不到水位不该拦住世界开机
            logger.warning("规律水位读不出来,这次开机按「没有水位」算", exc_info=True)
            return
        marks = row.get("marks") if isinstance(row, dict) else None
        if not isinstance(marks, dict):
            return

        now = int(self.clock)
        hydrated: dict[str, int] = {}
        ahead: list[str] = []
        for rule_id, value in marks.items():
            try:
                tick = int(value)
            except (TypeError, ValueError):
                continue
            if tick > now:
                ahead.append(str(rule_id))
                continue
            hydrated[str(rule_id)] = tick
        if ahead:
            logger.warning(
                "规律 %s 的水位(%s)比世界时钟还新(钟在 %d)—— 当作没有水位;"
                "留着它,这几条规律会一直等到时钟追上来,而那段静默日志上一个字都没有",
                "、".join(sorted(ahead)), RULE_MARKS_ROW, now,
            )
        # **只填缺,不覆盖** —— 和创世/重连同一条纪律。这个进程自己跑出来的水位
        # 是**现在**,库里那份是这个进程开机之前的**过去**。
        for rule_id, tick in hydrated.items():
            self._rule_last_run.setdefault(rule_id, tick)
        self._rule_marks_saved = dict(hydrated)

    def _persist_rule_marks(self) -> None:
        """水位写回 `:meta`。**只在真变了的时候写。**

        两道节制,都是为了不让这条修法变成"每 tick 一次 Redis 写":

        - **只落 `every > 1` 的规律**。每 tick 都算的规律没有节流可丢 —— 给它记
          水位,记的是一件不影响任何判断的事,代价却是每 tick 一次写。
        - **落的是全量,比的也是全量**:一轮里没有任何一条节流规律到点,这里就是
          一次字典比较,不碰 Redis。
        """
        store = self.meta_store
        if store is None:
            return
        desired = {
            rule.id: int(self._rule_last_run[rule.id])
            for rule in self.world_rules
            if getattr(rule, "interval_ticks", 1) > 1 and rule.id in self._rule_last_run
        }
        if desired == self._rule_marks_saved:
            return
        try:
            store.put(RULE_MARKS_ROW, {"marks": desired, "updated_tick": int(self.clock)})
        except Exception:  # noqa: BLE001 - 写不回去不该掀翻 tick,但绝不无声
            logger.warning(
                "规律水位写不回 %s —— 这个世界下次开机会把到点的规律多算一轮",
                RULE_MARKS_ROW, exc_info=True,
            )
            return
        self._rule_marks_saved = desired

    # ── 事件发生了,得有人经历它(witness-memory) ─────────────────────────

    def _emit_rule_event(self, event: dict[str, Any]) -> None:
        """规律 `emit` 出来的一条门槛事件:**先记进世界的历史,再变成在场者的记忆。**

        顺序是承重的:事件是**事实**,记忆是从事实里长出来的**经历**,所以事实的
        seq 必须在前。反过来的话,她"记得"的那件事在日志里还没发生。
        """
        self._record_and_deliver(event)
        try:
            self._witness_rule_event(event)
        except Exception:  # noqa: BLE001 - 记忆这一半塌了不许掀翻 tick(和规律引擎同一条)
            logger.warning("规律事件 %r 的见证者算不出来", event.get("type"), exc_info=True)

    def _witness_rule_event(self, event: dict[str, Any]) -> None:
        """一条 `emit` 事件 → 在场者的记忆(witness-memory)。

        **补的是这个引擎里一条真实的裂缝**:世界的历史和角色的经历此前是两个不相交
        的集合。线上那个雨季世界的高潮「江水漫堤」发生了,事件在日志里躺着,而四个
        角色关于它的记忆是 **0 条** —— 江晚当时就站在堤上,水从她脚边漫过第二级台阶,
        她永远不会记得这件事,也永远不会在深夜档里提起它。作者知道这个洞:他在节拍
        脚本里用 `broadcast_memory` + 逐人手写 `memory` 手工搭这座桥。**这座桥该是
        引擎的。**

        三条:

        - **声明本身就是开关**(和 perception / ontology 逐字同构):作者不写
          `importance`,这一层整个缺席,行为与从前逐位相同。世界的量每天有几十万次
          变化,默认让每一次门槛跨越都变成全员的记忆,等于用世界的物理法则把她的
          记忆冲刷干净。
        - **谁在场按位置算,不按"谁订阅了"**:`owner == "world"` 是整个世界的事
          (名册里所有人);挂在某样东西身上的事,只有此刻和它同处一地的人看得见。
          位置查不到 = **没有见证者**,不是"所有人" —— 猜成所有人的话,一棵不知道
          在哪的树倒了,全世界都记得。
        - **走 `memory_seed` 这条现成的路**,不另造第二条写记忆的路:它是重放安全的
          (`MemoryStore.rebuild` 有行就一动不动),`_apply_memory_trigger` 的实时
          分支与 `_rebuild_memories` 的重建分支从那里往后逐字对称。
        """
        if self.memory_store is None:
            return
        payload = event.get("payload") or {}
        if "importance" not in payload:
            return                      # 作者没声明 → 这一层缺席
        try:
            importance = float(payload["importance"])
        except (TypeError, ValueError):
            logger.warning(
                "规律 %s 的 importance 不是个数(%r)—— 这件事没有人会记得",
                payload.get("rule"), payload.get("importance"),
            )
            return
        if importance <= 0:
            return
        importance = min(1.0, importance)

        owner = str(payload.get("owner") or "").strip()
        witnesses = self._rule_event_witnesses(owner)
        if not witnesses:
            # 不是错,只是这会儿没人在旁边(半夜的江堤)。但**说一句** —— 一个作者
            # 写了 importance 却一条记忆都没落地的世界,沉默地长得和没写一样。
            logger.info(
                "规律 %s 发的 %r 没有见证者(owner=%r)—— 这一刻没人在场,没有记忆落地",
                payload.get("rule"), event.get("type"), owner,
            )
            return

        # 她记住的是那句话;作者没写就退回事件的名字 —— 一个叫得出名字的东西,
        # 比一条空记忆强。
        summary = str(payload.get("text") or "").strip() or str(event.get("type") or "")
        for agent_id in witnesses:
            seed = memory_seed_event(
                agent_id,
                {"kind": WITNESS_MEMORY_KIND, "summary": summary, "importance": importance},
            )
            # 来路留在事件上(不进记忆行):日后要问"这条记忆是哪条规律种的",
            # 日志里答得出来。
            seed["payload"]["rule"] = payload.get("rule")
            seed["payload"]["source_type"] = event.get("type")
            self._record_and_deliver(seed)

    def _after_interaction(
        self, who: str, target: str, verb: str, affordance: Any, here: str,
    ) -> None:
        """这件事真的做成了之后,世界还欠谁一笔 —— **一个接缝,不是三个**。

        三条路都会走到这里(一下子的事、一起做的事、长过程收尾),而"做成之后
        还要做什么"往后只会变多。分散在三个调用点上加的话,第四样东西迟早只加
        在其中两处,而漏掉的那一处照跑、日志干净。

        次序承重:**记忆在前,旁白在后**。记忆是同步的(她当场就记得),旁白是
        一次可能很慢的网络调用丢进线程池 —— 反过来的话没有任何区别,但读代码的
        人会以为旁白挡着记忆。
        """
        self._witness_interaction(who, target, verb, affordance, here)
        self._narrate_interaction(who, target, verb, affordance, here)

    def _narrate_interaction(
        self, who: str, target: str, verb: str, affordance: Any, here: str,
    ) -> None:
        """**人做的那一下也进旁白** —— 此前只有角色的动作有旁白。

        一个玩家和一个角色并排站着擦同一扇窗,世界的量一样地动,而旁白里只有
        她那一句 —— 于是那条时间线读起来像"她一个人在这儿忙活",而他就在旁边。
        这条补的是那半句。

        三道闸,缺一不可:

        - **只管玩家那一半**(`player:` 前缀)。角色的旁白早就有了,走的是行为树
          那条路(`emit_action`);在这里再发一次等于同一件事写两遍旁白。
        - **作者声明了 `importance` 才有**:和见证记忆同一根轴(2.5 血缘四问的
          「变体轴」写死了这一条)—— 擦一次杯子不值得一句旁白,而"值不值得"只有
          作者说得出。
        - **`narrative.player.enabled`,默认关**。旁白是一次 LLM 调用,而它按玩家
          的每一次动作触发 —— 默认开等于替每个已有世界多开一笔账。

        **永不在 tick 线程上调**(和 `emit_action` 那条旁白逐字同一条):丢进
        `_narrative_pool`,世界照走,那句话什么时候写出来什么时候落。
        """
        if not who.startswith(self.PLAYER_PREFIX):
            return
        if getattr(affordance, "importance", None) is None:
            return
        if self._narrative_pool is None or self.narrative_history is None:
            return
        if not self._player_narrative_enabled():
            return
        self._narrative_pool.submit(
            self._generate_narrative,
            who,
            # 形状和角色那条逐字相同(`{"kind", "params"}`),所以旁白提供方
            # **一处分支都不用加** —— 它拿到的只是"谁做了什么"。
            {
                "kind": "interact",
                "params": {
                    "target": target, "verb": verb,
                    "label": affordance.label or verb,
                },
            },
            # 他没有黑板(引擎不模拟他的身体)。**照实说比假装有一份好**:
            # 给一份空的 `raw`,位置那一格是真的。
            {"location": here, "raw": {}},
        )

    def _player_narrative_enabled(self) -> bool:
        """默认 **关**。见 `_narrate_interaction` 第三条。"""
        if self.config_store is None:
            return False
        return bool(self.config_store.get("narrative.player.enabled", default=False))

    def _witness_interaction(
        self, who: str, target: str, verb: str, affordance: Any, here: str,
    ) -> None:
        """一次交互 → 在场者的记忆(witness-memory 的另一半)。

        规律那一半补的是"世界发生的事没人记得";这一半补的是**人做的事也没人
        记得** —— 同一条裂缝的另一端。此前一个人可以当着满屋子人的面把那棵树
        砍了,事件在日志里躺着,而屋里没有一个人记得这件事发生过。

        三条纪律逐字照抄 `_witness_rule_event`,不另立一套:

        - **声明本身就是开关**:作者不在这条能力上写 `importance`,这一层整个
          缺席,行为与从前逐位相同。**没有默认值** —— 给它一个缺省等于替每个
          作者宣布"世界上任何一次交互都值得记一辈子",于是记忆里塞满了谁又端详
          了一次杯子,真正要紧的那几件淹在里面。
        - **谁在场按位置算**:此刻和这件事同处一地的人。位置查不到 = 没有见证者,
          不是"所有人"。
        - **走 `memory_seed` 这条现成的路**,复用 `WITNESS_MEMORY_KIND`,来路
          (`source_type` / `affordance`)写在**事件**上而不是记忆行里。

        ⚠️ **做的人是玩家还是角色,这条路上一处分支都没有。** 见证者从
        `_agent_locations()` 里来(它只装引擎模拟得动的那些人),玩家因此自然
        不在其中 —— 不是被一句 `if` 滤掉的。同理,做这件事的角色**自己也是见证
        者**:她当然记得自己刚干了什么。名字过 `_relation_name`(它两种人都答得
        出),所以一句"谁做的"对玩家和角色是同一句话。

        **算不出来不许掀翻这次交互**(和 `_emit_rule_event` 同一条,只是那边守的是
        tick、这边守的是调用方):世界的量已经落库了,这时候抛出去会让一次成功的
        交互在她眼里变成一句报错,而她刚做过的那件事已经真的发生了。
        """
        try:
            self._seed_interaction_memories(who, target, verb, affordance, here)
        except Exception:  # noqa: BLE001
            logger.warning(
                "%s.%s 的见证者算不出来 —— 这次没有记忆落地", target, verb, exc_info=True
            )

    def _seed_interaction_memories(
        self, who: str, target: str, verb: str, affordance: Any, here: str,
    ) -> None:
        """见证记忆的正文。分出来只为了让上面那一层守得住 —— 别在这里加第二道闸。"""
        if self.memory_store is None:
            return
        importance = getattr(affordance, "importance", None)
        if importance is None:
            return                      # 作者没声明 → 这一层缺席
        importance = min(1.0, float(importance))
        if importance <= 0:
            # 写了 0 和没写不是一句话:前者是"我想过了,不值一提"。两者的**行为**
            # 一样(不记),但前者过了作者那道声明,所以不该被当成 bug 报出来。
            return
        if not here:
            return
        witnesses = [
            agent_id for agent_id, loc in self._agent_locations().items() if loc == here
        ]
        if not witnesses:
            # 不是错,只是这会儿屋里没别人(也没她自己 —— 玩家一个人在场时正是
            # 这个样子)。但**说一句**:一个作者写了 importance 却一条记忆都没落地
            # 的世界,沉默地长得和没写一样。
            logger.info(
                "%s 对 %s 做了 %s,而 %s 这会儿没有见证者 —— 没有记忆落地",
                who, target, verb, here,
            )
            return

        label = affordance.label or verb
        entity = self.ontology.entities.get(target) if self.ontology is not None else None
        what = entity.name if entity is not None and entity.name else target
        actor = self._relation_name(who)
        for agent_id in witnesses:
            # 「我照料,在老槐树」/「江晚照料,在老槐树」—— 句子的形状和共同经历
            # 那条逐字同源(`_settle_joint_experience`),两种记忆读起来才像出自
            # 同一个世界。**自己做的事用「我」**:她的记忆里写着"江晚照料了树",
            # 等于让她从外面看着自己。这一句分的是"是不是我",不是"是不是玩家"。
            subject = "我" if agent_id == who else actor
            seed = memory_seed_event(
                agent_id,
                {
                    "kind": WITNESS_MEMORY_KIND,
                    "summary": f"{subject}{label},在{what}",
                    "importance": importance,
                },
            )
            # 来路留在事件上(不进记忆行),和规律那一半同一条:日后要问"这条记忆
            # 是哪个能力种下的",日志里答得出来。
            seed["payload"]["affordance"] = f"{target}.{verb}"
            seed["payload"]["source_type"] = "entity_interaction"
            seed["payload"]["actor"] = who
            self._record_and_deliver(seed)

    def _rule_event_witnesses(self, owner: str) -> list[str]:
        """这件事发生的时候,谁在场。

        三种 owner 三条路,分开是因为"她在哪"和"它在哪"根本不是一张表:角色的位置
        在黑板上(`_agent_locations`),东西的位置在 `stock_places` 里。
        """
        from anima_world.stocks import WORLD_OWNER

        if not owner or owner == WORLD_OWNER:
            # 整个世界的事:名册里所有人。**在路上的人也算** —— 江水漫堤不会因为
            # 她正走在半道上就绕开她。
            return list(self.agents)

        locs = self._agent_locations()
        if owner.startswith("agent:"):
            subject = owner.split(":", 1)[1]
            # 这件事就发生在她身上,所以**她一定在场**,哪怕此刻走在路上
            # (`_agent_locations` 里在途 = 不在任何地方,那条规矩管的是旁观者)。
            seen = [subject] if subject in self.agents else []
            here = locs.get(subject)
            if here:
                seen += [aid for aid, loc in locs.items() if loc == here and aid != subject]
            return seen

        place = self._entity_place(owner)
        if not place:
            return []
        return [aid for aid, loc in locs.items() if loc == place]

    def _entity_place(self, owner: str) -> str | None:
        """一样东西此刻在哪。`stock_places` 是权威,本体的声明是兜底。

        两处而不是一处,是因为**东西可以在运行期被挪**(`visibility_store.place`),
        而本体里那个 `location` 是它出生时写下的。只读声明的话,一棵被搬走的树
        会一直在原来那个地方被人"看见"。
        """
        store = self.visibility_store
        if store is not None:
            try:
                place = store.place_of(owner)
            except Exception:  # noqa: BLE001 - 读不到位置 = 没有见证者,不是掀翻 tick
                logger.warning("查不到 %r 在哪", owner, exc_info=True)
                place = None
            if place:
                return str(place)
        ontology = self.ontology
        if ontology is not None:
            entity = getattr(ontology, "entities", {}).get(owner)
            location = getattr(entity, "location", None)
            if location:
                return str(location)
        return None

    def actions_now(self) -> dict[str, str]:
        """此刻每个人正在做的那个动作 —— `{"agent:<id>": "work", …}`,**人和玩家一份**。

        两个来源合成一份:她的来自行为树写下的 `_current_action`,他的来自
        `_settle_player_actions` 每 tick 取的那份快照。合并点只有这一处,凡是要问
        "这个人此刻在干嘛"的地方都从这里取 —— `_agents_doing` 的规律侧、感知块里
        那句「此刻在做什么」、自主轮的在场名单,共用同一份事实。

        分两处各算一次的话必然会岔开,而岔开的那天不会报错:一边说她在工作,另一边
        把她写成闲着,两句话进同一份提示词。
        """
        now: dict[str, str] = {}
        for agent_id, action in self._current_action.items():
            if action is not None:
                now[self.stock_owner_of(agent_id)] = str(action.kind)
        for player_id, kind in self._player_action_now.items():
            if kind:
                now[self.stock_owner_of(f"{self.PLAYER_PREFIX}{player_id}")] = str(kind)
        return now

    def _agents_doing(self, action_kind: str) -> list[str]:
        """此刻正在做某个动作的**人**,按 `agent:<id>` 返回 —— 供 `for_each.action` 用。

        修炼、采矿、耕种都是这一类:投入的是**时间**(每 tick 一份),而速率由
        行为者自己的量决定。

        **玩家也在这份名单里。** 她的来源是行为树写下的 `_current_action`,他的是
        `_settle_player_actions` 每 tick 取的那份快照 —— 两个来源,一份名单(见
        `actions_now`)。少了后一半的话,`{"action": …}` 那半边规律对人整个缺席,而
        互补的 `{"not_action": …}` 却算得到他:他只吃得到往下拖的那一条。
        """
        return [
            owner for owner, kind in self.actions_now().items() if kind == action_kind
        ]

    def _maybe_run_autonomy(self, now: WorldTime) -> None:
        """到点了就喊一声"该问问她们了",然后**立刻返回**(autonomy)。

        调度器不认识 LLM,也不认识能力注册表 —— 它只认识 `World` 注进来的这个回调,
        和 `_present_players` / `chat_state` 同一个模式。回调必须自己把活丢到别的
        线程去:**时钟永远不等网络**,这是这个引擎最老的一条不变量。

        节流在这里而不是在回调里:一个漏了节流的回调会变成每 tick 一次 LLM 调用,
        而那在演示速度下是每秒一次。
        """
        hook = self._autonomy_hook
        if hook is None:
            return
        interval = max(1, int(self._autonomy_interval))
        if self.clock % interval:
            return
        due = [
            brain.agent.id for brain in self.agents.values()
            if brain.agent.id not in self._transit   # 在赶路的人不做别的事
        ]
        if not due:
            return
        try:
            hook(due, now)
        except Exception:  # noqa: BLE001 - 自主轮次挂了绝不掀翻 tick
            logger.warning("autonomy hook failed", exc_info=True)

    def _maybe_run_contact(self, now: WorldTime) -> None:
        """到点了就喊一声"看看她们会不会想起谁",然后**立刻返回**(contact)。

        和 `_maybe_run_autonomy` 逐字同构,包括那两条理由:节流在这里而不是在
        回调里(漏了节流就是每 tick 一次判定,演示速度下是每秒一次);回调自己
        负责把要打网络的那一段丢到别的线程去 —— **时钟永远不等网络**。

        ⚠️ **在赶路的人这里不排除。** autonomy 排除他们是因为那一层要她当场做一件
        事,而赶路时她做不了;想起一个人不需要她停下来 —— 在火车上想起谁是最常见
        的情形之一。赶路进的是 `readiness` 那一层(`transit` 硬闸),由 `World` 侧
        按配置决定,不在这里替它做主。
        """
        hook = self._contact_hook
        if hook is None:
            return
        interval = max(1, int(self._contact_interval))
        if self.clock % interval:
            return
        due = [brain.agent.id for brain in self.agents.values()]
        if not due:
            return
        try:
            hook(due, now)
        except Exception:  # noqa: BLE001 - 想起谁这件事挂了绝不掀翻 tick
            logger.warning("contact hook failed", exc_info=True)

    def _fire_due_followups(self) -> None:
        """她说的"等会儿再说"到点了 —— 真的回来敲一次门(chat-agent,#15)。

        只落一行"我等会儿再说"而没有人回来,就又是一条声明过没人读的机制:玩家
        等着,她永远不回来。兑现走 issue #13 那条 `agent_hail` —— **敲门不是对话**,
        不产生记忆、不动关系、不开会话,只是"我回来了"。

        玩家不在场时**不发**,那条约留着等他回来(给不在的人写事件是这个仓库最在意
        的那类错)。但也不永远留着:过期一个世界日就作罢并说明,否则玩家下周登录会
        收到上周那句"等我一下"。
        """
        store = self.chat_state
        if store is None:
            return
        try:
            due = store.due_followups(self.clock)
        except Exception:  # noqa: BLE001 - 读不到约不该掀翻 tick
            logger.warning("读 followup 队列失败", exc_info=True)
            return
        if not due:
            return
        present: dict[str, Any] = {}
        if self._present_players is not None:
            try:
                present = self._present_players() or {}
            except Exception:  # noqa: BLE001
                logger.warning("could not read present players", exc_info=True)
                present = {}
        grace = max(1, 1440 // self._minutes_per_tick())
        for row in due:
            agent_id, player_id = row["agent_id"], row["player_id"]
            brain = self.agents.get(agent_id)
            if brain is None:
                store.mark_followup_fired(row["id"], self.clock)
                continue
            info = present.get(player_id)
            if info is None:
                if self.clock - int(row["due_tick"]) > grace:
                    logger.info(
                        "%s 对 %s 说的「等会儿再说」过期作罢:约在 tick %s,已经过了一个世界日"
                        "而人一直不在场", agent_id, player_id, row["due_tick"],
                    )
                    store.mark_followup_fired(row["id"], self.clock)
                continue
            here = brain.agent.blackboard.read("loc") or brain.agent.location
            store.mark_followup_fired(row["id"], self.clock)
            store.clear_quiet(agent_id, player_id, kind="delay")
            self._record_event({
                "type": "agent_hail",
                "who": agent_id,
                "loc": here,
                "payload": {
                    "agent_id": agent_id,
                    # 玩家收到的推送上写的就是这个。少了它,推送里是「bai 来找你了」。
                    "agent_name": self.agent_display_name(agent_id),
                    "player_id": player_id,
                    "player_name": (info or {}).get("display_name") or player_id,
                    "location": here,
                    "location_name": self.place_name(here or ""),
                    # 这一条不是"闲着想找人说话",是她欠你的那句话。宿主可以按
                    # 这个字段把它显示成"她回来了"而不是"她来打招呼"。
                    "reason": row.get("kind") or "delayed_reply",
                    "note": row.get("reason"),
                },
            })

    def claim_hail(self, agent_id: str, player_id: str) -> str:
        """她这会儿能不能主动去跟这个玩家搭话 —— 能就当场记下水位并返回空串,
        不能就返回一句人话的理由。

        **查和记是同一个调用。** 分成两步的话,两条路(闲着时的
        `_maybe_hail_player`、autonomy 里的 `reach_out` 工具)会各查各的、
        各记各的,水位迟早对不上 —— 而对不上的样子就是玩家连着挨两次搭话。

        这条闸是一次真的对局逼出来的:玩家正一句一句跟她聊着,autonomy 让她
        `reach_out` 插了两次话,两次都是招呼生客的口气(「你是第一次来吧」)——
        而她刚给这个人做过一杯咖啡。`_hailed` 那道"一天一次"只挡住了它自己
        这条路,工具那条整个绕过去了。

        **今天已经跟他说过话也算开过口。** 搭话是开场白,而开场白一天只有一次;
        接着说的那句叫接话,不叫搭话。判据取 `contact_store.last_contact_tick`
        (`World.chat` / `record_chat_turn` 两扇门都写它),所以宿主走哪条门
        进来的对话都算数。
        """
        day = self.world_time().day
        if self._hailed.get((agent_id, player_id)) == day:
            return "今天已经跟他打过招呼了"
        store = getattr(self, "contact_store", None)
        if store is not None:
            try:
                last = store.get(agent_id, player_id).get("last_contact_tick")
            except Exception:  # noqa: BLE001 - 读不到水位不该掀翻 tick
                logger.warning("读 contact 水位失败 agent=%s player=%s",
                               agent_id, player_id, exc_info=True)
                last = None
            if last is not None and self.world_time(int(last)).day == day:
                return "今天已经跟他说过话了 —— 这会儿开口是接话,不是搭话"
        self._hailed[(agent_id, player_id)] = day
        return ""

    def _maybe_hail_player(self, agent: Agent) -> None:
        """「想找个人说说话」时,同地的在场玩家也算一个人(issue #13,访客模型)。

        这是"角色主动来找你"的最小形态,而且刻意挂在 `idle_social` 上而不是 planner
        的动作空间上:**没有 key 就没有 planner**,而没有 key 是默认状态 —— README
        承诺的那一屏必须在默认状态下成立。

        **敲门不是对话。** 这里不产生记忆、不动关系、不写会话:玩家还没回话,什么也
        没发生。否则你会看到"她来找过我",转头问她却毫无印象 —— 照跑,但给错东西。
        真正的对话仍然由玩家发起 `World.chat`,走原来那条完整的链。

        在场以 TTL 为准(`World.who_is_present`),所以不会去敲一个断线三小时的人的门。
        """
        if self._present_players is None:
            return
        here = agent.blackboard.read("loc") or agent.location
        if not here or agent.id in self._transit:
            return  # 在途不算在场,与 _colocated_agents 同一条规矩
        try:
            candidates = [
                (pid, info) for pid, info in self._present_players().items()
                if (info or {}).get("location") == here
            ]
        except Exception:  # noqa: BLE001 - 读不到在场名单就当没人,绝不掀翻 tick
            logger.warning("could not read present players", exc_info=True)
            return
        for player_id, info in candidates:
            # 一天一次。招呼是招呼,不是每 tick 都要拍一下肩膀 —— needs 抖动那一课。
            # 水位和 `reach_out` 那条路共用一个(见 `claim_hail`)。
            if self.claim_hail(agent.id, player_id):
                continue
            self._record_event({
                "type": "agent_hail",
                "who": agent.id,
                "loc": here,
                "payload": {
                    "agent_id": agent.id,
                    "agent_name": self.agent_display_name(agent.id),
                    "player_id": player_id,
                    "player_name": info.get("display_name") or player_id,
                    "location": here,
                    "location_name": self.place_name(here or ""),
                },
            })

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
            "location": self.place_name(
                agent.blackboard.read("loc") or agent.location or ""
            ),
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
        except Exception as exc:  # noqa: BLE001 - a dead judge must not stop the world
            logger.warning("relationship judge failed for %s↔%s", a_id, b_id, exc_info=True)
            self.note_subsystem("relationship_judge", False, f"{type(exc).__name__}: {exc}")
            return
        self.note_subsystem("relationship_judge", result is not None,
                            "" if result is not None else "judge produced no usable verdict")
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
            names = {a_id: context["a"].get("name") or a_id,
                     b_id: context["b"].get("name") or b_id}
            for as_id, target_id, delta, axes in (
                (a_id, b_id, result.delta_a_to_b * factor, result.axes_a_to_b),
                (b_id, a_id, result.delta_b_to_a * factor, result.axes_b_to_a),
            ):
                scaled_axes = {k: v * factor for k, v in axes.items()} if axes else {}
                if _is_noise(delta, scaled_axes):
                    continue  # damped-to-noise or a no-op delta — event-log/SSE noise
                payload = {"kind": "sentiment_delta", "as": as_id, "target": target_id,
                           "delta": delta,
                           "as_name": names[as_id], "target_name": names[target_id]}
                if scaled_axes:  # relations-v5: finer axes ride the same event, same damping
                    payload["axes"] = scaled_axes
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
        self, agent_id: str, player_id: str, player_name: str | None,
        transcript: list[dict[str, Any]],
        conversation_id: int | None = None, conversation_summary: str = "",
    ) -> None:
        """player-visitor: a closed PLAYER conversation gets a verdict from
        the real transcript. Deltas only (the `conversation` event already
        mints the agent's memory — D3); both directions agent↔player, so the
        player rides the whole relationship machinery (bands/edges/relabel)
        with zero changes there. Never blocks: snapshot under the lock,
        judge on the pool.

        `conversation_id` / `conversation_summary` 是**出处**,原样骑在落地的
        那条 `sentiment_delta` 上。它们不进判定的提示词(模型判的是这段转录,
        不是这场对话的编号)—— 它们存在的理由只有一个:玩家那一侧要看得见
        「上一次是什么改变了它」,而一句"你们更亲近了"如果说不出出处,和一根
        进度条没有区别。
        """
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
                "location": self.place_name(
                    brain.agent.blackboard.read("loc") or brain.agent.location or ""
                ),
            }
            # Submit under the lock: stop() nulls the pool refs while holding
            # it, so here the pool is either alive or None — re-read it.
            pool = self._judge_pool
            if pool is None or self._stopped:
                return
            provenance: dict[str, Any] = {
                "conversation_id": int(conversation_id) if conversation_id is not None else None,
                "conversation_summary": str(conversation_summary or ""),
            }
            pool.submit(self._user_judge_worker, agent_id, player_id, context, provenance)

    def _user_judge_worker(self, agent_id: str, player_id: str, context: dict[str, Any],
                           provenance: dict[str, Any] | None = None) -> None:
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
            # 名字随事件走。玩家的 id 是一串 uuid,而这条事件会长成一条
            # `relation_shift` 记忆,那条记忆会被八卦原样转述给别人 ——
            # "她对 8f3c-… 的关系进入「亲近」"传出去没有一个人看得懂。
            names = {agent_id: context["a"].get("name") or agent_id,
                     player_id: context.get("player_name") or player_id}
            for as_id, target_id, delta, axes in (
                (agent_id, player_id, result.delta_a_to_b * factor, result.axes_a_to_b),
                (player_id, agent_id, result.delta_b_to_a * factor, result.axes_b_to_a),
            ):
                scaled_axes = {k: v * factor for k, v in axes.items()} if axes else {}
                if _is_noise(delta, scaled_axes):
                    continue
                payload = {"kind": "sentiment_delta", "as": as_id, "target": target_id,
                           "delta": delta,
                           "as_name": names[as_id], "target_name": names[target_id]}
                if scaled_axes:
                    payload["axes"] = scaled_axes
                # 出处。**两个键一律带上,查不到就写 None / 空串** —— 缺键和
                # 「这一条没有出处」在读的那一侧长得不一样,而后者是一句真话。
                payload.update(provenance or
                               {"conversation_id": None, "conversation_summary": ""})
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
        # 上路的那一下就从可见性表上撤下来 —— 晚一个 tick 撤,那一 tick 里她的
        # 提示词就自相矛盾一次(见 `_unplace_actor`)。
        self._unplace_actor(agent.id)
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
        except Exception as exc:  # noqa: BLE001 - flavor text is never worth a crash
            logger.warning("narrative provider failed for %s", agent_id, exc_info=True)
            self.note_subsystem("narrative", False, f"{type(exc).__name__}: {exc}")
            return
        self.note_subsystem("narrative", True)
        with self._lock:
            if self._stopped or self.narrative_history is None:
                return
            self.narrative_history.append(text)
            narrative_ev = {
                "target_agent_id": agent_id,
                "who": agent_id,
                "type": "narrative",
                "payload": {
                    "text": text,
                    "speaker": agent_id,
                    # 玩家读到的发言人。`speaker` 是机器可读的那一半,留着不动。
                    "speaker_name": self.agent_display_name(agent_id),
                },
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
        return {"location": blackboard.read("loc"), "raw": blackboard.snapshot()}

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
