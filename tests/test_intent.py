"""`World.intend()`:告诉她接下来打算做什么,世界替她走完脚步。

`act()` 是"现在做这一件事",`intend()` 是"接下来这几步"。区别不是语法糖:一个 LLM
驱动的进程**不该一步一次网络往返地编排走路** —— 那又贵又编得烂。她该说"先走到咖啡店,
然后干活",剩下交给世界。

盯四件事,第二条是全部设计的关键:

1. 一串动作按序走完,做完一步少一条
2. **身体能打断它** —— 调用是"设定意图",不是"执行到底"。否则紧急带被架空,
   "饿死也要把店开了"就回来了(`_wrap_with_needs_band` 花力气解掉的就是它)
3. **队列空的世界行为逐字不变** —— 这一层是纯加法
4. **一步真生效了才往前走一格** —— 在途时会被重挑很多次,挑一次弹一次会把后面
   几步一起吃掉
"""
from __future__ import annotations

from _worldfile import open_world_at

import pytest

from anima_world.api import World
from anima_world.needs import URGENT


@pytest.fixture()
def world(tmp_path):
    w = open_world_at(str(tmp_path / "world.db"), force_mock_llm=True)
    w.tick(50)
    _settle(w, sorted(w.scheduler.agents)[0])
    yield w
    w.close()


def _settle(world: World, agent: str, limit: int = 288) -> None:
    """等到她手上没有正在赶的路 —— 这几条测试都得从"她此刻能起程"开始。

    在途时 `emit_action("walk")` 会被"她在赶路"挡回来,于是队列的第一步永远不生效、
    永远不往前走一格,而这几条要验的根本不是那道闸。更坏的是 `_elsewhere()` 拿的是
    她**此刻站在哪**:人在路上时那是她刚离开的地方,于是"走去别处"算出来的目的地
    正是她自己已经在走的那一个,断言便再也分不清"世界没执行"和"她本来就要去那儿"。

    从前这里不必等,只是因为所有世界都从午夜起步、跑 50 tick 恰好是凌晨没人动 ——
    而几点开门如今是这个世界作者的意见(`world.start_time`)。
    """
    for _ in range(limit):
        if agent not in world.scheduler._transit:
            return
        world.tick(1)
    raise AssertionError(f"{agent} 整整一个世界日都在路上,这条测试没法起头")


def _an_agent(world: World) -> str:
    return sorted(world.scheduler.agents)[0]


def _elsewhere(world: World, agent: str) -> str:
    here = world._tool_runtime.agent_location(agent)
    return next(p for p in world._tool_runtime.point_ids() if p != here)


def _run_until_done(world: World, agent: str, limit: int = 80) -> int:
    for spent in range(limit):
        world.tick(1)
        if not world.intent(agent):
            return spent
    return limit


def test_a_multi_step_intent_runs_in_order_and_drains(world):
    agent = _an_agent(world)
    target = _elsewhere(world, agent)

    queued = world.intend(agent, [
        {"verb": "walk", "params": {"location": target}},
        {"verb": "work"},
    ])
    assert queued["queued"] == 2

    _run_until_done(world, agent)
    assert world._tool_runtime.agent_location(agent) == target
    assert world.intent(agent) == []


def test_the_body_can_interrupt_and_she_resumes_after(world):
    """**这条是全部设计的关键。**

    她刚决定的事排在身体之下 —— 饿到紧急线就先去吃,吃完回来接着走。队列不许被
    这次打断吃掉:被打断是特性,不是失败。
    """
    world.config_set("needs.enabled", True)
    agent = _an_agent(world)
    blackboard = world.scheduler.agents[agent].agent.blackboard
    world.intend(agent, [
        {"verb": "walk", "params": {"location": _elsewhere(world, agent)}},
        {"verb": "work"},
    ])
    world.tick(2)
    remaining = len(world.intent(agent))
    assert remaining, "还没走完就空了,这条测试没在验它想验的"

    blackboard.write("need.hunger", URGENT - 0.05)
    world.tick(1)

    assert blackboard.read("_selected_action_id") == "eat", (
        "饿到紧急线了还在照原计划走 —— 紧急带被意图架空了"
    )
    assert len(world.intent(agent)) == remaining, "被打断时队列被吃掉了"

    blackboard.write("need.hunger", 0.9)
    _run_until_done(world, agent)
    assert world.intent(agent) == [], "吃完之后没接着走完"


def test_an_empty_queue_leaves_the_world_byte_for_byte_unchanged(world):
    """没人调用 `intend()` 的世界必须和以前一模一样 —— 这一层是纯加法。"""
    agent = _an_agent(world)
    assert world.intent(agent) == []
    blackboard = world.scheduler.agents[agent].agent.blackboard
    world.tick(20)
    assert blackboard.read("_selected_action_id") != "follow_intent", (
        "队列是空的,却选中了意图节点"
    )


def test_the_queue_advances_only_when_a_step_actually_took_effect(world):
    """走路要花很多 tick,期间意图节点被重挑很多次 —— 不许挑一次弹一次。"""
    agent = _an_agent(world)
    world.intend(agent, [
        {"verb": "walk", "params": {"location": _elsewhere(world, agent)}},
        {"verb": "work"},
    ])
    world.tick(1)          # 起程:第一步生效,弹掉
    assert len(world.intent(agent)) == 1

    in_transit_ticks = 0
    for _ in range(40):
        if not world.scheduler._transit.get(agent):
            break
        # 此刻确凿还在路上,所以第二步不可能已经执行 —— 队列必须还是 1
        assert len(world.intent(agent)) == 1, (
            f"在途第 {in_transit_ticks} tick 队列就少了 —— 挑一次弹一次,"
            "后面几步被吃掉了"
        )
        world.tick(1)
        in_transit_ticks += 1
    assert in_transit_ticks, "走路没花时间,这条测试没在验它想验的"


def test_a_verb_that_is_not_a_daily_action_is_refused(world):
    """`intend()` 只收过日子的动作 —— 广播/静音那些是 `act()` 的事。"""
    agent = _an_agent(world)
    with pytest.raises(ValueError) as caught:
        world.intend(agent, [{"verb": "broadcast", "params": {"text": "喂"}}])
    assert "walk" in str(caught.value), "拒了但没说可用的是什么"

    with pytest.raises(KeyError):
        world.intend("并不存在的人", [{"verb": "walk", "params": {"location": "cafe"}}])


def test_intending_nothing_cancels_what_she_was_going_to_do(world):
    agent = _an_agent(world)
    world.intend(agent, [{"verb": "walk", "params": {"location": _elsewhere(world, agent)}}])
    assert world.intent(agent)
    assert world.intend(agent, None)["queued"] == 0
    assert world.intent(agent) == []
