"""economy-v4:物品、钱、店铺与价格漂移 —— 世界的物质层。

账本原则:**余额与库存是事件的投影**(`payment` / `item_transfer` /
`item_consume`),对账 = 重放,天生防复制品 bug。表里只放定义与当前值
(data-plane):`item_defs` 是作者数据,`shop_stock` 是店铺当前货架。

价格漂移是每世界日一次的纯函数结算:卖得多涨、卖不动跌,
夹在 [base×0.25, base×4] 之间 —— 你把咖啡买断,明天真的更贵。

默认关闭(config `economy.enabled`)。

这个模块是**纯数据与算法**(默认物品、价格漂移曲线、创世常量)。存取在
`anima_world.redis_state.RedisEconomyStore`;SQLite 版存取函数已随 world.db 层
退役,只有 `take_stock` 还留着一个吃连接的签名(api.py 的玩家购物路径仍在用,
它只要求 `execute`/`commit` 两个方法,鸭子类型)。
"""

from __future__ import annotations

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

GENESIS_STIPEND = 30.0  # 创世安家费,种子可按人覆写(world_seed 的 agents[].money)
ITEM_KINDS = ("consumable", "durable", "artwork")  # item_defs.kind 的 CHECK 约束


def drift_price(
    base: float, price: float, sold: int, restocked: int,
    *, k: float = 0.08, floor: float = 0.25, cap: float = 4.0,
) -> float:
    """供需慢漂移:压力 = (卖出-补货)/(卖出+补货),七三开平滑逼近目标价。"""
    pressure = (sold - restocked) / max(sold + restocked, 1)
    target = price * (1 + k * pressure)
    return max(base * floor, min(base * cap, 0.7 * price + 0.3 * target))
