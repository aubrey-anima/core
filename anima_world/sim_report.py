"""`simulate --report`:把一次快进读成一份机器可读的运行摘要(#11)。

**为什么这份数据必须由引擎出**:它全在事件日志里,但 db 格式与事件 schema 是
引擎的私有契约。让创作台自己伸手进 `world.db` 去数,等于把工具钉死在某个 db
format 上 —— 全 import 原则的反面。所以引擎给口径,消费方按需读取。

摘要回答的是"这个世界好不好玩"能被实证的那部分:
作息设计的相遇窗口到底兑现没有、有没有人整天无事发生、关系有没有在生长。

纯函数,输入是事件列表,不碰世界、不碰时钟 —— 所以离线对任何一个 `world.db`
都能重算,而且可测。

`report_format_version` 与引擎版本分开:统计口径变了(加一个桶、换一种归类)
不该逼消费方升引擎,反过来也一样。
"""

from __future__ import annotations

from typing import Any, Iterable

from anima_world.types import Event
from anima_world.world_time import DEFAULT_MINUTES_PER_TICK, TICKS_PER_DAY, WALL_CLOCK_FLOOR

REPORT_FORMAT_VERSION = 2

# 事件密度的分桶。一个事件只进一个桶,`other` 兜底。
#
# 口径(format 2):**按天的统计只覆盖世界 tick 上的事件**。2.0 之前聊天子系统给
# `conversation` 盖的是墙钟(见 `WALL_CLOCK_FLOOR`),按 tick 折算成"天"会得到
# 六百多万 —— 所以这类事件不进 `by_day`,也不参与 horizon。源头已经修了(事件的
# 时基统一由 `Scheduler._record_event` 盖),这道口径留给**导入进来的老日志**:
# 那批 ts 收不回来,而报表还得读得动它们。它们仍然计入
# `total` 与 `by_type`,并在 `wall_clock_events` 里单独点名:少算比算错更隐蔽,
# 一个聊了一整晚的世界不该拿到一份干干净净、chat 桶为 0 的摘要。
# 于是等式是:`sum(by_day[*].total) + wall_clock_events == total`。
_MOVE = "move"
_WORK = "work"
_SLEEP = "sleep"
_CHAT = "chat"
_IDLE = "idle"
_PLAN = "plan"
_NARRATIVE = "narrative"
_RELATION = "relation"
_ECONOMY = "economy"
_MEMORY = "memory"
_GENESIS = "genesis"
_OTHER = "other"

BUCKETS = (_MOVE, _WORK, _SLEEP, _CHAT, _IDLE, _PLAN, _NARRATIVE,
           _RELATION, _ECONOMY, _MEMORY, _GENESIS, _OTHER)

# 时间分配的活动桶。`transit` 是"在途",与 move 事件同源但语义不同:
# move 数的是"动了几次",transit 数的是"路上花了多久"。
_TRANSIT = "transit"
_EAT = "eat"
ACTIVITIES = (_WORK, _IDLE, _SLEEP, _TRANSIT, _CHAT, _EAT, _OTHER)

# 这几类事件不改变"这个角色此刻在做什么",不该打断上一段活动:
# 叙事是对刚才那个动作的描写,关系判定是聊完之后的异步结算。
_NON_ACTIVITY = {"narrative", "state_change:sentiment", "state_change:sentiment_delta",
                 "state_change:r_type", "memory_seed", "plan", "payment",
                 "item_transfer", "item_consume", "beat_fired", "conversation"}


def _kind(event: Event) -> str:
    """事件的细分类型:`state_change` 要看 payload.kind 才知道是哪一种。"""
    if event.type == "state_change":
        return f"state_change:{(event.payload or {}).get('kind', 'sentiment')}"
    return event.type


def _bucket(event: Event) -> str:
    kind = _kind(event)
    # 世界骨架的登记(名册/地图/能力目录)不是"发生了什么",单列一桶,
    # 否则创世那一天的密度会被一堆开场登记撑起来,与后面几天没法比。
    if kind in ("agent_join", "location_join", "capability_registered"):
        return _GENESIS
    if kind in ("travel", "state_change:location_join", "agent_move",
                "agent_leave", "agent_return"):
        return _MOVE
    if kind == "state_change:agent_state":
        status = ((event.payload or {}).get("state") or {}).get("status")
        return {"working": _WORK, "sleeping": _SLEEP}.get(status, _OTHER)
    if kind == "agent_action":
        action = (event.payload or {}).get("action", "")
        if action == "chat":
            return _CHAT
        if action.startswith("idle"):
            return _IDLE
        return _OTHER
    if kind in ("conversation", "user_message"):
        return _CHAT
    if kind == "plan":
        return _PLAN
    if kind == "narrative":
        return _NARRATIVE
    if kind in ("state_change:sentiment", "state_change:sentiment_delta", "state_change:r_type"):
        return _RELATION
    if kind in ("payment", "item_transfer", "item_consume"):
        return _ECONOMY
    if kind == "memory_seed":
        return _MEMORY
    return _OTHER


def _activity(event: Event) -> str | None:
    """这个事件把角色切换到哪种活动?None = 不切换。"""
    kind = _kind(event)
    if kind in _NON_ACTIVITY:
        return None
    if kind in ("travel", "state_change:location_join"):
        return _TRANSIT
    if kind == "state_change:agent_state":
        status = ((event.payload or {}).get("state") or {}).get("status")
        return {"working": _WORK, "sleeping": _SLEEP}.get(status)
    if kind == "agent_action":
        action = (event.payload or {}).get("action", "")
        if action == "chat":
            return _CHAT
        if action.startswith("idle"):
            return _IDLE
        if action == "eat":
            return _EAT
        return _OTHER
    return None


def _location_timeline(events: list[Event], horizon: int) -> dict[str, list[tuple[int, int, str]]]:
    """每个角色的 [起 tick, 止 tick, 地点) 区间表 —— 在途与离场不产生区间。

    在途**不算在场**是这里唯一有分量的判断:两个人各自走向咖啡店的路上不算
    相遇,否则"作息设计的相遇窗口兑现没有"这个问题就永远是"兑现了"。
    """
    timeline: dict[str, list[tuple[int, int, str]]] = {}
    current: dict[str, tuple[int, str | None]] = {}  # agent -> (since_tick, location|None)

    def _switch(agent_id: str, tick: int, location: str | None) -> None:
        since, previous = current.get(agent_id, (tick, None))
        if previous is not None and tick > since:
            timeline.setdefault(agent_id, []).append((since, tick, previous))
        current[agent_id] = (tick, location)

    for event in events:
        who = event.who
        if not who:
            continue
        kind = _kind(event)
        if kind == "agent_join":
            _switch(who, event.ts, (event.payload or {}).get("location") or event.loc)
        elif kind == "agent_return":
            _switch(who, event.ts, (event.payload or {}).get("location") or event.loc)
        elif kind == "agent_leave":
            _switch(who, event.ts, None)
        elif kind == "travel":
            _switch(who, event.ts, None)  # 出发即离场
        elif kind == "state_change:location_join":
            _switch(who, event.ts, (event.payload or {}).get("location"))

    for agent_id, (since, location) in current.items():
        if location is not None and horizon > since:
            timeline.setdefault(agent_id, []).append((since, horizon, location))
    return timeline


def _overlaps(
    a: list[tuple[int, int, str]], b: list[tuple[int, int, str]]
) -> list[tuple[int, int, str]]:
    """两条区间表里"同时同地"的那些片段,按时间归并相邻的。"""
    raw: list[tuple[int, int, str]] = []
    for a_start, a_end, a_loc in a:
        for b_start, b_end, b_loc in b:
            if a_loc != b_loc:
                continue
            start, end = max(a_start, b_start), min(a_end, b_end)
            if end > start:
                raw.append((start, end, a_loc))
    raw.sort()
    merged: list[tuple[int, int, str]] = []
    for start, end, loc in raw:
        if merged and merged[-1][2] == loc and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end), loc)
        else:
            merged.append((start, end, loc))
    return merged


def _relationship_curves(events: list[Event]) -> list[dict[str, Any]]:
    """每对 sentiment 的起止与拐点。绝对值赋值、增量累加,与投影同一套折叠规则。"""
    series: dict[tuple[str, str], list[float]] = {}
    value: dict[tuple[str, str], float] = {}
    for event in events:
        if event.type != "state_change":
            continue
        payload = event.payload or {}
        kind = payload.get("kind")
        if kind not in ("sentiment", "sentiment_delta"):
            continue
        as_id, target = payload.get("as") or event.who, payload.get("target")
        if not as_id or not target:
            continue
        key = (as_id, target)
        try:
            if kind == "sentiment":
                value[key] = float(payload["sentiment"])
            else:
                value[key] = max(-1.0, min(1.0, value.get(key, 0.0) + float(payload.get("delta", 0.0))))
        except (TypeError, ValueError, KeyError):
            continue
        series.setdefault(key, []).append(value[key])

    curves = []
    for (as_id, target), points in sorted(series.items()):
        deltas = [b - a for a, b in zip(points, points[1:]) if abs(b - a) > 1e-9]
        turning = sum(
            1 for x, y in zip(deltas, deltas[1:]) if (x > 0) != (y > 0)
        )
        curves.append({
            "as": as_id,
            "target": target,
            "start": round(points[0], 4),
            "end": round(points[-1], 4),
            "min": round(min(points), 4),
            "max": round(max(points), 4),
            "changes": len(points),
            "turning_points": turning,
        })
    return curves


def build_run_report(
    events: Iterable[Event],
    *,
    ticks: int,
    minutes_per_tick: int = DEFAULT_MINUTES_PER_TICK,
    engine_version: str | None = None,
    beats: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """把一段事件日志读成运行摘要。纯函数;`ticks` 是这次跑到的 tick 数。

    `beats` 是这个世界**声明过**的那一串节拍(`RedisBeatsStore.definitions()`)。
    给了就多一段 `beats`:**声明了几拍、响了几拍、哪几拍一直没响**。

    ⚠️ **没有它这一段答不出来,而这正是它欠了很久的原因**(FOR-STUDIO §0-②:
    创作台列的五个问题里唯一没答上的一个)。`beat_fired` 事件一直是现成的 ——
    缺的是**分母**:节拍从前只活在一个 `--beats` 文件里,而这条命令只读日志。
    3.7.0 把节拍收进世界(看板 D1)之后,这个差集才第一次算得出来。
    **一拍都没响 = 这个脚本白写,而作者必须当场知道** —— 只报 `fired` 的话,
    "响了 0 拍"和"这个世界压根没有剧情"在屏幕上长得一模一样。
    """
    events = sorted(events, key=lambda e: (e.ts, e.seq))
    if engine_version is None:
        from anima_world import __version__ as engine_version

    per_day = TICKS_PER_DAY(minutes_per_tick)
    # 双时基防护:只有世界 tick 上的事件参与任何 tick 算术(horizon、按天分桶、
    # 时间分配、区间相遇)。一条墙钟 ts 混进来就会把 horizon 拽到 1.78e9。
    timed = [e for e in events if e.ts < WALL_CLOCK_FLOOR]
    wall_clock_events = len(events) - len(timed)
    horizon = max(ticks, max((e.ts for e in timed), default=0))

    # 纯计数(不做 tick 算术)覆盖全部事件 —— 防护只挡算术,不许把事件吞掉。
    by_type: dict[str, int] = {}
    per_agent_events: dict[str, int] = {}
    for event in events:
        by_type[_kind(event)] = by_type.get(_kind(event), 0) + 1
        if event.who:
            per_agent_events[event.who] = per_agent_events.get(event.who, 0) + 1

    by_day: dict[int, dict[str, int]] = {}
    for event in timed:
        day = event.ts // per_day
        by_day.setdefault(day, {})
        bucket = _bucket(event)
        by_day[day][bucket] = by_day[day].get(bucket, 0) + 1

    # 角色名册取自 agent_join —— 与世界重开时的取法一致,日志是唯一权威。
    roster = []
    for event in events:
        if event.type == "agent_join" and event.who and event.who not in roster:
            roster.append(event.who)

    # 时间分配:一段活动持续到下一段开始,与引擎"动作变了才发事件"的语义一致。
    spans: dict[str, list[tuple[int, str]]] = {}
    for event in timed:
        activity = _activity(event)
        if activity is None or not event.who:
            continue
        marks = spans.setdefault(event.who, [])
        if marks and marks[-1][1] == activity:
            continue
        marks.append((event.ts, activity))

    agents = []
    for agent_id in roster:
        marks = spans.get(agent_id, [])
        ticks_by_activity = dict.fromkeys(ACTIVITIES, 0)
        for (start, activity), (next_start, _) in zip(marks, marks[1:] + [(horizon, "")]):
            ticks_by_activity[activity] += max(0, next_start - start)
        measured = sum(ticks_by_activity.values())
        doing_something = sum(
            ticks_by_activity[a] for a in (_WORK, _CHAT, _EAT, _OTHER)
        )
        agents.append({
            "id": agent_id,
            "events": per_agent_events.get(agent_id, 0),
            "ticks_by_activity": ticks_by_activity,
            "share_by_activity": {
                activity: round(count / measured, 4) if measured else 0.0
                for activity, count in ticks_by_activity.items()
            },
            # 一眼可见:整场只有闲逛/睡觉/走路的人。这是"三日试炼"最想抓的
            # 那一类——世界跑了,人却什么也没发生。
            "idle_only": doing_something == 0,
        })

    timeline = _location_timeline(timed, horizon)
    encounters = []
    for index, first in enumerate(roster):
        for second in roster[index + 1:]:
            shared = _overlaps(timeline.get(first, []), timeline.get(second, []))
            if not shared:
                continue
            by_location: dict[str, int] = {}
            for start, end, location in shared:
                by_location[location] = by_location.get(location, 0) + (end - start)
            total = sum(by_location.values())
            encounters.append({
                "a": first,
                "b": second,
                "meetings": len(shared),
                "ticks": total,
                "minutes": total * minutes_per_tick,
                "by_location": dict(sorted(by_location.items())),
            })
    encounters.sort(key=lambda row: (-row["ticks"], row["a"], row["b"]))

    return {
        "report_format_version": REPORT_FORMAT_VERSION,
        "engine_version": engine_version,
        "world": {
            "ticks": ticks,
            "days": -(-horizon // per_day) if horizon else 0,
            "minutes_per_tick": minutes_per_tick,
            "ticks_per_day": per_day,
            "agents": roster,
        },
        "events": {
            "total": len(events),
            "by_type": dict(sorted(by_type.items())),
            # 单列点名:这些事件计入 total/by_type,但没有世界时间可归属。
            "wall_clock_events": wall_clock_events,
            # 稀疏:只列真的发生过事情的天。稠密展开会把一条墙钟 ts 变成
            # 六百万个空行 —— 放不下是 MemoryError,放得下是假答案。
            "by_day": [
                {
                    "day": day,
                    "total": sum(buckets.values()),
                    "buckets": {b: buckets.get(b, 0) for b in BUCKETS},
                }
                for day, buckets in sorted(by_day.items())
            ],
        },
        "agents": agents,
        "encounters": encounters,
        "relationships": _relationship_curves(events),
        # **分母和分子都在**(3.7.0)。`declared` 是 `None` 说明这次调用没给节拍表
        # —— 那是"问不出来",不是"这个世界没有剧情",两者在这里必须分得开。
        "beats": _beat_coverage(events, beats),
    }


def _beat_coverage(
    events: Iterable[Event], beats: Iterable[dict[str, Any]] | None,
) -> dict[str, Any]:
    """哪几拍响过、哪几拍一直没响。

    响过的按 `beat_fired` 事件数(日志是唯一权威,和重启后重建 fired-set 同一份
    来源);声明过的按世界里那份脚本。**`unfired` 是这一段存在的全部理由** ——
    作者要的不是"响了几拍",是"我写的哪几拍白写了"。
    """
    fired: list[str] = []
    for event in events:
        if event.type != "beat_fired":
            continue
        beat_id = str((event.payload or {}).get("beat_id") or "")
        if beat_id and beat_id not in fired:
            fired.append(beat_id)
    if beats is None:
        return {"declared": None, "fired": sorted(fired), "unfired": None}
    declared = [str(b.get("id") or "") for b in beats if str(b.get("id") or "")]
    return {
        "declared": declared,
        "fired": [b for b in declared if b in fired],
        # 声明了却没响的 —— **按作者写的顺序**,那是他读它的顺序。
        "unfired": [b for b in declared if b not in fired],
        # 日志里有、而今天的脚本里没有的:剧情被改过(或者这是一份编辑过的世界)。
        # 不报的话,`declared` 与 `fired` 的两个长度对不上而没人说得出为什么。
        "fired_not_declared": sorted(b for b in fired if b not in declared),
    }
