"""事件日志是唯一真相 —— 那它就得真的重建得出世界。

这个仓库的架构承诺只有一句:历史进事件日志,状态是它的投影。投影错了不会报错,
只会给出一个**看起来完全正常的错答案** —— 世界照跑,而"谁在哪"取决于你有没有
重启过。这里守两个漏斗:

1. **位置**。角色走路发的是 `state_change:location_join`,而投影拿这条事件去
   *注册地点*,从不写 `agents[who].location`。于是运行中活黑板说柔在咖啡店、
   投影说她在家;重开一次,开机 roster 读投影(`__main__.py:334` 本来就读对了),
   于是所有人被传送回出生地。
2. **玩家动作**。`player_action` 的四个字段全在事件顶层,而 `_event_log_payload`
   只搬 `payload` —— 落库恒为 `{}`。玩家在世界里做过什么,重放之后一个字不剩。

两条都不写死 tick 数:世界逐次不确定,固定 288 会假绿。位置那条**轮询到有人真
的动了**为止,动不了就当场失败(那本身就是信号)。
"""
from __future__ import annotations

from _worldfile import open_world_at

import json

import pytest

from anima_world.api import World


def _live(world: World) -> dict[str, str]:
    """活黑板说的"谁在哪"。在途时 `loc` 仍是出发地,所以它变了就等于落地了。"""
    return {
        aid: brain.agent.blackboard.read("loc") or brain.agent.location or ""
        for aid, brain in world.scheduler.agents.items()
    }


def _projected(world: World) -> dict[str, str]:
    return {
        aid: state.location or ""
        for aid, state in world.scheduler._memory_projection.agents.items()
    }


def _tick_until_someone_arrives_elsewhere(world: World, *, max_ticks: int = 4000):
    """跑到有人真的走到别处为止,返回 (谁, 出生地, 现在在哪)。

    轮询而不是跑固定 tick:作息与行为树逐次不同,写死 tick 数迟早假绿。
    """
    spawn = _live(world)
    ticked = 0
    while ticked < max_ticks:
        world.tick(10)
        ticked += 10
        now = _live(world)
        for aid, where in now.items():
            if where and where != spawn.get(aid):
                return aid, spawn[aid], where
    raise AssertionError(
        f"{max_ticks} tick 内没有任何角色离开出生地 —— 这条测试失去了意义,先查世界为什么不动"
    )


def test_the_projection_agrees_with_the_blackboard_while_the_world_runs(tmp_path):
    """运行中活黑板和投影必须对"谁在哪"给同一个答案。

    这条盯的是 `_record_event` 的折叠顺序:`_stream_event` 会把
    `state_change:location_join` 改写成 `agent_action{action:"walk"}`,若折叠发生
    在改写之后,折进投影的根本不是那条位移事件 —— 于是即使 projection.py 改对了,
    运行中的投影照样冻结,而重开后又是对的。「答案取决于你有没有重启过」比统一
    错更难查。
    """
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        who, spawn, arrived = _tick_until_someone_arrives_elsewhere(world)
        assert _projected(world)[who] == arrived, (
            f"{who} 从 {spawn} 走到了 {arrived},投影却仍说 {_projected(world)[who]}"
        )


def test_where_everyone_is_survives_a_restart(tmp_path):
    """重开世界,人不该被传送回出生地。"""
    db = str(tmp_path / "w.db")
    with open_world_at(db, force_mock_llm=True) as world:
        who, spawn, arrived = _tick_until_someone_arrives_elsewhere(world)
        assert arrived != spawn

    with open_world_at(db, force_mock_llm=True) as reopened:
        assert _live(reopened)[who] == arrived, f"{who} 重开后被传送回了 {_live(reopened)[who]}"
        presence = reopened.world_context(who, "p1").get("presence", {})
        assert presence.get("location_id") == arrived, "world_context 必须说同一个地方"


def _player_action_rows(world: World) -> list[tuple[str, str]]:
    return [
        (e.who, json.dumps(e.payload, ensure_ascii=False))
        for e in world.scheduler.event_log.replay()
        if e.type == "player_action"
    ]


def test_a_player_action_reaches_the_event_log_with_its_content(tmp_path):
    """玩家做了什么,得真的落在库里 —— 不是一个空对象。

    `role` 这一格从前恒等于字面量 `"player"`(那是形参的默认值),于是它验的其实
    只是"这个键在"。而事件是**不可改的历史**:记一个从来没成立过的身份进去,
    以后谁都分不清"他当时的身份是 player"和"这一路压根没拿到身份"——
    所以这里两种都验,而且给一个真身份,让它验的是一个有意义的值。
    """
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.player_move("p1", "cafe", role="旅人")
        world.player_action("p1", "挥手", {"target": "夏"})
        # 宿主这一路没给身份,但世界记着 —— 落库的该是世界记着的那个
        world.player_action("p1", "点头", {})
        # 从没报过身份的人:那一格是空的,不是一个假身份
        world.player_action("p2", "张望", {})

        rows = _player_action_rows(world)
        assert len(rows) == 3
        payload = json.loads(rows[0][1])
        assert payload.get("action") == "挥手", f"落库的 payload 是 {payload!r}"
        assert payload.get("details") == {"target": "夏"}
        assert payload.get("player_id") == "p1"
        assert payload.get("role") == "旅人"
        assert json.loads(rows[1][1]).get("role") == "旅人"
        assert json.loads(rows[2][1]).get("role") == ""


def test_a_replayed_player_action_keeps_the_shape_the_live_stream_had(tmp_path):
    """重放出来的形状必须和实时流一致 —— 否则宿主按文档读字段会在老历史上炸。

    四个键全要:只回填 `action` 而漏掉 `player_id`/`role`/`details`,就是把同一个
    bug 换个地方犯,而现有断言照绿。
    """
    db = str(tmp_path / "w.db")
    with open_world_at(db, force_mock_llm=True) as world:
        world.player_move("p1", "cafe", role="旅人")
        world.player_action("p1", "挥手", {"target": "夏"})
        live = [e for e in world.events() if e.get("type") == "player_action"][-1]
        assert live["player_id"] == "p1"
        assert live["role"] == "旅人"
        assert live["action"] == "挥手"
        assert live["details"] == {"target": "夏"}

    with open_world_at(db, force_mock_llm=True) as reopened:
        replayed = [e for e in reopened.events() if e.get("type") == "player_action"]
        assert replayed, "重开后 player_action 事件不见了"
        for key in ("player_id", "role", "action", "details"):
            assert replayed[-1].get(key) == live[key], (
                f"重放后 {key} 是 {replayed[-1].get(key)!r},实时流里是 {live[key]!r}"
            )


def test_投影不许就地改写一条已经发生过的事件(tmp_path):
    """**"那条事件说了什么"只能有一个答案。**

    `_apply_agent_join` 曾经把事件 payload 里那个 `spec` 直接拿去当投影的状态
    (同一个 dict,没拷贝)。于是后来的一条 `persona_update` 顺着
    `agent.spec.update(...)` **就地改写了那条创世 `agent_join` 在内存里的样子** ——
    `World.events()`(内存窗口)看到的 payload 里多出了 goals,而 `events export`
    与任何一次重放都没有。日志才是对的,内存那份是被改过的。

    没造成过数据损失(goals 自己有一条 persona_update 事件),但它是个地雷:
    投影往后加的任何一处写,都会静默地改写"历史显示成什么样"。
    """
    import fakeredis

    from anima_world.api import World

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    with World.open("fidelity", redis=client, force_mock_llm=True) as world:
        shown = [e for e in world.events()
                 if e["type"] == "agent_join" and e["who"] == "夏"][0]
        logged = [
            json.loads(raw) for raw in client.lrange("anima:fidelity:events", 0, -1)
        ]
        written = [e for e in logged
                   if e.get("type") == "agent_join" and e.get("who") == "夏"][0]

    assert shown["payload"] == written["payload"], (
        "内存窗口里的事件和日志里的对不上 —— 有人在事后改写了已经发生过的事"
    )


def test_她在哪只能有一个答案(tmp_path):
    """`World.state()` 交给宿主的一个人身上,不该有两个"她在哪"。

    `state` 装的是她**在干什么**,位置的家是 `agent.location`。而 `work` 曾经顺手
    往 `state` 里再写一份 —— `sleep` 不清它,`walk` 也不清它,于是那份拷贝只增不减,
    停在她上一次干活的地方。线上量出来是 21 个人里 13 个对不上,而宿主读哪一个
    全凭它先看见谁。这正是这个仓库最怕的坏法:世界照跑,答案是错的,没有任何报错。

    两半都要钉,**而且要分开钉**(投影那道闸会把发事件那一侧的错盖住):
    1. **发出去的那条事件**里就不该有位置 —— 它进日志、进导出、进 `.cyberworld`,
       而行为树的 `do_work` 把它写死成 `workshop`,她可能正在码头;
    2. **历史里那些老事件重放回来**也不该把它带回来 —— 线上世界的投影是重放
       MySQL 折出来的,只修发事件那一侧,已经写下的每一条 `work` 都会原样复活它。
    """
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        who = sorted(world.scheduler.agents)[0]
        assert world.act(who, "work", {}, surface="body")["ok"] is True
        world.tick(1)

        worked = [
            e for e in world.scheduler.event_log.replay()
            if e.who == who and (e.payload.get("state") or {}).get("status") == "working"
        ]
        assert worked, "前提没成立:她根本没干成活"
        for e in worked:
            assert "location" not in e.payload["state"], (
                f"落进日志的那条 work 自带了一个位置:{e.payload['state']!r}"
            )

        # 老历史长这样(线上库里躺着的就是它):干活的同时报了个位置
        world._record_and_fan({
            "type": "state_change",
            "who": who,
            "payload": {
                "kind": "agent_state",
                "state": {"status": "working", "location": "上一次干活的地方"},
            },
        })
        world.tick(1)

        shown = world.state()["agents"][who]
        assert shown["state"].get("status") == "working", (
            f"前提没成立:她根本没在干活({shown['state']!r})"
        )
        assert "location" not in shown["state"], (
            f"{who} 身上有两个位置:location={shown['location']!r},"
            f"state.location={shown['state'].get('location')!r}"
        )
