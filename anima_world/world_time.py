"""World calendar: a pure function of the scheduler's tick clock.

`Scheduler.clock` (monotonic ticks) is the world's ONLY time state. The
calendar — what day it is, what o'clock — is derived from it on demand and
never stored (bt-duties D3). Storing "the current hour" anywhere would be a
second source of truth, which is exactly the trap nested-map D7 closed for the
map.

`minutes_per_tick` comes from `config.world.minutes_per_tick` (default 5). The
runtime config defaults to one tick per 300 real seconds, so world time and
wall time advance at the same rate without changing the existing calendar.
"""

from __future__ import annotations

from dataclasses import dataclass

MINUTES_PER_DAY = 24 * 60

DEFAULT_MINUTES_PER_TICK = 5

# 世界里跑着两种时基:引擎给事件盖的是世界时钟(从 0 开始的 tick 数),而聊天
# 子系统(M3.5)给 `conversation` 盖的是墙钟(chat_session.py 的 clock 是
# `time.time()`)。tick 数不可能长到 Unix 时间戳那个量级(一秒一 tick 也要 30
# 多年),所以 ts 到了这条线以上就是墙钟,不是世界时间。
#
# 每个按 tick 做算术的地方都必须先过这道闸,否则一条聊天记录就能把结果拽到
# 1.78e9:时钟恢复曾经因此把日历读成"第 6194323 天",运行摘要曾经因此按天稠密
# 展开成 620 万项。**这是一条口径,不是某个模块的私事。**
WALL_CLOCK_FLOOR = 1_000_000_000


@dataclass(frozen=True)
class WorldTime:
    day: int
    hour: int
    minute: int
    minute_of_day: int


def TICKS_PER_DAY(minutes_per_tick: int = DEFAULT_MINUTES_PER_TICK) -> int:  # noqa: N802
    """How many ticks make one world day."""
    return MINUTES_PER_DAY // max(1, int(minutes_per_tick))


def world_time(tick: int, minutes_per_tick: int = DEFAULT_MINUTES_PER_TICK) -> WorldTime:
    """Map a tick count onto (day, hour, minute). Pure; holds no state."""
    mpt = max(1, int(minutes_per_tick))
    total_minutes = max(0, int(tick)) * mpt
    day, minute_of_day = divmod(total_minutes, MINUTES_PER_DAY)
    hour, minute = divmod(minute_of_day, 60)
    return WorldTime(day=day, hour=hour, minute=minute, minute_of_day=minute_of_day)


def parse_hhmm(value: str) -> int:
    """"08:00" → 480. Raises ValueError on anything else (seed-time validation)."""
    hours, _, minutes = str(value).partition(":")
    h, m = int(hours), int(minutes)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"not a valid HH:MM time: {value!r}")
    return h * 60 + m
