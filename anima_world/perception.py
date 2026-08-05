"""认知层:世界的量里,**她感知得到哪些**。

`stocks` 是客观状态(树多高、矿还剩多少、她功力多少)。但客观存在 ≠ 她知道 ——
这两层混成一层,就会得到一个**无所不知的角色**:她随口说出矿的确切储量、别人暗中
的恨意、隔着半个地图那棵树的高度。那比"她什么都不知道"糟得多:不知道最坏是她没
注意到(玩家看得见她没注意),而知道得太多是**当场破戏,而且不可挽回**。

所以这一层的默认值定死:**没声明 = 感知不到。** 作者要哪个量被看见,就显式声明它
是哪一档:

| 档 | 意思 | 例子 |
|---|---|---|
| `self`   | 只有主人自己知道 | 她自己的功力、饿不饿 |
| `here`   | 得在同一个地方才知道 | 这棵树多高(要 `stock_places` 说它在哪) |
| `public` | 人人皆知 | 季节、粮价、战争 |
| `hidden` | 谁也不知道(默认) | 矿的真实储量、暗中的恨意 |

声明按 `(owner 种类, 量名)` 走,`*` 是通配 —— 因为可见性是"这类量是什么性质"的属性,
不是每个实例的属性:所有树的 `size` 都是在场可见,不必一棵棵写。

**声明本身就是开关。** 没有 `perception.enabled` 这种配置项:一个没声明过任何可见性
的世界,这一层是空的、不进提示词、不花一个 token。要点亮就去声明,粒度天然比一个
全局开关细。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

SELF, HERE, PUBLIC, HIDDEN = "self", "here", "public", "hidden"
VISIBILITIES = (SELF, HERE, PUBLIC, HIDDEN)
ANY_KIND = "*"

# 提示词里那一段。三行分别是"你自己/你这儿/人人都知道",空的那档不出现。
DEFAULT_PERCEPTION_BLOCK_TEMPLATE = (
    "【你此刻感觉到的】\n{lines}\n"
    "这些是你**确实知道**的事,可以自然地提到;没写在这儿的你就不知道,不要猜、"
    "也不要编具体数字。"
)


def _trim(value: float) -> str:
    """`8.0` → `8`,`0.35` → `0.35` —— 提示词里不要出现 `8.000000000001`。"""
    rounded = round(float(value), 3)
    return str(int(rounded)) if rounded == int(rounded) else str(rounded)


@dataclass
class Perception:
    """她此刻感知到的东西。分三档,便于渲染成人话,也便于宿主自己用。"""

    own: dict[str, float] = field(default_factory=dict)
    here: dict[str, dict[str, float]] = field(default_factory=dict)
    public: dict[str, float] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    # 本体给的两样:一行人话、她能对它做什么。没有本体的世界这两个是空的。
    glosses: dict[str, str] = field(default_factory=dict)
    verbs: dict[str, list[str]] = field(default_factory=dict)
    units: dict[str, dict[str, str]] = field(default_factory=dict)
    # 这里还有多少样东西没带进来。**必须说出来** —— 截断了却不吭声,等于让她在一个
    # "她以为只有三棵树"的世界里做决定,而她永远不会知道自己被骗了。
    overflow: int = 0

    def is_empty(self) -> bool:
        return not (self.own or self.here or self.public)

    def to_dict(self) -> dict[str, Any]:
        return {"own": dict(self.own), "here": {k: dict(v) for k, v in self.here.items()},
                "public": dict(self.public), "overflow": self.overflow}

    def describe_here(self, owner: str) -> str:
        """`门口那棵老橡树[tree:harbor_oak](树冠遮住半条街):树高 3.2米。可以照料、收取产出`"""
        name = self.labels.get(owner) or owner
        gloss = self.glosses.get(owner)
        units = self.units.get(owner, {})
        body = "、".join(
            f"{key} {_trim(value)}{units.get(key, '')}"
            for key, value in sorted(self.here.get(owner, {}).items())
        )
        verbs = self.verbs.get(owner)
        # id 只在她**真能对它做点什么**时才露出来:那是 `interact` 的参数,不是装饰。
        # 一个只能看不能碰的东西带上 `tree:harbor_oak` 这种字符串,纯粹是提示词噪音,
        # 还会诱她去调一个必然被拒的调用。
        line = f"{name}[{owner}]" if verbs and name != owner else name
        line = f"{line}({gloss})" if gloss else line
        line = f"{line}:{body}" if body else line
        # 能力放在最后,因为它是**她下一步能做的事** —— 属性她只会念,动词她会用。
        return f"{line}。可以{'、'.join(verbs)}" if verbs else line

    def render(self, template: str = DEFAULT_PERCEPTION_BLOCK_TEMPLATE) -> str | None:
        """渲染成提示词里的一段。感知不到任何东西就返回 None —— 空块不进提示词。"""
        if self.is_empty():
            return None
        lines: list[str] = []
        if self.own:
            body = "、".join(f"{key} {_trim(value)}" for key, value in sorted(self.own.items()))
            lines.append(f"- 你自己:{body}")
        for owner in sorted(self.here):
            lines.append(f"- 这里的{self.describe_here(owner)}")
        if self.overflow:
            lines.append(f"- 这里还有 {self.overflow} 样别的东西,你没细看")
        if self.public:
            body = "、".join(f"{key} {_trim(value)}" for key, value in sorted(self.public.items()))
            lines.append(f"- 人人都知道:{body}")
        try:
            return template.format(lines="\n".join(lines))
        except (KeyError, IndexError, ValueError):
            logger.warning("perception 块渲染失败,这轮不带感知")
            return None


def visibility_of(
    rules: dict[tuple[str, str], str], owner_kind: str, key: str
) -> str:
    """先精确匹配 (种类, 量名),再通配 (`*`, 量名),都没有就是 `hidden`。"""
    if (owner_kind, key) in rules:
        return rules[(owner_kind, key)]
    if (ANY_KIND, key) in rules:
        return rules[(ANY_KIND, key)]
    return HIDDEN


def why_not_perceivable(
    rules: dict[tuple[str, str], str],
    *,
    agent_id: str,
    here: str,
    owner: str,
    key: str,
    place_of: Any = None,
) -> str:
    """她此刻**感知不到** `(owner, key)` 的理由;感知得到就返回空串。

    这是 `perceive()` 那三档的逐项版本,给行为树的 `StockCondition` 用。三个理由
    区别很大,所以分开说而不是返回一个 bool:

    | 理由 | 意思 | 作者该怎么办 |
    |---|---|---|
    | `hidden` | 这个量根本没声明过可见性 | **写错了** —— 这条分支永不触发,要吼一声 |
    | `not_mine` | 声明成 `self`,而这是别人的量 | 写错了,同上 |
    | `elsewhere` | 声明成 `here`,而她此刻不在那儿 | **正常** —— 走过去就看得见了 |

    前两条是静态的(世界怎么跑都不会变),第三条随她走动而变。把它们混成一个
    bool,调用方就只能要么对正常情况刷屏、要么对写错的声明一声不吭。
    """
    kind = owner.split(":", 1)[0] if ":" in owner else owner
    level = visibility_of(rules, kind, key)
    if level == HIDDEN:
        return "hidden"
    if owner == f"agent:{agent_id}":
        return ""  # 自己身上的量:声明成哪一档,她自己都知道
    if level == PUBLIC:
        return ""
    if level == SELF:
        return "not_mine"
    where = place_of(owner) if place_of is not None else None
    return "" if here and where == here else "elsewhere"


def perceive(
    *,
    agent_id: str,
    here: str,
    stock_store: Any,
    visibility: Any,
    world_owner: str = "world",
    ontology: Any = None,
    default_budget: int = 5,
) -> Perception:
    """这个角色此刻感知到什么。纯读,无 LLM。

    `visibility` 是鸭子类型(`RedisVisibilityStore`),要求 `rules_map` / `at` 两个方法;
    SQLite 版 `VisibilityStore` 已退役。

    三档各查一次:自己身上的、同地那些东西的、全局公开的。没有任何声明时三次查询
    都会落空,整块为空 —— 所以未声明的世界在这里几乎不花代价。

    `ontology` 给的是两样**不改变她看得见什么**的东西:同地那一档的上限(按种类),
    以及每样东西的一行人话与能力动词。没有本体的世界照旧跑,只是列表按
    `default_budget` 封顶、没有人话也没有动词。
    """
    rules = visibility.rules_map()
    result = Perception()
    if not rules:
        return result

    own_owner = f"agent:{agent_id}"
    for key, value in stock_store.of(own_owner).items():
        level = visibility_of(rules, "agent", key)
        if level in (SELF, HERE, PUBLIC):
            # 自己的量:`self` 当然看得见;声明成 here/public 的更宽,也看得见。
            result.own[key] = value

    if here:
        # **按种类分别封顶。** 有上限本身不是可选的:这一格里站着一万棵树的话,
        # 它们住 Redis 还是 MySQL 都一样把提示词撑爆 —— 存储分层保护不了提示词,
        # 所以有界性归这里(渲染器),不归存储。
        # 排序按 owner id:要的是**确定**,不是"最有意思的那几个"(那需要一个
        # 谁也说不清的重要度,而不确定的截断会让同一个世界每次给她不同的现实)。
        by_kind: dict[str, list[tuple[str, str | None]]] = {}
        for owner, label in sorted(visibility.at(here).items()):
            if owner == own_owner:
                continue
            by_kind.setdefault(owner.split(":", 1)[0] if ":" in owner else owner, []).append(
                (owner, label)
            )
        for kind, members in by_kind.items():
            budget = ontology.budget_of(members[0][0]) if ontology is not None else default_budget
            for owner, label in members[:budget]:
                seen = {
                    key: value
                    for key, value in stock_store.of(owner).items()
                    if visibility_of(rules, kind, key) in (HERE, PUBLIC)
                }
                if not seen:
                    continue
                result.here[owner] = seen
                if label:
                    result.labels[owner] = label
                if ontology is not None:
                    gloss, verbs = ontology.describe(owner)
                    if gloss:
                        result.glosses[owner] = gloss
                    if verbs:
                        result.verbs[owner] = verbs
                    units = ontology.units_of(owner)
                    if units:
                        result.units[owner] = units
            result.overflow += max(0, len(members) - budget)

    for key, value in stock_store.of(world_owner).items():
        if visibility_of(rules, "world", key) == PUBLIC:
            result.public[key] = value

    return result
