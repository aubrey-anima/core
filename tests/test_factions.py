"""阵营:站队、声望、士气(玩法层批 3c §2.6,3.13.0)。

两条纪律,两道闸:**站了回不去**(而且是**内核**拒,不是插件自己判)、
**计票那四样一行都没有**。
"""
from __future__ import annotations

import pytest

from _worldfile import open_world_at, write_seed_file

_WORLD = {
    "agents": [{"id": "阿岚", "name": "阿岚", "location": "cafe",
                "personality": "安静"}],
    "locations": [{"id": "cafe", "name": "咖啡馆", "description": "小店"}],
    "kinds": [{"id": "group", "gloss": "一个阵营",
               "affordances": {"look": {}}}],
    "entities": [
        {"id": "group:狮心会", "name": "狮心会", "location": "cafe"},
        {"id": "group:卡塞尔", "name": "卡塞尔", "location": "cafe"},
    ],
}


def _world(tmp_path, name="f"):
    path = write_seed_file(tmp_path / f"{name}.cyberworld", _WORLD)
    world = open_world_at(str(tmp_path / f"{name}.db"), world_file=path,
                          force_mock_llm=True)
    world.config_set("factions.enabled", True)
    return world


def _join(world, pid, side):
    from anima_world import factions as F

    return world.scheduler.apply_edge_effect(
        {"type": f"{F.PLUGIN_ID}.{F.MEMBER_EDGE}",
         "from": f"player:{pid}", "to": side}, {})


def test_站了就回不去_而且是内核拒(tmp_path):
    """🔴 设计 §4.4 的原话。**而放行的样子是安静的** ——
    两条 `member_of` 同时挂着,`plugin list` 看不出来,
    而提示词里他同时属于两个阵营。所以这一层**不自己判**:
    声明 `exclusive`,让内核拒。
    """
    with _world(tmp_path, "join") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        assert _join(world, "p1", "group:狮心会") is True
        first = world.player_faction("p1")
        # 🔴 再站一次 —— 内核那一刻就该拒
        again = _join(world, "p1", "group:卡塞尔")
        after = world.player_faction("p1")

    assert first["faction"] == "group:狮心会", first
    assert again is False, "站了第二边而内核放行了 —— exclusive 是一句摆设"
    assert after["faction"] == "group:狮心会", (
        f"第二次站队把第一次盖掉了:{after}")


def test_阵营那一格_没有有几个人(tmp_path):
    """🔴 **计票 / 人数上限 / 禁地 / 全城声望一行都不许有** ——
    四样全是聚合或 gates,而那是批 4。

    **在一个没有聚合的引擎上手写一遍计票,就是把批 4 那件事做进一个插件里**,
    而那份实现以后要被删掉重写;在被删掉之前,它是**第二份真相**。
    ⚠️ 这条闸拦的是**写这段代码的人自己** —— 计票"顺手写一下"很便宜。
    """
    from anima_world import factions as F
    from anima_world.__main__ import contract_payload

    with _world(tmp_path, "count") as world:
        world.player_move("p1", "cafe")
        world.player_move("p2", "cafe")
        world.tick(2)
        _join(world, "p1", "group:狮心会")
        _join(world, "p2", "group:狮心会")
        board = world.player_faction("p1")

    assert set(board) == set(F.BOARD_KEYS), sorted(board)
    for banned in ("members", "member_count", "count", "tally", "rank"):
        assert banned not in board, f"阵营那一格报了「{banned}」—— 那要聚合"
    # 契约里点名了这一版不做的那四样
    seg = contract_payload()["factions"]
    assert set(seg["deferred"]) == set(F.DEFERRED)
    assert "tally" in seg["deferred"] and "forbidden_area" in seg["deferred"]
    # 出厂插件里**一行聚合都没有**
    body = F.factory_plugin()
    assert body.get("rules") in (None, []), (
        f"`factions` 声明了规律 —— 计票最容易从这儿溜进来:{body.get('rules')}")


def test_声望那句人话_引擎给_而分档表只有一份(tmp_path):
    """和 `tension_text` 逐字同一条:各译一遍的话,同一个数会在
    引擎屏 / 创作台 / 站点上说三种话。"""
    from anima_world import factions as F
    from anima_world.__main__ import contract_payload

    seg = contract_payload()["factions"]
    assert [tuple(b) for b in seg["standing_bands"]] == [
        tuple(b) for b in F.STANDING_BANDS]
    assert F.standing_text(0.0) == "生面孔"
    assert F.standing_text(0.9) == "一句顶十句"

    with _world(tmp_path, "word") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        assert world.player_faction("p1")["text"] == "你还没站队。"
        _join(world, "p1", "group:狮心会")
        said = world.player_faction("p1")["text"]

    assert "狮心会" in said and "生面孔" in said, said
