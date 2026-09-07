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


#: 一个**作者写的**插件:阵营上挂一个「投奔」动词。
#: 🔴 **这才是站队的真路** —— 上一版这个文件直接调 `apply_edge_effect` 传字面量,
#: 那绕过了插件装没装上、作者写不写得出这条效果、动词跑不跑得到这儿**全部三件**。
_TOUKAO = {
    "id": "menpai", "version": "1.0.0", "label": "门派",
    "verbs": {"投奔": {"target": "group", "label": "投奔",
                      "effects": [{"link": {"type": "factions.member_of",
                                            "from": "self", "to": "target"}}]}},
}


def _world(tmp_path, name="f"):
    path = write_seed_file(tmp_path / f"{name}.cyberworld",
                           {**_WORLD, "plugins": [dict(_TOUKAO)]})
    world = open_world_at(str(tmp_path / f"{name}.db"), world_file=path,
                          force_mock_llm=True)
    world.config_set("factions.enabled", True)
    return world


def _join(world, pid, side):
    """站队走**真门**:玩家对那个阵营做「投奔」。"""
    got = world.scheduler.perform_affordance(f"player:{pid}", side, "投奔")
    return bool(got.get("ok")) and all(
        row.get("ok") for row in (got.get("edges") or [{"ok": False}]))


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


def test_作者写不出叛出改投_而那不是漏了(tmp_path):
    """🔴 **A 末轮 ④,调度台裁**:3.13.0 把出厂的边放开给所有插件连之后,
    **一条作者写的 `unlink` 就能让人叛出改投** —— 而「站了就回不去」是
    3c §2.10 ④ 的**产品裁决**,不该由一条动词推翻。

    ⚠️ 判据是**两头都验**:`link` 那一刻内核拒第二次站队(上面那条),
    而这一条保证**没有第二条路把第一条边摘掉**。少了任何一半,那句话都不成立。
    """
    from anima_world.__main__ import contract_payload, world_plugin_errors

    bad = {"id": "menpai2", "version": "1.0.0", "label": "门派",
           "verbs": {"叛出": {"target": "group", "label": "叛出",
                             "effects": [{"unlink": {"type": "factions.member_of",
                                                     "from": "self",
                                                     "to": "target"}}]}}}
    said = world_plugin_errors({"plugins": [bad]})
    assert any("关着的" in line for line in said), (
        f"作者写得出「叛出」——「站了就回不去」被一条动词绕开了:{said}")
    # 而线索那条边三个 op 全开(它没有这条产品裁决)
    ok = {**bad, "verbs": {"忘掉": {"target": "group", "label": "忘掉",
                                   "effects": [{"unlink": {"type": "clues.knows",
                                                           "from": "self",
                                                           "to": "target"}}]}}}
    assert world_plugin_errors({"plugins": [ok], "kinds": [{"id": "group"}]}) == [], (
        "把线索那条边也一起关了 —— 拦过头和漏掉一样坏")
    seg = contract_payload()["plugins"]["shared_edge_ops"]
    assert seg["factions.member_of"] == ["link"], seg
    assert "leave" in contract_payload()["factions"]["deferred"], (
        "关掉一条路而不说「放人走去哪儿找」,下一个人会以为是漏了")


def test_一条拍也叛不出_两扇门都拒(tmp_path):
    """🔴 **A 四轮 ①**:上一版那道闸只写在插件效果那一层,**剧情拍整个绕过它** ——
    一条三行的拍 `unlink factions.member_of` 装得进去、跑得动、
    `ops_applied` 里有它,而「站了就回不去」同时写在 gloss、CHANGELOG 和契约三处。

    **写在一扇门上的规矩不是规矩。** 判断挪进内核(`apply_edge_effect`),
    拍与动词共用一份;加载期那一半也要说 —— **一条装得进去、跑起来什么都不做的拍,
    比一条装不进去的坏得多**。
    """
    from anima_world.__main__ import authored_layer_errors

    beat = {"id": "叛出", "for_each": {"node": "player"},
            "trigger": {"at": {"day": 0, "minute_of_day": 5}},
            "payload": [{"op": "unlink", "type": "factions.member_of",
                         "from": "player", "to": "group:狮心会"}]}
    said = authored_layer_errors({**_WORLD, "beats": [beat]}, complete=True)
    assert any("关着的" in line for line in said), (
        f"一条拍写得出叛出,而离线那两扇门一声不吭:{said}")

    # 运行期那一半:就算绕过加载期(库里那份老拍),边也不许动
    with _world(tmp_path, "beatleave") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        assert _join(world, "p1", "group:狮心会") is True
        got = world.scheduler._beat_edge(
            {"op": "unlink", "type": "factions.member_of",
             "from": "player:p1", "to": "group:狮心会"})
        after = world.player_faction("p1")

    assert got is None, "拍把那条边摘掉了 —— 而它该连 `ops_applied` 都进不去"
    assert after["faction"] == "group:狮心会", (
        f"站过的那一边被一条拍摘掉了:{after}")
