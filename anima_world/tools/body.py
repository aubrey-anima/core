"""过日子的动作:走、吃、干活、睡、找人说话。

这些一直都在,但**不是动词** —— 它们住在 `bt_actions` 表里,只有行为树够得着。
于是同一个人有两套能力,取决于谁在触发她:聊天里她能"走开",排班表里没有这个词;
排班表里她会"走到咖啡店",而她自己决定时挑不了。这个割裂此前没人注意到,因为两边
各自都能跑。

`bt_actions` 其实也不是动词表 —— 它是**已经绑好参数的调用表**:

    go_to_cafe  = walk(location="cafe")
    chat_with_夏 = chat(target="夏")

所以那 20 行其实是 6 个动词的若干绑定,而 `chat_with_*` 那几行会随人口线性增长。
这个模块把那 6 个动词还原成动词,`bt_actions` 退回它本来的身份:绑定表。

**实现一行都不重复。** 每个动词都委托 `Scheduler.emit_action` —— 行为树走的就是它。
于是"排班让她走"和"她自己决定走"在世界里是同一件事:一样发 `travel` /
`location_join`,一样要走路花时间,一样在途中不可打断。另写一份"外部版本的走路"
迟早和行为树那份分叉,而分叉的那天没人会发现。

`emit_action` 返回 `False` 表示**世界说"还不行"**(她在半路上、要找的人不在这儿)。
那不是失败,是"这一下没成";照实报,不假装成功。
"""

from __future__ import annotations

import logging
from typing import Any

from anima_world.tools.base import BODY, ToolCallError, ToolContext, ToolResult, tool

logger = logging.getLogger(__name__)


def _do(ctx: ToolContext, kind: str, params: dict[str, Any]) -> ToolResult:
    """把一个动作交给行为树走的那条路。"""
    took = ctx.runtime.do_action(ctx.agent_id, kind, dict(params))
    if not took:
        # 世界说"还不行"——她在半路上,或者要找的人不在这儿。不是失败,是没成。
        return ToolResult(
            ok=False,
            error="世界这会儿不接这个动作(她在赶路,或者对方不在这儿)",
            detail={"kind": kind, "params": dict(params), "took": False},
        )
    return ToolResult(detail={"kind": kind, "params": dict(params), "took": True})


@tool(
    id="walk",
    writes=("events:travel",),
    kind="walk",
    description="走到某个地方去。**要花时间**,途中她在路上(不是瞬移)",
    params={"location": {"type": "string", "description": "去哪儿", "required": True}},
    surfaces=(BODY,),
)
def walk(ctx: ToolContext, params: dict) -> ToolResult:
    where = str(params.get("location") or "").strip()
    if not where:
        raise ToolCallError("没说去哪儿")
    known = set(ctx.runtime.point_ids())
    if where not in known:
        # 在场语义由引擎守住:模型编一个不存在的地名是常事,不该变成一次静默的空动作
        raise ToolCallError(f"没有 {where} 这个地方;有的是 {sorted(known)}")
    return _do(ctx, "walk", {"location": where})


@tool(
    id="work",
    writes=("events:state_change",),
    kind="work",
    description="在此刻所在的地方干活",
    params={},
    surfaces=(BODY,),
)
def work(ctx: ToolContext, params: dict) -> ToolResult:
    here = ctx.runtime.agent_location(ctx.agent_id)
    return _do(ctx, "work", {"location": here} if here else {})


@tool(
    id="eat",
    writes=("events:agent_action", "events:item_consume"),
    kind="eat",
    description="吃东西。**付钱是副作用**:没货没钱就降级成吃随身干粮,不会卡住",
    params={},
    surfaces=(BODY,),
)
def eat(ctx: ToolContext, params: dict) -> ToolResult:
    return _do(ctx, "eat", {})


@tool(
    id="sleep",
    writes=("events:state_change",),
    kind="sleep",
    description="睡觉",
    params={},
    surfaces=(BODY,),
)
def sleep(ctx: ToolContext, params: dict) -> ToolResult:
    return _do(ctx, "sleep", {})


@tool(
    id="talk_to",
    writes=("events:agent_action", "events:memory_seed", "memories"),
    kind="chat",
    description="跟同在这儿的另一个角色搭话(对方不在这儿就不成)",
    params={"target": {"type": "string", "description": "跟谁", "required": True}},
    surfaces=(BODY,),
)
def talk_to(ctx: ToolContext, params: dict) -> ToolResult:
    who = str(params.get("target") or "").strip()
    if not who:
        raise ToolCallError("没说跟谁")
    if who == ctx.agent_id:
        raise ToolCallError("她不能跟自己搭话")
    if who not in ctx.runtime.agent_ids():
        raise ToolCallError(f"这个世界里没有 {who}")
    return _do(ctx, "chat", {"target": who})


# 引擎里的"闲着"有两种,不是一种 —— 照实登记,别为了表好看合并:
# `idle_wander` 是行为树的兜底(什么也不特意做),`idle_social` 是"找个人待着"。
# 合并成一个 `idle` 会让她再也表达不了"我想找人",而那是需求系统里 social 那条曲线
# 唯一的出口。
@tool(
    id="wander",
    writes=("events:agent_action",),
    kind="idle_wander",
    description="待着,什么也不特意做",
    params={},
    surfaces=(BODY,),
)
def wander(ctx: ToolContext, params: dict) -> ToolResult:
    return _do(ctx, "idle_wander", {})


@tool(
    id="seek_company",
    writes=("events:agent_action",),
    kind="idle_social",
    description="想找个人待着 —— 不指定是谁,看这会儿谁在",
    params={},
    surfaces=(BODY,),
)
def seek_company(ctx: ToolContext, params: dict) -> ToolResult:
    return _do(ctx, "idle_social", {})
