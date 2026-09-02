"""主持人:世界永远先开口(3.9.0,2026-09-02 裁决 §2.1)。

玩家点进一个世界,从前看到的是名册、地图,和**一个空白的聊天框**。跑团桌上没有这个
问题,因为 GM 永远先开口:场景 → 选项 → 后果。

这一族钉六件:

- **一次调用交出一屏**(场景与选项必须同一次取,否则是对一个动着的世界取两次快照)。
- **每一项都指向今天已经存在的那扇门**,`door.method` 与每种门的 params 键集都是闭集。
- **自由输入永远最后一项,而且不占名额。**
- **只在四个时刻开口**,别处没有第二条生成场景的路。
- **挑法纯算术、可复现**;LLM 只写字,没有否决权。
- 🔴 **藏起来的人一个字都不许漏** —— 不进候选,也不进给模型的那份提示。
"""
from __future__ import annotations

import json

import pytest

from _worldfile import open_world_at, write_seed_file

from anima_world import host as host_mod


@pytest.fixture()
def world(tmp_path):
    """内置橱窗 —— 它自带一个 `billing: "hidden"` 的角色(遥),正好是这一族的夹具。"""
    with open_world_at(tmp_path / "w.db") as w:
        w.player_move("p1", "cafe")
        w.tick(3)
        yield w


# ── 一屏的形状 ───────────────────────────────────────────────────────────────

def test_一次调用就交出一屏(world):
    turn = world.host_turn("p1")
    from anima_world.__main__ import contract_payload
    for key in contract_payload()["host"]["turn_keys"]:
        assert key in turn, f"契约里报了 {key!r},返回里没有"
    assert turn["scene"]["text"].strip(), "场景不许是空的"
    assert len(turn["options"]) >= 3


def test_每一项都指向一扇今天已经有的门_而且params的键集照契约(world):
    from anima_world.__main__ import contract_payload

    spec = contract_payload()["host"]
    for option in world.host_turn("p1")["options"]:
        assert option["kind"] in spec["option_kinds"]
        assert option["tone"] in spec["tones"]
        method = option["door"]["method"]
        assert method in spec["door_methods"]
        wanted = {k.rstrip("?") for k in spec["door_params"][method] if not k.endswith("?")}
        assert set(option["door"]["params"]) >= wanted, (
            f"{method} 少了 {wanted - set(option['door']['params'])} —— "
            "消费方按契约那一段对表,发不出来的键他就只能猜"
        )
        for key in spec["option_keys"]:
            assert key in option


def test_自由输入永远是最后一项_而且不占名额(world):
    world.config_set("host.max_options", 3)
    turn = world.host_turn("p1")
    assert turn["options"][-1]["kind"] == "free"
    assert turn["options"][-1]["available"] is True
    assert len([o for o in turn["options"] if o["kind"] != "free"]) <= 3
    assert len(turn["options"]) == 4, "自由输入不该把别人挤掉"


def test_四格拒绝是原样透传的_不另算一遍(world):
    """另算一份就是第二套判断,而两套判断迟早给出不同答案 —— 且不报错。"""
    menu = {
        (t["id"], v["verb"]): v
        for t in world.player_options("p1")["targets"] for v in t["verbs"]
    }
    for option in world.host_turn("p1")["options"]:
        if option["kind"] != "verb":
            continue
        body = option["id"][len("opt:verb:"):]
        target, _, verb = body.rpartition(":")
        source = menu[(target, verb)]
        assert (option["available"], option["reason"], option["refusal"], option["cost"]) == (
            source["available"], source["reason"], source["refusal"], source["cost"]
        )


# ── 挑法:纯算术、可复现 ──────────────────────────────────────────────────────

def test_同一个世界同一时刻挑两次_逐项相同(world):
    first = [o["id"] for o in world.host_turn("p1")["options"]]
    second = [o["id"] for o in world.host_turn("p1")["options"]]
    assert first == second


def test_三种口味各先取一个(world):
    """设计稿 §3.2 的起点:一个安全的、一个有风险的、一个社交的。"""
    pool = [
        {"id": f"opt:verb:a{i}", "kind": "verb", "tone": "safe"} for i in range(9)
    ] + [
        {"id": "opt:talk:x", "kind": "talk", "tone": "social"},
        {"id": "opt:verb:z", "kind": "verb", "tone": "risky"},
    ]
    got = host_mod.select_options(pool, limit=3)
    assert {o["tone"] for o in got if o["kind"] != "free"} == {"safe", "risky", "social"}


def test_名额是零时也还有自由输入():
    got = host_mod.select_options([{"id": "a", "kind": "verb", "tone": "safe"}], limit=0)
    assert [o["kind"] for o in got] == ["free"]


# ── 四个时刻:闸在引擎里 ─────────────────────────────────────────────────────

def test_没到时刻就闭嘴_原样返回上一屏(world):
    first = world.host_turn("p1")
    assert first["scene"]["source"] in ("mock", "llm")
    again = world.host_turn("p1")
    assert again["scene"]["source"] == "cached"
    assert again["scene"]["text"] == first["scene"]["text"]


def test_换了地方就再开一次口(world):
    world.host_turn("p1")
    world.player_move("p1", "workshop")
    turn = world.host_turn("p1")
    assert turn["trigger"] == "arrive"
    assert turn["scene"]["source"] != "cached"


def test_新的一天算一个时刻(world):
    world.host_turn("p1")
    world.tick(288)
    assert world.host_turn("p1")["trigger"] == "new_day"


def test_他点我该干嘛_受冷却管而且冷却值带在返回里(world):
    """站点对世界只有 `/internal/v1/*`,够不着 `contract --json` ——
    一个到不了消费方的冷却值等于没有这个冷却值。"""
    world.config_set("host.ask_cooldown_ticks", 999999)
    world.host_turn("p1")
    turn = world.host_turn("p1", ask=True)
    assert turn["scene"]["source"] == "cached", "冷却里连点十下不该是十次 LLM"
    assert turn["ask_ready"] is False
    assert turn["ask_ready_tick"] > turn["tick"]
    world.config_set("host.ask_cooldown_ticks", 0)
    assert world.host_turn("p1", ask=True)["scene"]["source"] != "cached"


def test_刚开口那一次_那个读数说的是这一屏不是上一屏(world):
    """🔴 **一个读数和它旁边那扇门说两句话,比没有这个读数更坏。**

    上一版拿"写之前读到的那一份"算冷却:换个地方(引擎当场写了一屏新的)之后,
    返回里写着 `ask_ready: true`,而紧接着的 `ask=True` 却答 `cached` ——
    站点照着它把按钮点亮,玩家点下去没反应。

    ⚠️ 上面那条用例咬不住它:那儿的冷却是 999999,`last` 是几 tick 前的,
    两种算法都答 `false`。**咬得住它的是"上一屏早就过了冷却、而这一屏刚写下"** ——
    这也是"试牙也要试对地方"在这一族里的第二次。
    """
    world.config_set("host.ask_cooldown_ticks", 12)
    world.host_turn("p1")
    world.tick(60)                                    # 上一屏早就过了冷却
    world.player_move("p1", "workshop")               # → arrive,当场写一屏新的
    turn = world.host_turn("p1")
    assert turn["scene"]["source"] != "cached", "这一趟确实开了口"
    assert turn["ask_ready_tick"] == turn["tick"] + 12, "冷却该从这一刻起算"
    assert turn["ask_ready"] is False

    # 读数说按不动 —— 那么它就真的按不动,两边说同一句话。
    asked = world.host_turn("p1", ask=True)
    assert asked["scene"]["source"] == "cached"

    world.tick(12)
    later = world.host_turn("p1")
    assert later["ask_ready"] is True
    assert world.host_turn("p1", ask=True)["scene"]["source"] != "cached", (
        "读数说按得动,那扇门就得真的开"
    )


def test_刷新之后开场还在_不靠调用方记住(tmp_path):
    """场景是生成出来的、不可复现 —— 所以它落成一条事件,那一屏是它的投影。"""
    with open_world_at(tmp_path / "r.db") as w:
        w.player_move("p2", "cafe")
        w.tick(2)
        text = w.host_turn("p2")["scene"]["text"]
    with open_world_at(tmp_path / "r.db") as w2:      # 同一个世界,另开一次
        again = w2.host_turn("p2")
        assert again["scene"]["text"] == text
        assert again["scene"]["source"] == "cached"


def test_开口那一下落成一条事件(world):
    world.host_turn("p1")
    rows = [e for e in world.scheduler.event_log.replay() if e.type == "host_scene"]
    assert len(rows) == 1
    assert rows[0].payload["player_id"] == "p1"
    assert rows[0].payload["text"]
    assert rows[0].payload["options"]


def test_那条事件不在插件订得到的白名单上():
    """宁少勿多:进了那张表就是一句拿不掉的公开契约。"""
    from anima_world.events import SUBSCRIBABLE_EVENTS
    assert "host_scene" not in SUBSCRIBABLE_EVENTS
    assert "player_join" not in SUBSCRIBABLE_EVENTS


# ── 🔴 藏起来的人 ───────────────────────────────────────────────────────────

def _hidden_names(world) -> list[str]:
    out = []
    for row in world.roster()["agents"]:
        if str(row.get("billing")) == "hidden":
            out += [str(row.get("agent_id")), str(row.get("name"))]
    return [n for n in out if n]


def test_藏起来的人不进候选_也不进散文(world):
    """那三扇结构化的门壳能按行筛,而这一屏是散文,名字是模型写进去的 ——
    壳筛不了,**筛一半比不筛更坏**,所以这道闸只能在引擎侧。"""
    hidden = _hidden_names(world)
    assert hidden, "橱窗里本来就有一个 billing:hidden 的人,夹具没了这一条就白测"
    world.player_move("p1", "workshop")               # 他就在那儿
    turn = world.host_turn("p1")
    blob = json.dumps(turn, ensure_ascii=False)
    for name in hidden:
        assert name not in blob, f"{name!r} 漏进了这一屏"
    assert not [o for o in turn["options"] if o["kind"] == "talk"
                and o["door"]["params"].get("agent_id") in hidden]


def test_给模型的那份提示里根本没有他的名字(world):
    """**不是"给了再叮嘱它别说"** —— 提示里没有第二个名字来源,模型手上就没有他。"""
    hidden = _hidden_names(world)
    world.player_move("p1", "workshop")
    options = host_mod.select_options(
        world._host_candidates("p1", "workshop",
                               {loc["id"]: loc for loc in world.state()["locations"]},
                               world.player_options("p1")),
        limit=5,
    )
    messages = host_mod.scene_messages(
        place_name="建筑工作室", place_desc="", day=1, hour=9, minute=0,
        world_setting="", options=options,
    )
    blob = json.dumps(messages, ensure_ascii=False)
    for name in hidden:
        assert name not in blob, f"{name!r} 进了给模型的提示"


def test_模板句也不含他的名字(world):
    """mock 和真提示两边各拼一份的话,mock 迟早会说出一个藏起来的人的名字。"""
    hidden = _hidden_names(world)
    world.player_move("p1", "workshop")
    options = host_mod.select_options(
        world._host_candidates("p1", "workshop",
                               {loc["id"]: loc for loc in world.state()["locations"]},
                               world.player_options("p1")),
        limit=5,
    )
    text = host_mod.mock_scene(place_name="建筑工作室", day=1, hour=9, options=options)
    for name in hidden:
        assert name not in text


# ── 边界:没有本体层的世界,主持人照样开口 ───────────────────────────────────

BARE = {
    "agents": [{"id": "阿岚", "name": "阿岚", "location": "村口", "personality": "安静"}],
    "locations": [{"id": "村口", "name": "村口", "description": "一棵歪脖子树。"},
                  {"id": "井边", "name": "井边", "description": "水很凉。"}],
}


def test_没写kinds的世界_blocked不吞掉主持人(tmp_path):
    """`player_options` 在那种世界里直接 `blocked:"no_ontology"` 且 `own` 是空的 ——
    而人、地点、邀请这三类选项一个都不依赖本体层。"""
    path = write_seed_file(tmp_path / "bare.json", BARE)
    with open_world_at(tmp_path / "b.db", world_file=path) as w:
        w.player_move("p3", "村口")
        w.tick(2)
        turn = w.host_turn("p3")
        assert turn["blocked"] == "no_ontology"
        assert turn["blocked_text"], "那句人话要原样透传"
        assert turn["scene"]["text"].strip()
        kinds = {o["kind"] for o in turn["options"]}
        assert "talk" in kinds and "travel" in kinds
        assert len(turn["options"]) >= 3


def test_没有key的时候整条路走模板_而且不碰网(tmp_path):
    """没配 key 是这个引擎的默认状态 —— 一个"没 key 就不成立"的机制等于缺席。"""
    path = write_seed_file(tmp_path / "m.json", BARE)
    with open_world_at(tmp_path / "m.db", world_file=path) as w:
        w.player_move("p4", "村口")
        w.tick(2)
        turn = w.host_turn("p4")
        assert turn["scene"]["source"] == "mock"
        assert "村口" in turn["scene"]["text"]


def test_没有player_id当场拒绝(world):
    with pytest.raises(ValueError):
        world.host_turn("  ")


def test_还没落脚的人_那句话也要念得通(tmp_path):
    """拿一个空的地名去拼,出来的是「你在。」—— **一句念不通的话和一句错的一样贵**。
    这一格是拿真 CLI 敲出来的,不是想出来的。"""
    with open_world_at(tmp_path / "n.db") as w:
        turn = w.host_turn("nowhere")          # 从没走过一步
        assert turn["place"] == ""
        assert "你在。" not in turn["scene"]["text"]
        assert turn["scene"]["text"].strip()
        assert turn["blocked"] == "unknown_player_location"
        assert [o for o in turn["options"] if o["kind"] == "travel"], "至少得给他几个去处"
