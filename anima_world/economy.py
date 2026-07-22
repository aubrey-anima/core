"""economy-v4:物品、钱、店铺与价格漂移 —— 世界的物质层。

账本原则:**余额与库存是事件的投影**(`payment` / `item_transfer` /
`item_consume`),对账 = 重放,天生防复制品 bug。表里只放定义与当前值
(data-plane):`item_defs` 是作者数据,`shop_stock` 是店铺当前货架。

价格漂移是每世界日一次的纯函数结算:卖得多涨、卖不动跌,
夹在 [base×0.25, base×4] 之间 —— 你把咖啡买断,明天真的更贵。

默认关闭(config `economy.enabled`)。
"""

from __future__ import annotations

import sqlite3
from typing import Any

TOWN = "__town__"  # 小镇金库:工资的来源、消费的去向,允许负债

# 首启种进 item_defs / cafe 货架的默认物品(空表才种,作者数据不覆盖)
DEFAULT_ITEMS = [
    ("coffee", "咖啡", "consumable", 6.0, {"hunger": 0.15, "energy": 0.1}),
    ("sandwich", "三明治", "consumable", 12.0, {"hunger": 0.5}),
    ("sketchbook", "速写本", "durable", 25.0, {}),
]
DEFAULT_STOCK = [("cafe", "coffee", 12), ("cafe", "sandwich", 8), ("cafe", "sketchbook", 3)]

RESTOCK_PER_DAY = 3
MAX_STOCK = 20


def drift_price(
    base: float, price: float, sold: int, restocked: int,
    *, k: float = 0.08, floor: float = 0.25, cap: float = 4.0,
) -> float:
    """供需慢漂移:压力 = (卖出-补货)/(卖出+补货),七三开平滑逼近目标价。"""
    pressure = (sold - restocked) / max(sold + restocked, 1)
    target = price * (1 + k * pressure)
    return max(base * floor, min(base * cap, 0.7 * price + 0.3 * target))


def seed_defaults(conn: sqlite3.Connection) -> None:
    """空表才种(populated 表是作者/运行数据,绝不覆盖)。"""
    import json

    (items,) = conn.execute("SELECT COUNT(*) FROM item_defs").fetchone()
    if items == 0:
        for item_id, name, kind, base_price, restores in DEFAULT_ITEMS:
            conn.execute(
                "INSERT INTO item_defs (id, name, kind, base_price, restores) VALUES (?, ?, ?, ?, ?)",
                (item_id, name, kind, base_price, json.dumps(restores, ensure_ascii=False)),
            )
    (stock,) = conn.execute("SELECT COUNT(*) FROM shop_stock").fetchone()
    if stock == 0:
        for location_id, item_id, quantity in DEFAULT_STOCK:
            row = conn.execute(
                "SELECT base_price FROM item_defs WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                continue
            conn.execute(
                "INSERT INTO shop_stock (location_id, item_id, quantity, price) VALUES (?, ?, ?, ?)",
                (location_id, item_id, quantity, float(row[0])),
            )
    conn.commit()


def cheapest_meal(conn: sqlite3.Connection, location_id: str) -> dict[str, Any] | None:
    """某地货架上最便宜的、还有货的吃食。没有就 None(照样能吃,只是不花钱)。"""
    row = conn.execute(
        "SELECT s.item_id, s.price, s.quantity, d.name FROM shop_stock s"
        " JOIN item_defs d ON d.id = s.item_id"
        " WHERE s.location_id = ? AND s.quantity > 0 AND d.kind = 'consumable'"
        " ORDER BY s.price ASC LIMIT 1",
        (location_id,),
    ).fetchone()
    if row is None:
        return None
    return {"item_id": row[0], "price": float(row[1]), "quantity": int(row[2]), "name": row[3]}


def take_stock(conn: sqlite3.Connection, location_id: str, item_id: str, qty: int = 1) -> bool:
    cur = conn.execute(
        "UPDATE shop_stock SET quantity = quantity - ? WHERE location_id = ? AND item_id = ?"
        " AND quantity >= ?",
        (qty, location_id, item_id, qty),
    )
    conn.commit()
    return cur.rowcount > 0


def daily_price_pass(conn: sqlite3.Connection, sales: dict[tuple[str, str], int]) -> None:
    """日切:补货 + 按当日销量漂移价格。纯 SQL/算术,tick 帧内零 LLM。"""
    rows = conn.execute(
        "SELECT s.location_id, s.item_id, s.quantity, s.price, d.base_price"
        " FROM shop_stock s JOIN item_defs d ON d.id = s.item_id"
    ).fetchall()
    for location_id, item_id, quantity, price, base_price in rows:
        sold = sales.get((location_id, item_id), 0)
        conn.execute(
            "UPDATE shop_stock SET quantity = ?, price = ? WHERE location_id = ? AND item_id = ?",
            (
                min(MAX_STOCK, int(quantity) + RESTOCK_PER_DAY),
                round(drift_price(float(base_price), float(price), sold, RESTOCK_PER_DAY), 2),
                location_id, item_id,
            ),
        )
    conn.commit()
