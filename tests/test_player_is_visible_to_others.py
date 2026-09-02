"""玩家在别人眼里到底存不存在(3.9.0,2026-09-02 裁决 §2.4)。

病灶一句话:**那道闸读错了地方。** `Scheduler._actor_is_visible_to_others()` 问的是
「她身上有没有别人看得见的量」,而它从前读的是**本体层** `ontology.kinds["agent"]
.quantities` —— 可**插件声明的事实只落进可见性表,根本不进 `ontology.kinds`**。
判假之后 `_settle_player_places()` 整个不跑,玩家永远不进 `stock_places`,
**任何 NPC 的 `perception` 里都没有他**。作者写下「别人看得出你的评级」,
世界照跑、日志干净、屏幕上一个字都没有。

2026-09-02 线上《龙族》就是那样的世界:`ontology --kind agent --json` 现敲,
`quantities` 是 `[]` —— 装上评级插件而不修这道闸,那句话对 37 个人一次都不会发生。

两半必须一起,缺一半就是「第一天红第二天绿」那种假修法:判据换成读可见性表,
**以及那个缓存要能失效**(它从前只设一次、一处都不失效,而插件的开关是热的)。
"""
from __future__ import annotations

import json

import pytest

from _worldfile import open_world_at, write_seed_file


ROLE = {
    "id": "role", "version": "1.0.0", "label": "评级",
    "facts": {"评级": {
        "bearer": "player", "shape": "state", "default": "预科",
        "visibility": "public", "label": "评级",
        "values": [{"name": "预科", "description": "还没参加过 3E,档案上什么都没有。"},
                   {"name": "F", "description": "垫底那一档,谁提起你都要顿一下。"},
                   {"name": "E", "description": "勉强站住了脚。"}]}},
}

# ⚠️ **本体层的 `agent` 一个量都没有** —— 这正是线上龙族的形状,也正是触发条件。
# 给它加一个 here 档的量,这一族会整个变绿而什么都没测到。
SEED = {
    "agents": [{"id": "恺撒", "name": "恺撒", "location": "校园", "personality": "傲慢"}],
    "locations": [{"id": "校园", "name": "卡塞尔学院", "description": "常青藤下的红砖楼。"}],
    "kinds": [{"id": "notice", "gloss": "一张告示", "affordances": {"look": {}},
               "prompt": {"budget": 2}}],
    "entities": [{"id": "notice:board", "name": "布告栏", "location": "校园"}],
    "plugins": [ROLE],
}


@pytest.fixture()
def world(tmp_path):
    path = write_seed_file(tmp_path / "seed.json", SEED)
    with open_world_at(tmp_path / "w.db", world_file=path) as w:
        w.player_move("p1", "校园")
        w.tick(1)
        yield w


def test_本体层一个量都没有时_插件给玩家挂的public事实照样到得了别人眼里(world):
    per = world.perception("恺撒")
    assert "agent:player:p1" in per["here"], (
        "玩家整个不在感知里 —— 那道闸多半又去读本体层了"
    )
    assert per["here"]["agent:player:p1"]["role.评级"] == 0.0
    assert per["words"]["here"]["agent:player:p1"]["role.评级"] == "预科"
    assert per["notes"]["here"]["agent:player:p1"]["role.评级"].startswith("还没参加过")


def test_那句描述真的进了她的提示词(world):
    blocks = json.dumps(world.debug_prompt("恺撒", player_id="p1", message="在吗"),
                        ensure_ascii=False)
    for word in ("评级", "预科", "还没参加过 3E"):
        assert word in blocks, f"提示词里没有 {word!r}"


def test_玩家真的落进了可见性表那张位置表(world):
    assert world.scheduler.visibility_store.place_of("agent:player:p1") == "校园"


def test_一个真的什么都没声明的世界_这一步照旧不做(tmp_path):
    """**声明本身就是开关**:没有任何 here/public 声明的世界,行为逐位如旧。"""
    bare = {k: v for k, v in SEED.items() if k != "plugins"}
    path = write_seed_file(tmp_path / "bare.json", bare)
    with open_world_at(tmp_path / "bare.db", world_file=path) as w:
        w.player_move("p2", "校园")
        w.tick(1)
        assert w.scheduler._actor_is_visible_to_others() is False
        assert w.scheduler.visibility_store.place_of("agent:player:p2") is None
        assert "agent:player:p2" not in w.perception("恺撒")["here"]


def test_运行期新声明一个public的量_不必重启就生效(tmp_path):
    """一个要重启才生效的东西,在一个跑着的线上世界里就等于不生效。"""
    bare = {k: v for k, v in SEED.items() if k != "plugins"}
    path = write_seed_file(tmp_path / "hot.json", bare)
    with open_world_at(tmp_path / "hot.db", world_file=path) as w:
        w.player_move("p3", "校园")
        w.tick(1)
        assert w.scheduler._actor_is_visible_to_others() is False   # 先量到"关着"
        w.declare_visibility("agent", "气色", "here", label="气色")
        w.set_stocks("agent:player:p3", {"气色": 1.0})
        w.tick(1)
        assert w.scheduler._actor_is_visible_to_others() is True
        assert "agent:player:p3" in w.perception("恺撒")["here"], "缓存没跟着声明走"


def test_撤掉最后一条声明之后那个答案也要跟着变(tmp_path):
    """`undeclare` 是插件裁剪/卸载唯一的出口 —— 它同样是"声明变了"。"""
    path = write_seed_file(tmp_path / "u.json", SEED)
    with open_world_at(tmp_path / "u.db", world_file=path) as w:
        assert w.scheduler._actor_is_visible_to_others() is True
        w.scheduler.visibility_store.undeclare("agent", "role.评级")
        assert w.scheduler._actor_is_visible_to_others() is False
