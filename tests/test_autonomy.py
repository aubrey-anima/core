"""定时轮次:没人跟她说话的时候,她自己决定要不要做点什么。

聊天里的能力只在**玩家开口之后**才有机会被选中,于是"她能主动"这件事此前全靠人来
触发。这些测试盯的是那条触发链真的通:时钟到点 → 快照 → 决定 → **能力真的执行**
→ 玩家那边看得见,而且

- 时钟**永不等网络**(这条是这个引擎最老的不变量,也是最容易在加挂钩时破掉的)
- 默认什么都不做、默认整个关着
- 有上限,而且"被问"不算"用掉额度"
- 找一个不在场的人是空动作,当场拒绝
"""
from __future__ import annotations

from _worldfile import open_world_at, run_cli

import threading
import time

import pytest

from anima_world.api import World

A_DAY = 288


class DecidingLLM:
    """按脚本替她做决定,并记下**是哪条线程**在调 LLM。"""

    def __init__(self, *replies: str, delay: float = 0.0) -> None:
        self.replies = list(replies)
        self.delay = delay
        self.threads: list[str] = []
        self.prompts: list[str] = []

    def _next(self, messages) -> str:
        self.threads.append(threading.current_thread().name)
        self.prompts.append("\n".join(m["content"] for m in messages))
        if self.delay:
            time.sleep(self.delay)
        return self.replies.pop(0) if self.replies else "无"

    async def stream(self, messages):
        yield self._next(messages)

    async def complete(self, messages) -> str:
        return self._next(messages)


def _world(tmp_path, *replies: str, delay: float = 0.0, interval: int = 1,
           enabled: bool = True, agents: int = 1) -> tuple[World, DecidingLLM]:
    """默认**一个角色**的世界:脚本化的回复是全局按调用顺序弹出的一个列表,
    多角色时哪句话落在谁身上取决于处理顺序 —— 单角色让每条测试的断言不用猜顺序。
    需要验证"每个角色各自有自己的额度"这类跨角色行为时显式传 `agents=3`。
    """
    world = open_world_at(str(tmp_path / "w.db"), agents=agents, force_mock_llm=True)
    llm = DecidingLLM(*replies, delay=delay)
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


def _settle(world: World, predicate, timeout: float = 5.0):
    """等那条跑在世界自己循环上的轮次落地。轮询而不是 sleep 一个定数。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
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

    LLM 调用故意慢半秒:`tick()` 必须立刻返回,而且那次调用必须发生在**别的线程**上。
    """
    world, llm = _world(tmp_path, "无", delay=0.5)
    with world:
        _seat_player(world)

        ticking = threading.current_thread().name
        started = time.monotonic()
        world.tick(1)
        elapsed = time.monotonic() - started

        assert elapsed < 0.2, f"tick 被 LLM 拖住了({elapsed:.2f}s)"
        _settle(world, lambda: llm.threads)
        assert llm.threads and all(name != ticking for name in llm.threads), llm.threads


def test_doing_nothing_is_the_default_and_leaves_no_trace(tmp_path):
    """什么都不做是常态 —— 不发**自主轮次自己的**事件、不占额度。

    每六小时一条"她想了想,没做"的事件会把日志灌满而不带一点信息。世界本身照常
    发它自己的事件(行为树的状态切换之类),这条只盯 autonomy 有没有多插一条。
    """
    world, _ = _world(tmp_path, "无", "无", "无")
    with world:
        _seat_player(world)

        world.tick(3)
        _settle(world, lambda: world.autonomy_stats()["quiet"])

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
            if world.autonomy_stats()["acted"]:
                break
            time.sleep(0.05)
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
            time.sleep(0.05)
        _settle(world, lambda: world.autonomy_stats()["acted"] >= 2)

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
            time.sleep(0.05)
        _settle(world, lambda: world.autonomy_stats()["acted"] >= 2)

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
        _settle(world, lambda: world.autonomy_stats()["failed"])

        assert world.autonomy_stats()["failed"] >= 1
        assert world.inbox("p9") == [] and world.inbox("p1") == []
        assert "不在" in str(world.autonomy_stats()["last"])


def test_she_cannot_reach_out_to_someone_present_but_elsewhere(tmp_path):
    """在场以 TTL 为准,但**还要同地**:玩家在世界里(TTL 没过期)不等于就在她跟前。

    修这条之前 `reach_out` 只查全局在场名单,于是在工作室的角色能"主动去找"一个
    正在咖啡店的玩家 —— 隔着半个地图打招呼,而这个能力的整个意义就是"她走过来"。
    """
    world, _ = _world(tmp_path, '〔tool:reach_out {"player_id": "p1", "text": "喂"}〕')
    with world:
        elsewhere = next(loc for loc in ("home", "cafe", "workshop") if loc != _where(world, "夏"))
        world.player_move("p1", elsewhere)   # p1 在场,但不在夏这儿

        world.tick(1)
        _settle(world, lambda: world.autonomy_stats()["failed"])

        assert world.autonomy_stats()["failed"] >= 1
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
        _settle(world, lambda: world.autonomy_stats()["failed"])

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
            time.sleep(0.05)
        _settle(world, lambda: world.autonomy_stats()["failed"])

        mine = [e for e in world.inbox("p1") if e["payload"].get("reason") == "initiative"]
        assert mine == [], f"同一天里她开了两次口:{mine}"


def test_a_chat_only_capability_is_not_offered_in_a_timed_round(tmp_path):
    """自主轮次里没有"对方"这个人:end_conversation / walk_away 在那儿没有意义,
    给了只会写出一堆关掉空会话的动作。"""
    world, llm = _world(tmp_path, "无")
    with world:
        _seat_player(world)
        world.tick(1)
        _settle(world, lambda: llm.prompts)

        menu = llm.prompts[0]
        assert "reach_out" in menu and "broadcast" in menu
        for chat_only in ("walk_away", "end_conversation", "delay_reply", "wait_for_user"):
            assert chat_only not in menu, f"{chat_only} 在定时轮次里没有意义"


def test_a_capability_she_picked_that_is_not_on_this_surface_is_refused(tmp_path):
    world, _ = _world(tmp_path, '〔tool:walk_away {}〕')
    with world:
        _seat_player(world)
        before = _where(world, "夏")

        world.tick(1)
        _settle(world, lambda: world.autonomy_stats()["quiet"])

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
        time.sleep(0.2)

        assert llm.prompts == [], "开关关着还去问她要不要做点什么 = 白花 LLM"
        assert world.inbox("p1") == []
        assert world.autonomy_stats() == {
            "asked": 0, "acted": 0, "quiet": 0, "failed": 0, "last": None,
        }


def test_tools_off_means_autonomy_off(tmp_path):
    """没有能力可挑的轮次是一次白花的调用 —— 两个开关得一起点亮。"""
    world, llm = _world(tmp_path, "无")
    with world:
        world.config_set("chat.tools.enabled", False)
        _seat_player(world)
        world.tick(2)
        time.sleep(0.2)

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
        _settle(world, lambda: world.autonomy_stats()["asked"] >= 1)

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
            _settle(world, lambda: world.autonomy_stats()["last"])
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
        _settle(world, lambda: world.autonomy_stats()["asked"])
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
        _settle(world, lambda: world.autonomy_stats()["asked"])

    done = run_cli("doctor", "--world-id", "w")
    assert "定时轮次" in done.stdout, done.stdout
    assert "问过" in done.stdout, done.stdout
