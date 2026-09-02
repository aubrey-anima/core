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
| 事件日志里 **`narrative` 那一支** | ❌ | 同上 |
| **采样时机本身** | ❌ 除非先等池静下来 | 见 `_quiesce` —— 这道闸自己栽过一次 |
| 事件日志里**其余全部**(按**多重集**比,不比次序) | ✅ 逐条相同 | 见下面那三段 |

🔴 **这三行 2026-08-26 改过两次,而两次都是被证据推着走的 —— 值得逐字读一遍。**

**第一版(我)说得太宽**:「事件日志逐字节相同对任何代码都不成立」,证据是连跑两趟
条数不同(110 vs 112)、关掉叙事之后**次序**仍然不同。**一句说宽了的免责,和一盏
假绿灯是同一件事** —— 它让本来验得了的那一大半跟着一起豁免掉了。

**第二版(验收 A)把它收窄**:差的只是 `narrative` 那一支;滤掉它之后 old / new
各两趟条数相同、`(type, payload)` **逐条**相同。A 是对的那一半 —— 那一大半确实验得了。

**第三版(我,照 A 的方法多跑几趟)**:A 那句"逐条相同"**在次序上是运气**。
同一份代码连跑两趟,`103` 条一条不差、类型计数一模一样,而**第 59 条上
`state_change` 和 `travel` 换了位置** —— 关系判定和叙事一样跑在**线程池**上
(`judge` 那条),它落在哪一 tick 由机器决定。三趟连着跑又全同,所以它是**时好时坏**,
而一条时好时坏的闸比没有更贵:它红的时候没人知道该不该信。

**所以这道闸比的是多重集(排过序),而那仍然是逐条精确相等,不是"差不多"** ——
少一条、多一条、payload 差一个字节都会红。**被排除在外的只有"次序"这一件事,
而它由线程池决定,不由这份代码决定。**

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
import time

import pytest

from _worldfile import open_world_at, redis_for

GOLDEN = json.loads(
    (pathlib.Path(__file__).parent / "data" / "needs_legacy_golden.json").read_text()
)

#: `state()` 里由**线程池**喂的那三格。见模块 docstring 那张表 —— 它们对未改动的
#: 引擎也不可复现,所以不进这道闸。
# 🔴 **`player_join` 也滤掉,而这不是"金线红了就把它加进白名单"。**
# 这两条金线量的是一件很具体的事:**把 needs / economy 从内核搬成出厂插件,行为
# 一个字节都没变**。基线是从**旧路那棵树**采的,而 3.9.0 给引擎加了一条全新的事件
# (`player_join`:他第一次走进这个世界那一天,`for_each: {"node":"player"}` 的剧情拍
# 拿它当零点)—— 那是一次**独立的、有意的**行为变更,不是搬家搬出来的偏差。
# ⚠️ **不能靠"重采基线"化解**:基线记的是旧路的行为,而旧路的代码已经删了 ——
# 拿 HEAD 重采一遍,这道闸就变成 HEAD 和 HEAD 比,永远绿而且什么都不测。
# 所以滤,而且**只滤这一种**:多一条别的、少一条、payload 差一个字节,照旧当场红。
# 🆕 **`pack_installed` 同理(3.10.0)**:橱窗从这一版起自带一条 `pack` 记录
# (「做了却开箱看不见等于没做」),于是每个从橱窗建起来的世界创世那一趟多一条
# `pack_installed`。它和 `player_join` 逐字同一种情形 —— **一次独立的、有意的
# 行为变更,不是搬家搬出来的偏差**,而基线所在的那棵旧路的树里根本没有这个事件。
# ⚠️ 同样不能靠重采化解,理由和上面那句逐字相同。
_PARITY_IGNORED_EVENTS = ("narrative", "player_join", "pack_installed")

ASYNC_KEYS = ("narrative_log", "recent_events", "runtime")


def _quiesce(world, *, timeout: float = 30.0) -> None:
    """**等线程池把手上的活干完,再采样。**

    🔴 **这一条是这道闸自己的 bug,而它长成了这道闸最怕的样子**:2026-08-26
    调度台在隔离树上连跑两趟全量 —— 第一趟 `test_提示词逐字节相同` 红、第二趟全绿、
    单跑也绿。我自己在 30 个文件之后跑同一条,红的是 `test_基线自己是可复现的`
    (同一个进程里连采两趟就不一样)。**同一棵树、同一条命令、两个答案。**

    病根不是顺序依赖,是**采样时机**:反思、关系判定、叙事跑在 `ThreadPoolExecutor`
    上("时钟永不等网络"),它们的产物落在第几 tick **由这台机器此刻忙不忙决定**。
    全量跑的时候 CPU 紧,某个池产物在渲染提示词**之前**或**之后**落地 —— 而记忆
    多一行、少一行,她的提示词就是另一份字节。**一条时好时坏的闸比没有更贵**,
    这句话我在多重集那一条上说过,这次轮到我自己。

    **修法是等它静下来,不是放宽 sha。** 这道闸比的本来就是"静止态":
    同一个世界跑完 288 tick、尘埃落定之后长什么样。判据照 `test_erase_player`
    里那个 `_quiesce` 同一手 —— **事件数连着几轮不动**就算落完
    (`max_seq` 是所有池产物最终都要经过的那个窄口)。
    """
    log = world.scheduler.event_log
    deadline = time.monotonic() + timeout
    stable, last = 0, -1
    while time.monotonic() < deadline:
        now = log.max_seq()
        stable = stable + 1 if now == last else 0
        if stable >= 5:
            return
        last = now
        time.sleep(0.05)
    raise AssertionError(
        "等了 30 秒线程池还没静下来 —— 这道闸比的是静止态,而这个世界没停"
    )


# 🔴 **`_quiesce` 只治了这个病的一半 —— 另一半 2026-08-26 修完了(裁决 ②)。**
#
# 病是这样的:这道闸此前**时好时坏**,而且在 `fd1f20d`(那个"修好了 quiesce"的
# 提交)上一样红 —— 交叉重跑量的,不是推的:
#
#     base(fd1f20d) 9 趟 → 红 5   |   同日 head 9 趟 → 红 9
#     (先各连跑 5 趟,再 base / head 交替跑 4 对,控住"机器此刻忙不忙")
#
# 红的一条是 **`test_基线自己是可复现的`** —— **它比的是同一份代码对它自己**,
# 所以它红的时候说的一定不是"谁改坏了什么"。
#
# **病根**:上面 `_run` 只把**叙事**关了,而**关系判定跑在另一个线程池上**
# (`scheduler._judge_pool`),它的产物落在第几 tick 同样由这台机器决定。
#
# **修法(裁决 ②)**:两池全关 + **从旧路那棵树重采基线**。写者当时报的两条出路
# ——「重采只能从插件路采」——**建立在一个假前提上**:旧路没有被删掉,它在 git 里
# (`fa1507b^` = `a6b3da3`,`git worktree` 一敲就在)。
#
# 新基线的出处、怎么跑的、和上一份逐格差在哪,全写在
# `tests/data/needs_legacy_golden.json` 的 `_采自` 里,每一条都可复核:
# `needs` 与 `prompt_sha` 和上一份**逐字节相同**;`stocks` 只差那三个 `needs.*` 键
# (= 搬家本身);`events` 103 → 99,少的 4 条**点得出名字**(2 条 memory_seed +
# 2 条 state_change,全是关系判定那一次的产物)。
# 上一份没被抹掉:`fa1507b:tests/data/needs_legacy_golden.json`,sha256 记在那儿。
#
# ⚠️ **采样那一步有个坑**:`PYTHONPATH` 单独放是不够的 —— 本仓 editable 装的是一个
# `__editable__` 的 meta_path finder,它比 `sys.path` **先说话**,于是
# `anima_world.__file__` 会照旧指回工作树,而采基线的人以为自己在旧树上跑。
# 先把它从 `sys.meta_path` 摘掉。判据是**正的**:
# `hasattr(anima_world.needs, "factory_plugin")` 在旧树上是 **False**。
#
# **判据不是"跑一趟绿了"**:连跑 **9 趟全绿**(2026-08-26 实测,起跑 load 1.13)。
#
# 那才是这一族闸该有的顺序 —— **先决定比什么,再采基线**,不是反过来。


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
        # **两个池都关**(2026-08-26 裁决 ②)。只关叙事的那一版让这道闸时好时坏:
        # 关系判定跑在另一个线程池上,它的产物落在第几 tick 同样由这台机器决定。
        # 交叉重跑量过:base 9 趟红 5、head 9 趟红 9,而红的一条是
        # `test_基线自己是可复现的` —— 它比的是**同一份代码对它自己**。
        # 基线也是这么采的(见 golden 里的 `_采自`),所以两边可比。
        world.scheduler.narrative = None
        world.scheduler.relationship_judge = None
        world.tick(GOLDEN["ticks"])
        _quiesce(world)
        agents = sorted(world.scheduler.agents)
        state = {k: v for k, v in world.state().items() if k not in ASYNC_KEYS}
        return {
            "needs": {a: world.needs(a) for a in agents},
            "stocks": {o: world.stocks(o) for o in sorted(world.stock_owners())},
            "prompt_sha": {
                a: hashlib.sha256(json.dumps(world.debug_prompt(a), ensure_ascii=False,
                                             sort_keys=True).encode()).hexdigest()
                for a in agents},
            # 🔴 **非叙事事件的多重集** —— 被验收 A 收回来的那一半(见模块 docstring)。
            # **排序之后比**:次序不进这道闸,理由在 docstring 里。
            "events": sorted(
                [e.type, json.dumps(e.payload, ensure_ascii=False, sort_keys=True)]
                for e in world.scheduler.event_log.replay()
                if e.type not in _PARITY_IGNORED_EVENTS
            ),
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
        # 🔴 **每一格搬家在这儿加一行,而加行的时候要问一句:它真的是搬家吗。**
        # `economy.coins` 是第 2 期 2d-① 那一格(钱包搬成 projected 事实)——
        # 它落在这个橱窗世界上,因为橱窗把 `economy.enabled` 点亮了。
        # ⚠️ **逐键放行,不许写成"多出来的忽略掉"**:那样一个真的改坏了旧量的
        # 改动会从这条缝里溜过去。
        moved = {f"needs.{need}" for need in ("energy", "hunger", "social")}
        moved |= {"economy.coins"}
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
            # ⚠️ **硬断言,不是"有才比"**(2026-08-26 验收 A):写成
            # `if key in got[owner]` 的话,哪天键名一改这三条就**静默跳过**,
            # 而这条用例照绿 —— 一条只在"东西还在"时才检查的断言,正是它要防的
            # 那种改动最先绕开的地方。
            assert key in got[owner], f"{owner} 上没有 {key} —— 搬家没搬到他头上?"
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


def test_非叙事事件逐条相同_按多重集(tmp_path):
    """🔴 **被验收 A 收回来的那半道闸,而收回来的分寸自己也校准过一次。**

    完整那三段在模块 docstring 里。一句话:**比的是多重集,不是次序** ——
    少一条、多一条、payload 差一个字节都会红;而"第 59 条和第 60 条换了位置"
    不红,因为那由线程池决定(关系判定和叙事都跑在上面),不由这份代码决定。
    """
    got = _run(tmp_path)["events"]
    assert got == GOLDEN["events"], (
        "非叙事事件变了。**先问是不是行为该变** —— 这一族由 tick 线程发出,"
        "和量一样确定,它变了就是这个世界变了"
    )


def test_中途吃一次_末态仍然逐位同(tmp_path):
    """🔴 **288 tick 的末态比不出"吃"这个动作**(2026-08-26 验收 A 指出的闸设计洞)。

    A 的 P1 正是从这条缝里溜过去的:`_apply_item_restores` 只写黑板,而 3.8.0 起
    黑板是派生值 —— 吃完那一刻对、下一 tick 被盖回去。而 parity 比的是**末态**,
    demo 的 mock 路上"吃"要么没发生、要么被后面那两百多个 tick 淹没了。

    这一条中途真吃一次再走一 tick:**那一口下去的效果必须留下来。**
    """
    db = tmp_path / "ate.db"
    redis_for(db)
    with open_world_at(str(db), force_mock_llm=True) as world:
        world.config_set("needs.enabled", True)
        world.config_set("economy.enabled", True)
        world.config_set("planner.enabled", False)
        world.config_set("autonomy.enabled", False)
        world.scheduler.narrative = None
        world.scheduler.economy_store.put_item(
            "big_bowl", "大碗面", "consumable", 12.0, {"hunger": 0.5})
        world.tick(10)
        owner = world.scheduler.stock_owner_of("夏")
        before = world.stocks(owner)["needs.hunger"]
        world.scheduler._record_event({
            "type": "item_consume", "who": "夏",
            "payload": {"who": "夏", "item_id": "big_bowl", "source": "test"}})
        eaten = world.stocks(owner)["needs.hunger"]
        assert eaten > before, "吃了一碗回 0.5 的面,量表一格没动"
        world.tick(1)
        kept = world.stocks(owner)["needs.hunger"]
        assert kept > before, (
            f"吃完 {eaten},走一 tick 剩 {kept},而吃之前是 {before} —— "
            "那一口被下一次折算盖回去了(黑板是派生值,真值在量表里)"
        )
