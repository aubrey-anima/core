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
from typing import Any, Iterable, Mapping

from anima_world.expressions import Expression, ExpressionError, compile_expression
from anima_world.perception import HIDDEN, VISIBILITIES
from anima_world.rules import BUILTIN_NAMES, WORLD_PREFIX, bad_output_name

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

# 她能对一个东西做什么。**闭集**,和 `edges` 的谓词闭集同一条纪律:
# 放开就要重算上界。Dwarf Fortress 的 raws 是反例边界 —— 能给生物加
# `[CARNIVOROUS]`,却造不出自定义食性,因为效果终归由引擎实现。
# 与其假装开放,不如把边界写在这里。
AFFORDANCES = (
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

# 能力的人话。她提示词里读到的是这些词,不是 `harvest` —— 英文动词在中文提示词里
# 是**噪音**,而她要照着它行动。
AFFORDANCE_VERBS = {
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

# 同一个地方东西太多时,最多带几个进提示词。作者没在种类上声明就用它 ——
# 有上限本身不是可选的(那是这一层的判据),可选的只是**几个**。
DEFAULT_HERE_BUDGET = 5


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

    @property
    def changes_world(self) -> bool:
        return bool(self.outputs) or bool(self.costs) or bool(self.consumes)

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
        """这个东西认不认这个动词。认不得返回 `None` —— 由调用方报成拒绝。"""
        entity = self.entities.get(owner)
        kind = self.kinds.get(entity.kind if entity else owner_kind(owner))
        return kind.affordances.get(verb) if kind else None

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
        verbs = [AFFORDANCE_VERBS[a] for a in kind.affordances if a in AFFORDANCE_VERBS]
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
                if name not in AFFORDANCES:
                    errors.append(
                        f"{label}:不认识的 affordance {name!r} —— 只认 {list(AFFORDANCES)}。"
                        f"这是个闭集:引擎认得的动词才有实现,放开它只会得到一个"
                        f"她说得出、做不到的能力"
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
    unknown = set(spec) - {"when", "set", "requires", "costs", "consumes"}
    if unknown:
        errors.append(
            f"{label}.affordances.{verb}:不认识的字段 {sorted(unknown)} —— "
            f"只认 when / set(关于它)与 requires / costs / consumes(关于她)。"
            f"门槛事件归规律那一层(能力只管这一下改了什么)"
        )

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
            conditions=tuple(conditions),
            outputs=outputs,
            requires=tuple(requires),
            costs=charges,
            consumes=spending,
        ),
        [],
    )


def _parse_quantity(label: str, name: str, spec: Any) -> tuple[Quantity | None, list[str]]:
    errors: list[str] = []
    if isinstance(spec, (int, float)) and not isinstance(spec, bool):
        # 简写:只给默认值,可见性走默认(hidden)。
        return (Quantity(key=name, default=float(spec)), errors)
    if not isinstance(spec, dict):
        return (None, [f"{label}.quantities.{name} 必须是数字(默认值)或对象"])

    unknown = set(spec) - {"default", "visible", "visibility", "label", "unit"}
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

    if errors:
        return (None, errors)
    return (
        Quantity(
            key=name,
            default=default,
            visibility=visibility,
            label=str(spec.get("label") or "").strip(),
            unit=str(spec.get("unit") or "").strip(),
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
        # selector_kind == "action":作用在"此刻正在做这个动作的角色"上,
        # 动作名是开集(作者可以自定义动作),这里不查 —— 查了会拒掉合法的世界。

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
    # 花掉的东西(item_id → 几个)。**不由这里落库** —— 库存只有事件日志一个来源,
    # 调用方照着发 `item_consume`。这里给的是"该扣什么",不是"扣好了"。
    consumed: dict[str, int] = field(default_factory=dict)
    refusal: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.refusal


def apply_affordance(
    affordance: Affordance,
    *,
    values: Mapping[str, float],
    world_values: Mapping[str, float] | None = None,
    me_values: Mapping[str, float] | None = None,
    held: Mapping[str, int] | None = None,
    now: int = 0,
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
    """
    holdings = {str(k): int(v) for k, v in (held or {}).items()}
    namespace: dict[str, Any] = {
        **{f"{WORLD_PREFIX}{k}": v for k, v in (world_values or {}).items()},
        **{f"{ME_PREFIX}{k}": v for k, v in (me_values or {}).items()},
        **{f"{HAVE_PREFIX}{k}": float(v) for k, v in holdings.items()},
        **dict(values),
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
                refusal=f"你现在做不了这件事:`{requirement}` 不成立",
                reason="incapable",
            )
    # 花掉一样东西**自带**一道"你得有"的门,不必作者再写一遍 `have_x >= n`:
    # 要花掉自己没有的东西,唯一讲得通的意思就是做不了。少写的那一遍正是要害 ——
    # 只写 `consumes` 的世界里她会用一包不存在的肥料把活干完,而库存扣不到负数,
    # 于是连账上都看不出来。
    for item_id, quantity in sorted(affordance.consumes.items()):
        on_hand = holdings.get(item_id, 0)
        if on_hand < quantity:
            return AffordanceOutcome(
                verb=affordance.verb,
                refusal=f"你手上的 {item_id} 不够:要 {quantity} 个,你有 {on_hand} 个",
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
                refusal=f"这会儿不行:`{condition}` 不成立",
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
        consumed=dict(affordance.consumes),
    )
