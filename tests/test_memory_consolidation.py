"""夜间固化(R5):它接管什么、不许悄悄废掉什么。

这一层最容易犯的错不是算错,是**两个机制里一个安静地废掉另一个**。第一版的固化
用"水位 > 0"当反思的门,并把水位清成 0 —— 于是:

- 每个角色每世界日都反思一次(每次一趟 LLM,而橱窗世界点亮了这个开关);
- 攒了半天还没到阈值的水位被抹掉,`memory.reflection_threshold` 那条路
  **在开着固化的世界里等于被停掉** —— 而读数上两边都对。

修法是"一个触发条件,不是两个":阈值一个字没变,变的只是它在**哪一刻兑现** ——
白天只攒不发,越过阈值的那一次留到夜里(这正是 R5 存在的理由:反思是一次 LLM
调用,别跟她正在说的话抢线程)。
"""
from __future__ import annotations

import pytest


@pytest.fixture
def consolidating(open_world, bare_seed):
    """一个开了固化、接着反思器的毛坯世界。"""
    world = open_world(world_file=bare_seed)
    if world.scheduler.reflector is None or world.scheduler._judge_pool is None:
        pytest.skip("这个世界没接反思器")
    world.config_set("memory.consolidation.enabled", True)
    world.config_set("memory.reflection_threshold", 3.0)
    return world


def _agent(world) -> str:
    return next(iter(world.scheduler.agents))


def _big_things_happened(world, who: str, n: int, importance: float = 1.0) -> None:
    """走真路让她**真的**记下 n 件事(`memory_seed` 事件 → 记忆行 + 水位)。

    直接调 `_note_memory_written` 只推水位、不留记忆,而反思是拿她最近的记忆当
    上下文的 —— 那样测出来的"反思了一次"是一个空转的读数。
    """
    for i in range(n):
        world.scheduler._record_event({
            "type": "memory_seed", "who": who,
            "payload": {"agent_id": who, "kind": "obs",
                        "summary": f"第{i}件大事发生了", "importance": importance},
        })


def test_a_day_that_did_not_accumulate_enough_does_not_reflect(consolidating):
    """攒不够就不反思,而且**水位留着** —— 抹掉它就是把阈值那条路停掉。"""
    world = consolidating
    who = _agent(world)
    with world.scheduler._lock:
        world.scheduler._note_memory_written(who, 0.1, "obs")

    out = world.scheduler.consolidate_memories(now_tick=288)
    assert out["reflections"] == 0, (
        "0.1 的水位不该换来一次 LLM 调用 —— 「今天写过任何一条记忆的人今晚都反思」"
        "不是任何人做过的决定"
    )
    assert world.scheduler._reflection_watermark[who] == pytest.approx(0.1), (
        "没攒够的水位要留着接着攒;抹成 0 等于让她永远到不了阈值"
    )
    assert out["agents"] >= 1 and out["decayed"] >= 1, "衰减照做"


def test_the_threshold_is_cashed_out_at_night(consolidating):
    """越过阈值的那一次落在夜里 —— 兑现了,只是不在白天。"""
    world = consolidating
    who = _agent(world)
    _big_things_happened(world, who, 4)
    assert world.scheduler._reflection_watermark[who] == pytest.approx(4.0), (
        "白天只攒不发:开着固化的世界里跨过阈值不当场发起反思"
    )

    out = world.scheduler.consolidate_memories(now_tick=288)
    assert out["reflections"] == 1
    assert world.scheduler._reflection_watermark[who] == pytest.approx(0.0)
    world.scheduler.stop(wait=True)     # 排干判官池,反思落地
    insights = world.reflections(who)
    assert insights, "夜里那一次必须真的产出洞察,不只是一个 +1 的读数"


def test_one_night_is_one_reflection_not_a_storm(consolidating):
    """一天攒了三倍的阈值,夜里也只反思一次 —— 洞察催生洞察是风暴,不是思考。"""
    world = consolidating
    who = _agent(world)
    _big_things_happened(world, who, 10)

    assert world.scheduler.consolidate_memories(now_tick=288)["reflections"] == 1


def test_reflections_are_only_counted_when_the_work_was_handed_off(consolidating):
    """没有线程池时读数必须是 0 —— "反思了 3 次"的假读数比没有读数更坏。"""
    world = consolidating
    who = _agent(world)
    _big_things_happened(world, who, 4)
    world.scheduler._judge_pool = None

    assert world.scheduler.consolidate_memories(now_tick=288)["reflections"] == 0


def test_a_store_without_decay_pass_still_prunes_and_reflects(consolidating):
    """缺 `decay_pass` 的后端上,清扫与反思照做。

    老代码把这个判断和 `continue` 绑在一起(而且在循环里,每个角色一条 warning),
    于是一个缺方法的 store 会连带把 prune / reflect 一起跳过 —— 三件事里少了两件,
    而返回的 `agents` 照样在涨。
    """
    world = consolidating
    who = _agent(world)
    real = world.scheduler.memory_store

    class NoDecay:
        def __getattr__(self, name):
            if name == "decay_pass":
                raise AttributeError(name)
            return getattr(real, name)

    _big_things_happened(world, who, 4)
    world.scheduler.memory_store = NoDecay()
    try:
        out = world.scheduler.consolidate_memories(now_tick=288)
    finally:
        world.scheduler.memory_store = real

    assert out["decayed"] == 0, "没有 decay_pass 就一次都没衰减 —— 别报一个假数"
    assert out["agents"] >= 1
    assert out["reflections"] == 1, "反思不该被一个缺掉的衰减方法连带跳过"


def test_the_day_rollover_does_not_decay_twice(consolidating):
    """固化**接管**日切的衰减,不是叠加 —— `decay_pass` 不幂等,跑两遍是平方。"""
    world = consolidating
    calls: list[str] = []
    real = world.scheduler.memory_store

    class Counting:
        def decay_pass(self, agent_id, now_tick, ticks_per_day=288):
            calls.append(agent_id)
            return real.decay_pass(agent_id, now_tick, ticks_per_day)

        def __getattr__(self, name):
            return getattr(real, name)

    world.scheduler.memory_store = Counting()
    try:
        world.scheduler._on_day_rollover()
    finally:
        world.scheduler.memory_store = real

    assert len(calls) == len(set(calls)), f"每个角色一天只许衰减一遍,实际 {calls}"


def test_with_consolidation_off_nothing_changes(open_world, bare_seed):
    """开关关着时逐位如旧:白天跨过阈值当场反思。"""
    world = open_world(world_file=bare_seed)
    if world.scheduler.reflector is None or world.scheduler._judge_pool is None:
        pytest.skip("这个世界没接反思器")
    who = _agent(world)
    world.config_set("memory.reflection_threshold", 2.0)
    _big_things_happened(world, who, 2)

    assert world.scheduler._reflection_watermark[who] == pytest.approx(0.0), (
        "默认(不开固化)时,越过阈值当场发起反思并把水位清零"
    )
    world.scheduler.stop(wait=True)
    assert world.reflections(who), "这一条是引擎默认值的样子,不许被 R5 改掉"
