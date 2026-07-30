"""世界的规律,作为数据(world-rules)。

这个引擎一直在做同一件事:把硬编码的东西变成数据 —— 提示词进了 `prompt_templates`,
行为树进了 `bt_nodes`,剧情进了 `beats.json`。**规律**是这条线上最后一段:今天
needs 的衰减曲线、economy 的价格漂移都是写死在 Python 里的,而"树怎么长""矿怎么
枯""修炼一小时涨多少功力"这类东西**因世界而异**,不该由引擎替所有世界决定。

一条规律长这样:

```jsonc
{ "id": "tree_growth",
  "every": {"ticks": 12},                 // 多久算一次(节流,不是步长)
  "for_each": {"kind": "tree"},           // 谁参与
  "when": ["world_season != 3"],          // 可选:条件
  "set": {"size": "min(size + growth_rate * dt, max_size)"},
  "emit": [{"when": "size >= max_size", "type": "tree_matured"}] }
```

设计上定死的五条,每条都有代价换来的理由:

1. **量 = (owner, key, value)**,owner 前缀即种类(`tree:oak_01` / `agent:夏` /
   `world`)。不发明新的实体系统 —— 和账本的 holder 可以是角色/`player:x`/`__town__`
   完全同构。
2. **`dt` 是真实流逝的 tick,`every` 只是节流。** 所以一万棵树不必每 tick 算一万次:
   `dt` 保证**没有累积漂移**(`needs.settle()` 就是这个形状)。
   实测(2026-07-30):一万棵树跑一个世界日(288 tick / 24 次求值)1.4 秒,约
   4.8ms/tick。但这个数**是改出来的不是天生的** —— 第一版逐个 owner 查快照、逐个
   owner 提交,2000 棵树就到 72ms/tick(快进一年要两小时)。所以往这一层加东西时
   留意:**按类批量查 + 整轮一次 commit** 是这条承诺的全部依据(见
   `stocks.snapshot_kind` / `write_round`;`test_world_rules.py` 有回归测试盯着)。
   准确说是"不漂",不是"任何时刻都一样":节流意味着任一瞬间可能有最多一个 `every`
   的变化**还没结算**。读到的值因此可能滞后 —— 滞后会在下一次求值时一次补回来,
   但要精确到 tick 的量就别把 `every` 写大。
3. **同一轮里读到的都是这一轮开始前的值**(双缓冲)。规律 A 写的量被规律 B 读时,
   顺序就会变成隐藏的语义 —— 那是所有规则引擎都会撞的墙。代价是连锁反应要等下一轮,
   换来的是可预测、与顺序无关、能并行。
4. **连续变化不发事件。** 一万棵树每天 24 万次数值变化,逐条发事件会把日志淹掉
   (needs 有过血泪教训:一次抖动让事件量 19.7×、叙事 32×)。只有 `emit` 声明的
   **门槛**跨过去时才发一条,而且是**边沿触发** —— 算之前不满足、算之后满足。
5. **加载时严格校验。** 表达式写错、引用了没声明的量、`every` 写反 —— 全部在世界
   启动前当场报错。和节拍脚本同一条硬要求:坏脚本不许流到运行期。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from anima_world.expressions import Expression, ExpressionError, compile_expression

logger = logging.getLogger(__name__)

# 表达式里恒有的两个名字(不需要声明就能读)。
BUILTIN_NAMES = ("dt", "now")
# `world` 这个 owner 的量,在任何表达式里都能以 `world_<key>` 读到。
WORLD_PREFIX = "world_"

_SELECTOR_KINDS = ("kind", "owner", "action")


class RuleError(ValueError):
    """规律脚本有毛病 —— 逐条列出,一次性报完。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid world rules:\n" + "\n".join(f"- {e}" for e in errors))


@dataclass(frozen=True)
class Emit:
    """门槛事件:算之前不满足、算之后满足 —— 才发。"""

    when: Expression
    type: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Rule:
    id: str
    interval_ticks: int
    selector_kind: str            # "kind" | "owner" | "action"
    selector_value: str
    outputs: dict[str, Expression]
    conditions: tuple[Expression, ...] = ()
    emits: tuple[Emit, ...] = ()

    def reads(self) -> frozenset[str]:
        names: set[str] = set()
        for expression in (*self.outputs.values(), *self.conditions):
            names |= expression.names
        for emit in self.emits:
            names |= emit.when.names
        return frozenset(names)


def parse_rules(entries: Any) -> list[Rule]:
    """把 JSON 里的规律列表编译成 `Rule`。**任何一条坏了就整体拒绝。**

    不逐条丢弃(那是种子里可选字段的宽容原则):规律是世界的物理法则,少一条不是
    "少一点内容",是这个世界从此算错。宁可开不了机。
    """
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise RuleError([f"rules 必须是一个列表,收到 {type(entries).__name__}"])

    errors: list[str] = []
    rules: list[Rule] = []
    seen: set[str] = set()

    for index, entry in enumerate(entries):
        label = f"rules[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{label} 必须是对象,收到 {type(entry).__name__}")
            continue
        rule_id = str(entry.get("id") or "").strip()
        if not rule_id:
            errors.append(f"{label} 少了 id")
            continue
        label = f"rules[{index}] ({rule_id})"
        if rule_id in seen:
            errors.append(f"{label}:id 重复了")
            continue
        seen.add(rule_id)

        try:
            rules.append(_parse_one(rule_id, label, entry))
        except RuleError as exc:
            errors.extend(exc.errors)

    if errors:
        raise RuleError(errors)
    return rules


def _parse_one(rule_id: str, label: str, entry: dict[str, Any]) -> Rule:
    errors: list[str] = []

    interval = _parse_interval(label, entry.get("every"), errors)
    selector = _parse_selector(label, entry.get("for_each"), errors)

    raw_outputs = entry.get("set")
    outputs: dict[str, Expression] = {}
    if not isinstance(raw_outputs, dict) or not raw_outputs:
        errors.append(f"{label}:set 必须是「量名 → 表达式」的对象,而且不能为空")
    else:
        for key, source in raw_outputs.items():
            try:
                outputs[str(key)] = compile_expression(source)
            except ExpressionError as exc:
                errors.append(f"{label}.set.{key}:{exc}")

    conditions: list[Expression] = []
    raw_when = entry.get("when") or []
    if not isinstance(raw_when, list):
        errors.append(f"{label}:when 必须是表达式列表")
    else:
        for position, source in enumerate(raw_when):
            try:
                conditions.append(compile_expression(source))
            except ExpressionError as exc:
                errors.append(f"{label}.when[{position}]:{exc}")

    emits: list[Emit] = []
    raw_emit = entry.get("emit") or []
    if not isinstance(raw_emit, list):
        errors.append(f"{label}:emit 必须是列表")
    else:
        for position, spec in enumerate(raw_emit):
            emit_label = f"{label}.emit[{position}]"
            if not isinstance(spec, dict):
                errors.append(f"{emit_label} 必须是对象")
                continue
            event_type = str(spec.get("type") or "").strip()
            if not event_type:
                errors.append(f"{emit_label} 少了 type")
            payload = spec.get("payload") or {}
            if not isinstance(payload, dict):
                errors.append(f"{emit_label}:payload 必须是对象")
                payload = {}
            try:
                emits.append(Emit(when=compile_expression(spec.get("when")),
                                  type=event_type, payload=dict(payload)))
            except ExpressionError as exc:
                errors.append(f"{emit_label}.when:{exc}")

    if errors:
        raise RuleError(errors)

    return Rule(
        id=rule_id,
        interval_ticks=interval,
        selector_kind=selector[0],
        selector_value=selector[1],
        outputs=outputs,
        conditions=tuple(conditions),
        emits=tuple(emits),
    )


def _parse_interval(label: str, raw: Any, errors: list[str]) -> int:
    """`every` 是**节流**,不是步长 —— 真实流逝由 `dt` 带。缺省每 tick 都算。"""
    if raw is None:
        return 1
    if not isinstance(raw, dict) or not raw:
        errors.append(f"{label}:every 必须是 {{\"ticks\": N}} 或 {{\"days\": N}}")
        return 1
    unknown = set(raw) - {"ticks", "days"}
    if unknown:
        errors.append(f"{label}:every 只认 ticks / days,不认 {sorted(unknown)}")
        return 1
    ticks = 0
    for unit, per_unit in (("ticks", 1), ("days", 288)):
        if unit not in raw:
            continue
        try:
            value = int(raw[unit])
        except (TypeError, ValueError):
            errors.append(f"{label}:every.{unit} 不是整数")
            return 1
        if value <= 0:
            errors.append(f"{label}:every.{unit} 必须为正")
            return 1
        ticks += value * per_unit
    return max(1, ticks)


def _parse_selector(label: str, raw: Any, errors: list[str]) -> tuple[str, str]:
    if not isinstance(raw, dict) or len(raw) != 1:
        errors.append(
            f"{label}:for_each 必须正好一个键 —— "
            f"{{\"kind\": …}} / {{\"owner\": …}} / {{\"action\": …}}"
        )
        return ("owner", "world")
    key, value = next(iter(raw.items()))
    if key not in _SELECTOR_KINDS:
        errors.append(f"{label}:for_each 不认 {key!r},只认 {list(_SELECTOR_KINDS)}")
        return ("owner", "world")
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label}:for_each.{key} 必须是非空字符串")
        return ("owner", "world")
    return (key, value.strip())


def rule_errors(entries: Any) -> list[str]:
    """只解释、不抛 —— 给 `validate` 这类命令用。"""
    try:
        parse_rules(entries)
    except RuleError as exc:
        return list(exc.errors)
    return []


def missing_names(rule: Rule, known: Iterable[str]) -> list[str]:
    """这条规律读了、但世界里并不存在的量。

    只能是**建议**不能是拒绝:量可以在世界跑起来之后才被创建(种一棵树),
    而且 `world_*` 全局也可能是后来才写的。加载时把它当错误会让一个设计正确的
    世界开不了机 —— 与其如此,不如在这里报出来让人看见。
    """
    available = set(known) | set(BUILTIN_NAMES)
    return sorted(name for name in rule.reads() if name not in available)
