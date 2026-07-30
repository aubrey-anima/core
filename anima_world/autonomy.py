"""定时轮次:没人跟她说话的时候,她自己决定要不要做点什么。

聊天里的能力(#15)只在**玩家开口**之后才有机会被选中。于是"她能拒绝、能走开、能
公开说一句"这件事,仍然全靠人来触发 —— 世界自己转的那一半里,她只会按行为树过日子。
这个模块补的是那个触发:每隔一段世界时间问她一次"此刻想做点什么吗",她可以什么都
不做(而且**默认就该什么都不做**)。

四条纪律,和引擎其它 LLM 路径完全一致:

1. **永不在 tick 线程调用。** 调度器只在到点时喊一声,快照当场取、决定与执行都在
   世界自己那条事件循环上跑。时钟永远不等网络。
2. **默认关闭**(`autonomy.enabled`),而且**默认什么都不做**:提示词把"不做"写成
   头一个选项。一个每六小时都非要干点什么的角色,比一个安静的角色假得多。
3. **有上限**(`autonomy.max_per_day`):她一天最多主动几次。没有这条,一个话痨
   角色会把玩家的收件箱刷满,而每一条都是一次 LLM 花销。
4. **在场的语义照旧由引擎守住**:找一个不在场的人是空动作(见 `reach_out`)。

这一轮里她**只能挑能力,不产出台词**(`reach_out` 的 `text` 参数除外)——没有人在
听的时候,一段散文没有去处,写进日志只会变成一堆没人读的独白。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_TICKS = 72   # 5 分钟/tick 时是 6 个世界小时
DEFAULT_MAX_PER_DAY = 2

# 措辞是踩过坑改的(2026-07-30,真模型实测)。第一版把"什么都不做"说了三遍
# ——"你可以什么都不做""**这也是最常见的选择**""什么都不想做就回无"—— 而"什么时候
# 该主动"一句没写。结果:**18 轮 0 次动作**,这一层是个永远不触发的空机制。
#
# 这和 issue #15 那次是同一个错的镜像:那次是"给了能力却没给许可",这次是"给了
# 三重别动的许可"。而且我当时用**提示词**去做限流 —— 明明已经有
# `autonomy.max_per_day` 这个真正的硬上限。两道刹车叠在一起,车就不走了。
# 分工应该是:**硬上限管节制,提示词让她像个人。**
DEFAULT_DECIDE_PROMPT = (
    "你是{name}。{personality}\n\n"
    "现在是第 {day} 天 {hh}:{mm},你在{location},{activity}。\n"
    "{present_block}"
    "{state_block}"
    "【此刻你想做点什么吗】\n"
    "没有人在跟你说话 —— 但人不是只在被搭话时才有动作。想做就做,不想做就算了,"
    "两者都正常。\n"
    "什么时候值得主动:**身边有你在意(或在意不起来)的人,而你还没跟他开口**;"
    "有件事你想让这一带的人都知道;有人反复越界让你不想再理他;"
    "或者你就是想找个人说句话。\n"
    "要做就只输出一行,别的什么都不要写:\n"
    "〔tool:能力名 {{\"参数\": 值}}〕\n"
    "可用的能力:\n"
    "{tool_menu}\n"
    "确实没什么想做的,就只回一个字:无。"
)

_PRESENT_TEMPLATE = "这会儿在你身边的人:{names}。\n"
_NOBODY_TEMPLATE = "这会儿你身边没有别人。\n"


@dataclass
class AutonomyContext:
    """一轮决定要用到的快照。**在锁内取,之后只读** —— worker 绝不碰活状态。"""

    agent_id: str
    name: str
    personality: str = ""
    day: int = 0
    hour: int = 0
    minute: int = 0
    location: str = ""
    activity: str = "闲着"
    present: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def present_block(self) -> str:
        if not self.present:
            return _NOBODY_TEMPLATE
        names = "、".join(
            f"{person.get('name') or person['id']}({person['id']})"
            for person in self.present
        )
        return _PRESENT_TEMPLATE.format(names=names)

    def state_block(self) -> str:
        return "".join(f"- {note}\n" for note in self.notes)


def build_messages(template: str, ctx: AutonomyContext, tool_menu: str) -> list[dict[str, str]]:
    system = template.format(
        name=ctx.name,
        personality=ctx.personality,
        day=ctx.day,
        hh=f"{ctx.hour:02d}",
        mm=f"{ctx.minute:02d}",
        location=ctx.location or "某个地方",
        activity=ctx.activity,
        present_block=ctx.present_block(),
        state_block=ctx.state_block(),
        tool_menu=tool_menu,
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "此刻你想做点什么吗?"},
    ]


def decide_nothing(reason: str) -> dict[str, Any]:
    return {"acted": False, "reason": reason}


def parse_decision(text: str, allowed: Sequence[str]) -> dict[str, Any]:
    """从回包里读出她要调的那个能力。读不懂 = 什么都不做,并说明原因。

    宽容的方向是**不做**:一次读错而误发一条搭话,玩家会收到一句莫名其妙的消息;
    一次读错而漏掉一个动作,只是这次她安静了。后者便宜得多。
    """
    from anima_world.directives import strip_directives

    raw = (text or "").strip()
    if not raw:
        return decide_nothing("她没回话(空回复),这轮什么也不做")
    _, directives = strip_directives(raw)
    calls = [directive for directive in directives if directive.kind == "tool"]
    if not calls:
        return decide_nothing("她选择什么都不做")
    call = calls[0]
    if call.name not in allowed:
        logger.warning("自主轮次里挑了一个这个面上没有的能力 %r,忽略", call.name)
        return decide_nothing(f"她挑了 {call.name},但那个能力在这一轮上没有")
    if len(calls) > 1:
        # 一轮只做一件事:定时轮次不是 loop(#17 那条路是显式的、玩家在场时的)。
        logger.info("自主轮次里挑了 %s 个能力,只执行第一个", len(calls))
    return {"acted": True, "tool": call.name, "params": dict(call.params)}
