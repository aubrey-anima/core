"""Beat director: authored story beats fired into a running world.

The beat script is CONFIGURATION (like world_seed.json and the map): the
engine never edits it and it stays out of the event log. Which beats have
FIRED is simulation history: each firing records one `beat_fired` event and
the fired-set is rebuilt from those events on recovery (beat-director D1).

Loading is strict — a malformed script refuses to start, with every error
listed, because the script is authored data and feedback must be immediate.
Firing degrades — a predicate that fails to evaluate reads as "not met", a
bad payload op is skipped with a warning — because a running world must
never crash over one beat (D4).

This module is pure data + evaluation; it imports no scheduler/storage code.
The Scheduler owns the side effects (recording events, registering agents).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable

from anima_world.types import Projection
from anima_world.world_time import MINUTES_PER_DAY, WorldTime

logger = logging.getLogger(__name__)

# Ops whose expansion is pure event construction (`expand_event_op`).
# `agent_join` and `location_desc` need scheduler side effects and are
# handled there (Scheduler._expand_beat_op).
EVENT_OPS = {
    "memory", "broadcast_memory", "sentiment_delta", "r_type", "persona_update",
    # 物质层:op 曾经只能改"她怎么想",改不了"她有什么"。一个作者写不出"父亲的
    # 怀表在这一幕里被弄丢了"——只能写一条"她觉得很难过"的记忆去暗示。这两条
    # 展开成账本已有的事件类型(payment / item_transfer),余额与库存本来就是它们
    # 的投影,所以不新增 schema、不改 db 格式。
    "pay", "grant_item",
}
VALID_OPS = EVENT_OPS | {"agent_join", "location_desc", "agent_leave", "agent_return"}

# 物质 op 里 `from`/`to` 允许写的非角色持有者。金库允许负债(economy.TOWN),
# 世界是凭空来源 —— 一件道具"本来就在她口袋里"不需要有人先失去它。
BEAT_WORLD_HOLDER = "__world__"
_TOWN_HOLDER = "__town__"  # economy.TOWN,这里不 import 存储层

# 导演能观察到的世界。谓词曾经只有两个(关系值、同地),于是节拍脚本对世界的绝大
# 部分状态是瞎的 —— 需求、钱、物品、关系描述、记忆一律看不见,剧情只能靠"到点了"
# 和"两个人碰上了"来推。这些都是投影/黑板里已经有的量,不进事件日志、不改 db。
_VALID_PREDICATES = {
    "sentiment", "co_located", "r_type", "need", "money", "has_item", "memory",
}

NEED_NAMES = ("energy", "hunger", "social")

# 谓词的必填字段 —— 与 `OP_REQUIRED_FIELDS` 同一个用途:作者照文档写就该能过,
# 而镜像端照这份表写自己的校验。`anima-world contract` 报它。
PREDICATE_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "sentiment": ("as", "target", "op", "value"),
    "co_located": ("agents",),
    "r_type": ("as", "target", "contains"),
    "need": ("agent", "need", "op", "value"),
    "money": ("agent", "op", "value"),
    "has_item": ("agent", "item"),
    "memory": ("agent", "contains"),
}
_AGENT_BUNDLE_KEYS = {"id", "name", "location", "personality"}  # world_seed agent entry shape


class BeatScriptError(ValueError):
    """A beat script failed validation; carries every error found."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("invalid beat script:\n" + "\n".join(f"- {e}" for e in errors))


class BeatScript:
    """A validated, ordered list of beat dicts."""

    def __init__(self, beats: list[dict[str, Any]]):
        self.beats = beats

    @classmethod
    def load(cls, path: str | Path) -> "BeatScript":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BeatScriptError([f"cannot read beat script {path}: {exc}"]) from exc
        return cls.from_data(data)

    @classmethod
    def from_data(cls, data: Any) -> "BeatScript":
        errors = _validate_script(data)
        if errors:
            raise BeatScriptError(errors)
        beats = [dict(b) for b in data["beats"]]
        _warn_unpaired_leaves(beats)
        return cls(beats)


# ── validation (strict, load-time) ───────────────────────────────────────────


def _validate_script(data: Any) -> list[str]:
    if not isinstance(data, dict) or not isinstance(data.get("beats"), list):
        return ["script must be an object with a 'beats' list"]
    errors: list[str] = []
    ids: set[str] = set()
    beats = data["beats"]
    for i, beat in enumerate(beats):
        label = f"beats[{i}]"
        if not isinstance(beat, dict):
            errors.append(f"{label}: not an object")
            continue
        beat_id = beat.get("id")
        if not isinstance(beat_id, str) or not beat_id:
            errors.append(f"{label}: missing or empty 'id'")
        elif beat_id in ids:
            errors.append(f"{label}: duplicate id {beat_id!r}")
        else:
            ids.add(beat_id)
            label = f"beat {beat_id!r}"
        if beat.get("once") not in (None, True):
            errors.append(f"{label}: 'once' must be true — repeating beats are not supported (v1)")
        errors.extend(_validate_trigger(beat.get("trigger"), label))
        errors.extend(_validate_payload(beat.get("payload"), label))
    errors.extend(_validate_after_graph(beats, ids))
    return errors


def _validate_trigger(trigger: Any, label: str) -> list[str]:
    if not isinstance(trigger, dict) or not any(k in trigger for k in ("at", "after", "when")):
        return [f"{label}: 'trigger' must contain at least one of at/after/when"]
    errors: list[str] = []
    at = trigger.get("at")
    if at is not None:
        if not isinstance(at, dict) or not isinstance(at.get("day"), int) or at["day"] < 0:
            errors.append(f"{label}: trigger.at needs an integer day >= 0")
        else:
            minute = at.get("minute_of_day", 0)
            if not isinstance(minute, int) or not (0 <= minute < MINUTES_PER_DAY):
                errors.append(f"{label}: trigger.at.minute_of_day must be an integer in [0, {MINUTES_PER_DAY})")
    after = trigger.get("after")
    if after is not None and (not isinstance(after, str) or not after):
        errors.append(f"{label}: trigger.after must be a beat id string")
    when = trigger.get("when")
    if when is not None:
        if not isinstance(when, list):
            errors.append(f"{label}: trigger.when must be a list of predicates")
        else:
            for pred in when:
                errors.extend(_validate_predicate(pred, label))
    return errors


def _needs_fields(kind: str) -> tuple[str, ...]:
    """每个谓词的必填字段 —— 与 op 表同一个用途,`contract` 也报它。"""
    return PREDICATE_REQUIRED_FIELDS.get(kind, ())


def _validate_predicate(pred: Any, label: str) -> list[str]:
    if not isinstance(pred, dict):
        return [f"{label}: predicate is not an object"]
    kind = pred.get("pred")
    if kind not in _VALID_PREDICATES:
        return [f"{label}: unknown predicate {kind!r} (supported: {sorted(_VALID_PREDICATES)})"]
    errors: list[str] = []
    for field in _needs_fields(kind):
        if field not in pred:
            errors.append(f"{label}: {kind} predicate needs {field!r}")
    for field in ("as", "target", "agent", "need", "item"):
        if field in _needs_fields(kind) and field in pred:
            if not isinstance(pred.get(field), str) or not pred[field]:
                errors.append(f"{label}: {kind} predicate needs a non-empty {field!r}")
    if "op" in _needs_fields(kind) and pred.get("op") not in ("gte", "lte"):
        errors.append(f"{label}: {kind} predicate op must be 'gte' or 'lte'")
    if "value" in _needs_fields(kind) and not isinstance(pred.get("value"), (int, float)):
        errors.append(f"{label}: {kind} predicate needs a numeric 'value'")
    if kind == "need" and pred.get("need") not in (None, *NEED_NAMES):
        errors.append(
            f"{label}: unknown need {pred.get('need')!r} (supported: {sorted(NEED_NAMES)})"
        )
    if kind == "co_located":
        agents = pred.get("agents")
        if (
            not isinstance(agents, list)
            or len(agents) < 2
            or not all(isinstance(a, str) and a for a in agents)
        ):
            errors.append(f"{label}: co_located predicate needs a list of >= 2 agent ids")
    if kind in ("r_type", "memory") and not isinstance(pred.get("contains"), str):
        errors.append(f"{label}: {kind} predicate needs a string 'contains'")
    return errors


_OP_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "memory": ("agent_id", "summary"),
    "broadcast_memory": ("location", "summary"),
    "sentiment_delta": ("as", "target", "delta"),
    "r_type": ("as", "target"),
    "persona_update": ("agent_id", "spec"),
    "location_desc": ("location", "description"),
    "agent_leave": ("agent_id",),
    "agent_return": ("agent_id", "location"),
    "pay": ("from", "to", "amount"),
    "grant_item": ("agent_id", "item_id"),
}


# 对外自述(`anima-world contract`)用的完整表。`agent_join` 的必填是一个 agent
# 对象(形状 = 种子里的 agent 条目),它走 `_validate_agent_bundle` 而不是通用的
# 必填字段循环 —— 但契约面上不该因为实现分了两条路就缺一格,镜像端会照着这份
# 表去写自己的校验。`VALID_OPS` 与它必须逐项对齐(test_contract_command.py 守着)。
OP_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    **_OP_REQUIRED_FIELDS,
    "agent_join": ("agent",),
}


def _validate_payload(payload: Any, label: str) -> list[str]:
    if not isinstance(payload, list) or not payload:
        return [f"{label}: 'payload' must be a non-empty list of ops"]
    errors: list[str] = []
    for j, op in enumerate(payload):
        op_label = f"{label}.payload[{j}]"
        if not isinstance(op, dict):
            errors.append(f"{op_label}: not an object")
            continue
        kind = op.get("op")
        if kind not in VALID_OPS:
            errors.append(f"{op_label}: unknown op {kind!r} (supported: {sorted(VALID_OPS)})")
            continue
        for field in _OP_REQUIRED_FIELDS.get(kind, ()):
            if field not in op:
                errors.append(f"{op_label}: op {kind!r} requires field {field!r}")
        if kind == "sentiment_delta" and not isinstance(op.get("delta"), (int, float)):
            errors.append(f"{op_label}: sentiment_delta 'delta' must be numeric")
        if kind == "r_type" and "r_type" not in op and "r_type_back" not in op:
            errors.append(f"{op_label}: r_type op needs r_type and/or r_type_back")
        if kind == "persona_update" and not isinstance(op.get("spec"), dict):
            errors.append(f"{op_label}: persona_update 'spec' must be an object")
        if kind in ("memory", "broadcast_memory"):
            errors.extend(_validate_memory_fields(op, op_label))
        if kind == "agent_join":
            errors.extend(_validate_agent_bundle(op.get("agent"), op_label))
    return errors


def _validate_memory_fields(mem: Any, label: str) -> list[str]:
    """Numeric/typed checks shared by memory ops and agent_join.memories —
    a bad importance must fail at LOAD, not as a float() at fire time (the
    record loop runs outside the per-op guard)."""
    errors: list[str] = []
    if "importance" in mem and not isinstance(mem.get("importance"), (int, float)):
        errors.append(f"{label}: 'importance' must be numeric")
    if "summary" in mem and not isinstance(mem.get("summary"), str):
        errors.append(f"{label}: 'summary' must be a string")
    return errors


def _validate_agent_bundle(bundle: Any, label: str) -> list[str]:
    """agent_join sub-structures (relations/memories/goals) are validated as
    strictly as top-level ops — their expansion events bypass the per-op
    guard once recorded, so garbage must never load."""
    if not isinstance(bundle, dict) or not _AGENT_BUNDLE_KEYS.issubset(bundle):
        return [f"{label}: agent_join needs an 'agent' object with {sorted(_AGENT_BUNDLE_KEYS)}"]
    errors: list[str] = []
    goals = bundle.get("goals")
    if goals is not None and not isinstance(goals, (str, list)):
        errors.append(f"{label}: agent.goals must be a string or list")
    duties = bundle.get("duties")
    if duties is not None and not isinstance(duties, list):
        errors.append(f"{label}: agent.duties must be a list")
    relations = bundle.get("relations")
    if relations is not None:
        if not isinstance(relations, list):
            errors.append(f"{label}: agent.relations must be a list")
        else:
            for k, rel in enumerate(relations):
                rel_label = f"{label}.agent.relations[{k}]"
                if not isinstance(rel, dict) or not isinstance(rel.get("with"), str) or not rel["with"]:
                    errors.append(f"{rel_label}: needs a non-empty 'with' agent id")
                elif "sentiment" in rel and not isinstance(rel["sentiment"], (int, float)):
                    errors.append(f"{rel_label}: 'sentiment' must be numeric")
    memories = bundle.get("memories")
    if memories is not None:
        if not isinstance(memories, list):
            errors.append(f"{label}: agent.memories must be a list")
        else:
            for k, mem in enumerate(memories):
                mem_label = f"{label}.agent.memories[{k}]"
                if not isinstance(mem, dict) or not isinstance(mem.get("summary"), str):
                    errors.append(f"{mem_label}: needs a string 'summary'")
                else:
                    errors.extend(_validate_memory_fields(mem, mem_label))
    # 角色卡走**和作者层同一份判断**(`character_card.card_errors`):中途入场的人
    # 一样要出现在玩家的通讯录里,而两份判断迟早给出不同答案。
    if "card" in bundle:
        from anima_world.character_card import card_errors

        errors.extend(card_errors(bundle.get("card"), label=f"{label}.agent"))
    return errors


_OP_AGENT_FIELDS = ("agent_id", "as", "target")


def beat_script_warnings(
    data: Any,
    *,
    known_agents: Iterable[str] = (),
    known_locations: Iterable[str] = (),
) -> list[str]:
    """脚本引用了世界里不存在的东西 —— 一行一条,**只警告不拒绝**。

    为什么不能拒绝:一个 beat 完全可以先 `agent_join` 一个新角色,后面的 beat 再对
    他做事;那时 `known_agents` 里当然没有他。同理,节拍可以指向种子之外的地点。
    把这类检查升级成加载期错误,会让设计正确的脚本在升级后开不了机。

    但沉默也不行:引用错一个 id,那个 beat 会**静默作废并且被永久标记已触发**
    (`beat_fired` 是历史,重启不重放)—— 剧情就这么没了,而且不可挽回。
    """
    if not isinstance(data, dict):
        return []
    beats = data.get("beats")
    if not isinstance(beats, list):
        return []

    agents = set(known_agents)
    # 脚本自己引进来的角色也算数,否则"先入场再使用"会被误报。
    for beat in beats:
        for op in (beat.get("payload") or []) if isinstance(beat, dict) else []:
            if isinstance(op, dict) and op.get("op") == "agent_join":
                bundle = op.get("agent")
                if isinstance(bundle, dict) and bundle.get("id"):
                    agents.add(str(bundle["id"]))
    locations = set(known_locations)

    warnings: list[str] = []

    def _check(kind: str, value: Any, pool: set[str], label: str) -> None:
        name = str(value or "")
        if not pool or not name or name in pool:
            return
        warnings.append(f"{label}: {kind} {name!r} 不在这个世界里(已知:{sorted(pool)})")

    for index, beat in enumerate(beats):
        if not isinstance(beat, dict):
            continue
        label = f"beats[{index}] ({beat.get('id', '?')})"
        for pred in (beat.get("trigger") or {}).get("when") or []:
            if not isinstance(pred, dict):
                continue
            for key in ("as", "target"):
                _check("角色", pred.get(key), agents, f"{label}.trigger")
            for who in pred.get("agents") or []:
                _check("角色", who, agents, f"{label}.trigger")
        for j, op in enumerate(beat.get("payload") or []):
            if not isinstance(op, dict):
                continue
            op_label = f"{label}.payload[{j}] {op.get('op', '?')}"
            if op.get("op") != "agent_join":
                for key in _OP_AGENT_FIELDS:
                    _check("角色", op.get(key), agents, op_label)
            _check("地点", op.get("location"), locations, op_label)
    return warnings


def _validate_after_graph(beats: list[Any], ids: set[str]) -> list[str]:
    """`after` refs must point at existing beats and must not form a cycle."""
    errors: list[str] = []
    after_of: dict[str, str] = {}
    for beat in beats:
        if not isinstance(beat, dict):
            continue
        beat_id, trigger = beat.get("id"), beat.get("trigger")
        after = trigger.get("after") if isinstance(trigger, dict) else None
        if not isinstance(beat_id, str) or not isinstance(after, str) or not after:
            continue
        if after not in ids:
            errors.append(f"beat {beat_id!r}: trigger.after references unknown beat {after!r}")
        elif after == beat_id:
            errors.append(f"beat {beat_id!r}: trigger.after forms a cycle (references itself)")
        else:
            after_of[beat_id] = after
    for start in after_of:
        seen = {start}
        node = after_of.get(start)
        while node is not None:
            if node in seen:
                errors.append(f"beat {start!r}: trigger.after forms a cycle")
                break
            seen.add(node)
            node = after_of.get(node)
    return errors


def _warn_unpaired_leaves(beats: list[dict[str, Any]]) -> None:
    """An `agent_leave` with no `agent_return` anywhere in the script is
    usually an authoring slip — but permanent departure is legitimate drama,
    so this warns and never blocks (agent-leave-return D5)."""
    left: set[str] = set()
    returned: set[str] = set()
    for beat in beats:
        for op in beat.get("payload", []):
            if op.get("op") == "agent_leave":
                left.add(op.get("agent_id"))
            elif op.get("op") == "agent_return":
                returned.add(op.get("agent_id"))
    for agent_id in sorted(left - returned):
        logger.warning("beat script: agent_leave for %r has no agent_return — permanent departure", agent_id)


# ── trigger evaluation (runtime, degrade-never-raise) ────────────────────────


def trigger_ready(
    beat: dict[str, Any],
    now: WorldTime,
    fired: set[str],
    projection: Projection,
    agent_locs: dict[str, str],
    reader: Any | None = None,
) -> bool:
    """All trigger conditions ANDed. `at` means "no earlier than" — the tick
    granularity (5 world minutes by default) would miss an equality match.
    A predicate that errors reads as not-met: firing wrongly is worse than
    firing late, and the beat retries every tick anyway."""
    trigger = beat.get("trigger") or {}
    at = trigger.get("at")
    if at is not None:
        due = (int(at.get("day", 0)), int(at.get("minute_of_day", 0)))
        if (now.day, now.minute_of_day) < due:
            return False
    after = trigger.get("after")
    if after is not None and after not in fired:
        return False
    for pred in trigger.get("when") or []:
        try:
            if not _eval_predicate(pred, projection, agent_locs, reader):
                return False
        except Exception:  # noqa: BLE001 - a broken predicate must not stop the world
            logger.warning(
                "beat %r: predicate %r failed to evaluate — treated as not met",
                beat.get("id"), pred, exc_info=True,
            )
            return False
    return True


def _compare(value: float, pred: dict[str, Any]) -> bool:
    threshold = float(pred["value"])
    return value >= threshold if pred.get("op") == "gte" else value <= threshold


def _eval_predicate(
    pred: dict[str, Any],
    projection: Projection,
    agent_locs: dict[str, str],
    reader: Any | None = None,
) -> bool:
    """一个谓词的求值。读不到的东西一律读作"未满足" —— 与运行期降级同一条规矩:
    宁可晚触发,不可错触发,而且下个 tick 还会再试。"""
    kind = pred.get("pred")
    if kind == "sentiment":
        rel = projection.relations.get((pred["as"], pred["target"]))
        return _compare(rel.sentiment if rel is not None else 0.0, pred)
    if kind == "co_located":
        # 用活黑板的位置,不用投影。**理由不是"投影不追落地"**(1.1.1 起它追了)——
        # 是投影不知道"在途":transit 是纯内存态,重启即丢。两个正在赶路的人若按
        # 投影算就成了还在起点同处一室,节拍照常触发、还写进记忆。
        # 与 `Scheduler._is_colocated` 同一条规矩:在途即不在场。
        locs = set()
        for agent_id in pred["agents"]:
            loc = agent_locs.get(agent_id)
            if not loc:
                return False
            locs.add(loc)
        return len(locs) == 1
    if kind == "r_type":
        rel = projection.relations.get((pred["as"], pred["target"]))
        return bool(rel is not None and str(pred["contains"]) in (rel.r_type or ""))
    if kind == "money":
        return _compare(float(projection.balances.get(pred["agent"], 0.0)), pred)
    if kind == "has_item":
        held = projection.inventories.get(pred["agent"], {})
        return int(held.get(pred["item"], 0)) >= int(pred.get("min", 1))
    if kind == "need":
        if reader is None:
            return False
        value = reader.need(str(pred["agent"]), str(pred["need"]))
        return False if value is None else _compare(float(value), pred)
    if kind == "memory":
        if reader is None:
            return False
        needle = str(pred["contains"])
        return any(needle in text for text in reader.memories(str(pred["agent"])))
    return False


# ── payload expansion (pure ops only) ────────────────────────────────────────


# 目标之间的分隔符。作者(和替作者写目标的那个模型)拿它们串一行,
# 而 `{goals}` 是逐条渲染的 —— 不拆开的话,一整行会变成"一个目标"。
_GOAL_SPLIT = re.compile(r"[；;、\n]+")


def _split_goal_text(text: str) -> list[str]:
    return [part.strip() for part in _GOAL_SPLIT.split(text) if part.strip()]


def _looks_char_split(items: list[str]) -> bool:
    """这份 goals 是不是一整句话被按**字**拆开的结果。

    判据要窄:元素多(≥4)、而且**每一个都只有一个字**。真目标没有一个字的
    ("学会放手"最短也四个字),更不会一连四个都是。窄到这个程度之后,误判需要
    一个作者真的写下 `["静", "默", "等", "待"]` —— 那本来也是一份坏数据。
    """
    return len(items) >= 4 and all(len(item.strip()) == 1 for item in items)


def coerce_goals(value: Any) -> list[Any]:
    """把作者写的 goals 收成一列**人读得出**的目标。

    两种坏形状,同一个下场:`{goals}` 直接进 planner 的提示词,而它是逐条渲染的。

    1. 一整个字符串(`"摆脱母亲的控制；重新定义自己的人生"`)。对它做 `list(...)`
       会按字符拆开;此前这里只挡住了这一步,把整行当成**一个**目标 —— 不炸,
       但那一条里塞着两个目标,分隔符原样进提示词。现在按分隔符拆开。
    2. **已经被按字拆开的一列**(`["摆","脱","母","亲",…]`)。这一形状是
       `list[str]`,合法得挑不出毛病,于是它一路畅通无阻地进了世界:线上那个
       897e282865f5 九个角色全中,每人背着十几二十个单字目标,而 planner 每天
       照着它排一天的日子。上游(创作台 `concept.py` 的 `_short_lines`)已经
       堵住了产出侧,但**引擎这一头当时一声不吭地收下了** —— 这正是这个仓库
       最怕的那类:照跑、日志干净、作者三个月后才发现。

    修法是把字拼回去再按分隔符拆:`["摆","脱",…,"；","重",…]` → 拼 →
    `"摆脱母亲的控制；重新定义自己的人生"` → 拆 → 两条。拼接是**无损**的,
    所以这不是猜一个答案出来,是把丢掉的那一步倒回去。
    """
    if isinstance(value, str):
        return _split_goal_text(value)
    if isinstance(value, (list, tuple)):
        items = [item for item in value]
        texts = [str(item) for item in items]
        if _looks_char_split(texts):
            repaired = _split_goal_text("".join(texts))
            logger.warning(
                "goals 是一列单字(%d 条)—— 按字拆开的痕迹,已拼回并重新拆成 %d 条:%s",
                len(texts), len(repaired), repaired,
            )
            return repaired
        # 一条里塞着分隔符的照样拆开(模型常把两个目标写进同一个数组元素),
        # 非字符串的元素原样留着 —— 拆它没有意义,而丢它是另一种静默的少装。
        out: list[Any] = []
        for item in items:
            if isinstance(item, str):
                out.extend(_split_goal_text(item))
            else:
                out.append(item)
        return out
    return []


def memory_seed_event(agent_id: str, mem: dict[str, Any], default_kind: str = "beat") -> dict[str, Any]:
    """One `memory_seed` event, payload identical to rich-injection's genesis
    shape (consumed by MemoryStore via Scheduler._apply_memory_trigger)."""
    return {
        "type": "memory_seed",
        "who": agent_id,
        "payload": {
            "agent_id": agent_id,
            "kind": str(mem.get("kind", default_kind)),
            "summary": str(mem.get("summary", "")),
            "importance": float(mem.get("importance", 0.5)),
            "anchor": bool(mem.get("anchor", False)),
        },
    }


def expand_relations(
    agent_id: str, relations: list[dict[str, Any]], known_agents: set[str]
) -> list[dict[str, Any]]:
    """Symmetric relation-seed events for a joining agent, genesis-style
    (_seed_relations): both directions, absolute sentiment. Values are
    coerced HERE, inside the per-op guard — a bad value must fail during
    expansion, never in the unguarded record loop. Payloads carry
    `seed: True` so TriggerEngine knows this is exogenous backfill, not a
    lived relationship swing (no relation_shift memory, no friendship edge)."""
    events: list[dict[str, Any]] = []
    for rel in relations:
        other = rel.get("with")
        if other not in known_agents:
            logger.warning("beat agent_join relation references unknown agent %r — skipping", other)
            continue
        if "sentiment" in rel:
            sentiment = float(rel["sentiment"])
            for as_id, target_id in ((agent_id, other), (other, agent_id)):
                events.append({
                    "type": "state_change",
                    "who": as_id,
                    "payload": {"kind": "sentiment", "as": as_id, "target": target_id,
                                "sentiment": sentiment, "seed": True},
                })
        if "r_type" in rel or "r_type_back" in rel:
            fwd = str(rel.get("r_type", "acquaintance"))
            back = str(rel.get("r_type_back", "acquaintance"))
            for as_id, target_id, f, b in ((agent_id, other, fwd, back), (other, agent_id, back, fwd)):
                events.append({
                    "type": "state_change",
                    "who": as_id,
                    "payload": {"kind": "r_type", "as": as_id, "target": target_id,
                                "r_type": f, "r_type_back": b, "seed": True},
                })
    return events


def expand_event_op(
    op: dict[str, Any], *, agent_locs: dict[str, str], known_agents: set[str]
) -> list[dict[str, Any]]:
    """Expand one EVENT_OPS op into raw event dicts (Scheduler records them).

    Every event type here already exists: `memory_seed` (rich-injection),
    `sentiment_delta` (llm-relationship-judge), `r_type`/`persona_update`
    (genesis injection / M6). An op referencing an unknown agent expands to
    nothing, with a warning — runtime degrade, never raise (D4).
    """
    kind = op.get("op")

    if kind == "memory":
        agent_id = op.get("agent_id")
        if agent_id not in known_agents:
            logger.warning("beat memory op references unknown agent %r — skipping", agent_id)
            return []
        return [memory_seed_event(agent_id, op)]

    if kind == "broadcast_memory":
        location = op.get("location")
        witnesses = [aid for aid, loc in agent_locs.items() if loc == location]
        if not witnesses:
            logger.warning("beat broadcast_memory at %r found no witnesses — nothing injected", location)
            return []
        return [memory_seed_event(aid, op) for aid in witnesses]

    if kind == "pay":
        src, dst = str(op.get("from") or ""), str(op.get("to") or "")
        try:
            amount = float(op.get("amount"))
        except (TypeError, ValueError):
            logger.warning("beat pay op has a non-numeric amount %r — skipping", op.get("amount"))
            return []
        for holder in (src, dst):
            if holder not in known_agents and holder not in (_TOWN_HOLDER, BEAT_WORLD_HOLDER):
                logger.warning("beat pay references unknown holder %r — skipping", holder)
                return []
        if amount <= 0:
            # 投影对 amount<=0 是 no-op(payment 只加不减),所以负数不是"反向转账",
            # 是一条什么也不做的事件。作者以为钱转了,其实没有 —— 明说。
            logger.warning("beat pay amount must be > 0 (got %r) — skipping;反向转账请把 from/to 调过来", amount)
            return []
        return [{
            "type": "payment", "who": dst,
            "payload": {"from": src, "to": dst, "amount": amount,
                        "reason": str(op.get("reason") or "beat")},
        }]

    if kind == "grant_item":
        agent_id = str(op.get("agent_id") or "")
        if agent_id not in known_agents:
            logger.warning("beat grant_item references unknown agent %r — skipping", agent_id)
            return []
        try:
            qty = int(op.get("qty", 1))
        except (TypeError, ValueError):
            qty = 0
        if qty == 0:
            logger.warning("beat grant_item qty must be non-zero — skipping")
            return []
        source = str(op.get("from") or BEAT_WORLD_HOLDER)
        # qty 为负 = 拿走。投影只认正数,所以调换两端而不是发一条 no-op。
        holder_from, holder_to = (source, agent_id) if qty > 0 else (agent_id, source)
        return [{
            "type": "item_transfer", "who": agent_id,
            "payload": {"from": holder_from, "to": holder_to,
                        "item_id": str(op.get("item_id")), "qty": abs(qty)},
        }]

    if kind == "sentiment_delta":
        as_id, target = op.get("as"), op.get("target")
        for aid in (as_id, target):
            if aid not in known_agents:
                logger.warning("beat sentiment_delta references unknown agent %r — skipping", aid)
                return []
        return [{
            "type": "state_change",
            "who": as_id,
            "payload": {"kind": "sentiment_delta", "as": as_id, "target": target,
                        "delta": float(op.get("delta", 0.0))},
        }]

    if kind == "r_type":
        as_id, target = op.get("as"), op.get("target")
        for aid in (as_id, target):
            if aid not in known_agents:
                logger.warning("beat r_type references unknown agent %r — skipping", aid)
                return []
        payload: dict[str, Any] = {"kind": "r_type", "as": as_id, "target": target}
        if "r_type" in op:
            payload["r_type"] = str(op["r_type"])
        if "r_type_back" in op:
            payload["r_type_back"] = str(op["r_type_back"])
        return [{"type": "state_change", "who": as_id, "payload": payload}]

    if kind == "persona_update":
        agent_id = op.get("agent_id")
        if agent_id not in known_agents:
            logger.warning("beat persona_update references unknown agent %r — skipping", agent_id)
            return []
        spec = dict(op.get("spec") or {})
        if "goals" in spec:
            spec["goals"] = coerce_goals(spec["goals"])
        return [{
            "type": "state_change",
            "who": agent_id,
            "payload": {"kind": "persona_update", "spec": spec},
        }]

    logger.warning("beat op %r is not an event op — skipping", kind)
    return []


# ── the director ─────────────────────────────────────────────────────────────


class BeatDirector:
    """Tracks which beats have fired and answers "what is due now?".

    Owned by the Scheduler and only ever called under its lock. The fired-set
    starts from replayed `beat_fired` events (recovery) — the script file and
    the log are aligned by beat id, so an already-fired id never re-fires and
    a newly added id participates normally (D1).
    """

    def __init__(self, script: BeatScript, fired: set[str] | None = None):
        self.script = script
        self.fired: set[str] = set(fired or ())
        # Membership, not arithmetic: the replayed fired-set may contain ids
        # from an earlier script version that this script no longer has —
        # counting them made has_pending() short-circuit a script whose own
        # beats had never fired (caught by agent-leave-return's restart test).
        self._pending: set[str] = {b["id"] for b in script.beats} - self.fired

    def due_beats(
        self, now: WorldTime, projection: Projection, agent_locs: dict[str, str],
        reader: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Beats whose trigger is satisfied this tick. Computed against the
        fired-set as of NOW: a beat whose `after` prerequisite fires this same
        tick becomes due on the next one."""
        return [
            beat
            for beat in self.script.beats
            if beat["id"] not in self.fired
            and trigger_ready(beat, now, self.fired, projection, agent_locs, reader)
        ]

    def mark_fired(self, beat_id: str) -> None:
        self.fired.add(beat_id)
        self._pending.discard(beat_id)

    def has_pending(self) -> bool:
        """False once every beat OF THIS SCRIPT has fired — the scheduler's
        short-circuit so an exhausted script costs nothing per tick."""
        return bool(self._pending)
