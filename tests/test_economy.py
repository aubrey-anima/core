"""物品与经济(`economy.enabled`,默认关):账本即投影、价格漂移、吃饭买单、玩家购物。

原路线图的 v4.0 已并入首发 1.0.0,**没有** 4.x 引擎、也没有 db format 4 ——
物质层是随开关发的能力,db 格式仍是 1。
"""
from __future__ import annotations

import json

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


# ── 种子的物质层入口(#12) ─────────────────────────────────────────────────
# 物质层从首发起就有机制,却曾是唯一一个没有创世入口的子系统:小说里"她把父亲
# 的怀表一直带在身上"只能丢掉,或降级成一句记忆文本。


def _seed_with_material(tmp_path) -> str:
    from importlib import resources

    seed = json.loads(
        (resources.files("anima_world") / "world_seed.json").read_text(encoding="utf-8")
    )
    seed["items"] = [
        {"id": "coal", "name": "煤", "kind": "consumable", "base_price": 3.0,
         "restores": {"energy": 0.2}},
    ]
    seed["agents"][0]["inventory"] = [{"item": "父亲的怀表", "note": "从不离身"}]
    seed["agents"][0]["money"] = 120
    seed["agents"][1]["money"] = 0
    for location in seed["locations"]:
        if location["id"] == "cafe":
            location["stock"] = [{"item": "coal", "qty": 20},
                                 {"item": "手织围巾", "qty": 2, "price": 45}]
    path = tmp_path / "material_seed.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_seed_can_author_money_inventory_and_shelves(tmp_path):
    with World.open(str(tmp_path / "w.db"), seed_path=_seed_with_material(tmp_path),
                    force_mock_llm=True) as world:
        agents = [entry["id"] for entry in json.loads(
            (tmp_path / "material_seed.json").read_text(encoding="utf-8"))["agents"]]
        rich, broke, unset = agents[0], agents[1], agents[2]
        assert world.balance(rich) == pytest.approx(120.0)
        assert world.balance(broke) == pytest.approx(0.0), "写 0 就是一分没有,不是回落默认"
        assert world.balance(unset) == pytest.approx(30.0), "没写 money = 今天的行为"
        # 只被引用、没有定义的 id 自动补定义,所以信物直接可用。
        assert world.inventory(rich) == {"父亲的怀表": 1}
        shelf = {row["item_id"]: row for row in world.shop("cafe")}
        assert shelf["coal"]["name"] == "煤" and shelf["coal"]["quantity"] == 20
        assert shelf["手织围巾"]["price"] == pytest.approx(45.0)
        assert "coffee" not in shelf, (
            "种子一碰物质层,演示物品就该整体让位 —— 半真半假的货架比空货架更难查"
        )


def test_a_seed_that_ignores_the_material_layer_still_gets_the_demo_shelf(tmp_path):
    """缺字段 = 今天的行为。这条是 #12 承诺的宽容原则的另一半。"""
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        assert {row["item_id"] for row in world.shop("cafe")} == {
            "coffee", "sandwich", "sketchbook"
        }


def test_broken_material_entries_are_dropped_one_by_one_not_fatally(tmp_path):
    """坏条目逐条丢弃、绝不拦启动 —— 种子只读进空库一次,半个世界比没世界更糟。"""
    from importlib import resources

    seed = json.loads(
        (resources.files("anima_world") / "world_seed.json").read_text(encoding="utf-8")
    )
    seed["items"] = [
        {"id": "ok", "name": "好东西", "kind": "durable"},
        {"id": "bad_kind", "kind": "不存在的种类"},   # 违反 item_defs 的 CHECK
        {"name": "没有 id"},
    ]
    seed["agents"][0]["inventory"] = [
        {"item": "ok", "qty": 2},
        {"item": "ok", "qty": "三"},      # qty 不可转数
        {"item": "ok", "qty": 0},         # 非正数
        "整个条目不是对象",
    ]
    seed["agents"][0]["money"] = "很多"    # money 不可转数 → 回落默认
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")

    with World.open(str(tmp_path / "w.db"), seed_path=str(path), force_mock_llm=True) as world:
        agent = seed["agents"][0]["id"]
        assert world.inventory(agent) == {"ok": 2}, "好条目照常生效"
        assert world.balance(agent) == pytest.approx(30.0)
        assert "bad_kind" not in {row["item_id"] for row in world.shop("cafe")}


def test_seeded_inventory_survives_reopen(tmp_path):
    """随身物品走的是事件,不是表 —— 所以它和账本一样,对账即重放。"""
    db = str(tmp_path / "w.db")
    seed_path = _seed_with_material(tmp_path)
    with World.open(db, seed_path=seed_path, force_mock_llm=True) as world:
        owner = json.loads((tmp_path / "material_seed.json").read_text(
            encoding="utf-8"))["agents"][0]["id"]
        assert world.inventory(owner) == {"父亲的怀表": 1}
    with World.open(db, force_mock_llm=True) as reopened:
        assert reopened.inventory(owner) == {"父亲的怀表": 1}
