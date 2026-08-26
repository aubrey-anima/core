"""插件:作者往世界里加机制,而**内核不认识任何具体系统**。

设计稿 `docs/设计-插件系统.md`;这里是**第 1 期**的落地 —— `plugin` 记录、命名空间、
`number`/`state` 两种事实、规律、触发器、装/升/卸。边、判定、聚合归后面几期。

## 这一层为什么存在

引擎里的「需求」「经济」「关系」都是写死的 Python:`needs.py` 里三个量、一张衰减表、
一张恢复表。**做不完的正是这种机制** —— 一个修真世界要灵力,一个江湖世界要声望,
而每加一个就得改一次引擎。插件把这件事翻过来:**机制是数据**,和规律、本体、节拍
住在同一份 `.cyberworld` 里,由同一条装载路进世界。

## 三条边界(它们是这一层的全部内容)

- **命名空间即插件 id。** 事实的存储键是 `<id>.<key>`,住在**今天的** `stock:{owner}`
  hash 里 —— 一个字节的新存储都不造。两个插件各声明一个「灵力」因此不会撞车,
  而撞车的样子本来是安静的。
- **只写自己的命名空间。** 规律的 `set`、触发器的 `set` 都过 `rules.bad_output_name`
  的 `namespace=` 那道门,越界**开不了机**。
- **读别人的要声明。** `reads: ["reputation.score"]`;没声明就读 = 开不了机并点名。
  依赖图定装载顺序,缺依赖当场报。

## 这一版**不认**什么(而"不认"是明说的,不是没想到)

`fact_shapes` 只有 **`number`** 和 **`state`**。`timer` 与 `text` 这一版**开不了机**
并点名 —— 理由各不相同,写在这儿免得下一个人以为是漏了:

- `text` 要一个**存字符串**的地方,而量表那个 hash 存的是 `[float, tick]`。
  给它造一份新存储是这一层明说不做的事(见上面第一条),所以它等的是有地方住。
- `timer` 的用法是 `p.k.age` / `p.k.active` —— **两层点号**,而表达式那一层
  只收一层(理由在 `expressions._validate` 那段注释里,它是安全边界)。
  给 timer 一套语法要先想清楚两层点号的语义,不该顺手塞进这一期。

**消费方按 `contract --json` 的 `plugins.fact_shapes` 探测,别照设计稿列表写** ——
设计稿说的是这套架构装得下什么,契约说的是**这一版引擎收不收**。

## `state` 在表达式里是**序号**,不是那个词

一个 `state` 事实存的是它在 `values` 里的**下标**(从 0 起,按声明顺序),
所以 `sect.rank >= 2` 就是「到了第三档」——`values` 的顺序**是承重的**。
写成 `sect.rank == "内门"` 会**开不了机**并告诉作者该写几:一个 float 和一个 str
比大小在 Python 里是 TypeError,而那会变成运行期跳过一条规律 —— 安静地不发生。
她**读到**的仍然是那个词(`values[i].name`)与那句描述,那是提示词那一层的事。
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

from anima_world.expressions import Expression, ExpressionError, compile_expression
from anima_world.perception import VISIBILITIES, band_errors, parse_bands
from anima_world.rules import Rule, RuleError, parse_rules

logger = logging.getLogger(__name__)

#: 插件 id 的形状。**禁 `.`**(它是命名空间的分隔符)、**禁 `-`**(表达式里
#: `a-b` 是减法),所以只剩小写字母、数字、下划线。
PLUGIN_ID_PATTERN = r"^[a-z][a-z0-9_]{1,31}$"
_PLUGIN_ID = re.compile(PLUGIN_ID_PATTERN)

#: 事实名的形状:非空、不带 `.` / `:`、不以下划线开头(下划线那条和
#: `expressions` 里那道安全边界是同一条)。中文当然可以 —— 这个引擎中文优先。
_BAD_FACT_MARKS = (".", ":", " ")

#: **内核保留字。** 拿它们当插件 id,表达式里就分不出"这是插件的事实"还是
#: "这是内核的名字" —— 而分不出的样子是安静的:`now.x` 读不到东西,作者看不出为什么。
RESERVED_IDS = frozenset({
    "world", "agent", "location", "event", "now", "calendar", "me", "self",
    "from", "to", "edge", "edges", "dt", "day", "hour", "minute",
    "minute_of_day", "rand", "plugin", "plugins",
})

#: 这一版**收**的事实形状。⚠️ 消费方读 `contract --json` 的 `plugins.fact_shapes`,
#: 别照设计稿那张表写 —— 那张表说的是架构装得下什么。
FACT_SHAPES = ("number", "state")

#: 声明得了、但这一版**开不了机**的那两种,连同"为什么"。写成数据是为了让报错里
#: 有理由:一句光秃秃的"不支持 timer"会让作者以为自己写错了字。
DEFERRED_SHAPES: dict[str, str] = {
    "timer": "它的用法是 `p.k.age` / `p.k.active` —— 两层点号,而表达式那一层"
             "只收一层(那是安全边界)。给它一套语法要先想清楚两层的语义",
    "text": "它要一个存字符串的地方,而量表那个 hash 存的是 [float, tick]。"
            "插件系统明说不造新存储,所以它等的是有地方住",
}

#: 事实挂在谁身上。`entity:<kind>` 是一族(kind 由作者的 `kinds` 声明)。
BEARER_FORMS = ("agent", "world", "location", "entity:<kind>")

#: 触发器与规律的效果原语。这一版两个;`spawn`/`destroy`/`link`/`unlink`/`transfer`
#: 归第 2 期(它们要边和插件声明的 kind)。
EFFECTS = ("set", "emit")

#: `text` 形状将来的默认上限。这一版用不上,写在这儿是因为它已经进了契约。
DEFAULT_TEXT_MAX_CHARS = 400


class PluginError(ValueError):
    """插件声明有毛病 —— **一次列全**,和本体、规律、节拍逐字同一条纪律。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid plugins:\n" + "\n".join(f"- {e}" for e in errors))


@dataclass(frozen=True)
class Fact:
    """一个插件事实的声明。**存储键是 `plugin.key`**,和表达式里写的逐字相同。"""

    plugin: str
    key: str
    bearer: str
    shape: str = "number"
    default: float = 0.0
    visibility: str = "hidden"
    label: str = ""
    unit: str = ""
    bands: tuple[tuple[float, str], ...] = ()
    #: `bands` 的**第三项**:这一档是什么感觉。进提示词,紧跟档词。
    band_notes: tuple[str, ...] = ()
    #: `state` 的取值(名字, 描述),顺序**承重** —— 表达式里的序号就是这个下标。
    values: tuple[tuple[str, str], ...] = ()
    low: float | None = None
    high: float | None = None

    @property
    def qualified(self) -> str:
        return f"{self.plugin}.{self.key}"

    @property
    def owner_kind(self) -> str:
        """它落在可见性表的哪一行上 —— `entity:tree` → `tree`。"""
        return self.bearer.split(":", 1)[1] if self.bearer.startswith("entity:") else self.bearer

    def clamp(self, value: float) -> float:
        """写入时夹一道。`state` 永远夹在 `[0, len(values)-1]` 上。"""
        low, high = self.low, self.high
        if self.shape == "state":
            low, high = 0.0, float(max(0, len(self.values) - 1))
        if low is not None:
            value = max(low, value)
        if high is not None:
            value = min(high, value)
        return value

    def word(self, value: float) -> str:
        """她读到的那个词(`state` 是值名,`number` 是档词);没有就是空串。"""
        if self.shape == "state":
            index = int(round(self.clamp(float(value))))
            return self.values[index][0] if 0 <= index < len(self.values) else ""
        from anima_world.perception import band_word

        return band_word(self.bands, float(value)) or ""

    def note(self, value: float) -> str:
        """她读到的那句**描述**(档的第三项 / 值的 description);没有就是空串。"""
        if self.shape == "state":
            index = int(round(self.clamp(float(value))))
            return self.values[index][1] if 0 <= index < len(self.values) else ""
        if not self.bands:
            return ""
        chosen = 0
        for position, (threshold, _word) in enumerate(self.bands):
            if float(value) >= threshold:
                chosen = position
        return self.band_notes[chosen] if chosen < len(self.band_notes) else ""


@dataclass(frozen=True)
class Trigger:
    """因一件事而变。**订的是事件,不是量** —— 量那一半是规律。"""

    plugin: str
    id: str
    event: str
    bearer: str
    conditions: tuple[Expression, ...] = ()
    sets: tuple[tuple[str, Expression], ...] = ()
    emits: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Plugin:
    id: str
    version: str
    engine_min: str = ""
    label: str = ""
    reads: frozenset[str] = frozenset()
    facts: dict[str, Fact] = field(default_factory=dict)
    rules: tuple[Rule, ...] = ()
    triggers: tuple[Trigger, ...] = ()

    def bearers(self) -> frozenset[str]:
        return frozenset(fact.bearer for fact in self.facts.values())


# ── 解析 ────────────────────────────────────────────────────────────────────


def parse_plugins(
    entries: Any, *, ticks_per_day: int = 288,
    subscribable: Iterable[str] | None = None,
) -> list[Plugin]:
    """把 `plugin` 记录编译成 `Plugin`。**任何一条坏了就整体拒绝,一次列全。**

    和 `parse_kinds` / `parse_rules` / `BeatScript.from_data` 逐字同一条:一份坏声明
    留在库里,下场不是"少一点内容",是这个世界从此有一部分机制静默地不存在。

    `subscribable` 是第 0 期那张白名单(`events.SUBSCRIBABLE_EVENTS`)。触发器订的
    事件必须在它上面,**或者**是任何插件的 `<id>.<type>` —— 后者由调用方在装载时
    连着依赖一起查(这里只认白名单与"带点号的插件事件"这个形状)。
    ⚠️ **缺省是引擎那张真表,不是空集。** 空集当缺省的下场很具体:一个忘了传它的
    调用方会把**每一个**合法的触发器都判成"订不到",而报错里那句"可订的是 []"
    看上去像是这一版引擎一个事件都不许订 —— 一句会把人指向错误方向的报错,
    比不报错还贵。要真的验空集就显式传 `subscribable=()`。
    """
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise PluginError([f"plugins 必须是一个列表,收到 {type(entries).__name__}"])

    errors: list[str] = []
    plugins: list[Plugin] = []
    seen: dict[str, str] = {}
    if subscribable is None:
        from anima_world.events import SUBSCRIBABLE_EVENTS

        subscribable = SUBSCRIBABLE_EVENTS
    known = set(subscribable)

    for index, entry in enumerate(entries):
        label = f"plugins[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{label} 必须是对象,收到 {type(entry).__name__}")
            continue
        plugin_id = str(entry.get("id") or "").strip()
        if not plugin_id:
            errors.append(f"{label} 少了 id")
            continue
        label = f"plugins[{index}] ({plugin_id})"
        problem = bad_plugin_id(plugin_id)
        if problem:
            errors.append(f"{label}:{problem}")
            continue
        if plugin_id in seen:
            errors.append(
                f"{label}:id 重复了 —— 同一份文件里两个 `{plugin_id}`。"
                "升级是**换一份文件**里写更高的 version,不是在同一份里写两遍"
            )
            continue
        seen[plugin_id] = label
        try:
            plugins.append(_parse_one(plugin_id, label, entry, ticks_per_day, known))
        except PluginError as exc:
            errors.extend(exc.errors)

    if errors:
        raise PluginError(errors)
    return plugins


def bad_plugin_id(plugin_id: str) -> str | None:
    """这个 id 当不当得了命名空间。当得了返回 `None`。"""
    if plugin_id in RESERVED_IDS:
        return (
            f"`{plugin_id}` 是内核保留字,当不了插件 id —— 表达式里就分不出"
            f"「这是插件的事实」还是「这是内核的名字」了。保留的是:"
            f"{sorted(RESERVED_IDS)}"
        )
    if not _PLUGIN_ID.match(plugin_id):
        return (
            f"id 要合 `{PLUGIN_ID_PATTERN}`:小写字母开头,只许小写字母/数字/下划线,"
            "2~32 个字符。**不许有 `.`**(它是命名空间的分隔符),"
            "**也不许有 `-`**(表达式里 `a-b` 是减法)"
        )
    return None


def _parse_one(
    plugin_id: str, label: str, entry: dict[str, Any], ticks_per_day: int,
    subscribable: set[str],
) -> Plugin:
    errors: list[str] = []
    version = str(entry.get("version") or "").strip()
    if not version:
        errors.append(
            f"{label} 少了 version —— 升级与拒绝降级全靠它,没有它这个插件"
            "装第二遍时引擎分不出「同一份」和「新一份」"
        )

    reads: set[str] = set()
    raw_reads = entry.get("reads") or []
    if not isinstance(raw_reads, list):
        errors.append(f"{label}:reads 必须是列表")
    else:
        for position, item in enumerate(raw_reads):
            name = str(item or "").strip()
            if "." not in name:
                errors.append(
                    f"{label}.reads[{position}]:要写成 `<别的插件id>.<事实名>`,"
                    f"收到 {name!r}"
                )
                continue
            if name.split(".", 1)[0] == plugin_id:
                errors.append(
                    f"{label}.reads[{position}]:`{name}` 是自己的事实,"
                    "不用声明(声明它只会让读的人以为这是个外部依赖)"
                )
                continue
            reads.add(name)

    facts: dict[str, Fact] = {}
    raw_facts = entry.get("facts") or {}
    if not isinstance(raw_facts, dict):
        errors.append(f"{label}:facts 必须是「事实名 → 声明」的对象")
    else:
        for key, spec in raw_facts.items():
            try:
                fact = _parse_fact(plugin_id, f"{label}.facts.{key}", str(key), spec)
            except PluginError as exc:
                errors.extend(exc.errors)
                continue
            facts[fact.key] = fact

    # 自己的事实 + 声明过的依赖 = 这个插件的表达式允许读到的命名空间名字。
    allowed = {f"{plugin_id}.{key}" for key in facts} | reads
    allowed |= {f"me_{name}" for name in allowed}

    rules: tuple[Rule, ...] = ()
    raw_rules = entry.get("rules")
    if raw_rules is not None:
        try:
            # **规律 id 也进命名空间。** 它是节流水位的键(`_rule_last_run`)与骰子的
            # 第二个坐标 —— 和世界自己一条同名的规律撞上,两条会共用一个水位,
            # 于是其中一条永远少跑,而没有一处会报错。
            rules = tuple(
                replace(rule, id=f"{plugin_id}.{rule.id}")
                for rule in parse_rules(raw_rules, ticks_per_day=ticks_per_day,
                                        namespace=plugin_id)
            )
        except RuleError as exc:
            errors.extend(f"{label}.{e}" for e in exc.errors)
    for rule in rules:
        errors.extend(_undeclared_reads(f"{label}.rules ({rule.id})", rule.reads(),
                                        plugin_id, allowed))

    triggers: list[Trigger] = []
    raw_triggers = entry.get("triggers") or []
    if not isinstance(raw_triggers, list):
        errors.append(f"{label}:triggers 必须是列表")
    else:
        trigger_ids: set[str] = set()
        for position, spec in enumerate(raw_triggers):
            try:
                trigger = _parse_trigger(
                    plugin_id, f"{label}.triggers[{position}]", spec, facts,
                    allowed, subscribable,
                )
            except PluginError as exc:
                errors.extend(exc.errors)
                continue
            if trigger.id in trigger_ids:
                errors.append(f"{label}.triggers[{position}]:id `{trigger.id}` 重复了")
                continue
            trigger_ids.add(trigger.id)
            triggers.append(trigger)

    # `state` 事实和字符串比大小 —— **加载期拦下来**。放行的话它是运行期的
    # `TypeError`,而运行期错误的样子是"这条规律安静地跳过了"。
    for rule in rules:
        errors.extend(_state_compared_to_string(
            f"{label}.rules ({rule.id})", rule, plugin_id, facts))

    if errors:
        raise PluginError(errors)

    return Plugin(
        id=plugin_id, version=version,
        engine_min=str(entry.get("engine_min") or "").strip(),
        label=str(entry.get("label") or "").strip(),
        reads=frozenset(reads), facts=facts, rules=rules, triggers=tuple(triggers),
    )


def _parse_fact(plugin_id: str, label: str, key: str, spec: Any) -> Fact:
    errors: list[str] = []
    if not key.strip():
        raise PluginError([f"{label}:事实名不能为空"])
    if key.startswith("_"):
        errors.append(f"{label}:事实名不许以下划线开头(表达式那一层的安全边界)")
    for mark in _BAD_FACT_MARKS:
        if mark in key:
            errors.append(f"{label}:事实名里不能有 {mark!r}")
    if not isinstance(spec, dict):
        raise PluginError([f"{label}:声明必须是对象,收到 {type(spec).__name__}"])

    shape = str(spec.get("shape") or "number").strip()
    if shape in DEFERRED_SHAPES:
        errors.append(
            f"{label}:`{shape}` 这一版还不收 —— {DEFERRED_SHAPES[shape]}。"
            f"这一版收的是 {list(FACT_SHAPES)}"
            "(按 `contract --json` 的 `plugins.fact_shapes` 探测,别照设计稿写)"
        )
    elif shape not in FACT_SHAPES:
        errors.append(f"{label}:不认识的 shape `{shape}`;收的是 {list(FACT_SHAPES)}")

    bearer = str(spec.get("bearer") or "").strip()
    if not bearer:
        errors.append(f"{label} 少了 bearer —— 这个事实挂在谁身上?{list(BEARER_FORMS)}")
    elif bearer not in ("agent", "world", "location") and not bearer.startswith("entity:"):
        errors.append(f"{label}:不认识的 bearer `{bearer}`;形状是 {list(BEARER_FORMS)}")
    elif bearer.startswith("entity:") and not bearer[len("entity:"):].strip():
        errors.append(f"{label}:`entity:` 后面要写种类 id")

    visibility = str(spec.get("visibility") or "hidden").strip()
    if visibility not in VISIBILITIES:
        errors.append(f"{label}:不认识的可见档 `{visibility}`;只有 {sorted(VISIBILITIES)}")

    values: list[tuple[str, str]] = []
    bands: tuple[tuple[float, str], ...] = ()
    notes: list[str] = []
    low = high = None
    default = 0.0

    if shape == "state":
        raw_values = spec.get("values")
        if not isinstance(raw_values, list) or not raw_values:
            errors.append(
                f"{label}:`state` 要一份非空的 `values` —— **顺序承重**,"
                "表达式里读到的序号就是这个下标"
            )
        else:
            for position, item in enumerate(raw_values):
                if isinstance(item, str):
                    values.append((item, ""))
                elif isinstance(item, dict) and str(item.get("name") or "").strip():
                    values.append((str(item["name"]).strip(),
                                   str(item.get("description") or "").strip()))
                else:
                    errors.append(
                        f"{label}.values[{position}]:要么是一个名字,"
                        "要么是 `{\"name\": …, \"description\": …}`"
                    )
        raw_default = spec.get("default")
        if raw_default is None:
            default = 0.0
        elif isinstance(raw_default, str):
            names = [name for name, _ in values]
            if raw_default not in names:
                errors.append(
                    f"{label}:default `{raw_default}` 不在 values 里 —— "
                    f"声明过的是 {names}"
                )
            else:
                default = float(names.index(raw_default))
        else:
            default = _as_float(label, "default", raw_default, errors, 0.0)
    else:
        default = _as_float(label, "default", spec.get("default", 0.0), errors, 0.0)
        raw_range = spec.get("range")
        if raw_range is not None:
            if (not isinstance(raw_range, (list, tuple)) or len(raw_range) != 2):
                errors.append(f"{label}.range 要写成 `[下限, 上限]`")
            else:
                low = _as_float(label, "range[0]", raw_range[0], errors, 0.0)
                high = _as_float(label, "range[1]", raw_range[1], errors, 0.0)
                if low is not None and high is not None and low > high:
                    errors.append(f"{label}.range:下限 {low} 比上限 {high} 还大")
        raw_bands = spec.get("bands")
        if raw_bands is not None:
            bands, notes, problems = _parse_banded(label, raw_bands)
            errors.extend(problems)

    if errors:
        raise PluginError(errors)

    return Fact(
        plugin=plugin_id, key=key, bearer=bearer, shape=shape, default=float(default),
        visibility=visibility, label=str(spec.get("label") or "").strip(),
        unit=str(spec.get("unit") or "").strip(), bands=bands,
        band_notes=tuple(notes), values=tuple(values), low=low, high=high,
    )


def _parse_banded(
    label: str, raw: Any,
) -> tuple[tuple[tuple[float, str], ...], list[str], list[str]]:
    """插件的 `bands`:**[阈值, 档词] 或 [阈值, 档词, 这一档是什么感觉]**。

    第三项是这一期给作者的那件东西(老板原话:「数字可以加入别名,95 是亲密无间,
    然后加入描述」)。它**只加不改** —— 老的两项写法一个字不变,而 `perception`
    那份 `band_errors` / `parse_bands` 是**已发布的契约**,收的就是两项,所以这里
    先把第三项摘掉再交给它:**判断仍然只有一份**,这一层只多认一列。
    """
    if not isinstance(raw, (list, tuple)):
        return (), [], [f"{label}.bands 必须是一个数组:[[阈值, 词] 或 [阈值, 词, 描述], …]"]
    trimmed: list[Any] = []
    notes: list[str] = []
    problems: list[str] = []
    for index, row in enumerate(raw):
        if isinstance(row, (list, tuple)) and len(row) == 3:
            note = row[2]
            if note is not None and not isinstance(note, str):
                problems.append(
                    f"{label}.bands[{index}][2]:描述要是一句话(字符串),"
                    f"收到 {type(note).__name__}"
                )
                note = ""
            trimmed.append([row[0], row[1]])
            notes.append(str(note or "").strip())
        else:
            trimmed.append(row)
            notes.append("")
    problems.extend(band_errors(trimmed, label=label))
    if problems:
        return (), [], problems
    return parse_bands(trimmed), notes, []


def _as_float(label: str, what: str, raw: Any, errors: list[str], fallback: float) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        errors.append(f"{label}.{what}:{raw!r} 不是一个数")
        return fallback


def _parse_trigger(
    plugin_id: str, label: str, spec: Any, facts: Mapping[str, Fact],
    allowed: set[str], subscribable: set[str],
) -> Trigger:
    errors: list[str] = []
    if not isinstance(spec, dict):
        raise PluginError([f"{label} 必须是对象,收到 {type(spec).__name__}"])
    trigger_id = str(spec.get("id") or "").strip()
    if not trigger_id:
        errors.append(f"{label} 少了 id")
    on = spec.get("on") or {}
    event = str((on or {}).get("event") or "").strip() if isinstance(on, dict) else ""
    if not event:
        errors.append(f"{label} 少了 `on.event`")
    elif event not in subscribable and "." not in event:
        errors.append(
            f"{label}:订不到 `{event}` —— 它不在这一版引擎的可订事件表上,"
            f"也不是某个插件的 `<id>.<type>`。可订的是 {sorted(subscribable)}"
            "(问 `contract --json` 的 `plugins.subscribable_events`,别照文档抄)"
        )

    bearer = str(((spec.get("for_each") or {}) or {}).get("node") or "agent").strip() \
        if isinstance(spec.get("for_each"), dict) else "agent"
    if bearer not in ("agent", "world", "location") and not bearer.startswith("entity:"):
        errors.append(f"{label}.for_each.node:不认识的 `{bearer}`;{list(BEARER_FORMS)}")

    conditions: list[Expression] = []
    raw_when = spec.get("when") or []
    if not isinstance(raw_when, list):
        errors.append(f"{label}:when 必须是表达式列表")
    else:
        for position, source in enumerate(raw_when):
            try:
                conditions.append(compile_expression(source))
            except ExpressionError as exc:
                errors.append(f"{label}.when[{position}]:{exc}")

    sets: list[tuple[str, Expression]] = []
    emits: list[dict[str, Any]] = []
    raw_effects = spec.get("effects") or []
    if not isinstance(raw_effects, list) or not raw_effects:
        errors.append(f"{label}:effects 必须是非空列表 —— 一个什么都不做的触发器"
                      "只会让读的人以为它在做什么")
    else:
        for position, effect in enumerate(raw_effects):
            where = f"{label}.effects[{position}]"
            if not isinstance(effect, dict) or len(effect) != 1:
                errors.append(f"{where}:要写成 `{{\"set\": …}}` 或 `{{\"emit\": …}}`,"
                              f"一条一个原语(这一版有 {list(EFFECTS)})")
                continue
            (kind, body), = effect.items()
            if kind == "set":
                if not isinstance(body, dict) or not body:
                    errors.append(f"{where}.set 必须是「事实名 → 表达式」的对象")
                    continue
                for name, source in body.items():
                    from anima_world.rules import bad_output_name

                    problem = bad_output_name(str(name), namespace=plugin_id)
                    if problem:
                        errors.append(f"{where}.set.{name}:{problem}")
                        continue
                    local = str(name)[len(plugin_id) + 1:]
                    if local not in facts:
                        errors.append(
                            f"{where}.set.{name}:这个插件没声明过 `{local}` 这个事实;"
                            f"声明过的是 {sorted(facts)}"
                        )
                        continue
                    try:
                        sets.append((str(name), compile_expression(source)))
                    except ExpressionError as exc:
                        errors.append(f"{where}.set.{name}:{exc}")
            elif kind == "emit":
                if not isinstance(body, dict):
                    errors.append(f"{where}.emit 必须是对象")
                    continue
                event_type = str(body.get("type") or "").strip()
                if not event_type:
                    errors.append(f"{where}.emit 少了 type")
                    continue
                if not event_type.startswith(f"{plugin_id}."):
                    errors.append(
                        f"{where}.emit.type:插件只发得出自己命名空间的事件 —— "
                        f"要写成 `{plugin_id}.<名字>`,收到 `{event_type}`。"
                        "发别人的名字下去,订它的插件会以为那件事真的发生过"
                    )
                    continue
                payload = body.get("payload") or {}
                if not isinstance(payload, dict):
                    errors.append(f"{where}.emit.payload 必须是对象")
                    payload = {}
                emits.append({"type": event_type, "payload": dict(payload),
                              "text": str(body.get("text") or "")})
            else:
                errors.append(f"{where}:不认识的效果 `{kind}`;这一版有 {list(EFFECTS)}")

    # 触发器读得到:自己的事实 + 声明过的依赖 + `event.<数字格>` + 内核日历。
    reads: set[str] = set()
    for expression in conditions:
        reads |= expression.names
    for _name, expression in sets:
        reads |= expression.names
    errors.extend(_undeclared_reads(label, reads, plugin_id, allowed, event_ok=True))

    if errors:
        raise PluginError(errors)
    return Trigger(plugin=plugin_id, id=trigger_id, event=event, bearer=bearer,
                   conditions=tuple(conditions), sets=tuple(sets), emits=tuple(emits))


#: 表达式里**不必声明**就读得到的名字(内核的日历与流逝)。
_BUILTIN_READS = frozenset({"dt", "now", "day", "hour", "minute", "minute_of_day"})


def _undeclared_reads(
    label: str, names: Iterable[str], plugin_id: str, allowed: set[str],
    *, event_ok: bool = False,
) -> list[str]:
    """读了没声明的东西 —— **开机失败并点名**(插件系统那条读边界的落点)。"""
    out: list[str] = []
    for name in sorted(names):
        bare = name[3:] if name.startswith("me_") else name
        if name in _BUILTIN_READS or bare in _BUILTIN_READS:
            continue
        if event_ok and bare.startswith("event."):
            continue
        if "." not in bare:
            # 光名字 = 内核的量(这个 bearer 自己身上那些)。插件读得到,
            # 但读不到别的插件的 —— 那一支下面判。
            continue
        if name in allowed:
            continue
        owner = bare.split(".", 1)[0]
        if owner == plugin_id:
            out.append(
                f"{label}:读了 `{name}`,而这个插件没声明过这个事实 —— "
                "是不是写错了字?"
            )
        else:
            out.append(
                f"{label}:读了别的插件的 `{name}`,而 `reads` 里没有它。"
                f"要读就写进 `\"reads\": [\"{bare}\"]` —— **未声明的依赖是开不了机的**,"
                "因为装载顺序是按依赖图定的,而没声明的依赖排不进那张图"
            )
    return out


def _state_compared_to_string(
    label: str, rule: Rule, plugin_id: str, facts: Mapping[str, Fact],
) -> list[str]:
    """`sect.rank == "内门"` —— 加载期拦下来,并告诉作者该写几。

    放行的下场是运行期 `TypeError`(float 和 str 比不了),而运行期错误在这一层
    的样子是**这条规律安静地跳过了** —— 世界照跑、日志里一行 warning,而作者
    看到的是"我的规律不生效"。
    """
    out: list[str] = []
    for expression in (*rule.outputs.values(), *rule.conditions):
        for node in ast.walk(expression._tree):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left, *node.comparators]
            for left, right in zip(operands, operands[1:]):
                for a, b in ((left, right), (right, left)):
                    if not (isinstance(a, ast.Attribute)
                            and isinstance(a.value, ast.Name)):
                        continue
                    if not (isinstance(b, ast.Constant) and isinstance(b.value, str)):
                        continue
                    if a.value.id != plugin_id:
                        continue
                    fact = facts.get(a.attr)
                    if fact is None or fact.shape != "state":
                        continue
                    names = [name for name, _ in fact.values]
                    where = names.index(b.value) if b.value in names else None
                    out.append(
                        f"{label}:`{a.value.id}.{a.attr}` 是 `state`,在表达式里它是"
                        f"**序号**(按 values 的顺序,从 0 起),不是那个词 —— "
                        + (f"`{b.value}` 是第 {where} 档,写 {where}"
                           if where is not None
                           else f"而且 `{b.value}` 根本不在 values 里({names})")
                        + "。放行的话它是运行期 TypeError,而那的样子是这条规律安静地跳过了"
                    )
    return out


# ── 依赖图 ──────────────────────────────────────────────────────────────────


def order_plugins(plugins: Iterable[Plugin]) -> list[Plugin]:
    """按依赖排装载顺序。**缺依赖、成环都当场报**,不悄悄挑一个顺序。

    顺序要紧的地方只有一处而且真实:装载时给现有 bearer 种默认值,而一个读了
    `reputation.score` 的插件在它自己的规律第一次求值时,那个量得已经在库里 ——
    否则第一轮读到"不存在的量",这条规律安静地跳过一次。
    """
    by_id = {plugin.id: plugin for plugin in plugins}
    errors: list[str] = []
    for plugin in by_id.values():
        for name in sorted(plugin.reads):
            owner = name.split(".", 1)[0]
            target = by_id.get(owner)
            if target is None:
                errors.append(
                    f"插件 `{plugin.id}` 读 `{name}`,而这个世界里没有装 `{owner}` "
                    "这个插件 —— 缺依赖开不了机(放行的话它的规律会每一轮都"
                    "安静地跳过一次,而作者看到的是「我的机制不生效」)"
                )
            elif name.split(".", 1)[1] not in target.facts:
                errors.append(
                    f"插件 `{plugin.id}` 读 `{name}`,而 `{owner}` 没有声明过这个事实;"
                    f"它声明过的是 {sorted(target.facts)}"
                )
    if errors:
        raise PluginError(errors)

    ordered: list[Plugin] = []
    state: dict[str, int] = {}

    def visit(plugin: Plugin, trail: tuple[str, ...]) -> None:
        mark = state.get(plugin.id, 0)
        if mark == 2:
            return
        if mark == 1:
            raise PluginError([
                f"插件依赖成环:{' → '.join((*trail, plugin.id))} —— "
                "环上没有一个「先装谁」的答案"
            ])
        state[plugin.id] = 1
        for name in sorted(plugin.reads):
            target = by_id.get(name.split(".", 1)[0])
            if target is not None:
                visit(target, (*trail, plugin.id))
        state[plugin.id] = 2
        ordered.append(plugin)

    for plugin in sorted(by_id.values(), key=lambda p: p.id):
        visit(plugin, ())
    return ordered


def version_tuple(version: str) -> tuple[int, ...]:
    """`"1.2.3"` → `(1, 2, 3)`;读不懂的段按 0 算(比较得出来就行)。"""
    out: list[int] = []
    for part in str(version or "").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


# ── 装 / 升 / 卸 ────────────────────────────────────────────────────────────
#
# 三件事共用一份"这个插件此刻装的是什么"的记录(`:plugins` 那个 hash),而**记录
# 里存的是事实名清单,不是声明本身** —— 声明的权威是世界文件那一份,库里存一份
# 拷贝就是第二真相源。记清单是为了**裁剪**:升级时要知道"上一版有而这一版没有的
# 是哪几个",而那件事只有记录答得出。


@dataclass
class InstallReport:
    """一次装载干了什么。**每一格都要说得出话** —— 静默的装载分不出"装上了"和"跳过了"。"""

    installed: list[str] = field(default_factory=list)     # 头一回装
    upgraded: list[tuple[str, str, str]] = field(default_factory=list)   # (id, 旧, 新)
    unchanged: list[str] = field(default_factory=list)     # 同版本,只填缺
    dropped_facts: dict[str, list[str]] = field(default_factory=dict)    # 裁剪掉的
    seeded: int = 0                                        # 填了几个默认值


def install_plugins(
    plugins: Iterable[Plugin], *, store: Any, stock_store: Any, visibility_store: Any,
    owners_of: Any, tick: int = 0, bodies: Mapping[str, Any] | None = None,
) -> InstallReport:
    """把一组(已经排好序的)插件装进这个世界。**幂等。**

    `owners_of(bearer)` 由调用方给:它知道这个世界此刻有哪些 agent、哪些地点、
    哪个种类有哪些实例。**这一层不去猜** —— 猜的话它就得认识调度器、地点表和本体,
    而那正是"内核不认识任何具体系统"要挡的方向。

    ⚠️ **`agent` 那一支在这里只装"已经在册的"。** 后来才出现的人(节拍 `agent_join`、
    重启中途加入、第一次露面的玩家)走 `Scheduler.seed_actor_quantities` —— 那是
    **玩家与角色唯一共同的窄口**,本体层的量早就挂在那儿了,插件的事实跟着走同一条。
    两处各写一遍的话,漏掉任何一处的样子都是安静的:`me_qi.灵力` 恒为 0,
    她做什么都被拒,而回执只说"你做不了"。

    **升级 = 同 id 更高 version**:声明里没了的事实**裁剪**(删值、删可见性行)并
    记进 `dropped_facts`;**低于已装版本当场拒绝** —— 一次"降级"在这一层不是回退,
    是拿旧声明去覆盖新数据,而那不可逆。
    """
    report = InstallReport()
    errors: list[str] = []
    for plugin in plugins:
        row = store.get(plugin.id) or None
        if row is not None:
            was = str(row.get("version") or "")
            if version_tuple(was) > version_tuple(plugin.version):
                errors.append(
                    f"插件 `{plugin.id}`:这个世界里装的是 {was},而文件里是 "
                    f"{plugin.version} —— **不降级**。降级不是回退,是拿旧声明去盖"
                    "新数据(上一版新加的事实会被当成「声明里没了」裁掉),而那不可逆"
                )
                continue
    if errors:
        raise PluginError(errors)

    for plugin in plugins:
        row = store.get(plugin.id) or None
        was = str((row or {}).get("version") or "")
        had = set((row or {}).get("facts") or ())

        # ① 可见性:声明的镜像,**每次都照新声明重写**(和 `_apply_ontology` 里
        #    `redeclare_kinds` 那一半逐字同一条理由:镜像不跟着改,同一个量会有
        #    两个答案,而她读到的是镜像那个)。
        for fact in plugin.facts.values():
            if fact.visibility == "hidden":
                continue
            # `state` 借 `bands` 那条路进感知:档位就是它的序号,档词就是值名。
            # **一套渲染,不是两套** —— 两套的下场是同一个世界里"档"和"状态"在
            # 提示词里读起来是两种东西,而作者写的时候把它们当同一件事。
            bands = fact.bands
            notes = fact.band_notes
            if fact.shape == "state":
                bands = tuple((float(i), name) for i, (name, _n) in enumerate(fact.values))
                notes = tuple(note for _n, note in fact.values)
            visibility_store.declare(
                fact.owner_kind, fact.qualified, fact.visibility,
                fact.label or fact.key, bands=bands, notes=notes)

        # ② 默认值:**只填缺,不覆盖**(创世那条纪律)。
        for fact in plugin.facts.values():
            if fact.bearer == "agent":
                continue          # 走 `seed_actor_quantities` 那个窄口
            for owner in owners_of(fact.bearer):
                have = stock_store.of(owner)
                if fact.qualified in have:
                    continue
                stock_store.set_many(owner, {fact.qualified: fact.default}, tick=int(tick))
                report.seeded += 1

        # ③ 裁剪:上一版有、这一版没有的那几个。**这是插件系统和今天的本体层最大
        #    的一处不同** —— 本体层不裁剪(收严会让已发布世界开不了机,`kind_keys`
        #    那笔账),而插件在**自己的命名空间**里裁自己的,谁都不会被误伤。
        gone = sorted(had - set(plugin.facts))
        if gone:
            report.dropped_facts[plugin.id] = gone
            _prune_facts(plugin.id, gone, stock_store=stock_store,
                         visibility_store=visibility_store, owners_of=owners_of)

        # **声明原文存进库,编译在读取侧** —— 和 `RedisRulesStore` / `RedisOntologyStore`
        # 逐字同一条。理由是"世界"这个东西住在键前缀里,不住在世界文件里:一个从
        # `--world-file` 建起来的世界,下一次开机手上没有那份文件,而它的规律、种类、
        # 插件都得照旧跑。**世界文件是来源,库是世界。**
        # 摘出来的那几格(version / facts / …)是**派生的**,存它们只为一件事:
        # 裁剪要知道"上一版有哪几个事实",而那件事新声明说不出来。
        store.put(plugin.id, {
            "id": plugin.id, "version": plugin.version, "label": plugin.label,
            "facts": sorted(plugin.facts),
            "bearers": sorted(plugin.bearers()),
            "rules": len(plugin.rules), "triggers": len(plugin.triggers),
            "reads": sorted(plugin.reads),
            "body": dict((bodies or {}).get(plugin.id) or {}),
        })
        if row is None:
            report.installed.append(plugin.id)
        elif was != plugin.version:
            report.upgraded.append((plugin.id, was, plugin.version))
        else:
            report.unchanged.append(plugin.id)
    return report


#: 事实住在哪些 owner 名下 —— 裁剪与卸载都要扫这些。
_PRUNE_KINDS = ("agent", "world", "location")


def _prune_facts(
    plugin_id: str, keys: Iterable[str], *, stock_store: Any, visibility_store: Any,
    owners_of: Any,
) -> int:
    """把这几个事实从这个世界里删干净:值、owner 索引、可见性行。"""
    qualified = [f"{plugin_id}.{key}" for key in keys]
    if not qualified:
        return 0
    dropped = 0
    seen: set[str] = set()
    for bearer in _PRUNE_KINDS:
        for owner in owners_of(bearer):
            seen.add(owner)
    # `entity:*` 那一支的 owner 名单由调用方给(它认识本体);这里再兜一次底,
    # 拿 owner 索引扫一遍 —— 一个被裁掉的事实留在某个实例身上,下场和"撤掉的量
    # 还进提示词"逐字相同。
    try:
        seen.update(stock_store.owners())
    except Exception:  # noqa: BLE001 - 扫不动名单不该掀翻装载
        logger.warning("扫 owner 名单失败,插件裁剪只能按已知 bearer 走", exc_info=True)
    for owner in sorted(seen):
        have = stock_store.of(owner)
        for name in qualified:
            if name in have:
                stock_store.delete(owner, name)
                dropped += 1
    for name in qualified:
        for kind in set(_PRUNE_KINDS) | {
            k for (k, key) in visibility_store.rules_map() if key == name
        }:
            try:
                visibility_store.undeclare(kind, name)
            except AttributeError:      # 老的可见性 store 没有这扇门
                break
    return dropped


def remove_plugin(
    plugin_id: str, *, store: Any, stock_store: Any, visibility_store: Any,
    owners_of: Any, dry_run: bool = False,
) -> dict[str, Any]:
    """卸掉一个插件:它的事实、可见性行、记录。**它的规律与触发器随记录一起消失**
    (它们不落库 —— 权威是世界文件那份声明,库里只记"装的是哪一版")。

    ⚠️ **不删别的插件的东西**,一个字都不碰:命名空间就是这条边界的落点。
    ⚠️ **它抹不掉历史** —— 这个插件发过的事件留在日志里,和 `forget_player` 一条。
    日志是唯一的真相,而"这个世界曾经装过这个插件"是一件真的发生过的事。
    """
    row = store.get(plugin_id)
    if row is None:
        return {"plugin": plugin_id, "found": False, "facts": 0,
                "keys": [], "dry_run": bool(dry_run)}
    keys = sorted(row.get("facts") or ())
    if dry_run:
        qualified = [f"{plugin_id}.{key}" for key in keys]
        count = 0
        try:
            owners = sorted(stock_store.owners())
        except Exception:  # noqa: BLE001
            owners = []
        for owner in owners:
            have = stock_store.of(owner)
            count += sum(1 for name in qualified if name in have)
        return {"plugin": plugin_id, "found": True, "facts": count,
                "keys": keys, "dry_run": True}
    dropped = _prune_facts(plugin_id, keys, stock_store=stock_store,
                           visibility_store=visibility_store, owners_of=owners_of)
    store.drop(plugin_id)
    return {"plugin": plugin_id, "found": True, "facts": dropped,
            "keys": keys, "dry_run": False}


def stored_bodies(store: Any) -> list[dict[str, Any]]:
    """库里那几个插件的**声明原文**,按 id 排序。

    ⚠️ **一行没有 `body` 的记录会被跳过并点名。** 那种行只可能来自一次半截的写入
    (或者一份手改过的库),而一个"记着装过、却说不出装的是什么"的插件是最坏的
    形状:`plugin list` 报得出它,规律和触发器却一条都不跑,世界照跑、日志干净。
    """
    out: list[dict[str, Any]] = []
    for plugin_id, row in sorted((store.all() or {}).items()):
        body = (row or {}).get("body")
        if not isinstance(body, dict) or not body:
            logger.warning(
                "插件 %r 在库里只有一行记录、没有声明原文 —— 它的规律与触发器"
                "这一趟一条都不会跑。拿它的世界文件重新装一次(`--world-file`)",
                plugin_id,
            )
            continue
        out.append(dict(body))
    return out


def merge_bodies(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """库里那几份 + 文件里那几份,按 id 去重,**文件里那份赢**。

    和 `kinds` 逐字同一条理由:插件是**法不是状态** —— 它身上没有任何会随时间漂的
    东西(会漂的是它的事实,而那些住在量表里,这里一个字都不碰)。反过来的话,
    一个跑着的世界里的插件永远升不了级。
    """
    out = [dict(row) for row in incoming]
    have = {str(row.get("id") or "") for row in out}
    out += [dict(row) for row in existing if str(row.get("id") or "") not in have]
    return out
