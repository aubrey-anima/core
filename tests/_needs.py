"""按一条需求到某个值 —— **写量表,不写黑板**(3.8.0 之后)。

⚠️ **这一层从 3.8.0 起翻过来了。** 需求的值搬进了量表(`stock:agent:<id>` 里的
`needs.energy` 这几个键,和树高、灵力同一张表),黑板上那几格 `need.*` 变成了
**派生的**:调度器每 tick 从量表折一次写上去。

于是 `blackboard.write("need.hunger", 0.05)` 这种老写法**下一 tick 就被盖回去** ——
而盖回去的样子是"我明明改了,世界不认",没有一处报错。这个小助手写两处:
量表(真值)与黑板(免得这一 tick 之内还有人读旧的),测试里两处都要对得上。
"""
from __future__ import annotations

from anima_world.needs import PLUGIN_ID


def set_need(world, agent_id: str, need: str, value: float) -> None:
    scheduler = world.scheduler
    scheduler.stock_store.set_many(
        scheduler.stock_owner_of(agent_id),
        {f"{PLUGIN_ID}.{need}": float(value)}, tick=int(scheduler.clock),
    )
    brain = scheduler.agents.get(agent_id)
    if brain is not None:
        brain.agent.blackboard.write(f"need.{need}", float(value))
