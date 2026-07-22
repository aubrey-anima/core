"""物品与经济(v4.0 / db 4):账本即投影、价格漂移、吃饭买单、玩家购物。"""
from __future__ import annotations

import pytest

from anima_world.api import World
from anima_world.economy import TOWN, drift_price
from anima_world.projection import project_events
from anima_world.types import Event


def _ev(seq, type_, payload, who=None):
    return Event(seq=seq, ts=seq, type=type_, loc=None, payload=payload, who=who)


def test_ledger_is_a_projection_of_events():
    proj = project_events([
        _ev(1, "payment", {"from": TOWN, "to": "夏", "amount": 30.0}),
        _ev(2, "payment", {"from": "夏", "to": TOWN, "amount": 12.0}),
        _ev(3, "item_transfer", {"from": "shop:cafe", "to": "夏", "item_id": "sandwich", "qty": 2}),
        _ev(4, "item_consume", {"who": "夏", "item_id": "sandwich"}),
        _ev(5, "payment", {"from": "夏", "to": TOWN, "amount": -5.0}),  # 负数金额必须被拒
    ])
    assert proj.balances["夏"] == pytest.approx(18.0)
    assert proj.inventories["夏"] == {"sandwich": 1}


def test_price_drifts_up_when_demand_outruns_restock():
    hot = drift_price(10.0, 10.0, sold=9, restocked=3)
    cold = drift_price(10.0, 10.0, sold=0, restocked=3)
    assert hot > 10.0 > cold
    # 永远夹在 [base×0.25, base×4]
    assert drift_price(10.0, 200.0, sold=99, restocked=0) <= 40.0
    assert drift_price(10.0, 0.01, sold=0, restocked=99) >= 2.5


@pytest.fixture
def world(tmp_path):
    w = World.open(str(tmp_path / "w.db"), force_mock_llm=True)
    w.config_set("economy.enabled", "true")
    yield w
    w.close()


def test_genesis_stipend_and_default_shelf(world):
    assert world.balance("夏") == pytest.approx(30.0), "创世安家费"
    shelf = {row["item_id"]: row for row in world.shop("cafe")}
    assert "coffee" in shelf and shelf["coffee"]["quantity"] > 0


def test_hungry_agent_at_cafe_buys_a_meal(world):
    world.config_set("needs.enabled", "true")
    world.tick(1)
    brain = world.scheduler.agents["夏"]  # 夏在咖啡店
    brain.agent.blackboard.write("need.hunger", 0.05)
    before_stock = {r["item_id"]: r["quantity"] for r in world.shop("cafe")}
    before_balance = world.balance("夏")
    world.tick(1)
    payments = [
        ev for ev in world.events()
        if ev.get("type") == "payment" and ev["payload"].get("from") == "夏"
    ]
    assert payments and payments[-1]["payload"]["reason"].startswith("meal:")
    assert world.balance("夏") < before_balance
    after_stock = {r["item_id"]: r["quantity"] for r in world.shop("cafe")}
    assert sum(after_stock.values()) == sum(before_stock.values()) - 1, "货架必须少一件"


def test_day_rollover_pays_wages_and_drifts_prices(world):
    coffee_before = next(r for r in world.shop("cafe") if r["item_id"] == "coffee")
    balance_before = world.balance("遥")
    world.scheduler.clock = 287  # 日界前一 tick
    world.tick(1)
    assert world.balance("遥") == pytest.approx(balance_before + 20.0), "日切发工资"
    coffee_after = next(r for r in world.shop("cafe") if r["item_id"] == "coffee")
    assert coffee_after["quantity"] > coffee_before["quantity"], "日切补货"
    assert coffee_after["price"] < coffee_before["price"], "没人买,价格该跌"


def test_player_buy_moves_money_and_item(world):
    world.player_move("p1", "cafe")
    world.player_topup("p1", 50.0)
    receipt = world.player_buy("p1", "cafe", "sandwich")
    assert receipt["wallet"] == pytest.approx(50.0 - receipt["price"])
    assert world.inventory("player:p1") == {"sandwich": 1}
    assert world.balance(TOWN) != 0.0
    world.player_move("p2", "cafe")
    world.player_topup("p2", 1.0)
    with pytest.raises(ValueError):
        world.player_buy("p2", "cafe", "sketchbook")  # 钱包不够必须拒绝


def test_ledger_survives_reopen_via_replay(tmp_path):
    db = str(tmp_path / "w.db")
    with World.open(db, force_mock_llm=True) as world:
        world.config_set("economy.enabled", "true")
        world.player_move("p1", "cafe")
        world.player_topup("p1", 50.0)
        world.player_buy("p1", "cafe", "coffee")
        balance = world.balance("player:p1")
    with World.open(db, force_mock_llm=True) as reopened:
        assert reopened.balance("player:p1") == pytest.approx(balance), "对账 = 重放"
        assert reopened.inventory("player:p1") == {"coffee": 1}
