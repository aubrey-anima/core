"""玩家在世界里活多久 —— issue #13 的地基。

**访客模型**:玩家不是居民。他的位置与在场只活在宿主进程里(不落库),世界侧只留下
他造成的**后果** —— 记忆、关系、图谱边、账本。这是自洽的,但今天缺了一半:
`world.players` **只有写、没有删**。`player_move` 是唯一入口,而 CLI 每聊一轮都调
一次;一个长跑的宿主里,咖啡店会攒下一屋子早就下线的幽灵访客。

今天这还无所谓 —— 玩家对角色不可见,没人读那份名单。**一旦让角色看得见在场的玩家
(#13 的主体),它就变成可见的错**:NPC 会走去找一个断线三小时的人,并把一场没有
人在的对话写进事件日志。照跑,但给错东西。

所以离场语义是前置条件,不是配套改进:先有"他走了",才谈得上"她去找他"。
"""
from __future__ import annotations

import pytest

from anima_world.api import World


@pytest.fixture
def world(tmp_path):
    w = World.open(str(tmp_path / "w.db"), force_mock_llm=True)
    yield w
    w.close()


def test_a_player_can_leave(world):
    world.player_move("p1", "cafe")
    assert world.who_is_present() == ["p1"]

    world.player_leave("p1")
    assert world.who_is_present() == []


def test_leaving_twice_is_not_an_error(world):
    """宿主的断线回调可能重入 —— 幂等,别让它抛。"""
    world.player_move("p1", "cafe")
    world.player_leave("p1")
    world.player_leave("p1")
    assert world.who_is_present() == []


def test_a_player_who_stops_touching_the_world_expires(world):
    """没有心跳的宿主不该留下永久幽灵:超过 TTL 就当他走了。"""
    world.player_move("p1", "cafe")
    world.tick(1)

    # 把最后活跃时间推回去,等价于"很久没动静了"
    world.players["p1"]["last_seen"] -= world.player_ttl_seconds + 1
    assert world.who_is_present() == []


def test_any_interaction_counts_as_a_heartbeat(world):
    """说话、走动、动作都算"我还在" —— 不给宿主强加一个额外的心跳契约。"""
    world.player_move("p1", "cafe")
    world.players["p1"]["last_seen"] -= world.player_ttl_seconds + 1
    assert world.who_is_present() == []

    world.chat_reply("夏", [{"role": "user", "content": "我回来了"}],
                     player_id="p1", display_name="阿檀")
    assert world.who_is_present() == ["p1"], "聊一句就该重新算在场"


def test_what_a_visitor_leaves_behind_outlives_the_visit(world):
    """访客模型的另一半:他走了,但他造成的后果留在世界里。"""
    world.player_move("p1", "cafe")
    reply = world.chat_reply("夏", [{"role": "user", "content": "你好"}],
                             player_id="p1", display_name="阿檀")
    world.record_chat_turn("夏", "p1", [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": reply},
    ])
    world.player_leave("p1")

    assert world.who_is_present() == []
    conversations = world.scheduler.event_log.conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = 'conversation'"
    ).fetchone()[0]
    assert conversations == 1, "人走了,他造成的历史必须还在"


def test_presence_does_not_survive_a_restart(tmp_path):
    """在场是会话状态,刻意不落库 —— 重启即新访,这是访客模型的定义。"""
    db = str(tmp_path / "w.db")
    with World.open(db, force_mock_llm=True) as world:
        world.player_move("p1", "cafe")
        assert world.who_is_present() == ["p1"]

    with World.open(db, force_mock_llm=True) as reopened:
        assert reopened.who_is_present() == []
