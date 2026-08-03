"""四张小表的存储缝:需求 / 小团体 / 反思水位 / 经济。

这四处和别的表不一样:它们是**模块级函数直接吃 `sqlite3.Connection`**
(`needs.load(conn, …)` / `cliques.load_cliques(conn)` / `economy.take_stock(conn, …)`),
而不是 store 类。于是"换个后端"这件事对它们无从下手 —— 没有可替换的东西。

这个模块只做一件事:**把那些函数收进小 store 类,让存储访问有个缝**。

两条纪律:

1. **SQLite 版一行逻辑都不重写**,全部委托现有函数。价格漂移曲线、遗忘式的需求
   结算、小团体的并查集 —— 这些是世界的规则,不是存储;把它们抄进新类,就等于给
   一条规则开了第二份实现,而两份规则迟早算出不同的世界。
2. **接口按"世界要问什么"来定**,不按 SQL 长什么样。`cheapest_meal` 是"这儿最便宜
   的一顿饭是什么",不是"select 三个表 join 一下" —— 后者换不了后端。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from anima_world import cliques as cliques_mod
from anima_world import economy as economy_mod
from anima_world import needs as needs_mod


class NeedsStore:
    """她的身体:energy / hunger / social 三条曲线的当前值与结算时刻。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def load(self, agent_id: str) -> dict[str, float]:
        return needs_mod.load(self._conn, agent_id)

    def persist(self, agent_id: str, values: dict[str, Any], tick: int) -> None:
        needs_mod.persist(self._conn, agent_id, values, tick)


class CliqueStore:
    """小团体。**是派生缓存,不是历史** —— 重算即真相,所以写是全量重写。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def store(self, groups: list[list[str]], tick: int) -> None:
        cliques_mod.store_cliques(self._conn, groups, tick)

    def load(self) -> list[dict[str, Any]]:
        return cliques_mod.load_cliques(self._conn)


class ReflectionStore:
    """反思水位:上次反思之后累积了多少"值得想一想"的分量。

    也是当前值,不是历史 —— 反思本身是 `memory_seed` 事件,重放能重建。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, agent_id: str) -> float:
        row = self._conn.execute(
            "SELECT accumulated_importance FROM reflection_state WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        return float(row[0] or 0.0) if row else 0.0

    def set(self, agent_id: str, value: float) -> None:
        """水位落盘(周期性 checkpoint 用)。"""
        self._conn.execute(
            "INSERT INTO reflection_state (agent_id, accumulated_importance)"
            " VALUES (?, ?) ON CONFLICT(agent_id) DO UPDATE SET"
            " accumulated_importance = excluded.accumulated_importance",
            (agent_id, float(value)),
        )
        self._conn.commit()

    def add(self, agent_id: str, amount: float) -> float:
        fresh = self.get(agent_id) + float(amount)
        self._conn.execute(
            "INSERT INTO reflection_state (agent_id, accumulated_importance)"
            " VALUES (?, ?) ON CONFLICT(agent_id) DO UPDATE SET"
            " accumulated_importance = excluded.accumulated_importance",
            (agent_id, fresh),
        )
        self._conn.commit()
        return fresh

    def reset(self, agent_id: str, tick: int) -> None:
        self._conn.execute(
            "INSERT INTO reflection_state (agent_id, accumulated_importance, last_reflection_tick)"
            " VALUES (?, 0, ?) ON CONFLICT(agent_id) DO UPDATE SET"
            " accumulated_importance = 0, last_reflection_tick = excluded.last_reflection_tick",
            (agent_id, int(tick)),
        )
        self._conn.commit()


class EconomyStore:
    """货架与物品。接口按**世界要问什么**来定,不按 SQL 长什么样。

    `cheapest_meal` 是"这儿最便宜的一顿饭是什么" —— 换成任何后端这个问题都成立;
    而"三表 join 取 min" 换不了后端。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def seed_defaults(self) -> None:
        economy_mod.seed_defaults(self._conn)

    def seed_authored(self, *args: Any, **kwargs: Any) -> Any:
        return economy_mod.seed_authored(self._conn, *args, **kwargs)

    def cheapest_meal(self, location_id: str) -> dict[str, Any] | None:
        return economy_mod.cheapest_meal(self._conn, location_id)

    def take_stock(self, location_id: str, item_id: str, qty: int = 1) -> bool:
        return economy_mod.take_stock(self._conn, location_id, item_id, qty)

    def daily_price_pass(self, sales: dict[tuple[str, str], int]) -> None:
        economy_mod.daily_price_pass(self._conn, sales)

    def restores_of(self, item_id: str) -> dict[str, float]:
        """吃了它能恢复什么。**给 store 一个方法,而不是让调用方直查表** ——
        直查的那一处会在换后端时静默失效:世界照跑,只是吃东西不再补需求了。"""
        import json

        row = self._conn.execute(
            "SELECT restores FROM item_defs WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None or not row[0]:
            return {}
        try:
            return dict(json.loads(row[0]) or {})
        except (TypeError, ValueError):
            return {}

    def items(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, name, kind, base_price, restores FROM item_defs ORDER BY id"
        ).fetchall()
        return [
            {"id": r[0], "name": r[1], "kind": r[2],
             "base_price": float(r[3] or 0), "restores": r[4]}
            for r in rows
        ]

    def shelves(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT location_id, item_id, quantity, price FROM shop_stock"
            " ORDER BY location_id, item_id"
        ).fetchall()
        return [
            {"location_id": r[0], "item_id": r[1],
             "quantity": int(r[2] or 0), "price": float(r[3] or 0)}
            for r in rows
        ]
