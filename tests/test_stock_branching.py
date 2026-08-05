"""排班能按世界的量分支 —— 而且只按**她知道的**量分支。

钟点排班("八点到六点半照看店里")是这个引擎在这之前能表达的全部,而人不是那样活的:
面粉见底了才去进货,树长到够高了才去收。`TimeWindow` 表达不了这一类,`Condition`
只比黑板上的相等 —— 于是"按世界的状态决定"整个缺席,只能靠一个 LLM 规划器去补,
而那条路又贵又不该管这么钝的事。

这里验四件事,每一件都对着一种"能跑但给错东西":

1. 量没 settle 上来时 FAILURE —— 没开这一层的世界逐 tick 行为不变
2. 打错比较符**当场抛**,不是静默地永不触发
3. **她感知不到的量进不了黑板** —— 一条读得到"隔着半个地图那棵树的高度"的排班,
   等于让她用她不可能知道的事做决定,而那连一行提示词都不留
4. 端到端:橱窗世界里她真的走过去照料了那棵树,树真的长高了
"""
from __future__ import annotations

from _worldfile import open_world_at

import json
import logging

import pytest

from anima_world.bt_nodes import Blackboard, Status, StockCondition
from anima_world.perception import why_not_perceivable


# ── 叶子本身 ────────────────────────────────────────────────────────────────


def test_量还没上黑板时不许炸也不许成功():
    """这一层没开、第一个 tick 之前、她此刻感知不到 —— 三种情况同一个答案。"""
    leaf = StockCondition("tree:oak", "树高", "<", 6)
    assert leaf.tick(Blackboard()) == Status.FAILURE

    bb = Blackboard()
    bb.write("stock.tree:oak.树高", None)
    assert leaf.tick(bb) == Status.FAILURE


def test_量到了就成立():
    leaf = StockCondition("tree:oak", "树高", "<", 6)
    bb = Blackboard()
    bb.write("stock.tree:oak.树高", 3.2)
    assert leaf.tick(bb) == Status.SUCCESS
    bb.write("stock.tree:oak.树高", 6.0)
    assert leaf.tick(bb) == Status.FAILURE


@pytest.mark.parametrize(
    "op,current,expected",
    [("<", 1, Status.SUCCESS), ("<=", 2, Status.SUCCESS), (">", 3, Status.SUCCESS),
     (">=", 2, Status.SUCCESS), ("==", 2, Status.SUCCESS), ("!=", 2, Status.FAILURE)],
)
def test_六个比较符都真的比对了(op, current, expected):
    bb = Blackboard()
    bb.write("stock.w.x", current)
    assert StockCondition("w", "x", op, 2).tick(bb) == expected


def test_打错比较符当场抛而不是永不触发():
    """静默的话:世界照跑、日志干净、作者以为自己写对了,而这条分支一次也不会来。"""
    with pytest.raises(ValueError) as exc:
        StockCondition("tree:oak", "树高", "=<", 6)
    assert "=<" in str(exc.value), str(exc.value)


# ── 感知这一道闸 ────────────────────────────────────────────────────────────


_RULES = {("tree", "树高"): "here", ("world", "季节"): "public", ("agent", "功力"): "self"}


def _place_of(owner):
    return {"tree:near": "cafe", "tree:far": "workshop"}.get(owner)


@pytest.mark.parametrize(
    "owner,key,reason",
    [
        ("tree:near", "树高", ""),          # 同地:看得见
        ("tree:far", "树高", "elsewhere"),  # 隔着半个地图:看不见,但走过去就行
        ("world", "季节", ""),              # 人人皆知
        ("tree:near", "含碳量", "hidden"),   # 压根没声明过 —— 作者写错了
        ("agent:别人", "功力", "not_mine"),  # 别人的私事
        ("agent:我", "功力", ""),            # 自己的:声明成哪一档她都知道
    ],
)
def test_感知不到的理由要分得开(owner, key, reason):
    """三个理由区别很大:前两个是作者写错了(要吼),`elsewhere` 是正常的。"""
    assert why_not_perceivable(
        _RULES, agent_id="我", here="cafe", owner=owner, key=key, place_of=_place_of
    ) == reason


# ── 端到端:种子 → 树 → 感知 → 动手 → 量变了 ───────────────────────────────


_SEED = {
    "locations": [
        {"id": "cafe", "name": "咖啡店", "description": "拐角那家"},
        {"id": "yard", "name": "后院", "description": "堆着几只空木箱"},
    ],
    "agents": [{"id": "甲", "name": "甲", "location": "cafe", "personality": "安静"}],
    "kinds": [{
        "id": "tree",
        "gloss": "一棵树",
        "quantities": {
            "树高": {"default": 1.0, "visibility": "here", "unit": "米"},
            "最大树高": 12,
        },
        "affordances": {"tend": {"when": ["树高 < 最大树高"],
                                 "set": {"树高": "min(树高 + 0.5, 最大树高)"}}},
    }],
    "entities": [{"id": "tree:oak", "name": "老橡树", "location": "cafe"}],
    "stocks": [{"owner": "tree:oak", "values": {"树高": 2.0, "最大树高": 12}}],
}


def _seed_world(tmp_path, seed):
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return open_world_at(str(tmp_path / "w.db"), seed_path=str(path), force_mock_llm=True)


def _with_duty(**gate):
    seed = json.loads(json.dumps(_SEED))
    seed["agents"][0]["duties"] = [{
        "name": "tend_oak", "start": "00:00", "end": "23:59", "kind": "interact",
        "params": {"target": "tree:oak", "verb": "tend"},
        "when_stock": {"owner": "tree:oak", "key": "树高", "op": "<", "value": 4, **gate},
    }]
    return seed


def _height(world):
    return world.scheduler.stock_store.of("tree:oak").get("树高")


def test_她照着排班真的把树浇高了(tmp_path):
    """全部意义在这一条:量 → 分支 → 动作 → 量变了 → 分支自己关掉。"""
    with _seed_world(tmp_path, _with_duty()) as world:
        assert _height(world) == 2.0
        for _ in range(60):
            world.tick(1)
        # 2.0 起步、每次 +0.5、闸门设在 4 —— 停在第一个 >= 4 的值上,不是无限长。
        assert _height(world) == pytest.approx(4.0), _height(world)
        events = [
            e for e in world.history(limit=5000)["events"]
            if e["type"] == "entity_interaction"
        ]
        assert events, "她动手了却没进世界的历史"
        assert events[0]["payload"]["target"] == "tree:oak"


def test_闸门关着时她一下也不动(tmp_path):
    """树已经够高 —— 同一棵树、同一个时间窗,只是量不满足。"""
    seed = _with_duty()
    seed["stocks"] = [{"owner": "tree:oak", "values": {"树高": 9.0, "最大树高": 12}}]
    with _seed_world(tmp_path, seed) as world:
        for _ in range(30):
            world.tick(1)
        assert _height(world) == 9.0, "闸门关着,她却动手了"


def test_隔着半个地图的量进不了她的黑板(tmp_path):
    """她在咖啡店,树在后院 —— 那棵树多高她无从知道,这条分支就不该触发。"""
    seed = _with_duty()
    seed["entities"] = [{"id": "tree:oak", "name": "老橡树", "location": "yard"}]
    with _seed_world(tmp_path, seed) as world:
        for _ in range(20):
            world.tick(1)
        bb = world.scheduler.agents["甲"].agent.blackboard
        assert bb.read("stock.tree:oak.树高") is None, "她读到了看不见的量"
        assert _height(world) == 2.0, "她隔着半个地图把树浇高了"


def test_没声明可见性的量会吼一声(tmp_path, caplog):
    """静态的坏:世界怎么跑这条分支都永不触发。不吼的话作者永远查不出来。"""
    seed = _with_duty(key="最大树高")  # 这个量没声明 visibility → hidden
    with _seed_world(tmp_path, seed) as world:
        with caplog.at_level(logging.WARNING):
            world.tick(2)
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "最大树高" in messages and "永远不会触发" in messages, messages


def test_没有按量分支的世界黑板上一个量都没有(tmp_path):
    """缺席 = 零成本、行为逐 tick 不变 —— 和这个仓库每一层的默认值同一条。"""
    with _seed_world(tmp_path, _SEED) as world:
        world.tick(3)
        bb = world.scheduler.agents["甲"].agent.blackboard
        assert not [k for k in bb.snapshot() if k.startswith("stock.")]


def test_排班动手和聊天里动手是同一条(tmp_path):
    """两条路都落到 `perform_affordance`;分叉的那天没人会发现,所以在这儿钉住。"""
    with _seed_world(tmp_path, _with_duty()) as world:
        world.tick(1)
        before = _height(world)
        out = world.scheduler.perform_affordance("甲", "tree:oak", "tend")
        assert out["ok"] is True, out
        assert _height(world) == pytest.approx(before + 0.5)

        # 讲不通的调用和"这会儿不行"要分得开 —— 聊天那条路据此决定报错还是报话。
        assert world.scheduler.perform_affordance("甲", "tree:oak", "harvest")["reason"] == "unknown_verb"
        assert world.scheduler.perform_affordance("甲", "没这东西", "tend")["reason"] == "unknown_entity"


# ── 作者写的树:动作叶子得真有动作 ─────────────────────────────────────────


def test_作者树里没绑动作的叶子会吼一声(tmp_path, caplog):
    """节点 id 在动作表里查不到 → `lookup` 回落 idle_wander:树成功、世界不动。"""
    seed = json.loads(json.dumps(_SEED))
    seed["agents"][0]["behavior_tree"] = [
        {"node_id": "root", "type": "selector", "parent": None, "sort": 0},
        {"node_id": "浇水", "type": "action", "parent": "root", "sort": 0},
    ]
    with caplog.at_level(logging.WARNING):
        with _seed_world(tmp_path, seed):
            pass
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "浇水" in messages and "idle_wander" in messages, messages


def test_作者树的动作叶子能随身带着自己的动作(tmp_path):
    seed = json.loads(json.dumps(_SEED))
    seed["agents"][0]["behavior_tree"] = [
        {"node_id": "root", "type": "selector", "parent": None, "sort": 0},
        {"node_id": "够矮才浇", "type": "sequence", "parent": "root", "sort": 0},
        {"node_id": "矮吗", "type": "stock_condition", "parent": "够矮才浇", "sort": 0,
         "params": {"owner": "tree:oak", "key": "树高", "op": "<", "value": 3}},
        {"node_id": "浇水", "type": "action", "parent": "够矮才浇", "sort": 1,
         "action": {"kind": "interact", "params": {"target": "tree:oak", "verb": "tend"}}},
        {"node_id": "idle_wander", "type": "action", "parent": "root", "sort": 1},
    ]
    with _seed_world(tmp_path, seed) as world:
        for _ in range(20):
            world.tick(1)
        assert _height(world) == pytest.approx(3.0), _height(world)
