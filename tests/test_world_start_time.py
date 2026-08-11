"""世界从**几点**开始 —— `world.start_time`。

tick 0 是午夜。而新世界跑在演示速度上(1 tick/现实秒),于是"装上包看到的第一屏"
是三个人在各自家里睡觉 —— README 承诺的是"30 秒看它跑起来"。

修法不是把引擎默认值从午夜改成九点:**几点开门是这个世界作者的意见**,和作息表
是同一件事,该和作息表写在同一份文件里。所以这一层的形状是
"引擎默认值等价于从前的行为 + 世界文件里的一行"。这个文件盯三件事:

1. 不声明它的世界逐位不变(仍是午夜)。
2. 声明了的世界从那个时刻起步,而换算跟着**这个世界**的 `world.minutes_per_tick` 走。
3. **只在这个前缀第一次有钟的时候生效** —— 一个跑了三天的世界重开,钟一格都不许动。
   第 3 条是这一层唯一会造成数据损失的坏法:把一个活着的世界的现在拨回创世那一刻。
"""
from __future__ import annotations

from _worldfile import read_seed_file, write_seed_file

import pytest

from anima_world.world_time import parse_hhmm


def _seed_with(bare_seed, tmp_path, name: str, **config) -> str:
    """毛坯世界 + 几行 config —— 从毛坯派生,作息表跟着橱窗走不会脱节。"""
    seed = read_seed_file(bare_seed)
    seed["config"] = {**(seed.get("config") or {}), **config}
    return write_seed_file(tmp_path / f"{name}.cyberworld", seed)


def test_不声明的世界仍然从午夜起步(open_world, bare_seed):
    """引擎默认值 = 没人说话时的样子。改这一条等于替所有世界做决定。"""
    world = open_world(world_file=bare_seed)
    assert world.scheduler.clock == 0
    stamp = world.world_time()
    assert (stamp.day, stamp.hour, stamp.minute) == (0, 0, 0)


def test_橱窗世界从开门的时刻起步(open_world):
    """内置世界(旧港)声明了自己几点开门 —— 开箱第一屏不该是三个人在睡觉。"""
    world = open_world()
    stamp = world.world_time()
    assert (stamp.day, stamp.hour, stamp.minute) == (0, 14, 30)
    assert world.scheduler.clock == parse_hhmm("14:30") // 5


def test_换算跟着这个世界的一天有多长走(open_world, bare_seed, tmp_path):
    """一天有多少 tick 是**每个世界自己的配置**。把 288 抄进换算,一个
    1 分钟/tick 的世界就会在早上六点开在 tick 72(= 01:12)上,而且不报错。"""
    path = _seed_with(
        bare_seed, tmp_path, "fine", **{"world.start_time": "06:00", "world.minutes_per_tick": 1},
    )
    world = open_world(world_file=path)
    assert world.scheduler.clock == 360
    stamp = world.world_time()
    assert (stamp.hour, stamp.minute) == (6, 0)


def test_重开一个跑过的世界钟一格都不许动(open_world, fresh_redis):
    """**这一层唯一会造成数据损失的坏法。**

    起始时刻只在这个前缀第一次有钟的时候生效。做不到的话,每一次重启都会把这个
    世界的现在拨回创世那一刻 —— 而世界照跑、日志干净。

    第二半(作者事后改了开门时刻)才是真正咬得住的那一半:往**回**拨会被事件重放
    的 `max(clock, ts)` 兜住,看上去像是好的;往**前**拨没有任何东西兜得住,而
    "把开门时刻从早上改到晚上"就是往前拨。
    """
    world = open_world("keeps_time", redis=fresh_redis)
    world.tick(7)
    was = world.scheduler.clock
    assert was == parse_hhmm("14:30") // 5 + 7
    world.close()

    again = open_world("keeps_time", redis=fresh_redis)
    assert again.scheduler.clock == was

    again.config_set("world.start_time", "23:00")
    again.close()
    third = open_world("keeps_time", redis=fresh_redis)
    assert third.scheduler.clock == was, "作者改了开门时刻,一个跑着的世界被拨到了晚上"


def test_一个已经在午夜的老世界不会被挪到下午(open_world, fresh_redis):
    """这一层落地之前建的世界钟就停在 0 —— 它是**这个世界的现在**,不是"还没设过"。

    `setnx` 是那道真闸,所以这条钉的是"这一层没有绕过它的第二条路"。
    """
    world = open_world("old_world", redis=fresh_redis)
    world.scheduler.clock = 0
    world.close()

    again = open_world("old_world", redis=fresh_redis)
    assert again.scheduler.clock == 0


def test_下午开门的世界不会在第一帧补上一整个上午(open_world):
    """**创世那一刻是"此刻",不是 tick 0。**

    一个量的 `updated_tick` 决定下一次规律求值的 `dt`。播种时把它钉成 0、而钟从
    174 起步,橱窗那棵橡树的第一帧就会补上 175 tick 的生长(0.7 米)——世界照跑、
    日志干净,而那正是这个仓库最怕的坏法。它以前恒等于 0 只是因为世界都从午夜开始。
    """
    world = open_world()
    tree = "tree:harbor_oak"
    before = world.stocks(tree)["树高"]
    rate = world.stocks(tree)["生长速度"]
    world.tick(1)
    grew = world.stocks(tree)["树高"] - before
    assert grew <= rate + 1e-9, f"第一帧长了 {grew},而一 tick 只该长 {rate}"


def test_写错的起始时刻当场开不了机(open_world, bare_seed, tmp_path):
    """降级只有一种样子:悄悄退回午夜。而作者永远发现不了 —— 他只会觉得
    "这个世界怎么老是从半夜开始"。"""
    path = _seed_with(bare_seed, tmp_path, "bad", **{"world.start_time": "下午两点半"})
    with pytest.raises(ValueError, match="world.start_time"):
        open_world(world_file=path)


def test_一个跑着的世界不因为起始时刻写错而开不了机(open_world, fresh_redis):
    """严格只留在它承重的那一刻。一个跑了三天的世界的钟早就有了,那个值对它
    已经没有任何意义 —— 拿它把世界挡在门外,是拿一条用不上的规矩换一次停机。"""
    world = open_world("running", redis=fresh_redis)
    world.tick(3)
    world.config_set("world.start_time", "下午两点半")
    was = world.scheduler.clock
    world.close()

    again = open_world("running", redis=fresh_redis)
    assert again.scheduler.clock == was
