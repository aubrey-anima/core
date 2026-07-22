"""anima_world.api.World —— 纯库门面的回归。

引擎没有 HTTP:任何用世界的模块 import 本包,用函数操作 world.db。
这里守住门面的核心动线:开世界 → 走时钟 → 读状态 → 聊天 → 记回合 →
玩家动作 → 改配置 → 关闭(存快照)→ 重开(恢复)。全程 Mock LLM,离线。
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


def test_close_saves_snapshot_and_world_reopens(tmp_path):
    db = str(tmp_path / "w.db")
    with World.open(db, force_mock_llm=True) as world:
        world.tick(3)
        tick = world.state()["world_time"]["tick"]

    with World.open(db, force_mock_llm=True) as reopened:
        state = reopened.state()
        assert state["runtime"]["snapshot"]["available"] is True
        assert state["world_time"]["tick"] == tick, "重开必须接着上次的时钟走"


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
