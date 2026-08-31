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
from anima_world.rules import (
    WORLD_PREFIX, Rule, RuleError, namespaced_output, parse_rules,
)

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
#:
#: 🔴 **`actor` / `agent` / `player` 是三个词,而这一格是 2026-08-26 拍的**
#: (第 1 期回报里问的那条产品向,老板自判):
#:
#:   `actor`  —— 角色**和**玩家。**这是今天的语义**(`stock_owner_of` 那条
#:               「同一个命名空间」),出厂 `needs` 声明的就是它。
#:   `agent`  —— 只角色。
#:   `player` —— 只玩家。
#:
#: 为什么值得三个词而不是一个开关:**它是形状,不是产品判断。** 一个作者写「灵力」
#: 时心里想的是谁,只有他知道;而引擎从前**没有地方让他说** —— 挂上去就两种人都有,
#: 那不是一个决定,是一个默认值。⚠️ **`agent` 这个词的含义因此变窄了**,而它是
#: 第 1 期刚公布的:老的声明写 `agent` 时要的是「今天的语义」= 现在的 `actor`。
#: 所以**装载时把 `agent` 读成 `actor`**,并在 `contract` 里把这件事说出来 ——
#: 收紧一个刚发出去的词而不留兼容,下场是第 1 期写的插件安静地少覆盖一半人。
BEARER_FORMS = ("actor", "agent", "player", "world", "location", "entity:<kind>")

#: 老词 → 今天的语义。**只有这一条,而且它是加法**(第 1 期的 `agent` = 两种人)。
BEARER_ALIASES = {"agent": "actor"}

#: 边上的事实收哪几种形状。**比节点多一个 `text`,而这不是偏心,是存储的形状**:
#: 节点事实住在量表里(`[float, tick]`,存不下字符串),而边自己那一行本来就是
#: 一份 JSON。⚠️ **`timer` 两边都不收**,而第 2 期的理由和第 1 期不同了:
#: 边有了之后它**不缺地方住**,它缺的是 `p.k.age` / `p.k.active` 那套语法 ——
#: 而两层点号这一版只在边的三个前缀下面开(`expressions.EDGE_PREFIXES`)。
#: **更要紧的是它不缺能力**:一个存着 tick 的 `number` 加上内核的 `now`,
#: `now - qi.中毒起始 < 100` 就是 `.active`,`now - qi.中毒起始` 就是 `.age`。
#: **为一层语法糖开一道语法,不划算** —— 那道语法正是第 1 期特意关掉的那一道。
EDGE_FACT_SHAPES = ("number", "state", "text")

#: 效果原语。第 2 期把边那三个补上;`spawn` / `destroy` 走动词那条路
#: (它们编译成 affordance 的 `spawn` / `destroys_target`,见 `Verb`)。
EFFECTS = ("set", "emit", "link", "unlink", "transfer")

#: 一个事实的**真相住在哪儿**。这是第 2 期 2b 的全部内容(设计稿 §9.3)。
#:
#:   `stored`    —— 量表里那个数**就是**真相。默认,和第 1 期逐位相同。
#:   `projected` —— 真相是日志里那一串 `<插件>.<事实>.delta`,
#:                  量表里那个数只是**物化视图**。
#:
#: 🔴 **为什么值得多一种模式**:钱包与随身库存今天都是事件重放折出来的
#: (`Projection.balances` / `inventories`),而搬成一个直接写的事实就**丢掉了
#: 「可重放」** —— 「你为什么只剩三块钱」的唯一答案正是那一串 `payment` 事件。
#: 一个直接写的余额答不出这个问题,而且它答不出来的时候不报错。
#: 代价写死了两条:**开机要重放一遍**(内核对 `balances` 本来就在做),
#: **规律读到的是上一轮物化的值**(和双缓冲同构)。
FACT_MODES = ("stored", "projected")

#: `mode:"projected"` 那一格 `sources` 里,一条声明认哪几个键(裁决 ④,2026-08-26)。
#:
#: 🔴 **它是 2d 真正的拦路石**:折叠端只认 `.delta` 后缀,而设计 §9.3 说的是
#: 「`payment` 事件照旧是 `economy.coins` 的 delta」—— **这两句对不上**。
#: 三条路里两条是坏的:改发 `economy.coins.delta`(`payment` 在
#: `SUBSCRIBABLE_EVENTS` 上,**改名 = 破坏消费方**)· 两条都发(**同一笔钱记两遍账**)。
#: 只有"多一格声明"这条不破坏任何消费方。
#:
#:   `event`      —— 认哪一种事件。**只收内核白名单上的**(见 `_parse_sources`)。
#:   `amount`     —— 数在载荷的哪一格(缺省 `amount`)。
#:   `credit`     —— 载荷里哪一格装着"加的那一头"的名字。
#:   `debit`      —— 哪一格装着"减的那一头"。两头至少要有一头。
#:   `owner_form` —— 那个名字是**人的 id**(`actor`,折的时候前缀 `agent:`)
#:                   还是**照原样**(`raw`,`__town__` / `shop:cafe` 那种)。缺省 `actor`。
#:
#: ⚠️ **符号是这张表里最容易写反、而且写反了不报错的一格**,所以两头分开写死成
#: 两个键名(`credit` / `debit`),不给一个 `sign` 让作者自己算。
PROJECTED_SOURCE_KEYS = ("event", "amount", "credit", "debit", "owner_form", "round")

#: `round` 不写时折到第几位。**钱要写 2**,理由见 `projection._apply_payment` 的
#: docstring:二进制浮点存不下 0.1,`60 − 5.23 − 1.16 − …` 折下来是
#: `0.3799999999999921`;门禁读的是这个数,一笔"正好够"的交易迟早会被它拒掉,
#: **而那一次不报错也不留痕**。两套进位就是两个钱包,而它们只在小数第三位往后
#: 分家 —— 那正是"看着一样、判起来不一样"的形状。
DEFAULT_FACT_ROUND = 6

#: `owner_form` 的两个取值。`actor` = 载荷里那个名字是人的 id(`夏` / `player:p1`),
#: 折进量表时要前缀成 `agent:…`(和 `Scheduler.stock_owner_of` 逐字同一条规则);
#: `raw` = 照原样(`__town__` / `shop:cafe` 这种不是人的持有者)。
OWNER_FORMS = ("actor", "raw")

#: `projected` 这一版只收 `number`,而理由不是"还没做":**delta 是一个差值**,
#: 而一个枚举名(`state`)或一句话(`text`)身上没有"差值"这回事 ——
#: 「从『外门』变成『内门』」折不成一个可以相加的数。
#: 要让一段状态可重放,它该是一串"变成了什么"的事件,那是另一种形状。
PROJECTED_SHAPES = ("number",)

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
    #: `text` 那一支专用(只在边上收得下,见 `EDGE_FACT_SHAPES`)。
    text_default: str = ""
    max_chars: int = DEFAULT_TEXT_MAX_CHARS
    #: 存储键要不要带 `<plugin>.` 前缀。
    #:
    #: 🔴 **挂在插件自己声明的种类上的事实,不带。** 那个种类的 id 本身就是
    #: `<plugin>.<名>`(`forge.sword`)—— **种类就是命名空间**,再前缀一次既多余,
    #: 又会撞上本体那一层两道既有的闸(量名不许带 `.`、affordance 的 `set` 写不到
    #: 带点号的名字上)。而那两道闸挡的是**真的**跨实体写,不该为插件放开。
    #: 挂在**共用**载体上的(`actor` / `world` / `location`)照旧带 —— 那儿两个插件
    #: 各声明一个「重量」是会撞的。
    namespaced: bool = True
    #: `stored`(默认)还是 `projected`。见 `FACT_MODES`。
    mode: str = "stored"
    #: 把哪些**既有的内核事件**认成自己的 delta。见 `PROJECTED_SOURCE_KEYS`。
    sources: tuple[dict[str, Any], ...] = ()

    @property
    def qualified(self) -> str:
        """存储里那个键。见 `namespaced` —— 插件自己种类上的事实是**裸名字**。"""
        return f"{self.plugin}.{self.key}" if self.namespaced else self.key

    @property
    def projected(self) -> bool:
        return self.mode == "projected"

    @property
    def delta_event(self) -> str:
        """这个事实的每一次变化落成的那条事件。**只有 `projected` 的才发。**"""
        return f"{self.plugin}.{self.key}.delta"

    @property
    def owner_kind(self) -> str:
        """它落在可见性表的哪一行上 —— `entity:tree` → `tree`。

        ⚠️ **`actor` / `agent` / `player` 三种 bearer 落的是同一行 `agent`**,
        而那不是含糊:可见性表按**种类**声明,而玩家的量 owner 是
        `agent:player:<id>` —— 种类就是 `agent`。三个词分的是"**种谁**",
        不是"**谁看得见**"。
        """
        if self.bearer.startswith("entity:"):
            return self.bearer.split(":", 1)[1]
        if self.bearer in ("actor", "player"):
            return "agent"
        return self.bearer

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

    @property
    def label_text(self) -> str:
        return self.label or self.key

    def render(self, value: Any) -> str:
        """她读到的那一段。**分过档的读档词,数字不上屏;没分档的这一节不列。**"""
        if self.shape == "text":
            return f"{self.label_text} {value}" if value else ""
        word = self.word(float(value))
        return f"{self.label_text} {word}" if word else ""

    def quantity_spec(self) -> dict[str, Any]:
        """这条事实喂给本体那一层时长什么样(`kinds[].quantities` 的一格)。

        **它就是今天的 `Quantity` 声明**,一个新字段都不加 —— `visibility` /
        `label` / `unit` / `bands` 那几格本体层本来就认。`state` 的 `values`
        翻成 `bands`(档位就是序号、档词就是值名),于是**一套渲染,不是两套**。
        """
        spec: dict[str, Any] = {"default": self.default, "visibility": self.visibility}
        if self.label:
            spec["label"] = self.label
        if self.unit:
            spec["unit"] = self.unit
        if self.shape == "state":
            spec["bands"] = [[float(i), name] for i, (name, _n) in enumerate(self.values)]
        elif self.bands:
            spec["bands"] = [[float(t), w] for t, w in self.bands]
        return spec

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
class PluginKind:
    """插件声明的一种**节点**:`entity:<k>`(东西)或 `group:<k>`(有成员的)。

    🔴 **它编译成一个普普通通的本体种类**(`ontology.kinds`,id 是
    `<plugin>.<local>`),而不是引擎里另开的一族。这一条是这一期最省事、也最该
    这么做的一处判断:种类那一层已经有**出生自检**(`check_entity`)、
    **"生成必须要代价"**、`prompt.budget`、可见性、拒绝语 —— 插件另建一套的话,
    那几件要么重写一遍、要么悄悄不生效,而"悄悄不生效"正是这个仓库最怕的形状。

    ⚠️ **`group` 与 `entity` 这一版只差一个记号。** 设计稿自己在 §13 ⑥ 里说不准
    这一刀该不该切(「组织有的行为(解散时成员怎么办)是 entity 没有的」)——
    那些行为这一期一件都没做,所以**现在就把它们做成两种东西是在猜**。
    记号存着(`members`),契约里报出来,等第一件真的只属于 group 的行为出现时
    再分家 —— 那时才知道分在哪儿。
    """

    plugin: str
    local: str
    group: bool = False
    gloss: str = ""
    budget: int | None = None
    facts: dict[str, "Fact"] = field(default_factory=dict)

    @property
    def kind_id(self) -> str:
        """本体里那个种类 id。**带命名空间** —— 两个插件各声明一个 `item` 不会撞。

        ⚠️ 种类 id 不许带**冒号**(那是实例 id 的分隔符),但点号是可以的,
        所以 `economy.item:coffee` 这种实例 id 拆得开:`owner_kind` 按第一个冒号切。
        """
        return f"{self.plugin}.{self.local}"


@dataclass(frozen=True)
class Verb:
    """插件给世界加的一件"能做的事"。

    **按 tool-calling 的 JSON schema 声明**(`name` / `description` / `parameters`,
    设计 §12.3):这样**NPC 挑动词和玩家点按钮读的是同一份定义** —— 而它们从前是
    两份,一份在提示词里、一份在界面上,分岔了不报错。

    🔴 **它编译成目标那个种类上的一个 affordance。** 于是 `requires` / `costs` /
    `consumes` / `duration` / `occupies` / `spawn` / `destroys_target` 那一整摞
    **一件都不用重写** —— 连"声明了 `spawn` 却没写代价就开不了机"那条纪律都跟着来。
    """

    plugin: str
    name: str
    target: str = ""
    label: str = ""
    description: str = ""
    body: dict[str, Any] = field(default_factory=dict)
    #: `link` / `unlink` / `transfer` 那三条,原样交给 `Scheduler.apply_edge_effect`。
    #:
    #: 🔴 **动词必须连得起边来,不能只有触发器那一条路。** 设计稿 §4.2 那四个例子
    #: (开宗立派 / 逐出 / 提拔 / 给东西)**没有一个**是"等某件事发生" —— 它们全是
    #: 「他按一下」。只给触发器的话,作者写「入门」时唯一的写法是自己 emit 一条
    #: 事件再订它,而那条路上"按下去到底成没成"隔了一整个 tick 才知道。
    links: tuple[dict[str, Any], ...] = ()

    @property
    def qualified(self) -> str:
        return f"{self.plugin}.{self.name}"

    def schema(self) -> dict[str, Any]:
        """给模型看的那份工具声明(tool-calling 的形状)。"""
        return {
            "name": self.qualified,
            "description": self.description or self.label or self.qualified,
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string",
                               "description": f"对哪一个 `{self.target}` 做"},
                },
                "required": ["target"],
            },
        }


@dataclass(frozen=True)
class EdgeType:  # noqa: D101 - 见下
    """一种边:有方向、有约束、身上挂事实。

    `exclusive` / `exclusive_to` 是**内核在 `link` 那一刻查的约束**,不是建议:
    「一个人只能在一个门派」写成 `exclusive`,而放行它的样子是安静的 ——
    两条 `member_of` 同时挂着,`plugin list` 看不出来,提示词里她同时是两个门派的人。
    """

    plugin: str
    name: str
    label: str = ""
    src: str = "agent"
    dst: str = "agent"
    exclusive: bool = False        # 起点那一端唯一
    exclusive_to: bool = False     # 终点那一端唯一
    symmetric: bool = False        # 两个方向共一份事实
    facts: dict[str, "Fact"] = field(default_factory=dict)

    @property
    def qualified(self) -> str:
        """存储里那个类型名。**带命名空间** —— 两个插件各声明一个 `owns` 不会撞。"""
        return f"{self.plugin}.{self.name}"


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
    #: `link` / `unlink` / `transfer` 那三条,原样交给 `Scheduler.apply_edge_effect`。
    links: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Plugin:
    id: str
    version: str
    engine_min: str = ""
    label: str = ""
    reads: frozenset[str] = frozenset()
    facts: dict[str, Fact] = field(default_factory=dict)
    edges: dict[str, EdgeType] = field(default_factory=dict)
    kinds: dict[str, PluginKind] = field(default_factory=dict)
    verbs: dict[str, Verb] = field(default_factory=dict)
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
    errors += unknown_keys(label, entry, PLUGIN_KEYS, "plugin_keys")
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
                fact = _parse_fact(plugin_id, f"{label}.facts.{key}", str(key), spec,
                                   subscribable=subscribable)
            except PluginError as exc:
                errors.extend(exc.errors)
                continue
            facts[fact.key] = fact

    kinds: dict[str, PluginKind] = {}
    raw_kinds = entry.get("kinds") or {}
    if not isinstance(raw_kinds, dict):
        errors.append(f"{label}:kinds 必须是「`entity:<名>` / `group:<名>` → 声明」的对象")
    else:
        for name, spec in raw_kinds.items():
            try:
                kind = _parse_kind(plugin_id, f"{label}.kinds.{name}", str(name), spec)
            except PluginError as exc:
                errors.extend(exc.errors)
                continue
            kinds[kind.local] = kind

    edges: dict[str, EdgeType] = {}
    raw_edges = entry.get("edges") or {}
    if not isinstance(raw_edges, dict):
        errors.append(f"{label}:edges 必须是「边类型名 → 声明」的对象")
    else:
        for name, spec in raw_edges.items():
            try:
                edge = _parse_edge(plugin_id, f"{label}.edges.{name}", str(name), spec)
            except PluginError as exc:
                errors.extend(exc.errors)
                continue
            edges[edge.name] = edge

    # ⚠️ **动词排在边后面,而这个顺序是承重的**:动词的 `effects` 里那三条
    # (`link`/`unlink`/`transfer`)要当场查"这个插件声明过这种边吗" ——
    # 反过来的话那道闸只能查一张空表,于是它对每一条都说"没声明过",
    # 或者(更坏)被写成"表是空的就放行"。
    verbs: dict[str, Verb] = {}
    raw_verbs = entry.get("verbs") or {}
    if not isinstance(raw_verbs, dict):
        errors.append(f"{label}:verbs 必须是「动词名 → 声明」的对象")
    else:
        for name, spec in raw_verbs.items():
            try:
                verb = _parse_verb(plugin_id, f"{label}.verbs.{name}", str(name),
                                   spec, kinds, edges, facts, reads)
            except PluginError as exc:
                errors.extend(exc.errors)
                continue
            verbs[verb.name] = verb

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
        where = f"{label}.rules ({rule.id})"
        # 🔴 **规律发的事件也得是自己的命名空间**(2026-08-27 收紧)。
        #
        # 触发器那一层早就查了、会拒(`_parse_trigger` 的 `emit` 那一支),
        # 而规律这一层从前不查 —— **同一个插件里两种写法两种下场**,
        # 而作者读不出为什么。发别人的名字下去,订它的插件会以为那件事真的发生过。
        #
        # ⚠️ **这是一次收紧,理由是量出来的**:第 1 期到今天四个仓库里一条
        # `plugin` 记录都没有,`3.8.0` 没打 tag、PyPI 停在 `3.7.0`、线上镜像是
        # `anima-world:3.7.0`,出厂那三个里唯一发事件的写的就是全名。
        # **消费方为零,所以现在收最便宜** —— 再晚就真的会破坏谁了。
        for emit in rule.emits:
            if not str(emit.type).startswith(f"{plugin_id}."):
                errors.append(
                    f"{where}.emit:插件只发得出自己命名空间的事件 —— "
                    f"要写成 `{plugin_id}.<名字>`,收到 `{emit.type}`。"
                    "发别人的名字下去,订它的插件会以为那件事真的发生过"
                    "(触发器那一层一直是这么查的,规律这一层从今天起一样)"
                )
        if rule.selector_kind != "edge":
            errors.extend(_undeclared_reads(where, rule.reads(), plugin_id, allowed))
            # 🔴 **写的那一半从前没人查**(2026-08-27 验收 C 实测)。
            #
            # `bad_output_name` 只查了前缀:`menpai.<任何字>` 一律放行。于是一条
            # **只写不读**的规律写下 `menpai.声望`(而这个插件的种类上那个事实
            # 其实叫裸名 `声望`)—— **`validate` 说绿、零 warning、日志零字**,
            # 而那张量表里并排住下两个量:`声望` 停在默认值没人更新,
            # `menpai.声望` 每 tick 在涨而没有一处读它。
            # 作者看到的只有「我的声望不动」,而 `rule_stats()` 报的是 written。
            #
            # ⚠️ **触发器那一层早就这么查了**(`_parse_trigger` 的 `set` 那一支),
            # 所以这不是加一道新闸,是把**同一个插件里两种写法的两种下场**抹平 ——
            # 一条 `set` 写在触发器里当场拒、写在规律里静默丢,作者读不出为什么。
            for name in rule.outputs:
                local = name[len(plugin_id) + 1:] \
                    if name.startswith(f"{plugin_id}.") else name
                if local in facts:
                    continue
                errors.append(
                    f"{where}.set.{name}:这个插件的顶层 `facts` 里没有 `{local}`;"
                    f"声明过的是 {sorted(facts)}。"
                    "⚠️ **种类上声明的事实(`kinds.<…>.facts`)量名是裸的,"
                    "规律写不到它** —— 要让规律改一个挂在这种东西身上的量,"
                    f"把它声明成顶层 `facts` 并写 `\"bearer\": \"entity:{plugin_id}."
                    "<你的种类>\"`,名字就带上命名空间了。"
                    "放行的下场是安静的:那张量表里会并排住下两个量,"
                    "规律更新的是没人读的那一个"
                )
            continue
        # 🆕 第 2 期:`for_each: {"edge": …}`。**两道闸和节点那一层不是同一道**,
        # 因为读写的对象换了:读得到的是 `edge.*` / `src.*` / `dst.*`,
        # 写得到的**只有边自己的事实**。
        local = rule.selector_value
        if local.startswith(f"{plugin_id}."):
            local = local[len(plugin_id) + 1:]
        declared = edges.get(local)
        if declared is None:
            errors.append(
                f"{where}.for_each.edge:这个插件没声明过 `{local}` 这种边;"
                f"声明过的是 {sorted(edges)} —— **只跑得动自己声明的边**。"
                "放行的话这条规律每一轮都在一张空表上求值,而 `rule_stats()` "
                "报的是 skipped:专门用来回答\"这层跑通了吗\"的仪表说的是"
                "\"没什么可算的\""
            )
            continue
        rules = tuple(replace(r, selector_value=f"{plugin_id}.{local}")
                      if r is rule else r for r in rules)
        # 🆕 **边上的规律从 2e 起也发得出 `emit`。**
        #
        # 上一轮这里是一条**加载期拒绝**,而那不是保守:那时 `_evaluate_edge_rules`
        # 一条 emit 都不发,而契约里 `effects` 含 `emit`、`rule_selectors` 含 `edge`,
        # **没有一格说这个组合不成立** —— 静默不支持是撒谎。
        # 这一轮它有使用者了(邀请的过期规律:到点发一件事),所以**收进来**,
        # 而收进来是**加法**:`edge_rule_effects` 从 `["set"]` 变 `["set","emit"]`。
        # **顺序是承重的**:先有使用者,再开口子 —— 反过来就是本仓那三道闸挡的
        # 「超前于消费方」。
        for name in rule.outputs:
            fact_name = name[len(plugin_id) + 1:] if name.startswith(f"{plugin_id}.") \
                else name
            if fact_name not in declared.facts:
                errors.append(
                    f"{where}.set.{name}:`{local}` 这种边上没声明过 `{fact_name}`;"
                    f"这条边身上有的是 {sorted(declared.facts)}。"
                    "**边上的规律只写得到边自己的事实** —— 写两端节点身上的量是"
                    "扇入,和 `bad_output_name` 挡的那件事逐字同一种:一条作用在"
                    "一百条边上的规律,每条读到的都是同一份旧值"
                )
        errors.extend(_undeclared_reads(
            where, rule.reads(), plugin_id,
            allowed | {f"edge.{plugin_id}.{key}" for key in declared.facts},
            on_edge=True))

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
                    allowed, subscribable, edges,
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
        reads=frozenset(reads), facts=facts, edges=edges,
        kinds=kinds, verbs=verbs, rules=rules, triggers=tuple(triggers),
    )


#: 插件声明的节点两种前缀。`group` 多一个 `members` 记号(见 `PluginKind`)。
KIND_PREFIXES = ("entity:", "group:")

#: `entity:` / `group:` 后面那个局部名的形状。**非空、不带冒号/点号/空白。**
#:
#: 创作台在这一格上是全仓**唯一一处写死的空白判断**(它诉求里点名的)——
#: 给一格正则,它就不必猜。形状照 `PLUGIN_ID_PATTERN` 那条先例。
#: ⚠️ 中文当然可以(这个引擎中文优先),所以不是 `[a-z]` 那种白名单,
#: 而是**把不行的那几样排掉**。
KIND_LOCAL_PATTERN = r"^[^\s.:]+$"
_KIND_LOCAL = re.compile(KIND_LOCAL_PATTERN)

#: 规律那三格从 `rules` 转出来 —— **同一份常量,不抄第二遍**
#: (抄一遍就是"契约说六个、引擎认七个"那种漂移的来路)。
from anima_world.rules import (  # noqa: E402
    EMIT_KEY_REQUIRES, EMIT_KEYS, EMIT_REQUIRED_KEYS, RULE_EVERY_KEYS, RULE_KEYS,
)

#: 触发器的 `for_each.node` **真受理的那几种**。⚠️ 和 `BEARER_FORMS`(事实挂在谁
#: 身上,六个词)**不是一张表**:这一层收四种,`actor` / `player` 当场拒。
#: 两张表混用过一轮,下场是报错里点着自己拒的取值。
TRIGGER_BEARER_NODES = ("agent", "world", "location")
TRIGGER_BEARER_PREFIXES = ("entity:",)
#: 上面两张表拼出来的**对外形状**,契约那一格(`plugins.trigger_bearer_keys`)的键集
#: 必须和它逐格相等 —— 闸在 `tests/test_plugins.py`,加一种而忘了报,当场红。
TRIGGER_BEARER_FORMS = (*TRIGGER_BEARER_NODES,
                        *(f"{prefix}<kind>" for prefix in TRIGGER_BEARER_PREFIXES))

#: 一条触发器里写得到的键;`id` / `on` / `effects` 三个必填。
TRIGGER_KEYS = ("id", "on", "for_each", "when", "effects")
TRIGGER_REQUIRED_KEYS = ("id", "on", "effects")

#: 🔴 **触发器效果里的 `emit`,键名单和规律那一层的 `emit` 不是同一份。**
#:
#: 规律的 `emit` 有 `when` / `on` / `importance` —— 门槛与边沿是**规律**那一层的
#: 概念(它每 tick 都在算,得有个"跨过去那一下"的说法);触发器的 `emit`
#: **已经"因一件事而发"了**,再要一个 `when` 是同一件事说两遍。
#: ⚠️ **别把这两格合成一格** —— 合了,创作台那边对着一份合法声明就是假红。
TRIGGER_EMIT_KEYS = ("type", "payload", "text")

#: `link` / `unlink` / `transfer` 那三条效果的体。
EDGE_EFFECT_KEYS = ("type", "from", "to", "by_dst", "facts")

#: 一条规律必填的那几个键(`every` 不写 = 每 tick 都算)。
RULE_REQUIRED_KEYS = ("id", "for_each", "set")

#: `plugin` 记录**顶层**写得到的键。
PLUGIN_KEYS = ("id", "version", "engine_min", "label", "reads",
               "facts", "edges", "kinds", "verbs", "rules", "triggers")

#: 一条事实(节点上的、边上的、种类上的,**同一份**)写得到的键。
#: ⚠️ `bearer` 在边与种类上由引擎替作者填,写不写都行;`sources` / `mode`
#: 只对 `projected` 有意义,而那一层自己会拒。
FACT_KEYS = ("bearer", "shape", "mode", "sources", "default", "visibility",
             "label", "unit", "range", "bands", "values", "max_chars")

#: 一种边写得到的键。
EDGE_KEYS = ("label", "from", "to", "exclusive", "exclusive_to", "symmetric", "facts")

#: 插件声明的一种**节点**写得到的键。⚠️ 名字里带 `PLUGIN_`:
#: `ontology.KIND_KEYS` 是**作者层**那张表,两者不是一回事。
PLUGIN_KIND_KEYS = ("gloss", "budget", "prompt", "facts")

#: 🔴 **插件这一族里每一个"会查不认识的键"的层级,一层一格。**
#:
#: 它存在的理由是一条**反向闸**:创作台那侧有一条"盲区不许变多"的断言,
#: 而这一格让它收得到底 —— **加一层却忘了报,盲区就多一个**,而那件事没有一处
#: 会报错(作者写下的那一格根本不在,退出码 0、日志干净)。
#: ⚠️ **加一层就往这儿加一行**,`tests/test_plugins.py` 那条用例逐格点名。
#:
#: ⚠️ **2026-08-31 起这句话不再是"`plugin` 记录里的层级"**:第十二格
#: `authored_edge_keys` 住在**作者层的 `edge` 记录**上(收件箱 D44),不在 `plugin`
#: 记录里。改口是有意的 —— 这张表答的是「插件这一族有哪几层会查不认识的键」,
#: 而**按记录类型给它设边界,正是"一层一层收"那个 bug 的形状**:新开的那一层
#: 恰好在边界外,于是它又一次不在名单上。
STRICT_LEVELS = (
    "plugin_keys", "fact_keys", "edge_keys", "kind_keys", "verb_keys",
    "rule_keys", "emit_keys", "trigger_keys", "trigger_emit_keys",
    "edge_effect_keys", "projected_source_keys", "authored_edge_keys",
)


def unknown_keys(label: str, spec: Any, allowed: tuple[str, ...],
                 grid: str) -> list[str]:
    """这一层多写了哪几个键 —— **一份判断,十一个层级共用**。

    🔴 **一层一层收本身就是这个 bug 的形状**:每收一层,下一次总有人量出新的一层。
    所以这一句写一遍,每层调它一次;而每层的名单都进契约(`STRICT_LEVELS`),
    创作台那条"盲区不许变多"的闸因此收得到底。

    不认识的键**照收然后丢掉**的下场,比"不支持"坏得多:作者写下的那一格
    **根本不在**,而退出码 0、日志干净 —— 他只看得到「我写的那一格没生效」。
    """
    if not isinstance(spec, dict):
        return []
    odd = sorted(set(spec) - set(allowed))
    if not odd:
        return []
    return [
        f"{label}:不认识的键 {odd} —— 写下去它会被「静默丢掉」,"
        f"而你看到的只是「我写的那一格没生效」。这一层收的是 {list(allowed)}"
        f"(问 `contract --json` 的 `plugins.{grid}`,别照文档记一份清单)"
    ]

#: 🔴 **动词的 target 永远不收的那几个词**(裁决 ①,2026-08-26)。
#:
#: 它们不是"还没支持",是**这条路不从这儿走**:对着一个人做的动作要过同意那道门,
#: 而 affordance 这一层没有同意的位置。连带一条更硬的:affordance 一旦挂得上
#: `agent`,`spawn` / `destroys_target` 就**自动对人成立** —— 「造人」老板拍过走
#: `create_agent` 工具路,「抹掉一个人」在这个引擎里根本没有语义(她的一生连着
#: Brain / 记忆 / 转录 / 法务抹除)。要挡就得给 `agent` 开一张例外表,
#: **而例外表本身就是"这一层不该管角色"的证据**。
BUILTIN_TARGETS = ("agent", "actor", "player", "world", "location")

#: 动词的 `effects` 这一版收哪几条。**`set` 不在这儿** —— 动词改量走它自己的
#: `set`(本体那一层已经有了,`me_*` / `have_*` 都认),再开一个入口就是同一件事
#: 两个写法,而两个写法迟早在语义上分岔。
EDGE_VERB_EFFECTS = ("link", "unlink", "transfer")



def _parse_kind(plugin_id: str, label: str, name: str, spec: Any) -> PluginKind:
    errors: list[str] = []
    if not isinstance(spec, dict):
        raise PluginError([f"{label}:声明必须是对象,收到 {type(spec).__name__}"])
    errors += unknown_keys(label, spec, PLUGIN_KIND_KEYS, "kind_keys")
    prefix = next((p for p in KIND_PREFIXES if name.startswith(p)), "")
    if not prefix:
        errors.append(
            f"{label}:名字要写成 `entity:<名>` 或 `group:<名>`,收到 {name!r} —— "
            "**前缀是承重的**:它说的是这一族东西有没有成员"
        )
    local = name[len(prefix):] if prefix else name
    if not _KIND_LOCAL.match(local):
        errors.append(
            f"{label}:`{prefix}` 后面那个名字不能为空,也不能带冒号/点号/空白 —— "
            f"形状是 `{KIND_LOCAL_PATTERN}`"
            "(问 `contract --json` 的 `plugins.kind_local_pattern`,别自己写死判断)"
        )

    facts: dict[str, Fact] = {}
    raw_facts = spec.get("facts") or {}
    if not isinstance(raw_facts, dict):
        errors.append(f"{label}.facts 必须是「事实名 → 声明」的对象")
    else:
        for key, fact_spec in raw_facts.items():
            body = dict(fact_spec) if isinstance(fact_spec, dict) else fact_spec
            if isinstance(body, dict):
                # 挂在这个种类上 —— 作者不必再写一遍 bearer。
                body.setdefault("bearer", f"entity:{plugin_id}.{local}")
            try:
                fact = _parse_fact(plugin_id, f"{label}.facts.{key}", str(key), body,
                                   namespaced=False)
            except PluginError as exc:
                errors.extend(exc.errors)
                continue
            facts[fact.key] = fact

    budget = spec.get("budget", (spec.get("prompt") or {}).get("budget")
                      if isinstance(spec.get("prompt"), dict) else None)
    if budget is not None:
        try:
            budget = int(budget)
        except (TypeError, ValueError):
            errors.append(f"{label}.budget:{budget!r} 不是一个数")
            budget = None
    if errors:
        raise PluginError(errors)
    return PluginKind(
        plugin=plugin_id, local=local, group=name.startswith("group:"),
        gloss=str(spec.get("gloss") or "").strip(), budget=budget, facts=facts,
    )


#: 动词声明里,**原样交给本体那一层**的那几个键。它们的语义一个字都不重新定义 ——
#: `AFFORDANCE_KEYS` 是权威,这里只是把作者写的那份转交过去。
_VERB_PASSTHROUGH = (
    "when", "set", "requires", "costs", "consumes", "duration", "occupies",
    "spawn", "destroys_target", "participants", "importance",
)

#: 动词声明里作者写得到的键。**消费方问这一格,别照文档维护一份清单** ——
#: 那份清单会烂,而烂了的样子是创作台对着一份合法声明报一条假红。
VERB_KEYS = ("target", "label", "description", "effects", *_VERB_PASSTHROUGH)


def _parse_verb(plugin_id: str, label: str, name: str, spec: Any,
                kinds: Mapping[str, PluginKind],
                edges: Mapping[str, "EdgeType"] | None = None,
                facts: Mapping[str, Fact] | None = None,
                reads: Iterable[str] = ()) -> Verb:
    edges = edges or {}
    facts = facts or {}
    errors: list[str] = []
    if not isinstance(spec, dict):
        raise PluginError([f"{label}:声明必须是对象,收到 {type(spec).__name__}"])
    if not name.strip() or any(m in name for m in (*_BAD_FACT_MARKS, ":")):
        errors.append(f"{label}:动词名不能为空,也不能带冒号/点号/空格")

    target = str(spec.get("target") or "").strip()
    if not target:
        errors.append(
            f"{label} 少了 target —— **这一版只收有目标的动词**。"
            "「开宗立派」那种不对着任何东西做的动词还没有一条调用路"
            "(今天的能力调用一律是 `act(她, interact, {target, verb})`),"
            "写了它这个世界就该开不了机,而不是装上去之后谁也点不动它"
        )
    elif target in BUILTIN_TARGETS:
        # 🔴 **裁决 ①(2026-08-26):`agent` 永不进 `verb_target_forms`。**
        #
        # 不是"这一期还没做",是**这条路不从这儿走**。affordance 的形状是
        # 「一个人、一样东西、一个瞬间」,target 格里放进一个人,`拜师` 就是 A
        # 单方面把 B 变成师父 —— 而这个引擎为「他肯不肯」建过一整套东西
        # (邀请三扇门 + `joint_gate` + `INVITE_OUTCOMES`),老板 08-20 拍的第一条
        # 纪律就是**拒绝必须是一等公民**。放开的代价不是多一种开机失败,是
        # **把同意重新变成不可拒绝的**,而它的样子是安静的:世界照跑、日志干净、
        # 边真的连上了。
        #
        # 从前这里一个字都没查,下场是**开机 29 行 Python 栈,而末行怪的是
        # `kinds` 不是插件**(2026-08-26 验收 C 实测)。
        errors.append(
            f"{label}.target:**对着一个人做的动词不从这条路走**,`{target}` 不收。"
            "能力(affordance)的形状是「一个人、一样东西、一个瞬间」—— 把一个人"
            "放进 target 格,`拜师` 就成了单方面把别人变成师父,而这个引擎为"
            "「他肯不肯」建过一整套东西(邀请三扇门),**拒绝是一等公民**。"
            "对人的动词走**工具路 + 同意门**(设计 §12.3),排在第 3 期和判定同期。"
            "这一格收什么,问 `contract --json` 的 `plugins.verb_target_forms` —— "
            f"里面**没有** {sorted(BUILTIN_TARGETS)}"
        )
    else:
        prefix = next((p for p in KIND_PREFIXES if target.startswith(p)), "")
        local = target[len(prefix):] if prefix else target
        if prefix and local not in kinds:
            errors.append(
                f"{label}.target:这个插件没声明过 `{target}`;"
                f"声明过的是 {sorted(KIND_PREFIXES[0] + k for k in kinds)}"
            )
        # 🔴 **裸串那一支从前一个字都没查**(2026-08-26 验收 C):`target: "swrd"`
        # (少了个 o)两扇门全绿、开机全绿,然后**静默长出一个空种类**,
        # 永远不会有实例 —— 而作者看到的是「我的动词点不动」。
        # 这一层不认识作者写的 `kinds`,所以真正的判在 `__main__._merge_plugin_kinds`
        # (那儿两份种类都在手上);这里只把**这个插件自己声明过**的那一支收住。

    links: list[dict[str, Any]] = []
    raw_effects = spec.get("effects") or []
    if not isinstance(raw_effects, list):
        errors.append(f"{label}.effects 必须是列表")
    else:
        for position, effect in enumerate(raw_effects):
            where = f"{label}.effects[{position}]"
            if not isinstance(effect, dict) or len(effect) != 1:
                errors.append(
                    f"{where}:要写成 `{{\"link\": …}}` / `{{\"unlink\": …}}` / "
                    f"`{{\"transfer\": …}}`,一条一个原语"
                )
                continue
            (kind, body_spec), = effect.items()
            if kind not in ("link", "unlink", "transfer"):
                # `set` / `emit` 那两条**不走这儿**:动词改量走 `set`(本体那一层
                # 已经有了,而且它认 `me_*` / `have_*`),`emit` 走 `entity_interaction`
                # 那条既有的路。在这儿再开一个入口 = 同一件事两个写法,而两个写法
                # 迟早在语义上分岔。
                errors.append(
                    f"{where}:动词的 `effects` 这一版只收 "
                    f"{list(EDGE_VERB_EFFECTS)} —— 改量写在动词自己的 `set` 里"
                    "(那是本体那一层,`me_*` / `have_*` 都认)"
                )
                continue
            spec_out = _parse_link_effect(plugin_id, where, kind, body_spec,
                                          edges, errors)
            if spec_out is not None:
                links.append(spec_out)

    # 🔴 **`costs` / `set` 写到插件命名空间上,判在这儿**(3.8.0,2026-08-27,第二波 ①)。
    #
    # 本体那一层**有意不判**这一族(它手上没有插件的声明,判了就是恒为假红),
    # 而从前它按"怪名字"一律拒 —— 下场是**一个插件的动词改不动它自己的事实**:
    # 「施法耗灵力」写不出来,而拒绝语指着一句和插件毫无关系的话
    # (「跨实体的相互作用 v1 还表达不了」,那说的是作者层规律的扇入)。
    errors += _verb_namespaced_writes(plugin_id, label, spec, target, kinds, facts,
                                      reads)

    # 🔴 **不认识的键当场拒,别静默丢**(2026-08-27,创作台接第 2 期时测出来的)。
    #
    # 从前这一行只挑认识的那几个,多写的**照收然后丢掉** —— 作者写下的那一格
    # 根本不在,而退出码 0、日志干净。**和「target 写错字静默长出空种类」同族**,
    # 也和 `sources` 那一层早就有的逐键检查对不上(同一份文件里两种脾气)。
    unknown = sorted(set(spec) - set(VERB_KEYS))
    if unknown:
        errors.append(
            f"{label}:不认识的键 {unknown} —— 写下去它会被「静默丢掉」,"
            f"而你看到的只是「我写的那一格没生效」。这一版收的是 {list(VERB_KEYS)}"
            "(问 `contract --json` 的 `plugins.verb_keys`,别照文档记一份清单)"
        )
    body = {key: spec[key] for key in _VERB_PASSTHROUGH if key in spec}
    label_text = str(spec.get("label") or "").strip()
    if label_text:
        body["label"] = label_text
    if errors:
        raise PluginError(errors)
    return Verb(plugin=plugin_id, name=name, target=target, label=label_text,
                description=str(spec.get("description") or "").strip(), body=body,
                links=tuple(links))


#: 动词的 `costs` 写在**施动者**身上,`set` 写在**目标**身上 —— 两侧收的 bearer 不同。
_ACTOR_BEARERS = ("actor", "player")


def _verb_namespaced_writes(
    plugin_id: str, label: str, spec: dict[str, Any], target: str,
    kinds: Mapping[str, PluginKind], facts: Mapping[str, Fact],
    reads: Iterable[str] = (),
) -> list[str]:
    """动词写到 `<插件>.<事实>` 上时,那个事实真的存在、而且挂对了身子吗。

    🔴 **裁决(2026-08-27,第二波 ①):写只写得到自己的命名空间,`costs` 也不例外。**
    设计稿 §4.2 有一个 `costs: {"economy.coins": "economy.coins - 500"}` 的例子,
    而它写在第 1 期定下那条边界之前 —— **以边界为准**,理由有三条,而第二条是硬的:

    1. **三条写路必须给同一个答案。** 规律的 `set`、触发器的 `set`、动词的 `costs`
       都是"写一个事实";其中两条早就只写得到自己的命名空间,第三条放开就是
       同一件事两种规矩,而作者读不出为什么。
    2. 🔴 **别人的事实可能是 `projected`,而直接写它下次重开就没了。**
       `economy.coins` 今天正是 `mode: "projected"`(真相是 `payment` 事件流,
       量表里那个数是物化视图)—— 一条 `costs` 把它扣掉,**下一次重开物化一遍
       就回来了,而没有一处报错**。而"别人的事实是不是投影"不是写它的人管得着的。
    3. **花钱那件事有它自己的路**:`payment` 事件 → 投影。要拦一个买不起的人,
       用 `reads` 读别人的事实 + `requires` 挡住 —— **读别人的可以,写别人的不行**,
       这和 `reads` 整套设计逐字一致。真正的跨插件扣费等 economy 动词化那一期。
    """
    out: list[str] = []
    reads = set(reads)
    # 🆕 **表达式里的名字也要判**(3.8.0,2026-08-28 第三波 A2)。
    #
    # 本体那一层从这一轮起放行「有主」的名字(那个命名空间真装着插件时),
    # 于是 `me_qi.没这个` 这种**声明过的插件、没声明过的事实**在那一层过得去 ——
    # 而运行期每一次 `ok: False`。作者拿到的是「开机绿、动词永远调不动」,
    # 那比开不了机坏得多。**键那一半判在下面,名字这一半判在这儿。**
    from anima_world.expressions import ExpressionError, compile_expression

    sources: list[tuple[str, Any]] = []
    for key in ("when", "requires"):
        raw_list = spec.get(key)
        if isinstance(raw_list, list):
            sources += [(f"{key}[{i}]", src) for i, src in enumerate(raw_list)]
    for key in ("costs", "set"):
        raw_map = spec.get(key)
        if isinstance(raw_map, dict):
            sources += [(f"{key}.{n}", src) for n, src in raw_map.items()]
    for where, source in sources:
        try:
            names = compile_expression(source).names
        except ExpressionError:
            continue                    # 写坏了的表达式归本体那一层报
        for name in sorted(names):
            bare = name[3:] if name.startswith("me_") else name
            # 🔴 **`world_` 也要剥,和 `_undeclared_reads` 那一处同一条**
            # (2026-08-28 修回归):读自己挂在 `world` 上的事实,写法就是
            # `world_<插件>.<事实>` —— 不剥的话这道闸看到的是 `world_wet` 这个
            # "别人的插件 id",于是报「写进 reads」,照着改下一句变成
            # 「没有装 `world_wet` 这个插件」。**同一条死胡同我在两处各修过一次,
            # 而第二处是我自己新开的** —— 剥前缀这件事只该有一份判断。
            if bare.startswith(WORLD_PREFIX) and not bare.startswith(f"{plugin_id}."):
                bare = bare[len(WORLD_PREFIX):]
            if not namespaced_output(bare):
                continue                # 裸名字归本体那一层判
            if not bare.startswith(f"{plugin_id}."):
                # 🆕 **读别人的要 `reads` 声明,动词这条路也不例外**
                # (3.8.0,第三波 B5 裁决)。REFERENCE §10.2 把这三条写成
                # **这一层的边界**,而动词这条路从前整个绕过它 —— 一条写在文档里
                # 而某条路不守的边界,比没有这条边界更坏:读的人会以为它守着。
                # ⚠️ 它承重在**装载顺序**上:读别人事实的插件要排在它后面装,
                # 否则第一轮读到的是一个还没种下的量。
                if bare not in reads:
                    out.append(
                        f"{label}.{where}:读了别的插件的 `{name}`,而 `reads` 里"
                        f'没有它。要读就写进 `"reads": ["{bare}"]`。'
                        "⚠️ **而那个插件也得真的在这个世界里** —— `reads` 指着一个"
                        "没装的插件同样开不了机;出厂那几个(`economy` / `needs` / "
                        "`invitation`)**今天 `reads` 不到**,它们不是作者记录。"
                        "**读别人的可以,写别人的不行**(见 "
                        "`contract --json` 的 `plugins.namespaced_write_gloss`)"
                    )
                continue
            local = bare[len(plugin_id) + 1:]
            if local in facts:
                continue
            out.append(
                f"{label}.{where}:读了 `{name}`,而这个插件的顶层 `facts` 里没有 "
                f"`{local}`;声明过的是 {sorted(facts)}。"
                "⚠️ 写在 `kinds.<…>.facts` 里的那一族**量名是裸的**,不带命名空间。"
                "**放行的下场是开机绿、而这个动词每一次调用都算不出来** —— "
                "作者看到的是「点不动」,没有一处说得出为什么"
            )
    for where, raw, side in (("costs", spec.get("costs"), "actor"),
                             ("set", spec.get("set"), "target")):
        if not isinstance(raw, dict):
            continue
        for key in raw:
            name = str(key).strip()
            if not namespaced_output(name):
                continue                       # 裸名字归本体那一层判
            owner, local = name.split(".", 1)
            if owner != plugin_id:
                out.append(
                    f"{label}.{where}.{name}:**写不到别的插件的事实上** —— "
                    f"`{owner}` 那一格归它自己管。读得到、写不了:要拦一个"
                    f"「买不起 / 不够格」的人,把 `{owner}.{local}` 写进 `reads`,"
                    "再用 `requires` 挡住。🔴 直接写还有一条更硬的理由:"
                    "别人的事实可能是 `projected`(它的真相是事件流,量表里那个数"
                    "只是物化视图),**扣下去下一次重开就回来了,而没有一处报错**"
                )
                continue
            fact = facts.get(local)
            if fact is None:
                out.append(
                    f"{label}.{where}.{name}:这个插件的顶层 `facts` 里没有 "
                    f"`{local}`;声明过的是 {sorted(facts)}。"
                    "⚠️ 写在 `kinds.<…>.facts` 里的那一族**量名是裸的**,"
                    f"直接写 `{local}` 就行,不带命名空间"
                )
                continue
            if fact.projected:
                out.append(
                    f"{label}.{where}.{name}:`projected` 的事实**写不得** —— "
                    "它的真相是那串 delta 事件,量表里的数只是物化视图,"
                    "直接写下去**重开一次就回到折出来的那个数**,而没有一处报错。"
                    "要改它,发一条它认领的事件"
                )
                continue
            if side == "actor" and fact.bearer not in _ACTOR_BEARERS:
                out.append(
                    f"{label}.costs.{name}:`costs` 扣的是**施动者**身上的量,而 "
                    f"`{local}` 挂在 `{fact.bearer}` 上 —— 扣不到人身上去。"
                    f"施动者那一侧的 bearer 是 {list(_ACTOR_BEARERS)}"
                )
            elif side == "target":
                want = f"entity:{verb_kind_id(plugin_id, target)}"
                if fact.bearer != want:
                    out.append(
                        f"{label}.set.{name}:`set` 写的是**目标**身上的量,而 "
                        f"`{local}` 挂在 `{fact.bearer}` 上,这个动词的目标是 "
                        f"`{want}` —— 写下去落不到目标头上"
                    )
    return out


def verb_kind_id(plugin_id: str, target: str) -> str:
    """这个动词挂在哪个**本体种类**上。

    ⚠️ **只有一份判断。** 编译 `kinds` 行那一处和登记边效果那一处必须答同一个
    答案 —— 各算一遍的话,动词会挂在 A 上而它的边效果登记在 B 上,于是
    **点得动、什么也不发生**,而两边的日志都干净。
    """
    prefix = next((p for p in KIND_PREFIXES if target.startswith(p)), "")
    return f"{plugin_id}.{target[len(prefix):]}" if prefix else target


def compile_kind_rows(plugins: Iterable[Plugin]) -> list[dict[str, Any]]:
    """插件声明的种类与动词 → **普普通通的本体 `kinds` 行**。

    这一步是这一期最省事的那处判断:把它们喂进作者层那条已经在跑的路
    (`_precheck_ontology` → `_seed_ontology` → `_load_ontology` → `_apply_ontology`),
    于是**出生自检、"生成必须要代价"、`prompt.budget`、可见性、拒绝语、
    `resolve` 的跨引用闸**一件都不用重写 —— 而重写它们的下场是"要么重写一遍、
    要么悄悄不生效"。

    ⚠️ **量名带命名空间,动词名不带。** 量住在一张**跨种类共用**的表里
    (`stock:{owner}`),所以两个插件各声明一个「重量」必须分得开;而动词住在
    **它自己那个种类**的声明里,`economy.item` 上的 `买` 和 `sect.token` 上的 `买`
    本来就不会碰面 —— 给它加前缀只会让她提示词里读到 `economy.买`,那是噪音,
    而她要照着它行动。
    """
    by_kind: dict[str, dict[str, Any]] = {}
    for plugin in plugins:
        for kind in plugin.kinds.values():
            row: dict[str, Any] = {"id": kind.kind_id, "quantities": {}, "affordances": {}}
            if kind.gloss:
                row["gloss"] = kind.gloss
            if kind.budget is not None:
                row["prompt"] = {"budget": kind.budget}
            for fact in kind.facts.values():
                row["quantities"][fact.qualified] = fact.quantity_spec()
            by_kind[kind.kind_id] = row
    for plugin in plugins:
        for verb in plugin.verbs.values():
            kind_id = verb_kind_id(plugin.id, verb.target)
            row = by_kind.get(kind_id)
            if row is None:
                # 挂在**作者写的**种类上(`tree`)—— 那一行不归这里造,交给调用方
                # 并进去。⚠️ **调用方必须查它到底在不在**(`_merge_plugin_kinds`):
                # 不查的下场是 `target: "swrd"`(少个 o)静默长出一个空种类,
                # 永远不会有实例,而作者看到的是「我的动词点不动」(验收 C 实测)。
                row = by_kind.setdefault(kind_id, {"id": kind_id, "quantities": {},
                                                   "affordances": {}, "_merge": True})
            row["affordances"][verb.name] = dict(verb.body)
    return [
        {k: v for k, v in row.items() if k != "_merge" and (v or k == "id")}
        for row in by_kind.values()
    ]


def uncreatable_edges(
    plugins: Iterable[Plugin], *, seeded: Iterable[str] = (),
) -> dict[str, list[str]]:
    """声明了、而**没有任何动词或触发器造得出**的那几种边。

    🆕 **`seeded` 是作者层里种下的那几种边**(3.8.0,收件箱 D44)。开了 `edge`
    段之后,「造得出」多了第三条路 —— 而这一句要是不跟着改口,它会对一份
    **写着初始成员**的世界报一句假警报,而作者会去加一个他并不需要的动词。
    ⚠️ 种下的边和 `link` 不是一回事:种下的是**创世那一批**,`link` 管的是
    **后来的人**。所以一个"只种不连"的门派仍然是正当的(创派三弟子写死,
    此后不收徒),这一句对它闭嘴是对的。

    🔴 **它是警告不是错误**,理由和 `_authored_unreachable_requirements` 逐字相同:
    一个还没写完的世界是正当的,而**开机是权威** —— 引擎自己收得下这种声明,
    离线比它严就是假红。但**没有一处会说话**才是真正的问题:作者以为门派建好了,
    而那张边表永远是 0 条,提示词里一个字都不会出现,`plugin list` 上也看不出
    "它本来就造不出来"和"还没有人入门"的差别。

    判得动的理由很具体:**一条边只连得动声明它的那个插件自己的**那几条效果
    (`_parse_link_effect` 那道闸),所以不必去猜别的插件会不会来造它 ——
    数自己这一份就够。

    🔴 **只有 `link` 算「造得出」,`transfer` 和 `unlink` 都不算**(2026-08-27
    验收 A 挑出来的,而它挑对了):`Scheduler.apply_edge_effect` 的 `transfer` 那一支
    是 `of_src`(或 `of_dst`)**把已有的行搬个家** —— 空表上它一行都取不到,当场
    返回 False。**一个只有 `transfer` 动词的插件,边表永远是空的**,而上一版把
    `transfer` 也算成造法,于是这句警告对它**恰好不响** —— 一条在最该响的时候
    闭嘴的警告,比没有这条警告更坏。`unlink` 同理:断一条不存在的边是空操作。
    ⚠️ **出厂插件不进这一趟**(离线那一侧手上只有作者层):`invitation` 那条边是
    内核直接物化的,没有任何动词造它 —— 拿这条规矩去量它会得到一句假警报。
    """
    planted = set(seeded)
    out: dict[str, list[str]] = {}
    for plugin in plugins:
        made = {
            str(spec.get("type") or "")
            for source in (*plugin.verbs.values(), *plugin.triggers)
            for spec in getattr(source, "links", ())
            if spec.get("op") == "link"      # ⚠️ transfer 只搬已有的行,见上
        }
        made |= planted
        idle = sorted(edge.qualified for edge in plugin.edges.values()
                      if edge.qualified not in made)
        if idle:
            out[plugin.id] = idle
    return out


#: 作者层一条 `edge` 记录写得到的键(3.8.0,收件箱 D44)。
#:
#: 🔴 **`facts` 有意不收,而这不是漏了。** 运行期 `link` 那条路上,声明里的事实
#: **带命名空间落库**(`apply_edge_effect` 写的是 `f"{plugin}.{key}"`),而效果里
#: 手写的 `facts` 是**原样**塞进去的 —— 同一个键两种写法两种下场,而那正是
#: 「量表里并排住下两个量」那一族(§3.50)的第三个入口。收作者层这一格,等于把
#: 一个已经在打架的语义再复制一份;不收,声明过的默认值照旧逐个落地
#: (`apply_edge_effect` 替它填),而作者写了会**当场看到一句拒绝**,不是静默丢掉。
#: ⚠️ 运行期那处不一致本身是一条独立的账,记在 FOR-STUDIO §3.60,别在这儿顺手改。
AUTHORED_EDGE_KEYS = ("type", "from", "to")


def authored_edge_errors(
    entries: Any, plugins: Iterable[Plugin], *,
    factory_ids: Iterable[str] = (),
    namespace_list_is_complete: bool = False,
) -> tuple[list[str], list[str]]:
    """作者层种下的那几条边立不立得住 —— **开机与离线两扇门共用这一份**。

    返回 `(errors, warnings)`。3.8.0 收件箱 D44 开 `edge` 段时新增。

    **这道闸分得清"查得动"和"查不动",而分界不是 `--edit`,是数据本身**:

    - 边的 `type` 写成 `<插件>.<边名>` 的形状、两端不空、没有不认识的键、
      同一条边没写两遍 —— **永远查得动**(在这份文件自己肚子里)。
    - `<插件>` 在手上这份名单里 → 那它必须真的声明过这种边,而且两端的
      节点 id 要配得上声明的那一端、`exclusive` 不许在同一份文件里自相矛盾。
      **也查得动**(声明就在手上)。
    - `<插件>` 不在名单里 → **答案取决于这份名单全不全**,见下。

    🔴 **`namespace_list_is_complete` 是这个函数最要紧的一格**(2026-08-31 验收 A
    逮的 P1)。它说的是**调用方手上那份插件名单是不是这个世界的全集**:

    - **开机 = 是**(`_plugin_bodies` 合了「出厂 + 库里 + 文件」三个来源)。
      名单里没有,就是这个世界里**真的没有这个插件** → **当场报错**。
    - **离线两扇门 = 否**(手上只有这份文件)。一次编辑最常见的形状就是"只带
      几条边",而那种边的声明在**目标世界的库里** → **说出来这一格没查**,不猜。

    ⚠️ **第一版把这两件事写成了同一句**(开机原样复用了离线那条 `continue`),
    于是一个把 `menpai` 打成 `menpais` 的作者拿到的是:**开机成功、边真的落库、
    幻影类型被 sadd 进 `edge_types`、节点 id 那道闸一次都没跑**,而屏幕上印着的
    正是这个函数写的那句「一份完整的世界文件走到这儿八成是漏了 `plugin` 段 ——
    那种文件真开机时会当场红」。**说那句话的就是开机,而它没红。**
    一句自己证伪自己的诊断比没有诊断更坏:它让读的人相信这条路已经有人守着。

    🔴 **出厂插件的边一律拒**(`factory_ids`):`invitation.invites` 是内核**投影的
    物化视图**(`rebuild_invitation_edges` 每次开机照日志重建一遍),手写一行进去
    要么下一秒被抹掉、要么就是**伪造演化态** —— 而伪造演化态"没有任何地方会报错"
    正是这个系统最怕的那一种。作者层种的是创世态,不是别人折出来的账。
    """
    rows = list(entries or ())
    if not rows:
        return [], []
    declared: dict[str, EdgeType] = {}
    owners: set[str] = set()
    for plugin in plugins:
        owners.add(plugin.id)
        for edge in plugin.edges.values():
            declared[edge.qualified] = edge
    factory = set(factory_ids)

    errors: list[str] = []
    warnings: list[str] = []
    unchecked: set[str] = set()
    seen: set[tuple[str, str, str]] = set()
    # `exclusive` / `exclusive_to` 在**这一份文件之内**自相矛盾的那两格。
    # 放行的样子是安静的:`apply_edge_effect` 只 `logger.warning` 一句然后
    # 返回 False,于是"我明明写了三条,世界里只有一条"而屏幕上什么都没有。
    by_src: dict[tuple[str, str], str] = {}
    by_dst: dict[tuple[str, str], str] = {}

    for index, row in enumerate(rows):
        label = f"edges[{index}]"
        if not isinstance(row, dict):
            errors.append(
                f"{label}:一条边必须是一个对象,收到 {type(row).__name__}")
            continue
        errors += unknown_keys(label, row, AUTHORED_EDGE_KEYS, "authored_edge_keys")
        edge_type = str(row.get("type") or "").strip()
        src = str(row.get("from") or "").strip()
        dst = str(row.get("to") or "").strip()
        missing = [k for k, v in (("type", edge_type), ("from", src), ("to", dst))
                   if not v]
        if missing:
            errors.append(
                f"{label}:少了 {missing} —— 一条边是"
                f"`{{\"type\": \"<插件>.<边名>\", \"from\": …, \"to\": …}}`")
            continue
        namespace, _, local = edge_type.partition(".")
        if not namespace or not local:
            errors.append(
                f"{label}:`type` 要写成 `<插件>.<边名>`(边类型名带命名空间,"
                f"两个插件各声明一个 `owns` 才不会撞),收到 {edge_type!r}")
            continue
        if namespace in factory:
            errors.append(
                f"{label}:`{edge_type}` 是「出厂插件 `{namespace}` 的边」,"
                "作者层里种不得 —— 那几条是内核「投影的物化视图」"
                "(每次开机照事件日志重建一遍),手写一行进去要么下一秒被抹掉、"
                "要么就是伪造这个世界的历史,而两种都不报错。"
                "它该由发生的事情长出来,不是由文件写死")
            continue
        key = (edge_type, src, dst)
        if key in seen:
            errors.append(
                f"{label}:这条边写了两遍(`{edge_type}` {src} → {dst})—— "
                "边是幂等的,第二条什么也不多做;删掉一条,或者其中一条本来"
                "想写的是别的两端")
            continue
        seen.add(key)
        if namespace not in owners:
            if namespace_list_is_complete:
                # **开机手上是全集,所以这一格它答得出** —— 名单里没有,就是这个
                # 世界里真的没有这个插件。放行的样子是安静的:边真的落库、
                # 幻影类型进 `edge_types`、而节点 id 那道闸一次都不会跑。
                errors.append(
                    f"{label}:这个世界里没有名叫 `{namespace}` 的插件,"
                    f"所以 `{edge_type}` 这种边不存在;装着的是 "
                    f"{sorted(owners) or '(一个都没有)'} —— "
                    "打错一个字的下场是那一行安安静静待在库里,"
                    "规律读不到它、提示词里一个字都不会出现")
                continue
            # **查不动的那一格,说出来。** 见 docstring。
            unchecked.add(namespace)
            continue
        spec = declared.get(edge_type)
        if spec is None:
            mine = sorted(e.qualified for e in declared.values()
                          if e.plugin == namespace)
            errors.append(
                f"{label}:插件 `{namespace}` 没声明过 `{edge_type}` 这种边;"
                f"它声明过的是 {mine or '(一种都没有)'} —— "
                "种一条没人声明过的边,那一行会安安静静地待在库里,"
                "而规律读不到它、提示词里一个字都不会出现")
            continue
        for where, end, node in (("from", spec.src, src), ("to", spec.dst, dst)):
            problem = edge_node_id_error(end, node, plugin_id=spec.plugin)
            if problem is not None:
                errors.append(f"{label}.{where}:{problem}")
        if spec.exclusive:
            first = by_src.get((edge_type, src))
            if first is not None:
                errors.append(
                    f"{label}:`{edge_type}` 声明成 `exclusive`(起点那一端唯一),"
                    f"而 {src} 在这份文件里已经有一条了({first})—— "
                    "开机只会认第一条,第二条静静地不生效")
            else:
                by_src[(edge_type, src)] = f"{label} → {dst}"
        if spec.exclusive_to:
            first = by_dst.get((edge_type, dst))
            if first is not None:
                errors.append(
                    f"{label}:`{edge_type}` 声明成 `exclusive_to`(终点那一端唯一),"
                    f"而 {dst} 在这份文件里已经有一条了({first})—— "
                    "开机只会认第一条,第二条静静地不生效")
            else:
                by_dst[(edge_type, dst)] = f"{label} ← {src}"

    if unchecked:
        warnings.append(
            f"这份文件里种了 {sorted(unchecked)} 名下的边,而「没有哪个 `plugin` "
            "记录声明过它们」 —— 如果那些插件已经装在目标世界里(一次编辑最常见的"
            "形状就是只带几条边),这些边照旧连得上,而「离线这一格答不了」;"
            "要在这里就查它,把那几条 `plugin` 记录也放进这份包。"
            "⚠️ 一份「完整」的世界文件走到这儿八成是漏了 `plugin` 段 —— "
            "那种文件真开机时会当场红")
    return errors, warnings


def borrowed_kind_errors(
    plugins: Iterable[Plugin], declared: Iterable[str],
) -> list[str]:
    """动词借的那个种类,这个世界里真的有吗 —— **开机与离线两扇门共用这一份**。

    🔴 **它从前只住在开机那一侧**(`__main__._merge_plugin_kinds`),于是
    `target: "swrd"`(少了个 o)这种写法**离线两扇门答绿、真开机退 1** ——
    正是 §3.28 治过的那一族假绿的又一格,而这一次它是插件带进来的。
    创作台出包前那道闸读的就是离线那两扇门:绿灯放行,作者拿着一份开不了机的包
    去发布,而怪罪的方向多半还是错的。

    ⚠️ **判据里那份 `declared` 是作者自己写的 `kinds`,不是并过插件行的那一份** ——
    并过之后每一个借来的名字都"存在"了(`compile_kind_rows` 给它造了一行),
    这道闸就永远查不出东西来。开机那侧本来就是拿并之前那份判的,两边必须同一份。
    """
    known = {str(k) for k in declared}
    borrowed = borrowed_kind_ids(plugins)
    return [
        f"动词 {'、'.join(sorted(borrowed[kind_id]))} 的 target 指着 "
        f"`{kind_id}`,而这个世界里没有这个种类 —— 是不是写错了字?"
        f"这个世界声明过的种类是 {sorted(known)}。"
        "**放行的下场是安静的**:它会长出一个空种类,永远不会有实例,"
        "而你看到的只是「我的动词点不动」"
        for kind_id in sorted(set(borrowed) - known)
    ]


def borrowed_kind_ids(plugins: Iterable[Plugin]) -> dict[str, list[str]]:
    """动词挂到了**别人**种类上的那几个 id → 哪几个动词借的。

    调用方(`__main__._merge_plugin_kinds`)拿它去查「这个种类真的存在吗」——
    **只有那儿两份种类都在手上**(插件的 + 作者写在 `kinds` 里的)。
    """
    out: dict[str, list[str]] = {}
    for plugin in plugins:
        own = {kind.kind_id for kind in plugin.kinds.values()}
        for verb in plugin.verbs.values():
            kind_id = verb_kind_id(plugin.id, verb.target)
            if kind_id in own:
                continue
            out.setdefault(kind_id, []).append(f"{plugin.id}.{verb.name}")
    return out



#: 边的两端认哪几种节点。⚠️ **和 `bearer` 不是一张表**:边连的是**节点**,
#: 而 `actor` / `player` 那两个词说的是"给谁种事实" —— 一个是图上的位置,
#: 一个是播种的范围。合成一张表会让 `{"from": "player"}` 读起来像是一种节点类型。
EDGE_ENDS = ("agent", "player", "location", "world",
             "entity:<kind>", "group:<kind>")

#: 🔴 **上面那张表里,后两个是「形状」不是「值」**(2026-08-26 验收 C 实跑逮的)。
#: 它从前只列四个裸词,而 `_parse_edge` 真收 `entity:` / `group:` 前缀 ——
#: **照契约判的 tool 会拒掉一个引擎跑得起来的世界**,而那是这一族最贵的错法
#: (假红灯:作者去改一个没错的东西)。判的时候用这个前缀集,别拿裸词做等值比较。
EDGE_END_PREFIXES = ("entity:", "group:")

#: 边的两端写成节点 id 时长什么样。**一端一行,和 `EDGE_ENDS` 一一对应。**
#:
#: 🔴 **它从前只是 `__main__` 里一个手写的 dict**(契约那一格),而 2026-08-31 开
#: 作者层 `edge` 段时要**照着它判**一条边写得对不对 —— 判的地方和印的地方各存一份,
#: 就是这个仓库最贵的那种不一致:契约印 A、闸按 B 判,而两边都不报错。
#: 收成一份常量之后,`contract --json` 的 `plugins.edge_node_id_forms` 与
#: `authored_edge_errors` 读的是同一行。
#:
#: ⚠️ **`player` 那一行最容易写错**:玩家的节点 id 是 `agent:player:<id>`,不是
#: `player:<id>` —— 玩家和角色**同一个量表命名空间**(`Scheduler.stock_owner_of`),
#: 而边的两端用的就是那个 owner key。
EDGE_NODE_ID_FORMS: dict[str, str] = {
    "agent": "agent:<agent_id>",
    "player": "agent:player:<player_id>",
    "location": "location:<location_id>",
    "world": "world",
    "entity:<kind>": "<kind>:<实例名>(就是那条 entity 记录的 id)",
    "group:<kind>": "<kind>:<实例名>(同上)",
}

#: 玩家节点 id 的前缀。写死在这儿而不是拼 `Scheduler.PLAYER_PREFIX`,理由是
#: 这一层不认识 scheduler;闸钉着两边相等
#: (`tests/test_plugins.py::test_契约说得出这两个答案_而且那几格是真的`)。
_PLAYER_NODE_PREFIX = "agent:player:"


def edge_node_id_error(end: str, node: str, *, plugin_id: str = "") -> str | None:
    """一个节点 id 配不配得上它那一端的声明。**配得上就答 `None`。**

    🔴 **`entity:` / `group:` 那两端写的是「局部名」,而实例 id 里是「全名」** ——
    声明 `{"to": "group:sect"}` 的插件 `menpai`,它的实例 id 是
    `menpai.sect:青云门`,不是 `sect:青云门`。所以这里补命名空间走的是
    `verb_kind_id`(**动词挂哪个种类**问的是同一个问题、同一个函数)。
    ⚠️ 自己再算一遍 `f"{plugin}.{end}"` 的话,`KIND_PREFIXES` 哪天多一个前缀,
    动词那一边跟上了、这一边没有,而不一致的样子是一盏假红灯。

    作者层 `edge` 段(3.8.0,收件箱 D44)那道闸的核心一句。它查的是**形状**,
    不是"这个东西存不存在" —— 后者是跨引用(实例可能在目标世界的库里),
    而形状在这份文件自己肚子里就判得动。

    🔴 **为什么形状值得当硬错误拦**:一条 `{"from": "阿岚"}`(少了 `agent:` 前缀)
    的边**建得出来**,`edge:` 那个 hash 里真有这一行 —— 而 `src.体力` 读的是
    `stock_store.of("阿岚")`(空的)、`connected` 那一档比的是她的 owner key
    (对不上),于是这条边**谁也读不到、谁也看不见**,零报错。
    和「量名拼错当场开不了机」逐字同一种病,所以给它同一种下场。
    """
    node = str(node or "")
    if end == "world":
        return None if node == "world" else (
            f"`world` 那一端只能写 `world` 本身,收到 {node!r}")
    if end == "player":
        return None if node.startswith(_PLAYER_NODE_PREFIX) and node[len(
            _PLAYER_NODE_PREFIX):] else (
            f"`player` 那一端要写 `{EDGE_NODE_ID_FORMS['player']}`,收到 {node!r} —— "
            "⚠️ 玩家和角色是「同一个量表命名空间」,所以是 `agent:player:p1`,"
            "不是 `player:p1`")
    if end == "agent":
        ok = (node.startswith("agent:") and not node.startswith(_PLAYER_NODE_PREFIX)
              and node[len("agent:"):])
        return None if ok else (
            f"`agent` 那一端要写 `{EDGE_NODE_ID_FORMS['agent']}`,收到 {node!r}"
            + ("(那是一个玩家 —— 玩家只接得上声明成 `player` 的那一端,"
               "而这一端声明的是 `agent`)"
               if node.startswith(_PLAYER_NODE_PREFIX) else ""))
    if end == "location":
        return None if node.startswith("location:") and node[len("location:"):] else (
            f"`location` 那一端要写 `{EDGE_NODE_ID_FORMS['location']}`,收到 {node!r}")
    # `entity:<kind>` / `group:<kind>` —— 前缀即种类,和实例 id 的规矩逐字同一条。
    if any(end.startswith(prefix) for prefix in EDGE_END_PREFIXES):
        kind = verb_kind_id(plugin_id, end) if plugin_id else end.partition(":")[2]
        head, _, tail = node.partition(":")
        if head == kind and tail:
            return None
        return (
            f"`{end}` 那一端要写 `{kind}:<实例名>`(就是那条 entity 记录的 id;"
            f"⚠️ 种类名在实例 id 里是全名 `{kind}`,不是声明里那个局部名),"
            f"收到 {node!r}")
    return f"不认识的一端 `{end}` —— 这是插件声明里的问题,不是这条边的"


def _parse_edge(plugin_id: str, label: str, name: str, spec: Any) -> EdgeType:
    errors: list[str] = []
    if not name.strip() or any(m in name for m in _BAD_FACT_MARKS):
        errors.append(f"{label}:边类型名不能为空,也不能带 {list(_BAD_FACT_MARKS)}")
    if not isinstance(spec, dict):
        raise PluginError([f"{label}:声明必须是对象,收到 {type(spec).__name__}"])
    errors += unknown_keys(label, spec, EDGE_KEYS, "edge_keys")

    # ⚠️ **声明里这两个键仍然叫 `from` / `to`**(设计稿那两个词),而**表达式里**
    # 的前缀是 `src` / `dst` —— 因为 `from` 是 Python 关键字,`ast` 连语法都过不去。
    # 两套词只在这一处分岔,理由写在 `expressions.EDGE_PREFIXES` 上。
    src = str(spec.get("from") or "agent").strip()
    dst = str(spec.get("to") or "agent").strip()
    for where, value in (("from", src), ("to", dst)):
        if value in EDGE_ENDS or value.startswith(("entity:", "group:")):
            continue
        errors.append(
            f"{label}.{where}:不认识的一端 `{value}`;认的是 {list(EDGE_ENDS)} "
            "或者 `entity:<种类>` / `group:<种类>`"
        )

    facts: dict[str, Fact] = {}
    raw_facts = spec.get("facts") or {}
    if not isinstance(raw_facts, dict):
        errors.append(f"{label}.facts 必须是「事实名 → 声明」的对象")
    else:
        for key, fact_spec in raw_facts.items():
            body = dict(fact_spec) if isinstance(fact_spec, dict) else fact_spec
            if isinstance(body, dict):
                # 边上的事实**不写 bearer**(它挂在这条边上,不挂在谁身上)——
                # 借节点那份解析器,所以这里替它填一个,免得作者被要求写一个
                # 对边毫无意义的键。
                body.setdefault("bearer", "world")
            try:
                fact = _parse_fact(plugin_id, f"{label}.facts.{key}", str(key), body,
                                   shapes=EDGE_FACT_SHAPES)
            except PluginError as exc:
                errors.extend(exc.errors)
                continue
            facts[fact.key] = fact

    if errors:
        raise PluginError(errors)
    return EdgeType(
        plugin=plugin_id, name=name,
        label=str(spec.get("label") or "").strip(), src=src, dst=dst,
        exclusive=bool(spec.get("exclusive")),
        exclusive_to=bool(spec.get("exclusive_to")),
        symmetric=bool(spec.get("symmetric")), facts=facts,
    )


def _parse_fact(plugin_id: str, label: str, key: str, spec: Any,
                *, shapes: tuple[str, ...] = FACT_SHAPES,
                namespaced: bool = True,
                subscribable: Iterable[str] | None = None) -> Fact:
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
    errors += unknown_keys(label, spec, FACT_KEYS, "fact_keys")

    shape = str(spec.get("shape") or "number").strip()
    if shape in DEFERRED_SHAPES and shape not in shapes:
        errors.append(
            f"{label}:`{shape}` 这一版还不收 —— {DEFERRED_SHAPES[shape]}。"
            f"这里收的是 {list(shapes)}"
            "(按 `contract --json` 的 `plugins.fact_shapes` / `edge_fact_shapes` "
            "探测,别照设计稿写)"
        )
    elif shape not in shapes:
        errors.append(
            f"{label}:不认识的 shape `{shape}`;这里收的是 {list(shapes)}"
            + ("(⚠️ `text` 只在**边**上收得下 —— 节点的事实住在量表里,"
               "那儿存的是 `[float, tick]`)" if shape == "text" else "")
        )

    mode = str(spec.get("mode") or "stored").strip()
    if mode not in FACT_MODES:
        errors.append(f"{label}:不认识的 mode `{mode}`;只有 {list(FACT_MODES)}")
        mode = "stored"
    elif mode == "projected" and shape not in PROJECTED_SHAPES:
        errors.append(
            f"{label}:`{shape}` 的事实做不了 `projected`,而这不是"
            "「还没做」—— **delta 是一个差值**,而一个枚举名或一句话身上没有"
            "「差」这回事:「从『甲』变成『乙』」折不成一个可以相加的数。"
            f"`projected` 收的是 {list(PROJECTED_SHAPES)}"
            "(按 `contract --json` 的 `plugins.projected_shapes` 探测)"
        )
        mode = "stored"
    elif mode == "projected" and (
            not namespaced
            or str(spec.get("bearer") or "").strip().startswith(("entity:", "group:"))):
        # 挂在插件自己种类上的事实住在那个实例的量表里,而实例**会被 `destroy`
        # 抹掉** —— 一串折向一个不存在的 owner 的 delta,重放出来的是一个没有主人
        # 的数。要让"东西身上的量"可重放,先得回答"它没了之后那串账归谁",
        # 而那是另一期的事。
        errors.append(
            f"{label}:挂在**一样东西**上的事实做不了 `projected` —— "
            "那样东西会被 `destroy` 抹掉,而一串折向一个不存在的主人的 delta,"
            "重放出来是一个没有主人的数。挂在 `actor` / `world` / `location` "
            "上的可以(问 `contract --json` 的 `plugins.projected_bearers`)。"
            "⚠️ 写在 `kinds` 里和写在顶层 `facts` 里(`bearer: \"entity:…\"`)"
            "**是同一件事**,两种写法一样拒 —— 从前只拦住了前一种,"
            "而后一种照收:契约说不收、引擎照收,是这一族最贵的不一致"
        )
        mode = "stored"

    sources = _parse_sources(label, spec, mode, errors, subscribable)

    bearer = str(spec.get("bearer") or "").strip()
    # 第 1 期写 `agent` 的插件要的是「今天的语义」(角色 + 玩家)—— 收紧一个刚发
    # 出去的词而不留兼容,下场是那些插件安静地少覆盖一半人。
    bearer = BEARER_ALIASES.get(bearer, bearer)
    if not bearer:
        errors.append(f"{label} 少了 bearer —— 这个事实挂在谁身上?{list(BEARER_FORMS)}")
    elif (bearer not in ("actor", "player", "world", "location")
          and not bearer.startswith("entity:")):
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

    if shape == "text":
        # 边上那一行本来就是 JSON,字符串直接住得下。上限照契约那一格。
        max_chars = spec.get("max_chars", DEFAULT_TEXT_MAX_CHARS)
        try:
            max_chars = int(max_chars)
        except (TypeError, ValueError):
            errors.append(f"{label}.max_chars:{max_chars!r} 不是一个数")
            max_chars = DEFAULT_TEXT_MAX_CHARS
        if errors:
            raise PluginError(errors)
        return Fact(plugin=plugin_id, key=key, bearer=bearer, shape="text",
                    default=0.0, visibility=visibility,
                    label=str(spec.get("label") or "").strip(),
                    text_default=str(spec.get("default") or ""),
                    max_chars=max_chars, namespaced=namespaced, mode=mode)
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
        namespaced=namespaced, mode=mode, sources=sources,
    )


def _parse_sources(
    label: str, spec: dict[str, Any], mode: str, errors: list[str],
    subscribable: Iterable[str] | None,
) -> tuple[dict[str, Any], ...]:
    """`sources`:把哪些既有的**内核**事件认成这个事实的 delta(裁决 ④)。

    🔴 **它和「插件伪造不了别家的投影」是同一道边界的两面。** 折叠端那道闸靠
    **同一性**关上了「谁发的 ≠ 改谁的」那条路(事件类型必须恰好是
    `<那个事实>.delta`);而 `sources` 是一张**作者写的**表 —— 它要是认得了
    `<别家>.<事实>.delta`,刚关上的那扇门就从这儿又开了,只是这次是**声明式**地开。
    所以这一格**只收 `SUBSCRIBABLE_EVENTS` 上那几种内核事件**:那张表上的每一条
    都是引擎自己发的,没有一条来自别的插件。
    """
    raw = spec.get("sources")
    if raw is None:
        return ()
    if mode != "projected":
        errors.append(
            f"{label}.sources:只有 `mode: \"projected\"` 的事实认得了别的事件 —— "
            "一个直接写的事实再认一条事件当自己的 delta,就是**两个写者写同一个数**,"
            "而两份真相里有一份不更新是这个仓库最怕的坏法"
        )
        return ()
    if not isinstance(raw, list) or not raw:
        errors.append(f"{label}.sources 必须是非空列表")
        return ()
    known = set(subscribable or ())
    out: list[dict[str, Any]] = []
    for position, entry in enumerate(raw):
        where = f"{label}.sources[{position}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} 必须是对象")
            continue
        # **和另外十层同一句话**(2026-08-27):从前这一层的报错不说该去问契约的
        # 哪一格,于是读它的人只能照文档记一份会烂的清单 —— 而"每层的名单都进契约"
        # 这条纪律的一半正是**让报错自己点名那一格**。
        unknown = unknown_keys(where, entry, PROJECTED_SOURCE_KEYS,
                               "projected_source_keys")
        if unknown:
            errors.extend(unknown)
            continue
        event = str(entry.get("event") or "").strip()
        if not event:
            errors.append(f"{where} 少了 event")
            continue
        if known and event not in known:
            errors.append(
                f"{where}.event:`{event}` 不是**内核**事件 —— 这一格只认引擎自己发的"
                f"那几种(`contract --json` 的 `plugins.subscribable_events`)。"
                "认别的插件的事件等于让一张作者写的表把「谁发的 ≠ 改谁的」那扇门"
                "重新打开,只是这次是声明式地开"
            )
            continue
        credit = str(entry.get("credit") or "").strip()
        debit = str(entry.get("debit") or "").strip()
        if not credit and not debit:
            errors.append(
                f"{where}:`credit` / `debit` 至少要有一头 —— 一条谁也不加谁也不减的"
                "声明,读的人会以为它在做什么"
            )
            continue
        owner_form = str(entry.get("owner_form") or "actor").strip()
        if owner_form not in OWNER_FORMS:
            errors.append(f"{where}.owner_form:只有 {list(OWNER_FORMS)}")
            continue
        try:
            digits = int(entry.get("round", DEFAULT_FACT_ROUND))
        except (TypeError, ValueError):
            errors.append(f"{where}.round:{entry.get('round')!r} 不是一个整数")
            continue
        if not 0 <= digits <= 12:
            errors.append(f"{where}.round:只收 0~12 位")
            continue
        out.append({"event": event,
                    "amount": str(entry.get("amount") or "amount").strip(),
                    "credit": credit, "debit": debit, "owner_form": owner_form,
                    "round": digits})
    return tuple(out)


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
    edges: Mapping[str, "EdgeType"] | None = None,
) -> Trigger:
    errors: list[str] = []
    edges = edges or {}
    if not isinstance(spec, dict):
        raise PluginError([f"{label} 必须是对象,收到 {type(spec).__name__}"])
    unknown = sorted(set(spec) - set(TRIGGER_KEYS))
    if unknown:
        errors.append(
            f"{label}:不认识的键 {unknown} —— 写下去会被**静默丢掉**;"
            f"收的是 {list(TRIGGER_KEYS)}(`plugins.trigger_keys`)"
        )
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
    # 🔴 **报错要印它真受理的那几个,不是 `BEARER_FORMS`**(2026-08-27 验收 A):
    # 那张表有六个词,而这一层只收四种 —— 上一版把 `actor` / `player` 一起印了出去,
    # 而它俩**正是这一层当场拒的**。一句点名了自己拒的取值的报错,会让作者照着它
    # 再写一遍,再被拒一次。
    if bearer not in TRIGGER_BEARER_NODES \
            and not bearer.startswith(TRIGGER_BEARER_PREFIXES):
        errors.append(f"{label}.for_each.node:不认识的 `{bearer}`;"
                      f"这一层收的是 {list(TRIGGER_BEARER_FORMS)}"
                      "(问 `contract --json` 的 `plugins.trigger_bearer_keys`)")

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
    links: list[dict[str, Any]] = []
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
                errors += unknown_keys(f"{where}.emit", body, TRIGGER_EMIT_KEYS,
                                       "trigger_emit_keys")
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
            elif kind in ("link", "unlink", "transfer"):
                spec_out = _parse_link_effect(plugin_id, where, kind, body,
                                              edges, errors)
                if spec_out is not None:
                    links.append(spec_out)
            else:
                errors.append(f"{where}:不认识的效果 `{kind}`;这一版有 {list(EFFECTS)}")

    # 触发器读得到:自己的事实 + 声明过的依赖 + `event.<数字格>` + 内核日历。
    reads: set[str] = set()
    for expression in conditions:
        reads |= expression.names
    for _name, expression in sets:
        reads |= expression.names
    errors.extend(_undeclared_reads(label, reads, plugin_id, allowed, event_ok=True))
    errors.extend(_bearer_mismatch_reads(label, reads, plugin_id, facts, bearer))

    if errors:
        raise PluginError(errors)
    return Trigger(plugin=plugin_id, id=trigger_id, event=event, bearer=bearer,
                   conditions=tuple(conditions), sets=tuple(sets), emits=tuple(emits),
                   links=tuple(links))


#: 边那三条效果里,`from` / `to` 认得出的几个词。**认不出就是一个光名字**
#: (当成节点 id 用),`Scheduler._resolve_node` 那一句"认不出就是空串,不猜"是同一条。
EDGE_EFFECT_NODES = ("self", "target", "spawned", "event.who")


def _parse_link_effect(
    plugin_id: str, where: str, kind: str, body: Any,
    edges: Mapping[str, "EdgeType"], errors: list[str],
) -> dict[str, Any] | None:
    """`link` / `unlink` / `transfer` 那三条的声明。**触发器和动词共用这一份。**

    共用是承重的:两份判断迟早分岔,而分岔的样子是"同一条 `link` 写在触发器里
    过得了闸、写在动词里过不了",作者读不出为什么。
    """
    if not isinstance(body, dict):
        errors.append(f"{where}.{kind} 必须是对象")
        return None
    errors += unknown_keys(f"{where}.{kind}", body, EDGE_EFFECT_KEYS,
                           "edge_effect_keys")
    edge_type = str(body.get("type") or "").strip()
    if not edge_type:
        errors.append(f"{where}.{kind} 少了 type")
        return None
    local = edge_type[len(plugin_id) + 1:] \
        if edge_type.startswith(f"{plugin_id}.") else edge_type
    if local not in edges:
        errors.append(
            f"{where}.{kind}.type:这个插件没声明过 `{local}` 这种边;"
            f"声明过的是 {sorted(edges)} —— **只连得动自己声明的边**,"
            "别的插件的边由它自己管"
        )
        return None
    return {"op": kind, "type": f"{plugin_id}.{local}",
            "from": body.get("from"), "to": body.get("to"),
            "by_dst": bool(body.get("by_dst")),
            "facts": dict(body.get("facts") or {})}


#: `for_each.node` 那个词 → 这一层的事实挂在谁身上时才读得到(`bearer`)。
#: ⚠️ `agent` 那一档收两个词:玩家和角色**同一个量表命名空间**。
_BEARER_FOR_NODE: dict[str, tuple[str, ...]] = {
    "agent": ("actor", "player"),
    "world": ("world",),
    "location": ("location",),
}


def _bearer_mismatch_reads(
    label: str, names: Iterable[str], plugin_id: str,
    facts: Mapping[str, "Fact"], node: str,
) -> list[str]:
    """读自己的事实,而那个事实**根本不在这条路够得着的那张量表上**(第三波 A1)。

    🔴 **从前这一族装载期全绿、运行期每一次都炸**:一个 `for_each: {"node": "agent"}`
    的触发器读自己挂在 `world` 上的事实,写 `wet.潮位` —— 那张量表是**她身上的**,
    世界那份在另一个 owner 上。下场是每来一条事件就一条 `ExpressionError`,
    触发器**一次不响**,而声明面完全正常。

    正确的写法是 `world_<插件>.<事实>`(和规律那一层读全局量逐字同一个写法)——
    所以这里不只是拒,**还要把该写的那串给他**。
    """
    want = _BEARER_FOR_NODE.get(node)
    if want is None:                   # `entity:<kind>` 那一支:目标种类归别处判
        want = (f"entity:{node[len('entity:'):]}",) if node.startswith("entity:") else ()
    out: list[str] = []
    for name in sorted(set(names)):
        bare = name[3:] if name.startswith("me_") else name
        prefixed = bare.startswith(WORLD_PREFIX)
        if prefixed:
            bare = bare[len(WORLD_PREFIX):]
        if not bare.startswith(f"{plugin_id}."):
            continue                   # 别人的事实归 `_undeclared_reads` 判
        fact = facts.get(bare[len(plugin_id) + 1:])
        if fact is None:
            continue                   # 没声明过,同样归上面那道闸
        if prefixed:
            if fact.bearer != "world":
                out.append(
                    f"{label}:`{name}` 前面那个 `world_` 是「读世界身上那个量」,"
                    f"而 `{bare}` 挂在 `{fact.bearer}` 上 —— 去掉 `world_` 直接写 "
                    f"`{bare}`"
                )
            continue
        if want and fact.bearer not in want:
            fix = (f"`{WORLD_PREFIX}{bare}`" if fact.bearer == "world"
                   else "把它挂到这条触发器够得着的那张量表上")
            out.append(
                f"{label}:读了 `{name}`,而它挂在 `{fact.bearer}` 上 —— 这条触发器"
                f"落在 `{node}` 头上,读的是**那张量表**,`{fact.bearer}` 那份在另一个"
                f"owner 上。**放行的下场是运行期每来一条事件炸一次、触发器一次不响,"
                f"而声明面完全正常**。要读世界身上那个量,写 {fix}"
            )
    return out


#: 表达式里**不必声明**就读得到的名字(内核的日历与流逝)。
_BUILTIN_READS = frozenset({"dt", "now", "day", "hour", "minute", "minute_of_day"})


def _undeclared_reads(
    label: str, names: Iterable[str], plugin_id: str, allowed: set[str],
    *, event_ok: bool = False, on_edge: bool = False,
) -> list[str]:
    """读了没声明的东西 —— **开机失败并点名**(插件系统那条读边界的落点)。

    `on_edge=True` 是 `for_each: {"edge": …}` 那一支:表达式里多三个前缀
    (`edge.` / `src.` / `dst.`)。**前缀剥掉之后判据一个字没变** —— 剥不掉的
    才是真的越界。另写一份判断的下场是两边迟早给出不同答案,而那种不一致
    表现成"边上的规律放行了一个节点上放不行的名字"。
    """
    out: list[str] = []
    for name in sorted(names):
        if on_edge:
            for prefix in ("edge.", "src.", "dst."):
                if name.startswith(prefix) and name not in allowed:
                    name = name[len(prefix):]
                    break
        bare = name[3:] if name.startswith("me_") else name
        # 🆕 **`world_` 也要剥**(3.8.0,2026-08-28 第三波 A1)。
        #
        # 读世界身上那个量的写法是 `world_<量名>`,而一个挂在 `world` 上的**插件
        # 事实**的量名本来就是 `<插件>.<事实>` —— 于是正确的写法是
        # `world_wet.潮位`。不剥的话,这道闸看到的是 `world_wet` 这个"插件 id",
        # 于是报「读了别的插件的 …,要读就写进 reads」;作者照着改,下一句就变成
        # 「这个世界里没有装 `world_wet` 这个插件」—— **一条把人引进死胡同的
        # 拒绝语,比没有拒绝语更贵。**
        if bare.startswith(WORLD_PREFIX) and bare not in allowed:
            bare = bare[len(WORLD_PREFIX):]
        if name in _BUILTIN_READS or bare in _BUILTIN_READS:
            continue
        if event_ok and bare.startswith("event."):
            continue
        if "." not in bare:
            # 光名字 = 内核的量(这个 bearer 自己身上那些)。插件读得到,
            # 但读不到别的插件的 —— 那一支下面判。
            continue
        if name in allowed or bare in allowed:
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
# 三件事共用一份"这个插件此刻装的是什么"的记录(`:plugins` 那个 hash)。
# 🔴 **那一行里存的就是声明原文**(`body`),外加几格摘出来的派生值 ——
# 和 `RedisRulesStore` 逐字同一个形制:定义存原文,编译在读取侧。
# **世界住在键前缀里,不住在世界文件里**:一个从 `--world-file` 建起来的世界,
# 下一次开机手上没有那份文件,而它的插件得照旧跑。
# ⚠️ 这段注释第一版写的是"记录里存的不是声明本身" —— **那句是错的**,而且和同一个
# commit 里 `_install_plugins` / REFERENCE / CHANGELOG 说的正相反(2026-08-26 验收 B:
# 「错的那句正好在读者最会去查行形状的地方」)。
# 摘出来那几格只为一件事:**裁剪只有它答得出**(上一版有而这一版没有的是哪几个)。


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
            if fact.bearer in ("actor", "player"):
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
            "edges": sorted(edge.qualified for edge in plugin.edges.values()),
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
    owners_of: Any, dry_run: bool = False, edge_store: Any = None,
    emit: Any = None,
) -> dict[str, Any]:
    """卸掉一个插件:它的事实、可见性行、记录。**它的规律与触发器随记录一起消失**
    (它们不落库 —— 权威是世界文件那份声明,库里只记"装的是哪一版")。

    ⚠️ **不删别的插件的东西**,一个字都不碰:命名空间就是这条边界的落点。
    ⚠️ **它抹不掉历史** —— 这个插件发过的事件留在日志里,和 `forget_player` 一条。
    日志是唯一的真相,而"这个世界曾经装过这个插件"是一件真的发生过的事。

    🔴 **而"不改历史"不等于"不留痕迹":卸载**自己**要发一条 `plugin.removed`。**
    第 1 期这条漏了,而漏的方式值得记一笔:REFERENCE 里用「历史一个字不动」把这句
    承诺换掉了 —— 那两句话看着像同一件事,其实差着方向:**追加一条事件不是改历史,
    它正是这个引擎记事情的唯一方式**。而别的插件要订它 —— **一个插件的卸载是另一个
    插件的输入**(它读的那个事实从此不存在了,它得知道)。
    `emit` 由调用方给(它认识事件日志,这一层不认识),不给就只是不发。
    """
    row = store.get(plugin_id)
    if row is None:
        return {"plugin": plugin_id, "found": False, "facts": 0, "edges": 0,
                "edge_types": [], "keys": [], "dry_run": bool(dry_run)}
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
        edge_types = sorted(row.get("edges") or ())
        cut = sum(len(edge_store.all(t)) for t in edge_types) if edge_store else 0
        return {"plugin": plugin_id, "found": True, "facts": count, "edges": cut,
                "edge_types": edge_types, "keys": keys, "dry_run": True}
    dropped = _prune_facts(plugin_id, keys, stock_store=stock_store,
                           visibility_store=visibility_store, owners_of=owners_of)
    # **它声明的边也一起走。** 留着的话世界里挂着一族没有人再解释得了的连线:
    # `plugin list` 里没有它,而 `state()` 里那几条边还在。
    cut = 0
    for edge_type in sorted(row.get("edges") or ()):
        if edge_store is not None:
            cut += int(edge_store.drop_type(edge_type) or 0)
    store.drop(plugin_id)
    if emit is not None:
        # 载荷里只有**这次卸掉了什么**,没有声明本身:订它的插件要的是"哪些事实
        # 从此不存在了",而不是一份它读不懂的别人的声明。
        emit({
            "type": "plugin.removed", "who": None,
            "payload": {"plugin": plugin_id,
                        "version": str(row.get("version") or ""),
                        "facts": keys, "edge_types": sorted(row.get("edges") or ()),
                        "dropped_facts": dropped, "dropped_edges": cut},
        })
    return {"plugin": plugin_id, "found": True, "facts": dropped, "edges": cut,
            "edge_types": sorted(row.get("edges") or ()),
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
