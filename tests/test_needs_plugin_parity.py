"""§9.2 那道闸:**needs 搬成出厂插件之后,世界的行为逐位不变。**

设计稿 `docs/设计-插件系统.md` §9 的检验标准只有一条:**形状对不对,不看例子,
看能不能把出厂的东西用同一形状搬出去。** 而"搬出去了"这句话唯一可验的意思是
——同一份世界、同一个种子、同一批 tick,**旧路和插件路给出同一个世界**。

## 这道闸比的是什么,以及**它有意不比什么**

先说不比什么,因为那一段是量出来的,不是让出来的:

| | 逐次可复现吗 | 为什么 |
|---|---|---|
| `needs()` / `stocks()` | ✅ 逐位相同 | 纯算术,跑在 tick 线程上 |
| `debug_prompt()` | ✅ 逐字节相同 | 同上 |
| `state()` 的 `narrative_log` / `recent_events` / `runtime` | ❌ | 叙事跑在**线程池**上("时钟永不等网络"),它落在哪一 tick 由机器决定 |
| 事件日志 | ❌ | 同上:同一份代码连跑两趟,条数与次序都能不一样 |

⚠️ **后两行不是"插件路做不到",是这个引擎做不到** —— 2026-08-26 拿**未改动的**
引擎连跑两趟量的:关掉叙事之后条数一样了,**次序仍然不同**;开着叙事时连条数都差
(110 vs 112)。**所以"事件日志逐字节相同"这条判据,对任何代码都不成立**,
把它写进闸里只会得到一条永远红的检查,而一条永远红的检查等于没有这条检查。

于是这道闸比的是**那三样真的可复现的**,而且每一样都是**逐字节 / 逐位相等**,
不是"近似":需求四个数、每个 owner 的量表、三份提示词的 sha256,
外加 `state()` **刨掉那三格之后**整份的 sha256。

## 基线是怎么来的

`tests/data/needs_legacy_golden.json` 是**旧路真跑出来的**(内置橱窗、288 tick、
`needs.enabled` 开、规划与自主关、叙事关),连采两趟确认相等之后落的盘。
🔴 **它是这条闸的全部意义**:旧路删掉之后,这份文件是"从前那个世界长什么样"
唯一的证据。改它之前先问一句 —— **是行为该变,还是我把它改坏了。**
"""
from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from _worldfile import open_world_at, redis_for

GOLDEN = json.loads(
    (pathlib.Path(__file__).parent / "data" / "needs_legacy_golden.json").read_text()
)

#: `state()` 里由**线程池**喂的那三格。见模块 docstring 那张表 —— 它们对未改动的
#: 引擎也不可复现,所以不进这道闸。
ASYNC_KEYS = ("narrative_log", "recent_events", "runtime")


_RUNS = [0]


def _run(tmp_path):
    """把橱窗世界跑 288 tick,采下可复现的那几样。**和采基线那一趟逐字同一条路。**

    ⚠️ **每一趟一个全新的世界。** `redis_for` 是"同一个路径 = 同一个 fakeredis",
    所以两趟共用一个路径的话,第二趟接着第一趟那个已经跑了 288 tick 的世界跑 ——
    而那不是"不可复现",是**根本没有重跑**。这条自己踩过一次(2026-08-26):
    `test_基线自己是可复现的` 当场红,而红的原因和被测的东西一点关系都没有。
    """
    _RUNS[0] += 1
    db = tmp_path / f"parity-{_RUNS[0]}.db"
    redis_for(db)
    with open_world_at(str(db), force_mock_llm=True) as world:
        world.config_set("needs.enabled", True)
        world.config_set("planner.enabled", False)
        world.config_set("autonomy.enabled", False)
        # 叙事跑在线程池上,而这道闸比的是 tick 线程算出来的东西。
        world.scheduler.narrative = None
        world.tick(GOLDEN["ticks"])
        agents = sorted(world.scheduler.agents)
        state = {k: v for k, v in world.state().items() if k not in ASYNC_KEYS}
        return {
            "needs": {a: world.needs(a) for a in agents},
            "stocks": {o: world.stocks(o) for o in sorted(world.stock_owners())},
            "prompt_sha": {
                a: hashlib.sha256(json.dumps(world.debug_prompt(a), ensure_ascii=False,
                                             sort_keys=True).encode()).hexdigest()
                for a in agents},
            "state_sha": hashlib.sha256(json.dumps(
                state, ensure_ascii=False, sort_keys=True,
                default=str).encode()).hexdigest(),
        }


def test_需求四个数逐位相同(tmp_path):
    """**逐位,不是近似。** 一个 `pytest.approx` 在这儿会放过"衰减公式的括号挪了
    一个位置"这种改动 —— 而那正是搬家最容易出的错。"""
    got = _run(tmp_path)["needs"]
    assert got == GOLDEN["needs"], (
        "旧路与插件路算出来的需求不一样。**先问是不是行为该变**,"
        "再考虑改基线 —— 那份基线是从前那个世界唯一的证据"
    )


def test_量表是旧的那份_外加搬过来的那三个量(tmp_path):
    """**这一条是唯一一处"预期会变",而变的形状要说死。**

    需求的值从 `:needs` 那张检查点表搬进了量表(`stock:agent:<id>`),所以量表
    必然多三个键 —— 那**就是**这次搬家本身。**但除此之外一个字节都不许动**,
    而且新来的那三个数必须**等于** `needs()` 报的那三个:一个键住在两个地方、
    两处值不一样,是这个仓库最怕的坏法。

    ⚠️ **不许把它写成"多出来的忽略掉"。** 那样一个真的改坏了旧量的改动会从这条
    缝里溜过去 —— 逐键比,只放行**恰好这三个**。
    """
    run = _run(tmp_path)
    got, want = run["stocks"], GOLDEN["stocks"]
    assert sorted(got) == sorted(want), "量表的 owner 名单变了"
    for owner in sorted(want):
        moved = {f"needs.{need}" for need in ("energy", "hunger", "social")}
        extra = set(got[owner]) - set(want[owner])
        assert extra <= moved, f"{owner} 上多出了搬家之外的量:{sorted(extra - moved)}"
        assert not set(want[owner]) - set(got[owner]), f"{owner} 上少了量"
        for key, value in want[owner].items():
            assert got[owner][key] == value, f"{owner}.{key} 变了(旧量不许动)"
        # 搬过来的那三个:**和 `needs()` 报的逐位相等** —— 两处不一致的话,
        # 行为树读一个、宿主读另一个,而两边都不报错。
        if not owner.startswith("agent:"):
            continue          # `world` 那一行没有需求可比
        agent = owner.split(":", 1)[1]
        for need in ("energy", "hunger", "social"):
            key = f"needs.{need}"
            if key in got[owner]:
                assert got[owner][key] == run["needs"][agent][need], (
                    f"{owner}.{key} 和 needs() 报的不是同一个数"
                )


def test_提示词逐字节相同(tmp_path):
    """她收到的那几个字**一个都不许变** —— 需求进的是提示词,不只是存储。"""
    got = _run(tmp_path)["prompt_sha"]
    assert got == GOLDEN["prompt_sha"], "提示词变了"


def test_state刨掉线程池那三格之后逐字节相同(tmp_path):
    got = _run(tmp_path)["state_sha"]
    assert got == GOLDEN["state_sha"], (
        f"`state()` 变了(已刨掉 {list(ASYNC_KEYS)});"
        "要看差在哪一格,把 ASYNC_KEYS 之外的键逐个 diff 一遍"
    )


def test_基线自己是可复现的(tmp_path):
    """**这条守的是这道闸本身。** 同一份代码连跑两趟必须给同一个答案 ——
    不然上面四条红起来时,分不出是搬家搬坏了还是这道闸自己在抖。
    """
    assert _run(tmp_path) == _run(tmp_path)
