"""在场是世界的状态,不是进程的状态。

**这是一次真人试玩逼出来的。** 两个容器在开局前 56 分钟重启过,于是 `World.players`
(当时是一个进程内的普通 dict)是空的;`player_location()` 返回空串;每一场对话都
静默退回"手机私聊"的措辞——「她看了一眼手机屏幕」——而人明明就坐在她对面。
没有异常、没有告警、日志干干净净。这是这个仓库最怕的那一类:照跑,但给错东西。

按分家判据它本来就该在 Redis:并发在场的玩家是**有界**的,而 `_present_roster`
**直接进她的决定上下文**。两条都指向 Redis。

顺带钉死另外三条容易一起走丢的:
- 在路上的人重启后仍然按原定 tick 到达(行程也是世界的状态);
- 过期只有一条规则(Redis 自己的 TTL),不是"键还在 + last_seen 也够新"两条;
- 一个进程启动时不许把另一个进程刚写下的在场状态盖回创世值(只填缺不覆盖)。
"""
from __future__ import annotations

from _worldfile import open_world_at, redis_for

import pytest

from anima_world import redis_state


def test_玩家重启之后还站在原地(tmp_path):
    db = str(tmp_path / "w.db")
    with open_world_at(db, force_mock_llm=True) as world:
        world.player_move("p1", "cafe")
        list(world.chat("夏", [{"role": "user", "content": "在吗"}],
                        player_id="p1", display_name="阿檀"))
        assert world.player_location("p1") == "cafe"

    with open_world_at(db, force_mock_llm=True) as reopened:
        assert reopened.who_is_present() == ["p1"], "重启把在场的人清空了"
        assert reopened.player_location("p1") == "cafe", \
            "他还坐在那儿,而世界说不知道他在哪 —— 下一句就退回手机私聊"
        assert (reopened.players.get("p1") or {}).get("display_name") == "阿檀"


def test_在路上的人重启之后仍然按原定时间到达(tmp_path):
    db = str(tmp_path / "w.db")
    with open_world_at(db, force_mock_llm=True) as world:
        world.player_move("p1", "cafe")
        trip = world.player_walk("p1", "workshop")
        assert trip["in_transit"], "这两点之间没有路,换一对地点再写这条"
        arrive_at = int(trip["arrive_at"])
        assert world.player_in_transit("p1")

    with open_world_at(db, force_mock_llm=True) as reopened:
        assert reopened.player_in_transit("p1"), "重启把他这段路抹掉了"
        assert reopened.player_location("p1") == "cafe", "在路上就还算在出发地"
        reopened.tick(max(1, arrive_at - int(reopened.scheduler.clock)))
        assert reopened.player_location("p1") == "workshop", "他再也到不了了"
        assert not reopened.player_in_transit("p1")


def test_过期只有一条规则_键还在不在(tmp_path):
    """从前 `_present_roster` 自己按 `last_seen` 过滤,而行本身没有过期时间。
    两套规则迟早给出不同答案,而两边都不报错。"""
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    try:
        world.player_move("p1", "cafe")
        assert world.who_is_present() == ["p1"]

        # 演一遍 TTL 走到头:Redis 把行删掉,索引里那个名字还在
        world.presence_store.expire_now("p1")
        assert world.who_is_present() == [], "过期的人还在名册上"
        assert world._present_roster() == {}
        assert world.player_location("p1") == ""

        # `last_seen` 只是给人看的,不许有任何判断读它
        world.player_move("p1", "cafe")
        world.presence_store.update("p1", {"last_seen": 0.0})
        assert world.who_is_present() == ["p1"], \
            "还有第二套过期规则在按 last_seen 判"
    finally:
        world.close()


def test_另一个进程写下的在场状态不会被启动盖掉(tmp_path):
    """多个进程可以操作同一个世界。第二个 `World.open` 不许把他挪回创世值。"""
    db = str(tmp_path / "w.db")
    with open_world_at(db, force_mock_llm=True) as first:
        first.player_move("p1", "workshop")
        with open_world_at(db, force_mock_llm=True) as second:
            assert second.player_location("p1") == "workshop"
            second.player_move("p1", "cafe")
        assert first.player_location("p1") == "cafe", \
            "两个进程各记各的在场 —— 世界里就有了两个他"


def test_在场不进导出(tmp_path):
    """`.cyberworld` 是**分发物**。导出一个世界发给别人,不该带着别人的玩家在哪儿;
    而且 TTL 落不进 JSON —— 装回去就是一份永不过期的假在场(和 `:lock` 同一条理由)。
    """
    from anima_world.world_package import dump_world_records

    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    try:
        world.player_move("p1", "cafe")
        keys = [
            r["key"] for r in dump_world_records(redis=redis_for(str(tmp_path / "w.db")), world_id="world")
            if r.get("kind") == "redis"
        ]
    finally:
        world.close()
    assert not any(":player" in k or k.endswith(":players") for k in keys), \
        f"在场表跟着世界被打包发出去了:{keys}"


def test_键名是契约(tmp_path):
    assert redis_state.players_index_key("w") == "anima:w:players"
    assert redis_state.player_key("w", "p1") == "anima:w:player:p1"
