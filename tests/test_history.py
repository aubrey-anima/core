"""全量事件历史的门。

`World.events()` 背后是 `deque(maxlen=200)` —— 一个内存窗口,不是历史。它的
docstring 写了"全量历史请离线读 events 表",但**返回值本身不带任何标记**:宿主拿到
一个 200 元素的列表,起始 seq 是 242,看不出前面还有 241 条。照它做统计就是对着一份
残缺时间线做统计,而且不会有任何报错。1.1.1 验证报表时就是这么被坑的。

这里守两件事:
1. `World.history()` 是**分页**的:`next_seq` 不是 None 就代表后面还有,截断没法被忽略。
2. `events(since_seq=…)` 在窗口已经滑过 `since_seq` 时**出声** —— 那正是宿主即将拿到
   一段有洞的历史却以为自己追上了的时刻。
"""
from __future__ import annotations

import logging

import pytest

from anima_world.api import World


def _busy_world(tmp_path, ticks: int = 2600) -> World:
    world = World.open(str(tmp_path / "w.db"), force_mock_llm=True)
    world.tick(ticks)
    return world


def test_history_pages_through_everything_the_window_dropped(tmp_path):
    """窗口只留 200 条,历史必须一条不少地取得回来。"""
    with _busy_world(tmp_path) as world:
        # 世界是活的:叙事跑在线程池上,分页期间还会有新事件落库。所以基准是
        # **开始分页那一刻**的历史,而不是"分页结束时的总数"——后者是移动目标。
        total, max_seq = world.scheduler.event_log.conn.execute(
            "SELECT COUNT(*), MAX(seq) FROM events"
        ).fetchone()
        assert total > 200, f"这个世界只产了 {total} 条事件,测不出窗口截断"

        seen, cursor, pages = [], 0, 0
        while True:
            page = world.history(since_seq=cursor, limit=100)
            seen.extend(page["events"])
            pages += 1
            if page["next_seq"] is None:
                break
            cursor = page["next_seq"]
            assert pages < 100, "分页没有收敛"

        seqs = [e["seq"] for e in seen]
        assert seqs == sorted(set(seqs)), "分页必须有序且不重不漏"
        assert len([s for s in seqs if s <= max_seq]) == total, (
            "开始分页那一刻已经存在的历史,必须一条不少地取回来"
        )
        assert len(world.events()) < total, "前提:窗口确实装不下"


def test_history_hands_back_the_same_shape_as_the_live_stream(tmp_path):
    """宿主不该为了读历史再写一个解析器。"""
    with _busy_world(tmp_path, ticks=20) as world:
        world.player_action("p1", "挥手", {"target": "夏"})
        live = [e for e in world.events() if e["type"] == "player_action"][-1]
        stored = [
            e for e in world.history(limit=10_000)["events"]
            if e["type"] == "player_action"
        ][-1]
        for key in ("seq", "ts", "type", "who", "player_id", "role", "action", "details"):
            assert stored.get(key) == live.get(key), f"{key} 对不上"


def test_history_can_filter_by_who_and_by_type(tmp_path):
    with _busy_world(tmp_path, ticks=300) as world:
        only_xia = world.history(who="夏", limit=10_000)["events"]
        assert only_xia and all(e["who"] == "夏" for e in only_xia)
        narratives = world.history(kind="narrative", limit=10_000)["events"]
        assert all(e["type"] == "narrative" for e in narratives)


def test_history_reports_the_total_so_a_host_knows_the_shape(tmp_path):
    with _busy_world(tmp_path, ticks=300) as world:
        def count() -> int:
            return world.scheduler.event_log.conn.execute(
                "SELECT COUNT(*) FROM events"
            ).fetchone()[0]

        # 夹逼:世界是活的,叙事线程池随时可能再落一条。total 只需落在这一刻的
        # 前后两次计数之间 —— 钉死等号会随机假红。
        before = count()
        page = world.history(limit=10)
        after = count()
        assert before <= page["total"] <= after
        assert len(page["events"]) == 10
        assert page["next_seq"] == page["events"][-1]["seq"]


def test_catching_up_from_beyond_the_window_says_so(tmp_path, caplog):
    """窗口滑过去之后再 catchup,拿到的是有洞的历史 —— 不许静默。"""
    with _busy_world(tmp_path) as world:
        with caplog.at_level(logging.WARNING, logger="anima_world.api"):
            world.events(since_seq=1)
        assert any("history" in r.getMessage() for r in caplog.records), (
            f"窗口早就滑过 seq=1 了,却一声不吭:{[r.getMessage() for r in caplog.records]}"
        )


def test_catching_up_from_inside_the_window_stays_quiet(tmp_path, caplog):
    """正常的增量拉取不该被一条 warning 淹掉。"""
    with _busy_world(tmp_path, ticks=20) as world:
        recent = world.events()
        cursor = recent[len(recent) // 2]["seq"]
        with caplog.at_level(logging.WARNING, logger="anima_world.api"):
            world.events(since_seq=cursor)
        assert not [r for r in caplog.records if "history" in r.getMessage()]
