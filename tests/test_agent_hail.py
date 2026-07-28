"""角色会不会主动来找你 —— issue #13 的主体。

关系此前是**单向发起**的:玩家能对角色产生影响(记忆、关系、图谱边、八卦),角色却
不会决定"今天去找阿檀聊聊" —— 玩家不在 `scheduler.agents`、不在投影、不在 planner
的动作空间里。对一个开篇讲"会记住你的角色"的引擎来说,「会记住你」成立,「会来找你」
不成立。

按**访客模型**落地:玩家仍然不是居民(在场不落库、重启即新访),但**在场期间是可
寻址的**。挂在 `idle_social` 上而不是 planner 的动作空间上是有意的 —— 没有 key 就
没有 planner,而没有 key 是默认状态。

两条边界必须守住:
- **敲门不是对话**:不产生记忆、不动关系、不开会话。玩家还没回话,什么也没发生。
  否则你会看到"她来找过我",转头问她却毫无印象。
- **在场以 TTL 为准**:不去敲一个断线三小时的人的门,也不给一场没有人在的对话写
  事件。
"""
from __future__ import annotations

import pytest

from anima_world.api import World

A_DAY = 288


def _hail_until(world, player_id: str, *, max_days: int = 4) -> list[dict]:
    """跑到有人来搭话为止。轮询而不是写死 tick —— 世界逐次不确定。"""
    for _ in range(max_days):
        world.tick(A_DAY)
        found = world.inbox(player_id)
        if found:
            return found
    return world.inbox(player_id)


def _where_is(world, agent_id: str) -> str:
    brain = world.scheduler.agents[agent_id]
    return brain.agent.blackboard.read("loc") or brain.agent.location


def test_a_character_comes_looking_for_a_player_who_is_actually_there(tmp_path):
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.tick(1)
        world.player_move("p1", _where_is(world, "柔"))

        hails = _hail_until(world, "p1")
        assert hails, "在场的玩家跟角色待在同一个地方好几天,一次都没被搭话"
        payload = hails[0]["payload"]
        assert payload["player_id"] == "p1"
        assert payload["agent_id"] in world.scheduler.agents


def test_knocking_is_not_a_conversation(tmp_path):
    """敲门不产生记忆、不动关系、不开会话 —— 玩家还没回话。"""
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.tick(1)
        world.player_move("p1", _where_is(world, "柔"))
        assert _hail_until(world, "p1"), "前提:得先真的被搭话"

        conn = world.scheduler.event_log.conn
        conversations = conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'conversation'"
        ).fetchone()[0]
        assert conversations == 0, "敲门不该写出一场没有人在的对话"

        relations = {
            pair for pair, rel in world.scheduler._memory_projection.relations.items()
            if "p1" in pair and abs(rel.sentiment) > 1e-9
        }
        assert not relations, f"敲门不该动关系:{relations}"

        memories = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE summary LIKE '%p1%'"
        ).fetchone()[0]
        assert memories == 0, "敲门不该在角色心里留下一条记忆"


def test_nobody_knocks_on_a_player_who_left(tmp_path):
    """离场之后不该再被搭话 —— 否则就是给不在的人写事件。"""
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.tick(1)
        world.player_move("p1", _where_is(world, "柔"))
        world.player_leave("p1")

        world.tick(A_DAY * 2)
        assert world.inbox("p1") == []


def test_nobody_knocks_on_a_player_who_went_quiet(tmp_path):
    """TTL 过期等同于离场 —— 幽灵访客不该收到敲门。"""
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.tick(1)
        world.player_move("p1", _where_is(world, "柔"))
        world.players["p1"]["last_seen"] -= world.player_ttl_seconds + 1

        world.tick(A_DAY * 2)
        assert world.inbox("p1") == []


def test_a_player_somewhere_else_is_not_hailed(tmp_path):
    """搭话要同地 —— 不然就成了隔空喊话。"""
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.tick(1)
        elsewhere = next(
            loc for loc in ("home", "cafe", "workshop")
            if loc != _where_is(world, "柔")
        )
        world.player_move("p2", elsewhere)
        world.player_move("p1", _where_is(world, "柔"))

        assert _hail_until(world, "p1"), "前提:同地那个得先收到"
        assert world.inbox("p2") == []


def test_the_inbox_supports_incremental_reads(tmp_path):
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.tick(1)
        world.player_move("p1", _where_is(world, "柔"))
        first = _hail_until(world, "p1")
        assert first

        cursor = first[-1]["seq"]
        assert world.inbox("p1", since_seq=cursor) == []
