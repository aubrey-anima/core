"""anima_world.api.World —— 纯库门面的回归。

引擎没有 HTTP:任何用世界的模块 import 本包,用函数操作 world.db。
这里守住门面的核心动线:开世界 → 走时钟 → 读状态 → 聊天 → 记回合 →
玩家动作 → 改配置 → 关闭 → 重开(从事件日志重放恢复)。全程 Mock LLM,离线。
"""
from __future__ import annotations

import pytest

from anima_world.api import World


@pytest.fixture
def world(tmp_path):
    w = World.open(str(tmp_path / "w.db"), force_mock_llm=True)
    yield w
    w.close()


def test_open_tick_state_roundtrip(world):
    assert len(world.scheduler.agents) == 3  # 内置种子:夏、遥、柔
    before = world.state()["world_time"]["tick"]
    world.tick(5)
    state = world.state()
    assert state["world_time"]["tick"] == before + 5
    assert set(state["agents"]) == set(world.scheduler.agents)
    assert state["locations"], "地图必须随 state 输出"
    assert state["runtime"]["llm"]["mock"] is True


def test_chat_streams_and_records_nothing(world):
    reply = world.chat_reply(
        "夏",
        [{"role": "user", "content": "你好"}],
        player_id="p1",
        display_name="阿宇",
    )
    assert reply  # Mock LLM 也要有回复
    # respond 路径不落平台历史:世界库里不该出现会话
    assert world.conversations("夏") == []


def test_chat_rejects_unknown_agent_and_bad_tail(world):
    with pytest.raises(KeyError):
        world.chat_reply("不存在", [{"role": "user", "content": "hi"}], player_id="p1")
    with pytest.raises(ValueError):
        world.chat_reply("夏", [{"role": "assistant", "content": "hi"}], player_id="p1")


def test_record_chat_turn_closes_and_emits_one_event(world):
    conversation_id = world.record_chat_turn(
        "夏",
        "p1",
        [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ],
    )
    convs = world.conversations("夏")
    assert [c["id"] for c in convs] == [conversation_id]
    assert convs[0]["status"] == "closed"
    assert len(world.conversation_messages(conversation_id)) == 2
    conversation_events = [
        ev for ev in world.events() if ev.get("type") == "conversation"
    ]
    assert len(conversation_events) == 1, "整场会话只在关闭时发一个事件"

    with pytest.raises(ValueError):
        world.record_chat_turn("夏", "p1", [{"role": "user", "content": "只有一半"}])


def test_player_move_and_action(world):
    world.player_move("p1", "cafe")
    assert world.state()["players"]["p1"]["location"] == "cafe"
    with pytest.raises(KeyError):
        world.player_move("p1", "不存在的地方")
    with pytest.raises(KeyError):
        world.player_move("p1", "oldport")  # region 不是可站立的 point

    world.player_action("p1", "挥手", {"target": "夏"})
    actions = [ev for ev in world.events() if ev.get("type") == "player_action"]
    assert actions and actions[-1]["player_id"] == "p1"
    assert actions[-1]["loc"] == "cafe"


def test_config_set_coerces_and_validates(world):
    world.config_set("scheduler.tick_rate", "2.5")
    assert world.config_get("scheduler.tick_rate") == 2.5
    with pytest.raises(KeyError):
        world.config_set("no.such.key", "1")
    with pytest.raises(ValueError):
        world.config_set("scheduler.tick_rate", "0")
    masked = {row["key"]: row["value"] for row in world.config_list()}
    assert "llm.api_key" in masked


def test_world_reopens_from_the_event_log(tmp_path):
    """时钟恢复语义:回到事件日志里最后一个世界时间戳。没有事件的静默 tick
    不是历史(事件溯源的本义),所以用一个确定性事件钉住最后时刻 ——
    依赖异步叙事事件恰好落盘的写法会 flaky。"""
    db = str(tmp_path / "w.db")
    with World.open(db, force_mock_llm=True) as world:
        world.tick(3)
        world.player_action("p1", "留下到此一游")  # ts = 当前时钟,同步落盘
        tick = world.state()["world_time"]["tick"]
        assert tick == 3

    with World.open(db, force_mock_llm=True) as reopened:
        state = reopened.state()
        assert state["world_time"]["tick"] == tick, "重开必须接着最后一个事件的时钟走"


def test_subscribe_receives_events(world):
    q = world.subscribe()
    try:
        world.player_action("p1", "跺脚")
        batch = q.get(timeout=2)
        assert any(ev.get("type") == "player_action" for ev in batch["events"])
    finally:
        world.unsubscribe(q)


def test_agent_context_is_bounded_grounding(world):
    ctx = world.agent_context("夏", "p1")
    assert "presence" in ctx
    assert ctx["presence"]["location_id"]


def test_world_has_exactly_one_projection(tmp_path):
    """回归:曾经有两份投影,第二份在运行中会停在开机状态。

    `_WorldView` 自己维护一份,开机全量重放建起来,此后只同步叙事日志和角色
    位置 —— 经济与关系变化一概不折叠。而 `scheduler._memory_projection` 每条
    事件都折叠。于是从 view 那份读余额会读到开机时的旧值(已删除的 snapshots
    表正是把它写回库里,才留下会累积的错账)。
    """
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.config_set("economy.enabled", "true")
        assert world._view.projection is world.scheduler._memory_projection, (
            "系统里只允许有一份投影"
        )
        world.tick(300)  # 跨过日切,工资会入账
        agent_id = next(iter(world.scheduler.agents))
        assert world._view.projection.balances[agent_id] == world.balance(agent_id), (
            "view 读到的余额必须与对外 API 一致 —— 不许停在开机状态"
        )
        assert world.balance("__town__") < 0, "金库发了工资就该是负的"


def test_state_reports_live_locations(tmp_path):
    """位置改成快照时读活黑板后,state() 必须仍与角色实际所在一致。"""
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.tick(50)
        live = {
            aid: (brain.agent.blackboard.read("loc") or brain.agent.location)
            for aid, brain in world.scheduler.agents.items()
        }
        reported = {aid: d["location"] for aid, d in world.state()["agents"].items()}
        assert {k: v for k, v in reported.items() if k in live} == live
