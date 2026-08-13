"""社交 / 离场 / 拒绝类能力(issue #15 的 v1 六条 + #17 的让位)。

现有的能力都是"过日子的动作"(吃/睡/走/工作/找人聊),这些是**关系触发**的:
她说"我走了"要真的走,说"我不想聊"下一条消息要真的被拒。
"""

from __future__ import annotations

import logging

from anima_world.tools.base import (
    AUTONOMY, CHAT, PLAYER, ToolCallError, ToolContext, ToolResult,
    resolve_location, tool,
)

logger = logging.getLogger(__name__)

_DEFAULT_MUTE_MINUTES = 5.0
_DEFAULT_DELAY_MINUTES = 10.0


def _minutes(params: dict, key: str, default: float, ctx: ToolContext, cap_key: str) -> float:
    raw = params.get(key, default)
    try:
        minutes = float(raw)
    except (TypeError, ValueError):
        raise ToolCallError(f"{key} 不是一个数:{raw!r}") from None
    if minutes <= 0:
        raise ToolCallError(f"{key} 必须为正:{minutes}")
    cap = float(ctx.runtime.config(cap_key, 1440.0) or 1440.0)
    if minutes > cap:
        # 不静默截断:她说"一个月别理我"而世界只执行一天,那是两回事。
        logger.warning(
            "%s 想静音 %s 分钟,超过上限 %s,按上限执行", ctx.agent_id, minutes, cap
        )
        minutes = cap
    return minutes


@tool(
    id="mute",
    writes=("agent_mutes", "events:state_change"),
    kind="mute",
    description="屏蔽这个人一段时间,期间他发来的消息一概不理",
    params={
        "minutes": {"type": "number", "description": "屏蔽多少分钟", "required": True},
        "reason": {"type": "string", "description": "为什么(只有作者与运维看得到)"},
    },
    surfaces=(CHAT, AUTONOMY),
)
def mute(ctx: ToolContext, params: dict) -> ToolResult:
    minutes = _minutes(params, "minutes", _DEFAULT_MUTE_MINUTES, ctx, "chat.tools.max_mute_minutes")
    reason = str(params.get("reason") or "").strip() or None
    expires = ctx.state.set_quiet(
        ctx.agent_id, ctx.player_id, kind="mute", minutes=minutes, reason=reason,
    )
    # 软静音(issue #15 开放问题 2):玩家还能发,世界当场拒。硬静音(锁输入框)
    # 由宿主按这条事件自己决定 —— 引擎不替 UI 做主。
    ctx.runtime.emit({
        "type": "state_change",
        "who": ctx.agent_id,
        "payload": {
            "kind": "mute_started",
            "as": ctx.agent_id,
            "target": ctx.player_id,
            "minutes": minutes,
            "expires_at": expires,
            "reason": reason,
        },
    })
    return ToolResult(
        end_conversation=True, stop_loop=True,
        detail={"minutes": minutes, "expires_at": expires},
    )


@tool(
    id="end_conversation",
    writes=("conversations",),
    kind="end",
    description="结束这次对话,但不屏蔽对方",
    params={"reason": {"type": "string", "description": "为什么"}},
)
def end_conversation(ctx: ToolContext, params: dict) -> ToolResult:
    return ToolResult(
        end_conversation=True, stop_loop=True,
        detail={"reason": str(params.get("reason") or "").strip()},
    )


@tool(
    id="delay_reply",
    writes=("agent_mutes", "agent_followups"),
    kind="delay",
    description="现在不方便说,等一会儿再回来找他",
    params={
        "minutes": {"type": "number", "description": "多少分钟以后回来", "required": True},
        "reason": {"type": "string", "description": "为什么"},
    },
)
def delay_reply(ctx: ToolContext, params: dict) -> ToolResult:
    minutes = _minutes(params, "minutes", _DEFAULT_DELAY_MINUTES, ctx, "chat.tools.max_delay_minutes")
    reason = str(params.get("reason") or "").strip() or None
    expires = ctx.state.set_quiet(
        ctx.agent_id, ctx.player_id, kind="delay", minutes=minutes, reason=reason,
    )
    # 兑现那一半:到点她真的回来敲一次门(issue #13 的 agent_hail 复用)。
    # 只落一条"等会儿"的记录而没有人回来,就又是一条声明过没人读的机制。
    due = ctx.runtime.tick() + max(1, ctx.runtime.ticks_for_minutes(minutes))
    followup_id = ctx.state.add_followup(
        ctx.agent_id, ctx.player_id, due_tick=due, kind="delayed_reply",
        reason=reason, created_tick=ctx.runtime.tick(),
    )
    return ToolResult(
        stop_loop=True,
        detail={"minutes": minutes, "expires_at": expires,
                "due_tick": due, "followup_id": followup_id},
    )


@tool(
    id="walk_away",
    writes=("conversations", "events:travel"),
    kind="walk_away",
    description="话说到一半就走人:面对面就真的离开这里,隔着手机就是挂断",
    params={"to_location": {"type": "string", "description": "去哪儿(不填就随便走开)"}},
)
def walk_away(ctx: ToolContext, params: dict) -> ToolResult:
    """面对面就真的走掉;隔着手机就是**挂断**,不发一趟没有意义的行程。

    这条是真模型跑出来的:她在咖啡店被骂,`walk_away` 走去工作室 —— 对的。但玩家
    还在咖啡店,下一句照旧发得到(手机私聊),于是她又"走开"了一次,再一次,连着四趟
    行程,而每一趟在世界里都是真事件。**对一个不在你面前的人"走开"是个空动作** ——
    在场的语义得由引擎守住,不能指望模型每次都想到自己此刻是在打电话。

    `to_location` 收**人话**(`resolve_location`,和 `walk` 同一份)。此前它把
    她写的那几个字原样递给 `move_agent`,而那一层只认 id:她读到的世界里写着
    「江堤」,写下「江堤」就被顶回一句光秃秃的「没有 江堤 这个地方」—— 连有哪些
    都不说,等于让她再猜一次。
    """
    here = ctx.runtime.agent_location(ctx.agent_id)
    if not ctx.runtime.face_to_face(ctx.agent_id, ctx.player_id):
        return ToolResult(
            end_conversation=True, stop_loop=True,
            detail={"degraded_to": "end_conversation", "reason": "对方不在你这儿,走开没有意义 —— 这一下等于挂断"},
        )
    raw = str(params.get("to_location") or "").strip()
    destination = resolve_location(ctx, raw) if raw else ""
    if not destination or destination == here:
        options = [pid for pid in ctx.runtime.point_ids() if pid != here]
        if not options:
            raise ToolCallError("这个世界只有一个地方,走不掉")
        # 不摇骰子:同一个世界里"走开"应当可复现。第一个不在这儿的地方就行。
        destination = options[0]
    moved = ctx.runtime.move_agent(ctx.agent_id, destination)
    return ToolResult(
        end_conversation=True, stop_loop=True,
        detail={"to": destination, **moved},
    )


@tool(
    id="refuse_topic",
    writes=("agent_refused_topics",),
    kind="topic_ban",
    description="以后不谈这个话题;对方再提就岔开",
    params={
        "keyword": {"type": "string", "description": "不谈什么", "required": True},
        "minutes": {"type": "number", "description": "多久之后可以再谈(不填就一直)"},
    },
    surfaces=(CHAT, AUTONOMY),
)
def refuse_topic(ctx: ToolContext, params: dict) -> ToolResult:
    keyword = str(params.get("keyword") or "").strip()
    if not keyword:
        raise ToolCallError("要拒绝的话题是空的")
    minutes = params.get("minutes")
    if minutes is not None:
        minutes = _minutes(params, "minutes", 60.0, ctx, "chat.tools.max_mute_minutes")
    ctx.state.refuse_topic(ctx.agent_id, keyword, minutes=minutes)
    return ToolResult(detail={"keyword": keyword, "minutes": minutes})


@tool(
    id="broadcast",
    writes=("events:agent_broadcast", "memories"),
    kind="broadcast",
    description="当众说一句话,这会儿在你身边的人都听得见,而且会记住",
    params={"text": {"type": "string", "description": "说什么", "required": True}},
    surfaces=(CHAT, AUTONOMY, PLAYER),
)
def broadcast(ctx: ToolContext, params: dict) -> ToolResult:
    """当众说一句 —— **在场的人真的会记住它**。

    原来这条只发一个 `agent_broadcast` 事件就完了:没有任何角色消费它,于是她"当众
    宣布"的后果是一行日志,世界里谁也不知道。而菜单还告诉她"世界里的人都能看到"——
    她照着一句假话做了决定。这是 CLAUDE.md 那条硬不变量的漏洞:**她的选择必须在世界
    里兑现**(`walk_away` 真起程、`delay_reply` 真回来敲门、`narrative_direction` 真挪人)。

    兑现走现成的路:给每个在场的角色发一条 `memory_seed`,调度器会折进他们的记忆 ——
    于是下次跟他们说话时,这句话在**他们的**提示词里。不新造广播收件箱:记忆本来就是
    这个引擎里"角色知道一件事"的表示。

    听众是**此刻同一个地方的人**,不是全世界:一句喊话传遍地图是那种"客观存在 = 人人
    皆知"的错(§2.9.4 认知层立的就是这条规矩)。要传更远,那是八卦的活。
    玩家侧不落记忆 —— 宿主自己读 `World.broadcasts()`,引擎不替 UI 做主。
    """
    text = str(params.get("text") or "").strip()
    if not text:
        raise ToolCallError("广播内容是空的")
    # 施动者可以是人:他当众说的一句,同样该进在场角色的记忆。**没有"自己"要排除**
    # —— 玩家不在 `agent_ids()` 里,而她广播时要跳过她自己。
    if ctx.is_player:
        here = ctx.runtime.player_location(ctx.player_id) or None
        speaker = ctx.runtime.player_name(ctx.player_id)
        who, exclude = f"player:{ctx.player_id}", None
    else:
        here = ctx.runtime.agent_location(ctx.agent_id) or None
        speaker = ctx.runtime.agent_names().get(ctx.agent_id) or ctx.agent_id
        who, exclude = ctx.agent_id, ctx.agent_id
    heard_by = [
        agent_id for agent_id in ctx.runtime.agent_ids()
        if agent_id != exclude and here and ctx.runtime.agent_location(agent_id) == here
    ]
    ctx.runtime.emit({
        "type": "agent_broadcast",
        "who": who,
        "loc": here,
        "payload": {
            "agent_id": who, "text": text,
            "audience": "here", "heard_by": heard_by,
        },
    })
    for listener in heard_by:
        ctx.runtime.emit({
            "type": "memory_seed",
            "who": listener,
            "loc": here,
            "payload": {
                "agent_id": listener,
                "kind": "observation",
                "summary": f"{speaker}当众说:{text}",
                "importance": 0.45,
            },
        })
    return ToolResult(detail={"text": text, "heard_by": heard_by})


@tool(
    id="wait_for_user",
    writes=(),
    kind="yield",
    description="我说完了,轮到你 —— 停下等对方开口",
    params={},
)
def wait_for_user(ctx: ToolContext, params: dict) -> ToolResult:
    """#17 的显式让位。它不是"什么也没做":它是 loop 唯一的正常出口。"""
    return ToolResult(stop_loop=True, detail={"reason": "explicit_yield"})


@tool(
    id="reach_out",
    writes=("events:agent_hail",),
    kind="hail",
    description="主动去找一个此刻在场的人开口(只有定时轮次里用得上)",
    params={
        "player_id": {"type": "string", "description": "找谁(在场名单里的那个 id)"},
        "text": {"type": "string", "description": "开口第一句说什么"},
    },
    surfaces=(AUTONOMY,),
    # 这条一直在**手写**同一件事(下面 `present_player_ids(ctx.agent_id)` 那两句)。
    # 声明出来不改变它的行为(默认关的那道闸之外,处理器里那两句照旧兜底),
    # 改变的是**别人问得到**:目录里有了这一格,宿主界面才知道这个按钮要人走过去。
    requires_colocation=True,
)
def reach_out(ctx: ToolContext, params: dict) -> ToolResult:
    """她自己决定来找你(定时轮次的主力能力)。

    走 issue #13 那条 `agent_hail`,并守住那条边界:**敲门不是对话** —— 不产生记忆、
    不动关系、不开会话。玩家还没回话,世界里什么也没发生;真正的对话仍然由
    `World.chat` 发起,走原来那条完整的链。这里比 #13 多的只有一句:她带着话来的,
    而不是一条空白的"有人找过你"。

    在场以 TTL 为准(`World.who_is_present`)**并且要同地**:找一个不在场、或者在场
    但不在她这儿的人都是空动作(隔着半个地图"走过来跟你说话"没有意义)——
    那正是这个仓库最在意的"给不在的人写事件",所以当场拒绝而不是照发。

    **一天开一次口,而且今天说过话就不算开口了**(`claim_hail`,水位和闲着时那条
    `_maybe_hail_player` 共用)。少了这一句的样子是实测出来的:玩家正一句一句跟她
    聊着,这条能力插了两次话,两次都是招呼生客的口气。
    """
    target = str(params.get("player_id") or "").strip()
    present = ctx.runtime.present_player_ids(ctx.agent_id)
    if not present:
        raise ToolCallError("这会儿她身边没有人")
    if not target:
        target = present[0]
    if target not in present:
        raise ToolCallError(f"{target} 不在她身边 —— 给不在场的人发一条搭话是空动作")
    refusal = ctx.runtime.claim_hail(ctx.agent_id, target)
    if refusal:
        raise ToolCallError(refusal)
    text = str(params.get("text") or "").strip()
    here = ctx.runtime.agent_location(ctx.agent_id)
    ctx.runtime.emit({
        "type": "agent_hail",
        "who": ctx.agent_id,
        "loc": here or None,
        "payload": {
            "agent_id": ctx.agent_id,
            # 玩家收到的推送上写的就是这个,和调度器那两条 agent_hail 一样。
            "agent_name": ctx.runtime.agent_names().get(ctx.agent_id, ctx.agent_id),
            "player_id": target,
            "player_name": ctx.runtime.player_name(target),
            "location": here,
            "location_name": ctx.runtime.point_names().get(here, here),
            "reason": "initiative",   # 不是"闲着想找人",是她自己决定的
            "text": text,
        },
    })
    return ToolResult(detail={"player_id": target, "text": text})
