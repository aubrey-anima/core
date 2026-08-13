"""**按钮上得写得出字来** —— 玩家那一侧的能力表要自带选项。

## 缺口原本长什么样

一次真人试玩里,网站问引擎要玩家能做什么,拿回来的是这个:

    interact —— "东西的 id 和它能被怎么做,都写在你感觉到的那几行里"
      target: "东西的 id,像 tree:harbor_oak"

两句话都是**写给她的**。「你感觉到的那几行」是她的提示词里的一个块,玩家那一侧
根本没有那个东西;`tree:harbor_oak` 是橱窗世界里的 id,和这个世界一点关系没有。
于是宿主只剩一条路:画一个文本框,让人自己猜一个 id 和一个动词打进去。

**这不是文案问题。** 同一份声明有两个读者(她、和一个人),而这一层只写给了一个。
写给人的那一半要能**直接渲染成按钮**:这儿有什么、它能被怎么做、这会儿点得动吗、
点不动是为什么。

## 三条不变量

1. **数据来自已有的那条路** —— perception(她/他感知得到什么)+ ontology(这东西
   能被怎么做)+ affordance 的那几道闸(这会儿成不成)。新造第二套真相的下场是
   菜单说得动、点下去世界不认,而**不报错**。
2. **四类拒绝一个都不许合并**:`conditions`(等一会儿)/ `incapable`(去补足)/
   `busy`(先把手上这件做完)/ 讲不通的那一摞。合成一句"现在不能",一个累坏了的
   人会挨扇窗点过去。
3. **一个字节都不写**。看一眼菜单不该改变世界 —— 它每一帧都要被渲染一次。
"""
from __future__ import annotations

import json

import pytest

from _worldfile import write_seed_file, open_world_at, run_cli, redis_for


_SEED = {
    "locations": [
        {"id": "cafe", "name": "咖啡店", "description": "拐角那家"},
        {"id": "yard", "name": "后院", "description": "堆着几只空木箱"},
    ],
    "agents": [{"id": "夏", "name": "苏晚夏", "location": "cafe", "personality": "开朗"}],
    "kinds": [
        {"id": "agent", "quantities": {
            "体力": {"default": 100.0, "visibility": "self", "unit": "点"},
        }},
        {"id": "window", "gloss": "一扇窗", "quantities": {
            "干净": {"default": 0.0, "visibility": "here"},
        }, "affordances": {
            "擦一擦": {
                "when": ["干净 < 10"],
                "requires": ["me_体力 >= 4"],
                "set": {"干净": "干净 + 1"},
                "costs": {"体力": "me_体力 - 4"},
            },
            "同擦": {
                "label": "一起擦",
                "participants": {"min": 1, "max": 2},
                "requires": ["me_体力 >= 4"],
                "set": {"干净": "干净 + 3"},
                "costs": {"体力": "me_体力 - 4"},
            },
            "大扫除": {
                "duration": 6,
                "occupies": True,
                "requires": ["me_体力 >= 10"],
                "set": {"干净": "干净 + 8"},
                "costs": {"体力": "me_体力 - 10"},
            },
        }},
    ],
    "entities": [
        {"id": "window:cafe", "name": "临街那扇", "location": "cafe"},
        {"id": "window:yard", "name": "后院那扇", "location": "yard"},
    ],
    "config": {"social.joint.min_willingness": 0},
}


@pytest.fixture
def world(open_world, tmp_path):
    seed = json.loads(json.dumps(_SEED))
    return open_world(world_file=write_seed_file(tmp_path / "w.cyberworld", seed))


def _targets(world, player_id="p1"):
    return {row["id"]: row for row in world.player_options(player_id)["targets"]}


def _verbs(row):
    return {v["verb"]: v for v in row["verbs"]}


# ── 菜单说的是人话,不是写给她的那几行 ──────────────────────────────────────


def test_玩家那一侧的说明不指望她的提示词(world):
    """`interact` 的说明原文指路指到「你感觉到的那几行」—— 那是她的提示词里的一个块。
    玩家那一侧没有那个东西,于是这句话对一个人等于没说。"""
    menu = {row["id"]: row for row in world.player_tools()}
    text = menu["interact"]["description"] + json.dumps(
        menu["interact"]["params_schema"], ensure_ascii=False
    )
    assert "感觉到的那几行" not in text, "玩家那一侧的说明还在指她的提示词"
    assert "tree:harbor_oak" not in text, (
        "拿橱窗世界的 id 当例子 —— 每个别的世界里它都是一句错的话"
    )


def test_菜单自带选项_宿主照它画按钮(world):
    """给了 `player_id` 的菜单要把"这儿有什么、能怎么做"直接挂在参数上。
    不挂的话宿主只能画一个文本框,而文本框里打什么它自己也不知道。"""
    world.player_move("p1", "cafe")
    menu = {row["id"]: row for row in world.player_tools("p1")}
    opts = menu["interact"]["params_schema"]["target"]["options"]
    assert [o["value"] for o in opts] == ["window:cafe"]
    assert opts[0]["label"] == "临街那扇", "按钮上印 id 等于没画"
    verb_opts = menu["interact"]["params_schema"]["verb"]["options"]
    assert {o["value"] for o in verb_opts} >= {"擦一擦", "同擦", "大扫除"}
    # 不给 player_id 的那份**一个字都不变** —— 老调用方照旧。
    plain = {row["id"]: row for row in world.player_tools()}
    assert "options" not in plain["interact"]["params_schema"]["target"]


# ── 选项本身:这儿有什么、能被怎么做、这会儿点得动吗 ────────────────────────


def test_这儿有什么_带着人话名字和那一行说明(world):
    world.player_move("p1", "cafe")
    rows = _targets(world)
    assert set(rows) == {"window:cafe"}, "别处那扇不该出现在他的菜单上"
    assert rows["window:cafe"]["name"] == "临街那扇"
    assert rows["window:cafe"]["gloss"] == "一扇窗"
    assert rows["window:cafe"]["kind"] == "window"


def test_每个动词自带能不能点_以及自造动词的人话(world):
    world.player_move("p1", "cafe")
    verbs = _verbs(_targets(world)["window:cafe"])
    assert verbs["擦一擦"]["available"] is True
    assert verbs["擦一擦"]["reason"] == ""
    # `label` 是作者给的人话。按钮上印 `同擦` 而作者写的是「一起擦」,等于把作者
    # 那一栏白写了。
    assert verbs["同擦"]["label"] == "一起擦"


def test_要人一起的那条自报它要人一起(world):
    """点下去才发现"这件事得有人一起做",是一次没有任何人预告过的失败。"""
    world.player_move("p1", "cafe")
    row = _verbs(_targets(world)["window:cafe"])["同擦"]
    assert row["available"] is False
    assert row["reason"] == "participants_missing"
    assert row["participants"] == {"min": 1, "max": 2}
    assert row["candidates"], "要人一起,却不说这儿有谁"
    assert row["candidates"][0]["name"] == "苏晚夏"


# ── 四类拒绝,一个都不许合并 ────────────────────────────────────────────────


def test_他累了那条是做不了_不是这会儿不行(world):
    world.player_move("p1", "cafe")
    world.scheduler.stock_store.set_many("agent:player:p1", {"体力": 1.0}, tick=0)
    verbs = _verbs(_targets(world)["window:cafe"])
    assert verbs["擦一擦"]["reason"] == "incapable"
    assert verbs["擦一擦"]["refusal"], "说不行却不说为什么"


def test_世界说这会儿不行是另一类(world):
    world.player_move("p1", "cafe")
    world.scheduler.stock_store.set_many("window:cafe", {"干净": 10.0}, tick=0)
    verbs = _verbs(_targets(world)["window:cafe"])
    assert verbs["擦一擦"]["reason"] == "conditions"
    # **同一扇窗上的两个动词各判各的。** 一样东西只给一个"能不能动"的总答案,
    # 就会把「它已经够干净了,不用再抹了」读成「这扇窗这会儿碰不得」。
    assert verbs["大扫除"]["available"] is True


def test_他手上还有一件事没做完是第四类(world):
    """`busy` 该等自己手上这件做完 —— 既不是换一扇窗,也不是去补体力。"""
    world.player_move("p1", "cafe")
    started = world.player_tool(
        "p1", "interact", {"target": "window:cafe", "verb": "大扫除"}
    )
    assert started["ok"] is True, started
    verbs = _verbs(_targets(world)["window:cafe"])
    assert verbs["擦一擦"]["reason"] == "busy"
    assert verbs["大扫除"]["reason"] == "busy"


# ── 看一眼菜单不该改变世界 ──────────────────────────────────────────────────


def test_算一遍菜单一个字节都不写(world):
    """它每一帧都要被渲染一次。算一次掉一次体力的话,一个开着页面发呆的人会
    在世界里累死,而没有一行日志说这不对劲。"""
    world.player_move("p1", "cafe")
    before_events = len(world.scheduler.event_log.replay())
    for _ in range(5):
        world.player_options("p1")
    assert world.stocks("window:cafe")["干净"] == 0.0
    assert world.scheduler.stock_store.of("agent:player:p1").get("体力") == 100.0
    assert len(world.scheduler.event_log.replay()) == before_events


def test_世界不知道他在哪时说得出原因(world):
    """空菜单加一句沉默,读起来像"这儿什么也没有" —— 而真相是宿主没调过
    `player_move`,一个玩家怎么点都改不了。"""
    out = world.player_options("nobody")
    assert out["targets"] == []
    assert out["blocked"] == "unknown_player_location"
    assert out["location"] == ""


def test_在路上的时候不算站在任何地方(world):
    world.player_move("p1", "cafe")
    world.player_walk("p1", "yard")
    out = world.player_options("p1")
    assert out["targets"] == []
    assert out["blocked"] == "in_transit"


# ── 空菜单的那句话也是人话 ───────────────────────────────────────────────────
#
# `blocked` 是**给机器的**枚举,而站点把它原样印在了玩家屏幕上 —— 线上真的这样:
# 一个正在赶路的人打开面板,看见的是一行 `in_transit`。这不是站点粗心:它旁边那块
# 货架面板自己写了一句「你还在路上 —— 到了再看」,两块姊妹面板于是一块说人话、
# 一块说引擎的话。**分叉的原因是这句话没有主人。**
#
# 和每个动词那对 `reason`(枚举)+ `refusal`(人话)逐字同构:那一对是引擎写的,
# 这一句也该是 —— `_too_poor` 的注释早就写明了理由「另写一遍的话,两句迟早分叉」。


def _blocked_words(world, player_id):
    out = world.player_options(player_id)
    return out["blocked"], out["blocked_text"]


def test_空菜单的原因给两份_一份给机器一份给人(world):
    """枚举留着(宿主要分支),旁边多一句他读得懂的。"""
    reason, said = _blocked_words(world, "nobody")
    assert reason == "unknown_player_location", "枚举是契约,不许改成人话"
    assert said and reason not in said, f"引擎的词漏给玩家了:{said!r}"

    world.player_move("p1", "cafe")
    world.player_walk("p1", "yard")
    reason, said = _blocked_words(world, "p1")
    assert reason == "in_transit"
    assert said and reason not in said, f"引擎的词漏给玩家了:{said!r}"
    assert "路上" in said, f"得说出他为什么点不动:{said!r}"


def test_没被挡住的时候那句话是空的(world):
    """有东西可点还挂一句"你做不了",比不说更坏。"""
    world.player_move("p1", "cafe")
    out = world.player_options("p1")
    assert out["blocked"] == "" and out["blocked_text"] == ""


def test_每一个挡住的理由都配了一句话(world):
    """新加一个 `blocked` 枚举而忘了配话,玩家屏幕上就又出现一个英文标识符 ——
    而那一天没有任何一处会报错。所以这条按**引擎自己那张表**逐个点名。
    """
    from anima_world.api import World

    for reason in World._BLOCKED_WORDS:
        said = World._BLOCKED_WORDS[reason]
        assert said and reason not in said, f"{reason} 这一格还是引擎的话:{said!r}"


# ── CLI 出口 ────────────────────────────────────────────────────────────────


def test_cli_能打出一个玩家此刻的菜单(tmp_path):
    db = tmp_path / "w.db"
    redis_for(db)
    seed = json.loads(json.dumps(_SEED))
    path = write_seed_file(tmp_path / "w.cyberworld", seed)
    with open_world_at(str(db), force_mock_llm=True, world_file=path) as world:
        world.player_move("p1", "cafe")

    result = run_cli("player", "options", "--world-id", "w", "--player", "p1", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    # 在场表落 Redis(3.2.0)之后,**另一个进程真的看得见他站在哪儿** —— 所以这里
    # 等的是一份真菜单。这条断言同时是那次搬家的跨进程验收:它在搬家之前必红
    # (那时 `players` 是进程内的裸 dict,CLI 只会回 `unknown_player_location`)。
    assert payload["blocked"] == ""
    assert payload["location"] == "cafe"
    assert [t["id"] for t in payload["targets"]] == ["window:cafe"]


def test_cli_不认识的玩家说得出为什么是空的(tmp_path):
    """从没露过面的人:菜单是空的,而**空要说得出为什么**。

    不说的话宿主只能把一份空菜单画成"这儿什么都没有" —— 而真相是引擎不知道
    这个人在哪儿,两件事该让玩家看到完全不同的界面。
    """
    db = tmp_path / "w.db"
    redis_for(db)
    path = write_seed_file(tmp_path / "w.cyberworld", json.loads(json.dumps(_SEED)))
    with open_world_at(str(db), force_mock_llm=True, world_file=path):
        pass

    result = run_cli("player", "options", "--world-id", "w", "--player", "陌生人", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["blocked"] == "unknown_player_location"
    assert payload["targets"] == []


def test_cli_对不存在的世界一律拒绝(tmp_path):
    redis_for(tmp_path / "w.db")
    result = run_cli("player", "options", "--world-id", "nope", "--player", "x")
    assert result.returncode == 2


# ── 拒绝里印的是那样东西的名字,不是它的 id ─────────────────────────────────


_ITEM_SEED = json.loads(json.dumps(_SEED))
_ITEM_SEED["items"] = [
    {"id": "notepad", "name": "一叠点播条", "kind": "durable", "base_price": 5.0},
]
_ITEM_SEED["kinds"][1]["affordances"]["投一张"] = {
    "consumes": {"notepad": 1},
    "set": {"干净": "干净 + 1"},
}


def test_缺东西那句拒绝印的是人话名字_不是英文_id(open_world, tmp_path):
    """线上真的印给玩家看了:「你手上的 notepad 不够:要 1 个,你有 0 个」——
    一个全中文的世界,而这个世界自己的 `item_defs` 里 `notepad` 就叫「一叠点播条」。

    晚潮那一局里 13 样东西全中(paint / soil / battery / coal / tar / bulb /
    token / redstring / chum / bone / oil / patch / notepad)。而这句话不只上玩家
    的屏幕:`tools/body.py` 把它当 `ToolResult.error` 递回给**她**,于是一个说中文
    的角色会把 `notepad` 念出来。
    """
    world = open_world(world_file=write_seed_file(tmp_path / "i.cyberworld", _ITEM_SEED))
    world.player_move("p1", "cafe")
    verb = _verbs(_targets(world)["window:cafe"])["投一张"]
    assert verb["available"] is False and verb["reason"] == "incapable"
    assert "一叠点播条" in verb["refusal"], (
        f"拒绝里印的是 id 不是名字:{verb['refusal']!r}"
    )
    assert "notepad" not in verb["refusal"]


_HAVE_SEED = json.loads(json.dumps(_ITEM_SEED))
_HAVE_SEED["kinds"][1]["affordances"]["点一首"] = {
    "requires": ["have_notepad >= 2"],
    "set": {"干净": "干净 + 1"},
}


def test_requires_里的_have_也翻成人话(open_world, tmp_path):
    """`have_` 和 `consumes` 那道门印的是同一样东西,不该一处说人话一处说 id。

    作者两种写法都合法(`consumes` 自带门,但他也可以显式写 `requires`),而玩家
    看到的是哪一句纯属实现细节 —— 两条路上漏出同一个 `notepad` 才是这个 bug 的
    完整形状。
    """
    world = open_world(world_file=write_seed_file(tmp_path / "h.cyberworld", _HAVE_SEED))
    world.player_move("p1", "cafe")
    verb = _verbs(_targets(world)["window:cafe"])["点一首"]
    assert verb["available"] is False and verb["reason"] == "incapable"
    assert "一叠点播条" in verb["refusal"], (
        f"拒绝里印的是 id 不是名字:{verb['refusal']!r}"
    )
    assert "have_" not in verb["refusal"] and "notepad" not in verb["refusal"]
    assert ">= 2" in verb["refusal"], "还差多少要说得出来"


# ── 按下去之前就说清要付什么 ────────────────────────────────────────────────
#
# 试玩现场:玩家在咖啡车上点了「重描一遍」,按钮亮着、点下去成了,然后接下来一
# 小时的世界时间里,他点什么都只得到一句「你手上还有一件事没做完」。
#
# 那句拒绝本身是对的(第四类 `busy`,而且刚刚修成人话了)。错的是**它来晚了**:
# 一个把人锁住一小时的按钮,在按下去之前和一个瞬间完成的按钮长得一模一样。
# 玩家没有任何办法预防,而预防不了的代价教不会他任何东西 —— 这正是 `duration`
# 那一格的注释里写着的同一条理由("关口只在起头查一次",因为付了十个月再被拒
# 掉他没法预防)。
#
# 代价这件事只有引擎说得出来:`duration` / `occupies` 住在本体声明里,而宿主
# 从来看不见本体。所以这一句归引擎写,宿主原样显示 —— 和四类拒绝的措辞同一条。


def test_要花时间的那条_按下去之前就说要花多久(world):
    world.player_move("p1", "cafe")
    verbs = _verbs(_targets(world)["window:cafe"])
    said = verbs["大扫除"]["cost"]
    assert said, "一个把人锁住半小时的按钮,不该和一个瞬间完成的按钮长得一样"
    assert "30 分钟" in said, f"6 个 tick × 5 分钟:{said!r}"
    assert "tick" not in said, f"引擎的词漏给玩家了:{said!r}"


def test_占不占用他是代价的真实形状(world):
    world.player_move("p1", "cafe")
    said = _verbs(_targets(world)["window:cafe"])["大扫除"]["cost"]
    assert "做不了别的" in said, (
        f"「要花 30 分钟」和「这 30 分钟里做不了别的」是两件事:{said!r}"
    )


def test_一下子的事不说废话(world):
    """每条都挂一句"要花 0 分钟"的话,真正要花时间的那条就淹了。"""
    world.player_move("p1", "cafe")
    assert _verbs(_targets(world)["window:cafe"])["擦一擦"]["cost"] == ""
