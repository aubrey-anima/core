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

from anima_world.tools.base import (
    AUTONOMY, BODY, CHAT, PLAYER, ToolCallError, ToolContext, ToolResult,
    resolve_location, tool,
)

logger = logging.getLogger(__name__)


def _here(ctx: ToolContext) -> str:
    """施动者这会儿在哪。**不是"她在哪"** —— 玩家点"在这儿干活"说的是他自己站的地方。"""
    if ctx.is_player:
        return ctx.runtime.player_location(ctx.player_id)
    return ctx.runtime.agent_location(ctx.agent_id)


def _do(ctx: ToolContext, kind: str, params: dict[str, Any]) -> ToolResult:
    """把一个动作交给行为树走的那条路;玩家走的是并排的那条。

    **动词和它的后果是同一份**,分叉只在执行器:她有行为树,人没有。
    """
    if ctx.is_player:
        took = ctx.runtime.player_do_action(ctx.player_id, kind, dict(params))
        who = "你"
    else:
        took = ctx.runtime.do_action(ctx.agent_id, kind, dict(params))
        who = "她"
    if not took:
        # 世界说"还不行"——他在半路上,或者要找的人不在这儿。不是失败,是没成。
        return ToolResult(
            ok=False,
            error=f"世界这会儿不接这个动作({who}在赶路,或者对方不在这儿)",
            detail={"kind": kind, "params": dict(params), "took": False},
        )
    return ToolResult(detail={"kind": kind, "params": dict(params), "took": True})


@tool(
    id="walk",
    writes=("events:travel",),
    kind="walk",
    # 这一条**三个面共用**(CHAT / BODY / PLAYER),所以主语不能写「她」——
    # PLAYER 面上它是玩家自己那个按钮的说明,一句「途中她在路上」说的是别人。
    # 同一份清单两个读者那条的又一处,只是这回漏在文案上。
    # ⚠️ **强调不许写成 `**…**`**(3.6.0 第六轮 2026-08-20 改):这一条上了 PLAYER
    # 面,那四个星号会**原样印在玩家的按钮说明上**;她的提示词里也一样是噪音。
    # 记号统一用「」(`Scheduler._named` 同一个:终端、提示词、玩家屏幕上都长得一样)。
    description="走到某个地方去。「要花时间」,途中人在路上(不是瞬移)",
    params={"location": {"type": "string", "description": "去哪儿", "required": True}},
    surfaces=(CHAT, BODY, PLAYER),
)
def walk(ctx: ToolContext, params: dict) -> ToolResult:
    """走到某个地方去 —— **聊天里也够得着**。

    这个模块开头那段说的割裂("聊天里她能走开,排班表里没有这个词;排班表里她会走到
    咖啡店,而她自己决定时挑不了")在 `walk` 上一直没合拢:它给了 BODY 和 PLAYER,
    独独没给 CHAT。于是最显眼的那条边还开着 —— 玩家说「带我去个安静点的地方」,
    她答应了,还自己挑了地方:

        潮汐里3号。我那儿。……(程屿拉下卷帘门,锁扣咔哒一声。)……二十分钟。

    下一轮她的散文里人已经摸黑上到三楼掏钥匙了,而世界里她还站在唱片店
    (`loc=records`,一条日志都不报错)。这正是"只改提示词的版本"那种坏法:
    她说了走,世界没动,而**没有任何一层会发现**。她手上没有别的办法 ——
    聊天面上只有 `walk_away`(离开这场对话),没有"去某个地方"。

    地名收**人话**:她写的是"潮汐里3号",不是 `flat`。只认 id 的话她第一次调用
    必然失败,而那正是这一轮反复撞见的那条缝(用一种写法印给她,按另一种写法验她的
    回答)。走 `resolve_location` —— 和 `walk_away` 同一份,因为**拒绝的那句话
    必须一模一样**;它第二层照旧认 id,所以老调用方一个字都不用改。
    """
    where = str(params.get("location") or "").strip()
    if not where:
        raise ToolCallError("没说去哪儿")
    return _do(ctx, "walk", {"location": resolve_location(ctx, where)})


@tool(
    id="work",
    writes=("events:state_change",),
    kind="work",
    description="在此刻所在的地方干活",
    params={},
    surfaces=(BODY,),
)
def work(ctx: ToolContext, params: dict) -> ToolResult:
    here = _here(ctx)
    return _do(ctx, "work", {"location": here} if here else {})


@tool(
    id="eat",
    writes=("events:agent_action", "events:item_consume"),
    kind="eat",
    # 只在 BODY 面上(不上玩家屏幕),但一样不写星号 —— 它进的是她的提示词,
    # 而 `**` 在提示词里同样是噪音,她还得照着它行动。
    description="吃东西。「付钱是副作用」:没货没钱就降级成吃随身干粮,不会卡住",
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
    # **这两处是稍后落的,不是当场落的。** 一次搭话当场只发 `agent_action`;
    # 她记住的那句话是**关系判定**的产物,而判定按"LLM 永不在 tick 线程调用"
    # 那条硬不变量跑在判定线程池上(`Scheduler._submit_chat_judgment`)。于是
    # `act()` 返回时它们**还没变** —— 实测 40 次里 10 次如此,而这正是
    # `test_verb_writes.py` 那条测试四分之一概率变红的全部原因:它拿一个同步的
    # 瞬间去量一件异步的事。不写这一行,声明读起来就是"调用返回时都变好了",
    # 而照着它办事的宿主会随机看见一个"她说了话但谁也没记住"的世界。
    writes_late=("events:memory_seed", "memories"),
    kind="chat",
    description="跟同在这儿的另一个角色搭话(对方不在这儿就不成)",
    params={"target": {"type": "string", "description": "跟谁", "required": True}},
    surfaces=(BODY,),
)
def talk_to(ctx: ToolContext, params: dict) -> ToolResult:
    who = str(params.get("target") or "").strip()
    if not who:
        raise ToolCallError("没说跟谁")
    # 玩家点 talk_to 的对象**正是**这一轮的那个角色 —— 只有她自己搭自己才是错的。
    if who == ctx.agent_id and not ctx.is_player:
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


# 她照着提示词说"照料"而不是 `tend`,而人话反查现在归 `Ontology.affordance_of`:
# 动词放开之后,自造动词的人话住在**声明**里,引擎手上没有那张表可查。


@tool(
    id="interact",
    writes=("stocks", "events:entity_interaction"),
    kind="interact",
    description=(
        "对你这儿的一样东西做点什么(照料、收取产出、读……)。"
        # 别提那个块的名字:同一句说明要在两个面上都成立,而【你此刻感觉到的】
        # 这个标题只有聊天那一路才有 —— 自主轮次里那几行是光秃秃的。指路指到一个
        # 她那一轮根本看不见的标题上,和没指一样。
        "东西的 id 和它能被怎么做,都写在你感觉到的那几行里 —— 带方括号 id、"
        "后面跟着「可以……」的就是。"
        "标着「得有人一起」的那些,要用 with 点名跟你一起做的人"
    ),
    # 写给**人**的那一半。上面那句指路指到「你此刻感觉到的」那个提示词块上,
    # 而玩家那一侧没有那个东西 —— 一句对一个人等于没说的话。这儿有什么、能被
    # 怎么做、这会儿点不点得动,走 `World.player_options()`(它每一帧算一次,
    # 且和真点下去那一次共用同一条判定)。
    player_description=(
        "对你这儿的一样东西做点什么(照料、收取产出、擦一擦……)。"
        "这会儿有哪些东西、每样能被怎么做、点不点得动,"
        "都在 target / verb 两个参数的 options 里"
    ),
    params={
        "target": {
            "type": "string", "required": True,
            # 别举例子。举一个 id 出来的下场是:那个 id 只在橱窗世界里存在,
            # 而每个别的世界读到的都是一句错的话。
            "description": "对哪样东西(options 里给的 id)",
        },
        "verb": {
            "type": "string", "required": True,
            "description": "做什么(options 里给的动词;人话和 id 都认)",
        },
        # 一起做的事才用得上。**必须进 params_schema**:菜单上没有的参数她永远
        # 不会填,于是那些能力对她等于不存在(#15 那一课的原话)。
        "with": {
            "type": "array",
            "description": "跟你一起做这件事的人(名字或 id);只有标着「得有人一起」的才要填",
        },
    },
    # **玩家也做得了。** 从前不进 PLAYER 面,理由是"施动者、产出归属、扣减全按
    # 角色写的" —— 那份账现在有了(玩家身上的量、他此刻在哪、扣他的账、一起做事
    # 时算他一份)。不开的样子是一扇她擦得了、我擦不了的窗:同一个世界里两套物理。
    #
    # **它是唯一进自主面的日常动词**,而且是被一轮真世界逼进去的:那个世界有 116 条
    # 规律、76 个实体、一整套动词,而她自己决定时的菜单上只有四样社交能力,三样要
    # 跟前有人 —— 于是 63 次问出来 0 次动作。世界里能做的事和她自己挑得动的事,
    # 是两张不相交的表。
    #
    # 别的日常动词(`walk`/`work`/`eat`/`sleep`)**照旧不进**:那几样是行为树按
    # 排班和需求带在管的,摆进菜单等于给同一件事开第二个不商量的入口 —— 她会在
    # 上班时间决定去睡觉,而排班表根本不知道。`interact` 没有这个问题:行为树里
    # 没有任何一条会替她去照料一棵树。
    surfaces=(BODY, AUTONOMY, PLAYER),
    # 她这儿一样能动的东西都没有时,这条必然失败。而她感觉到的那几行正是它的
    # 参数表:那里没有带 id 的东西,菜单上就不该有这个动词。
    requires_target_entity=True,
)
def interact(ctx: ToolContext, params: dict) -> ToolResult:
    target = str(params.get("target") or "").strip()
    verb = str(params.get("verb") or "").strip()
    if not target:
        raise ToolCallError("没说对什么东西")
    if not verb:
        raise ToolCallError("没说做什么")
    outcome = ctx.runtime.interact_with(
        ctx.world_actor_id, target, verb,
        participants=_party(params.get("with")), player_id=ctx.player_id,
    )
    if not outcome.get("ok"):
        # 世界说"这会儿不行"(果子还没熟)。不是失败,是没成 —— 照实报。
        return ToolResult(ok=False, error=str(outcome.get("refusal") or "这会儿不行"), detail=outcome)
    return ToolResult(detail=outcome)


def _party(raw: Any) -> list[str]:
    """`with` 收成一份名单。

    **一个字符串也收**:模型写 `"with": "柔"` 和 `"with": ["柔"]` 的概率各占一半,
    而按 JSON 类型严格拒绝的话,她那一轮的下场是"没说跟谁一起",接着收到一句
    "这件事得有人一起做" —— 一次她永远学不会的失败。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        # 「柔、白霜」和「柔,白霜」都得认:提示词里的顿号是中文写法的默认。
        return [p.strip() for p in raw.replace("、", ",").replace(",", ",").split(",") if p.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(p).strip() for p in raw if str(p).strip()]
    raise ToolCallError(f"with 得是一份名单,收到 {type(raw).__name__}")
