"""玩家在世界里活多久 —— issue #13 的地基。

**访客模型**:玩家不是居民。世界侧留下的是他造成的**后果** —— 记忆、关系、图谱边、
账本。(⚠️ 这段原本写的是"他的位置与在场只活在宿主进程里(不落库)"——**3.2.0 推翻了
那一半**:在场落 Redis 带 TTL,见 `test_presence_survives_restart.py`。)这是自洽的,
但当初缺了一半:
`world.players` **只有写、没有删**。`player_move` 是唯一入口,而 CLI 每聊一轮都调
一次;一个长跑的宿主里,咖啡店会攒下一屋子早就下线的幽灵访客。

今天这还无所谓 —— 玩家对角色不可见,没人读那份名单。**一旦让角色看得见在场的玩家
(#13 的主体),它就变成可见的错**:NPC 会走去找一个断线三小时的人,并把一场没有
人在的对话写进事件日志。照跑,但给错东西。

所以离场语义是前置条件,不是配套改进:先有"他走了",才谈得上"她去找他"。
"""
from __future__ import annotations

from _worldfile import open_world_at

import pytest

from anima_world.api import World


@pytest.fixture
def world(tmp_path):
    w = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
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

    # 让 TTL 走到头(3.2.0:过期由 Redis 自己做,不再是"把 last_seen 推回去")
    world.presence_store.expire_now("p1")
    assert world.who_is_present() == []


def test_any_interaction_counts_as_a_heartbeat(world):
    """说话、走动、动作都算"我还在" —— 不给宿主强加一个额外的心跳契约。"""
    world.player_move("p1", "cafe")
    world.presence_store.expire_now("p1")
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
    conversations = sum(
        1 for e in world.scheduler.event_log.replay() if e.type == "conversation"
    )
    assert conversations == 1, "人走了,他造成的历史必须还在"


def test_同处一室的别的玩家她也看得见(world):
    """在场块的 `others` 从前只从 `scheduler.agents` 里拼 —— 一个三个人的房间在她
    眼里只有一个人。她会当着另外那位的面说只该对你说的话,而世界里明明写着他在场。"""
    world.player_move("p1", "cafe")
    world.player_move("p2", "cafe")
    world.players["p2"]["display_name"] = "阿檀"
    others = world.agent_context("夏", "p1")["presence"]["others"]
    assert "阿檀" in others


def test_正在跟她说话的那位不进在场名单(world):
    """身份块已经单独讲过他(「X 和你都在……,因此这是面对面交谈」)。再进 `others`
    就会被补上一句"他只是同场角色,不是正在和你说话的人" —— 同一段提示词自相矛盾。"""
    world.player_move("p1", "cafe")
    world.players["p1"]["display_name"] = "阿檀"
    others = world.agent_context("夏", "p1")["presence"]["others"]
    assert "阿檀" not in others


def test_在别处的玩家不算在场(world):
    world.player_move("p1", "cafe")
    world.player_move("p2", "workshop")
    world.players["p2"]["display_name"] = "阿檀"
    others = world.agent_context("夏", "p1")["presence"]["others"]
    assert "阿檀" not in others


def test_没报过名字的玩家用称呼进提示词_不是他的_id(world):
    """她读到什么就说什么(`_place_name` 那一课)—— 把 uuid 放进提示词就是让她
    把 uuid 念出口。"""
    world.player_move("p1", "cafe")
    world.player_move("11111111-2222-3333-4444-555555555555", "cafe")
    others = world.agent_context("夏", "p1")["presence"]["others"]
    assert "11111111" not in others and "访客" in others


def test_在路上的人_快照里不能报成站在出发地(world):
    """快照那扇门是宿主唯一读的那扇 —— 它说他站在哪,界面就把他画在哪。

    角色那一半早就有这一格(`_agent_activity` 的 `activity.transit`),那儿的注释
    写着理由:「少了这一格,一个埋头做了十个月椅子的人在 `state()` 里看上去和闲着
    一模一样,而宿主的界面只认这里」。玩家这一半漏了,于是线上真的这样:他点了
    「走去澡堂」,屏幕上他还站在铁匠巷,而同一秒 `player_options` 正拿一句「你在
    路上 —— 到了地方就能动手了」把他能干的事全挡了。同一块屏幕的两半互相打脸,
    而位置那一半是**假的** —— `_player_here` 说他这会儿不在任何地方。
    """
    world.player_move("p1", "cafe")
    trip = world.player_walk("p1", "workshop")
    assert trip["in_transit"] is True, "前提没成立:这两点之间没有路费"

    row = world.state()["players"]["p1"]
    assert row.get("in_transit") is True, (
        "快照把一个在赶路的人报成站在出发地 —— 而拒绝那句话说的正是这个差别"
    )
    assert row["location"] == "cafe", (
        "位置不该抹成空串:调用方要分得开「他在路上」和「世界不知道他在哪」"
    )
    transit = row.get("transit") or {}
    assert transit.get("to") == "workshop", (
        "只给一个布尔的话,界面说得出「在路上」却说不出去哪、还有多久 —— "
        "而时间这一种代价只有看得见才咬得住人"
    )
    assert int(transit.get("eta_minutes") or 0) > 0


def test_presence_survives_a_restart(tmp_path):
    """**3.2.0 反过来了。** 这条从前断言"重启即新访",理由写的是"在场是会话状态"。

    一次真人试玩证伪了它:容器重启之后世界不知道人就坐在她对面,于是每一句都退回
    手机私聊的措辞,而且一个字的告警都没有。在场是**世界**的状态;过期归 TTL 管
    (上面那两条),不归"进程活多久"管。细节见 `test_presence_survives_restart.py`。
    """
    db = str(tmp_path / "w.db")
    with open_world_at(db, force_mock_llm=True) as world:
        world.player_move("p1", "cafe")
        assert world.who_is_present() == ["p1"]

    with open_world_at(db, force_mock_llm=True) as reopened:
        assert reopened.who_is_present() == ["p1"]
        assert reopened.player_location("p1") == "cafe"
