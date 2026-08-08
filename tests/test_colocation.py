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
    """**这一条是那道闸的全部代价所在。** 线上根本没人调 `player_move`,于是"异地"
    是每一次调用的默认值 —— 默认收紧就是当场打断线上世界。"""
    _give_scarf(world)
    world.player_move("p1", "yard")     # 她在 cafe
    outcome = _direct(world, "give", object="红围巾")
    assert outcome.ok is True, "开关关着时行为必须和 2.2.0 逐位相同"


def test_关着的时候连位置都不查(world):
    """宿主从没调过 `player_move` 的世界(也就是线上那个)照旧能给东西。"""
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
    assert "不知道你这会儿在哪" in outcome.text


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
    world.config_set("presence.enforce_colocation", True)
    world.config_set("chat.tools.enabled", True)
    world.player_move("p1", "yard")
    result = world.act("夏", "reach_out", {"player_id": "p1"},
                       player_id="p1", surface="autonomy")
    assert result["ok"] is False and "你在 yard" in result["error"]

    world.players["p1"].pop("location")
    result = world.act("夏", "reach_out", {"player_id": "p1"},
                       player_id="p1", surface="autonomy")
    assert "没调过 player_move" in result["error"]


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


def test_presence_名单从落库那份补齐_而不是只看这个进程(world):
    """**玩家的位置是进程内的**(`World.players` 是刻意的内存态),而这条命令永远
    是另开一个进程问的。只看内存那份的话,它会对着一个热闹的世界说"没人跟她说过话"
    —— 而那是一句彻头彻尾的谎。"""
    world._note_player_contact("夏", "p9", "老熟人")
    report = world.presence()
    rows = {row["player_id"]: row for row in report["players"]}
    assert "p9" in rows and rows["p9"]["seen_before"] is True
    assert rows["p9"]["known"] is False, "落库的是名字与水位,不是位置"
    assert rows["p9"]["name"] == "老熟人"
    assert report["location_source"] == "process-memory", (
        "那道闸依赖的东西活不过一次重启、也跨不过第二个进程 —— 这一格是警告,不是元数据"
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
    assert "进程内存" in done.stdout, "不说这一句的话,读的人会照着一个假警报去改宿主"
