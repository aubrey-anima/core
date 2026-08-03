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
import sqlite3
import threading
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

    def is_empty(self) -> bool:
        return not (self.own or self.here or self.public)

    def to_dict(self) -> dict[str, Any]:
        return {"own": dict(self.own), "here": {k: dict(v) for k, v in self.here.items()},
                "public": dict(self.public)}

    def render(self, template: str = DEFAULT_PERCEPTION_BLOCK_TEMPLATE) -> str | None:
        """渲染成提示词里的一段。感知不到任何东西就返回 None —— 空块不进提示词。"""
        if self.is_empty():
            return None
        lines: list[str] = []
        if self.own:
            body = "、".join(f"{key} {_trim(value)}" for key, value in sorted(self.own.items()))
            lines.append(f"- 你自己:{body}")
        for owner, values in sorted(self.here.items()):
            name = self.labels.get(owner) or owner
            body = "、".join(f"{key} {_trim(value)}" for key, value in sorted(values.items()))
            lines.append(f"- 这里的{name}:{body}")
        if self.public:
            body = "、".join(f"{key} {_trim(value)}" for key, value in sorted(self.public.items()))
            lines.append(f"- 人人都知道:{body}")
        try:
            return template.format(lines="\n".join(lines))
        except (KeyError, IndexError, ValueError):
            logger.warning("perception 块渲染失败,这轮不带感知")
            return None


class VisibilityStore:
    """可见性声明 + 东西在哪。两张小表,访问经共享锁。"""

    def __init__(self, conn: sqlite3.Connection, lock: Any | None = None) -> None:
        self._conn = conn
        self._lock = lock if lock is not None else threading.RLock()

    # ── 可见性声明 ──────────────────────────────────────────────────────────

    def declare(self, owner_kind: str, key: str, visibility: str,
                label: str | None = None) -> None:
        if visibility not in VISIBILITIES:
            raise ValueError(f"可见性只能是 {list(VISIBILITIES)},收到 {visibility!r}")
        with self._lock:
            self._conn.execute(
                "INSERT INTO stock_visibility (owner_kind, key, visibility, label)"
                " VALUES (?, ?, ?, ?) ON CONFLICT(owner_kind, key) DO UPDATE SET"
                " visibility=excluded.visibility, label=excluded.label",
                (owner_kind, key, visibility, label),
            )
            self._conn.commit()

    def declarations(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT owner_kind, key, visibility, label FROM stock_visibility"
                " ORDER BY owner_kind, key"
            ).fetchall()
        return [
            {"kind": kind, "key": key, "visibility": visibility, "label": label}
            for kind, key, visibility, label in rows
        ]

    def rules_map(self) -> dict[tuple[str, str], str]:
        """`(种类, 量名) → 档`。一次读全,给一轮感知用(表很小)。"""
        return {
            (row["kind"], row["key"]): row["visibility"] for row in self.declarations()
        }

    # ── 东西在哪 ────────────────────────────────────────────────────────────

    def place(self, owner: str, location: str, label: str | None = None) -> None:
        """这棵树在咖啡店。`here` 档要靠它才知道"在场"。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO stock_places (owner, location, label) VALUES (?, ?, ?)"
                " ON CONFLICT(owner) DO UPDATE SET location=excluded.location,"
                " label=COALESCE(excluded.label, stock_places.label)",
                (owner, location, label),
            )
            self._conn.commit()

    def at(self, location: str) -> dict[str, str | None]:
        """这个地方有哪些东西 → `{owner: label}`。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT owner, label FROM stock_places WHERE location = ? ORDER BY owner",
                (location,),
            ).fetchall()
        return {owner: label for owner, label in rows}

    def labels(self) -> dict[str, str | None]:
        """每个量的**所有**位置标签 —— 搬家(换后端)时要一次拿全。

        补它是因为 Redis 版先有了它,而**接口不对等就不是替换**:少一个方法,
        调用方就会在某条路上撞见 AttributeError,而那条路多半是最少走的那条。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT owner, label FROM stock_places"
            ).fetchall()
        return {str(owner): label for owner, label in rows}

    def place_of(self, owner: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT location FROM stock_places WHERE owner = ?", (owner,)
            ).fetchone()
        return row[0] if row else None


def visibility_of(
    rules: dict[tuple[str, str], str], owner_kind: str, key: str
) -> str:
    """先精确匹配 (种类, 量名),再通配 (`*`, 量名),都没有就是 `hidden`。"""
    if (owner_kind, key) in rules:
        return rules[(owner_kind, key)]
    if (ANY_KIND, key) in rules:
        return rules[(ANY_KIND, key)]
    return HIDDEN


def perceive(
    *,
    agent_id: str,
    here: str,
    stock_store: Any,
    visibility: VisibilityStore,
    world_owner: str = "world",
) -> Perception:
    """这个角色此刻感知到什么。纯读,无 LLM。

    三档各查一次:自己身上的、同地那些东西的、全局公开的。没有任何声明时三次查询
    都会落空,整块为空 —— 所以未声明的世界在这里几乎不花代价。
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
        for owner, label in visibility.at(here).items():
            if owner == own_owner:
                continue
            kind = owner.split(":", 1)[0] if ":" in owner else owner
            seen = {
                key: value
                for key, value in stock_store.of(owner).items()
                if visibility_of(rules, kind, key) in (HERE, PUBLIC)
            }
            if seen:
                result.here[owner] = seen
                if label:
                    result.labels[owner] = label

    for key, value in stock_store.of(world_owner).items():
        if visibility_of(rules, "world", key) == PUBLIC:
            result.public[key] = value

    return result
