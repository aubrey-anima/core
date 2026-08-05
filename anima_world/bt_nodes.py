"""Behavior Tree node library.

Pure-Python BT with 4 node types:
- Selector: pick first child that succeeds (OR)
- Sequence: run all children in order, fail fast (AND)
- Condition: read blackboard, return SUCCESS/FAILURE
- Action: execute a callback, return its Status
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, ClassVar


class Status(str, Enum):
    """String enum: Status.SUCCESS == 'SUCCESS' is True (str inheritance)."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    RUNNING = "RUNNING"


class Blackboard:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def read(self, key: str) -> Any:
        return self._data.get(key)

    def write(self, key: str, value: Any) -> None:
        self._data[key] = value

    def snapshot(self) -> dict[str, Any]:
        """整块拿走。**别去摸 `_data`** —— 黑板有可能不住在这个进程里
        (`anima_world.redis_state.RedisBlackboard`),那时候 `_data` 不存在。"""
        return dict(self._data)

    def __repr__(self) -> str:
        return f"Blackboard({self._data!r})"


# Type alias: a callback that takes a blackboard and returns a Status.
NodeCallback = Callable[[Blackboard], Status]


class Node:
    """Base class for behavior tree nodes."""

    name: ClassVar[str] = "node"

    def tick(self, blackboard: Blackboard) -> Status:
        raise NotImplementedError


@dataclass
class Condition(Node):
    """Return SUCCESS if blackboard[key] == expected, else FAILURE."""

    key: str
    expected: Any
    name: ClassVar[str] = "condition"

    def tick(self, blackboard: Blackboard) -> Status:
        return (
            Status.SUCCESS
            if blackboard.read(self.key) == self.expected
            else Status.FAILURE
        )


@dataclass
class MemoryCondition(Node):
    """Read-only condition over `MemoryStore` (M4): count memories matching
    `predicate` for `agent_id`, SUCCESS if the count is >= `expected`.

    Deliberately duck-typed on `memory_store` (any object with a
    `.query(agent_id=...)` method) rather than importing `MemoryStore` —
    keeps this foundational BT module decoupled from the storage layer.
    Complements `Condition` (reads blackboard = current state) with a query
    over history; the two must not be used interchangeably (design.md D7).

    `predicate` supports two forms:
    - ``kind:X`` — count memories with ``kind == X``
    - ``with:agent:Y`` — count memories whose summary mentions agent Y
    """

    agent_id: str
    predicate: str
    expected: int
    memory_store: Any | None = None
    name: ClassVar[str] = "memory_condition"

    def tick(self, blackboard: Blackboard) -> Status:
        store = self.memory_store if self.memory_store is not None else blackboard.read("memory_store")
        if store is None:
            return Status.FAILURE
        memories = store.query(agent_id=self.agent_id)
        count = sum(1 for m in memories if self._matches(m))
        return Status.SUCCESS if count >= self.expected else Status.FAILURE

    def _matches(self, memory: dict[str, Any]) -> bool:
        if self.predicate.startswith("kind:"):
            return memory.get("kind") == self.predicate[len("kind:"):]
        if self.predicate.startswith("with:agent:"):
            partner = self.predicate[len("with:agent:"):]
            return partner in memory.get("summary", "")
        return False


@dataclass
class TimeWindow(Node):
    """SUCCESS while the world clock is inside [start_min, end_min).

    `Condition` can only compare for equality, which cannot express a duty like
    "tend the cafe 08:00–18:00" (bt-duties D4). Reads the minute-of-day the
    scheduler writes onto the blackboard each tick; the BT library stays free
    of any dependency on the clock itself.

    `end_min < start_min` means the window wraps across midnight (22:00–07:00).
    `start_min == end_min` is an empty window and never fires. A blackboard with
    no time on it yet yields FAILURE rather than raising — a tree must never
    explode just because it ran before the first tick wrote the clock.
    """

    start_min: int
    end_min: int
    key: str = "time.minute_of_day"
    name: ClassVar[str] = "time_window"

    def tick(self, blackboard: Blackboard) -> Status:
        now = blackboard.read(self.key)
        if not isinstance(now, int):
            return Status.FAILURE
        if self.start_min == self.end_min:
            return Status.FAILURE
        if self.start_min < self.end_min:
            inside = self.start_min <= now < self.end_min
        else:  # wraps past midnight
            inside = now >= self.start_min or now < self.end_min
        return Status.SUCCESS if inside else Status.FAILURE


COMPARISONS: dict[str, Callable[[float, float], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}


@dataclass
class StockCondition(Node):
    """世界的量到了某个数 —— 于是排班能按量分支(而不只是按钟点)。

    "八点到六点半照看店里"是钟点排班能表达的全部,而人不是那样活的:面粉见底了
    才去进货,树长到够高了才去收,钱不够了才去多做一天工。`TimeWindow` 表达不了
    这一类,`Condition` 只能比黑板上的相等 —— 于是"按世界的状态决定"这件事在树里
    整个缺席,只能靠一个 LLM 规划器去补,而那条路又贵又不该管这么钝的事。

    **只读黑板。** 量由调度器每 tick settle 到 `stock.<owner>.<key>`,和 `need.*`
    同一条路子 —— 这个模块不认得任何存储,也不认得 Redis。

    **而 settle 那一步会先过一遍感知**(`perception.why_not_perceivable`):她感知
    不到的量不会出现在黑板上,于是这里 FAILURE。这条是有意的:一条读得到"暗中的
    恨意"或"隔着半个地图那棵树的高度"的排班,等于让她**用她不可能知道的事做决定**。
    那和角色随口说出矿的确切储量是同一种破戏,只是这次破在行为上 —— 连一行提示词
    都不留,只有一个"她怎么就突然动身了"。

    量还没 settle 上来(这一层没开、第一个 tick 之前、她此刻感知不到)一律 FAILURE
    而不是抛 —— 和 `TimeWindow` / `NeedAction` 同一条:一棵树绝不能因为读不到数据
    就炸掉,这才使得"加了这个叶子的世界"和"没加的世界"逐 tick 一样。

    `owner` 写 `self` 指她自己(settle 时解析成 `agent:<id>`)—— 作者写树的时候
    并不知道这棵树会挂到谁身上。
    """

    owner: str
    key: str
    op: str
    value: float
    name: ClassVar[str] = "stock_condition"

    def __post_init__(self) -> None:
        if self.op not in COMPARISONS:
            # 当场抛,不要默默 FAILURE:一个打错的比较符会让这条分支**永不触发**,
            # 而世界照跑、日志干净、作者以为自己写对了。
            raise ValueError(
                f"stock_condition 不认识的比较符 {self.op!r} —— 只认 {list(COMPARISONS)}"
            )

    def tick(self, blackboard: Blackboard) -> Status:
        current = blackboard.read(f"stock.{self.owner}.{self.key}")
        if not isinstance(current, (int, float)) or isinstance(current, bool):
            return Status.FAILURE
        ok = COMPARISONS[self.op](float(current), float(self.value))
        return Status.SUCCESS if ok else Status.FAILURE


@dataclass
class Action(Node):
    """Execute a callback and return its status.

    On SUCCESS, stores ``action_id`` under ``_selected_action_id`` on the
    blackboard so the caller (Brain) can know which leaf action fired.
    """

    behaviour: NodeCallback
    action_id: str | None = None
    name: ClassVar[str] = "action"

    def tick(self, blackboard: Blackboard) -> Status:
        status = self.behaviour(blackboard)
        if status == Status.SUCCESS and self.action_id is not None:
            blackboard.write("_selected_action_id", self.action_id)
        return status


@dataclass
class PlanAction(Node):
    """The free-time leaf: run whatever the planner scheduled for right now.

    SUCCESS when a current plan step sits on the blackboard (the scheduler
    writes it each tick), FAILURE otherwise — so a Selector falls straight
    through to `idle_wander` when the agent has no plan, and the world behaves
    exactly as it did before the planner existed.

    Reads the blackboard and nothing else: the BT library stays free of any
    dependency on the planner or an LLM.
    """

    action_id: str = "follow_plan"
    name: ClassVar[str] = "plan"

    def tick(self, blackboard: Blackboard) -> Status:
        if not blackboard.read("plan.kind"):
            return Status.FAILURE
        blackboard.write("_selected_action_id", self.action_id)
        return Status.SUCCESS


@dataclass
class IntentAction(Node):
    """她刚决定要做的事 —— **排在身体之下、排班之上**。

    这是"很多进程操作同一个世界"里 agent 那一侧的落点:一个 LLM 驱动的进程说
    "先走到咖啡店,然后干活",队列进来,世界替她走完脚步。她**不该**一步一次网络
    往返地编排走路,那又贵又编得烂。

    位置是有理由的:

    - **在身体之下** —— 饿到一定程度先吃,再去开店。她刚决定的事不该架空紧急带,
      否则"饿死也要把店开了"就回来了(`_wrap_with_needs_band` 花力气解掉的就是它)。
    - **在排班之上** —— 她此刻明确决定的事,该压过排班表。否则"她自己决定"是假的。

    队列空的时候 FAILURE,于是没人调用 `intend()` 的世界**行为逐字不变** ——
    这一层是纯加法。
    """

    action_id: str = "follow_intent"
    name: ClassVar[str] = "intent"

    def tick(self, blackboard: Blackboard) -> Status:
        queue = blackboard.read("intent.queue") or []
        if not queue:
            return Status.FAILURE
        blackboard.write("_selected_action_id", self.action_id)
        return Status.SUCCESS


@dataclass
class NeedAction(Node):
    """needs-v3 leaf: fire a restorative action when a need runs low.

    Reads `need.<name>` the scheduler settles onto the blackboard each tick.
    A blackboard with no need values (needs disabled, or pre-first-tick)
    yields FAILURE — the band is inert and the tree behaves exactly as v2,
    which is what makes wrapping every tree unconditionally safe.

    **迟滞**:`threshold` 是开始恢复的线,`release` 是收工的线。一旦已经在补这条
    需求,就补到 `release` 才罢手 —— 否则跨过 `threshold` 的那一 tick 就收工,角色
    永远卡在触发线上方抖(实测 hunger 只有两个取值),一顿饱饭都没吃过,而每次
    抖动都发一条 agent_action + 一条 narrative。

    判据不是新开一份状态,而是 `need._restoring`:scheduler 每 tick 写"当前动作在
    补哪几条需求"。所以重启即自愈,最坏是早收工一 tick 然后重新触发。
    `release=None` 的节点(老库里的作者树)逐 tick 保持旧行为。
    """

    need: str
    threshold: float
    action_id: str
    release: float | None = None
    name: ClassVar[str] = "need_action"

    def tick(self, blackboard: Blackboard) -> Status:
        value = blackboard.read(f"need.{self.need}")
        if not isinstance(value, (int, float)):
            return Status.FAILURE
        limit = self.threshold
        if self.release is not None:
            restoring = blackboard.read("need._restoring") or ()
            if self.need in restoring:
                limit = self.release
        if float(value) >= limit:
            return Status.FAILURE
        blackboard.write("_selected_action_id", self.action_id)
        return Status.SUCCESS


@dataclass
class Selector(Node):
    """Try children in order; return SUCCESS on first non-FAILURE child.

    FAIL → try next child.
    SUCCESS → short-circuit, return SUCCESS.
    RUNNING → short-circuit, return RUNNING (not FAILURE, so don't fall through).
    """

    children: list[Node | NodeCallback]
    name: ClassVar[str] = "selector"

    def tick(self, blackboard: Blackboard) -> Status:
        for child in self.children:
            status = _tick_any(child, blackboard)
            if status != Status.FAILURE:
                return status
        return Status.FAILURE


@dataclass
class Sequence(Node):
    """Run children in order; return FAILURE on first non-SUCCESS child.

    SUCCESS → continue to next child.
    FAILURE → short-circuit, return FAILURE.
    RUNNING → short-circuit, return RUNNING (not SUCCESS, so stops the chain).
    """

    children: list[Node | NodeCallback]
    name: ClassVar[str] = "sequence"

    def tick(self, blackboard: Blackboard) -> Status:
        for child in self.children:
            status = _tick_any(child, blackboard)
            if status != Status.SUCCESS:
                return status
        return Status.SUCCESS


def _tick_any(node_or_cb: Node | NodeCallback, blackboard: Blackboard) -> Status:
    """Tick a Node (has .tick) or a bare callback (called directly).

    Callbacks may accept 0 args (``lambda: Status.SUCCESS``) or 1 arg
    (``lambda bb: ...``). We call with blackboard when the signature
    allows it, otherwise with no args.
    """
    if not isinstance(node_or_cb, Node) and callable(node_or_cb):
        try:
            return node_or_cb(blackboard)  # type: ignore[operator,no-any-return]
        except TypeError:
            return node_or_cb()  # type: ignore[operator,no-any-return]
    return node_or_cb.tick(blackboard)


def tick(root: Node, blackboard: Blackboard) -> Status:
    """Tick the root node of a behavior tree. Convenience entrypoint."""
    return root.tick(blackboard)


# ── M3 default BT factory ──────────────────────────────────────────────────


def default_bt() -> Selector:
    """Default autonomous BT used by the `serve` command.

    Chat is handled by the standalone M3.5 chat subsystem, not the BT, so the
    default tree only drives autonomous behavior: wander when idle.
    """
    return Selector(
        [
            Action(lambda bb: Status.SUCCESS, "idle_wander"),
        ]
    )
