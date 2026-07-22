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
