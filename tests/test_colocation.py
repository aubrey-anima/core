"""异地就只能打电话。

在这之前**玩家是个幽灵**:不管角色在哈尔滨还是三亚,他都能面对面说话、给东西、
一起做事 —— 位置这个维度等于白设计了。而引擎里位置从来都是真的:走路花时间、同地
才看得见对方身上的量、`reach_out` 老早就拒绝不在场的人。只有玩家这一侧一直没人管。

这份守四件事,**第二件最要紧**:

1. 声明了 `requires_colocation` 的能力,玩家不在她跟前时拒绝
2. **默认关着,而关着时行为和今天逐位相同** —— 引擎侧收紧会当场打断线上世界
3. 拒绝要有回执,而且**三种原因分得开**(你在别处 / 世界不知道你在哪 / 她在赶路)
4. 玩家开口让她做的事**不受影响** —— 那是一句话,而一句话打电话也说得出来
"""
from __future__ import annotations

import json

import pytest

from _worldfile import write_seed_file

from anima_world import tools as tools_mod


_SEED = {
    "locations": [
        {"id": "cafe", "name": "咖啡店", "description": "拐角那家"},
        {"id": "yard", "name": "后院", "description": "堆着几只空木箱"},
    ],
    "agents": [{"id": "夏", "name": "苏晚夏", "location": "cafe", "personality": "开朗"}],
    "items": [{"id": "scarf", "name": "红围巾", "price": 5}],
}


@pytest.fixture
def world(open_world, tmp_path):
    w = open_world(world_file=write_seed_file(
        tmp_path / "w.cyberworld", json.loads(json.dumps(_SEED))))
    w.config_set("economy.enabled", True)
    yield w


def _give_scarf(world, player_id="p1"):
    """让玩家手上真有一条围巾 —— 库存是事件的投影,不记账的东西下一次重放就没了。"""
    world._record_and_fan({
        "type": "item_transfer", "who": "world",
        "payload": {"from": "world", "to": f"player:{player_id}",
                    "item_id": "scarf", "qty": 1, "item_name": "红围巾"},
    })
    world.scheduler.catch_up_projection()


def _direct(world, action, player_id="p1", **params):
    return world._director.direct(
        agent_id="夏", player_id=player_id,
        params={"target": "夏", "action": action, **params},
    )


# ── ① 声明在能力上,不是写死在引擎里 ────────────────────────────────────────


def test_能力自己声明要不要当面():
    """`reach_out` 的处理器一直在**手写**同一件事。声明出来改变的是"别人问得到"——
    宿主界面照它决定按钮什么时候可点,点下去才发现的话,那是一次没有任何人预告过
    的失败。"""
    assert tools_mod.get("reach_out").requires_colocation is True
    assert tools_mod.get("mute").requires_colocation is False, (
        "静音隔着手机也成立 —— 挡掉它等于宣称异地就不能拒绝一个人"
    )


def test_目录里带这一格_宿主不该靠猜(world):
    catalog = {row["id"]: row for row in world.verbs()}
    assert catalog["reach_out"]["requires_colocation"] is True
    assert catalog["walk"]["requires_colocation"] is False
    payloads = {row["id"]: row for row in tools_mod.capability_payloads()}
    assert payloads["reach_out"]["requires_colocation"] is True


# ── ② 默认关,而且关着时逐位不变 ────────────────────────────────────────────


def test_默认关着(world):
    assert world.config_get("presence.enforce_colocation") is False


def test_关着的时候异地的_give_照旧成功(world):
    """**这一条是那道闸的全部代价所在。** 立这条闸时(3.2.0,2026-08)线上一个宿主
    都没调 `player_move`,于是"异地"是每一次调用的默认值 —— 默认收紧就是当场打断
    线上世界。⚠️ 那句实况 2026-08-13 前后就过期了(站点接上了),但**这条测试的
    意义没过期**:它钉的是"关着时逐位相同",而那和线上谁调不调没关系。"""
    _give_scarf(world)
    world.player_move("p1", "yard")     # 她在 cafe
    outcome = _direct(world, "give", object="红围巾")
    assert outcome.ok is True, "开关关着时行为必须和 2.2.0 逐位相同"


def test_关着的时候连位置都不查(world):
    """宿主从没调过 `player_move` 的世界照旧能给东西。

    (括号里原本写着"也就是线上那个" —— 3.2.0 时是实话,2026-08-13 之后不是了。
    钉的东西不变:**没告诉世界你在哪** 的世界,关着闸时一切照旧。)
    """
    _give_scarf(world)
    assert _direct(world, "give", object="红围巾").ok is True


def test_关着的时候_act_不查在场(world):
    world.config_set("chat.tools.enabled", True)
    world.player_move("p1", "yard")
    result = world.act("夏", "reach_out", {"player_id": "p1"},
                       player_id="p1", surface="autonomy")
    # 处理器自己那道闸照旧兜底(它一直在那儿)—— 但拒绝的话不该是新那道闸拒的。
    assert "要当面才办得到" not in str(result.get("error") or "")


# ── ③ 开了之后:拒绝要有回执,三种原因分得开 ────────────────────────────────


def test_异地给不了东西_而且说得出两头都在哪(world):
    world.config_set("presence.enforce_colocation", True)
    _give_scarf(world)
    world.player_move("p1", "yard")
    outcome = _direct(world, "give", object="红围巾")
    assert outcome.ok is False
    assert outcome.detail["reason"] == "elsewhere"
    assert "后院" in outcome.text and "咖啡店" in outcome.text, (
        "只说一头会读成一句谎 —— 玩家不知道该往哪儿走"
    )
    assert "说话" in outcome.text, "得告诉他还剩下什么能做"


def test_世界不知道你在哪_和你站错了地方是两回事(world):
    """**这一条是这道闸最容易长出的谎。** 合成一句"你不在她跟前"的话,一个宿主
    根本没接 `player_move` 的世界,看起来会像是玩家自己站错了地方 —— 而他做什么
    都改不了。"""
    world.config_set("presence.enforce_colocation", True)
    _give_scarf(world)
    outcome = _direct(world, "give", object="红围巾")
    assert outcome.ok is False
    assert outcome.detail["reason"] == "unknown_player_location"
    assert outcome.detail["gate"] == "player_where_unknown", outcome.detail
    # ⚠️ 措辞 2026-08-21 换成三扇门共用的那一份(`together.colocation_line`),
    # 从前这扇门自己写「不知道你**这会儿**在哪」,隔壁两扇写「不知道你在哪」——
    # 同一件事三种说法,而三处各自都读得通,所以谁也不会去比。
    assert "世界这会儿不知道你在哪" in outcome.text
    assert "player_move" not in outcome.text


def test_同地就放行(world):
    world.config_set("presence.enforce_colocation", True)
    _give_scarf(world)
    world.player_move("p1", "cafe")
    outcome = _direct(world, "give", object="红围巾")
    assert outcome.ok is True


def test_她在赶路时说的是等她落脚_不是你站错了(world):
    world.config_set("presence.enforce_colocation", True)
    _give_scarf(world)
    world.player_move("p1", "cafe")
    world.scheduler._transit["夏"] = {"from": "cafe", "to": "yard", "arrive_at": 999}
    outcome = _direct(world, "give", object="红围巾")
    assert outcome.ok is False and outcome.detail["reason"] == "agent_in_transit"


def test_act_的回执也要说得出是三种里的哪一种(world):
    """⚠️ 第二段那条断言 3.6.0 第五轮(2026-08-20)**换过一次**:它原先钉着
    「没调过 `player_move`」,把一句**过期的实况**焊成了这扇门的契约 —— 而这一支
    至少有三种来路(他下线了 / 在场记录过了 15 分钟 / 宿主确实没落过位置),
    前两种里宿主刚刚才调过。测试钉住一句假话,那句假话就再也改不动了。
    现在钉的是**这句话不许指认宿主**,加上它和邀请门那半边说的是同一句。

    ⚠️ **比整句,不比子串**(3.6.0 第六轮 2026-08-20 换的第二次)。上一版这里写的
    是 `"你在 yard" in result["error"]` —— 而 `yard` 是**裸 id**:玩家在这扇门上
    读到「你在 yard」,在隔壁那扇邀请门上读到的却是「你在后院」。只认子串的断言
    对两种写法**同时成立**,于是这条治过一次的病(CHANGELOG 3.6.0
    `### Fixed —— 她读到的地名一半是人话、一半是 id`)在这个函数里原样长回来时,
    测试一条都没红。

    ⚠️ **这四句里不再出现动词名**(3.6.0 第七轮 2026-08-20 换的第三次):从前是
    「「reach_out」要当面才办得到」—— 记号对了,框里那几个字母还是**函数名**,
    而读它的是刚刚才动手做过这件事的玩家。动词名没丢,它在 `result["tool"]` 里,
    下面第一段顺手钉住这一点:**去掉的只是玩家读到的那份,不是那个信息本身。**
    """
    world.config_set("presence.enforce_colocation", True)
    world.config_set("chat.tools.enabled", True)
    world.player_move("p1", "yard")
    result = world.act("夏", "reach_out", {"player_id": "p1"},
                       player_id="p1", surface="autonomy")
    assert result["ok"] is False
    assert result["error"] == (
        "这件事要当面才办得到 —— 你在后院,苏晚夏在咖啡店。"
        "隔着这么远,你只能跟她说话"
    ), result["error"]
    # 玩家那句话里不点动词名,而宿主排障要的那一格照旧在。
    assert result["tool"] == "reach_out", result
    assert "reach_out" not in result["error"], result["error"]

    world.players["p1"].pop("location")
    result = world.act("夏", "reach_out", {"player_id": "p1"},
                       player_id="p1", surface="autonomy")
    assert result["error"] == (
        "这件事要当面才办得到,而世界这会儿不知道你在哪 —— "
        "你可能已经离开这个世界了,也可能是这一程还没把落脚处告诉世界。苏晚夏在咖啡店"
    ), result["error"]
    assert "player_move" not in result["error"], result["error"]


def test_两扇门上同一件事必须是同一句话(world):
    """当不成面的每一种,在**两扇门**上都得是同一句话:他直接动手那扇
    (`World._colocation_error`)和他按「好」那扇(`World._invite_absence`)。

    守的不是措辞本身,是**"逐字同一句"由谁保证**。第五轮在 `_colocation_error`
    头上写下「和 `_invite_absence` 那一支逐字同一句」,而那时它俩已经不一样了
    (裸 id vs 人话地名,外加一个空格)—— **靠人手抄两遍来维持"逐字相同",正是
    那一轮塌掉的机制**。

    ⚠️ **这条测试 2026-08-20 只覆盖四支里的一支,而它的名字听上去覆盖全部** ——
    两个验收员各自独立把它读成了"两扇门整体统一了"。当时的实况写在这里当反面
    教材:她在途、他在别处时,邀请门说「她在路上,还没落脚」,动手那扇说
    「苏晚夏在咖啡店」——**那是她的出发地**,两个地名都对,合起来是一句谎。
    病根是**判据**不是措辞:那扇门从不问 `_transit`,只按 `here == where` 猜。

    ✅ **2026-08-21 连判据带句子一起收完了**(看板 D27/D24),所以这条测试**按
    整族铺开**:五个闸一个不落,少一个就是有人往族里加了闸没写用例。
    """
    from anima_world import together

    world.config_set("presence.enforce_colocation", True)
    world.config_set("chat.tools.enabled", True)

    def both_doors():
        """两扇门问同一个处境。回 (动手那扇的整句, 按「好」那扇的 (谁不在, 那句话))。"""
        result = world.act("夏", "reach_out", {"player_id": "p1"},
                           player_id="p1", surface="autonomy")
        return result["error"], world._invite_absence(
            {"agent_id": "夏", "loc": "cafe"}, "p1")

    seen: set[str] = set()

    def same_sentence(expect_gate, expect_absent):
        seen.add(expect_gate)
        assert world._tool_runtime._colocation_gate("夏", "p1") == expect_gate
        error, (absent, refusal) = both_doors()
        assert absent == expect_absent, (expect_gate, absent, refusal)
        assert error == "这件事要当面才办得到,而" + refusal, (error, refusal)
        assert "cafe" not in refusal and "yard" not in refusal, (
            "拼给玩家看的句子里出现地点变量,先问一句「过 place_name() 了吗」")
        return refusal

    # ① 他从没被 `player_move` 过 —— 世界不知道他在哪。
    line = same_sentence("player_where_unknown", "unknown")
    assert "咖啡店" in line, line

    # ② 她在途、**他在别处**(2026-08-20 那句谎恰好只在这个处境下说出口)。
    world.player_move("p1", "yard")
    world.scheduler._transit["夏"] = {"from": "cafe", "to": "yard", "arrive_at": 999}
    line = same_sentence("inviter_in_transit", "agent")
    assert "在路上" in line and "咖啡店" not in line, (
        "她的**出发地**不是她此刻在哪 —— 这正是 D27 那句谎", line)
    world.scheduler._transit.pop("夏", None)

    # ③ 世界不知道**她**在哪。
    blackboard = world.scheduler.agents["夏"].agent
    blackboard.blackboard.write("loc", "")
    blackboard.location = ""
    line = same_sentence("inviter_where_unknown", "unknown")
    assert "在路上" not in line, ("查不到 ≠ 她在赶路", line)
    blackboard.blackboard.write("loc", "cafe")
    blackboard.location = "cafe"

    # ④ **他自己在赶路**(D24:2026-08-21 之前这一种根本走不到这儿)。
    world.player_walk("p1", "cafe")
    assert world.player_in_transit("p1") is True
    line = same_sentence("player_in_transit", "player")
    assert "你这会儿在路上" in line, line

    assert seen == set(together.COLOCATION_GATES) - {"player_not_here"}, sorted(seen)


def test_四句话里从前没人守的那两句(world):
    """`_colocation_error` 会说**四**句话,而 2026-08-20 第七轮逐句反打补丁量出来:
    **只有两句塌了会红**。做法是把四句话一句一句改坏、每次只改一句,跑遍十个提到
    「当面 / colocation」的测试文件(基线 287 项):

    | 那一句 | 改坏之后 |
    |---|---|
    | 没说是替哪个玩家(`not player_id`) | **287 全绿** ← 没人守 |
    | 世界不知道你在哪(`not where`) | 2 failed |
    | 她这会儿在路上(`here == where`) | **287 全绿** ← 没人守 |
    | 你在 X、她在 Y(两地不同) | 1 failed |

    这一轮改的正好是这四句话的措辞(拿掉框里那个动词名),而其中两句**没有任何
    东西拦得住它们悄悄改回去**。所以这条把那两句补上,守的是同一件事:玩家读到
    的句子里不出现动词名,而动词名照旧在 `result["tool"]` 里交给宿主。

    ⚠️ **第四句的账 2026-08-21 结清了**:她在 `cafe → yard` 的路上、玩家站在 yard
    时,这扇门从前会报出她的**出发地**(「苏晚夏在咖啡店」)—— 一句假话,所以上一轮
    **有意不钉**(钉住一句假话,那句假话就再也改不动了)。现在判据换成了
    `_colocation_gate()`,句子换成了三扇门共用的那一份,那一支由本文件的
    `test_两扇门上同一件事必须是同一句话` 按整族铺开钉住。
    """
    world.config_set("presence.enforce_colocation", True)
    world.config_set("chat.tools.enabled", True)

    # ① 这次调用压根没说是替哪个玩家 —— 这句话是给宿主排障的,不是给玩家看的。
    result = world.act("夏", "reach_out", {"player_id": "p1"}, surface="autonomy")
    assert result["ok"] is False
    assert result["error"] == "这件事要当面才办得到,而这次调用没说是替哪个玩家", result
    assert "reach_out" not in result["error"], result["error"]

    # ③ 她在赶路。**判据是问出来的,不是"两处地名恰好相同"猜出来的** ——
    #   所以他站在她的出发地(下面第一段)还是站在别处(第二段),说的是同一句话。
    world.player_move("p1", "cafe")
    world.scheduler._transit["夏"] = {"from": "cafe", "to": "yard", "arrive_at": 999}
    result = world.act("夏", "reach_out", {"player_id": "p1"},
                       player_id="p1", surface="autonomy")
    assert result["error"] == (
        "这件事要当面才办得到,而苏晚夏这会儿在路上,还没落脚 —— 不是你不在"
    ), result
    world.player_move("p1", "yard")     # 他站到别处去 —— 从前这一步会把话变成谎
    assert world.act("夏", "reach_out", {"player_id": "p1"},
                     player_id="p1", surface="autonomy")["error"] == result["error"]
    assert result["tool"] == "reach_out", result
    assert "reach_out" not in result["error"], result["error"]


def test_一份打算里排不进要当面的动词(world):
    """一份打算是给未来几个 tick 的,那时候谁在她跟前没有人知道 —— 放进去的话,
    它会在某个说不清的时刻静默地做不成。"""
    world.config_set("presence.enforce_colocation", True)
    spec = tools_mod.get("walk")
    object.__setattr__(spec, "requires_colocation", True)
    try:
        with pytest.raises(ValueError, match="排不进"):
            world.intend("夏", [{"verb": "walk", "params": {"location": "yard"}}])
    finally:
        object.__setattr__(spec, "requires_colocation", False)


# ── ④ 开口让她做的事不受影响 ────────────────────────────────────────────────


@pytest.mark.parametrize("action,params", [
    ("sleep", {}),
    ("eat", {}),
    ("go", {"place": "yard"}),
    ("act", {"detail": "早点休息"}),
])
def test_隔着电话照样指挥得动她(world, action, params):
    """**判据是施动者是谁。** 「你去睡觉」是一句话,而一句话打电话也说得出来。
    把导演那几条一起挡掉,等于宣称"异地就不能跟她说话",而那正是这一层想保住的
    另一半(见 `chat_service.respond` 的两段身份声明)。"""
    world.config_set("presence.enforce_colocation", True)
    world.player_move("p1", "yard")
    outcome = _direct(world, action, **params)
    assert outcome.detail.get("reason") not in (
        "elsewhere", "unknown_player_location", "agent_in_transit",
    ), f"{action} 是玩家开口,不是玩家动手 —— 不该被同地闸挡"


def test_异地照样说得上话(world):
    """这一层保住的那一半:说话永远不挡。"""
    world.config_set("presence.enforce_colocation", True)
    world.player_move("p1", "yard")
    reply = world.chat_reply("夏", [{"role": "user", "content": "在吗"}], player_id="p1")
    assert isinstance(reply, str) and reply


# ── presence:那道闸的体检 ──────────────────────────────────────────────────


def test_presence_报得出谁没有位置(world):
    world.player_move("p1", "cafe")
    world.chat_state  # noqa: B018 - 只是确保子系统已经装好
    world._touch_player("p2", display_name="没位置的那个")
    report = world.presence()
    rows = {row["player_id"]: row for row in report["players"]}
    assert rows["p1"]["known"] is True and rows["p1"]["face_to_face"] == ["夏"]
    assert rows["p2"]["known"] is False, (
        "「不知道他在哪」和「他在一个没有角色的地方」是两件事"
    )
    assert report["unplaced"] == 1
    assert report["enforced"] is False


def test_presence_名单从落库那份补齐_而不是只看在场名册(world):
    """在场带 TTL,只回答"此刻谁在";而"这个世界跟谁打过交道"没有 TTL。
    只看在场那份的话,它会对着一个热闹的世界说"没人跟她说过话" —— 一句彻头彻尾的谎。"""
    world._note_player_contact("夏", "p9", "老熟人")
    report = world.presence()
    rows = {row["player_id"]: row for row in report["players"]}
    assert "p9" in rows and rows["p9"]["seen_before"] is True
    assert rows["p9"]["known"] is False, "落库的是名字与水位,不是位置"
    assert rows["p9"]["name"] == "老熟人"
    assert report["location_source"] == "redis", (
        "3.2.0 起在场真的落库了 —— 这一格从警告降成元数据,但不许删(镜像端在读它)"
    )


def test_presence_命令有位置就退_0_没位置就退_1(tmp_path):
    """**退出码是 1 所以它能进 CI** —— 和 `ontology --check`、`--ticks 0` 同一个用法。"""
    from _worldfile import open_world_at, run_cli

    w = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    try:
        w._note_player_contact("夏", "p2", "没位置的那个")
        w.scheduler.checkpoint()
    finally:
        w.close()

    done = run_cli("presence", "--world-id", "w")
    assert done.returncode == 1
    assert "player_move" in done.stdout
    assert "过期" in done.stdout, "不说清为什么会没位置,读的人会照着一个假警报去改宿主"
