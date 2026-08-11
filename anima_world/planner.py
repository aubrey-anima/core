"""The free-time planner: an LLM decides what an agent does between duties.

bt-duties gave every agent a behavior tree whose duty branches fire on the
world clock. This module fills what the duties leave open: once a world day,
in the background, an LLM is asked to lay out a sequence of actions for that
agent's free windows, in character. The scheduler installs the plan and feeds
the current step to the tree's `follow_plan` leaf, which sits below every duty
and above `idle_wander`.

Three rules hold this together:

- **Never on the tick thread.** The LLM is called from a background worker.
  A synchronous LLM call inside the tick loop is what froze this world for five
  days; the tick thread only ever reads an already-installed plan.
- **Free time is derived, never stored.** Free windows = the day minus the
  agent's duty windows, read off its `time_window` BT nodes. Storing them
  separately would let them drift away from the duties they came from.
- **Degrade, never break.** A step naming a place that doesn't exist is
  dropped; a plan whose steps are all bad becomes no plan at all; an LLM that
  errors, times out or babbles yields no plan. No plan means the `follow_plan`
  leaf fails and the tree falls through to `idle_wander` — exactly the behavior
  that existed before this module. The floor is the old world, never worse.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from anima_world.world_time import MINUTES_PER_DAY

logger = logging.getLogger(__name__)

# Duty-only kinds: the LLM plans free time, so it does not get to schedule the
# job it is already contractually doing (`work` is what the duty branch emits).
_DUTY_ONLY_KINDS = {"work", "sleep"}

# 需求键 → 给 LLM 看的说法。规划器读的是"她这会儿什么状态",不是内部变量名。
_NEED_LABELS = {"energy": "精力", "hunger": "饱腹", "social": "社交"}

_DEFAULT_PROMPT = (
    "你是{name}。{personality}\n\n"
    "{goals}"
    "{situation}"
    "现在是第 {day} 天。你的固定日程之外，还有这些空闲时段（24 小时制）：\n"
    "{free_windows}\n\n"
    "你在空闲时段能做的事：\n{action_space}\n\n"
    "{memories}"
    "请按你的性格安排这些空闲时段，输出一个 JSON 数组，每项形如：\n"
    '{{"start_min": <当天 0:00 起的分钟数，0 到 1439>, "kind": "<动作>", '
    '"params": {{...}}, "note": "<一句话理由>"}}\n'
    "只安排今天以内的事：start_min 最大 1439（23:59）。"
    "空闲时段的末端写作 24:00，那是这一天的收尾，不是可以起头的时刻；"
    "过了午夜的事留给明天那份计划。\n"
    "只输出 JSON 数组，不要解释。动作和参数必须来自上面的清单。"
)
# 上界为什么要写出来:`_format_windows` 把一天的末端印成 `24:00`（作为**结束**
# 时刻那是对的），于是模型顺手把 `start_min` 写成 1440、1500、1530 —— 每一条都
# 被 `validate_steps` 丢掉,只在日志里留一行 "starts outside the day"。世界照跑,
# 她傍晚那几步计划却没了,而没有任何人会去看那行日志。丢弃这道闸留着(不许 LLM
# 说的东西不经核对就进世界),这里补的是它从来没被告知过范围。


@dataclass(frozen=True)
class PlanStep:
    start_min: int
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_min": self.start_min,
            "kind": self.kind,
            "params": dict(self.params),
            "note": self.note,
        }


@dataclass(frozen=True)
class Plan:
    agent_id: str
    day: int
    steps: tuple[PlanStep, ...] = ()

    def step_at(self, minute_of_day: int) -> PlanStep | None:
        """The last step that has already started — a time-point trigger, not a
        duration model, so a skipped tick can never knock the plan out of sync."""
        current: PlanStep | None = None
        for step in self.steps:
            if step.start_min <= minute_of_day:
                current = step
            else:
                break
        return current

    def to_payload(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "day": self.day,
            "steps": [s.to_dict() for s in self.steps],
        }


def free_windows(duties: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The day minus the duty windows. A duty that wraps midnight is split in
    two before subtracting, so the arithmetic stays on a simple line."""
    busy: list[tuple[int, int]] = []
    for start, end in duties:
        if start == end:
            continue
        if start < end:
            busy.append((start, end))
        else:  # wraps midnight → two spans
            busy.append((start, MINUTES_PER_DAY))
            busy.append((0, end))
    busy.sort()

    free: list[tuple[int, int]] = []
    cursor = 0
    for start, end in busy:
        if start > cursor:
            free.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < MINUTES_PER_DAY:
        free.append((cursor, MINUTES_PER_DAY))
    return free


def action_space(
    bt_store: Any, location_store: Any, agent_ids: list[str], me: str
) -> dict[str, dict[str, list[str]]]:
    """What this agent could actually do right now, expanded from the live world.

    Kinds come from `bt_actions`; `walk` targets are the map's point rows (never
    regions — agents stand on points); `chat` targets are the other agents. The
    LLM picks from this, so it cannot invent a place or a person.
    """
    kinds = {row["kind"] for row in bt_store.actions()} - _DUTY_ONLY_KINDS
    points = [r["id"] for r in location_store.all() if r.get("kind", "point") == "point"]
    others = [a for a in agent_ids if a != me]

    space: dict[str, dict[str, list[str]]] = {}
    for kind in sorted(kinds):
        if kind == "walk":
            space[kind] = {"location": points}
        elif kind == "chat":
            space[kind] = {"target": others}
        else:
            space[kind] = {}
    return space


def validate_steps(raw: Any, space: dict[str, dict[str, list[str]]]) -> tuple[PlanStep, ...]:
    """Keep the steps that name real things; drop the rest, one by one.

    A single bad step must not cost the whole plan, and nothing the LLM says
    may reach the world without matching the action space it was given.
    """
    if not isinstance(raw, list):
        return ()
    steps: list[PlanStep] = []
    for item in raw:
        if not isinstance(item, dict):
            logger.warning("plan step is not an object, dropping: %r", item)
            continue
        kind = item.get("kind")
        if kind not in space:
            logger.warning("plan step names an unknown action %r, dropping", kind)
            continue
        try:
            start = int(item["start_min"])
        except (KeyError, TypeError, ValueError):
            logger.warning("plan step has no usable start_min, dropping: %r", item)
            continue
        if not 0 <= start < MINUTES_PER_DAY:
            logger.warning("plan step starts outside the day (%s), dropping", start)
            continue

        params = item.get("params") or {}
        if not isinstance(params, dict):
            logger.warning("plan step params are not an object, dropping: %r", item)
            continue
        allowed = space[kind]
        bad = False
        for key, choices in allowed.items():
            value = params.get(key)
            if value not in choices:
                logger.warning(
                    "plan step %r names %s=%r, which is not in the world, dropping",
                    kind, key, value,
                )
                bad = True
                break
        if bad:
            continue
        params = {k: v for k, v in params.items() if k in allowed}
        steps.append(PlanStep(start, kind, params, str(item.get("note") or "")))

    steps.sort(key=lambda s: s.start_min)
    return tuple(steps)


def _extract_json(text: str) -> Any:
    """Pull a JSON array out of an LLM reply, fenced or not. None when there
    isn't one — a chatty model must not take the world down."""
    cleaned = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.MULTILINE)
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    match = re.search(r"\[.*\]", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


def _format_windows(windows: list[tuple[int, int]]) -> str:
    def hhmm(m: int) -> str:
        return f"{m // 60:02d}:{m % 60:02d}"

    if not windows:
        return "（今天没有空闲时间）"
    return "\n".join(f"- {hhmm(s)} 到 {hhmm(e)}" for s, e in windows)


def _format_space(space: dict[str, dict[str, list[str]]]) -> str:
    lines = []
    for kind, params in space.items():
        if params:
            detail = "，".join(f"{k} 可选：{'/'.join(v)}" for k, v in params.items())
            lines.append(f"- {kind}（{detail}）")
        else:
            lines.append(f"- {kind}")
    return "\n".join(lines)


class SyncLLM:
    """Blocking `complete_sync` over the project's async LLM clients.

    The planner runs on a worker thread that has no event loop of its own, so
    each call spins one up. Blocking here is fine and in fact the point: this
    thread exists precisely so the tick thread never waits on an LLM. The
    timeout is read live from config, like every other M5 setting.
    """

    def __init__(
        self,
        client: Any,
        config_store: Any | None = None,
        timeout: float = 30.0,
        timeout_key: str = "planner.timeout",
    ) -> None:
        self._client = client
        self._config_store = config_store
        self._timeout = timeout
        # llm-relationship-judge code review #5: callers with their own
        # latency profile (the chat judge) read their own config key instead
        # of being silently coupled to planner tuning.
        self._timeout_key = timeout_key

    def complete_sync(self, messages: list[dict[str, str]]) -> str:
        timeout = self._timeout
        if self._config_store is not None:
            timeout = self._config_store.get(self._timeout_key, default=timeout)

        async def _run() -> str:
            return await asyncio.wait_for(self._client.complete(messages), timeout=float(timeout))

        return asyncio.run(_run())


class Planner:
    """Builds one agent's free-time plan. Called only from a background worker.

    Every collaborator is injected as a plain callable or duck-typed store, so
    this module imports no scheduler and no storage layer.
    """

    def __init__(
        self,
        llm: Any,
        bt_store: Any,
        location_store: Any,
        persona_provider: Callable[[str], dict[str, Any]],
        agent_ids: Callable[[], list[str]],
        duty_windows: Callable[[str], list[tuple[int, int]]],
        prompt_store: Any | None = None,
        memory_store: Any | None = None,
        situation_provider: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self._llm = llm
        self._bt_store = bt_store
        self._location_store = location_store
        self._persona_provider = persona_provider
        self._agent_ids = agent_ids
        self._duty_windows = duty_windows
        self._prompt_store = prompt_store
        self._memory_store = memory_store
        # 规划器此前只看得见"我是谁、我有哪些空窗、能做什么、记得什么"。
        # 它看不见自己站在哪、饿不饿、有没有钱、别人这会儿在忙什么 —— 于是它排出
        # 来的一天,依据比世界实际拥有的信息少得多:让一个在家的人去"继续在咖啡店
        # 待着",让一个身无分文的人去买东西,让两个人各自计划去找对方。
        self._situation_provider = situation_provider

    def make_plan(self, agent_id: str, day: int) -> Plan | None:
        """Ask the LLM for a day of free time. `None` on any failure — the
        caller leaves the agent planless and the tree falls back to idle."""
        space = action_space(
            self._bt_store, self._location_store, self._agent_ids(), agent_id
        )
        windows = free_windows(self._duty_windows(agent_id))
        if not windows or not space:
            return None

        messages = self._build_messages(agent_id, day, windows, space)
        try:
            reply = self._llm.complete_sync(messages)
        except Exception:  # noqa: BLE001 - a dead LLM must not stop the world
            logger.warning("planner LLM failed for %s", agent_id, exc_info=True)
            return None

        parsed = _extract_json(reply or "")
        steps = validate_steps(parsed, space)
        if not steps:
            logger.warning("planner produced no usable steps for %s; leaving it planless", agent_id)
            return None
        return Plan(agent_id=agent_id, day=day, steps=steps)

    def _situation_block(self, agent_id: str) -> str:
        """此刻的处境:在哪、需求、钱包、别人在做什么。

        与 `goals` 同一个套路 —— 渲染成一个自洽的块(没内容就是空串),所以模板不必
        写 if 分支;而老模板没有这个占位符也照样能渲染(`str.format` 忽略多余 kwarg)。
        """
        if self._situation_provider is None:
            return ""
        try:
            ctx = self._situation_provider(agent_id) or {}
        except Exception:  # noqa: BLE001 - 规划永远不该死于一次世界读
            logger.warning("situation provider failed for %s; planning blind", agent_id, exc_info=True)
            return ""
        lines: list[str] = []
        if ctx.get("location"):
            lines.append(f"- 你现在在{ctx['location']}")
        needs = ctx.get("needs") or {}
        if needs:
            readable = "、".join(
                f"{_NEED_LABELS.get(k, k)} {v:.0%}" for k, v in sorted(needs.items())
                if isinstance(v, (int, float))
            )
            if readable:
                lines.append(f"- 你的状态:{readable}")
        if ctx.get("money") is not None:
            lines.append(f"- 你身上有 {ctx['money']:.0f} 块钱")
        others = ctx.get("others") or []
        if others:
            lines.append("- 这会儿别人在做什么:" + ";".join(others))
        if not lines:
            return ""
        return "你此刻的处境：\n" + "\n".join(lines) + "\n\n"

    def _build_messages(
        self,
        agent_id: str,
        day: int,
        windows: list[tuple[int, int]],
        space: dict[str, dict[str, list[str]]],
    ) -> list[dict[str, str]]:
        persona = self._persona_provider(agent_id) or {}
        template = _DEFAULT_PROMPT
        if self._prompt_store is not None:
            template = self._prompt_store.get("planner.freetime", default=_DEFAULT_PROMPT)

        # prompt-grounding: goals render as one self-contained block (empty
        # string when the agent has none) so templates never need an
        # if-no-goals branch; templates predating {goals} keep working
        # because str.format ignores unused kwargs.
        goals = persona.get("goals") or []
        goals_block = (
            "你当前的目标：\n" + "\n".join(f"- {g}" for g in goals) + "\n\n" if goals else ""
        )
        variables = {
            "name": persona.get("name") or agent_id,
            "personality": persona.get("personality") or "",
            "day": day,
            "free_windows": _format_windows(windows),
            "action_space": _format_space(space),
            "memories": self._memory_block(agent_id),
            "goals": goals_block,
            "situation": self._situation_block(agent_id),
        }
        try:
            prompt = template.format(**variables)
        except (KeyError, IndexError, ValueError):
            # A stored template referencing an unknown placeholder (a
            # direct-SQL edit, or sample-vars drift) must degrade to the
            # default prompt, not silently disable planning for every agent
            # forever (prompt-grounding code review #4).
            logger.warning("planner.freetime template failed to render; using the default prompt")
            prompt = _DEFAULT_PROMPT.format(**variables)
        messages = []
        if self._prompt_store is not None:
            world = self._prompt_store.get("world.setting", default="").strip()
            if world:
                messages.append({"role": "system", "content": world})
        messages.append({"role": "user", "content": prompt})
        return messages

    def _memory_block(self, agent_id: str, k: int = 3) -> str:
        if self._memory_store is None:
            return ""
        try:
            memories = self._memory_store.query(agent_id=agent_id)[:k]
        except Exception:  # noqa: BLE001
            return ""
        if not memories:
            return ""
        bullets = "\n".join(f"- {m['summary']}" for m in memories)
        return f"你最近记得的事：\n{bullets}\n\n"
