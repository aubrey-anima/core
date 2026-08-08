"""`simulate --report`:引擎给出的运行摘要口径(#11)。

摘要是给创作台「三日试炼」用的:出厂前实证一个世界好不好玩 —— 作息设计的相遇
窗口兑现没有、有没有人整天无事发生、关系有没有在生长。所以这些断言全是**口径**
断言,不是实现细节;改动它们等于改一份别的仓库正在消费的格式。

事件都是手搭的:世界逐次不确定,靠跑一遍真世界来断言相遇次数必然假绿。
"""
from __future__ import annotations

from _worldfile import run_cli

import json
import subprocess
import sys
import time

import pytest

from anima_world.sim_report import REPORT_FORMAT_VERSION, build_run_report
from anima_world.types import Event

TICKS_PER_DAY = 288  # minutes_per_tick=5


def _ev(seq, ts, type_, who=None, payload=None, loc=None):
    return Event(seq=seq, ts=ts, type=type_, loc=loc, payload=payload or {}, who=who)


def _join(seq, who, location):
    return _ev(seq, 0, "agent_join", who, {"location": location, "spec": {}})


def _move(seq, ts, who, location):
    return _ev(seq, ts, "state_change", who, {"kind": "location_join", "location": location})


def _travel(seq, ts, who, to, arrive_at):
    return _ev(seq, ts, "travel", who, {"to": to, "arrive_at": arrive_at})


def _status(seq, ts, who, status):
    return _ev(seq, ts, "state_change", who, {"kind": "agent_state", "state": {"status": status}})


def _action(seq, ts, who, action):
    return _ev(seq, ts, "agent_action", who, {"action": action})


def test_report_declares_its_own_format_version_separately_from_the_engine():
    """统计口径变了不该逼消费方升引擎,反过来也一样。"""
    report = build_run_report([], ticks=0)
    assert report["report_format_version"] == REPORT_FORMAT_VERSION
    assert report["engine_version"]


def test_leaving_ends_the_encounter_at_departure_not_at_arrival():
    """出发即离场:一个人上了路就不在原地了,哪怕落地事件要 20 tick 后才来。

    夏 整场在 cafe;遥 也在 cafe,t=40 动身去 home,t=60 才落地。相遇必须在
    40 结束。把"还没发出 location_join"读成"还在原地",两人就会在遥早已走掉
    的路上继续"相处"20 tick —— 而"作息设计的相遇窗口兑现没有"正是靠这个数
    回答的,虚报比不报更糟。
    """
    events = [
        _join(1, "夏", "cafe"),
        _join(2, "遥", "cafe"),
        _travel(3, 40, "遥", "home", 60),
        _move(4, 60, "遥", "home"),
    ]
    report = build_run_report(events, ticks=100)
    (meeting,) = report["encounters"]
    assert meeting["a"] == "夏" and meeting["b"] == "遥"
    assert meeting["ticks"] == 40, "遥 40 就走了,路上那 20 tick 不算相处"
    assert meeting["meetings"] == 1
    assert meeting["minutes"] == 200
    assert meeting["by_location"] == {"cafe": 40}


def test_arriving_starts_the_encounter_at_arrival_not_at_departure():
    """反向的同一条规矩:走在半路上不算已经到了。"""
    events = [
        _join(1, "夏", "cafe"),
        _join(2, "遥", "home"),
        _travel(3, 40, "遥", "cafe", 60),
        _move(4, 60, "遥", "cafe"),
    ]
    (meeting,) = build_run_report(events, ticks=100)["encounters"]
    assert meeting["ticks"] == 40


def test_two_separate_visits_are_two_meetings_not_one_long_one():
    events = [
        _join(1, "夏", "cafe"),
        _join(2, "遥", "cafe"),
        _travel(3, 20, "遥", "home", 30),
        _move(4, 30, "遥", "home"),
        _travel(5, 60, "遥", "cafe", 70),
        _move(6, 70, "遥", "cafe"),
    ]
    (meeting,) = build_run_report(events, ticks=100)["encounters"]
    assert meeting["meetings"] == 2
    assert meeting["ticks"] == 20 + 30


def test_an_agent_who_only_idles_is_visible_at_a_glance():
    """「三日试炼」最想抓的一类:世界跑了,人却什么也没发生。"""
    events = [
        _join(1, "夏", "cafe"),
        _join(2, "遥", "cafe"),
        _status(3, 10, "夏", "working"),
        _action(4, 10, "遥", "idle_wander"),
        _action(5, 200, "遥", "idle_social"),
    ]
    by_id = {a["id"]: a for a in build_run_report(events, ticks=300)["agents"]}
    assert by_id["遥"]["idle_only"] is True
    assert by_id["夏"]["idle_only"] is False
    assert by_id["夏"]["share_by_activity"]["work"] > 0.9, "一段活动持续到下一段开始"


def test_sleeping_and_walking_do_not_count_as_doing_something():
    """睡觉和赶路是生活,不是"有事发生" —— 只会睡和走的人同样是 idle-only。"""
    events = [
        _join(1, "柔", "home"),
        _status(2, 10, "柔", "sleeping"),
        _travel(3, 100, "柔", "cafe", 110),
        _move(4, 110, "柔", "cafe"),
        _action(5, 120, "柔", "idle_wander"),
    ]
    (agent,) = build_run_report(events, ticks=200)["agents"]
    assert agent["idle_only"] is True
    assert agent["ticks_by_activity"]["sleep"] == 90
    assert agent["ticks_by_activity"]["transit"] == 20  # travel 10 + 落地后的 walk 10


def test_narrative_and_relationship_events_do_not_interrupt_an_activity():
    """叙事是对刚才那个动作的描写,关系判定是聊完之后的异步结算 —— 都不换活动。"""
    events = [
        _join(1, "夏", "cafe"),
        _status(2, 0, "夏", "working"),
        _ev(3, 50, "narrative", "夏", {"text": "夏擦了擦杯子"}),
        _ev(4, 60, "state_change", "夏", {"kind": "sentiment_delta", "as": "夏",
                                          "target": "遥", "delta": 0.2}),
    ]
    (agent,) = build_run_report(events, ticks=100)["agents"]
    assert agent["share_by_activity"]["work"] == 1.0


def test_relationship_curve_reports_ends_extremes_and_turning_points():
    """关系有没有在生长:起止、极值、拐了几次。绝对值赋值 + 增量累加,与投影同规则。"""
    events = [
        _join(1, "夏", "cafe"),
        _join(2, "遥", "cafe"),
        _ev(3, 0, "state_change", "夏", {"kind": "sentiment", "as": "夏",
                                         "target": "遥", "sentiment": 0.1}),
        _ev(4, 10, "state_change", "夏", {"kind": "sentiment_delta", "as": "夏",
                                          "target": "遥", "delta": 0.3}),
        _ev(5, 20, "state_change", "夏", {"kind": "sentiment_delta", "as": "夏",
                                          "target": "遥", "delta": -0.5}),
        _ev(6, 30, "state_change", "夏", {"kind": "sentiment_delta", "as": "夏",
                                          "target": "遥", "delta": 0.2}),
    ]
    (curve,) = build_run_report(events, ticks=50)["relationships"]
    assert (curve["as"], curve["target"]) == ("夏", "遥")
    assert curve["start"] == pytest.approx(0.1)
    assert curve["end"] == pytest.approx(0.1)
    assert curve["max"] == pytest.approx(0.4) and curve["min"] == pytest.approx(-0.1)
    assert curve["changes"] == 4
    assert curve["turning_points"] == 2, "涨→跌→涨 = 两个拐点;只看起止会读成「没变化」"


def test_event_density_is_bucketed_per_world_day_and_the_buckets_are_exhaustive():
    events = [
        _join(1, "夏", "cafe"),
        _status(2, 10, "夏", "working"),
        _action(3, TICKS_PER_DAY + 5, "夏", "chat"),
        _ev(4, TICKS_PER_DAY + 6, "plan", "夏", {"steps": []}),
        _ev(5, 2 * TICKS_PER_DAY + 1, "payment", "夏", {"from": "__town__", "to": "夏",
                                                        "amount": 20.0}),
    ]
    report = build_run_report(events, ticks=3 * TICKS_PER_DAY)
    days = {row["day"]: row for row in report["events"]["by_day"]}
    assert days[0]["buckets"]["genesis"] == 1, "开场登记单列,否则第 0 天没法与后面比"
    assert days[0]["buckets"]["work"] == 1
    assert days[1]["buckets"]["chat"] == 1 and days[1]["buckets"]["plan"] == 1
    assert days[2]["buckets"]["economy"] == 1
    for row in report["events"]["by_day"]:
        assert sum(row["buckets"].values()) == row["total"], (
            "一个事件只进一个桶,桶的并集必须恒等于总数"
        )
    # 口径(format 2):按天统计只覆盖世界 tick 上的事件,墙钟事件单列。
    assert (
        sum(row["total"] for row in report["events"]["by_day"])
        + report["events"]["wall_clock_events"]
        == report["events"]["total"]
    )


# ── 双时基:聊天事件打的是墙钟,不是世界 tick ────────────────────────────────

def _conversation(seq, who, target, *, ts=None):
    """**老世界**的 `conversation` ts 是墙钟(2.0 之前 `chat_session` 自己盖
    `time.time()`),而其余事件的 ts 是世界 tick。一条就够把按天统计撑到 620 万天。

    源头已经修了(事件的时基统一由 `Scheduler._record_event` 盖),所以这里**合成**
    那个形状 —— 报表要接的是导入进来的老日志,那批 ts 收不回来。"""
    return _ev(seq, int(time.time()) if ts is None else ts, "conversation", who,
               {"with": target, "summary": "聊了两句"})


def test_a_wall_clock_event_does_not_stretch_the_report_across_millions_of_days():
    """聊过一次天的世界,报表不该变成一个 620 万项的列表。

    `horizon = max(ticks, max(e.ts))` 把一个 Unix 时间戳当成了 tick,`by_day`
    再按 `range(max_day + 1)` 稠密展开 —— 放不下就是 MemoryError,放得下就是
    `days=6198680` 的假答案,以及被 horizon 稀释成 `other≈1.0` 的时间分配。
    引擎自己早就知道这条界线(scheduler.py 的 `_WALL_CLOCK_FLOOR`),只是报表没用上。
    """
    events = [
        _join(1, "夏", "cafe"),
        _status(2, 10, "夏", "working"),
        _conversation(3, "夏", "p1"),
    ]
    report = build_run_report(events, ticks=100)

    assert report["world"]["days"] == 1, f"days={report['world']['days']}"
    assert len(report["events"]["by_day"]) == 1
    share = report["agents"][0]["share_by_activity"]
    assert share["work"] > 0.9, f"墙钟 horizon 会把在岗时间稀释成 ~0:{share}"


def test_a_wall_clock_event_is_still_counted_just_not_placed_in_a_day():
    """防护不许把事件**吞掉** —— 那比撑爆更坏,因为测试会照绿。

    聊了一整晚的世界如果得到一份 `chat 桶 0`、total 少一截的干净摘要,消费方
    没有任何办法发现自己少读了东西。
    """
    events = [
        _join(1, "夏", "cafe"),
        _conversation(2, "夏", "p1"),
        _conversation(3, "夏", "p1"),
    ]
    report = build_run_report(events, ticks=100)

    assert report["events"]["total"] == 3, "墙钟事件必须仍计入总数"
    assert report["events"]["by_type"]["conversation"] == 2, "也必须仍计入 by_type"
    assert report["events"]["wall_clock_events"] == 2, "并且单列点名,不是静默丢弃"


def test_by_day_only_lists_days_that_actually_happened():
    """稀疏:第 0 天和第 5 天之间没有事件,就不该凭空造出四个空行。"""
    events = [_join(1, "夏", "cafe"), _action(2, 5 * TICKS_PER_DAY, "夏", "work")]
    report = build_run_report(events, ticks=5 * TICKS_PER_DAY)
    assert [row["day"] for row in report["events"]["by_day"]] == [0, 5]


def test_simulate_writes_a_report_a_tool_can_read(tmp_path):
    """端到端:创作台拿到的是这份文件,不是 stdout 里的一行告警。"""
    db = tmp_path / "w.db"
    report_path = tmp_path / "nested" / "report.json"
    result = run_cli("simulate", "--world-id", "w",
         "--ticks", "300", "--llm", "mock", "--report", str(report_path))
    assert result.returncode == 0, result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["report_format_version"] == REPORT_FORMAT_VERSION
    assert report["world"]["ticks"] == 300
    assert len(report["world"]["agents"]) == 3
    assert report["events"]["total"] > 0
    assert all("idle_only" in agent for agent in report["agents"])
