"""作者写的行为树,引擎认不认。

`bt_nodes` 表的 CHECK 放行七种节点类型,而 `BTStore._build_node` 只认得六种 ——
`need_action` 在 schema 里合法、在构造器里不存在。后果不是报错:构造器抛 ValueError,
调用方把它兜成一行 warning,然后**整棵作者树塌回 `default_bt()`**(一个只会
idle_wander 的根选择器)。世界照跑,角色什么也不干,而日志里那一行甚至不说是哪个
节点惹的祸。

这正是这个仓库最在意的那一类,而且它藏在"合法 SQL"后面:作者按 schema 写,写对了,
结果整个世界的行为被静默换掉。
"""
from __future__ import annotations

from _worldfile import open_world_at

import json
import logging

import pytest

from anima_world.api import World
from anima_world.bt_nodes import Blackboard, NeedAction, Status
from anima_world.world_store import BT_NODE_TYPES
from anima_world.needs import RELEASE, URGENT


def _insert_node(world, tree, node_id, type_, parent, sort, params):
    # 存储原语不做类型校验(和当年"合法 SQL"同一个位置):坏类型在 build 时才暴露。
    world.scheduler.bt_store.add_node(tree, node_id, type_, parent, sort, params)


def test_every_node_type_the_schema_allows_can_actually_be_built(tmp_path):
    """CHECK 与构造器必须认同一份类型集 —— 差一个,整棵树静默塌掉。"""
    buildable = set()
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        store = world.scheduler.bt_store
        for node_type in BT_NODE_TYPES:
            _insert_node(world, "probe", "root", node_type, None, 0, {
                "need": "hunger", "threshold": URGENT, "action_id": "eat",
                "key": "k", "expected": 1, "start": 0, "end": 1,
            })
            tree = store.build_tree("probe")
            # 塌回默认树 = 这个类型构造不出来
            if type(tree).__name__ != "Selector" or node_type in ("selector", "sequence"):
                buildable.add(node_type)
            else:
                built = store._build_node(
                    {"node_id": "root", "type": node_type, "params": {
                        "need": "hunger", "threshold": URGENT, "action_id": "eat",
                        "key": "k", "expected": 1, "start": 0, "end": 1,
                    }}, {}
                )
                buildable.add(node_type) if built is not None else None

    assert buildable == set(BT_NODE_TYPES), (
        f"schema 放行但构造不出来的类型:{sorted(set(BT_NODE_TYPES) - buildable)}"
    )


def test_a_need_action_node_does_not_collapse_the_whole_tree(tmp_path, caplog):
    """一个合法的 need_action 行,不该把作者写的整棵树换成 idle_wander。"""
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        _insert_node(world, "authored", "root", "selector", None, 0, {})
        _insert_node(world, "authored", "hungry", "need_action", "root", 0,
                     {"need": "hunger", "threshold": URGENT,
                      "release": RELEASE["hunger"], "action_id": "eat"})
        _insert_node(world, "authored", "fallback", "action", "root", 1, {})

        store = world.scheduler.bt_store
        with caplog.at_level(logging.WARNING):
            tree = store.build_tree("authored")

        assert not [r for r in caplog.records if "falling back" in r.getMessage()], (
            f"作者树被静默换掉了:{[r.getMessage() for r in caplog.records]}"
        )
        kids = getattr(tree, "children", [])
        assert any(isinstance(k, NeedAction) for k in kids), (
            f"need_action 没被造出来,树是 {tree!r}"
        )


def test_an_authored_need_action_carries_its_release_line(tmp_path):
    """作者写的收工线要生效,不然迟滞对作者树无效。"""
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        _insert_node(world, "authored", "root", "selector", None, 0, {})
        _insert_node(world, "authored", "hungry", "need_action", "root", 0,
                     {"need": "hunger", "threshold": 0.2, "release": 0.9,
                      "action_id": "eat"})
        leaf = [
            k for k in world.scheduler.bt_store.build_tree("authored").children
            if isinstance(k, NeedAction)
        ][0]

        bb = Blackboard()
        bb.write("need.hunger", 0.5)
        bb.write("need._restoring", ("hunger",))
        assert leaf.tick(bb) == Status.SUCCESS, "0.5 < 作者写的 release 0.9,应当继续吃"
        bb.write("need._restoring", ())
        assert leaf.tick(bb) == Status.FAILURE, "没在吃时用触发线 0.2"


def test_an_unknown_node_type_names_the_node_it_choked_on(tmp_path, caplog):
    """真遇到不认识的类型时,那行 warning 至少要说是哪个节点。"""
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        _insert_node(world, "weird", "root", "从未听说的类型", None, 0, {})
        with caplog.at_level(logging.WARNING):
            world.scheduler.bt_store.build_tree("weird")
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "从未听说的类型" in messages, messages


# ── 种子够得到行为树 ────────────────────────────────────────────────────────
#
# `agents[].duties` 只表达得了"时间窗 → 动作"这一种形状。作者要写条件分支、需求带、
# 嵌套选择器,就只能去手改 db —— 而那正是上面那个静默塌树的坑。

_SEED = {
    "locations": [{"id": "cafe", "name": "咖啡店", "description": "拐角那家"}],
    "agents": [{
        "id": "夏", "name": "苏晚夏", "location": "cafe", "personality": "开朗",
        "behavior_tree": [
            {"node_id": "root", "type": "selector", "parent": None, "sort": 0},
            {"node_id": "hungry", "type": "need_action", "parent": "root", "sort": 0,
             "params": {"need": "hunger", "threshold": 0.2, "release": 0.9,
                        "action_id": "eat"}},
            {"node_id": "idle_wander", "type": "action", "parent": "root", "sort": 1},
        ],
    }],
}


def _seed_world(tmp_path, seed):
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return open_world_at(str(tmp_path / "w.db"), seed_path=str(path), force_mock_llm=True)


def test_a_seed_can_author_a_behavior_tree_directly(tmp_path):
    with _seed_world(tmp_path, _SEED) as world:
        tree = world.scheduler.bt_store.build_tree("夏")
        assert any(isinstance(k, NeedAction) for k in getattr(tree, "children", [])), (
            f"种子写的 need_action 没进树:{tree!r}"
        )


def test_a_broken_node_in_an_authored_tree_never_blocks_boot(tmp_path):
    """坏种子让世界少一个分支,不该让世界开不了机 —— 与其它种子字段同一条规矩。"""
    seed = json.loads(json.dumps(_SEED))
    seed["agents"][0]["behavior_tree"].append(
        {"node_id": "坏的", "type": "根本不是节点类型", "parent": "root", "sort": 2}
    )
    with _seed_world(tmp_path, seed) as world:
        tree = world.scheduler.bt_store.build_tree("夏")
        assert any(isinstance(k, NeedAction) for k in getattr(tree, "children", []))


def test_a_seed_without_a_behavior_tree_still_uses_duties(tmp_path):
    """缺席 = 今天的行为,一个 tick 都不变。"""
    seed = json.loads(json.dumps(_SEED))
    del seed["agents"][0]["behavior_tree"]
    seed["agents"][0]["duties"] = [
        {"name": "开店", "start": "08:00", "end": "18:00", "kind": "work"}
    ]
    with _seed_world(tmp_path, seed) as world:
        rows = world.scheduler.bt_store._tree_rows("夏")
        assert any("开店" in r["node_id"] for r in rows), [r["node_id"] for r in rows]


def test_two_agents_may_name_a_duty_the_same_thing(tmp_path):
    """两个人各有一个「回铺子」,各回各的铺子。

    动作表原先是**全世界一张**(`set_action(node_id, …)`),而 `bt_nodes` 一直是
    **按人**存的(`(tree, node_id)`)。于是两个人只要给自己的班表起了同一个名字,
    后播种的那个人就把先播的那条**整行改写**掉 —— 理发的老八和补鞋的老铁都写
    「回铺子」,老铁从此走进理发店。

    这不是罕见的写法:一个世界里"回铺子/收摊/去码头"本来就是好几个人都会做的事,
    重名是作者层的常态而不是错误。而它的坏法是这个仓库最怕的那一种 —— 树建得起来、
    动作查得到、日志一行不错,只是**人去错了地方**,要到有人盯着某个 NPC 看一整天
    才发现。线上那个世界已经有三个人共用「睡」,一直没出事只是因为三条的内容恰好
    一模一样。
    """
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        store = world.scheduler.bt_store
        for aid, where in (("tie", "alley"), ("ba", "barber")):
            store.seed_duties(aid, [{
                "name": "回铺子", "start": "18:00", "end": "19:00",
                "kind": "walk", "params": {"location": where},
            }])

        for aid, where in (("tie", "alley"), ("ba", "barber")):
            got = store.action_table(aid).lookup("回铺子_do")
            assert got.params.get("location") == where, (
                f"{aid} 的「回铺子」被别人改写成了 {got.params.get('location')!r} —— "
                "世界照跑,只是他走错了门"
            )


def test_a_shared_binding_still_reaches_everyone(tmp_path):
    """按人分开之后,`go_to_*` / `chat_with_*` 这些**全局**绑定不许跟着分家。

    它们是"谁都能去那儿、谁都能找他说话",播的时候没有人称。分家分过头的话
    每个人的表里都查不到它们,`ActionTable.lookup` 回落 idle_wander ——
    同样是树成功、世界不动、日志干净。
    """
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        store = world.scheduler.bt_store
        store.set_action("go_to_pier", "walk", {"location": "pier"})
        store.seed_duties("tie", [{
            "name": "回铺子", "start": "18:00", "end": "19:00",
            "kind": "walk", "params": {"location": "alley"},
        }])
        for aid in ("tie", "ba", "谁都行"):
            got = store.action_table(aid).lookup("go_to_pier")
            assert got.kind == "walk" and got.params.get("location") == "pier", (
                f"{aid} 查不到全局绑定 go_to_pier,回落成了 {got.kind}"
            )
