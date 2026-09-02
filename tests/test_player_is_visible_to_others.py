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
    "locations": [{"id": "校园", "name": "卡塞尔学院", "description": "常青藤下的红砖楼。"},
                  {"id": "宿舍", "name": "学生宿舍", "description": "三个人的房间。"}],
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


def test_装包那一刻在线的玩家_也拿得到新声明的量(tmp_path):
    """🔴 **同一个世界里两种玩家,而差别只是「装包那会儿你在不在线」。**

    播种从前挂在 `_touch_player` 的 `fresh` 分支里,而 `fresh` 是那张**带 TTL** 的
    在场行报的 —— 可"这个世界声明了哪些量"是会变的:装一份编辑包、热开一个出厂
    插件,都在运行期。于是那一刻在线的老玩家拿不到新声明的量,`rc=0`、日志不提。

    理由和 `_record_player_join` 挪出 `fresh` 那一条**逐字相同**:
    「第一次」不能记在带 TTL 的在场上。
    """
    bare = {k: v for k, v in SEED.items() if k != "plugins"}
    base = write_seed_file(tmp_path / "hot2.json", bare)
    with open_world_at(tmp_path / "hot2.db", world_file=base) as w:
        w.player_move("old", "校园")                      # 装包之前就在线
        w.tick(1)
        assert "role.评级" not in w.stocks("agent:player:old")

    # 运行期装一份只含作者层的**编辑包** —— 创作台走的就是这条路。
    edit = write_seed_file(tmp_path / "edit.json", {"plugins": [ROLE]})
    with open_world_at(tmp_path / "hot2.db", world_file=edit) as w:
        w.player_move("old", "校园")                      # 他还在,只是又露了个面
        assert w.stocks("agent:player:old").get("role.评级") == 0.0, (
            "在线的老玩家没拿到新声明的量 —— 播种多半又挂回 `fresh` 分支里了"
        )
        readouts = {r["key"] for r in w.player_options("old")["own"]["readouts"]}
        assert "role.评级" in readouts

        w.player_move("brandnew", "校园")                 # 同一刻新进来的人
        assert w.stocks("agent:player:brandnew").get("role.评级") == 0.0


def test_没写kinds的世界_own那两格照样有(tmp_path):
    """🔴 `blocked` 该挡的只有 `targets`(3.9.0,创作台验收 C 逮的)。

    `own` 不依赖本体层也不依赖他站在哪:量住 `stock:agent:player:<id>`,档词与人话
    名字走**可见性表**。从前它挂在 `no_ontology` 早退后面 —— 于是一个用插件给玩家
    挂了 `role.评级` 而没写 `kinds` 的世界,Redis 里真有那个量、可见性表里真有 bands,
    而 `own.readouts` 是 `[]`,站点的状态四格一片空白;随手加一个 `kind`,
    同一份插件立刻出「评级 预科」。**"全绿而产物是坏的"那个形状。**
    """
    no_kinds = {k: v for k, v in SEED.items() if k not in ("kinds", "entities")}
    path = write_seed_file(tmp_path / "nokinds.json", no_kinds)
    with open_world_at(tmp_path / "nokinds.db", world_file=path) as w:
        w.player_move("p9", "校园")
        w.tick(1)
        menu = w.player_options("p9")
        assert menu["blocked"] == "no_ontology"
        assert menu["targets"] == [], "挡住的本来就是这一格"
        rows = {r["key"]: r for r in menu["own"]["readouts"]}
        assert "role.评级" in rows, "他身上真有这个量,而这一格把它吞了"
        assert rows["role.评级"]["word"] == "预科"
        assert rows["role.评级"]["text"] == "评级 预科"


def test_在路上的时候own也还在(tmp_path):
    """同一条理由:他在走路,不代表他身上的量消失了。"""
    path = write_seed_file(tmp_path / "walk.json", SEED)
    with open_world_at(tmp_path / "walk.db", world_file=path) as w:
        w.player_move("p8", "校园")
        w.tick(1)
        w.player_walk("p8", "宿舍")
        menu = w.player_options("p8")
        assert menu["blocked"] == "in_transit"
        assert {r["key"] for r in menu["own"]["readouts"]} >= {"role.评级"}


# ── 批 1.1 ④:一条不改任何量的能力,他按下去也得有一行回应(3.10.0)──────────
#
# 🔴 **真站实测**:玩家点「报到狮心会」,屏幕**纹丝不动**。那条能力 `changed` 是空的
# (报到不改任何量),而回执里只有一堆空 dict、`ToolResult.text` 空着 ——
# 世界里真的发生了一件事,而他读到的和什么都没按一样。

_QUIET = {
    "locations": [{"id": "yard", "name": "院子", "description": "有块牌子"}],
    "agents": [{"id": "甲", "name": "甲", "location": "yard", "personality": "安静"}],
    "kinds": [{"id": "group", "gloss": "一个社团",
               "quantities": {"人数": {"default": 1.0, "visibility": "here"}},
               # 一个量都不改 —— 正是真站那条。
               "affordances": {"报到": {"label": "报到"}}}],
    "entities": [{"id": "group:狮心会", "name": "狮心会", "location": "yard"}],
}


def _quiet_world(tmp_path, name):
    from _worldfile import open_world_at, write_seed_file

    path = write_seed_file(tmp_path / f"{name}.cyberworld", _QUIET)
    world = open_world_at(str(tmp_path / f"{name}.db"), world_file=path,
                          force_mock_llm=True)
    world.player_move("p1", "yard")
    world.tick(2)
    return world


def test_changed是空的_玩家也拿得到一行人话(tmp_path):
    with _quiet_world(tmp_path, "q1") as world:
        result = world.player_tool("p1", "interact",
                                   {"target": "group:狮心会", "verb": "报到"})
        assert result["ok"] is True
        assert result["detail"]["changed"] == {}, "夹具前提:这条能力一个量都不改"
        assert result["text"] == "你报到了狮心会。", result
        assert result["detail"]["said"] == result["text"], "两格必须是同一句话"


def test_那一句也进叙事日志_而且说得出它是模板不是模型写的(tmp_path):
    """🔴 那三道闸(作者写没写 `importance` / 有没有旁白池 / `narrative.player.enabled`)
    的理由是**旁白是一次 LLM 调用** —— 而这一句是模板,一次调用都不用。
    所以它是**地板**,那三道闸管的是**升级**。
    """
    with _quiet_world(tmp_path, "q2") as world:
        assert world.config_get("narrative.player.enabled") is False, "夹具前提:闸是关的"
        world.player_tool("p1", "interact", {"target": "group:狮心会", "verb": "报到"})
        # ⚠️ **按发言人筛。** 屋里那个角色自己也会有旁白(线程池什么时候落地由这台
        # 机器此刻忙不忙决定)—— 不筛的话这条用例会随机红,而红出来的话在指控
        # 一个没错的地方(「试牙也要试对地方」的邻居:**断言也要断对地方**)。
        rows = [e.payload for e in world.scheduler.event_log.replay()
                if e.type == "narrative" and e.payload.get("speaker") == "player:p1"]
        assert [r["text"] for r in rows] == ["你报到了狮心会。"], rows
        assert rows[0]["source"] == "template", rows[0]


def test_角色那条路一个字都没多_回执里没有said(tmp_path):
    """⚠️ **只给玩家。** 角色那条路的回执进的是她的对话流,多一句「你…了…」
    会变成她自己念出来的一句旁白。"""
    with _quiet_world(tmp_path, "q3") as world:
        out = world.act("甲", "interact",
                        {"target": "group:狮心会", "verb": "报到"}, surface="body")
        assert out.get("ok") is True, out
        assert "said" not in out, out
