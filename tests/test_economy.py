"""物品与经济(`economy.enabled`,默认关):账本即投影、价格漂移、吃饭买单、玩家购物。

原路线图的 v4.0 已并入首发 1.0.0,**没有** 4.x 引擎、也没有 db format 4 ——
物质层是随开关发的能力,db 格式仍是 1。
"""
from __future__ import annotations

from _needs import set_need
from _worldfile import read_seed_file, bundled_seed, open_world_at

import json
import pathlib

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
def world(tmp_path, bare_seed):
    # 素配:验的是创世安家费与默认货架,不是橱窗里作者写的那些(见 conftest)
    w = open_world_at(str(tmp_path / "w.db"), seed_path=bare_seed, force_mock_llm=True)
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
    set_need(world, "夏", "hunger", 0.05)   # 夏在咖啡店
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
    # 工资按真的上过多久班发(此前是每天无条件一份,于是整天睡觉的人和开了十小时
    # 店的人到手一样多)。这里直接把"上满一天"记上,单独验满勤 = 全额。
    world.scheduler._worked_ticks["遥"] = 288
    world.tick(1)
    assert world.balance("遥") == pytest.approx(balance_before + 20.0), "满勤拿全额"
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
    with open_world_at(db, force_mock_llm=True) as world:
        world.config_set("economy.enabled", "true")
        world.player_move("p1", "cafe")
        world.player_topup("p1", 50.0)
        world.player_buy("p1", "cafe", "coffee")
        balance = world.balance("player:p1")
    with open_world_at(db, force_mock_llm=True) as reopened:
        assert reopened.balance("player:p1") == pytest.approx(balance), "对账 = 重放"
        assert reopened.inventory("player:p1") == {"coffee": 1}


# ── 种子的物质层入口(#12) ─────────────────────────────────────────────────
# 物质层从首发起就有机制,却曾是唯一一个没有创世入口的子系统:小说里"她把父亲
# 的怀表一直带在身上"只能丢掉,或降级成一句记忆文本。


def _seed_with_material(tmp_path, bare_seed) -> str:
    # 建在素配种子上:这条验的是"作者写了就按作者的、没写的回落默认",而内置
    # 橱窗给每个角色都写了钱,第三个角色就再也测不到那个默认值(见 conftest)。
    seed = read_seed_file(bare_seed)
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


def test_seed_can_author_money_inventory_and_shelves(tmp_path, bare_seed):
    with open_world_at(str(tmp_path / "w.db"), seed_path=_seed_with_material(tmp_path, bare_seed),
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


def test_a_seed_that_ignores_the_material_layer_still_gets_the_demo_shelf(tmp_path, bare_seed):
    """缺字段 = 今天的行为。这条是 #12 承诺的宽容原则的另一半。

    必须用素配种子:内置橱窗**自己写了**货架,拿它来验"没写 stock 会怎样"是自相矛盾。
    """
    with open_world_at(str(tmp_path / "w.db"), seed_path=bare_seed, force_mock_llm=True) as world:
        assert {row["item_id"] for row in world.shop("cafe")} == {
            "coffee", "sandwich", "sketchbook"
        }


def test_broken_material_entries_are_dropped_one_by_one_not_fatally(tmp_path):
    """坏条目逐条丢弃、绝不拦启动 —— 种子只读进空库一次,半个世界比没世界更糟。"""
    from importlib import resources

    seed = bundled_seed()
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

    with open_world_at(str(tmp_path / "w.db"), seed_path=str(path), force_mock_llm=True) as world:
        agent = seed["agents"][0]["id"]
        assert world.inventory(agent) == {"ok": 2}, "好条目照常生效"
        assert world.balance(agent) == pytest.approx(30.0)
        assert "bad_kind" not in {row["item_id"] for row in world.shop("cafe")}


def test_seeded_inventory_survives_reopen(tmp_path, bare_seed):
    """随身物品走的是事件,不是表 —— 所以它和账本一样,对账即重放。"""
    db = str(tmp_path / "w.db")
    seed_path = _seed_with_material(tmp_path, bare_seed)
    with open_world_at(db, seed_path=seed_path, force_mock_llm=True) as world:
        owner = json.loads((tmp_path / "material_seed.json").read_text(
            encoding="utf-8"))["agents"][0]["id"]
        assert world.inventory(owner) == {"父亲的怀表": 1}
    with open_world_at(db, force_mock_llm=True) as reopened:
        assert reopened.inventory(owner) == {"父亲的怀表": 1}


def test_一样什么也补不回来的东西不是饭(tmp_path, bare_seed):
    """`kind == "consumable"` 单独一个判据太宽。

    一包肥、一管颜料也是"用一次就没"的东西,而 `eat` 挑的是**最便宜**的那个 ——
    于是一包 4 块的肥料会排在 6 块的咖啡前面,她把肥料当午饭吃掉,而且吃得很饱
    (需求照样归零)。判据得是"它补得回什么吗"。
    """
    seed = read_seed_file(bare_seed)
    seed["items"] = [
        {"id": "fertilizer", "name": "一包肥", "kind": "consumable", "base_price": 4.0},
        {"id": "coffee", "name": "咖啡", "kind": "consumable", "base_price": 6.0,
         "restores": {"hunger": 0.4}},
    ]
    seed["locations"][0].setdefault("stock", [])
    seed["locations"][0]["stock"] = [
        {"item": "fertilizer", "qty": 10, "price": 4.0},
        {"item": "coffee", "qty": 10, "price": 6.0},
    ]
    path = tmp_path / "fertilizer_seed.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    where = seed["locations"][0]["id"]
    with open_world_at(str(tmp_path / "w.db"), seed_path=str(path),
                       force_mock_llm=True) as world:
        meal = world.scheduler.economy_store.cheapest_meal(where)
        assert meal and meal["item_id"] == "coffee", "肥料被当成了饭"


def test_新世界折一次就够了不许折两遍(tmp_path, open_world, bare_seed):
    """**创世那条路把投影重折了一遍,却没挪水位。**

    `Scheduler.__init__` 建投影时日志还空着,水位于是停在 0;创世事件写完之后
    `__main__` 重折一次投影 —— 投影里有了那 20 多条,水位还是 0。于是下一次
    `catch_up_projection()`(`World.act()` 每次都调)把创世事件**再折一遍**:
    每个人的钱和随身物品当场翻倍。

    坏得最难查的是它的形状:只翻一次(第二次 catch_up 就正常了)、只在**创建
    这个世界的那个进程**里(重开一次读到的是对的)、日志本身一条不错。所以
    账面上永远看不出来 —— 事件重放出来的数和内存里的数不一样,而没人会去比。
    """
    seed = read_seed_file(bare_seed)
    seed["agents"][0]["money"] = 100
    seed["agents"][0]["inventory"] = [{"item": "怀表", "qty": 2}]
    who = seed["agents"][0]["id"]
    path = tmp_path / "genesis_fold.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")

    world = open_world(seed_path=str(path))
    inventories = world.scheduler._memory_projection.inventories
    assert world.balance(who) == 100.0, "创世本身就没播对"
    assert dict(inventories.get(who, {})) == {"怀表": 2}, "创世本身就没播对"

    # 水位必须已经在日志末尾。它是**因**,上面两条是果 —— 只断言果的话,
    # 换一种重折方式(比如把创世事件挪进 __init__)会让这条测试假绿。
    assert world.scheduler._projection_seq == max(e["seq"] for e in world.events())

    world.scheduler.catch_up_projection()
    assert world.balance(who) == 100.0, "创世的钱被折了两遍"
    assert dict(inventories.get(who, {})) == {"怀表": 2}, "创世的随身物品被折了两遍"
