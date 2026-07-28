"""Behavior Tree node library.

Pure-Python BT with 4 node types:
- Selector: pick first child that succeeds (OR)
- Sequence: run all children in order, fail fast (AND)
- Condition: read blackboard, return SUCCESS/FAILURE
- Action: execute a callback, return its Status
"""

from __future__ import annotations

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
