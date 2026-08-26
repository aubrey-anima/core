"""定时轮次:没人跟她说话的时候,她自己决定要不要做点什么。

聊天里的能力只在**玩家开口之后**才有机会被选中,于是"她能主动"这件事此前全靠人来
触发。这些测试盯的是那条触发链真的通:时钟到点 → 快照 → 决定 → **能力真的执行**
→ 玩家那边看得见,而且

- 时钟**永不等网络**(这条是这个引擎最老的不变量,也是最容易在加挂钩时破掉的)
- 默认什么都不做、默认整个关着
- 有上限,而且"被问"不算"用掉额度"
- 找一个不在场的人是空动作,当场拒绝

⚠️ **这个文件全绿,不等于 `_drain` 那条 barrier 走过**(2026-08-25 实测,`uptime`
报 load≈8–10 的机器上;这一段是给下一个改 `_drain` 的人看的)。`_drain` 是两半:
**① 往世界那条循环上投一个协程再等它回来**(一次往返,顺带让排在它前面的回调跑完)、
**② 到了那边再等掉眼前排着的其余 task**。三条判据量下来:

- 这个文件 26 条测试里有 **24 条调过 `_drain`**(22 条引擎的 + 下面那两条判据自测)。
  barrier 第一眼看见的 pending:**那 22 条引擎测试的每一次调用都是 0**;
  真等住过(pending=1)的只有那两条自测。
- 把 `_drain` 整个改成 `return`:**9 failed / 17 passed** —— 所以 ① **是承重的**,
  **7 条引擎测试**真的靠它。
- 只把 ② 那段 `while pending: await asyncio.wait(pending)` 换成 `pass`:
  **2 failed / 24 passed**,红的正是那两条自测 —— 所以 ② 在那 22 条上**一次都没走过**。

**两个别读错的方向**:别据此说"barrier 什么都没等"(① 承重,拿掉当场红 7 条);
也别拿"引擎那些测试还是绿的"当 `_drain` 的验收 —— 它们证的是 ①,
② 的证据只有那两条自测。加一条新测试想靠 ② 的话,先按上面第一条量一眼 pending,
别默认它走到了。

⚠️ **上面那三个数是同日晚些时候重新量过的**(`test_the_clock_never_waits_for_the_network`
当天改成用 `_GatedLLM` 之后):**9 / 17 与 2 / 24 一位没动**。这一句不是凑数 ——
**这三个数是在一份已经不存在的文件上量的**,而它们旁边没有任何一处会因为文件变了而变红,
所以改这个文件的人有义务顺手复量一次。复量的两条命令写在这儿,省得下一个人自己拼:

    T=$(mktemp -d) && git archive HEAD | tar -x -C $T && cp tests/test_autonomy.py $T/tests/
    # ① 在 $T 里给 `_drain` 开头插一行 `return`  → 预期 9 failed / 17 passed
    # ② 在 $T 里把那段 `while True: … await asyncio.wait(pending)` 换成 `pass` → 预期 2 failed / 24 passed
"""
from __future__ import annotations

from _worldfile import open_world_at, run_cli

import asyncio
import concurrent.futures
import os
import sys
import threading

import pytest

from anima_world.api import World

A_DAY = 288


class DecidingLLM:
    """按脚本替她做决定,并记下**是哪条线程**在调 LLM。

    ⚠️ **这里曾经有一个 `delay=` 旋钮**(`time.sleep(self.delay)`),给
    `test_the_clock_never_waits_for_the_network` 用来"让 LLM 慢半秒,看 `tick()`
    花了多久"。2026-08-25 连它一起拿掉了:一个"睡几秒"的旋钮就是一把挂钟,而这个
    文件里已经有两条判据栽在挂钟上。要卡住一次调用,用下面的 `_GatedLLM` ——
    **那一轮什么时候落地由测试说了算,不由这台机器的手速说了算。**
    """

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.threads: list[str] = []
        self.prompts: list[str] = []

    def _next(self, messages) -> str:
        self.threads.append(threading.current_thread().name)
        self.prompts.append("\n".join(m["content"] for m in messages))
        return self.replies.pop(0) if self.replies else "无"

    async def stream(self, messages):
        yield self._next(messages)

    async def complete(self, messages) -> str:
        return self._next(messages)


class _GatedLLM:
    """卡在一扇**由测试自己开的闸**上,而且不占着那条循环(用执行器等)。

    要点是"这一轮什么时候落地由测试说了算" —— 不靠睡一个定数。满载的机器上,
    测试线程自己也可能被饿掉半秒:那样一来"睡 0.5 秒的一轮"会在测试还没开口问
    之前就落了地,而用得着它的那几条测试要的恰恰是"它**还没**落地"这个前提。
    **用挂钟去搭一条证明挂钟不可信的测试,是同一个坑再踩一遍。**

    ⚠️ 它原先住在文件末尾那节"判据自己也得站得住"里,只服务两条自测。
    2026-08-25 起 `test_the_clock_never_waits_for_the_network` 也用它,
    所以搬到这儿和 `DecidingLLM` 并排 —— **它现在是这个文件的第二种 LLM 替身,
    不是自测的私产。**
    """

    def __init__(self, gate: threading.Event) -> None:
        self._gate = gate
        self.entered = threading.Event()   # 这一轮真的起来了(前提,不是结论)
        self.threads: list[str] = []

    async def complete(self, messages) -> str:
        self.entered.set()
        # `wait` 必须有上界:测试万一提前红了,闸就再没人开 —— 而执行器线程挂在
        # 那儿的话,解释器退出时 `concurrent.futures` 的 atexit 会去 join 它,
        # 于是 pytest 跑完了却不退出(一次真的挂死,就是这么来的)。
        # ⚠️ **这个数不参与任何判断**:它到点只是把这条调用放走,而每条用到它的
        # 测试都在"这次调用做完了没有"这个**事实**上下结论,不在秒数上。
        await asyncio.get_running_loop().run_in_executor(None, self._gate.wait, 60)
        self.threads.append(threading.current_thread().name)
        return "无"

    async def stream(self, messages):
        yield await self.complete(messages)


def _world(tmp_path, *replies: str, interval: int = 1,
           enabled: bool = True, agents: int = 1) -> tuple[World, DecidingLLM]:
    """默认**一个角色**的世界:脚本化的回复是全局按调用顺序弹出的一个列表,
    多角色时哪句话落在谁身上取决于处理顺序 —— 单角色让每条测试的断言不用猜顺序。
    需要验证"每个角色各自有自己的额度"这类跨角色行为时显式传 `agents=3`。
    """
    world = open_world_at(str(tmp_path / "w.db"), agents=agents, force_mock_llm=True)
    llm = DecidingLLM(*replies)
    world.chat_service._background_llm = llm
    world.config_set("chat.tools.enabled", True)
    world.config_set("autonomy.enabled", enabled)
    world.config_set("autonomy.interval_ticks", interval)
    world._install_autonomy()
    return world, llm


def _where(world: World, agent_id: str) -> str:
    brain = world.scheduler.agents[agent_id]
    return brain.agent.blackboard.read("loc") or brain.agent.location


def _seat_player(world: World, agent_id: str = "夏", player_id: str = "p1") -> None:
    """把玩家放到角色跟前 —— **必须在第一次 `tick()` 之前**做:`interval=1` 时
    第一次 tick 就会触发一轮自主决定,轮到时玩家还没注册在场,`reach_out` 会
    当场因"身边没有人"失败,白白吃掉一条脚本化的回复。"""
    world.player_move(player_id, _where(world, agent_id))


# 这条 barrier 自己也是那条循环上的一个 task —— 认得出来,才不会两条 barrier
# 互相等(一次 `_drain` 超时之后,上一条还悬在循环上)。
_BARRIER_NAME = "anima-test-drain-barrier"

# **兜底闸门,不是判据。** 只防死锁:跳了就明说是这台机器,绝不冒充引擎的结论。
# 忙机器上调大它不会改变任何一条测试的答案(答案由下面那条事件判据给),
# 所以它可以给得很宽。
_ROUND_GUARD_SECONDS = float(os.environ.get("ANIMA_TEST_ROUND_GUARD", "60"))


def _drain(world: World) -> None:
    """等世界自己那条循环把手上排着的轮次跑完 —— **这是事件,不是挂钟**。

    定时轮次由 tick 线程 `asyncio.run_coroutine_threadsafe` 丢到 `world._bridge._loop`
    上,而 `world.tick(n)` 是同步的:它返回时,那 n 轮的 task 已经全在那条循环上排着了。
    于是"落地没有"是一个**问得出来的事实** —— 往同一条循环上再投一个协程,
    等掉它眼前排着的其余 task 即可。

    ⚠️ **这里原先是一个 5 秒的挂钟**(`_settle(..., timeout=5.0)`):轮询到点就返回
    `predicate()`,而那时那一轮多半还没落地,于是红的是**调用方自己的断言** ——
    屏幕上写着「定时轮次没走通」。2026-08-25 四仓测试并行跑那趟,
    `test_a_broken_decision_call_never_takes_down_the_clock` 就是这么红的
    (同一份代码单跑 0.17 秒绿、整文件 24 项绿、独占重跑全绿)。
    **它红出来的样子,和真的把时钟拖垮逐字相同** —— 而下一个看见它红的人多半在 CI 上,
    没有第二台机器可以复核。判据脆到会替引擎认罪,比没有这条测试更坏。
    """
    loop = world._bridge._loop

    async def _barrier() -> None:
        current = asyncio.current_task()
        if current is not None:
            current.set_name(_BARRIER_NAME)
        while True:
            pending = [
                task for task in asyncio.all_tasks()
                if task is not current and task.get_name() != _BARRIER_NAME
            ]
            if not pending:
                break
            await asyncio.wait(pending)
        # 轮次的收尾(`_on_autonomy_round_done` 把崩溃喂回 `autonomy_stats`)挂在
        # task 的 done 回调上。它排在这条 barrier 被唤醒之前,但再让一次更保险:
        # 让循环把手上剩下的回调也走完,再回话。
        await asyncio.sleep(0)

    try:
        asyncio.run_coroutine_threadsafe(_barrier(), loop).result(_ROUND_GUARD_SECONDS)
    except concurrent.futures.TimeoutError:
        raise AssertionError(
            f"世界自己那条循环 {_ROUND_GUARD_SECONDS:g} 秒没跑完手上的轮次。"
            "**这不是引擎的结论**:要么这台机器太忙(把环境变量 "
            "ANIMA_TEST_ROUND_GUARD 调大再看一次),要么那条循环上真的死锁了。"
        ) from None


def _settle(world: World, predicate):
    """等**那一轮真的落地**,然后再看结论 —— 只给**要用那个结论**的调用点。

    没有 `timeout` 这个参数是有意的:一个能调的秒数会把"等够了"重新变成判据。

    ⚠️ **它不是 `assert`,也不重试。** `predicate` 只求值一次、**不参与任何判断**,
    等待全在 `_drain` 里。2026-08-25 验收挑出来:24 个调用点里 **20 个把返回值丢了**,
    于是 `_settle(world, lambda: world.autonomy_stats()["failed"])` 读起来像
    "等到 failed 非零",而它什么都不把 —— **一条看起来在把关、其实什么都不把的判据,
    出现了 20 次**,还挡在真正的 `assert` 前面替它顶了个名。
    那 20 处已改成直呼 `_drain(world)`(等待一个字没少,少的只是那句假话)。
    留在这儿的调用点只有一种形状:**返回值真的被用上** —— 现在 **5 处**,
    其中 4 处用了返回值,第 5 处是那条自测(它要的恰恰是"等不到时它不给结论")。
    ⚠️ **别拿 `grep -c '_settle(' tests/test_autonomy.py` 数调用点** ——
    `def` 那行、`_drain` 的病历里那句、以及这段说明自己(含这一行)都会被命中,
    真调用点只有 **5 处**,而这条命令答的是 **9**。
    **一条会命中自己说明的判据,和"还有 9 处没改"在屏幕上长得一模一样** ——
    要数就 `grep -n` 出来看行号:5 处在 `def _settle` 那一段之外。
    """
    _drain(world)
    return predicate()


def test_she_comes_looking_for_you_on_her_own(tmp_path):
    """整条链:时钟到点 → 她挑了 reach_out → 玩家的收件箱里真的有她带来的一句话。"""
    world, llm = _world(
        tmp_path,
        '〔tool:reach_out {"player_id": "p1", "text": "你今天还来吗?我留了豆子。"}〕',
    )
    with world:
        _seat_player(world)

        world.tick(1)   # 到点
        hails = _settle(world, lambda: [
            event for event in world.inbox("p1")
            if event["payload"].get("reason") == "initiative"
        ])
        assert hails, f"定时轮次没走通:{world.autonomy_stats()}"
        assert hails[0]["payload"]["text"] == "你今天还来吗?我留了豆子。"
        assert world.autonomy_stats()["acted"] == 1

        # 敲门不是对话(#13 的边界照旧):不开会话、不动关系。
        assert world.conversations(hails[0]["payload"]["agent_id"]) == []


def test_the_clock_never_waits_for_the_network(tmp_path):
    """引擎最老的一条不变量。加挂钩最容易破的就是它。

    判据是一个**事实**,不是一个秒数:把那次 LLM 调用卡在一扇只有测试开得了的闸上,
    于是"时钟等没等网络"变成一个问得出来的问题 ——
    **`tick()` 回来的那一刻,那次调用做完了没有?**

    - 没做完(健康):时钟先回来了,网络还在别的线程上挂着。
    - 做完了(破了):它只可能是被那次调用本身放回来的 —— 要么调用就发生在
      tick 线程上,要么 tick 在等那一轮的结果。两种都是"时钟等了网络"。

    ⚠️ **这里原先是 `assert elapsed < 0.2`**(让 LLM 睡 0.5 秒,量 `tick()` 花了多久)。
    它和同一个文件里已经修掉的 `_settle` 5 秒挂钟是同族,而且更险:红出来的原话是
    「tick 被 LLM 拖住了(0.31s)」—— **一句在指控引擎的话**,而真相多半是这台机器
    那 0.3 秒没排上 CPU。下一个看见它红的人多半在 CI 上,没有第二台机器可以复核。
    (2026-08-25 改。`_GatedLLM` 里那个 60 秒上界只防挂死:它到点只是把调用放走,
    **不参与判断** —— 判断读的是 `llm.threads` 这个列表空不空。)
    """
    gate = threading.Event()
    world, _ = _world(tmp_path, "无")
    llm = _GatedLLM(gate)
    try:
        world.chat_service._background_llm = llm
        _seat_player(world)

        ticking = threading.current_thread().name
        world.tick(1)
        finished_when_the_clock_came_back = list(llm.threads)

        assert not finished_when_the_clock_came_back, (
            "`tick()` 回来的时候,那次 LLM 调用**已经做完了** —— 而它卡在一扇"
            f"测试还没开的闸上,所以只能是时钟自己等到了它:{finished_when_the_clock_came_back}"
        )

        # 前提(不是结论):那一轮真的起来了,所以上面那句"还没做完"不是句空话。
        assert llm.entered.wait(_ROUND_GUARD_SECONDS), (
            f"这一轮 {_ROUND_GUARD_SECONDS:g} 秒都没排上那条循环。**这不是引擎的结论** ——"
            "要么这台机器太忙(调大 ANIMA_TEST_ROUND_GUARD 再看一次),要么那条循环死锁了"
        )

        gate.set()
        _drain(world)
        assert llm.threads, "闸开了,那次调用却始终没做完 —— 上面那条断言因此什么都没证"
        assert all(name != ticking for name in llm.threads), llm.threads
    finally:
        gate.set()
        world.close()


def test_doing_nothing_is_the_default_and_leaves_no_trace(tmp_path):
    """什么都不做是常态 —— 不发**自主轮次自己的**事件、不占额度。

    每六小时一条"她想了想,没做"的事件会把日志灌满而不带一点信息。世界本身照常
    发它自己的事件(行为树的状态切换之类),这条只盯 autonomy 有没有多插一条。
    """
    world, _ = _world(tmp_path, "无", "无", "无")
    with world:
        _seat_player(world)

        world.tick(3)
        _drain(world)

        assert world.autonomy_stats()["acted"] == 0
        assert world.autonomy_stats()["quiet"] >= 1
        assert world.inbox("p1") == []
        assert [
            e for e in world.history()["events"]
            if e["type"] == "agent_hail" and e["payload"].get("reason") == "initiative"
        ] == [], "什么都没做,却留下了一条主动搭话的事件"


def test_being_asked_is_not_the_same_as_using_up_the_quota(tmp_path):
    """上限是"主动几次",不是"被问几次" —— 否则安静的角色会被额度饿死。"""
    world, llm = _world(
        tmp_path, "无", "无",
        '〔tool:reach_out {"player_id": "p1", "text": "在吗?"}〕',
        interval=1,
    )
    with world:
        _seat_player(world)
        world.config_set("autonomy.max_per_day", 1)

        for _ in range(12):
            world.tick(1)
            _drain(world)          # 等那一轮落地,不是睡一个定数
            if world.autonomy_stats()["acted"]:
                break
        assert _settle(world, lambda: world.inbox("p1")), (
            f"两次不做之后那一次主动被额度吃掉了:{world.autonomy_stats()}"
        )


def test_the_daily_cap_actually_caps(tmp_path):
    """没有这条,一个话痨角色会把玩家的收件箱刷满,而每一条都是一次 LLM 花销。"""
    reach = '〔tool:reach_out {"player_id": "p1", "text": "还在吗?"}〕'
    world, _ = _world(tmp_path, *[reach] * 12, interval=1)
    with world:
        _seat_player(world)
        world.config_set("autonomy.max_per_day", 2)

        for _ in range(10):
            world.tick(1)
            _drain(world)          # 等那一轮落地,不是睡一个定数

        mine = [e for e in world.inbox("p1") if e["payload"].get("reason") == "initiative"]
        assert mine, "一次都没主动"
        assert len(mine) <= 2, f"上限是 2 次,实际 {len(mine)} 次"


def test_the_daily_cap_is_tracked_per_character(tmp_path):
    """三个角色各自有自己的额度,不是共用一个池子。"""
    reach = '〔tool:reach_out {"player_id": "p1", "text": "还在吗?"}〕'
    world, _ = _world(tmp_path, *[reach] * 30, interval=1, agents=3)
    with world:
        here = _where(world, "夏")
        for agent_id, brain in world.scheduler.agents.items():
            # 直接落地(不走行程):三个角色本来分散在三处,这条测试要的是
            # "同一地点站着三个人",不是行程本身。
            brain.agent.blackboard.write("loc", here)
            brain.agent.location = here
        world.player_move("p1", here)
        world.config_set("autonomy.max_per_day", 2)

        for _ in range(10):
            world.tick(1)
            _drain(world)          # 等那一轮落地,不是睡一个定数

        mine = [e for e in world.inbox("p1") if e["payload"].get("reason") == "initiative"]
        by_agent: dict[str, int] = {}
        for event in mine:
            by_agent[event["payload"]["agent_id"]] = by_agent.get(event["payload"]["agent_id"], 0) + 1
        assert by_agent, "一次都没主动"
        assert max(by_agent.values()) <= 2, by_agent


def test_she_cannot_reach_out_to_somebody_who_is_not_there(tmp_path):
    """给不在场的人写一条搭话是这个仓库最在意的那类错 —— 当场拒绝,不是照发。"""
    world, _ = _world(tmp_path, '〔tool:reach_out {"player_id": "p9", "text": "喂"}〕')
    with world:
        _seat_player(world)   # 在场的是 p1,不是 p9

        world.tick(1)
        _drain(world)

        assert world.autonomy_stats()["failed"] >= 1
        assert world.inbox("p9") == [] and world.inbox("p1") == []
        assert "不在" in str(world.autonomy_stats()["last"])


def test_she_cannot_reach_out_to_someone_present_but_elsewhere(tmp_path):
    """在场以 TTL 为准,但**还要同地**:玩家在世界里(TTL 没过期)不等于就在她跟前。

    修这条之前 `reach_out` 只查全局在场名单,于是在工作室的角色能"主动去找"一个
    正在咖啡店的玩家 —— 隔着半个地图打招呼,而这个能力的整个意义就是"她走过来"。

    现在这里有两道,**两道都要钉**:菜单不摆(她跟前没人时那一行根本不出现),
    动词自己照旧拒绝。只钉前一道等于把这条闸挪进了提示词,而 `World.act()` 是别的
    进程改这个世界的门 —— 它绕开菜单。
    """
    world, llm = _world(tmp_path, "无")
    with world:
        elsewhere = next(loc for loc in ("home", "cafe", "workshop") if loc != _where(world, "夏"))
        world.player_move("p1", elsewhere)   # p1 在场,但不在夏这儿

        world.tick(1)
        _drain(world)
        assert "reach_out" not in llm.prompts[0], "她跟前一个人都没有,菜单还摆着去找人搭话"

        refused = world.act("夏", "reach_out", {"player_id": "p1", "text": "喂"})
        assert refused["ok"] is False, refused
        assert world.inbox("p1") == []


def test_she_does_not_hail_someone_she_is_already_talking_with(tmp_path):
    """一次真的对局逼出来的:玩家正一句一句跟她聊着,自主轮次让她插了两次话,
    两次都是招呼生客的口气(「你是第一次来吧」)—— 而她刚给这个人做过一杯咖啡。

    搭话是开场白,今天已经说过话就不再是开口了。判据取 contact 水位,所以
    `World.chat` 和 `record_chat_turn` 两扇门进来的对话都算数。
    """
    world, _ = _world(tmp_path, '〔tool:reach_out {"player_id": "p1", "text": "你是第一次来吧?"}〕')
    with world:
        _seat_player(world)
        world.record_chat_turn("夏", "p1", [
            {"role": "user", "content": "老板,还是老样子"},
            {"role": "assistant", "content": "好嘞,一杯拿铁"},
        ])

        world.tick(1)
        _drain(world)

        assert world.inbox("p1") == [], "她把正在聊天的人当成了生客"
        assert "说过话" in str(world.autonomy_stats()["last"])


def test_the_hail_watermark_is_shared_with_the_idle_hail(tmp_path):
    """两条路(闲着时的 `_maybe_hail_player`、自主轮次的 `reach_out`)共用一个水位。

    各记各的话,一天之内玩家会连挨两次搭话 —— 而"一天一次"这条闸看上去还在。
    """
    reach = '〔tool:reach_out {"player_id": "p1", "text": "还在吗?"}〕'
    world, _ = _world(tmp_path, *[reach] * 6, interval=1)
    with world:
        _seat_player(world)
        assert world.scheduler.claim_hail("夏", "p1") == ""   # 闲着时那条先开了口

        for _ in range(4):
            world.tick(1)
            _drain(world)          # 等那一轮落地,不是睡一个定数

        mine = [e for e in world.inbox("p1") if e["payload"].get("reason") == "initiative"]
        assert mine == [], f"同一天里她开了两次口:{mine}"


def test_a_chat_only_capability_is_not_offered_in_a_timed_round(tmp_path):
    """自主轮次里没有"对方"这个人:end_conversation / walk_away 在那儿没有意义,
    给了只会写出一堆关掉空会话的动作。"""
    world, llm = _world(tmp_path, "无")
    with world:
        _seat_player(world)
        world.tick(1)
        _drain(world)

        menu = llm.prompts[0]
        assert "reach_out" in menu and "broadcast" in menu
        for chat_only in ("walk_away", "end_conversation", "delay_reply", "wait_for_user"):
            assert chat_only not in menu, f"{chat_only} 在定时轮次里没有意义"


def test_the_menu_drops_what_cannot_possibly_succeed_this_round(tmp_path):
    """提示词刚说完「这会儿你身边没有别人」,菜单不该还摆着「去找一个在场的人」。

    线上量出来的:一轮真世界 63 次问、0 次动作、5 次失败,五次全是同一句
    `reach_out 没成 —— 这会儿她身边没有人`。摆一个必然被拒的选项不只是浪费一次
    调用 —— 她挑了、被拒了,而这次失败**教不会她任何事**:她当时没有别的选择。
    """
    world, llm = _world(tmp_path, "无")
    with world:
        world.tick(1)   # 一个玩家都没坐下
        _drain(world)

        menu = llm.prompts[0]
        assert "这会儿你身边没有别人" in menu, "前提没成立:这一轮她跟前是有人的"
        assert "reach_out" not in menu, "找不到人的时候还摆着「去找人」"
        assert "broadcast" in menu, "把整张菜单都滤没了 —— 当众说一句不要求跟前有人"


def test_she_can_act_on_the_world_and_not_only_on_people(tmp_path):
    """自主菜单原先只有四样社交能力,三样要跟前有人 —— 于是一个有 116 条规律、
    76 个实体的世界里,她自己决定时能做的事**和世界里能做的事是两张不相交的表**。

    判据照旧是"她的选择在世界里兑现":世界的量真的变了,不是日志里多一行。
    """
    # ⚠️ `interval=2` 是承重的:默认的 `interval=1` 下**第一个 tick 就起一轮**,
    # 而它会当场把那句脚本化的回复用掉、把树照料了 —— 于是下面那行"照料之前的读数"
    # 取到的是**照料之后**的值,`树高 > before` 变成 `3.254 > 3.254`。它靠的是
    # "取读数比那一轮跑得快"这场竞赛,平时赢、满载时输(40 路 busy loop 下八跑两红,
    # 改判据之前之后一样红 —— **这条不是 `_settle` 的病,是同一族的另一处**)。
    # 而它红出来的样子是「她照料了那棵树,而树一点没长」:一句在指控引擎的话。
    world, _ = _world(tmp_path, '〔tool:interact {"target": "tree:harbor_oak", "verb": "tend"}〕',
                      interval=2)
    with world:
        world.set_stocks("agent:夏", {"体力": 100, "手艺": 1.0})
        world._record_and_fan({
            "type": "item_transfer", "who": "夏",
            "payload": {"from": "__town__", "to": "夏", "item_id": "garden_shears", "qty": 1},
        })
        world.tick(1)   # 不到点:一轮都不起
        before = world.stock("tree:harbor_oak", "树高")

        world.tick(1)   # 到点了
        _drain(world)

        assert world.autonomy_stats()["acted"] == 1, world.autonomy_stats()
        assert world.stock("tree:harbor_oak", "树高") > before, "她照料了那棵树,而树一点没长"


def test_a_capability_she_picked_that_is_not_on_this_surface_is_refused(tmp_path):
    world, _ = _world(tmp_path, '〔tool:walk_away {}〕')
    with world:
        _seat_player(world)
        before = _where(world, "夏")

        world.tick(1)
        _drain(world)

        assert world.autonomy_stats()["acted"] == 0
        assert _where(world, "夏") == before
        assert "walk_away" in str(world.autonomy_stats()["last"])


def test_it_is_off_by_default(tmp_path, bare_seed):
    """默认关闭:一个每六小时打一次 LLM 的世界,不该是开箱状态。"""
    # 素配种子:这条验的是**引擎默认值**是关的。内置橱窗替世界点亮了 autonomy
    # (那是产品决定),拿它来验"默认关不关"是在验橱窗的布置(见 conftest)。
    world = open_world_at(str(tmp_path / "w.db"), agents=1, seed_path=bare_seed,
                       force_mock_llm=True)
    llm = DecidingLLM('〔tool:reach_out {"player_id": "p1", "text": "喂"}〕')
    world.chat_service._background_llm = llm
    with world:
        _seat_player(world)
        world.tick(A_DAY)
        # 开关关着时 `_on_autonomy_due` 当场返回,压根不投轮次 —— 于是这里等的是
        # "循环手上什么都没有"。它比睡 0.2 秒**强**:真投了轮次的话,这一等会一直
        # 等到它落地,而不是睡完就下结论。
        _drain(world)

        assert llm.prompts == [], "开关关着还去问她要不要做点什么 = 白花 LLM"
        assert world.inbox("p1") == []
        # 只钉"四个数都是 0、没有最近一次" —— 整份对比会让**加一格诊断**
        # 变成一条红色测试,而这条问的是开关关不关,不是这个 dict 有几个键。
        stats = world.autonomy_stats()
        assert {k: stats[k] for k in ("asked", "acted", "quiet", "failed")} == {
            "asked": 0, "acted": 0, "quiet": 0, "failed": 0,
        }
        assert stats["last"] is None


def test_tools_off_means_autonomy_off(tmp_path):
    """没有能力可挑的轮次是一次白花的调用 —— 两个开关得一起点亮。"""
    world, llm = _world(tmp_path, "无")
    with world:
        world.config_set("chat.tools.enabled", False)
        _seat_player(world)
        world.tick(2)
        _drain(world)   # 同上:等的是"循环手上什么都没有",不是睡够 0.2 秒

        assert llm.prompts == []


def test_a_broken_decision_call_never_takes_down_the_clock(tmp_path):
    """分类器/决定这一路挂了,世界照跑 —— 和叙事、规划、判定同一条降级纪律。"""
    class Broken:
        async def stream(self, messages):
            raise RuntimeError("boom")
            yield ""  # pragma: no cover

        async def complete(self, messages):
            raise RuntimeError("boom")

    world, _ = _world(tmp_path, "无")
    with world:
        world.chat_service._background_llm = Broken()
        _seat_player(world)
        world.tick(3)
        _drain(world)

        assert world.world_time().day >= 0        # 时钟还在
        assert world.autonomy_stats()["acted"] == 0
        assert "失败" in str(world.autonomy_stats()["last"])


def test_a_crash_before_any_character_is_asked_is_recorded_not_swallowed(tmp_path):
    """`asyncio.run_coroutine_threadsafe` 是 fire-and-forget:没人读那个 Future 的话,
    一次异常会无声无息地消失(最多是 GC 时一句没人看的日志)。这条钉住"崩了也要
    留痕迹"——遇到过一次真实事故:能力目录取错了名字,整条链看着通、实际每轮都在
    崩,而 `autonomy_stats()` 在修之前会一直是全零,和"她没什么想做的"完全分不开。
    """
    import anima_world.tools as tools_mod

    world, _ = _world(tmp_path, "无")
    with world:
        _seat_player(world)
        real = tools_mod.tools_for
        tools_mod.tools_for = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            world.tick(1)
            _drain(world)
        finally:
            tools_mod.tools_for = real

        assert world.autonomy_stats()["last"] is not None
        assert "崩溃" in str(world.autonomy_stats()["last"])
        assert world.world_time().day >= 0   # 时钟没被拖垮


# ---- 快进里它不跑,而这件事必须看得见 ---------------------------------------


def test_simulate_says_out_loud_that_it_skips_autonomy(tmp_path):
    """`simulate` 不跑定时轮次 —— 这是有意的,但**不许无声**。

    `_autonomy_hook` 全仓库只在 `World._install_autonomy` 里赋值,而 `simulate`
    直接建 scheduler、从不构造 `World`。于是 `start` / `run` 会问她"此刻想做点什么
    吗",`simulate` 从来不问。快进一年 = 上千次网络往返,而快进的意义就是不等,
    所以不接它是对的。

    但出厂种子把 `autonomy.enabled` 点亮了,用户第一次快进看到的是
    `autonomy_stats()` 全 0 —— **分不清"她不想做"和"根本没跑起来"**,而那个函数
    存在的唯一理由就是把这两件事分开。所以要打一行。
    """
    import subprocess
    import sys

    done = run_cli("simulate",
         "--world-id", "w", "--ticks", "1", "--llm", "mock")
    assert done.returncode == 0, done.stderr
    assert "定时轮次" in done.stdout and "不在快进里跑" in done.stdout
    assert "anima-world run" in done.stdout, "只说不跑不够,得说清去哪儿看得到"


def test_the_notice_stays_quiet_when_autonomy_is_off(tmp_path, bare_seed):
    """开关关着就别提 —— 一句和你无关的警告只会训练你忽略所有警告。"""
    import subprocess
    import sys

    done = run_cli("simulate",
         "--world-id", "w", "--world-file", bare_seed,
         "--ticks", "1", "--llm", "mock")
    assert done.returncode == 0, done.stderr
    assert "定时轮次" not in done.stdout


# ---- 别的进程也得问得出来 ---------------------------------------------------


def test_another_process_can_ask_whether_the_chain_ever_ran(tmp_path):
    """这四个数**必须跨进程看得见**,否则它们的用处正好反过来。

    驱动世界的是 `anima-world run` 那个进程,而问"她到底主动过没有"的人几乎总在
    另一个进程里(CLI、运维台、宿主的健康检查)。计数器只活在内存里的时候,他拿到
    的永远是全 0 —— 一个"这条链从没跑过"的答案,而那恰恰是这四个数要用来**排除**
    的那种情况。诊断本身给出假阴性,比没有诊断更坏。
    """
    world, _ = _world(tmp_path, "无")
    with world:
        _seat_player(world)
        world.tick(1)
        _drain(world)
        assert world.autonomy_stats()["asked"] >= 1, "前提没成立:这一轮没问过她"

    # 另一个 World 实例 = 另一个进程的替身:它自己的内存计数器是全 0。
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as onlooker:
        assert onlooker._autonomy_stats["asked"] == 0, "前提没成立:它不该有内存计数"
        seen = onlooker.autonomy_stats()
        assert seen["asked"] >= 1, (
            f"另一个进程问出来是「一轮都没跑过」,而这个世界明明跑过:{seen}"
        )


def test_doctor_says_whether_she_ever_acted_on_her_own(tmp_path):
    """CLI 上问得到 —— 库里有而 CLI 上没有,对外面等于不存在(FOR-STUDIO 的判据)。"""
    world, _ = _world(tmp_path, "无")
    with world:
        _seat_player(world)
        world.tick(1)
        _drain(world)

    done = run_cli("doctor", "--world-id", "w")
    assert "定时轮次" in done.stdout, done.stdout
    assert "问过" in done.stdout, done.stdout


def test_doctor_tells_a_dead_chain_from_a_fresh_restart(tmp_path):
    """判据是"离上一轮过去多久了",不是那四个数。

    四个数只说得清"本次开机以来",而重启之后库里躺着的还是上一次开机那一行 ——
    光看数的话,"刚重启、新的一轮还没到"和"这条链死了"长得一模一样。而那正好
    就是这一节要分开的两件事,只是换了个地方犯同一个错。
    """
    world, _ = _world(tmp_path, "无")
    with world:
        _seat_player(world)
        world.tick(1)
        _drain(world)
        assert world.autonomy_stats().get("last_tick") is not None, "没记下上一轮在第几 tick"

    fresh = run_cli("doctor", "--world-id", "w")
    assert "隔了" not in fresh.stdout, f"刚跑过就报「太久没跑」了:{fresh.stdout}"

    # 时钟往前跳过两个间隔,而没有任何一轮跟上 —— 这才是"这条链死了"的样子。
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as onlooker:
        interval = int(onlooker.config_get("autonomy.interval_ticks", 72) or 72)
        last = int(onlooker.autonomy_stats()["last_tick"])
        onlooker.scheduler.redis.set(
            f"anima:{onlooker.scheduler.world_id}:clock", last + interval * 3
        )

    dead = run_cli("doctor", "--world-id", "w")
    assert "隔了" in dead.stdout, f"这条链停了三个间隔,doctor 一声不吭:{dead.stdout}"


def test_a_failure_keeps_its_own_line_after_later_rounds_go_quiet(tmp_path):
    """一次失败的理由不许被后面的沉默盖掉。

    `last` 每轮都被改写,而"什么都不做"是这一层的**常态** —— 一次失败后面跟上
    两轮沉默,那句理由就没了,只剩计数器上一个 `failed: 1`:你知道有一次没成,
    永远不知道是什么没成。而这一层的全部意义就是把失败的方式分开。

    真世界上撞见的:一个跑了 18 天的世界报 `failed: 1`,而库里、日志里都找不到
    那一次到底是什么。
    """
    world, _ = _world(tmp_path, '〔tool:reach_out {"player_id": "谁也不是"}〕', "无", "无")
    with world:
        _seat_player(world)
        world.tick(1)
        _drain(world)
        assert world.autonomy_stats()["failed"] >= 1, "前提没成立:这一轮没失败"
        failure = str(world.autonomy_stats()["last_failure"])
        assert failure and failure != "None", "失败没有留下自己那一格"

        # 再来两轮沉默 —— `last` 会被改写,`last_failure` 不该跟着没。
        world.tick(2)
        _drain(world)
        assert str(world.autonomy_stats()["last_failure"]) == failure, (
            "后面的沉默把那次失败的理由盖掉了"
        )


# ---- 判据自己也得站得住 -----------------------------------------------------
#
# 这一节测的不是引擎,是上面那个 `_settle`。理由是它骗过一次:2026-08-25 四仓测试
# 并行跑那趟,`test_a_broken_decision_call_never_takes_down_the_clock` FAILED,而
# 同一份代码单跑 0.17 秒绿、整文件 24 项绿、独占重跑全绿 —— 红的是判据,不是代码。
# **而一条抗故障用例失败,和真的把时钟拖垮,在屏幕上逐字相同。**
#
# ⚠️ 这两条用的 `_GatedLLM` **搬到文件开头去了**(和 `DecidingLLM` 并排):
# 2026-08-25 同日 `test_the_clock_never_waits_for_the_network` 也改成用它,
# 于是它不再是这一节的私产。


def test_settling_waits_for_the_round_to_land_not_for_a_number(tmp_path):
    """判据是「那一轮落地了」,不是「等够了几秒」。

    把那一轮卡在闸上:闸开之前 `_settle` **不许回来**。挂钟判据做不到这条 ——
    它到点就回来,把一个还没落地的空结论交给调用方的断言,而调用方的断言
    写着「定时轮次没走通」。

    ⚠️ 这条里的 0.3 秒只做**下界**用(证明它没有提前回来)。机器越忙它越容易成立,
    所以这个数不会让这条测试假红 —— 和它替掉的那个 5 秒上界正好相反。
    """
    module = sys.modules[__name__]
    original = module._ROUND_GUARD_SECONDS
    module._ROUND_GUARD_SECONDS = max(original, 30.0)   # 这条测的是判据本身,不吃旋钮
    gate = threading.Event()
    world, _ = _world(tmp_path, "无")
    llm = _GatedLLM(gate)
    try:
        world.chat_service._background_llm = llm
        _seat_player(world)
        world.tick(1)
        assert llm.entered.wait(30), "前提没成立:这一轮压根没排上那条循环"

        landed: list[object] = []
        waiter = threading.Thread(
            target=lambda: landed.append(_settle(world, lambda: llm.threads)),
            daemon=True,
        )
        waiter.start()
        waiter.join(0.3)
        assert waiter.is_alive(), "闸还关着、那一轮还没落地,而 _settle 已经回来了"

        gate.set()
        waiter.join(60)   # 只防挂死,不参与判断
        assert not waiter.is_alive(), "闸开了它还没回来"
        assert landed and landed[0], f"落地了却交回一个空结论:{landed}"
    finally:
        gate.set()
        module._ROUND_GUARD_SECONDS = original
        world.close()


def test_a_tripped_guard_says_it_is_the_machine_not_the_engine(tmp_path):
    """兜底闸门跳了,说的必须是「这台机器」,不是「引擎没走通」。

    这正是那 5 秒挂钟最贵的地方:等不到就返回一个空 `predicate()`,于是红出来的是
    **调用方自己的断言** ——「定时轮次没走通:{...}」,一句在指控引擎的话,而真相是
    这台机器那半秒没排上 CPU。现在等不到就当场说清是谁的问题,并且**不给结论**。
    """
    module = sys.modules[__name__]
    original = module._ROUND_GUARD_SECONDS
    gate = threading.Event()
    world, _ = _world(tmp_path, "无")
    llm = _GatedLLM(gate)
    try:
        world.chat_service._background_llm = llm
        _seat_player(world)
        world.tick(1)
        assert llm.entered.wait(30), "前提没成立:这一轮压根没排上那条循环"

        module._ROUND_GUARD_SECONDS = 0.05   # 闸还关着,所以它必跳
        with pytest.raises(AssertionError, match="不是引擎的结论"):
            _settle(world, lambda: llm.threads)

        # 闸门跳了**不等于那一轮丢了** —— 这也是它不配当判据的理由:
        # 它量的是这台机器的手速,不是世界做没做成那件事。
        module._ROUND_GUARD_SECONDS = max(original, 30.0)
        gate.set()
        assert _settle(world, lambda: llm.threads), "闸门跳了,而那一轮其实照样落了地"
    finally:
        gate.set()
        module._ROUND_GUARD_SECONDS = original
        world.close()
