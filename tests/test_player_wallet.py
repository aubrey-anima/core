"""玩家的钱有几个答案。

经济的第一条设计是"**账本是投影**":余额和库存没有表,是 `payment` / `item_transfer`
事件折叠出来的,对账 = 重放。玩家钱包却站在这条规矩外面:

- `player_topup` 只改内存里的 `players[pid]["wallet"]`,**不发任何事件**;
- `player_buy` 拿这个内存数做门禁,却把花费**发成 payment 事件**。

于是同一个玩家有两个余额:内存里是"充值 − 花费",账本投影里是"**负的花费**"。
`World.balance()` 读投影,所以一个刚充过钱的玩家在那里显示为负数。两个数谁也不知道
对方存在,而且都不报错。

收敛的方向只能是账本:内存那份重启即失效,而账本是这个引擎对"钱"的定义。
"""
from __future__ import annotations

import pytest

from anima_world.api import World


@pytest.fixture
def world(tmp_path):
    w = World.open(str(tmp_path / "w.db"), force_mock_llm=True)
    w.config_set("economy.enabled", "true")
    yield w
    w.close()


def _holder(player_id: str) -> str:
    return f"player:{player_id}"


def test_topping_up_lands_in_the_ledger(world):
    world.player_move("p1", "cafe")
    world.player_topup("p1", 50)
    assert world.balance(_holder("p1")) == 50.0, "充值必须走账本,不然它就不存在"


def test_the_wallet_and_the_ledger_agree_after_a_purchase(world):
    world.player_move("p1", "cafe")
    world.player_topup("p1", 50)

    stock = world.shop("cafe")
    if not stock:
        pytest.skip("演示世界的咖啡店没有货架")
    item = stock[0]
    result = world.player_buy("p1", "cafe", item["item_id"])

    assert result["wallet"] == world.balance(_holder("p1")), (
        "买完之后钱包和账本必须是同一个数"
    )
    assert result["wallet"] == pytest.approx(50.0 - item["price"])


def test_a_purchase_you_cannot_afford_is_refused_by_the_ledger(world):
    world.player_move("p1", "cafe")
    stock = world.shop("cafe")
    if not stock:
        pytest.skip("演示世界的咖啡店没有货架")
    with pytest.raises(ValueError):
        world.player_buy("p1", "cafe", stock[0]["item_id"])
    assert world.balance(_holder("p1")) == 0.0, "被拒的交易不该在账本上留下痕迹"


def test_the_balance_survives_a_restart(tmp_path):
    """内存那份重启即失效 —— 而钱是世界的一部分,不是会话的一部分。"""
    db = str(tmp_path / "w.db")
    with World.open(db, force_mock_llm=True) as world:
        world.config_set("economy.enabled", "true")
        world.player_topup("p1", 30)

    with World.open(db, force_mock_llm=True) as reopened:
        assert reopened.balance(_holder("p1")) == 30.0


def test_topup_returns_the_same_number_the_ledger_holds(world):
    assert world.player_topup("p1", 10) == world.balance(_holder("p1"))
    assert world.player_topup("p1", 5) == 15.0
