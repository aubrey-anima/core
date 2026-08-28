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
  "emit": [{"when": "size >= max_size", "type": "tree_matured",
            "importance": 0.6,            // 可选:她该多把它当回事(0~1)
            "text": "门口那棵橡树长满了",  // 可选:她记住的那句话
            "on": "rise"}] }              // 可选:哪个方向的跨越算数
```

`emit` 那三个可选字段各补一个洞,而且**都遵守"声明本身就是开关"**(不写 = 行为
和从前逐位相同):

- **`importance`**(0~1):不写的话这条事件**只进日志**,没有任何人会记得它 ——
  线上那个雨季世界的高潮「江水漫堤」真的发生了,而四个角色关于它的记忆是 0 条。
  写了,消费端(scheduler)才把它变成在场的人的一段记忆。`0` 是作者明说的
  "这件事不值得记",和不写等价。
- **`text`**:她记住的**那句话**。没写就回落成 `type` —— `江水漫堤` 也是一句
  真话,空着不是。反过来,**只写 `text` 不写 `importance` 当场开不了机**:那句话
  一个人都读不到,而静默无效是这个仓库最怕的坏法。
- **`on`**:`rise`(默认,不满足→满足)/ `fall`(满足→不满足)/ `both`。
  没有 `fall` 的话「江水漫堤」一生只有一次机会 —— 汛期年复一年、潮起潮落这类
  语义根本写不出来(线上那个世界的水位顶死上限 17 个世界日,一声不吭)。
  **双边沿仍然无状态**:两个值都在这一轮的双缓冲快照里,不存任何"上次是什么"。

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
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from anima_world.expressions import Expression, ExpressionError, compile_expression
from anima_world.world_time import TICKS_PER_DAY

logger = logging.getLogger(__name__)

# 表达式里恒有的名字(不需要声明就能读)。
#
# 前两个是流逝:`dt`(这些量上次被写到现在过了多少 tick)与 `now`(世界时钟)。
# 后四个是**日历** —— 由 `world_time.clock_names` 从 `now` 和这个世界的
# `world.minutes_per_tick` 折出来。它们是后加的,补的是一个把整类规律挡在门外的洞:
# "半夜还醒着要还睡眠债""日落之后果子不再长"都需要"这一天里的哪个时刻",而
# `now % 288` 这种手算解决不了 —— 一天多少 tick 是每个世界自己的配置。
#
# ⚠️ 内置名**盖过**同名的量(见 `stocks._apply` 的 namespace 组装顺序),所以
# 声明一个叫 `hour` 的量是个静默的陷阱:规律读到的永远是钟点,而作者以为读的是
# 他那个量。`ontology.parse_kinds` 因此把它当**加载期错误**拒掉。
BUILTIN_NAMES = ("dt", "now", "day", "hour", "minute", "minute_of_day")
# `world` 这个 owner 的量,在任何表达式里都能以 `world_<key>` 读到。
#
# 注意这一对名字是**不对称**的:世界规律**写**的是光秃秃的 `雨天数`(带前缀写反而
# 被 `bad_output_name` 拒掉),**读**回来却必须是 `world_雨天数`。任何一处拿"名字
# 对得上"当"读的是同一个量"来判的地方,都要把这层前缀补上 —— `drift_warnings`
# 就为此瞎过一次,而且恰好瞎在它被写出来针对的那类规律上。
WORLD_PREFIX = "world_"
WORLD_OWNER = "world"

# 谁参与这条规律。`action` / `not_action` 是一对:此刻**正在**做某件事的人,
# 和此刻**没在**做某件事的人。
#
# `not_action` 是被「熬夜攒睡眠债」逼出来的,而它逼出来的方式值得记一笔:这条规律
# 的意思是"半夜还醒着的人欠觉",而"醒着"在这个引擎里的写法只能是"没在睡"。少了
# 补集,它只有两种写法,而**两种都是错的**:
#
#   - 一条 `{"kind": "agent"}` 的规律 —— 睡着的人也一起欠觉,语义整个反了;
#   - 一条攒债 + 一条还债,都写 `睡眠债` —— 同一轮里两条规律抢同一个量,
#     后写的赢(见 `evaluate_due` 里那句 warning)。而两条 `every` 不同的话
#     它们只是**有时**相撞,于是错得断断续续,查都没法查。
#
# 补集本身没有引入任何新东西:它和 `action` 读的是同一份 `_current_action`,
# 只是取反。而"取反"这件事在数据里不可表达,恰恰会逼作者去改引擎。
#: 🆕 3.8.0 第 2 期:`{"edge": "<插件>.<边类型>"}` —— 作用在**一条边**上。
#: 表达式里读得到 `src.*` / `dst.*` / `edge.*`(见 `expressions.EDGE_PREFIXES`),
#: 而 `set` 写的是**边自己的事实**。
_SELECTOR_KINDS = ("kind", "owner", "action", "not_action", "edge")

#: 一条规律里作者写得到的键。**`contract --json` 的 `plugins.rule_keys` 报的就是它。**
#:
#: 🔴 **它同时是那道"不认识的键当场拒"的判据**(只作用在**插件**的规律上)——
#: 从前随便写一个 `link` / `effects` / `cooldown` 都照收然后丢掉,而作者看到的
#: 只是「我写的那一格没生效」。判据和契约共用一份,才不会有"契约说六个、
#: 引擎认七个"那种漂移。
RULE_KEYS = ("id", "every", "for_each", "set", "when", "emit")

#: `every` 认哪两个单位。
RULE_EVERY_KEYS = ("ticks", "days")

#: 一条 `emit` 里写得到的键;`type` 与 `when` 两个是必填
#: (少了 `type` 当场报,`when` 空表达式编译不过)。
EMIT_KEYS = ("type", "when", "payload", "importance", "text", "on")

#: 🆕 **这一层里"写了 A 就必须写 B"的那几对**(3.8.0,2026-08-27 第二波 ③)。
#:
#: 从前这条耦合只写在 `_parse_memory_text` 的代码里,而契约的 `emit_required_keys`
#: 只列了 `type` / `when` —— 于是创作台的校验器对一份 `{"text": …}` 的 emit
#: **判绿而引擎硬拒**。一条"契约说收、引擎不收"的差,比缺一格更贵:
#: 消费方照契约做出来的东西,在引擎那儿开不了机。
#: ⚠️ **它只管规律那一层的 `emit`**:触发器的 `emit`(`TRIGGER_EMIT_KEYS`)
#: 根本没有 `importance` 这一格 —— 那儿的 `text` 是**载荷里的一句话**,
#: 不进记忆,两层同名不同义。
EMIT_KEY_REQUIRES: dict[str, tuple[str, ...]] = {"text": ("importance",)}
EMIT_REQUIRED_KEYS = ("type", "when")

#: 上面那张表的公开名字 —— `contract --json` 的 `plugins.rule_selectors` 报的就是它。
#: **消费方问这一格,别照文档抄一份清单**(那份清单会烂,而烂了的样子是创作台
#: 对着一条合法的规律报一条假红)。
RULE_SELECTORS = _SELECTOR_KINDS


class RuleError(ValueError):
    """规律脚本有毛病 —— 逐条列出,一次性报完。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid world rules:\n" + "\n".join(f"- {e}" for e in errors))


# 门槛可以往哪个方向跨。缺省 `rise` = 从前的行为,逐位不变。
EMIT_EDGES = ("rise", "fall", "both")


@dataclass(frozen=True)
class Emit:
    """门槛事件:门槛被跨过去那一下才发(哪个方向由 `on` 说了算)。

    `importance` / `text` 是**给消费端的契约**,不是这一层自己用的:量这一层
    不认识记忆,也不认识地点(`who`/`loc` 由 scheduler 从 owner 反查)。它只负责
    把作者声明的轻重和那句话原样放进 `payload`,而且**只在作者真的声明过时才放** ——
    没声明的世界事件形状和从前一模一样。
    """

    when: Expression
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    importance: float | None = None
    text: str = ""
    on: str = "rise"

    def fires_on(self, edge: str) -> bool:
        """这次跨越(`rise` / `fall`)算不算数。"""
        return self.on == "both" or self.on == edge

    def memory_fields(self) -> dict[str, Any]:
        """要往 payload 里加的那两个键 —— 没声明就是空的。

        `importance` 为 0 也是空的:作者明说"这件事不值得记",那就一个字都别发下去,
        否则消费端还要再判一次同一件事(判据分两处写,迟早给出两个答案)。
        """
        if self.importance is None or self.importance <= 0:
            return {}
        return {"importance": float(self.importance), "text": self.text or self.type}


@dataclass(frozen=True)
class Rule:
    id: str
    interval_ticks: int
    selector_kind: str            # "kind" | "owner" | "action" | "not_action"
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


def parse_rules(entries: Any, *, ticks_per_day: int = TICKS_PER_DAY(),
                namespace: str | None = None) -> list[Rule]:
    """把 JSON 里的规律列表编译成 `Rule`。**任何一条坏了就整体拒绝。**

    不逐条丢弃(那是种子里可选字段的宽容原则):规律是世界的物理法则,少一条不是
    "少一点内容",是这个世界从此算错。宁可开不了机。

    `ticks_per_day` 是 `every: {"days": N}` 的换算,而**一天多少 tick 是每个世界
    自己的配置**(`world.minutes_per_tick`)。默认值只是"没人说话时的样子" ——
    真正在跑的那份由装载方喂进来。把 288 写死在这儿的下场是:一个把
    `minutes_per_tick` 调成 10 的世界里,`every: {"days": 1}` 一天跑两遍,而
    作者按"一天一次"写的常数因此翻倍 —— 世界照跑、日志干净,只有数是错的。

    `namespace` 是**插件的 id**(3.8.0)。给了它,`set` 的键就**必须**写成
    `<namespace>.<事实名>`,而且只准是这个命名空间的 —— 一个插件写不到别的插件
    身上,这是插件系统那条"只写自己命名空间"的落点,而不是一句愿望。
    不给(世界自己的规律)则一个字都没变:带点号的名字照旧被 `bad_output_name` 拒。
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
            rules.append(_parse_one(rule_id, label, entry, ticks_per_day,
                                    namespace=namespace))
        except RuleError as exc:
            errors.extend(exc.errors)

    if errors:
        raise RuleError(errors)
    # **这里不打警告。** 一次开机要解析三遍规律(播种 / 装载 / 本体预检),在这儿
    # 打就是同一句话说三遍,而说三遍的警告和没说过一样 —— 人会开始略过它。
    # 点名归**装载**那一处(`__main__._load_world_rules`,每次开机恰好一次)和
    # `validate`(作者真正会去看的地方);`drift_warnings` 是公开的,就是为这个。
    return rules


def drift_warnings(rules: Iterable[Rule]) -> list[str]:
    """**常数步长的自增规律** —— 算多算少结果就不一样。点名,但不拒绝。

    形状是这样的一条:

        {"every": {"days": 1}, "set": {"雨天数": "雨天数 + 1"}}

    它读了自己写的那个量(自增),而步子是个常数 `1`,和"到底过去了多久"无关。
    `every` 只是**节流**,`dt` 才是流逝 —— 一条不看 `dt` 的自增规律因此**对求值
    时机敏感**:多算一次就真的多走一整步。而求值时机不是它自己说了算的(进程重启、
    节流水位丢失、时钟跳变),于是这个世界的物理法则挂在了部署节奏上。

    线上那个世界正因为这个:调试期滚动重启,每次多烧一整天的雨,60 个世界日里
    雨天数 56 而应为 ~50,洪水提前两天半 —— 没有任何一处报错,只是这个世界的
    历法悄悄比它自己的时钟走得快。

    为什么不干脆拒绝:这是**建议**不是错误。一条 `{"季节": "floor(day/90) % 4"}`
    不读自己、算几遍都是同一个答案;而作者也可能真的想要"每次求值加一"的语义
    (比如数的就是"这条规律跑过几轮")。所以判据收得很紧 —— **读了自己写的那个量、
    又没有用 `dt`、而且 `every` 大于 1 tick**,三条都占才点名。误报够多次的警告
    等于没有警告。

    ⚠️ "读了自己"在**世界的量**上换一副样子:`for_each: {"owner": "world"}` 的规律
    写的是光秃秃的 `雨天数`,读回来却要写成 `world_雨天数`(见 `WORLD_PREFIX`)——
    上面那个例子按字面根本跑不起来。只按名字对得上判的话,这道闸对世界的量整个
    是瞎的,**而它被写出来针对的那次事故(线上的雨天数)恰恰就是一条世界规律**。
    """
    messages: list[str] = []
    for rule in rules:
        if rule.interval_ticks <= 1:
            continue          # 每 tick 都算的话根本没有"多算一次"这回事
        world_owned = rule.selector_kind == "owner" and rule.selector_value == WORLD_OWNER
        drifty = sorted(
            key for key, expression in rule.outputs.items()
            if "dt" not in expression.names
            and (key in expression.names
                 or (world_owned and f"{WORLD_PREFIX}{key}" in expression.names))
        )
        if not drifty:
            continue
        messages.append(
            f"规律 {rule.id}:每 {rule.interval_ticks} tick 才算一次,而 "
            f"set.{'/'.join(drifty)} 是自增式(读了它自己)却没有用 `dt` —— "
            "常数步长对「多算一次」不免疫:求值时机一变(重启、节流水位丢失、时钟跳变),"
            "量就平白多加一整份,而这个世界的物理法则从此挂在部署节奏上"
            "(线上那个世界因此把雨天数多烧了 6 天,洪水提前两天半)。"
            f"按流逝折算就不会漂:把常数乘上 `dt / {rule.interval_ticks}`"
        )
    return messages


def _parse_one(rule_id: str, label: str, entry: dict[str, Any],
               ticks_per_day: int = TICKS_PER_DAY(),
               *, namespace: str | None = None) -> Rule:
    errors: list[str] = []

    # 🔴 **不认识的键当场拒 —— 只在插件那一支上**(2026-08-27,创作台重量出来的)。
    #
    # 从前 `link` / `effects` / `cooldown` 随便写,**照收然后丢掉**:作者写下的
    # 那一格根本不在,而退出码 0、日志干净。这是插件这一层最后一个静默住户,
    # 和同一轮收掉的另外三格同种。
    # ⚠️ **作者层的 `rules` 一个字不动**:那是一个早就发出去的面(线上两个世界我
    # 够不着),而"没量过就别收"是这一单一路的口径 —— 收它要先量它。
    if namespace is not None:
        unknown = sorted(set(entry) - set(RULE_KEYS))
        if unknown:
            errors.append(
                f"{label}:不认识的键 {unknown} —— 写下去它会被**静默丢掉**,"
                f"而你看到的只是「我写的那一格没生效」。规律收的是 {list(RULE_KEYS)}"
                "(问 `contract --json` 的 `plugins.rule_keys`)。"
                "⚠️ 边那三条原语(`link`/`unlink`/`transfer`)是**动词与触发器**的"
                "效果,不是规律的 —— 规律这一层只有 `set` 与 `emit`"
                "(`plugins.edge_rule_effects`)"
            )
        for position, spec in enumerate(entry.get("emit") or []):
            if not isinstance(spec, dict):
                continue
            odd = sorted(set(spec) - set(EMIT_KEYS))
            if odd:
                errors.append(
                    f"{label}.emit[{position}]:不认识的键 {odd};"
                    f"收的是 {list(EMIT_KEYS)}(`plugins.emit_keys`)"
                )

    interval = _parse_interval(label, entry.get("every"), errors, ticks_per_day)
    selector = _parse_selector(label, entry.get("for_each"), errors)

    raw_outputs = entry.get("set")
    outputs: dict[str, Expression] = {}
    if not isinstance(raw_outputs, dict) or not raw_outputs:
        errors.append(f"{label}:set 必须是「量名 → 表达式」的对象,而且不能为空")
    else:
        for key, source in raw_outputs.items():
            name = str(key)
            problem = bad_output_name(name, namespace=namespace)
            if problem:
                errors.append(f"{label}.set.{name}:{problem}")
                continue
            try:
                outputs[name] = compile_expression(source, dice=True)
            except ExpressionError as exc:
                errors.append(f"{label}.set.{name}:{exc}")

    conditions: list[Expression] = []
    raw_when = entry.get("when") or []
    if not isinstance(raw_when, list):
        errors.append(f"{label}:when 必须是表达式列表")
    else:
        for position, source in enumerate(raw_when):
            try:
                conditions.append(compile_expression(source, dice=True))
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
            importance = _parse_importance(emit_label, spec, errors)
            text = _parse_memory_text(emit_label, spec, errors)
            on = _parse_edge(emit_label, spec.get("on"), errors)
            try:
                emits.append(Emit(when=compile_expression(spec.get("when"), dice=True),
                                  type=event_type, payload=dict(payload),
                                  importance=importance, text=text, on=on))
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


def _parse_importance(label: str, spec: dict[str, Any], errors: list[str]) -> float | None:
    """`importance`:0~1,可选。**不写 = 只进日志**,和从前逐位相同。

    上下界都是闸而不是 clamp:一个写了 `8` 的作者想的是"很重要",而按 1 截断之后
    他永远不会知道自己写错了刻度 —— 下一条写 `0.8` 的事件会和它一样重。
    """
    if "importance" not in spec:
        return None
    raw = spec["importance"]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        errors.append(
            f"{label}:importance 必须是 0~1 的数(她该多把这件事当回事),"
            f"收到 {type(raw).__name__}"
        )
        return None
    if not 0.0 <= float(raw) <= 1.0:
        errors.append(f"{label}:importance 必须落在 0~1 之间,收到 {raw}")
        return None
    return float(raw)


def _parse_memory_text(label: str, spec: dict[str, Any], errors: list[str]) -> str:
    """`text`:她记住的那句话,可选。**写了它就必须写 importance。**

    只写 `text` 的世界里,那句话一个人都读不到(没有 importance 就不进任何人的
    记忆)—— 世界照跑、日志干净,而作者以为她记住了。这正是"照跑但给错东西"。
    """
    if "text" not in spec:
        return ""
    raw = spec["text"]
    if not isinstance(raw, str):
        errors.append(
            f"{label}:text 必须是字符串(她记住的那句话),收到 {type(raw).__name__}"
        )
        return ""
    if not raw.strip():
        errors.append(f"{label}:text 是空的 —— 不写它会回落成事件类型,写个空串是白写")
        return ""
    # **判据就是上面那张表**,不是又写一遍键名:契约报的和引擎判的是同一份
    # (`tests/test_rules.py` / `tests/test_plugins.py` 各钉一条)。
    if any(need not in spec for need in EMIT_KEY_REQUIRES["text"]):
        errors.append(
            f"{label}:写了 text 却没写 importance —— 只有声明了 importance 的事件"
            "才进得了记忆,不然这句话一个人都读不到(只进日志)。"
            "要她记住就补一个 importance(0~1);只想在日志里留字就写进 payload"
        )
    return raw


def _parse_edge(label: str, raw: Any, errors: list[str]) -> str:
    """`on`:哪个方向的跨越算数。缺省 `rise` —— 不写的世界逐位不变。"""
    if raw is None:
        return "rise"
    value = raw.strip() if isinstance(raw, str) else raw
    if value not in EMIT_EDGES:
        errors.append(
            f"{label}:on 只认 \"rise\"(不满足→满足)/ \"fall\"(满足→不满足)/ "
            f"\"both\",收到 {raw!r}"
        )
        return "rise"
    return value


#: **一个插件命名空间下的名字长什么样**(`<插件id>.<事实名>`)。
#:
#: 这个形状放在这儿而不是 `plugins.py`,是因为**两层都要认它而它们不许互相 import**:
#: 本体那一层(affordance 的 `set` / `costs`)要认出"这个名字有主",插件那一层才知道
#: 主是谁、声明没声明。⚠️ 它**只认形状,不认名单** —— "这个插件到底存不存在、
#: 那个事实声明没声明"由插件层判(`plugins._parse_verb`),那儿两份声明都在手上。
#: 闸在 `tests/test_plugins.py`:这条正则和 `plugins.PLUGIN_ID_PATTERN` 必须对得上,
#: 各写一份的话,一个合法的插件 id 会在本体那一层被判成"怪名字"。
_NAMESPACED = re.compile(r"^[a-z][a-z0-9_]{1,31}\.[^\s.:]+$")


def namespaced_output(name: str) -> bool:
    """这个写入目标是不是**某个插件命名空间下**的名字。"""
    return bool(_NAMESPACED.match(name.strip()))


def bad_output_name(name: str, *, namespace: str | None = None) -> str | None:
    """`set` 的目标是不是一个这条规律**写得到**的量。写不到就当场拒。

    这道闸是踩出来的,而且踩的是最坏的形状。一条规律只能写**它自己那个 owner** 的量,
    但两种"看起来显然能写"的写法此前都被静默接受了:

        "set": {"world_总产量": "world_总产量 + 1"}   → 在那个角色名下建了个
                                                        叫 `world_总产量` 的量
        "set": {"mine:north.储量": "储量 - 1"}        → 同样,建了个带冒号的怪名字

    两次 `rule_stats()` 都报 `written: 5, skipped: 0` —— 专门用来回答"这层跑通了吗"的
    仪表**说的是成功**,而世界的量一动没动。这正是"照跑但给错东西"。
    `world_x` 尤其毒:**读**它是对的(任何表达式都能读全局),于是作者理所当然假设
    写也对称。

    为什么不干脆支持跨 owner 写,而是拒绝:双缓冲下**扇入没有意义**。一条
    `for_each: {"kind": "tree"}` 的规律作用在一百棵树上,每棵读到的 `world_总产量`
    都是这一轮开始前的同一个值,于是"每棵树 +1"的结果是 +1 而不是 +100 —— 一个
    看起来对、算出来错的语义,比当场报错坏得多。要跨实体的相互作用(挖矿让矿脉减少、
    收割让粮仓增加),那是 v2 要设计的东西,不是在这里悄悄放行。
    """
    if not name.strip():
        return "量名不能为空"
    if namespace is not None:
        # **插件只写自己的命名空间。** 这道门是"越界开不了机"那条纪律的落点 ——
        # 放行的样子和量名拼错逐字同一种:安静地在别人的命名空间里建一个量,
        # 而那个插件的规律永远读不到它。
        prefix = f"{namespace}."
        if not name.startswith(prefix):
            return (
                f"插件 `{namespace}` 只写得到自己的命名空间:`{name}` 要写成 "
                f"`{prefix}<事实名>`。写不到别的插件身上,也写不到内核的量上 ——"
                "放行的话它会安静地在别处建一个量,而这个插件的规律永远读不到它"
            )
        local = name[len(prefix):]
        if not local.strip():
            return f"`{name}` 后面没有事实名"
        for mark in (":", "."):
            if mark in local:
                return f"事实名里不能有 `{mark}`:`{name}`"
        return None
    if name.startswith(WORLD_PREFIX):
        return (
            f"写不到全局量:`{name}` 只会在这条规律自己的 owner 名下建一个叫这个名字的量。"
            f"读 `{name}` 是可以的,写不行 —— 要改全局量,用 "
            f'`"for_each": {{"owner": "world"}}` 的规律去写它自己的 `{name[len(WORLD_PREFIX):]}`'
        )
    for mark in (":", "."):
        if mark in name:
            return (
                f"写不到别的实体身上:`{name}` 只会在这条规律自己的 owner 名下建一个"
                f"带 `{mark}` 的怪名字。一条规律只写它自己那个 owner 的量 —— "
                "跨实体的相互作用(挖矿让矿脉减少)v1 还表达不了"
            )
    return None


def _parse_interval(label: str, raw: Any, errors: list[str],
                    ticks_per_day: int = TICKS_PER_DAY()) -> int:
    """`every` 是**节流**,不是步长 —— 真实流逝由 `dt` 带。缺省每 tick 都算。

    `days` 折成 tick 走的是**这个世界的** `world.minutes_per_tick`,不是写死的
    288 —— 那正是这个模块开头那句"`now % 288` 这种手算解决不了"说的事。
    """
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
    for unit, per_unit in (("ticks", 1), ("days", max(1, int(ticks_per_day)))):
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
            f"{{\"kind\": …}} / {{\"owner\": …}} / {{\"action\": …}} / {{\"not_action\": …}}"
        )
        return ("owner", "world")
    key, value = next(iter(raw.items()))
    if key not in _SELECTOR_KINDS:
        errors.append(f"{label}:for_each 不认 {key!r},只认 {list(_SELECTOR_KINDS)}")
        return ("owner", "world")
    # 🆕 3.8.0:`not_action` 收**一列**动作 —— `{"not_action": ["chat", "idle_social"]}`。
    #
    # 它是被 needs 搬家逼出来的,而那件事说明了这个补集为什么必须能收多个:
    # 「社交这个量,在聊天时回一点、在闲坐时回另一点、其余时候一直掉」——
    # 三条规律要**划分**所有人,而单数的 `not_action` 只排得掉一个动作,于是
    # 「不聊天」那一条会连「闲坐」的人一起算进去,和第二条**抢同一个量**。
    # 抢同一个量的下场写在 `evaluate_due` 里:后写的赢,而谁也看不见谁(双缓冲)。
    # **一个看起来对、算出来错的语义,比当场报错坏得多。**
    #
    # 只给 `not_action`,不给 `action`:两条 `action` 的规律作用在**互不相交**的
    # 名单上,本来就不会抢;而"这些动作里的任何一个"写成两条规律更好读。
    if key == "not_action" and isinstance(value, (list, tuple)):
        names = [str(v).strip() for v in value if str(v or "").strip()]
        if not names:
            errors.append(f"{label}:for_each.not_action 是空的 —— "
                          "一个什么都不排除的补集就是「所有人」,直接写 "
                          '{"kind": "agent"}')
            return ("owner", "world")
        # 内部表示仍然是一个字符串(`Rule` 是冻的、进得了缓存),用 `\x00` 连 ——
        # 动作名是作者写的标识符,里面不会有这个字节。
        return (key, "\x00".join(names))
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label}:for_each.{key} 必须是非空字符串"
                      "(`not_action` 也收一列动作名)")
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
