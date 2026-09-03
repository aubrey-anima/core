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


# ── 2a-②:第五个时刻「你回来了」(3.10.0)──────────────────────────────────

def test_离线太久回来_是return那一屏(tmp_path):
    """🔴 **两个判据都是减法,零新状态**:`host_scene` 载荷里本来就有 `tick`,
    在场行本来就带 TTL。加一张「谁什么时候离开过」的表是这一层最容易的错 ——
    那是第二份真相,而它和日志对不上时没有一处会报错。
    """
    from _worldfile import open_world_at

    with open_world_at(tmp_path / "ret.db") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        assert world.host_turn("p1")["trigger"] == "arrive"
        world.tick(288 * 2)
        world.player_leave("p1")            # 在场行没了 = 他真的走了
        turn = world.host_turn("p1")
        assert turn["trigger"] == "return", turn["trigger"]
        assert "没来了" in turn["scene"]["text"], turn["scene"]["text"]


def test_一直在玩的人_世界过了一天也不算他回来(tmp_path):
    """🔴 **"世界走了多久"不是"他离开了多久"**,而这两个数在屏幕上长得一模一样。

    第一版拿「现在的 tick 减他上一屏那个 tick」当离线 —— 而一个一直在玩的人拿到的
    多半是 `cached`,`last.tick` 根本不往前走,于是世界过了一天他会被告知
    「你有 1 天没来了」。判据换成**在场行还在不在**(而且必须在 `_touch_player`
    之前问:那一句本身就会把 TTL 续上,问晚一步答案永远是"在")。
    """
    from _worldfile import open_world_at

    with open_world_at(tmp_path / "stay.db") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        world.host_turn("p1")
        for _ in range(3):                  # 一边玩一边过日子
            world.tick(288)
            world.host_turn("p1")
        assert world.host_turn("p1")["trigger"] != "return"


def test_他上一屏之后装了新包_也是return_而且说得出本周更新(tmp_path):
    """「本周更新」读的是 `pack_installed`,**不是一份另攒的公告栏** —— 攒一份就多
    一种和日志对不上的坏法,而那时横幅上写着这周加了三件事,世界里一件都没有。"""
    from _worldfile import open_world_at, write_seed_file
    from anima_world.world_file import (
        WorldFileManifest, seed_to_author_records, write_world_file,
    )

    with open_world_at(tmp_path / "pk.db") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        assert world.host_turn("p1")["trigger"] == "arrive"
        path = tmp_path / "week2.cyberworld"
        write_world_file(
            path, WorldFileManifest(world_id="f", engine_min="3.10.0"),
            seed_to_author_records({"pack": {"id": "第二周", "version": "1.0.0",
                                             "note": "社团活动 / 夜宵"}}),
            compress=False, checksum=False)
        world.install_pack(str(path))
        turn = world.host_turn("p1")
        assert turn["trigger"] == "return", turn["trigger"]
        assert "社团活动" in turn["scene"]["text"], turn["scene"]["text"]


def test_return进了contract的moments(tmp_path):
    from anima_world.__main__ import contract_payload

    assert contract_payload()["host"]["moments"] == [
        "arrive", "new_day", "beat", "acted", "ask", "return"]


def test_藏起来的人在回顾里只叫有人_而事情照说(world):
    """🔴 **这一条是 3.10.1 补的漏**(验收 A ①):候选那道闸 3.9.0 就立了,
    而**回顾这一段从来没过它** —— 一个 `billing: "hidden"` 的角色给玩家转 500 块,
    屏幕上就印着「<他的名字>给了你 500 块。」,而且 `mock_scene` 与
    `scene_messages` **两条路一起漏**(两条都读同一个 `recap_lines`)。

    ⚠️ **事情本身照说,藏的是「谁」**:钱包多了 500 必须说 —— 整条吞掉会让他
    在一个"钱莫名其妙变了"的世界里做决定,那是另一种坏。

    这一条按**纯函数**测三支(转账 / 给东西 / 她约你),因为 `recap_lines` 的
    docstring 自己写着它存在的理由就是"哪几件算数、怎么说可以被单独测";
    下一条测的是**引擎真的把那份名单传进来了**。
    """
    rows = [
        {"type": "payment", "who": "暗", "payload": {"from": "暗", "to": "player:p1",
                                                     "amount": 500}},
        {"type": "item_transfer", "who": "暗",
         "payload": {"from": "暗", "to": "player:p1", "item_id": "k", "item_name": "钥匙"}},
        {"type": "agent_invites",
         "payload": {"player_id": "p1", "agent_id": "暗", "agent_name": "黑衣人",
                     "verb_label": "喝一杯", "target_name": "吧台"}},
    ]
    said = "\n".join(host_mod.recap_lines(
        rows, player_key="player:p1", agent_names={"暗": "黑衣人"},
        hidden_agents={"暗"}))
    assert "黑衣人" not in said and "暗" not in said, said
    # 三件事本身一件都不许少
    assert "500" in said and "钥匙" in said and "喝一杯" in said, said
    assert said.count(host_mod.HIDDEN_WHO) == 3, said

    # 试牙:不传那份名单,名字当场漏出来 —— 这正是 3.10.1 之前的样子。
    leaked = "\n".join(host_mod.recap_lines(
        rows, player_key="player:p1", agent_names={"暗": "黑衣人"}))
    assert "黑衣人" in leaked, leaked


def test_引擎真的把藏起来的那份名单传进了回顾(world):
    """上一条测的是纯函数会筛,这一条测的是**引擎真的把名单交给了它** ——
    两件事分开测,因为 3.10.1 之前坏的恰恰是后者(函数没有那个参数,
    而调用方当然也没传)。用**真发射点**:让那个藏起来的人真的开口约玩家。
    """
    hidden = [str(r["agent_id"]) for r in world.roster()["agents"]
              if str(r.get("billing")) == "hidden"]
    assert hidden, "橱窗里本来就有一个 billing:hidden 的人"
    him = hidden[0]
    names = _hidden_names(world)

    world.player_move("p1", "workshop")
    world.host_turn("p1")                    # 先有「上一屏」,回顾才有窗口起点
    world.scheduler.invite_player(
        him, "p1", target="bench", verb="talk", party=[], text="来一下",
        agent_name=world.scheduler.agent_display_name(him),
    )
    lines = world._host_recap("p1", since_seq=1)
    said = "\n".join(lines)
    assert said, "那条邀请一个字都没进回顾"
    for name in names:
        assert name not in said, f"藏起来的人「{name}」从回顾漏出来了:{said}"
    assert host_mod.HIDDEN_WHO in said, said

def test_回顾里的动词和东西是人话_不是英文id(world):
    """🔴 **验收 A ② 在真站上量到的那一句**:「你look了tree:harbor_oak」。

    病根是 `entity_interaction` 的三处发射点**一格人话都不写**,而回顾读的正是
    `verb_label` / `target_name`。⚠️ **用真发射点,不手塞 payload** ——
    手塞的用例证明的是"回顾会读这两格",而漏掉的恰恰是"发射点会写这两格"。
    """
    world.player_move("p1", "cafe")
    world.host_turn("p1")
    options = world.player_options("p1")
    target = next((t for t in options["targets"] for v in t["verbs"] if v["available"]), None)
    assert target is not None, "橱窗里该有一样点得动的东西"
    verb = next(v for v in target["verbs"] if v["available"])
    world.player_tool("p1", "interact", {"target": target["id"], "verb": verb["verb"]})

    events = [e for e in world.events() if e["type"] == "entity_interaction"]
    assert events, "没发出 entity_interaction"
    payload = events[-1]["payload"]
    assert payload.get("verb_label"), f"发射点没写 verb_label:{payload}"
    assert payload.get("target_name"), f"发射点没写 target_name:{payload}"
    # 而且它们是人话:不是内部 id
    assert payload["target_name"] == target["name"]
    # ⚠️ 该查的是**散文**,不是整个 JSON:实体 id 出现在 `door.params` 里是
    # **机器契约**(主持人是荐者,那扇门收的就是真 id)。第一版这里查了整个
    # blob,当场红 —— 而红得对:那是我把契约当成了泄漏。
    said = "".join(host_mod.recap_lines(
        [{"type": "entity_interaction", "who": "player:p1", "payload": payload}],
        player_key="player:p1"))
    assert said, "回顾一个字都没说"
    assert target["id"] not in said and verb["verb"] not in said, f"裸 id / 英文动词:{said}"
    assert target["name"] in said, said


# ── 第六个时刻:他自己刚做了一件事(3.11.0,批 3a)──────────────────────────


def test_同地同日点一个动词_屏也要换(tmp_path):
    """🔴 **这一条是「每操作一次就有新剧情」的判据本身。**

    在这一格之前,钥匙是 `(place, day, beat_seq)` —— 同一个地方、同一天里
    做一件事,`_host_trigger` 答 `None`、`scene.source` 是 `cached`:
    屏幕一动不动,而世界里真的发生了一件事。
    """
    from _worldfile import open_world_at

    with open_world_at(tmp_path / "acted.db") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        assert world.host_turn("p1")["trigger"] == "arrive"
        # 同地同日,再问一次 —— 没动手,就该闭嘴。
        assert world.host_turn("p1")["scene"]["source"] == "cached"
        world.player_action("p1", "看了看布告栏")
        turn = world.host_turn("p1")
        assert turn["trigger"] == "acted", turn["trigger"]
        assert turn["scene"]["source"] != "cached"
        # 而做完那一下之后不再动手,它照旧闭嘴 —— `acted` 是一次跃迁不是一盏常亮的灯。
        assert world.host_turn("p1")["scene"]["source"] == "cached"


def test_邀请过期不算他动手_他答了才算(tmp_path):
    """**「拒绝」和「过期」必须分得开** —— 这条邀请那一层写死的分界,在这一层
    的落法是:他没来得及答,屏幕不该因此重开一屏说「你刚做了什么」。

    ⚠️ 这一条试的是 `player_move_seq_of` 那个纯函数**被真的接上了**:
    两种结局走的是同一条 `invitation_settled`,只差 `payload.outcome` 一格。
    """
    from anima_world.host import player_move_seq_of

    for outcome, expected in (("expired", False), ("cancelled", False),
                              ("accepted", True), ("declined", True)):
        assert player_move_seq_of(
            "invitation_settled", {"player_id": "p1", "outcome": outcome},
            "xia", "player:p1") is expected, outcome


def test_那两条who是她的事件_按payload筛才筛得出来(tmp_path):
    """🔴 **这一格是这一层最容易写错的地方**(裁决 §2.10 ①)。

    `conversation` / `invitation_settled` 的 `who` 是**她**,不是他 ——
    照 `who` 筛的话这两种**永远筛不出来**,而下场是「聊完一轮屏幕不动」,
    零报错。试牙:把 `who` 换成她的 id,两条照样要答 True。
    """
    from anima_world.host import player_move_seq_of

    assert player_move_seq_of(
        "conversation", {"participants": [{"id": "p1", "kind": "user"}]},
        "苏晚夏", "player:p1") is True
    # 别人的会话不算他的操作
    assert player_move_seq_of(
        "conversation", {"participants": [{"id": "p2", "kind": "user"}]},
        "苏晚夏", "player:p1") is False


def test_别的state_change不算他动手(tmp_path):
    """到站那一条走的是 `state_change{kind: "location_join"}`,而**关系变了、
    人设被改写**走的是同一种事件 —— 只认 `type` 不认 `kind` 的话,一次好感度
    变化会被当成「他操作了一次」,于是编剧对着一件他没做过的事写下一拍。"""
    from anima_world.host import player_move_seq_of

    assert player_move_seq_of("state_change", {"kind": "location_join"},
                              "player:p1", "player:p1") is True
    assert player_move_seq_of("state_change", {"kind": "sentiment_delta"},
                              "player:p1", "player:p1") is False


def test_一条要花时间的动词_按下去也得有一句话(tmp_path):
    """🔴 **验收 C ③(真站上量的)**:批 1.1 承诺「按下去世界会说一句」,而一个
    带 `duration` 的动词(龙族那个 `报到`)回执里
    `{started, duration, ends_tick, occupies, changed:{}}` 一应俱全,
    **`text` 是空的** —— 玩家按下去,屏幕纹丝不动。

    病根:`_player_said` 挂在两个出口上,而「起了个头」是**第三、第四个**出口。
    ⚠️ **它说的必须是另一句话**:那件事开了个头,不是做完了 ——
    用同一句「你…了…」是在撒谎。

    ⚠️ **用真插件动词的形状**(带 `duration` 的自造动词),不是内置那几个:
    C 量到的就是插件动词那条路。
    """
    from _worldfile import open_world_at, write_seed_file

    seed = {
        "agents": [{"id": "甲", "name": "甲", "location": "hall", "personality": "温和"}],
        "locations": [{"id": "hall", "name": "大厅", "kind": "point", "x": 0, "y": 0,
                       "description": "很高的穹顶"}],
        "kinds": [{"id": "desk", "quantities": {"人气": {"default": 0.0,
                                                        "visibility": "here"}},
                   "affordances": {"报到": {"label": "报到", "duration": 6,
                                            "occupies": True,
                                            "set": {"人气": "人气 + 1"}}}}],
        "entities": [{"id": "desk:lion", "name": "狮心会报到处", "location": "hall"}],
    }
    path = write_seed_file(tmp_path / "dur.cyberworld", seed)
    with open_world_at(tmp_path / "dur.db", world_file=path) as world:
        world.player_move("p1", "hall")
        world.tick(2)
        got = world.player_tool("p1", "interact",
                                {"target": "desk:lion", "verb": "报到"})
        assert got["ok"], got
        assert got["detail"].get("started") is True, got["detail"]
        assert got.get("text"), f"起了个头却一个字不说:{got}"
        assert "报到" in got["text"] and "狮心会报到处" in got["text"], got["text"]
        # **说的是"开始了",不是"做完了"** —— 他还没做完
        assert got["text"] != "你报到了狮心会报到处。", got["text"]
