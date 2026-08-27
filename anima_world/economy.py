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


# ── 出厂插件:钱包那一格(3.8.0 第 2 期 2d-①)────────────────────────────────

PLUGIN_ID = "economy"


def factory_plugin() -> dict:
    """出厂的 economy 插件声明 —— **这一版只有钱包一格**(补裁 ⑤)。

    🔴 **别把它读成"economy 已经是插件了"。** 搬过来的是**一个事实**:
    `economy.coins`,靠 `sources` 认领引擎已经在发的 `payment` 事件。
    其余三样一格没动,而且各有各的理由(FOR-STUDIO §3.43 逐条写着):

    - **`buy` / `eat` / `give` 仍然是内核路**。`give` 是**永远**不走 affordance
      (target 是人);`buy` / `eat` 是**这一期不走** —— `World.player_buy` 抛五种
      手写中文 `LookupError`,而 `perform_affordance` 是四类、措辞另一套,
      **形状和文字都对不上**,而玩家那一屏印的就是这些话;`eat` 更直白,
      它是 tick 循环里的 BT 动作,根本不在 `interact` 那条路上。
      **换调用路 ⇒ 逐字节从原理上不成立。**
    - **货架仍住 `shop_stock`**(真 hash),没有变成边:钱包是**加法**
      (量表里多一个键),货架是**换掉一个真键** —— 一份老包装进换了键的新引擎,
      `shop_stock` 会原样落键而没有一处再读它,**店里空了、退出码 0、日志干净**。
    - **`economy.enabled` 语义一个字没动**(只挡 eat / wages,不挡 buy / give)。

    ⚠️ **这一格搬得动、而账本那一半搬不动,理由是结构性的**:`Projection.balances`
    是一本**按任意持有者字符串**记的账(`__town__` / `shop:cafe` 也在里面),
    而插件的事实住在**有类型的载体**上(`bearer`)。镇上的金库和店铺是**持有者**,
    但不是**载体** —— 所以 `balance()` 这一轮照旧读账本,没有改成读这个事实。
    """
    return {
        "id": PLUGIN_ID, "version": "1.0.0", "label": "钱",
        "facts": {"coins": {
            "bearer": "actor", "shape": "number", "mode": "projected",
            "default": 0.0,
            # **不进感知块**:钱今天只进规划上下文(`_plan_context`),
            # 进了感知块就是行为变更,而这一期的闸要的是逐字节相同。
            "visibility": "hidden",
            "label": "钱",
            # 🔴 **进位跟着账本走**:两位。理由逐字写在 `projection._apply_payment`
            # 上 —— 二进制浮点存不下 0.1,而门禁读的是这个数。
            # 折到六位就是第二个钱包,它们只在小数第三位往后分家。
            "sources": [{"event": "payment", "amount": "amount",
                         "credit": "to", "debit": "from", "round": 2}],
        }},
    }
