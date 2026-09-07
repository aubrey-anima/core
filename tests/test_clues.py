"""线索:一个「知道 / 不知道」的东西(玩法层批 3c §2.4,3.13.0)。

**零新原语** —— 一条线索是一个实体,「他知不知道」是一条边。
这个文件钉三条纪律,每条都是可验的(见 `anima_world/clues.py` 的模块 docstring)。
"""
from __future__ import annotations

import pytest

from _worldfile import open_world_at, write_seed_file

#: 🔴 **哨兵**:未解锁那条线索的正文。它**不许出现在任何一扇只读门的输出里**。
SECRET = "路明非其实是龙王"

_WORLD = {
    "agents": [{"id": "阿岚", "name": "阿岚", "location": "cafe",
                "personality": "安静"}],
    "locations": [{"id": "cafe", "name": "咖啡馆", "description": "小店"}],
    "kinds": [{"id": "clue", "gloss": "一条线索",
               "affordances": {"look": {}}}],
    "entities": [
        {"id": "clue:老橡树的来历", "name": "老橡树的来历", "location": "cafe",
         "gloss": "夏说这棵树是她外婆种的。"},
        {"id": "clue:昂热知道的那件事", "name": "昂热知道的那件事",
         "location": "cafe", "gloss": SECRET},
        {"id": "clue:第三条", "name": "第三条", "location": "cafe"},
    ],
}


def _world(tmp_path, name="c"):
    path = write_seed_file(tmp_path / f"{name}.cyberworld", _WORLD)
    world = open_world_at(str(tmp_path / f"{name}.db"), world_file=path,
                          force_mock_llm=True)
    world.config_set("clues.enabled", True)
    return world


def test_玩家在图上只有一个形状_写的人和读的人不许各写各的(tmp_path):
    """🔴 **验收 A ② 实测出来的**:边连上了,而板子读不到。

    这个引擎里玩家有两个形状,**各管一头**(`beats.bind_player` 逐字):
    量表按 `agent:player:<id>` 存,而关系 / 账本 / 库存 / 事件顶层的 `who` /
    在场位置一律是 `player:<id>`。**边属于前者**
    (`plugins.EDGE_NODE_ID_FORMS["player"]`,那一行的注释就写着「最容易写错」)。

    上一版 `_apply_verb_edges` 写 `agent:player:p1`、`player_clues` 读 `player:p1`
    —— **写的人和读的人错成了两个样子**,而两边都不报错:
    `link` 返回 True、日志一条不少,板子上是 0。
    ⚠️ 而夹具当时直接调 `apply_edge_effect` 传了个 `"player:p1"` 字面量,
    于是**它和读的那一半错成了同一个样子**,一条不红。

    现在规范化只有一处(`Scheduler._edge_node`),这条闸钉两件:
    ① 两种写法进去都落在**同一个节点**上;② 板子读的就是那个节点。
    """
    from anima_world.plugins import EDGE_NODE_ID_FORMS
    from anima_world.scheduler import Scheduler

    assert EDGE_NODE_ID_FORMS["player"] == "agent:player:<player_id>", (
        "契约里玩家节点的形状换了 —— 这条闸和 `_edge_node` 都照它写")
    assert Scheduler._edge_node("player:p1") == "agent:player:p1"
    assert Scheduler._edge_node("agent:player:p1") == "agent:player:p1"
    assert Scheduler._edge_node("夏") == "夏", "别把角色 id 也改写了"

    with _world(tmp_path, "node") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        edge = f"{clues_edge()}"
        # 两种写法各连一次 —— 落在同一个节点上,所以第二次是同一条边
        world.scheduler.apply_edge_effect(
            {"type": edge, "from": "player:p1", "to": "clue:老橡树的来历"}, {})
        world.scheduler.apply_edge_effect(
            {"type": edge, "from": "agent:player:p1", "to": "clue:第三条"}, {})
        rows = world.scheduler.edge_store.of_src(edge, "agent:player:p1")
        board = world.player_clues("p1")

    assert len(rows) == 2, f"两种写法落在了两个节点上:{rows}"
    assert board["known"] == 2, (
        f"边连上了而板子读不到 —— 写的人和读的人又各写各的:{board}")


def clues_edge() -> str:
    from anima_world import clues as C

    return f"{C.PLUGIN_ID}.{C.KNOWS_EDGE}"


def test_线索板只报存在与状态_不报内容(tmp_path):
    """🔴 **纪律 1**(设计 §5 那张表的原话)。

    ⚠️ 判据是**哨兵扫每一扇只读门** —— 不是"看看 board 里有没有 text 字段":
    内容可能从任何一格漏出去,而**漏了的那一格不会报错**。
    """
    with _world(tmp_path, "board") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        board = world.player_clues("p1")
        doors = [
            board, world.player_options("p1"), world.host_turn("p1"),
            world.player_story("p1"), world.player_perception("p1"),
        ]

    assert board["total"] == 3, board
    assert board["known"] == 0 and board["unknown"] == 3, board
    assert board["known_ids"] == [], (
        "一条都没解锁,却报得出 id —— 未解锁线索的 id 往往就是它的谜面")
    assert board["text"], "一句人话都没有"
    for door in doors:
        assert SECRET not in str(door), f"未解锁那条的正文漏出去了:{door}"
        assert "昂热知道的那件事" not in str(door), (
            f"未解锁那条的**名字**也漏了(名字就是谜面):{door}")


def test_解锁那三条路_都连得上(tmp_path):
    """**纪律 2 的正面**:三条路各写一遍,都能把 `clues.knows` 连上。"""
    from anima_world import clues as C

    with _world(tmp_path, "unlock") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        edge = f"{C.PLUGIN_ID}.{C.KNOWS_EDGE}"
        world.scheduler.apply_edge_effect(
            {"type": edge, "from": "player:p1", "to": "clue:老橡树的来历"}, {})
        board = world.player_clues("p1")

    assert board["known"] == 1 and board["unknown"] == 2, board
    assert board["known_ids"] == ["clue:老橡树的来历"], board
    # 已知那条**可以**点名 —— 他本来就知道
    assert "老橡树的来历" in str(board)


def test_契约点名了解锁只有三条路_而且不报怀疑那一档():
    """🔴 **纪律 2 / 3 写进契约**:一个等不来的东西会让人一直等着,
    而不是换个写法(和「作者层写不了 `confront`」同一课);
    而**报一个算不出来的档,就是让屏幕替引擎撒谎**。
    """
    from anima_world import clues as C
    from anima_world.__main__ import contract_payload

    seg = contract_payload()["clues"]
    assert seg["unlock_paths"] == list(C.UNLOCK_PATHS)
    assert len(seg["unlock_paths"]) == 3, seg["unlock_paths"]
    assert "judge" not in str(seg["unlock_paths"])
    # 两档,不是三档
    assert seg["states"] == ["known", "unknown"], seg["states"]
    assert "suspect" not in str(seg) and "怀疑" not in seg["gloss"].replace(
        "**不报「怀疑」那一档**", "")
    assert set(seg["options_keys"]) == set(C.BOARD_KEYS)


def test_没有本体层的世界_线索板也不塌(tmp_path):
    """`blocked` 该挡的只有「这儿有什么」—— 和 `own` / `person_verbs` 同一课。"""
    seed = {k: v for k, v in _WORLD.items() if k not in ("kinds", "entities")}
    path = write_seed_file(tmp_path / "bare.cyberworld", seed)
    with open_world_at(str(tmp_path / "bare.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.config_set("clues.enabled", True)
        world.player_move("p1", "cafe")
        world.tick(2)
        board = world.player_clues("p1")

    assert board["total"] == 0 and board["known"] == 0, board
    assert board["text"], "一个没有线索的世界也该有一句人话"
