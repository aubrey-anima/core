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

# **事件的 `ts` 只有一种时基:世界时钟(从 0 开始的 tick 数)。** 这条界线留着,
# 是因为 2.0 之前不是这样:`chat_session.close_conversation` 给 `conversation`
# 事件盖的是墙钟(`time.time()`),于是世界的历史里混进了 1.78e9 那个量级的 ts。
# tick 数不可能长到 Unix 时间戳那个量级(一秒一 tick 也要 30 多年),所以 ts 到
# 了这条线以上,就是一条**老世界留下的**墙钟记录。
#
# 当时的补法是在每个按 tick 做算术的地方各加一道闸 —— 时钟恢复曾经因此把日历
# 读成"第 6194323 天",运行摘要曾经因此按天稠密展开成 620 万项,两处各补一次。
# 而记忆那一路没人补:`MemoryStore.query` 按 `(tick, id)` DESC 排序,于是跟玩家
# 的那几条对话把每个角色的召回列表整个占满,一声不吭。**补闸的坏处就在这儿:
# 它要求你记得每一个消费方。** 源头已经改成 setdefault 世界时钟,这道闸从此只
# 面向历史数据(旧世界文件、没跑过迁移的 Redis 前缀)。
#
# 修历史数据用 `anima-world memory repair-ticks`(`World.repair_memory_ticks`)。
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


def clock_names(tick: int, minutes_per_tick: int = DEFAULT_MINUTES_PER_TICK) -> dict[str, int]:
    """作者写的表达式里恒有的那几个**时间**名字(`rules.BUILTIN_NAMES` 的一半)。

    存在的理由是一个洞:规律和能力此前**读不到时间**。手上只有 `now`(单调 tick
    数)和 `dt`(流逝了多久),而"半夜还醒着要还睡眠债""日落之后果子不再长""子时
    行功事半功倍"这一类全都需要**这一天里的哪个时刻** —— 没有它,凡是跟昼夜有关
    的规律一条都写不出来,作者只能去改引擎。

    `now % 288` 这种手算解决不了:一天多少 tick 取决于 `world.minutes_per_tick`,
    那是**每个世界自己的配置**,写进表达式就等于把一个配置值抄进了数据。

    从 `world_time()` 派生,不另算一遍 —— 世界日历只能有一个来源。
    """
    stamp = world_time(tick, minutes_per_tick)
    return {
        "day": stamp.day,
        "hour": stamp.hour,
        "minute": stamp.minute,
        "minute_of_day": stamp.minute_of_day,
    }


def parse_hhmm(value: str) -> int:
    """"08:00" → 480. Raises ValueError on anything else (seed-time validation)."""
    hours, _, minutes = str(value).partition(":")
    h, m = int(hours), int(minutes)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"not a valid HH:MM time: {value!r}")
    return h * 60 + m
