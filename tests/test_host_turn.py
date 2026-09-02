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
    # 🆕 3.10.0(批 1.1 ③):**进门那一屏之后的第一次问不起冷却** —— 见下面
    # 那条用例。所以要验冷却,得先把那一次免费的用掉。
    assert world.host_turn("p1", ask=True)["scene"]["source"] != "cached"
    turn = world.host_turn("p1", ask=True)
    assert turn["scene"]["source"] == "cached", "冷却里连点十下不该是十次 LLM"
    assert turn["ask_ready"] is False
    assert turn["ask_ready_tick"] > turn["tick"]
    assert turn["ask_ready_text"], "按不动的时候要说一句还要等多久"
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
    world.host_turn("p1", ask=True)                   # 用掉进门那一次免费的(③)
    world.tick(288)                                   # 上一屏早就过了冷却,而且换了一天
    turn = world.host_turn("p1")                      # → new_day,当场写一屏新的
    assert turn["trigger"] == "new_day"
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


def test_在路上的人_两扇门说同一句话(world):
    """🔴 从前 host 读在场那一行的原始 `location`,而 `player_options` 读它自己那条路
    —— 一个正在赶路的人被两扇门各说了一句:那扇门答 `blocked:in_transit`,
    而主持人照样报"你在咖啡店"、递上"和苏晚夏说说话(点得动)"。
    **两扇门对同一时刻说两句话,比其中任何一句错更难查。**"""
    world.player_walk("p1", "home")
    menu = world.player_options("p1")
    turn = world.host_turn("p1")
    assert menu["blocked"] == "in_transit"
    assert turn["blocked"] == menu["blocked"]
    assert (turn["place"], turn["place_name"]) == (menu["location"], menu["location_name"])
    assert "路上" in turn["scene"]["text"], "场景不该说他在出发地"

    for option in turn["options"]:
        if option["kind"] in ("travel", "free"):
            # 实测:在途再 walk 一次是**改主意重新起程**,不是被拒 ——
            # 把它一起灰掉等于凭空发明一条世界不认识的规矩。
            assert option["available"] is True, option["label"]
        else:
            assert option["available"] is False, option["label"]
            assert option["refusal"] == menu["blocked_text"], "那句人话该原样借,不另写"


def test_到了地方就恢复(world):
    world.player_walk("p1", "home")
    world.host_turn("p1")
    world.tick(200)
    turn = world.host_turn("p1")
    assert turn["blocked"] == ""
    assert turn["place"] == "home"
    assert any(o["available"] and o["kind"] not in ("travel", "free")
               for o in turn["options"]) or turn["place_name"]


def test_模板句里那几个人名是人名_不是按钮上的字(world):
    """从前拼的是 `label`,于是出来「这儿有人:和苏晚夏说说话。」——
    **一句念不通的话和一句错的一样贵**。候选自己带着 `who`,别从按钮上抠。"""
    turn = world.host_turn("p1")
    text = host_mod.mock_scene(place_name="咖啡店", day=0, hour=14,
                               options=turn["options"])
    assert "和苏晚夏说说话" not in text
    if "这儿有人" in text:
        assert "苏晚夏。" in text or "苏晚夏、" in text


def test_每一项都带一格who_而契约报着它(world):
    from anima_world.__main__ import contract_payload
    assert "who" in contract_payload()["host"]["option_keys"]
    for option in world.host_turn("p1")["options"]:
        assert "who" in option
        if option["kind"] in ("verb", "travel", "free"):
            assert option["who"] == "", "这几类没有「人」这一格"


# ── 批 1.1 ①②:先说刚发生了什么 · 人称与时辰(3.10.0)──────────────────────
#
# 🔴 **验收 C 2026-09-02 在真站上量的两条**:
# ① 第一拍响了(录取通知 + 一部 N96 + 800 块),而屏幕上一个字没提 —— 钱包突然
#    多了 800,没有一处说为什么。主持人这一屏是玩家**唯一**读得到的地方。
# ② day 0 **00:25** 的场景里写着「黄昏」「暮色」;`mock_scene` 用「你」而给模型的
#    那份提示整份用「他」—— 同一个世界,配了 key 和没配 key 是两种人称。

def test_上一屏之后发生的事_排在景的前面(tmp_path):
    from anima_world import host as host_mod

    with open_world_at(tmp_path / "h11.db") as world:
        world.player_move("p1", "cafe")
        world.tick(3)
        first = world.host_turn("p1")
        assert first["scene"]["source"] == "mock"
        # 世界给他 800 块、一件东西 —— 走的是账本已有的那两条事件。
        world.scheduler._record_and_deliver({
            "type": "payment", "who": "player:p1",
            "payload": {"from": "__town__", "to": "player:p1",
                        "amount": 800, "reason": "beat"},
        })
        world.scheduler._record_and_deliver({
            "type": "item_transfer", "who": "player:p1",
            "payload": {"from": "__world__", "to": "player:p1",
                        "item_id": "n96", "item_name": "一部诺基亚 N96", "qty": 1},
        })
        world.player_walk("p1", "workshop")   # 换地方 → arrive,这一屏会重说
        world.tick(2)
        again = world.host_turn("p1")
        text = again["scene"]["text"]
        assert "800" in text, f"钱包多了 800,屏幕上一个字没提:{text!r}"
        assert "N96" in text, f"他手上多了东西,屏幕上一个字没提:{text!r}"
        # 🔴 **顺序是承重的**:先说刚发生的事,再说景。
        assert text.index("800") < text.index("你在"), text


def test_第一屏没有回顾_而不是把整个创世倒给他(tmp_path):
    """⚠️ 一个刚进门的人身后没有「上一屏之后」。拿 0 当窗口起点会把创世那一摞
    (每个人的 `agent_join`、安家费、货架)全倒进他的第一屏。"""
    with open_world_at(tmp_path / "h12.db") as world:
        world.player_move("p1", "cafe")
        world.tick(3)
        first = world.host_turn("p1")
        assert first["scene"]["text"].startswith("第 "), first["scene"]["text"]


def test_时辰按世界钟分档_深夜不是清晨():
    from anima_world.host import daypart, free_option, mock_scene

    assert [daypart(h) for h in (0, 6, 10, 14, 18, 22)] == [
        "深夜", "清晨", "上午", "午后", "黄昏", "夜里"]
    said = mock_scene(place_name="你家", day=0, hour=0, options=[free_option()])
    assert "深夜" in said and "清晨" not in said, said


def test_给模型那份提示_人称是你_而且时辰是喂进去的():
    """🔴 **两条路的人称必须一样**,而差别只在一个环境变量上(有没有 key)。"""
    from anima_world.host import free_option, scene_messages

    msgs = scene_messages(place_name="你家", place_desc="", day=0, hour=0, minute=25,
                          world_setting="", options=[free_option()],
                          recap=["一封录取通知躺在你桌上。"])
    whole = msgs[0]["content"] + msgs[1]["content"]
    assert "第二人称" in msgs[0]["content"]
    assert "他在" not in whole and "他还没落脚" not in whole, whole
    assert "(深夜)" in whole and "「深夜」" in whole, whole
    # 刚发生的事排在最前,并且明说先说它。
    assert whole.index("一封录取通知") < whole.index("地点:"), whole


# ── 批 1.1 ③:进门那一屏之后的第一次问,不起冷却 ────────────────────────────

def test_进门那一屏之后第一次问_不起冷却(world):
    """🔴 **真站实测的那条**:龙族 `ask_cooldown_ticks=12`,而一 tick 是 5 真实分钟
    —— 一个刚进门的新玩家,「我该干嘛」那颗按钮**整整 60 真实分钟按不动**,
    而那正是他最需要按它的一个小时。

    冷却防的是「连点十下 = 十次 LLM 调用」,而**进门第一次问根本不是那件事**。
    """
    world.config_set("host.ask_cooldown_ticks", 999999)
    first = world.host_turn("p1")
    assert first["trigger"] == "arrive"
    assert first["ask_ready"] is True, "进门那一屏上,那颗按钮就该是亮的"
    assert first["ask_ready_text"] == "", "按得动的时候不该还挂着一句「还要等」"

    asked = world.host_turn("p1", ask=True)
    assert asked["scene"]["source"] != "cached", "进门第一次问被冷却挡了"
    # 用掉之后照常起冷却 —— 这一格没有被拆掉。
    assert world.host_turn("p1", ask=True)["scene"]["source"] == "cached"


def test_还要等多久是一句人话_而且按这个世界自己的时钟折算(world):
    """🔴 **别让宿主做 tick 数学。** 「还有几 tick」要变成「还要等几分钟」得知道
    `scheduler.tick_rate`,而站点对世界只有 `/internal/v1/*`、够不着配置 ——
    让每个宿主各算一遍,就是让它们各持一份对时钟的猜测,而猜错了不报错
    (按钮早亮或晚亮)。

    ⚠️ 同一个冷却值在演示速度的世界(1 tick/秒)上是十几秒、在线上是一小时 ——
    **同一个数字在两个世界里是两件事**,这正是它不该由宿主换算的理由。
    """
    world.config_set("host.ask_cooldown_ticks", 12)
    world.host_turn("p1")
    world.host_turn("p1", ask=True)          # 用掉进门那一次免费的

    world.config_set("scheduler.tick_rate", 1.0)          # 演示速度:1 tick/秒
    fast = world.host_turn("p1")["ask_ready_text"]
    assert "秒" in fast, fast

    world.config_set("scheduler.tick_rate", 1 / 300)      # 线上:5 真实分钟一个 tick
    slow = world.host_turn("p1")["ask_ready_text"]
    assert "分钟" in slow or "小时" in slow, slow
    assert fast != slow, "两个世界的时钟不一样,这句话不该一样"


def test_turn_keys_契约与真门逐格相等(world):
    """`ask_ready_text` 是 3.10.0 加的,而这张表是下游照着写解析器的那一行。

    ⚠️ 3.9.0 那一轮 `who` 那一格漏在 REFERENCE 上,站点三处同缺一格而**没有一处
    会红** —— 一份没人验的键表,和一句没人验的话是同一种东西。
    """
    from anima_world.__main__ import contract_payload

    turn = world.host_turn("p1")
    assert sorted(contract_payload()["host"]["turn_keys"]) == sorted(turn)


# ── 批 1.2:世界先开口,而这一条要开到对话里去(3.10.0)──────────────────────
#
# 老板 2026-09-02 刷新之后真进去玩,原话:
#   「让我去跟他们说话我不知道说啥,剧情没法往下走啊,不让他们自己搭话吗」
# 🔴 **「世界永远先开口」这条纪律,3.9.0 只做进了主持人那一屏,没做进对话里。**

def test_她先开口_没配key也有一句话(world):
    """**没配 key 是默认状态** —— 这不是降级路上的边角料,而是很多人读到的第一句。"""
    said = "".join(world.chat_open("夏", "p1"))
    assert said.strip(), "她被叫起来开口,却给了一个空白气泡"
    assert "夏" in said or "苏晚夏" in said, said


def test_她先开口那一轮_静音闸照旧管得住(world):
    """🔴 **静音闸、身份、在场一格都不跳** —— 它们答的是「这一场对话成不成立」,
    而那件事和谁先开口无关。开两条 prelude 的那天,一条路上守住的边界会在另一条上漏。
    """
    from anima_world.api import AgentUnavailable

    world.chat_state.set_quiet("夏", "p1", minutes=5)
    with pytest.raises(AgentUnavailable):
        list(world.chat_open("夏", "p1"))


def test_她先开口_不认识的人当场抛(world):
    with pytest.raises(KeyError):
        list(world.chat_open("根本没有这个人", "p1"))


def test_主持人那一屏的talk项_告诉宿主她先开口(world):
    """**判断在引擎侧**:让站点自己猜"要不要让她先说",就是让它拿一份对世界的
    猜测做决定。"""
    turn = world.host_turn("p1")
    talk = [o for o in turn["options"] if o["kind"] == "talk"]
    assert talk, turn["options"]
    assert talk[0]["door"] == {"method": "chat",
                               "params": {"agent_id": talk[0]["door"]["params"]["agent_id"],
                                          "opening": True}}, talk[0]["door"]


def test_建议句_没配key也有两三条_而且可复现(world):
    """🔴 **挑什么是纯算术,LLM 只写字** —— 同一个世界同一时刻挑两次逐字相同。
    **永远不空**:一份空的建议和没有这个功能一样。"""
    first = world.chat_suggestions("夏", "p1")
    assert 1 <= len(first) <= 3, first
    assert all(s.strip() for s in first)
    assert world.chat_suggestions("夏", "p1") == first, "同一时刻两次给了不同的现实"


def test_建议句_不认识的人当场抛(world):
    with pytest.raises(KeyError):
        world.chat_suggestions("根本没有这个人", "p1")


def test_她先开口那一句_用作者写的台词_而不是把旁白塞进引号里(tmp_path):
    """🔴 **拿真 CLI 敲一遍才发现的**:`line` 和 `narrate` 不是同一种东西 ——
    `line` 是作者写在 `hail` 上的**她的台词**(「师弟!下来一趟。」),
    `narrate` 是**旁白**(「手机震了一下,是个没存过的号码。」)。
    把旁白塞进引号里当她的台词念,出来的是一句念不通的话,而**一句念不通的话
    和一句错的一样贵**。
    """
    from _worldfile import open_world_at, write_seed_file

    seed = {
        "locations": [{"id": "宿舍", "name": "宿舍", "description": "四人间"}],
        "agents": [{"id": "芬格尔", "name": "芬格尔", "location": "宿舍",
                    "personality": "话多"}],
        "beats": [{"id": "夜宵局", "for_each": {"node": "player"},
                   "trigger": {"at": {"day": 0}},
                   "narrate": "手机震了一下,是个没存过的号码。",
                   "payload": [{"op": "hail", "agent_id": "芬格尔",
                                "target": "player", "line": "师弟!下来一趟。"}]}],
    }
    path = write_seed_file(tmp_path / "line.cyberworld", seed)
    with open_world_at(str(tmp_path / "line.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.player_move("p1", "宿舍")
        world.tick(3)
        said = "".join(world.chat_open("芬格尔", "p1"))
        assert "师弟!下来一趟。" in said, said
        assert "手机震了一下" not in said, f"旁白被当成她的台词念了:{said}"


def test_只有旁白没有台词时_旁白当旁白写(tmp_path):
    """对照组:**没有它,上面那条对一个「永远不用 narrate」的实现同样成立**。"""
    from anima_world.host import mock_opening

    said = mock_opening("芬格尔", beat_note="手机震了一下,是个没存过的号码。")
    assert said.startswith("手机震了一下"), said
    assert "「手机震了一下" not in said, f"旁白被塞进引号里:{said}"
