"""窗她擦得了,我也擦得了 —— 玩家也是世界里的一个施动者。

## 缺口原本长什么样

本体层这一轮长出了整层 `interact`:她读得到"这儿有扇窗,可以擦一擦",挑得动那个
动词,擦完真的少一把力气。而玩家那一侧的菜单(`player_tools()`)上只有"说句话"和
"走过去" —— 同一个世界里于是有**两套物理**:她那套有量、有代价、有做不到;人那套
只有走和说。

这不是"少了个按钮"。世界的说服力全在"我做的事和她做的事是同一件事"上:她照料那棵
树会累,我照料那棵树不会,那棵树就不是我们俩共处的同一棵树了。

## 补上的四样,以及各自守的东西

1. **他身上也有量。** `agent` 那份声明对他一样生效,落在 `_touch_player` 这个窄口上
   (角色那一半在 `Scheduler.register`)。少了它,`requires: ["me_体力 >= 4"]` 对他
   恒不成立 —— 世界里每一件要力气的事他都做不了,而回执只说"你做不了"。
2. **"他在哪"只有一个答案。** `_where_is` 一处分支,两种人都从那一句问出去。
   少了玩家那一半的样子是:他站在窗前点"擦一擦",世界回一句"它不在你这儿"。
3. **扣账不分人。** 一起做的事从前先把玩家滤掉,于是他白干:不掉体力、不烧材料,
   账上也看不出这顿饭是两个人付的钱。
4. **菜单要说得出前提。** `requires_target_entity` 不报出来的话,宿主会在一个
   空屋子里画一个点下去必然失败的按钮,而失败的原因写在引擎里、它那侧看不见。

## 这个文件守的

一律走 `World.player_tool()` 那条**真路** —— 直接调 `scheduler.perform_affordance`
的测试验不出这条链上任何一处断掉(创世投影折两遍那个 bug 就是这么漏过去的)。
"""
from __future__ import annotations

import json

import pytest

from _worldfile import write_seed_file


_SEED = {
    "locations": [
        {"id": "cafe", "name": "咖啡店", "description": "拐角那家"},
        {"id": "yard", "name": "后院", "description": "堆着几只空木箱"},
    ],
    "agents": [{"id": "夏", "name": "苏晚夏", "location": "cafe", "personality": "开朗"}],
    "kinds": [
        {"id": "agent", "quantities": {
            "体力": {"default": 100.0, "visibility": "self", "unit": "点"},
            # 起过人话名字的量。菜单上它叫「腕力」,拒绝句里也必须叫「腕力」——
            # 内部键漏出去的话玩家会去找一样屏幕上根本没有的东西。
            "腕子劲": {"default": 5.0, "visibility": "self", "label": "腕力"},
        }},
        {"id": "window", "gloss": "一扇窗", "quantities": {
            # 作者给这个量起过人话名字、也分过档 —— 她读到的就是这两样。
            # 玩家那一屏要读到同样的两样,这是 `readouts` 那条测试守的。
            "干净": {
                "default": 0.0,
                "visibility": "here",
                "label": "擦得多干净",
                "bands": [[0, "刚够看"], [10, "锃亮"]],
            },
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
            # 这两条只为一件事:让三种拒绝各说一次话。
            "抛光": {"requires": ["me_腕子劲 >= 999"], "set": {"干净": "干净 + 5"}},
            "上蜡": {
                "requires": ["me_腕子劲 >= 1"],
                "consumes": {"蜡": 1},
                "set": {"干净": "干净 + 2"},
            },
            # 阈值**落在档中间**的一条(5 在「刚够看」里,不在 0 / 10 那两条边界上)。
            # 档词说的是这一档的起点,所以这里的 `>=` 念出来必须松一格 —— 见 `_LOOSER`。
            "验收": {"when": ["干净 >= 5"], "set": {"干净": "干净 + 1"}},
            # 同一个比较号,阈值**压在边界上**(10 正是「锃亮」那档的起点)——
            # 这一条不许松,松了就是凭空多要一格。
            "封蜡": {"when": ["干净 >= 10"], "set": {"干净": "干净 + 1"}},
            # 拦在**世界自己那份量**上的一条。它的名字只写在 `stock_visibility` 里,
            # 不属于任何种类 —— 查本体的话这个名字压根不存在。
            "开窗透气": {"when": ["world_雨势 < 0.1"], "set": {"干净": "干净 + 1"}},
        }},
    ],
    "stocks": [{"owner": "world", "values": {"雨势": 0.8}}],
    "stock_visibility": [
        {"kind": "world", "key": "雨势", "visible": "public", "label": "雨",
         "bands": [[0, "没下"], [0.1, "在下"], [0.6, "瓢泼大雨"]]},
    ],
    "items": [{"id": "蜡", "name": "一小罐蜡", "price": 3}],
    "entities": [{"id": "window:cafe", "name": "临街那扇", "location": "cafe"}],
    # 这个世界里的人不挑同伴。**不是为了让测试变绿**:一个跟你毫无来往的人不肯陪你
    # 干活是红线 ① 的正确答案(`test_joint_activity` 专门守着它),而这一份验的是
    # 答应之后**账怎么记** —— 两件事分开验,合在一起的话前一条一变,后一条跟着假绿。
    "config": {"social.joint.min_willingness": 0},
}


@pytest.fixture
def world(open_world, tmp_path):
    seed = json.loads(json.dumps(_SEED))
    return open_world(world_file=write_seed_file(tmp_path / "w.cyberworld", seed))


def _wipe(world, player_id="p1", **params):
    return world.player_tool(
        player_id, "interact", {"target": "window:cafe", "verb": "擦一擦", **params}
    )


def _stamina(world, owner):
    return world.scheduler.stock_store.of(owner).get("体力")


# ── 他身上的量 ──────────────────────────────────────────────────────────────


def test_他一露面身上的量就落地了(world):
    """`agent` 那份声明对他一样生效。没落地的话 `me_体力` 恒为 0 —— 那道门永远
    关着,而回执只说"你做不了",一个字不提原因。"""
    world.player_move("p1", "cafe")
    # 逐个量填,不是逐个实体填:少一个量,读它的那道门就永远关着或永远开着。
    assert world.stocks("agent:player:p1") == {"体力": 100.0, "腕子劲": 5.0}


def test_已经有的值不被第二次露面盖回默认(world):
    """只填缺不覆盖,和创世那条同一句。覆盖的话,一个宿主每聊一轮调一次
    `player_move`,他的体力就永远满 —— 代价成了摆设。"""
    world.player_move("p1", "cafe")
    _wipe(world)
    world.player_move("p1", "cafe")
    assert _stamina(world, "agent:player:p1") == 96.0


# ── 他擦得动那扇窗 ──────────────────────────────────────────────────────────


def test_他擦一次窗真的干净了_他也真的累了(world):
    world.player_move("p1", "cafe")
    out = _wipe(world)
    assert out["ok"] is True, out
    assert out["detail"]["changed"] == {"干净": 1.0}
    assert out["detail"]["me_changed"] == {"体力": 96.0}
    assert out["detail"]["me_delta"] == {"体力": -4.0}
    assert world.stocks("window:cafe")["干净"] == 1.0
    assert _stamina(world, "agent:player:p1") == 96.0
    assert _stamina(world, "agent:夏") == 100.0, "扣的不该是她的账"


def test_这一下记在他名下(world):
    """事件的 `who` 是 `player:{id}` —— 记成她的话,历史里这扇窗是她擦的,
    而八卦、记忆、关系全长在那条事件上。"""
    world.player_move("p1", "cafe")
    _wipe(world)
    rows = [e for e in world.events() if e["type"] == "entity_interaction"]
    assert rows and rows[-1]["who"] == "player:p1"
    assert rows[-1]["loc"] == "cafe"


def test_他在别处就擦不了_而且两头都说的是地名不是键名(world):
    """只说"它在 cafe"会读成一句谎:他也可能就在 cafe,而真正的原因是引擎不知道
    **他**在哪(在路上就是这样)。

    而两头说出来的必须是**人话地名**。这句拒绝印在玩家屏幕上(`player_options`
    的 `refusal`),也当 `ToolResult.error` 递回给她 —— 「它在 cart,你在 noodle」
    是键名漏出来的样子,`place_name` 的 docstring 里记着这条教训的另一半
    (判定器写摘要那一处),`perform_affordance` 这一处当时漏了。
    """
    world.player_move("p1", "yard")
    out = _wipe(world)
    assert out["ok"] is False
    assert "咖啡店" in out["error"] and "后院" in out["error"], (
        f"两头都要说,而且说的是地名:{out['error']!r}"
    )
    assert "cafe" not in out["error"] and "yard" not in out["error"], (
        f"键名不该印给人看:{out['error']!r}"
    )
    assert world.stocks("window:cafe")["干净"] == 0.0


def test_做不了那句里没有引擎的前缀(world):
    """`me_` 是引擎的命名空间标记,不是这个世界里的名字 —— 作者声明的量叫「体力」。

    线上现场:「你现在做不了这件事:`me_主动 >= 1.2` 不成立」。玩家从中读不出任何
    可做的事,而这句话还会当 `ToolResult.error` 递给角色,于是她把 `me_` 念出来。
    **但阈值要留着**:抹掉算术就只剩一句"你做不了",而拒绝之所以教得会人,
    全靠它说得出还差什么。
    """
    world.player_move("p1", "cafe")
    world.scheduler.stock_store.set_many("agent:player:p1", {"体力": 1.0}, tick=0)
    out = _wipe(world)
    assert out["detail"]["reason"] == "incapable"
    assert "me_" not in out["error"], f"引擎的前缀漏出来了:{out['error']!r}"
    assert "你的体力" in out["error"] and ">= 4" in out["error"], (
        f"还差什么要说得出来:{out['error']!r}"
    )


def test_拒绝句里的量叫的是菜单上那个名字(world):
    """一个量,全世界只能有一个叫法。

    线上现场:菜单上写着「土 正好」,点下去被拒绝成「「土湿 > 0.55」不成立」——
    同一个量、同一屏、两个名字,而屏幕上根本没有「土湿」这个词。玩家会去找一样
    不存在的东西,而这条误导一次报错都不会有。`readouts` 走量声明里的 `label`,
    拒绝语也必须走同一份声明。

    三个作用域各查各的主人,所以三句都要验:裸名字查这个东西、`me_` 查这个人、
    `have_` 查东西的名字。
    """
    world.player_move("p1", "cafe")

    # 裸名字 —— 查的是这扇窗自己那份声明。「干净」分过档,所以阈值也念成档词。
    world.scheduler.stock_store.set_many("window:cafe", {"干净": 10.0}, tick=0)
    out = _wipe(world)
    assert out["detail"]["reason"] == "conditions"
    assert out["error"] == "这会儿不行:「擦得多干净 < 锃亮」不成立", out["error"]

    # `me_` —— 查的是他自己那份声明。
    out = world.player_tool("p1", "interact", {"target": "window:cafe", "verb": "抛光"})
    assert out["detail"]["reason"] == "incapable"
    assert "你的腕力 >= 999" in out["error"], f"{out['error']!r}"
    assert "腕子劲" not in out["error"], f"内部键漏出来了:{out['error']!r}"


def test_分过档的量拒绝语里念档词_没分档的留数字(world):
    """「`label` 换名字、`bands` 换值」的后一半 —— 它一直只做到了前一半。

    线上现场是同一屏上两行字:菜单上写着「土 正好」,按钮底下写着「土 > 0.55」。
    分过档的量,玩家**永远**读不到它的数字(`readout_text` 的规矩就是这样),
    于是 `0.55` 对他是一串没法比对的噪音 —— 和 `me_` 前缀、和内部键同一类,
    引擎的东西漏到了世界的话里。

    反过来**没分档的量必须留着数字**:「你的体力 >= 4」里那个 4 他在菜单上读得到
    (`体力 100点`),留着才教得会他还差多少。两半一起验,只验一半的话把数字
    一律抹掉也是绿的,而那正是这条规矩要拒绝的做法。
    """
    world.player_move("p1", "cafe")

    world.scheduler.stock_store.set_many("window:cafe", {"干净": 10.0}, tick=0)
    out = _wipe(world)
    assert "锃亮" in out["error"], f"档词没念出来:{out['error']!r}"
    assert "10" not in out["error"], f"分过档还漏数字:{out['error']!r}"

    # 屏幕上念的是同一个词 —— 这条规矩的全部意思就是这两处不许分叉。
    target = next(
        t for t in world.player_options("p1")["targets"] if t["id"] == "window:cafe"
    )
    row = next(r for r in target["readouts"] if r["key"] == "干净")
    assert row["word"] == "锃亮", row

    # 没分档的那一半:体力有单位没有档,数字照旧。
    world.scheduler.stock_store.set_many("agent:player:p1", {"体力": 1.0}, tick=0)
    out = _wipe(world)
    assert out["detail"]["reason"] == "incapable"
    assert "你的体力 >= 4" in out["error"], f"没分档的量不该抹掉数字:{out['error']!r}"


def test_阈值落在档中间时大于等于要松一格(world):
    """档词说的是**这一档的起点**,不是那个数,所以 `>=` 有时会自相矛盾。

    线上现场是「你的手上的活儿 >= 生手」印在一个屏幕上正写着「手上的活儿 生手」的
    人眼前:他明明就在这一档里,却被告知要够到这一档。念作「> 生手」才对 ——
    要的是比这一档更往上。这不是引擎在骗人,是档词有损,而有损是作者的分辨率在说话。

    ⚠️ 反过来**阈值压在边界上时不许动**:那时档词和那个数说的是同一件事,「>= 锃亮」
    读作"要够到锃亮",没有一处矛盾;松成「> 锃亮」是凭空多要一格。两条用的是**同一个
    比较号、同一个量、同一屏**,只差阈值落在哪儿 —— 分开验的话"一律松一格"也是绿的。
    """
    world.player_move("p1", "cafe")

    def _refuse(verb):
        out = world.player_tool("p1", "interact", {"target": "window:cafe", "verb": verb})
        assert out["detail"]["reason"] == "conditions", out
        return out["error"]

    # 5 落在「刚够看」(0–10)当中,不在任何一条边界上 —— 松一格。
    assert "擦得多干净 > 刚够看" in _refuse("验收")
    # 10 正是「锃亮」那档的起点 —— 原样留着。
    assert "擦得多干净 >= 锃亮" in _refuse("封蜡")

    # 屏幕上此刻正写着「刚够看」—— 头一句要是印成 `>= 刚够看`,两行就打架。
    target = next(
        t for t in world.player_options("p1")["targets"] if t["id"] == "window:cafe"
    )
    row = next(r for r in target["readouts"] if r["key"] == "干净")
    assert row["word"] == "刚够看", row


def test_世界自己那份量的名字也念得出来(world):
    """`world_` 那一档的名字**只写在 `stock_visibility` 里**,不属于任何种类。

    所以「拒绝语和屏幕念同一份声明」这件事,查本体是查不到的 —— 本体里根本没有
    `world` 这个种类的量,查到的永远是空,于是内部键原样漏给玩家。线上真的是这样:
    菜单上写着「江水位(米) 2.4」,点下去被拒绝成「世界的江水位」。
    可见性表才是并集(本体那份是它的上游,装载时播进去的),所以要查它。
    """
    world.player_move("p1", "cafe")
    out = world.player_tool("p1", "interact", {"target": "window:cafe", "verb": "开窗透气"})
    assert out["ok"] is False
    assert out["detail"]["reason"] == "conditions"
    assert "世界的雨 < 在下" in out["error"], f"{out['error']!r}"
    assert "雨势" not in out["error"], f"内部键漏出来了:{out['error']!r}"


def test_少材料那句把东西的名字划出边界(world):
    """中文不分词,而这句拒绝是散文:名字右边紧跟着一个「不」字。不划边界的话
    「你手上的一小罐蜡不够」读起来像是在说蜡的量词,玩家看不出该去买什么。
    """
    world.player_move("p1", "cafe")
    out = world.player_tool("p1", "interact", {"target": "window:cafe", "verb": "上蜡"})
    assert out["ok"] is False
    assert out["detail"]["reason"] == "incapable"
    assert "「一小罐蜡」不够" in out["error"], f"{out['error']!r}"
    assert "要 1 个,你有 0 个" in out["error"], (
        f"还差几个要说得出来:{out['error']!r}"
    )


def test_他累了收到的是做不了_不是这会儿不行(world):
    """两类拒绝她该做的事相反,他也一样:`conditions` 该等一会儿再来,
    `incapable` 该先去歇着。合成一个的话,一个累坏了的人会挨扇窗试过去。"""
    world.player_move("p1", "cafe")
    world.scheduler.stock_store.set_many("agent:player:p1", {"体力": 1.0}, tick=0)
    out = _wipe(world)
    assert out["ok"] is False
    assert out["detail"]["reason"] == "incapable"
    assert _stamina(world, "agent:player:p1") == 1.0, "拒绝时一个字都不写"


def test_世界说不行时窗和他都没变(world):
    world.player_move("p1", "cafe")
    world.scheduler.stock_store.set_many("window:cafe", {"干净": 10.0}, tick=0)
    out = _wipe(world)
    assert out["detail"]["reason"] == "conditions"
    assert _stamina(world, "agent:player:p1") == 100.0


# ── 一起擦 ──────────────────────────────────────────────────────────────────


def test_两个人一起擦时他也付他那一份(world):
    """从前名单先被滤掉玩家那一半,于是他白干:不掉体力、不烧材料,而账面上
    看不出这件事是两个人做的。"""
    world.player_move("p1", "cafe")
    out = world.player_tool(
        "p1", "interact",
        {"target": "window:cafe", "verb": "同擦", "with": ["夏"]},
    )
    assert out["ok"] is True, out
    assert world.stocks("window:cafe")["干净"] == 3.0, "目标身上的量只写一份"
    assert _stamina(world, "agent:player:p1") == 96.0
    assert _stamina(world, "agent:夏") == 96.0


def test_他不能跟自己一起擦_而且那句话是说给他听的(world):
    """回执的口气跟着施动者走。他点一次"跟我一起",收到一句"她不能跟自己……",
    说的是另一个人。"""
    world.player_move("p1", "cafe")
    out = world.player_tool(
        "p1", "interact",
        {"target": "window:cafe", "verb": "同擦", "with": ["我"]},
    )
    assert out["ok"] is False
    assert out["error"].startswith("你不能跟自己")


# ── 菜单与他看得见的东西 ────────────────────────────────────────────────────


def test_菜单上有它_而且说得出两个前提(world):
    """宿主照这份画按钮。少报一条前提,它就会在一个空屋子里画一个点下去必然
    失败的按钮 —— 而失败的原因写在引擎里,它那侧看不见。"""
    menu = {row["id"]: row for row in world.player_tools()}
    assert "interact" in menu, "她能擦、他不能,是同一个世界里的两套物理"
    assert menu["interact"]["requires_target_entity"] is True
    assert menu["interact"]["requires_colocation"] is False
    assert "with" in menu["interact"]["params_schema"], (
        "菜单上没有的参数没人会填,于是要一起做的那些能力对他等于不存在"
    )


def test_他看得见这儿有什么_以及那东西能被怎么做(world):
    """给了动词却不给"这儿有什么",宿主只能自己拿 `entities()` 和 `kinds()` 拼一份
    能力表 —— 而拼错了不报错,按钮点下去才发现世界不认。"""
    world.player_move("p1", "cafe")
    seen = world.player_perception("p1")
    assert seen["here"].get("window:cafe", {}).get("干净") == 0.0
    assert "擦一擦" in seen["verbs"]["window:cafe"]
    assert seen["own"]["体力"] == 100.0, "他自己的量也照同一份可见性声明来"


def test_菜单上的量报的是作者写的名字_不是内部键(world):
    """**这一屏上其余每一个名字都翻过了,只有量没翻。**

    东西的名字翻了(`name`)、那行说明翻了(`gloss`)、动词翻了(`label`)、
    拒绝翻成了人话(`refusal`)、代价翻成了人话(`cost`)—— 然后 `quantities`
    把 `phrase_age` / `tests_taken` 这样的内部键原样递出去,而宿主唯一能做的就是
    把它印在屏上(它没有别的东西可印)。线上那一屏于是长这样:「今日短语
    phrase_age 3」。作者明明写了 `label: "days since it changed"` 和 `unit`。

    `bands` 同理,而且更要紧:她读到的是「瓢泼大雨」,玩家读到的是 `雨势 0.82` ——
    同一个世界的同一个量,两个人看见两种东西。
    """
    world.player_move("p1", "cafe")
    opts = world.player_options("p1")
    target = next(t for t in opts["targets"] if t["id"] == "window:cafe")
    assert target["quantities"] == {"干净": 0.0}, "键与数字是契约,不许被改写"
    chips = {r["key"]: r for r in target["readouts"]}
    assert chips["干净"]["label"] == "擦得多干净", "作者给量起过名字,这一屏要用它"
    assert chips["干净"]["text"] == "擦得多干净 刚够看", "分档的量报档词,不报数字"

    me = opts["own"]
    assert me["readouts"][0]["text"] == "体力 100点", "没分档的量报数字,单位跟着数字走"


def test_他走了一半的时候不算站在任何地方(world):
    """在途不是"还在出发地"。算成出发地的话,他一边在路上一边擦着咖啡店的窗。"""
    world.player_move("p1", "cafe")
    world.player_walk("p1", "yard")
    out = _wipe(world)
    assert out["ok"] is False and "别处" in out["error"]


# ── 他也在"正在做什么"那张名单上 ────────────────────────────────────────────


_ACTION_RULES = [
    {"id": "开口的人松下来", "every": {"ticks": 1},
     "for_each": {"action": "chat"}, "set": {"随和": "随和 + 1"}},
    {"id": "闷着的人紧起来", "every": {"ticks": 1},
     "for_each": {"not_action": "chat"}, "set": {"闷头": "闷头 + 1"}},
    {"id": "赶路的人出汗", "every": {"ticks": 1},
     "for_each": {"action": "walk"}, "set": {"汗": "汗 + 1"}},
    {"id": "手上有活", "every": {"ticks": 1},
     "for_each": {"action": "interact"}, "set": {"手上活": "手上活 + 1"}},
]


@pytest.fixture
def rule_world(open_world, tmp_path):
    """同一个世界,外加四条按"此刻在做什么"分支的规律。"""
    seed = json.loads(json.dumps(_SEED))
    for kind in seed["kinds"]:
        if kind["id"] == "agent":
            kind["quantities"].update({
                q: {"default": 0.0, "visibility": "self"}
                for q in ("随和", "闷头", "汗", "手上活")
            })
        if kind["id"] == "window":
            # 占着人的那一件:20 tick 干完,期间他手上就是这件事。
            kind["affordances"]["擦到底"] = {
                "duration": 20, "occupies": True, "set": {"干净": "干净 + 5"},
            }
    seed["rules"] = _ACTION_RULES
    return open_world(world_file=write_seed_file(tmp_path / "r.cyberworld", seed))


def _mine(world, key, player_id="p1"):
    return world.scheduler.stock_store.of(f"agent:player:{player_id}").get(key)


def test_他说话的那会儿_那半边规律真的算到他头上(rule_world):
    """**世界的规律说"正在说话的人会变随和"时,说的是这个世界里的人,不分她和他。**

    她身上那半边由行为树写(`_current_action`),而人没有行为树 —— 于是
    `{"action": …}` 这半边规律里**从来没有过一个人**。线上那个世界 21 个角色的
    「随和」「手艺」「嗓子」每 tick 都在动,而每一个玩家的这三个量停在他进世界
    那一 tick 的默认值上,一动不动:日志干净、面板照画,照跑但给错东西。
    """
    world = rule_world
    world.player_move("p1", "cafe")
    list(world.chat("夏", [{"role": "user", "content": "你好"}], player_id="p1"))
    before = _mine(world, "随和")
    world.tick(1)
    assert _mine(world, "随和") > before, "他开着口,而那半边规律算不到他头上"


def test_而那一刻他不该同时算进另外那半边(rule_world):
    """互补的两半必须真的互补。

    `{"not_action": …}` 是"所有角色减去正在做这件事的",而人本来就有
    `stock:agent:player:*` 那一行 —— 所以他一直在这半边里。少了前一半的话,
    两半对他是**单边**的:他只吃得到往下拖的那一条,吃不到往上走的那一条。
    """
    world = rule_world
    world.player_move("p1", "cafe")
    list(world.chat("夏", [{"role": "user", "content": "你好"}], player_id="p1"))
    before = _mine(world, "闷头")
    world.tick(1)
    assert _mine(world, "闷头") == before, "他正说着话,却同时被算成闷着不吭声的"


def test_他在路上的那会儿算在赶路那半边(rule_world):
    """三个来源都是真的、当下的状态 —— 在途是其中一个,而且是唯一一个
    "他自己停不下来"的:存一份"他上次说他在做什么"的话,一个关掉浏览器的人
    会在世界里永远地走下去。"""
    world = rule_world
    world.player_move("p1", "cafe")
    world.player_walk("p1", "yard")
    assert world.player_in_transit("p1"), "前提没立住:他压根没上路"
    before = _mine(world, "汗")
    world.tick(1)
    assert _mine(world, "汗") > before, "他在路上,而赶路那半边规律算不到他"


def test_占着他的那件长过程也算他手上有活(rule_world):
    """优先级是约束由强到弱:占用 > 赶路 > 说话 —— 和拒绝那三类的排法同一条。"""
    world = rule_world
    world.player_move("p1", "cafe")
    out = world.player_tool(
        "p1", "interact", {"target": "window:cafe", "verb": "擦到底"}
    )
    assert out["ok"] is True, out
    before = _mine(world, "手上活")
    world.tick(1)
    assert _mine(world, "手上活") > before, "那件事正占着他,而世界不知道他手上有活"
