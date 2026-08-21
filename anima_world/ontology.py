"""世界的本体:**世界里能有什么东西**,作为可校验的声明。

引擎一直在把硬编码变成数据(提示词 → `prompt_templates`,行为树 → `bt_nodes`,
剧情 → `beats.json`,规律 → `world_rules`)。这一层补的是最底下那块:**"东西"本身**。

在这之前,一等实体只有角色和地点。别的一切(一棵树、一条船、一封信)只能偷渡成
量的 owner 前缀 —— `tree:oak_01` 这个 owner 挂着 size/growth_rate 两个 float,
而 `tree` 这个"种类"**不定义在任何地方**。于是:

    "for_each": {"kind": "tree"}      # 规律这么写
    stocks owner = "trees:oak_01"     # 世界里其实是这个

规律一条都不跑,`rule_stats()` 报 `evaluated: 0, skipped: 0`,零日志,世界照常转。
这是"照跑但给错东西"埋在最底层的那一种。

## 五条定死的设计,各自的代价与理由

1. **type / token 物理分成两张表。** 种类不是实例、实例不是种类,而且**在数据模型上
   就无法互相冒充**。Wikidata 用 P31(instance of)/ P279(subclass of)两个属性表达
   这一刀,靠作者自觉 —— 账单是 8400 万条 split-order pair、1760 万项被标成"class"
   的实例、以及自己是自己实例的循环。对策不是教育作者,是**让它不可表达**。

2. **量必须有 bearer。** 量是依附性的(形式本体论的 dependent continuant):它必须
   挂在一个存在的东西上。于是 owner 前缀从**命名约定**升级成**外键** —— 写一个 owner
   是 `trees:oak_01` 的量而 `trees` 没声明过,当场报错。这一刀几乎零成本、收益最大。

3. **两阶段加载,加载完冻结。** 先把全部声明读成符号表,再解析所有引用;未解析的
   引用当场抛错,并且**错误里带 (谁要的, 要什么类型, 要的名字)** 三元组(RimWorld 的
   `ResolveAllReferences` 就是这个形状,它的错误长这样:"Could not resolve
   cross-reference: No SoundDef named X found to give to ThingDef Y")。
   冻结是事件溯源逼的:运行期能注册新种类,重放就不再确定。

4. **不声明 `prompt` 就永不进提示词。** 和认知层"没声明 = 感知不到"完全同构 ——
   **声明本身就是开关**。这条同时是有界性的新判据,见下。

5. **继承只在加载期存在。** 单继承 + copy-down,展开完运行期看不见 `parent`。
   要的是模板复用(所有树共享量表,橡树多一个),不是类型层级 —— 深继承树在
   SNOMED 那边的下场是 IS-A 过载(把 role 塞进 kind 树),不值得。

## 有界性的判据变了

旧判据是"进得了提示词的 → 必须有界 → 住 Redis"。它有一个洞:**一万棵树住 Redis,
提示词照样炸**。存储位置保护不了提示词。而且引擎自己就在违反它 —— `memories` 住
MySQL 却天天进提示词,靠 top-k 有界。

新判据更强、把旧的包住,而且一样可验:

    每个能进提示词的类型,必须声明一个带上限的选择器。不声明 = 永不进提示词。

存储分层保留,但降级成**性能决策**,不再是提示词纪律。

## 明确没做的

不做推理机、不做 OWL / 开放世界(OWA 下"缺一个必填属性"不算错误只算"未知" ——
那正是这一层要抓的错)、不做 BFO/DOLCE 的完整上层树、不做 role/disposition/function
三分(BFO 内部常年扯皮,落到工程上没有判据)、不做 mereology 与 4D 时间片。

也**不收编角色与地点**:它们各有成熟实现(角色有 Brain / 黑板 / 行为树 / LLM 生命
周期,地点有嵌套几何),塞进通用表只会让通用表长出一堆只对某一类有意义的字段,而
收益是零。它们在这里登记成**内置只读种类**(`builtin=True`,元数据在别处),
共享一个命名空间 —— 这样引用校验和感知声明只有一条查找路径,不会长出第二份真相。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from anima_world.expressions import (
    Expression,
    ExpressionError,
    compile_expression,
    lower_bounds,
    reachable_ceiling,
    rewrite_source,
)
from anima_world.perception import HIDDEN, VISIBILITIES, band_word
from anima_world.rules import BUILTIN_NAMES, WORLD_PREFIX, bad_output_name
from anima_world.world_time import DEFAULT_MINUTES_PER_TICK, clock_names

logger = logging.getLogger(__name__)

# 引擎自己认识的种类:元数据在别处,数据里不许重新定义,但引用得到。
# `agent` / `location` 有自己的存储;`world` 是全局量那个 owner;`player` 是宿主的人。
BUILTIN_KINDS = ("agent", "location", "world", "player")

# ……但 `agent` 例外,而且**只在 `quantities` 这一格上例外**。
#
# 理由是能力(affordance)本来就不完整:Gibson 的 affordance 是**施动者与对象之间的
# 关系**,不是对象单方面的属性 —— 同一把斧头对有力气的人是"能砍",对没力气的人不是。
# 而在这之前,能力只读得到对象身上的量,于是这个世界里谁都一样能干:一个人可以连着
# 照料一百棵树而不累,没有任何东西挡得住。
#
# 挡住它需要"她身上也有量"。角色的元数据仍然不归这一层(她有 Brain / 黑板 / 行为树,
# 收编只会让通用表长出一堆只对角色有意义的字段),所以开的口子极窄:**只准声明量**,
# 不准声明 affordances / parent / prompt —— 她不是一样可以被 `tend` 的东西。
# 量本身住在 stock store 的 `agent:{id}` 下,和树的量同一个后端、同一套可见性。
DECLARABLE_BUILTINS = frozenset({"agent"})

# 表达式里读**施动者**身上的量的前缀,和 `world_` 同构:
#
#     "requires": ["me_体力 >= 10"]          # 她做不做得了
#     "costs":    {"体力": "me_体力 - 10"}    # 做完从她身上扣
#
# 为什么要前缀而不是直接把她的量拌进同一个命名空间:一棵树和一个人可以都有"高度",
# 而拌在一起时后写的那个赢 —— 静默地赢。
ME_PREFIX = "me_"

# 读**她随身带着几个某样东西**的前缀:
#
#     "requires": ["have_garden_shears >= 1"]   # 没剪子就修不了枝
#     "consumes": {"fertilizer": 1}             # 做完花掉一包肥
#
# 这一半补的是 Gibson 那个例子本身:"同一把斧头对有力气的人是能砍"里,**斧头**
# 此前根本没法出现在声明里 —— `requires` 只读得到量,而随身物品住在经济那一层的
# 库存里(事件日志的投影)。绕开的写法是给 `agent` 声明一个 `斧头: 0/1` 的量,
# 那等于把同一件事记在两个后端上,而两份真相里有一份不更新是这个仓库最怕的坏法。
#
# 读的是投影,所以这里只读不写;**花掉**东西走 `consumes` → `item_consume` 事件,
# 库存仍然只有事件日志一个来源。
HAVE_PREFIX = "have_"

# 她能对一个东西做什么。**这是默认词表,不是闭集** —— 作者可以自己声明动词。
#
# 它一度是闭集,理由写着"效果终归由引擎实现"(Dwarf Fortress 的 raws 是那个反例:
# 能给生物加 `[CARNIVOROUS]`,却造不出自定义食性)。那条理由在 `set`/`costs`/
# `consumes` 落地之后就**不成立了**:`apply_affordance` 从头到尾没有一处按动词分支,
# 效果整个是作者的数据。于是闭集买到的只剩两样 —— 拼错当场报错,和一张中文词表。
# 而这两样都不需要枚举:**声明过**就够了(和 `kinds` / perception 逐字同构)。
#
# 放开的是词,不是纪律:自造的动词照样要在 `kinds` 里声明一次,别处写错一个字
# 照样开不了机;英文动词照样必须给 `label`,因为她提示词里读到的是那几个字。
BUILTIN_AFFORDANCES = (
    "look",      # 看一眼(所有实体隐含都有)
    "use",       # 用它
    "take",      # 拿走
    "give",      # 给出去
    "tend",      # 照料(浇水、喂食、维护)
    "harvest",   # 收取产出
    "make",      # 制作/建造
    "damage",    # 破坏
    "read",      # 读(信、书、告示)
    "enter",     # 进去(船、房子)
)

# 实例 id 的形状:`kind:local`,namespaced string。
# 不用整数 id 是因为世界要打成 `.cyberworld` 分发、id 还要进提示词 ——
# 整数 id 必然引出 remap 表这道伤口(Minecraft 的 ResourceLocation 就是这个教训)。
_ENTITY_ID_RE = re.compile(r"^[^\s:]+:[^\s:]+$")
_KIND_ID_RE = re.compile(r"^[^\s:]+$")

# 内置能力的人话。她提示词里读到的是这些词,不是 `harvest` —— 英文动词在中文提示词里
# 是**噪音**,而她要照着它行动。自造的动词从 `label` 拿这一行;`label` 没写而动词
# 又是纯 ASCII 的,当场报错(见 `_affordance_label`)—— 那正是这张表要挡的东西。
BUILTIN_AFFORDANCE_LABELS = {
    "look": "端详",
    "use": "使用",
    "take": "拿走",
    "give": "送出",
    "tend": "照料",
    "harvest": "收取产出",
    "make": "制作",
    "damage": "毁坏",
    "read": "读",
    "enter": "进去",
}

# 自造动词只能长成这样:不含空白、不含冒号(冒号是实例 id 的分隔符)。
_VERB_RE = re.compile(r"^[^\s:]+$")
# 纯 ASCII 的动词进不了中文提示词 —— 见 `BUILTIN_AFFORDANCE_LABELS`。
_ASCII_RE = re.compile(r"^[\x00-\x7f]+$")

# 同一个地方东西太多时,最多带几个进提示词。作者没在种类上声明就用它 ——
# 有上限本身不是可选的(那是这一层的判据),可选的只是**几个**。
DEFAULT_HERE_BUDGET = 5

# 一个能力里认得的字段**全集**。校验器和 `contract --json` 读的是同一份 ——
# 抄成两份的话,新加一格时总会有一次只改了校验器:引擎收得下,而创作台问出来的
# 答案里没有它,两边都不报错。消费方的铁律是"问引擎、不读文档",那么被问的那一格
# 就必须**是**引擎判断时用的那一格,不是它的一份手抄本。
AFFORDANCE_KEYS = (
    "when", "set", "requires", "costs", "consumes", "label", "duration", "occupies",
    "spawn", "destroys_target", "participants", "importance",
)

# 一条 `kinds` 行认哪些字段(3.7.0 起进 `contract --json` 的 `seed.kind_keys`)。
#
# ⚠️ **这一格和上面那个 `AFFORDANCE_KEYS` 有一条要紧的差别,别读串了**:能力级
# 的不认识字段是**当场开不了机**,而**种类级的不认识字段今天被静默忽略**
# (`_parse_one_kind` 只挑它认得的几个键读)。所以这个元组是**"读得到的那几格"
# 的清单,不是一道闸** —— 它答的是「这一版引擎读不读得懂我写的这一格」,
# 不答「我写错了它会不会拦我」。收严那一条不在这一轮做:那会让写过额外键的
# 已发布世界当场开不了机,而这份清单本身就是为了让人**先看得见**才写下来的。
#
# `parent` 在这里,而 FOR-STUDIO §3.7 直到 2026-08-21 都没写过它 —— 于是创作台
# 不认这一格,对着一份完全合法的声明产出过一条**假红**。这正是"消费方问引擎、
# 不读文档"要有出口才成立的那一半。
KIND_KEYS = ("affordances", "gloss", "id", "parent", "prompt", "quantities")


class OntologyError(ValueError):
    """本体声明有毛病 —— 逐条列出,一次性报完。

    和 `RuleError` / `WorldSeedError` 同一条纪律:坏声明不许流到运行期。
    这一层尤其如此 —— 一个引用不到的种类不会让世界崩,只会让它**安静地少跑一半**。
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid world ontology:\n" + "\n".join(f"- {e}" for e in errors))


@dataclass(frozen=True)
class Quantity:
    """一个种类身上的量。**依附于 bearer**,所以它只能在种类声明里出现。"""

    key: str
    default: float = 0.0
    visibility: str = HIDDEN
    label: str = ""          # 进提示词时的人话名字;空则用 key
    unit: str = ""
    # `((0.0, "毛毛雨"), (0.4, "大雨"), …)`:她读到的不是 0.8,是"大雨"。
    # **可见性是量声明的一部分,分档同理** —— 写在同一处,不必再去
    # `stock_visibility` 写第二行。空 = 没分档 = 照旧给数字(逐位不变)。
    bands: tuple[tuple[float, str], ...] = ()

    def render_label(self) -> str:
        return self.label or self.key


@dataclass(frozen=True)
class PromptSpec:
    """这个种类进提示词时的**硬上限**:同一个地方最多带几个。

    只有 `budget` 一个字段是有意的。曾经还有个 `select`(here/self/public),但那
    是**第二份真相** —— 她看不看得见由量自己的可见档决定,而一个种类的不同量可以
    分属不同档,种类级的 `select` 只会和它们打架。剩下的就是这一层唯一新增的东西:
    一个上限。

    上限是硬的,不是提示词里的一句请求 —— autonomy 那次踩过反过来做的坑:
    用提示词限流,结果 18 轮 0 动作。
    """

    budget: int = DEFAULT_HERE_BUDGET


@dataclass(frozen=True)
class SpawnSpec:
    """做完这件事,世界里多出一个新东西。

    **它必须要代价。** 这是这一格唯一的硬约束(`_parse_affordance` 里那道闸):一个
    不要代价的生成,作者写下去的第二天世界里就有一百万棵树 —— 而挡住它的正确办法
    不是引擎给个配额。配额是**引擎的天花板**:撞上去时她收到的拒绝在世界里没有意义,
    "这个世界最多一百棵树"不是她能理解、能应对的东西,她也永远学不会。代价是**世界
    的理由**:她知道自己为什么做不到,也知道要做到得先补什么(`incapable`)。

    三种代价都算数(`costs` / `consumes` / `duration`),但**只有时间那种封得住** ——
    量能睡回来、材料能买回来,而一段时间过不去就是过不去。十月怀胎拦得住不是因为它贵。

    ⚠️ 而**代价只封得住速率,封不住存量**:体力天天回满的世界里,一百天就是一百个
    孩子。真实世界靠的是生灭成对,所以 `destroys_target` 和这一格是同一轮加的 ——
    只有生的引擎会让每个世界最后都挤爆,而且漏得很慢、很安静。
    """

    kind: str
    name: str = ""
    gloss: str = ""
    # 生在哪儿。空 = **这件事发生的那个地方**(她和那个东西所在处),
    # 否则是一个地点 id。给个默认而不是必填,是因为绝大多数出生就在当场。
    location: str = ""
    # 覆盖种类默认值的那几个量;没写的照种类声明落地。
    quantities: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ParticipantSpec:
    """这件事**得有人一起做**。

    `interact` 一直是单人的 —— `Affordance` 的定义原话就是"一个人、一个东西、
    一个瞬间"。于是两个站在同一个地方的人只能各干各的,而世界记不下"他们一起
    吃了顿饭"。这一格补的就是那件事。

    只有两个数,而且**没有 `consent` 开关**:同意永远是必须的。给作者一个
    `consent: false` 等于把"别人凭什么答应"整个交回去关掉,而"拉着谁就一起吃饭"
    正是这一层要挡的东西 —— 一个取消对方意志的能力,比没有这个能力坏得多。
    理由的全文在 `together.py` 的模块说明里。

    `maximum` 有个上限(`MAX_PARTICIPANTS`),理由是账面上的:一场共同经历要为
    **每一对有序对**发一条关系事件,人数是平方进去的。上限不是配额哲学那一套
    (代价才是世界的理由),是事件日志的有界性 —— 和"每个能进提示词的类型必须
    声明一个带上限的选择器"同一条。
    """

    # 除了发起的那个人之外,还要几个。**至少 1** —— 写 0 的话这件事根本不是
    # "一起",而作者写下 `participants` 时想的一定不是那个意思。
    minimum: int = 1
    maximum: int = 1

    def accepts(self, count: int) -> bool:
        return self.minimum <= int(count) <= self.maximum


# 一场共同经历最多几个人一起。`n(n-1)` 条关系事件,8 个人 = 56 条 —— 这已经是
# 一条事件日志该为"一顿饭"付的上限。
MAX_PARTICIPANTS = 8


@dataclass(frozen=True)
class Affordance:
    """她能对这个东西做的一件事,以及**做完之后世界怎么变**。

    效果(`when` / `set`)是可选的:`look` 通常什么也不改,声明它只是让"端详"这个词
    进提示词。但**可选的是有没有效果,不是有没有实现** —— 闭集里的每个动词她都真的
    调得动,调不动的词根本进不了闭集。

    为什么效果落在这里,而不是写成一条 `for_each: {"action": "tend"}` 的规律:
    规律那一层**有意拒绝**跨实体写(`rules.bad_output_name` 的长注释),理由是双缓冲
    下扇入没有意义 —— 一条作用在一百棵树上的规律,每棵读到的全局量都是同一个旧值,
    "每棵 +1" 会算成 +1。而一次能力调用是**一个人、一个东西、一个瞬间**:没有集合,
    就没有扇入,那条理由整个不适用。所以它不是在规律层开后门,是另一种东西。
    """

    verb: str
    # 她提示词里读到的那几个字。内置动词有默认,自造的要作者给。
    label: str = ""
    conditions: tuple[Expression, ...] = ()
    outputs: Mapping[str, Expression] = field(default_factory=dict)
    # 关于**她**的那一半。`requires` 只读得到 `me_*` 与 `have_*`,这是有意的硬约束:
    # 一条 requires 不成立永远只有一个意思 ——「你做不了」。它要是也能读树身上的量,
    # 就和 `when` 分不开了,而**分得开正是它存在的全部理由**(见 `refusal_reason`)。
    requires: tuple[Expression, ...] = ()
    costs: Mapping[str, Expression] = field(default_factory=dict)
    # 做完花掉的东西(item_id → 几个)。**自带一道"你得有"的门**,不必再写一遍
    # `requires: ["have_x >= 1"]`:要花掉一样自己没有的东西,唯一讲得通的意思就是
    # 做不了。写两遍则给了只写一遍的机会,而只写 `consumes` 的那个世界里,她会用
    # 一包不存在的肥料把活干完 —— 库存扣不到负数,于是连账上都看不出来。
    consumes: Mapping[str, int] = field(default_factory=dict)
    # 做这件事要多少个 tick。**0 = 一下子的事**(默认,老样子)。
    #
    # 时间是这个引擎此前完全说不出的那种代价。`costs` 扣的是量、`consumes` 扣的是
    # 东西,两样都能靠"过一天就回来"绕开;而**一段时间过不去就是过不去**。十月怀胎
    # 之所以拦得住,不是因为它贵,是因为它长。所以生成新实体必须挂在这上面,
    # 挂在 `costs` 上的话,体力回满的那天她就能再生一个。
    duration: int = 0
    # 这段时间占不占用她。**做椅子占用,怀胎不占用** —— 两者都是长过程,而
    # "这期间她还能不能干别的"才是代价的真实形状。占用的那种一次只能有一件。
    occupies: bool = True
    # 做完之后世界里多出一个东西 / 少掉这个东西。
    #
    # 生和灭是**同一轮**加的,不是两件事:代价只封得住"多久生一个",封不住"世界里
    # 一共有多少个"。体力天天回满的世界里,一百天就是一百个孩子 —— 速率有界、存量
    # 无界。真实世界不是靠出生的代价封的,是靠会生的东西都会死。
    spawn: SpawnSpec | None = None
    destroys_target: bool = False
    # 这件事得有人一起做。`None` = 单人的老样子(**默认**,所以已有的世界逐位不变)。
    participants: ParticipantSpec | None = None
    # 在场的人**该多把这件事记在心上**。`None` = 不写 = 这一层整个缺席,
    # 行为与从前逐位相同(和 `kinds` / perception / 规律的 `emit.importance`
    # 逐字同构:**声明本身就是开关**,所以这里没有默认值)。
    #
    # 为什么不设默认:给它一个 0.3 之类的缺省,等于替每个作者宣布"世界上任何一次
    # 交互都值得被人记一辈子" —— 于是记忆里塞满了谁又端详了一次杯子,而真正要紧
    # 的那几件淹在里面。记什么由写这个世界的人挑,引擎不替他挑。
    importance: float | None = None

    @property
    def is_joint(self) -> bool:
        """一起做的事。为真时 `perform_affordance` 要一份名单,而且每个人各自
        过闸、各自付代价、各自答应。"""
        return self.participants is not None

    @property
    def is_process(self) -> bool:
        """要花时间的事。为真时 `apply_affordance` 的结果**不当场落库**。"""
        return self.duration > 0

    @property
    def has_price(self) -> bool:
        """这件事要不要她付出点什么。生灭那一格的闸问的就是这一句。"""
        return bool(self.costs) or bool(self.consumes) or self.duration > 0

    @property
    def changes_world(self) -> bool:
        return (
            bool(self.outputs) or bool(self.costs) or bool(self.consumes)
            or self.spawn is not None or self.destroys_target
        )

    @property
    def needs_actor(self) -> bool:
        """这个能力看不看施动者。不看的话,谁来做都一样 —— 老样子。"""
        return bool(self.requires) or bool(self.costs) or bool(self.consumes)

    @property
    def item_refs(self) -> tuple[str, ...]:
        """这条能力提到了哪些东西的 id —— `resolve` 拿它去查这些东西存不存在。"""
        refs = set(self.consumes)
        for expression in (*self.conditions, *self.outputs.values(),
                           *self.requires, *self.costs.values()):
            for name in expression.names:
                if name.startswith(HAVE_PREFIX):
                    refs.add(name[len(HAVE_PREFIX):])
        return tuple(sorted(refs))


@dataclass(frozen=True)
class Kind:
    """一个种类的声明。这就是本体仓库本身。"""

    id: str
    gloss: str = ""                    # 一行人话。**进提示词的是它,不是属性表**
    quantities: Mapping[str, Quantity] = field(default_factory=dict)
    affordances: Mapping[str, Affordance] = field(default_factory=dict)
    prompt: PromptSpec | None = None
    builtin: bool = False

    def quantity_names(self) -> frozenset[str]:
        return frozenset(self.quantities)


@dataclass(frozen=True)
class Entity:
    """一个实例。**只有身份与元数据** —— 它的量住在 stock store 里。

    这就是 Type Object 那一刀的落点:随实例变的进这里,不变的留在 `Kind` 里只存
    一份。提示词里实例只引用种类名,不重复种类的描述 —— 有界性就是这么买到的。
    """

    id: str
    kind: str
    name: str
    gloss: str = ""                    # 这一个的补充描述;空则用种类的
    location: str | None = None

    @property
    def local_id(self) -> str:
        return self.id.split(":", 1)[1]


@dataclass(frozen=True)
class Ontology:
    """加载完、解析完、冻结之后的本体。运行期只读。"""

    kinds: Mapping[str, Kind] = field(default_factory=dict)
    entities: Mapping[str, Entity] = field(default_factory=dict)

    def add_entity(self, entity: Entity) -> None:
        """运行期多出一个东西。**只有实例这一半能动,种类那一半仍然是冻的。**

        这条不对称不是省事:规律是按种类校验的(`resolve`),运行期新增一个种类
        等于让"这条规律合不合法"随时间变化,重放就不再确定。而种一棵树只是多一个
        owner —— 规律早就写好了,量的默认值也早就声明过。
        """
        self.entities[entity.id] = entity          # type: ignore[index]

    def drop_entity(self, entity_id: str) -> None:
        self.entities.pop(entity_id, None)         # type: ignore[union-attr]

    def kind_of(self, entity_id: str) -> Kind | None:
        entity = self.entities.get(entity_id)
        return self.kinds.get(entity.kind) if entity else None

    def entities_of(self, kind: str) -> list[Entity]:
        return sorted(
            (e for e in self.entities.values() if e.kind == kind), key=lambda e: e.id
        )

    def declared_quantities(self, owner: str) -> frozenset[str]:
        """这个 owner 身上**声明过**哪些量。用于把 `missing_names` 从建议升级成闸。"""
        kind_id = owner_kind(owner)
        kind = self.kinds.get(kind_id)
        return kind.quantity_names() if kind else frozenset()

    def budget_of(self, owner: str) -> int:
        """这个 owner 所属的种类,同地最多带几个进提示词。**永远有个上限。**

        没声明的种类(以及完全没有本体的世界)回落到 `DEFAULT_HERE_BUDGET` ——
        有上限不是可选项:一万棵树住哪儿都一样炸提示词,存储分层保护不了它。
        """
        kind = self.kinds.get(owner_kind(owner))
        return kind.prompt.budget if kind and kind.prompt else DEFAULT_HERE_BUDGET

    def affordance_of(self, owner: str, verb: str) -> Affordance | None:
        """这个东西认不认这个动词。认不得返回 `None` —— 由调用方报成拒绝。

        **人话也认。** 她提示词里读到的是"照料"而不是 `tend`,于是她照着说出来的
        也是"照料" —— 只认 id 的话,引擎会回她一句"不认识这个动词",而那几个字
        正是引擎自己写给她的。从前这靠 `tools/body.py` 里一张全局的反查表,动词
        放开之后那张表就不够了(自造动词的人话住在声明里,不住在引擎里),而且
        它按世界范围反查:两个种类各有一个"照料"时,反查表只留得下一个。
        """
        entity = self.entities.get(owner)
        kind = self.kinds.get(entity.kind if entity else owner_kind(owner))
        if kind is None:
            return None
        found = kind.affordances.get(verb)
        if found is not None:
            return found
        # `describe()` 给一起做的事加了一句"(得有人一起)",而她照着提示词说话
        # 时会把那几个字**一起**说出来。引擎自己写下的注解,不该由她来负责剥掉 ——
        # 不剥的话下场是"不认识这个动词",而那个词就是引擎印给她的。
        bare = verb.split("(", 1)[0].split("(", 1)[0].strip() if verb else verb
        for affordance in kind.affordances.values():
            if affordance.label == verb or (bare and affordance.label == bare):
                return affordance
        return None

    def units_of(self, owner: str) -> dict[str, str]:
        """`{"树高": "米"}`。没有单位的量不出现。

        单位进提示词是有理由的:一个光秃秃的 `树高 3.2` 会被她自己补上一个单位,
        而她补的那个不一定是作者想的那个。
        """
        kind = self.kinds.get(owner_kind(owner))
        if kind is None:
            return {}
        return {q.key: q.unit for q in kind.quantities.values() if q.unit}

    def describe(self, owner: str) -> tuple[str, list[str]]:
        """这个 owner 的**一行人话**和**她能对它做什么**。

        进提示词的是这两样,不是属性表 —— 一串 `树高 3.2` 她只会念出来,而
        "你可以照料它"才是她下一步真能做的事。能力用中文,因为 `harvest` 在中文
        提示词里是噪音。
        """
        entity = self.entities.get(owner)
        kind = self.kinds.get(entity.kind if entity else owner_kind(owner))
        if kind is None:
            return ("", [])
        gloss = (entity.gloss if entity and entity.gloss else kind.gloss) or ""
        # 人话取自能力自己的 `label`(内置有默认,自造的由作者给)——
        # 从前这里查一张引擎的表,于是作者造得出的动词这里一个字也读不到。
        #
        # **一起做的事要在这里就说出来。** 不说的话她会一个人去试,每次都收到
        # 一句"这件事得有人一起做" —— 而提示词里那几个字正是引擎自己写给她的。
        # 一个只在调用之后才说得出前提的能力,和声明了却没人兑现是同一种坏。
        verbs = [
            f"{a.label or a.verb}(得有人一起)" if a.is_joint else (a.label or a.verb)
            for a in kind.affordances.values()
        ]
        return (gloss, verbs)

    def is_declared_owner(self, owner: str) -> bool:
        """这个 owner 指向一个存在的东西吗 —— 量的外键校验就是问这一句。"""
        if owner in self.entities:
            return True
        kind_id = owner_kind(owner)
        kind = self.kinds.get(kind_id)
        # 内置种类的实例住在别处(角色在投影里、地点在 locations 表),
        # 这一层认得它们的命名空间,但不持有它们的名单。
        return bool(kind and kind.builtin)


def owner_kind(owner: str) -> str:
    """`tree:oak_01` → `tree`;`world` → `world`。和 `stocks.owner_kind` 同一规则。"""
    return owner.split(":", 1)[0] if ":" in owner else owner


# ── 加载:第一阶段(读声明,建符号表) ─────────────────────────────────────────


def parse_kinds(entries: Any) -> dict[str, Kind]:
    """把 JSON 里的种类声明编译成符号表。**任何一条坏了就整体拒绝。**

    和 `parse_rules` 同一条理由:本体是这个世界"有什么"的定义,少一条不是"少一点
    内容",是这个世界从此有一部分东西静默地不存在。宁可开不了机。
    """
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise OntologyError([f"kinds 必须是一个列表,收到 {type(entries).__name__}"])

    errors: list[str] = []
    raw_by_id: dict[str, dict[str, Any]] = {}
    parents: dict[str, str] = {}

    for index, entry in enumerate(entries):
        label = f"kinds[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{label} 必须是对象,收到 {type(entry).__name__}")
            continue
        kind_id = str(entry.get("id") or "").strip()
        if not kind_id:
            errors.append(f"{label} 少了 id")
            continue
        label = f"kinds[{index}] ({kind_id})"
        if not _KIND_ID_RE.match(kind_id):
            errors.append(f"{label}:种类 id 不能含空白或冒号 —— 冒号是实例 id 的分隔符")
            continue
        if kind_id in BUILTIN_KINDS and kind_id not in DECLARABLE_BUILTINS:
            errors.append(
                f"{label}:{kind_id!r} 是引擎内置种类,不能在数据里重新定义"
                f"(它的元数据在别处;引用它是可以的)"
            )
            continue
        if kind_id in DECLARABLE_BUILTINS:
            extra = sorted(set(entry) - {"id", "quantities"})
            if extra:
                errors.append(
                    f"{label}:内置种类 {kind_id!r} 只能声明 quantities,不能声明 {extra} —— "
                    f"她不是一样可以被 tend 的东西,她的能力在行为树和聊天工具里。"
                    f"这里加的是**她身上的量**,好让能力看得见施动者"
                )
                continue
        if kind_id in raw_by_id:
            errors.append(f"{label}:id 重复了")
            continue
        raw_by_id[kind_id] = entry
        parent = entry.get("parent")
        if parent is not None:
            parents[kind_id] = str(parent).strip()

    # 继承:先查环和悬空父引用,再 copy-down 展开。展开完 `parent` 就消失了。
    for child, parent in parents.items():
        if parent not in raw_by_id or parent in BUILTIN_KINDS:
            errors.append(
                f"kinds ({child}):parent 引用不到 —— 没有名叫 {parent!r} 的种类"
                + (f"({parent!r} 是内置种类,不能当父类)" if parent in BUILTIN_KINDS else "")
            )
    for kind_id in raw_by_id:
        seen: list[str] = []
        cursor: str | None = kind_id
        while cursor is not None and cursor in parents:
            if cursor in seen:
                errors.append(f"kinds ({kind_id}):继承成环 —— {' → '.join(seen + [cursor])}")
                break
            seen.append(cursor)
            cursor = parents.get(cursor)

    if errors:
        raise OntologyError(errors)

    kinds: dict[str, Kind] = {}

    # `agent` 先解析:别的种类里的 `me_*` 要拿它的量表去查名字。**校验顺序不该等于
    # 书写顺序** —— 否则一条写在 agent 声明前面的 `me_力气` 会查不到而被拒绝,
    # 而它其实完全正确,作者只会看见一条自相矛盾的报错。
    actor_quantities: dict[str, Quantity] = {}
    if "agent" in raw_by_id:
        try:
            actor_quantities = dict(
                _parse_one_kind("agent", raw_by_id["agent"], None, {}).quantities
            )
        except OntologyError as exc:
            errors.extend(exc.errors)

    for kind_id in _inheritance_order(raw_by_id, parents):
        if kind_id in DECLARABLE_BUILTINS:
            continue
        try:
            kinds[kind_id] = _parse_one_kind(
                kind_id, raw_by_id[kind_id],
                kinds.get(parents.get(kind_id, "")), actor_quantities,
            )
        except OntologyError as exc:
            errors.extend(exc.errors)

    if errors:
        raise OntologyError(errors)

    for kind_id in BUILTIN_KINDS:
        kinds[kind_id] = Kind(
            id=kind_id, gloss="", builtin=True,
            quantities=actor_quantities if kind_id == "agent" else {},
        )
    return kinds


def unreachable_requirements(
    kinds: Mapping[str, Kind], rules: Iterable[Any] = (),
) -> list[str]:
    """**永远开不了的那道门** —— 门槛比这个量在这个世界里够得到的最高点还高。

    线上真有一条:`poster.撕下来` 要 `me_主动 >= 1.2`,而「主动」的默认值是 1.0,
    整个世界里唯一写它的表达式是 `max(me_主动 - 0.02, 0)` —— **只减不增**。那个
    按钮从这个世界开机第一秒起就永远不会亮,而玩家看得见它、点得到它、每次收到的
    都是「你的主动不够」。他会一直试。

    这比拼错量名更坏。拼错至少还有一道闸(当场开不了机),而这个连一句话都没有:
    世界照跑、日志干净、按钮排版正常。它是这个仓库最怕的那一类 ——「照跑但给错
    东西」的玩家版,而且**预防不了的失败教不会他任何东西**(`perform_affordance`
    那条纪律的原话,只是那儿说的是她)。

    为什么是**警告不是拒绝**:这个量可能被引擎之外的东西写(宿主直接调
    `stock_set`、节拍脚本、创作台的一次编辑),而那些这一层看不见。判据因此收得
    很紧,和 `rules.drift_warnings` 同一条纪律 —— `reachable_ceiling` 认不出的
    写法一律算「不知道」,不知道就不报。误报够多次的警告等于没有警告。

    只管 `me_*`:`have_*` 是随身物品(想买就买得到,没有"够不着"这回事),
    别的名字 `requires` 本来就不让读。
    """
    actor = kinds.get("agent")
    if actor is None:
        return []

    # 谁写得动她身上的量:所有种类的所有能力(`costs` 与 `set`)+ 所有规律的 `set`。
    # **少数一处就会多报一条**,而多报的那条指着一个其实开得了的门 —— 这道 lint
    # 的可信度全押在这张清单是全的。
    #
    # ⚠️ **两边读自己的写法不一样**:能力里是 `me_主动`(她身上的量,和对象的量要
    # 分得开),规律里是光名字 `主动`(那一层的 owner 就是她)。搞混的下场不是报错,
    # 是 `reachable_ceiling` 在语法树上找不到那个名字、答一个 `inf`,于是**每一条
    # 靠规律涨回来的量都被算成够不到** —— 而这道 lint 只会因此漏报吗?不会:
    # `max(嗓子 - …, 0)` 那种会连底也算成 inf,而 `min(主动 + …, 2)` 那种照样得 2,
    # 于是同一个量在两条规律下给出两个互相矛盾的上界。所以名字跟着来源走。
    writers: list[tuple[str, str, Expression]] = []
    for kind in kinds.values():
        for affordance in kind.affordances.values():
            for table in (affordance.costs, affordance.outputs):
                for quantity, expression in table.items():
                    writers.append((quantity, ME_PREFIX + quantity, expression))
    for rule in rules:
        for quantity, expression in (getattr(rule, "outputs", None) or {}).items():
            writers.append((quantity, quantity, expression))

    ceilings: dict[str, float] = {}
    written: set[str] = {quantity for quantity, _, _ in writers}
    for name, quantity in actor.quantities.items():
        ceiling = quantity.default
        for target, reads_as, expression in writers:
            if target == name:
                ceiling = max(ceiling, reachable_ceiling(expression, reads_as, quantity.default))
        ceilings[name] = ceiling

    messages: list[str] = []
    for kind in sorted(kinds.values(), key=lambda k: k.id):
        for verb, affordance in sorted(kind.affordances.items()):
            for expression in affordance.requires:
                for name, need in lower_bounds(expression).items():
                    if not name.startswith(ME_PREFIX):
                        continue
                    bare = name[len(ME_PREFIX):]
                    if bare not in ceilings or need <= ceilings[bare]:
                        continue
                    messages.append(
                        f"{kind.id}.{verb}.requires:`{expression}` 要「{bare}」至少 "
                        f"{need:g},而这个世界里它最高只到 {ceilings[bare]:g}"
                        f"(默认 {actor.quantities[bare].default:g},"
                        + ("写它的每一处都抬不高它" if bare in written else "没有任何一处写它")
                        + ")—— **这道门永远开不了**。玩家看得见这个按钮、点得到、"
                        "每次都被同一句话挡回来,而他做什么都不可能够。"
                        f"要么把门槛降到 {ceilings[bare]:g} 以内,"
                        f"要么给「{bare}」一条涨上去的路。"
                    )
    return messages


def _inheritance_order(raw: Mapping[str, Any], parents: Mapping[str, str]) -> list[str]:
    """父类先于子类 —— copy-down 要求的顺序。环已经在上一步拒掉了。"""
    ordered: list[str] = []
    placed: set[str] = set()

    def place(kind_id: str) -> None:
        if kind_id in placed or kind_id not in raw:
            return
        parent = parents.get(kind_id)
        if parent:
            place(parent)
        placed.add(kind_id)
        ordered.append(kind_id)

    for kind_id in raw:
        place(kind_id)
    return ordered


def _parse_one_kind(
    kind_id: str,
    entry: dict[str, Any],
    parent: Kind | None,
    actor_quantities: Mapping[str, Quantity],
) -> Kind:
    label = f"kinds ({kind_id})"
    errors: list[str] = []

    # copy-down:父类的量表和能力先铺进来,子类的同名声明覆盖它。
    quantities: dict[str, Quantity] = dict(parent.quantities) if parent else {}
    affordances: dict[str, Affordance] = dict(parent.affordances) if parent else {}

    raw_quantities = entry.get("quantities")
    if raw_quantities is not None:
        if not isinstance(raw_quantities, dict):
            errors.append(f"{label}:quantities 必须是「量名 → 声明」的对象")
        else:
            for key, spec in raw_quantities.items():
                name = str(key).strip()
                problem = _bad_quantity_name(name)
                if problem:
                    errors.append(f"{label}.quantities.{name}:{problem}")
                    continue
                quantity, quantity_errors = _parse_quantity(label, name, spec)
                errors.extend(quantity_errors)
                if quantity is not None:
                    quantities[name] = quantity

    raw_affordances = entry.get("affordances")
    if raw_affordances is not None:
        # 两种写法:`["look", "tend"]` 只声明动词(做了世界不变),
        # `{"tend": {"set": {...}}}` 连效果一起声明。列表形式是后者的退化,
        # 不是"旧格式" —— 一个只想让她端详的东西没有效果可写。
        if isinstance(raw_affordances, list):
            raw_affordances = {str(v).strip(): {} for v in raw_affordances}
        if not isinstance(raw_affordances, dict):
            errors.append(f"{label}:affordances 必须是列表,或「动词 → 效果」的对象")
        else:
            for value, spec in raw_affordances.items():
                name = str(value).strip()
                if not _VERB_RE.match(name):
                    errors.append(
                        f"{label}:动词 {name!r} 的形状不对 —— 不能是空的、不能带空白、"
                        f"不能带冒号(冒号是实例 id 的分隔符)"
                    )
                    continue
                affordance, affordance_errors = _parse_affordance(
                    label, name, spec, quantities, actor_quantities
                )
                errors.extend(affordance_errors)
                if affordance is not None:
                    affordances[name] = affordance

    prompt = parent.prompt if parent else None
    raw_prompt = entry.get("prompt")
    if raw_prompt is not None:
        prompt, prompt_errors = _parse_prompt(label, raw_prompt)
        errors.extend(prompt_errors)

    gloss = str(entry.get("gloss") or (parent.gloss if parent else "")).strip()
    if prompt is not None and not gloss:
        errors.append(
            f"{label}:声明了 prompt 就必须有 gloss —— 进提示词的是那一行人话,"
            f"不是属性表(贴一张属性表只会被念不会被用)"
        )

    if errors:
        raise OntologyError(errors)

    return Kind(
        id=kind_id,
        gloss=gloss,
        quantities=quantities,
        affordances=affordances,
        prompt=prompt,
    )


def _parse_affordance(
    label: str,
    verb: str,
    spec: Any,
    quantities: Mapping[str, Quantity],
    actor_quantities: Mapping[str, Quantity] = {},
) -> tuple[Affordance | None, list[str]]:
    """编译一个能力的效果。**写得到的量必须是声明过的** —— 两边都是。

    这道闸和规律那边同源:一个写到没声明的量上的能力,会凭空造出一个没人知道、
    也没有可见性声明的量 —— 她照料了一棵树,树上多出个谁也看不见的新属性,而且
    不报错。读也一样查:读到没声明的量恒为 0,于是 `when` 永远是同一个答案。

    施动者那一半(`requires` / `costs` / 表达式里的 `me_*`)走同一条闸,查的是
    `agent` 种类声明过的量。**一个查不到的 `me_力气` 恒为 0**,于是"力气够不够"
    这道门要么永远开着、要么永远关着 —— 正是这一层存在的那一类坏法。
    """
    errors: list[str] = []
    if spec is None:
        spec = {}
    if not isinstance(spec, dict):
        return (None, [f"{label}.affordances.{verb}:效果必须是对象,收到 {type(spec).__name__}"])
    unknown = set(spec) - set(AFFORDANCE_KEYS)
    if unknown:
        errors.append(
            f"{label}.affordances.{verb}:不认识的字段 {sorted(unknown)} —— "
            f"只认 when / set(关于它)、requires / costs / consumes(关于她)、"
            f"duration / occupies(关于时间)、spawn / destroys_target(关于生灭)、"
            f"participants(关于跟谁一起)、importance(关于在场的人记不记得住)"
            f"与 label(关于她怎么读它)。"
            f"门槛事件归规律那一层(能力只管这一下改了什么)"
        )

    importance = _parse_affordance_importance(label, verb, spec, errors)

    verb_label, label_errors = _affordance_label(label, verb, spec.get("label"))
    errors.extend(label_errors)

    # ── 时间 ──────────────────────────────────────────────────────────────
    duration = 0
    duration_ok = True
    raw_duration = spec.get("duration", 0)
    # 和 `consumes` 同一条:**只收整数**。tick 是可数的,而"做 2.5 个 tick"要么
    # 悄悄取整、要么引出一套半 tick 的语义,两条路都比不许坏。
    if isinstance(raw_duration, bool) or not isinstance(raw_duration, int):
        errors.append(
            f"{label}.affordances.{verb}.duration:必须是非负整数(单位 tick),"
            f"收到 {raw_duration!r}"
        )
        duration_ok = False
    elif raw_duration < 0:
        errors.append(
            f"{label}.affordances.{verb}.duration:必须是非负整数,收到 {raw_duration} —— "
            f"0 是「一下子的事」,没有比它更快的"
        )
        duration_ok = False
    else:
        duration = int(raw_duration)

    occupies = True
    raw_occupies = spec.get("occupies")
    if raw_occupies is not None:
        if not isinstance(raw_occupies, bool):
            errors.append(
                f"{label}.affordances.{verb}.occupies:必须是 true / false,"
                f"收到 {raw_occupies!r}"
            )
        elif duration_ok and duration <= 0:
            # 声明了却什么也不改 = 一句谎。作者写下 `occupies` 时想的是"这段时间
            # 她在忙",而没有 duration 就没有那段时间 —— 让它静默无效的话,他会
            # 以为自己已经表达过了。
            errors.append(
                f"{label}.affordances.{verb}.occupies:只在 duration > 0 时有意义 —— "
                f"一下子的事没有「这期间」可占用"
            )
        else:
            occupies = bool(raw_occupies)

    conditions: list[Expression] = []
    raw_when = spec.get("when") or []
    if not isinstance(raw_when, list):
        errors.append(f"{label}.affordances.{verb}:when 必须是表达式列表")
        raw_when = []
    for position, source in enumerate(raw_when):
        try:
            conditions.append(compile_expression(source))
        except ExpressionError as exc:
            errors.append(f"{label}.affordances.{verb}.when[{position}]:{exc}")

    outputs: dict[str, Expression] = {}
    raw_set = spec.get("set") or {}
    if not isinstance(raw_set, dict):
        errors.append(f"{label}.affordances.{verb}:set 必须是「量名 → 表达式」的对象")
        raw_set = {}
    for key, source in raw_set.items():
        name = str(key).strip()
        problem = bad_output_name(name)
        if problem:
            errors.append(f"{label}.affordances.{verb}.set.{name}:{problem}")
            continue
        if name not in quantities:
            errors.append(
                f"{label}.affordances.{verb}.set.{name}:这个种类没声明过 `{name}` 这个量 —— "
                f"写下去会在它身上凭空造出一个没人知道、也没有可见性的量。"
                f"声明过的是 {sorted(quantities)}"
            )
            continue
        try:
            outputs[name] = compile_expression(source)
        except ExpressionError as exc:
            errors.append(f"{label}.affordances.{verb}.set.{name}:{exc}")

    requires: list[Expression] = []
    raw_requires = spec.get("requires") or []
    if not isinstance(raw_requires, list):
        errors.append(f"{label}.affordances.{verb}:requires 必须是表达式列表")
        raw_requires = []
    for position, source in enumerate(raw_requires):
        try:
            requires.append(compile_expression(source))
        except ExpressionError as exc:
            errors.append(f"{label}.affordances.{verb}.requires[{position}]:{exc}")

    charges: dict[str, Expression] = {}
    raw_costs = spec.get("costs") or {}
    if not isinstance(raw_costs, dict):
        errors.append(f"{label}.affordances.{verb}:costs 必须是「她的量名 → 表达式」的对象")
        raw_costs = {}
    for key, source in raw_costs.items():
        name = str(key).strip()
        problem = bad_output_name(name)
        if problem:
            errors.append(f"{label}.affordances.{verb}.costs.{name}:{problem}")
            continue
        if name not in actor_quantities:
            errors.append(
                f"{label}.affordances.{verb}.costs.{name}:`agent` 种类没声明过 "
                f"`{name}` 这个量 —— 扣下去会在她身上凭空造出一个没人知道的属性。"
                f"先在 kinds 里写一条 {{\"id\": \"agent\", \"quantities\": {{…}}}};"
                f"她身上声明过的是 {sorted(actor_quantities)}"
            )
            continue
        try:
            charges[name] = compile_expression(source)
        except ExpressionError as exc:
            errors.append(f"{label}.affordances.{verb}.costs.{name}:{exc}")

    spending: dict[str, int] = {}
    raw_consumes = spec.get("consumes") or {}
    if not isinstance(raw_consumes, dict):
        errors.append(
            f"{label}.affordances.{verb}:consumes 必须是「东西的 id → 几个」的对象"
        )
        raw_consumes = {}
    for key, source in raw_consumes.items():
        item_id = str(key).strip()
        if not item_id:
            errors.append(f"{label}.affordances.{verb}.consumes:东西的 id 不能是空的")
            continue
        # 这里**只收整数**,不收表达式。花掉半包肥料是没有意思的,而库存本来就是整数;
        # 收表达式则要多解释一遍"算出 -1 会怎样"、"算出 0.5 会怎样",而这两个答案
        # 都只能是"不许"。少一个能问的问题,比多一分表达力值钱。
        if isinstance(source, bool) or not isinstance(source, int):
            errors.append(
                f"{label}.affordances.{verb}.consumes.{item_id}:必须是正整数,"
                f"收到 {source!r} —— 花掉的东西是数得清的个数,不是一个算出来的量"
            )
            continue
        if source <= 0:
            errors.append(
                f"{label}.affordances.{verb}.consumes.{item_id}:必须是正整数,收到 {source} —— "
                f"想凭空**给**她东西的话,这里不是那个地方(那是事件干的事)"
            )
            continue
        spending[item_id] = int(source)

    # ── 跟谁一起 ──────────────────────────────────────────────────────────
    participants, participant_errors = _parse_participants(
        label, verb, spec.get("participants")
    )
    errors.extend(participant_errors)

    # ── 生与灭 ────────────────────────────────────────────────────────────
    spawn, spawn_errors = _parse_spawn(label, verb, spec.get("spawn"))
    errors.extend(spawn_errors)

    destroys = False
    raw_destroys = spec.get("destroys_target")
    if raw_destroys is not None:
        if not isinstance(raw_destroys, bool):
            errors.append(
                f"{label}.affordances.{verb}.destroys_target:必须是 true / false,"
                f"收到 {raw_destroys!r}"
            )
        else:
            destroys = raw_destroys
    if destroys and raw_set:
        # 写到一个正要被抹掉的东西身上。两条里必有一条是作者没想清楚的,而引擎
        # 挑哪条都是猜 —— 落库再删,那几个值一秒都没人读到;删了再落库,写到一个
        # 不存在的 owner 上(量的外键那道闸正是为这个存在的)。
        errors.append(
            f"{label}.affordances.{verb}:`destroys_target` 和 `set` 不能一起写 —— "
            f"这个东西做完就没了,写在它身上的量没有任何人读得到。"
            f"要留下点什么就用 `spawn` 生一个新的"
        )

    has_price = bool(charges) or bool(spending) or duration > 0
    if (spawn is not None or destroys) and not has_price:
        # **生成必须要代价** —— 而挡住无限生成的正确办法不是配额。配额是引擎的
        # 天花板:撞上去时她收到的拒绝在世界里没有意义,她也永远学不会。代价是
        # 世界的理由:她知道自己为什么做不到、要做到得先补什么。
        # 灭同理:一个不要代价的"抹掉",一 tick 就能把世界清空。
        errors.append(
            f"{label}.affordances.{verb}:声明了 "
            f"{'spawn' if spawn is not None else 'destroys_target'} 就必须要代价 —— "
            f"写 costs(扣她的量)/ consumes(花掉材料)/ duration(要花的时间)"
            f"里的至少一样。不要代价的生灭,作者写下去的第二天世界里就是一百万个"
        )

    if errors:
        return (None, errors)

    def unresolved(expressions: Iterable[Expression]) -> tuple[list[str], list[str]]:
        """这些表达式读了哪些查不到的名字 —— 分成"它身上的"和"她身上的"两摞。"""
        theirs: list[str] = []
        mine: list[str] = []
        for expression in expressions:
            for name in expression.names:
                if name in BUILTIN_NAMES or name.startswith(WORLD_PREFIX):
                    continue
                if name.startswith(HAVE_PREFIX):
                    # 东西的 id 不在这一层查 —— 物品定义住在经济那一层,解析种类时
                    # 还看不见。`resolve` 拿着两边一起查(和地点、规律引用同一条路)。
                    continue
                if name.startswith(ME_PREFIX):
                    if name[len(ME_PREFIX):] not in actor_quantities:
                        mine.append(name)
                elif name not in quantities:
                    theirs.append(name)
        return (sorted(set(theirs)), sorted(set(mine)))

    everything = (*conditions, *outputs.values(), *requires, *charges.values())
    undeclared, unknown_me = unresolved(everything)
    if undeclared:
        errors.append(
            f"{label}.affordances.{verb}:读了没声明的量 {undeclared} —— "
            f"读到的会恒为 0,于是这个能力静默地永远做同一件事。"
            f"声明过的是 {sorted(quantities)}"
        )
    if unknown_me:
        errors.append(
            f"{label}.affordances.{verb}:读了 {unknown_me},但 `agent` 种类没声明过"
            f"这些量 —— 读到的会恒为 0,于是这道关于她的门要么永远开着、要么永远关着。"
            f"她身上声明过的是 {sorted(ME_PREFIX + n for n in actor_quantities)}"
        )

    # **`requires` 只准读她自己。** 这不是洁癖:一条 requires 不成立必须永远只有
    # 一个意思 ——「你做不了」。它要是能读树身上的量,那它就和 `when` 是同一样东西,
    # 而她收到拒绝时便再也分不出该「等一会儿」还是该「先去歇着」。
    foreign = sorted({
        name
        for expression in requires
        for name in expression.names
        if not name.startswith(ME_PREFIX)
        and not name.startswith(HAVE_PREFIX)
        and name not in BUILTIN_NAMES
    })
    if foreign:
        errors.append(
            f"{label}.affordances.{verb}.requires:读了 {foreign} —— requires 只准读 "
            f"`{ME_PREFIX}*`(她身上的量)与 `{HAVE_PREFIX}*`(她带着几个某样东西)。"
            f"关于这个东西的条件写进 `when`:两者的"
            f"拒绝理由不一样,「树还没长好」要她等,「你没力气了」要她先去别的地方"
        )

    if errors:
        return (None, errors)
    return (
        Affordance(
            verb=verb,
            label=verb_label,
            conditions=tuple(conditions),
            outputs=outputs,
            requires=tuple(requires),
            costs=charges,
            consumes=spending,
            duration=duration,
            occupies=occupies,
            spawn=spawn,
            destroys_target=destroys,
            participants=participants,
            importance=importance,
        ),
        [],
    )


def _parse_affordance_importance(
    label: str, verb: str, spec: dict[str, Any], errors: list[str]
) -> float | None:
    """`importance`:0~1,可选。**不写 = 谁都不记得**,和从前逐位相同。

    形状照抄规律那一层的 `_parse_importance`(`rules.py`),一个字都不改 ——
    作者在一处学会的刻度,到另一处必须还是同一把尺。上下界都是闸而不是 clamp:
    一个写了 `8` 的作者想的是"很重要",按 1 截断之后他永远不会知道自己写错了刻度。
    """
    if "importance" not in spec:
        return None
    raw = spec["importance"]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        errors.append(
            f"{label}.affordances.{verb}.importance:必须是 0~1 的数"
            f"(在场的人该多把这件事记在心上),收到 {type(raw).__name__}"
        )
        return None
    if not 0.0 <= float(raw) <= 1.0:
        errors.append(
            f"{label}.affordances.{verb}.importance:必须落在 0~1 之间,收到 {raw}"
        )
        return None
    return float(raw)


def _parse_participants(
    label: str, verb: str, raw: Any
) -> tuple[ParticipantSpec | None, list[str]]:
    """`participants` 那一格 —— 这件事得有几个人一起。

    **不写就是单人的老样子**,所以已有的世界一个字都不用改(和 `kinds` 这一层
    整体"声明本身就是开关"逐字同构)。写了就是作者在说"一个人做不成这件事",
    于是引擎开始要一份名单、逐个问过去。
    """
    if raw is None:
        return (None, [])
    where = f"{label}.affordances.{verb}.participants"
    if not isinstance(raw, dict):
        return (None, [
            f"{where}:必须是对象(形如 {{\"min\": 1, \"max\": 2}}),"
            f"收到 {type(raw).__name__}"
        ])

    errors: list[str] = []
    unknown = set(raw) - {"min", "max"}
    if unknown:
        # `consent` 是这里最可能被写下来的那个词,所以单独点它的名 —— 静默忽略
        # 的话,作者会以为自己关掉了同意,而世界照旧一个个去问。
        errors.append(
            f"{where}:不认识的字段 {sorted(unknown)} —— 只认 min / max。"
            f"没有 `consent` 这个开关:同意永远是必须的,"
            f"一个拉着谁就一起做事的能力等于取消对方的意志"
        )

    def count(key: str, default: int) -> int | None:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{where}.{key}:必须是整数(几个人),收到 {value!r}")
            return None
        return int(value)

    minimum = count("min", 1)
    if minimum is not None and minimum < 1:
        # 写 0 的话这件事根本不是"一起",而作者写下 `participants` 时想的一定
        # 不是那个意思。静默当成单人的话,他会以为自己已经表达过了。
        errors.append(
            f"{where}.min:至少是 1(除了发起的那个人之外还要几个),收到 {minimum} —— "
            f"一个人做不成「一起」;不需要别人就别写 participants"
        )
        minimum = None

    maximum = count("max", minimum if minimum is not None else 1)
    if minimum is not None and maximum is not None:
        if maximum < minimum:
            errors.append(f"{where}.max:不能小于 min({minimum}),收到 {maximum}")
        elif maximum > MAX_PARTICIPANTS:
            errors.append(
                f"{where}.max:最多 {MAX_PARTICIPANTS} 个,收到 {maximum} —— "
                f"一场共同经历要为每一对有序对发一条关系事件,人数是平方进去的"
            )

    if errors:
        return (None, errors)
    return (ParticipantSpec(minimum=int(minimum or 1), maximum=int(maximum or 1)), [])


def _parse_spawn(label: str, verb: str, raw: Any) -> tuple[SpawnSpec | None, list[str]]:
    """`spawn` 那一格。种类引用与地点引用留给 `resolve` —— 这里还看不见它们。

    量的键这里也不查,同理:要查得知道那个**新种类**声明过什么,而种类表是
    `parse_kinds` 的产物,这个函数正跑在它中间。两阶段加载的分工就是这样。
    """
    if raw is None:
        return (None, [])
    where = f"{label}.affordances.{verb}.spawn"
    if not isinstance(raw, dict):
        return (None, [f"{where}:必须是对象,收到 {type(raw).__name__}"])

    errors: list[str] = []
    unknown = set(raw) - {"kind", "name", "gloss", "location", "quantities"}
    if unknown:
        errors.append(f"{where}:不认识的字段 {sorted(unknown)}")

    kind_id = str(raw.get("kind") or "").strip()
    if not kind_id:
        errors.append(f"{where}:少了 kind —— 生出来的是**哪一种**东西")
    elif not _KIND_ID_RE.match(kind_id):
        errors.append(
            f"{where}.kind:种类 id 不能带空白或冒号(冒号是实例 id 的分隔符),"
            f"收到 {kind_id!r}"
        )

    location = raw.get("location")
    if location is not None and not str(location).strip():
        # 写了个空串和不写不是一回事:不写是"生在当场",而空串是作者以为自己
        # 指定了地方。静默当成当场的话,他会以为那句话生效了。
        errors.append(f"{where}.location:不能是空的 —— 不写就是「生在这件事发生的地方」")

    values: dict[str, float] = {}
    raw_values = raw.get("quantities") or {}
    if not isinstance(raw_values, dict):
        errors.append(f"{where}.quantities:必须是「量名 → 数」的对象")
        raw_values = {}
    for key, source in raw_values.items():
        name = str(key).strip()
        if isinstance(source, bool) or not isinstance(source, (int, float)):
            # **只收常数,不收表达式。** 一个新生的东西身上还没有任何值可读,
            # 而读施动者或母体的量是另一件事(那要先回答"读的是起头那一刻还是
            # 收尾那一刻"),没想清楚之前不开这个口。
            errors.append(
                f"{where}.quantities.{name}:必须是一个数,收到 {source!r} —— "
                f"新生的东西身上还没有值可读,所以这里不收表达式"
            )
            continue
        values[name] = float(source)

    if errors:
        return (None, errors)
    return (
        SpawnSpec(
            kind=kind_id,
            name=str(raw.get("name") or "").strip(),
            gloss=str(raw.get("gloss") or "").strip(),
            location=str(location).strip() if location else "",
            quantities=values,
        ),
        [],
    )


def _affordance_label(label: str, verb: str, raw: Any) -> tuple[str, list[str]]:
    """这个动词她读到的是哪几个字。

    动词放开之后这一步才有必要:内置的十个词引擎自带中文,自造的引擎不知道怎么念。
    判据是**纯 ASCII 的动词必须给 `label`** —— 不是为了严格,是因为 `describe()`
    的产物直接进她的提示词,而"你可以对它:端详、brew"里那个 brew 是噪音,她还得
    照着它行动。中文/日文之类的动词 id 本身就是人话,不必再写一遍。
    """
    if raw is not None:
        text = str(raw).strip()
        if not text:
            return ("", [f"{label}.affordances.{verb}.label:不能是空的"])
        return (text, [])
    builtin = BUILTIN_AFFORDANCE_LABELS.get(verb)
    if builtin:
        return (builtin, [])
    if _ASCII_RE.match(verb):
        return ("", [
            f"{label}.affordances.{verb}:自造的动词要给一行 `label` —— "
            f"她提示词里读到的是那几个字,而 {verb!r} 在中文提示词里是噪音。"
            f"内置自带中文的是 {list(BUILTIN_AFFORDANCE_LABELS)}",
        ])
    return (verb, [])


def _parse_quantity(label: str, name: str, spec: Any) -> tuple[Quantity | None, list[str]]:
    errors: list[str] = []
    # **量名不许和内置名撞车。** 内置名(`dt`/`now`/`day`/`hour`/`minute`/
    # `minute_of_day`)在表达式的命名空间里放在最后,盖过同名的量 —— 于是一个
    # 声明了 `hour` 的世界会安静地读到钟点,而作者以为读的是他那个量:量照存、
    # 规律照跑、日志干净,只有算出来的数是别人的。放行的坏处不对称,所以当场拒。
    if name in BUILTIN_NAMES:
        return (None, [
            f"{label}.quantities.{name}:`{name}` 是表达式的内置名"
            f"(全部内置名:{sorted(BUILTIN_NAMES)}),声明成量之后规律读到的会是"
            f"内置的那个值,而不是你这个量 —— 换个名字"
        ])
    if isinstance(spec, (int, float)) and not isinstance(spec, bool):
        # 简写:只给默认值,可见性走默认(hidden)。
        return (Quantity(key=name, default=float(spec)), errors)
    if not isinstance(spec, dict):
        return (None, [f"{label}.quantities.{name} 必须是数字(默认值)或对象"])

    unknown = set(spec) - {"default", "visible", "visibility", "label", "unit", "bands"}
    if unknown:
        errors.append(f"{label}.quantities.{name}:不认识的字段 {sorted(unknown)}")

    default = 0.0
    if "default" in spec:
        try:
            default = float(spec["default"])
        except (TypeError, ValueError):
            errors.append(f"{label}.quantities.{name}.default 不是数字")

    # 字段名两个都收:种子里历史上叫 `visible`,存储层叫 `visibility`。
    raw_visibility = spec.get("visibility", spec.get("visible", HIDDEN))
    visibility = str(raw_visibility).strip()
    if visibility not in VISIBILITIES:
        errors.append(
            f"{label}.quantities.{name}:不认识的可见档 {visibility!r},"
            f"只有 {sorted(VISIBILITIES)}"
        )
        visibility = HIDDEN

    # 分档:坏声明当场拒(和量名拼错同一条 —— 放行的样子是她一直在报数字,
    # 而提示词看上去完全正常)。判断走 perception 那一份,不另写。
    from anima_world.perception import band_errors, parse_bands

    errors.extend(band_errors(spec.get("bands"), label=f"{label}.quantities.{name}"))

    if errors:
        return (None, errors)
    return (
        Quantity(
            key=name,
            default=default,
            visibility=visibility,
            label=str(spec.get("label") or "").strip(),
            unit=str(spec.get("unit") or "").strip(),
            bands=parse_bands(spec.get("bands")),
        ),
        errors,
    )


def _parse_prompt(label: str, raw: Any) -> tuple[PromptSpec | None, list[str]]:
    errors: list[str] = []
    if not isinstance(raw, dict):
        return (None, [f"{label}:prompt 必须是 {{\"select\": …, \"budget\": N}}"])

    unknown = set(raw) - {"budget"}
    if unknown:
        errors.append(
            f"{label}.prompt:不认识的字段 {sorted(unknown)} —— 这里只有 budget。"
            f"她看不看得见由量自己的 visibility 决定"
        )

    budget = DEFAULT_HERE_BUDGET
    if "budget" in raw:
        try:
            budget = int(raw["budget"])
        except (TypeError, ValueError):
            errors.append(f"{label}.prompt.budget 不是整数")
        else:
            if budget <= 0:
                errors.append(
                    f"{label}.prompt.budget 必须为正 —— 要"
                    f"「不进提示词」就整个不写 prompt 这一段"
                )

    if errors:
        return (None, errors)
    return (PromptSpec(budget=budget), errors)


def _bad_quantity_name(name: str) -> str | None:
    if not name:
        return "量名不能为空"
    if name.startswith("world_"):
        # 和 `rules.bad_output_name` 同一条:`world_x` 是读全局量的语法,
        # 拿它当量名会得到一个永远读不到自己的量。
        return "量名不能以 `world_` 开头 —— 那是读全局量的前缀"
    for mark in (":", "."):
        if mark in name:
            return f"量名不能含 `{mark}`"
    return None


def parse_entities(entries: Any, kinds: Mapping[str, Kind]) -> dict[str, Entity]:
    """把 JSON 里的实例声明编译成注册表,**并当场解析 kind 引用**。

    这是第二阶段的一半:符号表已经建好了,所以一个引用不到的种类在这里就能报死,
    而不是等到世界跑起来发现"规律一条没跑"。
    """
    if entries is None:
        return {}
    if not isinstance(entries, list):
        raise OntologyError([f"entities 必须是一个列表,收到 {type(entries).__name__}"])

    errors: list[str] = []
    out: dict[str, Entity] = {}

    for index, entry in enumerate(entries):
        label = f"entities[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{label} 必须是对象,收到 {type(entry).__name__}")
            continue
        entity_id = str(entry.get("id") or "").strip()
        if not entity_id:
            errors.append(f"{label} 少了 id")
            continue
        label = f"entities[{index}] ({entity_id})"
        if not _ENTITY_ID_RE.match(entity_id):
            errors.append(
                f"{label}:实例 id 必须是 `种类:名字` 的形状(如 `tree:oak_01`)—— "
                f"前缀即种类,量的 owner 用的就是它"
            )
            continue
        if entity_id in out:
            errors.append(f"{label}:id 重复了")
            continue

        kind_id = owner_kind(entity_id)
        kind = kinds.get(kind_id)
        if kind is None:
            errors.append(_unresolved(label, "kind", kind_id, kinds))
            continue
        if kind.builtin:
            errors.append(
                f"{label}:{kind_id!r} 是内置种类,它的实例不在这里登记"
                f"(角色走 agents / 地点走 locations)"
            )
            continue

        unknown = set(entry) - {"id", "name", "gloss", "location", "quantities"}
        if unknown:
            errors.append(f"{label}:不认识的字段 {sorted(unknown)}")

        out[entity_id] = Entity(
            id=entity_id,
            kind=kind_id,
            name=str(entry.get("name") or entity_id.split(":", 1)[1]).strip(),
            gloss=str(entry.get("gloss") or "").strip(),
            location=(str(entry.get("location")).strip() if entry.get("location") else None),
        )

    if errors:
        raise OntologyError(errors)
    return out


def _unresolved(wanter: str, wanted_type: str, wanted_name: str, known: Iterable[str]) -> str:
    """未解析引用的标准说法:**(谁要的, 要什么类型, 要的名字)** 三元组。

    形状照 RimWorld 的 `ResolveAllReferences` —— 它的错误信息之所以有用,正是因为
    三样都在:光说"找不到 tree"没法定位,得说"是 rules[2] 要的、要的是一个 kind"。
    附上已知名单,因为这类错误九成是拼写(`trees` vs `tree`)。
    """
    candidates = sorted(str(k) for k in known)
    return (
        f"{wanter}:引用不到 —— 没有名叫 {wanted_name!r} 的 {wanted_type}。"
        f"已声明的是 {candidates}"
    )


# ── 加载:第二阶段(解析全部引用) ───────────────────────────────────────────


def resolve(
    kinds: Mapping[str, Kind],
    entities: Mapping[str, Entity],
    *,
    rules: Iterable[Any] = (),
    locations: Iterable[str] = (),
    items: Iterable[str] = (),
) -> Ontology:
    """第二阶段:把所有跨表引用解析一遍,**一个解析不了就整体拒绝**。

    这是这一整层真正的交付物。它抓的是同一类病 —— 引用了一个不存在的名字,而系统
    照跑、零报错、只是安静地少做一半事。

    查五样:

    - 规律的 `for_each: {"kind": …}` 指向声明过的种类
    - 规律的 `for_each: {"owner": …}` 指向存在的东西(或内置命名空间)
    - 规律读写的量,在它作用的那个种类上声明过 —— 这条把 `rules.missing_names`
      从"只能建议"升级成了闸(它当年只能建议,正是因为没有种类声明:
      "量可以在世界跑起来之后才被创建")
    - 实例的 `location` 指向存在的地点
    - 能力里的 `have_*` / `consumes` 指向定义过的物品 —— 物品是**闭集**(只在创世
      从种子里播),所以这条查得起来。查不到的那个 id 会让门永远关着,而世界照跑。
    """
    errors: list[str] = []

    for rule in rules:
        label = f"rules ({getattr(rule, 'id', '?')})"
        selector_kind = getattr(rule, "selector_kind", None)
        selector_value = getattr(rule, "selector_value", "")

        if selector_kind == "kind":
            kind = kinds.get(selector_value)
            if kind is None:
                errors.append(_unresolved(f"{label}.for_each.kind", "kind", selector_value, kinds))
                continue
            # 内置种类的量不归这一层声明(角色的功力、世界的季节各有各的来路),
            # 所以只查引用得到,不查量 —— 查了会拒掉一个完全合法的世界。
            if not kind.builtin:
                errors.extend(_check_rule_quantities(label, rule, kind))
        elif selector_kind == "owner":
            owner = selector_value
            kind_id = owner_kind(owner)
            kind = kinds.get(kind_id)
            if kind is None:
                errors.append(_unresolved(f"{label}.for_each.owner", "kind", kind_id, kinds))
                continue
            if not kind.builtin and owner not in entities:
                errors.append(
                    _unresolved(f"{label}.for_each.owner", "entity", owner, entities)
                )
                continue
            if not kind.builtin:
                errors.extend(_check_rule_quantities(label, rule, kind))
        # selector_kind == "action" / "not_action":作用在"此刻正在(没在)做这个
        # 动作的角色"上,动作名是开集(作者可以自定义动作),这里不查 ——
        # 查了会拒掉合法的世界。

    known_items = set(items)
    for kind in kinds.values():
        for affordance in kind.affordances.values():
            for item_id in affordance.item_refs:
                if item_id in known_items:
                    continue
                errors.append(
                    _unresolved(
                        f"kinds ({kind.id}).affordances.{affordance.verb}",
                        "item", item_id, known_items,
                    )
                )

    known_locations = set(locations)
    if known_locations:
        for entity in entities.values():
            if entity.location and entity.location not in known_locations:
                errors.append(
                    _unresolved(
                        f"entities ({entity.id}).location", "location",
                        entity.location, known_locations,
                    )
                )

    # `spawn` 的三样引用都在这一阶段查:生出来的是哪个种类、身上那几个量它声明过
    # 没有、指定的地方存不存在。**都放到运行期查是不行的** —— 一条生不出东西的
    # 能力不会让世界崩,只会让她每次都白付一次代价,而世界照跑、日志干净。
    for kind in kinds.values():
        for affordance in kind.affordances.values():
            spawn = affordance.spawn
            if spawn is None:
                continue
            where = f"kinds ({kind.id}).affordances.{affordance.verb}.spawn"
            born = kinds.get(spawn.kind)
            if born is None:
                errors.append(_unresolved(f"{where}.kind", "kind", spawn.kind, kinds))
            elif born.builtin:
                errors.append(
                    f"{where}.kind:{spawn.kind!r} 是内置种类,生不出来 —— "
                    f"角色走 agents / 地点走 locations,它们各有各的生命周期"
                )
            else:
                for name in sorted(spawn.quantities):
                    if name not in born.quantities:
                        errors.append(
                            f"{where}.quantities.{name}:{spawn.kind!r} 没声明过这个量 —— "
                            f"写下去会在新生的东西身上凭空造出一个没人知道、也没有"
                            f"可见性的量。声明过的是 {sorted(born.quantities)}"
                        )
            if spawn.location and known_locations and spawn.location not in known_locations:
                errors.append(
                    _unresolved(f"{where}.location", "location",
                                spawn.location, known_locations)
                )

    if errors:
        raise OntologyError(errors)
    return Ontology(kinds=dict(kinds), entities=dict(entities))


def _check_rule_quantities(label: str, rule: Any, kind: Kind) -> list[str]:
    """规律读写的量,必须在它作用的那个种类上声明过。

    **写**比读更该拒:一条 `set` 写到没声明的量上,会**凭空造出一个量** —— 世界照跑,
    值也在变,只是没有人知道它存在,也没有可见性声明(于是永远 hidden)。
    读一个没声明的量则会恒为 0,规律静默地永不触发。
    """
    declared = kind.quantity_names()
    if not declared:
        # 一个没声明任何量的种类(纯标记实体),规律作用在它上面一定是写错了。
        return [
            f"{label}:作用在种类 {kind.id!r} 上,但这个种类一个量都没声明 —— "
            f"这条规律无论怎么算都写不到任何地方"
        ]

    from anima_world.rules import BUILTIN_NAMES, WORLD_PREFIX

    errors: list[str] = []
    for name in sorted(getattr(rule, "outputs", {})):
        if name not in declared:
            errors.append(
                f"{label}.set.{name}:种类 {kind.id!r} 没声明过这个量 —— "
                f"写下去会凭空造出一个没人知道、也没有可见性声明的量。"
                f"已声明的是 {sorted(declared)}"
            )

    reads = getattr(rule, "reads", None)
    for name in sorted(reads() if callable(reads) else ()):
        if name in BUILTIN_NAMES or name.startswith(WORLD_PREFIX) or name in declared:
            continue
        errors.append(
            f"{label}:读了 {name!r},但种类 {kind.id!r} 没声明过它 —— "
            f"读到的会恒为 0,这条规律于是静默地永不触发。已声明的是 {sorted(declared)}"
        )
    return errors


def seed_quantities(ontology: Ontology, entity: Entity) -> dict[str, float]:
    """一个新实例出生时该有的量(种类声明的默认值)。"""
    kind = ontology.kinds.get(entity.kind)
    return {q.key: q.default for q in kind.quantities.values()} if kind else {}


def actor_quantities(ontology: Ontology) -> dict[str, float]:
    """一个角色身上声明过的量与默认值。没声明就是空的 —— 这一层于是整个不存在。"""
    kind = ontology.kinds.get("agent")
    return {q.key: q.default for q in kind.quantities.values()} if kind else {}


def visibility_declarations(ontology: Ontology) -> list[tuple[str, str, str, str]]:
    """种类声明**同时就是**可见性声明,不必再写一遍。

    返回 `(种类, 量名, 可见档, 人话名字)` 的列表,喂给 `VisibilityStore.declare`。
    认知层原本按 `(owner 种类, 量名)` 声明 —— 它一直在假设"种类"这个东西存在,
    只是那时种类还没有身份。现在有了,两边合成一处:**量的可见性是量声明的一部分**。

    保持正交是有意的:可见性是量声明上的 modifier,**不是种类树上的分支**。
    别造"隐藏实体"这种种类 —— 形式本体论那边把"认识论侵入本体"当作 SNOMED 的
    病灶点名批评过。
    """
    out: list[tuple[str, str, str, str]] = []
    for kind in ontology.kinds.values():
        # 内置种类里只有 `agent` 可能带量(`DECLARABLE_BUILTINS`),而它带的量
        # **必须**走这条:她自己身上的量声明成 `here` 才轮得到别人看见"她累了",
        # 而这正是这些量存在的一半意义 —— 另一半只有她自己知道(`self`)。
        if kind.builtin and not kind.quantities:
            continue
        for quantity in kind.quantities.values():
            # hidden 就是**没有行**的意思 —— 显式写一行 hidden 和不写行为完全相同,
            # 只会让 `visibility_rules()` 里混进一堆"其实看不见"的条目。留下来的
            # 那些于是恰好是"她感知得到的",这正是那张表该说的话。
            if quantity.visibility == HIDDEN:
                continue
            out.append((kind.id, quantity.key, quantity.visibility, quantity.render_label()))
    return out


# 日历(`rules.BUILTIN_NAMES`)在拒绝语里的说法。它们比几个前缀更容易漏,因为
# **不带前缀** —— 看上去就像作者自己写的量。而作者恰恰不可能声明一个叫 `hour` 的量
# (`parse_kinds` 当场拒,理由见 `rules.BUILTIN_NAMES` 上方),所以这六个名字永远是
# 引擎的,翻译不会撞上世界里的谁。
#
# `minute` 写成和 `minute_of_day` 平行的一句:光一个「分」在「钟点 == 8 且 分 >= 30」
# 里读得出是分钟,单独出现时会被读成分数。
_BUILTIN_SPOKEN = {
    "day": "第几天",
    "hour": "钟点",
    "minute": "一小时里的第几分钟",
    "minute_of_day": "一天里的第几分钟",
    "now": "世界的第几 tick",
    "dt": "这一步过去了多久",
}

# 阈值落在**档中间**时,`>=` / `<=` 要松一格。
#
# 档词说的是**这一档的起点**,不是那个数。阈值正好压在边界上时两者是同一件事,
# `>=` 原样留着(「发条 >= 满的」读得通);落在档中间时不是 —— 线上现场是
# 「你的手上的活儿 >= 生手」印在一个屏幕上正写着「手上的活儿 生手」的人眼前,
# 一句自相矛盾的话:他明明就在这一档里,却被告知要够到这一档。该说的是
# 「> 生手」—— 要的是比这一档更往上。
#
# `>` / `<` 不用动:拒绝时当前值本来就在阈值的另一头,「雾气 > 透亮」正好读成
# "要比你现在多"。`==` / `!=` 也不动 —— 它们本来就不该配一个有损的词,而作者
# 拿一个分过档的量写等号,该修的是那条声明。
_LOOSER = {">=": ">", "<=": "<"}


def speak_expression(
    expression: Expression,
    item_name: Callable[[str], str] | None = None,
    quantity_label: Callable[[str, str], str] | None = None,
    quantity_bands: Callable[[str, str], Any] | None = None,
) -> str:
    """把一条表达式的原文改写成**世界里的词**,给拒绝语用。

    `me_` / `world_` / `have_` 是**引擎的命名空间标记**,不是这个世界里的名字:
    作者声明的量叫「主动」,`me_` 只是"读她身上那份"的意思。而这句话最后印在玩家
    屏幕上(`player_options` 的 `refusal`),还会当 `ToolResult.error` 递给角色 ——
    线上现场是「你现在做不了这件事:`me_主动 >= 1.2` 不成立」,一个玩家从中读不出
    任何可做的事,而角色会把 `me_` 念出来。和 `place_name` 修掉的「她在一个叫 cart
    的地方见过人」、和 `consumes` 那句里的 `notepad` 是同一类:引擎的词汇漏进了
    世界的话。

    `quantity_label(scope, key)` 是同一件事的第三块:**量的内部键也不是这个世界里
    的名字。** 线上现场是玩家菜单上写着「土 正好」,点下去被拒绝成「「土湿 > 0.55」
    不成立」—— 同一个量、同一屏、两个名字,而屏幕上根本没有「土湿」这个词,他会去
    找一样不存在的东西。`scope` 是 `""`(对象自己的量)/ `"me"` / `"world"`,查不到
    就退回键本身。这条和 `readouts` 是同一条纪律的两半:一个量该被念成什么,全世界
    只能有一个答案。

    `quantity_bands(scope, key)` 是**同一条规矩的另一半**:「`label` 换名字、
    `bands` 换值」。分过档的量,玩家在屏幕上**永远**读不到它的数字(`readout_text`
    的规矩就是这样),于是拒绝语里的阈值对他是一串纯噪音 —— 线上现场是同一屏上
    写着「土 正好」而按钮底下写着「土 > 0.55」,他没有任何办法把两者比一比。
    阈值是那个量的一个**值**,所以它按同一份档表、同一个 `band_word` 念成档词:
    「土 > 正好」。**没分档的量照旧留数字**(「你的体力 >= 40」)—— 那个数他在
    菜单上读得到,留着才教得会他还差多少。

    档词是**有损**的,而这是作者的分辨率在说话,不是引擎在骗人:一档里的两个不同
    阈值念出来一模一样。要让拒绝语分得更细,作者该做的是**把档的边界划在阈值上** ——
    和玩家读到的分辨率对齐,而不是让引擎漏一个数字出去。

    有损带来一处**必须跟着松一格**的地方(`_LOOSER`):档词说的是这一档的**起点**,
    所以阈值落在档中间时,`>=` / `<=` 会印出一句自相矛盾的话 —— 「你的手上的活儿
    >= 生手」摆在一个屏幕上正写着「手上的活儿 生手」的人眼前。那时念作「> 生手」。
    阈值压在边界上时两者说的是同一件事,原样留着。

    **除此之外只改名字和阈值,不改算术。** `and` / `or` / `not` 照旧:它们在世界里
    没有另一个说法。算式里的数(`土 > 湿度 * 2` 里的 `2`)也照旧 —— 它不是那个量的
    一个值,念成档词就是胡说。

    **按语法树上的位置改,不按正则**(`rewrite_source`):正则的写法会咬自己刚
    写下的字(`土湿` 换成「土」之后,轮到名字 `土` 那一遍再换一次),也会改掉
    字符串字面量里恰好写着量名的地方。
    """
    if not expression.names:
        return expression.source

    def labelled(scope: str, key: str) -> str:
        return (quantity_label(scope, key) if quantity_label else "") or key

    def scoped(name: str) -> tuple[str, str] | None:
        """名字拆成(作用域, 量名)。**不是这个世界里的量**就返回 None ——
        `have_*` 数的是东西的个数、日历是引擎的,两样都没有档表。"""
        if name.startswith(ME_PREFIX):
            return ("me", name[len(ME_PREFIX):])
        if name.startswith(WORLD_PREFIX):
            return ("world", name[len(WORLD_PREFIX):])
        if name.startswith(HAVE_PREFIX) or name in _BUILTIN_SPOKEN:
            return None
        return ("", name)

    def spoken(name: str) -> str | None:
        if name.startswith(ME_PREFIX):
            return f"你的{labelled('me', name[len(ME_PREFIX):])}"
        if name.startswith(WORLD_PREFIX):
            return f"世界的{labelled('world', name[len(WORLD_PREFIX):])}"
        if name.startswith(HAVE_PREFIX):
            item_id = name[len(HAVE_PREFIX):]
            called = (item_name(item_id) if item_name else "") or item_id
            # 这里**有意不套「」**,虽然名字同样是作者写的。整条表达式在外面已经
            # 被 `「…」不成立` 围过一次,再套一层就是 `「你带着的「伞」 >= 1」`;
            # 而这一处的下一个字永远是运算符(`>=` / `and`),边界本来就断得开。
            # 该套的是 `consumes` 那句 —— 它是散文,右边紧跟着一个「不」字。
            return f"你带着的{called}"
        if name in _BUILTIN_SPOKEN:
            return _BUILTIN_SPOKEN[name]
        said = labelled("", name)
        return said if said != name else None

    def threshold(name: str, value: float, op: str) -> tuple[str, str] | None:
        if quantity_bands is None:
            return None
        pair = scoped(name)
        if pair is None:
            return None
        table = quantity_bands(*pair)
        word = band_word(table, value)
        if not word:
            return None
        # 阈值正好落在档的边界上时,档词和那个数说的是同一件事,`>=` 原样留着。
        # 落在档中间时不是 —— 见 `_LOOSER` 上面那段。
        edges = {float(edge) for edge, _ in table}
        return (word, op if value in edges else _LOOSER.get(op, op))

    return rewrite_source(expression, spoken, threshold)


@dataclass(frozen=True)
class AffordanceOutcome:
    """一次能力调用算出来的结果。**只算不写** —— 写由调用方落库。

    `refusal` 非空表示没成:她试了,条件不满足。照实报回去,不假装成功 ——
    和 `emit_action` 返回 `False` 同一条纪律。

    而**没成有两种,她该做的事完全相反**:

    | `reason` | 意思 | 她接下来该干什么 |
    |---|---|---|
    | `conditions` | 世界说"这会儿不行"(果子还没熟) | 等,或者换一棵 |
    | `incapable` | 她做不了(没力气了) | 换件别的事,或者先去补足 |
    | `error` | 表达式算不出来 | 作者的事,不是她的事 |

    合成一个的话,一个累坏了的人会挨棵树轮着试过去 —— 每一棵都告诉她"再等等"。
    """

    verb: str
    updates: dict[str, float] = field(default_factory=dict)
    me_updates: dict[str, float] = field(default_factory=dict)
    # 她身上那几个量**动之前**是多少(只留 `me_updates` 里出现过的那些)。
    # 留着它是为了 `me_deltas` —— 见那条属性。
    me_before: dict[str, float] = field(default_factory=dict)
    # 花掉的东西(item_id → 几个)。**不由这里落库** —— 库存只有事件日志一个来源,
    # 调用方照着发 `item_consume`。这里给的是"该扣什么",不是"扣好了"。
    consumed: dict[str, int] = field(default_factory=dict)
    refusal: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.refusal

    @property
    def me_deltas(self) -> dict[str, float]:
        """她身上那几个量**变了多少**(新值 − 旧值,带符号)。

        `costs` 里写的表达式算出来的是**新值**(和 `set` 一样,`me_体力 - 4` → 96),
        所以 `me_updates` 是"她现在剩多少",不是"她花了多少"。两者差得远:一次擦窗
        `me_updates` 报 96,而她其实只花了 4。

        差额必须**单独记下来**,不能让读日志的人自己去减:她身上的量不只被能力
        改,规律和需求带也在改 —— 拿前后两条 `entity_interaction` 相减,减出来的
        是这两次之间发生的**一切**,不是这一次的代价。
        """
        return {
            key: value - self.me_before.get(key, 0.0)
            for key, value in self.me_updates.items()
        }


def apply_affordance(
    affordance: Affordance,
    *,
    values: Mapping[str, float],
    world_values: Mapping[str, float] | None = None,
    me_values: Mapping[str, float] | None = None,
    held: Mapping[str, int] | None = None,
    item_name: Callable[[str], str] | None = None,
    quantity_label: Callable[[str, str], str] | None = None,
    quantity_bands: Callable[[str, str], Any] | None = None,
    now: int = 0,
    minutes_per_tick: int = DEFAULT_MINUTES_PER_TICK,
) -> AffordanceOutcome:
    """算一次能力调用:读目标与施动者此刻的量,给出两边要写回去的量。

    和规律那一层的 `_apply` 有意长得像(同一套受限表达式、同一份 `world_` 前缀),
    但**没有 `dt`**:能力是一下子的事,不是持续流逝。给它一个 `dt` 只会诱使作者
    写出"照料一次涨的量取决于上次有人照料是多久以前"这种反直觉的东西。

    **`requires` 先于 `when`。** 两条都不成立时,该告诉她的是"你做不了"——
    这是她此刻唯一能行动的那条信息(去歇着)。反过来先说"树还没长好",她只会
    走去下一棵树,再收到同一句,一棵一棵试到天亮。

    `held` 是她随身带着的东西(item_id → 几个),进 `have_*`。**没传就是空的**:
    一个没接经济层的调用方于是让每一道 `have_*` 的门都关着 —— 这是对的那一边,
    反过来默认"她什么都有"会让声明形同虚设。

    `item_name` 把 id 翻成人话,**只在真要拒绝的时候问**(顺利那一路一次都不查)。
    不给就退回 id —— 一样东西的名字本来就可以是它的 id("引用即存在"那条路)。
    这一句是给人读的:「你手上的 notepad 不够」印在一个全中文世界的玩家屏幕上,
    而且它还会当 `ToolResult.error` 递回给她,于是角色把 `notepad` 念出来。
    三条拒绝语共用它:`consumes` 那句、以及 `requires` / `conditions` 两句里
    `have_*` 的翻译(见 `speak_expression`)。

    `quantity_label` 同理,翻的是**量**的内部键 —— 玩家菜单上那个量叫「土」,拒绝
    句里不该叫它 `土湿`。也只在真要拒绝的时候问,理由见 `speak_expression`。
    `quantity_bands` 是它的另一半:分过档的量,阈值也念成档词 —— 玩家在屏幕上
    永远读不到那个数字,留着它等于在拒绝语里印一串他没法比对的噪音。
    """
    holdings = {str(k): int(v) for k, v in (held or {}).items()}
    namespace: dict[str, Any] = {
        **{f"{WORLD_PREFIX}{k}": v for k, v in (world_values or {}).items()},
        **{f"{ME_PREFIX}{k}": v for k, v in (me_values or {}).items()},
        **{f"{HAVE_PREFIX}{k}": float(v) for k, v in holdings.items()},
        **dict(values),
        # 日历(见 `rules.BUILTIN_NAMES`)。能力和规律读同一组名字 —— 一个能在
        # 规律里写「日落之后不长」的作者,理应能在能力上写「天黑了砍不了柴」。
        **clock_names(now, minutes_per_tick),
        "now": now,
        "dt": 0,
    }
    # 没带着的东西读作 0,而不是"名字不存在"。这两者在这里必须是同一件事:
    # 一个从没拿过剪子的人和一个刚把剪子放下的人,在"她现在能不能修枝"上没有区别。
    for item_id in affordance.item_refs:
        namespace.setdefault(f"{HAVE_PREFIX}{item_id}", 0.0)

    def broken(exc: ExpressionError) -> AffordanceOutcome:
        # 运行期降级:一次算不出来的调用不该掀翻这一轮,但绝不无声。
        logger.warning("能力 %s 算不出来:%s", affordance.verb, exc)
        return AffordanceOutcome(
            verb=affordance.verb, refusal=f"这会儿算不出来({exc})", reason="error"
        )

    for requirement in affordance.requires:
        try:
            able = requirement.evaluate(namespace)
        except ExpressionError as exc:
            return broken(exc)
        if not able:
            return AffordanceOutcome(
                verb=affordance.verb,
                refusal="你现在做不了这件事:"
                        f"「{speak_expression(requirement, item_name, quantity_label, quantity_bands)}」"
                        f"不成立",
                reason="incapable",
            )
    # 花掉一样东西**自带**一道"你得有"的门,不必作者再写一遍 `have_x >= n`:
    # 要花掉自己没有的东西,唯一讲得通的意思就是做不了。少写的那一遍正是要害 ——
    # 只写 `consumes` 的世界里她会用一包不存在的肥料把活干完,而库存扣不到负数,
    # 于是连账上都看不出来。
    for item_id, quantity in sorted(affordance.consumes.items()):
        on_hand = holdings.get(item_id, 0)
        if on_hand < quantity:
            called = (item_name(item_id) if item_name else "") or item_id
            return AffordanceOutcome(
                verb=affordance.verb,
                # 名字是作者写的,划得出边界才读得断 —— 见 `Scheduler._named`。
                # 空格也断得开,但同一屏上两种划法本身就是一种噪音,而且作者写得出
                # 带空格的名字(「a cup of tea」),那时空格什么都断不开。
                refusal=f"你手上的「{called}」不够:要 {quantity} 个,你有 {on_hand} 个",
                reason="incapable",
            )
    for condition in affordance.conditions:
        try:
            satisfied = condition.evaluate(namespace)
        except ExpressionError as exc:
            return broken(exc)
        if not satisfied:
            return AffordanceOutcome(
                verb=affordance.verb,
                refusal="这会儿不行:"
                        f"「{speak_expression(condition, item_name, quantity_label, quantity_bands)}」"
                        f"不成立",
                reason="conditions",
            )
    try:
        updates = {
            key: float(expression.evaluate(namespace))
            for key, expression in affordance.outputs.items()
        }
        # 代价和效果读的是**同一份**旧值 —— 双缓冲,和规律那一层同一条纪律。
        # 顺序敏感的话,"扣体力"和"树长高"谁先算就成了写声明时看不见的语义。
        me_updates = {
            key: float(expression.evaluate(namespace))
            for key, expression in affordance.costs.items()
        }
    except ExpressionError as exc:
        return broken(exc)
    return AffordanceOutcome(
        verb=affordance.verb,
        updates=updates,
        me_updates=me_updates,
        # 动之前是多少。读的是**同一份**旧值(双缓冲那一份),所以差额算出来
        # 恰好是这一次的代价,不掺别的。
        me_before={
            key: float((me_values or {}).get(key, 0.0)) for key in me_updates
        },
        consumed=dict(affordance.consumes),
    )


def finish_affordance(
    affordance: Affordance,
    *,
    values: Mapping[str, float],
    world_values: Mapping[str, float] | None = None,
    me_values: Mapping[str, float] | None = None,
    now: int = 0,
    minutes_per_tick: int = DEFAULT_MINUTES_PER_TICK,
) -> AffordanceOutcome:
    """一个长过程走完了 —— 只算它对**那个东西**的效果。

    和 `apply_affordance` 的分工是**时间上的**,不是功能上的:

        起头   apply_affordance  → 查关口、扣她的量、花掉材料
        走完   finish_affordance → 算这个东西变成什么样

    **关口不再查。** 这是有意的:付了代价、占了她十个月,到头来被一句"这会儿不行"
    拒掉的话,她没有任何办法预防那次失败 —— 而一个预防不了的失败教不会她任何东西,
    只会让长过程变成赌博。想让长过程可能落空的世界,让它落空在**起头**。

    但**效果读的是此刻的值,不是起头那一刻的**:一棵树在这十个月里自己长了,
    走完时该在它现在的高度上加。拿起头的快照算等于把这十个月的世界抹掉。
    """
    namespace: dict[str, Any] = {
        **{f"{WORLD_PREFIX}{k}": v for k, v in (world_values or {}).items()},
        **{f"{ME_PREFIX}{k}": v for k, v in (me_values or {}).items()},
        **dict(values),
        # 日历(见 `rules.BUILTIN_NAMES`)。能力和规律读同一组名字 —— 一个能在
        # 规律里写「日落之后不长」的作者,理应能在能力上写「天黑了砍不了柴」。
        **clock_names(now, minutes_per_tick),
        "now": now,
        "dt": 0,
    }
    try:
        updates = {
            key: float(expression.evaluate(namespace))
            for key, expression in affordance.outputs.items()
        }
    except ExpressionError as exc:
        logger.warning("能力 %s 收尾时算不出来:%s", affordance.verb, exc)
        return AffordanceOutcome(
            verb=affordance.verb, refusal=f"收尾时算不出来({exc})", reason="error"
        )
    return AffordanceOutcome(verb=affordance.verb, updates=updates)


def check_entity(
    ontology: Ontology,
    entity_id: str,
    *,
    values: Mapping[str, float],
    world_values: Mapping[str, float] | None = None,
    place: str | None = None,
) -> list[str]:
    """**这个东西活得了吗** —— 逐条列出它身上讲不通的地方,没问题就是空列表。

    出生自检。运行期生出来的东西走的不是创世那条路,而创世那条路上的闸(一次列全、
    当场开不了机)在这里一条都不在。少了这一步,一个新生的东西可以是这样:
    `entities` 里看着好好的,量却一个都没落地 —— 于是它的能力条件对着 0 求值,
    `tend` 安静地永远不生效,规律照跑、日志干净,作者要到三个月后发现那棵树没长
    才知道。**这正是这个仓库最怕的那一类,只是换到了运行期。**

    查四样,按"错了之后有多难发现"排:

    1. **种类还认得它。** 认不得的话下面三样都问不出口。
    2. **声明过的量一个不缺。** 逐个量查,不是查"有没有量" —— 后者放得过"给了一个
       `树高` 就算数",而那正是创世那边踩过的坑(其余量不落地,规律算不动)。
    3. **每条能力都给得出一个叫得出名字的结论。** 空跑一遍(`apply_affordance`
       本来就**只算不写**,所以这一步几乎免费)。判据不是"能成功" —— `conditions`
       (果子还没熟)和 `incapable`(她做不了)都算过关,那是世界在正常说话。
       只有 `error`(表达式算不出来)不算:那是作者的声明本身坏了。
    4. **它按声明的可见性真的在场。** 声明成 `here` 却不在任何地方,等于谁也看不见 ——
       这个东西在世界里存在,而没有任何人能碰到它,是一种比不存在更糟的存在。

    `place` 传 `None` 表示调用方不查在场那一条(比如还没接可见性表的场合)。
    """
    entity = ontology.entities.get(entity_id)
    if entity is None:
        return [f"{entity_id}:本体里没有这个东西"]
    kind = ontology.kinds.get(entity.kind)
    if kind is None:
        return [f"{entity_id}:它的种类 {entity.kind!r} 不在本体里 —— 这东西没有出身"]

    problems: list[str] = []
    for name in sorted(kind.quantities):
        if name not in values:
            problems.append(
                f"{entity_id}:量 {name!r} 没落地 —— 读到的会是 0,"
                f"于是用到它的条件与规律都安静地不生效"
            )

    for verb, affordance in sorted(kind.affordances.items()):
        outcome = apply_affordance(
            affordance,
            values=values,
            world_values=world_values,
            me_values={k: 0.0 for k in actor_quantities(ontology)},
            held={},
        )
        if outcome.reason == "error":
            problems.append(f"{entity_id}:能力 {verb!r} 算不出来 —— {outcome.refusal}")

    if place is not None and not place:
        visible = [q.key for q in kind.quantities.values() if q.visibility != HIDDEN]
        if visible:
            problems.append(
                f"{entity_id}:不在任何地方,而它有人看得见的量 {sorted(visible)} —— "
                f"没有位置的东西永远不在场,于是「在场可见」等于「永远看不见」"
            )
    return problems
