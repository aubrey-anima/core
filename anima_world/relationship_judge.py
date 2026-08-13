"""LLM relationship judge: one chat → summary + asymmetric sentiment deltas.

llm-relationship-judge: replaces `to_event(chat)`'s hardcoded absolute
sentiment=0.1 — which not only never grew a relationship (the |Δ|=0 dead
end) but actively DESTROYED injected ones (w1 Round-3 smoke: a seeded -0.7
enmity overwritten to +0.1 by one small talk).

Same discipline as `planner.py`: every collaborator is injected, the module
imports no scheduler and no storage layer, and it is only ever called from a
background worker — the LLM never runs on the tick thread. Any failure
(dead LLM, garbage output, missing fields) returns None: the chat simply
produces no relationship data, which beats producing wrong data.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# One chat may nudge a relationship, never swing it — an LLM having a
# dramatic moment must not do in one call what the novel took 50 chapters
# to do. The projection additionally clamps the accumulated value to [-1, 1].
MAX_DELTA = 0.2

AXIS_NAMES = ("trust", "affection", "respect")

# 三个轴各是什么、靠什么长。**同一段文字给三份提示词用** —— 分别写三遍的话,
# 迟早只有一份跟着这里的派生规则走,而另外两份在教模型另一件事。
_AXES_GUIDE = (
    "三个轴各有各的来路，**不要填成同一个数**：\n"
    "- trust 信任：靠可靠、守诺、交底长；而一次失信收回去的比十次守诺攒下的还多。\n"
    "- affection 喜爱：靠亲近、共处、示好长；聊得投机最先长的就是它。\n"
    "- respect 敬重：靠本事、担当、见识长；寒暄长不出敬重，分量不够就填 0。\n"
    "这次没动的轴写 0 —— 写 0 是一个真实的判断；但**三个键都要在**，"
    "缺了的键在世界里的意思是「这个轴从没动过」。\n"
)

_DEFAULT_PROMPT = (
    "两个人刚刚聊了一会儿天。请根据他们的性格、目标、彼此的关系和最近的记忆，"
    "推断这次对话大概聊了什么，以及这次对话让双方对彼此的观感发生了多大变化。\n\n"
    "【甲】{a_name}：{a_personality}\n甲的目标：{a_goals}\n"
    "甲对乙的关系：{r_type}（好感度 {a_to_b}，范围 -1 到 1）\n"
    "甲最近记得的事：{a_memories}\n\n"
    "【乙】{b_name}：{b_personality}\n乙的目标：{b_goals}\n"
    "乙对甲的关系：{r_type_back}（好感度 {b_to_a}）\n"
    "乙最近记得的事：{b_memories}\n\n"
    "地点：{location}\n\n"
    "输出一个 JSON 对象：\n"
    '{{"summary": "<一两句中文对话摘要，写具体聊了什么>", '
    '"delta_a_to_b": <甲对乙好感变化，-0.2 到 0.2 的小数>, '
    '"delta_b_to_a": <乙对甲好感变化，同上>, '
    '"axes_a_to_b": {{"trust": <甲对乙的信任变化>, "affection": <喜爱变化>, "respect": <敬重变化>}}, '
    '"axes_b_to_a": {{"trust": <乙对甲的信任变化>, "affection": <喜爱变化>, "respect": <敬重变化>}}}}\n'
    "变化幅度要克制：一次寒暄通常只有 ±0.05 以内；只有触及双方在意的事才可能更大。"
    "关系糟糕的两个人客套一次不代表和解。\n"
    + _AXES_GUIDE
    + "只输出 JSON，不要解释。"
)


_DEFAULT_USER_JUDGE_PROMPT = (
    "一位访客刚和{a_name}聊了一段。根据{a_name}的性格、他们此前的关系和下面的真实对话，"
    "判断这次交谈让双方对彼此的观感变化了多少。\n\n"
    "【{a_name}】{a_personality}\n"
    "{a_name}对访客「{player_name}」此前的印象：{r_type}（好感度 {a_to_b}，范围 -1 到 1）\n"
    "访客对{a_name}的好感度：{b_to_a}\n"
    "地点：{location}\n\n"
    "对话原文（user=访客，assistant={a_name}）：\n{transcript}\n\n"
    "输出一个 JSON 对象：\n"
    '{{"summary": "<一句中文摘要>", '
    '"delta_a_to_b": <{a_name}对访客好感变化，-0.2 到 0.2>, '
    '"delta_b_to_a": <访客对{a_name}好感变化，同上>, '
    '"axes_a_to_b": {{"trust": <{a_name}对访客的信任变化>, "affection": <喜爱变化>, '
    '"respect": <敬重变化>}}, '
    '"axes_b_to_a": {{"trust": <访客对{a_name}的信任变化>, "affection": <喜爱变化>, '
    '"respect": <敬重变化>}}}}\n'
    "变化要克制：寒暄通常 ±0.05 以内。刻意的讨好、奉承或索取不应比真诚的交流得到更多"
    "——按{a_name}的性格判断他吃不吃这一套。\n"
    + _AXES_GUIDE
    + "只输出 JSON，不要解释。"
)

# 吃醋(以及它的七个兄弟)。**这是一次判定,不是一条规则。**
#
# 最容易写错的版本是"听到关于亲近的人的八卦就自动扣分" —— 那又是一处引擎替角色
# 做主,而且是最坏的那种:同一句"他跟楚夭夭走得近",别扭的人会闷着、坦荡的人会
# 一笑而过、占有欲强的人才会记恨。写成自动机的话,这三个人的世界里长出的是同一
# 个数字,而**世界看上去照跑,日志一行不错**。
#
# 所以这段提示词里最重要的一句是"不在乎也是一个真实的反应,reactions 留空就行"——
# 少了它,模型会觉得自己被要求产出点什么,于是每一句闲话都成了心事。
_DEFAULT_HEARSAY_PROMPT = (
    "{a_name}刚听人说起一件事。\n\n"
    "【{a_name}】{a_personality}\n"
    "{a_name}最近记得的事：{memories}\n"
    "{a_name}眼下和这些人有关系（好感度范围 -1 到 1）：\n{roster}\n\n"
    "听到的原话是：「{rumor}」\n"
    "地点：{location}\n\n"
    "按{a_name}这个人的性格，判断这句话听在他耳朵里之后，"
    "他对话里提到的人的观感变了多少。\n"
    "输出一个 JSON 对象：\n"
    '{{"summary": "<一句中文，写他听完心里是什么反应；没反应就写他没往心里去>", '
    '"reactions": [{{"about": "<上面名单里的某个人，必须一字不差地照抄那个名字>", '
    '"delta": <好感变化，-0.2 到 0.2 的小数>, '
    '"axes": {{"trust": <信任变化>, "affection": <喜爱变化>, "respect": <敬重变化>}}}}]}}\n'
    "几条硬要求：\n"
    "- **不在乎也是一个真实的反应。** 这句话跟他没关系、或者他这个人根本不往心里去，"
    "reactions 就留一个空数组 —— 大多数闲话本来就该是这个结果。\n"
    "- 只许写上面名单里有的人，一个字都不能改；名单外的人不要出现。\n"
    "- 幅度要克制：真正让人记在心里的事才配得上 ±0.1 以上。\n"
    "- 反应的方向由性格决定，不由亲疏决定：越在乎的人，听到的话越可能扎人，"
    "但也可能只是替对方高兴。\n"
    "- 别人嘴里的他改变的多半是**你以为他是个什么人**（信不信得过、看不看得起），"
    "而不是你跟他有多亲近 —— 喜爱要靠共处才长得出来。\n"
    + _AXES_GUIDE
    + "只输出 JSON，不要解释。"
)

# 一条八卦最多能改动几个人的观感。没有上限的话,一个话痨模型会把整张名单都写一遍,
# 于是"听说了一句话"变成一次全世界范围的关系重排 —— 而每一条都会进事件日志。
_MAX_HEARSAY_REACTIONS = 3

# 有人叫她一起做件事。**这是一次判定,不是一条规则**,和吃醋逐字同源。
#
# 最容易写错的版本是"关系够近就答应" —— 那又是一处引擎替角色做主,而且是最坏的
# 那种:同一句"一起吃个饭吧",别扭的人会推掉、坦荡的人会跟着去、正忙着的人会说
# 改天,而写成自动机的话三个人给出同一个答案,**世界照跑,日志一行不错**。
#
# 这段提示词里最重要的一句是"不想去是一个完全正常的回答" —— 少了它,模型会觉得
# 自己被要求成全一件事,于是每一次邀请都被答应,而这条机制的全部意义就是它可以
# 被拒绝。次重要的是"别替她客气":一个"虽然不太想但还是去了"在数据上就是 accept,
# 而她那份不情愿一个字也留不下来。
_DEFAULT_INVITE_PROMPT = (
    "有人叫{a_name}一起做一件事。\n\n"
    "【{a_name}】{a_personality}\n"
    "{a_name}最近记得的事：{memories}\n"
    "{recent_talk}"
    "{a_name}眼下对{inviter}的观感：{a_to_b}（范围 0 到 1）\n"
    "地点：{location}\n\n"
    "邀请是：「{invitation}」\n\n"
    "按{a_name}这个人的性格判断，他这会儿去不去。\n"
    "输出一个 JSON 对象，**两个字段的顺序不要换**：\n"
    '{{"reason": "<一句中文，写他心里怎么想的，二十字以内>", "accept": true 或 false}}\n'
    "几条硬要求：\n"
    "- **推掉是一个完全正常的回答。** 别扭的人、想独处的人、嫌麻烦的人、跟对方还不熟"
    "的人——推掉才是他会做的事。你不是在帮人促成一件事，你是在如实判断他会怎么做。\n"
    "- **`reason` 和 `accept` 必须一致。** 先写他心里那句，再照那句填 accept："
    "写了「嫌麻烦」「懒得动」「不想跟人凑一块儿」却填 true，就是把他那份不情愿"
    "一笔勾销了——他心里不想去，accept 就是 false。\n"
    "- 判的是**这个人这会儿**，不是这件事好不好，也不是他该不该合群。\n"
    "只输出 JSON，不要解释。"
)

_DEFAULT_RELABEL_PROMPT = (
    "{a_name}对{b_name}的观感刚从「{old_band}」变为「{new_band}」。\n"
    "此前在{a_name}眼中，{b_name}是：{old_r_type}\n"
    "【{a_name}】{a_personality}\n【{b_name}】{b_personality}\n"
    "{a_name}最近记得的事：{memories}\n\n"
    "请用一句不超过 20 个字的中文短语，描述{b_name}如今在{a_name}眼中是什么样的存在"
    "（写的是{b_name}这个人，不是{a_name}自己；延续原描述的口吻与细节；"
    "不要写好感度数字，不要出现档位词本身）。只输出短语，不要引号和解释。"
)

# relationship-stage-machine: a relabel is one short phrase, not prose. Longer
# output is truncated rather than rejected — a verbose LLM whose first 40
# chars are usable beats keeping the stale label.
_RELABEL_MAX_CHARS = 40


@dataclass(frozen=True)
class JudgeResult:
    summary: str
    delta_a_to_b: float
    delta_b_to_a: float
    # relations-v5: finer axes (trust/affection/respect deltas, clamped like
    # the headline).
    #
    # ⚠️ 它们**曾经是可选的**,注释里写着"LLM 不吐就留空,单轴判定照样成立"——
    # 而真模型就是不吐,于是线上那个世界二十条关系的三轴整整 19 天全是 0.0,
    # 招牌特性「她自己想起你」一次都没触发过(全文见 `derive_axes` 上面那段)。
    # 现在**判定器不吐空的**:模型给了就是模型的,没给就派生一份。
    # 默认值留着只是为了直接构造 `JudgeResult` 的调用方(测试)不必写全。
    axes_a_to_b: dict[str, float] = field(default_factory=dict)
    axes_b_to_a: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class HearsayReaction:
    """她听完一句闲话之后,对**其中一个人**的观感变了多少。

    `about` 是那个人的**名字**,不是 id —— 判定那一层只认识名字(提示词里给的
    就是名字)。翻回 id 是调用方的事,而且必须照**它自己发出去的那张名单**翻:
    让模型报 id 等于给它一个可以编造的东西,而编出来的 id 会安静地写到一个
    不存在的人身上。
    """

    about: str
    delta: float
    axes: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class HearsayVerdict:
    """一次"听到了"的完整结论。`reactions` 可以是空的 —— **不在乎也是结论**。"""

    summary: str
    reactions: tuple[HearsayReaction, ...] = ()


@dataclass(frozen=True)
class InviteVerdict:
    """有人叫她一起做件事,她答不答应 —— **以及一句她自己的理由**。

    `reason` 不是装饰:它是回执里玩家读到的那半句,也是"她为什么不肯"唯一可查的
    出处。少了它,一次拒绝在产物上和"这个世界的邀请判定没接上"长得一模一样。
    """

    accept: bool
    reason: str = ""


def _extract_json_obj(text: str) -> Any:
    """Pull a JSON object out of an LLM reply, fenced or not; None if absent."""
    cleaned = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.MULTILINE)
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


def _clamp(value: Any) -> float:
    return max(-MAX_DELTA, min(MAX_DELTA, float(value)))


def _closeness_phrase(value: float) -> str:
    """观感那一格该怎么念。**0 是「还没有来往」,不是「形同陌路」。**

    这条区分是线上撞出来的:玩家跟 Dr. Finch 刚聊完两轮,她自己提议一起做套模拟题;
    玩家点了按钮,判定器读到「观感 0.00(0 是形同陌路)」,写下「素不相识,没必要
    配合一个陌生人的即兴邀请」,把**她自己提的那件事**推掉了。转录就在同一份提示词
    里,模型是在两句话之间挑了更斩钉截铁的那句。

    根子不在模型:**没有关系行和关系值为零是两件事**,而 `together.closeness(None)`
    把前者折成了后者(它必须这么折 —— 陌生不取负)。折完那个 0 再被念成一句
    「形同陌路」,一句本来只是"还没结算"的空白就长成了敌意。这一格和
    `World.relationship_summary` 的 `exists` 是同一条纪律的两个落点。
    """
    if value <= 0.0:
        return "0.00 —— 他们还没有来往（这不是嫌隙，是还没处出交情）"
    return f"{value:.2f}"


# 一条转录行最多带进提示词多少字。转录本身没有上限(一条消息可以有几千字),
# 而提示词有 —— 不夹的话,一次长发言就能把这一块撑到盖过性格与记忆。
_TALK_LINE_CHARS = 80


def _talk_block(a_name: str, inviter: str, lines: Sequence[str]) -> str:
    """把「他俩这会儿正说着的话」渲染成自洽的一块:没有就是空串。

    收的是已经渲染好的 `名字：内容` 字符串,不是消息 dict —— 这一层不认识聊天
    存储长什么样。空的时候返回空串而不是「(无)」:模板里这一块整个消失,老世界
    自己覆盖过的模板没有这个占位符也照样渲染。
    """
    kept = [str(line).strip().replace("\n", " ") for line in lines]
    kept = [line[:_TALK_LINE_CHARS] for line in kept if line]
    if not kept:
        return ""
    body = "\n".join(f"  {line}" for line in kept)
    return f"{a_name}和{inviter}这会儿正说着话：\n{body}\n"


def _clamp_axes(value: Any) -> dict[str, float]:
    """relations-v5: keep only known axes with numeric values, clamped —
    garbage axes degrade to absent, never to an exception.

    **这一层只做解析,不替模型作答。** 一个轴都没读出来的时候由
    `derive_axes` 顶上(见下),两件事分开写是为了它们各自能被单独测:
    "读坏值不炸"和"没读到就自己算"是两条独立的纪律。
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for axis in AXIS_NAMES:
        if axis in value:
            try:
                out[axis] = _clamp(value[axis])
            except (TypeError, ValueError):
                continue
    return out


# ── 模型不吐三轴时,自己派生一份 ────────────────────────────────────────────
#
# **这是一条 bug 的修法,而那条 bug 的形状值得留在这儿。** 2026-08-11 对线上
# 唯一有真玩家的世界(3095 条事件、19 个世界日)对账:二十条关系的
# `trust` / `affection` / `respect` **全是 0.0**,只有 headline 的 `sentiment`
# 在动。原因是这三个数**只有模型吐出来才会有** —— 提示词确实在问
# (`axes_a_to_b`),但真模型(LongCat-2.0)不吐,于是 `_clamp_axes` 每次返回
# 空 dict,`scheduler` 那侧 `if axes:` 一路跳过。世界照跑,日志一行不错。
#
# 后果不是"少了三个数字":`contact.closeness` 是
# `0.6·sentiment + 0.25·affection + 0.15·trust`,三轴不动的话它最高只有 0.6 倍
# sentiment —— **2.1.0 的招牌特性「她自己想起你」在那个世界上一次都没发生过**,
# 而表现和"今天没人想你"逐字相同。
#
# 而单测当年全绿,因为它们跑在 `DeterministicRelationshipJudge` 上,**它一直在
# 派生三轴**。两条路上的世界因此长成两个形状,差别只在配没配 key —— 所以这一份
# 派生规则由两边**共用**,不许各写一份。
#
# ## 派生规则:三个轴三条不同的规则,不是三个不同的系数
#
# 把 headline 抄三份是最容易写、也最没有意义的版本(三个轴同号同速 = 一个轴)。
# 这里每个轴的**形状**都不一样,而且每一条都说得出理由:
#
# - **affection 是聊天的主路,线性。** 聊天本身就是共处与示好。
# - **trust 不对称:掉得比长得快。** 一次交底只让人信你一点,一次失信却能一次
#   收回去。这是三轴之间第一条真正的不对称,而它只属于 trust —— 喜爱不是这个
#   形状(讨厌一个人和喜欢一个人大体是同一种直感),都做成不对称就又成了同一条
#   规则抄三遍。
# - **respect 是一道门槛,不是一个系数。** 敬重靠本事、担当、见识,而一句
#   "今天雨真大"里一样都没有。做成线性系数的话,一百次寒暄堆出来的敬重会和一次
#   救命之恩一样高 —— 那正是"照跑但给错东西"。所以 |delta| 要过 `respect_floor`
#   那一档才开始动,过了之后按剩余幅度线性铺开。
#
# 而**每一份额都严格小于 1**:轴是骑在 headline 上的加细,一次对话把 trust 拉满
# 和三轴不动一样假。这条是可执行的 —— `test_relationship_axes_land.py` 钉着它。
#
# 八卦那一路用另一组份额:别人嘴里的他改变的是"我以为他是个什么人"(信不信得过、
# 看不看得起),不是"我跟他有多亲近"。反过来写的话,一条八卦就能让人更喜欢一个
# 从没见过的人。


@dataclass(frozen=True)
class _AxisShares:
    """一组派生份额。`trust` 分涨跌两档,`respect` 带一道门槛。"""

    affection: float
    trust_gain: float
    trust_loss: float
    respect: float
    respect_floor: float   # |delta| / MAX_DELTA 要过这条线,敬重才开始动


CHAT_SHARES = _AxisShares(
    affection=0.80, trust_gain=0.35, trust_loss=0.90,
    respect=0.50, respect_floor=0.40,
)

HEARSAY_SHARES = _AxisShares(
    affection=0.20, trust_gain=0.70, trust_loss=0.95,
    respect=0.60, respect_floor=0.25,
)


def derive_axes(delta: float, shares: _AxisShares = CHAT_SHARES) -> dict[str, float]:
    """从 headline 派生三轴。**确定性** —— 不掷骰子,世界的可重放性靠这条。

    `delta` 已经被 `_clamp` 收在 ±`MAX_DELTA` 内,所以 `m` 是"这次互动有多重"
    的一个 [0, 1] 刻度。派生对 delta 是线性的(respect 那道门槛之外),于是
    "整段历史攒下来的轴 = 份额 × 攒下来的 sentiment",这让它的后果算得出来。
    """
    try:
        value = float(delta)
    except (TypeError, ValueError):
        value = 0.0
    weight = min(1.0, abs(value) / MAX_DELTA)
    trust_share = shares.trust_gain if value >= 0 else shares.trust_loss
    floor = min(0.99, max(0.0, shares.respect_floor))
    ramp = 0.0 if weight <= floor else min(1.0, (weight - floor) / (1.0 - floor))

    def axis(share: float) -> float:
        # `+ 0.0` 是在杀 `-0.0`:它进事件日志、进 `.cyberworld`,而一个读日志的人
        # 看到 `"respect": -0.0` 只会以为是别处算错了。数值上没差别,可读性上有。
        return round(_clamp(value * share), 4) + 0.0

    return {
        "trust": axis(trust_share),
        "affection": axis(shares.affection),
        "respect": axis(shares.respect * ramp),
    }


def _axes_or_derived(
    given: dict[str, float], delta: float, shares: _AxisShares
) -> tuple[dict[str, float], bool]:
    """`(要落库的三轴, 是不是派生出来的)`。

    **回落的粒度是「这一路」,不是「这一个轴」。** 模型只提了 trust 的时候不去
    补另外两个,因为补出来的那两个和它给的那一个不是同一次判断、也不是同一个
    量级 —— 拼在一起的那份东西没有任何人做出过。模型开了口就整份听它的;
    一个轴都没落地才派生。(显式的 `"respect": 0` 是开口,不是沉默。)
    """
    if given:
        return given, False
    return derive_axes(delta, shares), True


class RelationshipJudge:
    """Builds the judgment prompt, calls the LLM, validates the verdict."""

    def __init__(
        self,
        llm: Any,
        prompt_store: Any | None = None,
        health: Callable[[str, bool, str], None] | None = None,
    ) -> None:
        self._llm = llm
        self._prompt_store = prompt_store
        # **降级不许无声。** 模型不吐三轴时判定器自己派生一份 —— 那是回落,
        # 不是判断,所以它要和 planner / narrative 一样在 `subsystem_health`
        # 里看得见。`health` 就是 `Scheduler.note_subsystem` 的形状
        # (`__main__.build_serve_scheduler` 接上);没接上就退回日志。
        self.health = health
        self._axes_mode: str | None = None
        self.axes_native = 0     # 模型自己吐了三轴的次数
        self.axes_derived = 0    # 判定器替它派生的次数

    def _note_axes(self, derived: bool) -> None:
        """记一次三轴的来路,并在**档位切换的那一刻**吭一声。

        边沿触发,和 `Scheduler.note_subsystem` 逐字同一条理由:一个持续降级的
        子系统每次都刷一行的话,日志会被自己的健康报告淹掉。
        """
        if derived:
            self.axes_derived += 1
        else:
            self.axes_native += 1
        reason = (
            "模型没给 trust/affection/respect，三轴按 headline 派生"
            if derived else ""
        )
        if self.health is not None:
            self.health("relationship_axes", not derived, reason)
        mode = "derived" if derived else "native"
        if self._axes_mode == mode:
            return
        self._axes_mode = mode
        if derived:
            logger.warning("relationship_axes degraded: %s", reason)
        else:
            logger.info("relationship_axes: 模型自己在吐三轴了")

    def judge(
        self,
        a: dict[str, Any],
        b: dict[str, Any],
        relation: dict[str, Any],
        memories_a: list[str],
        memories_b: list[str],
        location: str,
    ) -> JudgeResult | None:
        template = _DEFAULT_PROMPT
        if self._prompt_store is not None:
            template = self._prompt_store.get("judge.relationship", default=_DEFAULT_PROMPT)

        variables = {
            "a_name": a.get("name", "甲"),
            "a_personality": a.get("personality", ""),
            "a_goals": "；".join(a.get("goals") or []) or "（无特别目标）",
            "b_name": b.get("name", "乙"),
            "b_personality": b.get("personality", ""),
            "b_goals": "；".join(b.get("goals") or []) or "（无特别目标）",
            "a_to_b": relation.get("a_to_b", 0.0),
            "b_to_a": relation.get("b_to_a", 0.0),
            "r_type": relation.get("r_type", "acquaintance"),
            "r_type_back": relation.get("r_type_back", "acquaintance"),
            "a_memories": "；".join(memories_a) or "（无）",
            "b_memories": "；".join(memories_b) or "（无）",
            "location": location or "（未知地点）",
        }
        try:
            prompt = template.format(**variables)
        except (KeyError, IndexError, ValueError):
            logger.warning("judge.relationship template failed to render; using the default")
            prompt = _DEFAULT_PROMPT.format(**variables)

        return self._verdict(prompt)

    def _verdict(self, prompt: str) -> JudgeResult | None:
        """Call the LLM and parse/clamp one JudgeResult; None on any failure."""
        try:
            reply = self._llm.complete_sync([{"role": "user", "content": prompt}])
        except Exception:  # noqa: BLE001 - a dead LLM must not stop the world
            logger.warning("relationship judge LLM call failed", exc_info=True)
            return None

        data = _extract_json_obj(reply or "")
        if not isinstance(data, dict):
            logger.warning("relationship judge produced no usable JSON; dropping")
            return None
        summary = data.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return None
        try:
            delta_ab = _clamp(data["delta_a_to_b"])
            delta_ba = _clamp(data["delta_b_to_a"])
        except (KeyError, TypeError, ValueError):
            return None
        # 模型不吐三轴时自己派生一份。**回落不是覆盖** —— 它开了口就整份听它的。
        axes_ab, fell_back_ab = _axes_or_derived(
            _clamp_axes(data.get("axes_a_to_b")), delta_ab, CHAT_SHARES)
        axes_ba, fell_back_ba = _axes_or_derived(
            _clamp_axes(data.get("axes_b_to_a")), delta_ba, CHAT_SHARES)
        self._note_axes(fell_back_ab or fell_back_ba)
        return JudgeResult(
            summary=summary.strip(),
            delta_a_to_b=delta_ab,
            delta_b_to_a=delta_ba,
            axes_a_to_b=axes_ab,
            axes_b_to_a=axes_ba,
        )

    def judge_user(
        self,
        a: dict[str, Any],
        player_name: str,
        relation: dict[str, Any],
        transcript: str,
        location: str,
    ) -> JudgeResult | None:
        """Verdict on a PLAYER conversation (player-visitor D3): unlike the
        NPC-NPC judge this reads the real transcript — no inference needed —
        and the prompt warns against flattery outscoring sincerity (the player
        has motives; the deltas stay clamped and damped regardless)."""
        template = _DEFAULT_USER_JUDGE_PROMPT
        if self._prompt_store is not None:
            template = self._prompt_store.get("judge.user_relationship", default=_DEFAULT_USER_JUDGE_PROMPT)
        variables = {
            "a_name": a.get("name", "甲"),
            "a_personality": a.get("personality", ""),
            "player_name": player_name or "访客",
            "a_to_b": relation.get("a_to_b", 0.0),
            "b_to_a": relation.get("b_to_a", 0.0),
            "r_type": relation.get("r_type", "初次见面的访客"),
            "transcript": transcript or "（无内容）",
            "location": location or "（未知地点）",
        }
        try:
            prompt = template.format(**variables)
        except (KeyError, IndexError, ValueError):
            logger.warning("judge.user_relationship template failed to render; using the default")
            prompt = _DEFAULT_USER_JUDGE_PROMPT.format(**variables)
        return self._verdict(prompt)

    def judge_hearsay(
        self,
        a: dict[str, Any],
        rumor: str,
        roster: dict[str, float],
        memories: list[str],
        location: str,
        known: Iterable[str] | None = None,
    ) -> HearsayVerdict | None:
        """她听到一句闲话之后的反应(吃醋那一条)。

        `roster` 是**她认识的人 → 她此刻对他的好感度**,按名字 —— 这是给模型看的
        那一半。`known` 是**这个世界里真有这么个人**的全集(不给就退回 `roster`),
        这是闸的那一半。让模型自由指认第三方的话,它编出的名字会翻不回任何 id,
        而"翻不回去就静默丢弃"和"根本没判定"在产物上一模一样 —— 于是这一层坏掉
        的样子,和它没接上长得完全相同。

        ⚠️ **两半必须分开,合成一个的话关系永远生不出来。** 线上真踩:她听到一句
        关于林迟的闲话,而她跟林迟还没来往过 —— 于是林迟不在她的 `roster` 里,
        整条反应被丢掉,日志上写的是「林迟不在名单上」,**而林迟就在这个世界里
        站着**(id `chi`)。合着的时候这一层只能让**已经认识的人**之间的关系动,
        一段关系永远不可能**从一句闲话里长出来** —— 而"我还没见过他,但我已经
        听说了他的事"恰恰是这个品类里最值钱的那一刻。
        闸一个字没松:编出来的名字照旧翻不回 id,照旧当场丢掉。

        `None` 表示这次判定没产出可用的东西(模型挂了、回包读不懂);和空的
        `reactions`(她听了不在乎)**是两件事**,调用方要分得开:前者要吭声,
        后者是正常世界里最常见的结果。
        """
        template = _DEFAULT_HEARSAY_PROMPT
        if self._prompt_store is not None:
            template = self._prompt_store.get("judge.hearsay", default=_DEFAULT_HEARSAY_PROMPT)
        variables = {
            "a_name": a.get("name", "甲"),
            "a_personality": a.get("personality", ""),
            "memories": "；".join(memories) or "（无）",
            "roster": "\n".join(f"- {name}：{value:+.2f}" for name, value in roster.items())
            or "-（他跟谁都还没什么来往）",
            "rumor": rumor or "（没说什么）",
            "location": location or "（未知地点）",
        }
        try:
            prompt = template.format(**variables)
        except (KeyError, IndexError, ValueError):
            logger.warning("judge.hearsay template failed to render; using the default")
            prompt = _DEFAULT_HEARSAY_PROMPT.format(**variables)

        try:
            reply = self._llm.complete_sync([{"role": "user", "content": prompt}])
        except Exception:  # noqa: BLE001 - a dead LLM must not stop the world
            logger.warning("hearsay judge LLM call failed", exc_info=True)
            return None
        data = _extract_json_obj(reply or "")
        if not isinstance(data, dict):
            logger.warning("hearsay judge produced no usable JSON; dropping")
            return None
        summary = data.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return None

        real = set(known) if known is not None else set(roster)
        reactions: list[HearsayReaction] = []
        for item in data.get("reactions") or ():
            if not isinstance(item, dict):
                continue
            about = str(item.get("about") or "").strip()
            if about not in real:        # 这个世界里没这么个人:模型编的,丢掉
                if about:
                    logger.warning(
                        "hearsay judge named %r, who is nobody in this world", about)
                continue
            try:
                delta = _clamp(item.get("delta"))
            except (TypeError, ValueError):
                continue
            if abs(delta) < 0.01:
                continue                 # 判了个 0 等于没判 —— 别为它发一条事件
            axes, fell_back = _axes_or_derived(
                _clamp_axes(item.get("axes")), delta, HEARSAY_SHARES)
            self._note_axes(fell_back)
            reactions.append(HearsayReaction(about=about, delta=delta, axes=axes))
            if len(reactions) >= _MAX_HEARSAY_REACTIONS:
                break
        return HearsayVerdict(summary=summary.strip(), reactions=tuple(reactions))

    def judge_invite(
        self,
        a: dict[str, Any],
        inviter: str,
        invitation: str,
        relation: dict[str, Any],
        memories: list[str],
        location: str,
        recent_talk: Sequence[str] = (),
    ) -> InviteVerdict | None:
        """有人叫她一起做件事,她答不答应(一起做事那条)。

        `None` 表示这次判定没产出可用的东西(模型挂了、回包读不懂)——
        和 `accept=False`(她不想去)**是两件事**,调用方要分得开:前者要吭声并
        退回 `together.decide_alone`,后者是这个世界里最正常的一种结果。

        ⚠️ **这条路上没有"世界说不行"**。同地、睡没睡、手上有没有事、做不做得了,
        全在调用方那一段(`Scheduler.joint_gate`)判完了 —— 拿一个睡着的人去问
        模型"你想不想去",它一定给得出一句像话的回答,而那句话是编的。

        `recent_talk` 是他和邀请人**这会儿正说着的话**,和 `memories` 不重复:
        记忆是会话关闭那一刻才落的,而邀请几乎总发生在会话中间 —— 只给记忆的话,
        判定器判的是一个「我不认识这个人」的处境,而他们刚聊了两轮。
        """
        template = _DEFAULT_INVITE_PROMPT
        if self._prompt_store is not None:
            template = self._prompt_store.get("judge.invite", default=_DEFAULT_INVITE_PROMPT)
        variables = {
            "a_name": a.get("name", "他"),
            "a_personality": a.get("personality", ""),
            "inviter": inviter or "有人",
            "invitation": invitation or "（没说清楚）",
            "a_to_b": _closeness_phrase(float(relation.get("a_to_b", 0.0) or 0.0)),
            "memories": "；".join(memories) or "（无）",
            "location": location or "（未知地点）",
            # 自洽的一块:没内容就是空串,老模板没有这个占位符也照样渲染
            # (`str.format` 忽略多余 kwarg,和规划器的 `{situation}` 同一个套路)。
            "recent_talk": _talk_block(a.get("name", "他"), inviter, recent_talk),
        }
        try:
            prompt = template.format(**variables)
        except (KeyError, IndexError, ValueError):
            logger.warning("judge.invite template failed to render; using the default")
            prompt = _DEFAULT_INVITE_PROMPT.format(**variables)
        try:
            reply = self._llm.complete_sync([{"role": "user", "content": prompt}])
        except Exception:  # noqa: BLE001 - a dead LLM must not stop the world
            logger.warning("invite judge LLM call failed", exc_info=True)
            return None
        data = _extract_json_obj(reply or "")
        if not isinstance(data, dict) or "accept" not in data:
            logger.warning("invite judge produced no usable JSON; falling back")
            return None
        raw = data.get("accept")
        if isinstance(raw, str):
            # 模型写 `"accept": "true"` 的概率不低,而按类型严格拒绝的话,这一次
            # 判定退回确定性那条路 —— 她的性格白读了,而且看不出来。
            lowered = raw.strip().lower()
            if lowered in ("true", "yes", "是", "答应", "1"):
                raw = True
            elif lowered in ("false", "no", "否", "不", "0"):
                raw = False
            else:
                return None
        if not isinstance(raw, bool):
            return None
        reason = data.get("reason")
        return InviteVerdict(
            accept=raw,
            reason=str(reason).strip()[:60] if isinstance(reason, str) else "",
        )

    def relabel(
        self,
        old_r_type: str,
        old_band: str,
        new_band: str,
        a: dict[str, Any],
        b: dict[str, Any],
        memories: list[str],
    ) -> str | None:
        """One short Chinese phrase re-describing a→b's relationship after a
        band crossing (relationship-stage-machine D5). r_type is free-form
        authored text — a mechanical band label would destroy it, so the LLM
        rewrites it in character. Any failure returns None and the old label
        stands: the worst case is the pre-change world where r_type froze."""
        template = _DEFAULT_RELABEL_PROMPT
        if self._prompt_store is not None:
            template = self._prompt_store.get("judge.relabel", default=_DEFAULT_RELABEL_PROMPT)
        variables = {
            "a_name": a.get("name", "甲"),
            "a_personality": a.get("personality", ""),
            "b_name": b.get("name", "乙"),
            "b_personality": b.get("personality", ""),
            "old_r_type": old_r_type or "acquaintance",
            "old_band": old_band,
            "new_band": new_band,
            "memories": "；".join(memories) or "（无）",
        }
        try:
            prompt = template.format(**variables)
        except (KeyError, IndexError, ValueError):
            logger.warning("judge.relabel template failed to render; using the default")
            prompt = _DEFAULT_RELABEL_PROMPT.format(**variables)
        try:
            reply = self._llm.complete_sync([{"role": "user", "content": prompt}])
        except Exception:  # noqa: BLE001 - a dead LLM must not stop the world
            logger.warning("relabel LLM call failed", exc_info=True)
            return None
        lines = [ln.strip() for ln in (reply or "").splitlines() if ln.strip()]
        if not lines:
            return None
        label = lines[0].strip("\"'“”‘’` ")
        return label[:_RELABEL_MAX_CHARS] or None


class DeterministicRelationshipJudge:
    """没有 LLM 时的判定器:让关系机制照常运转,而不是整体消失。

    **为什么需要它。** 没配 key 是默认状态,而 LLM 缺席时 `RelationshipJudge`
    每次都拿到一句无法解析的 Mock 回复、每次都返回 None。后果不是"关系变化小
    一点",而是**关系数据一条都不产生** —— 跨不了档、不生 relation_shift 记忆、
    不长图谱边、没有八卦源、planner 也读不到。三轴关系在 REFERENCE 里写的是
    「常开、不受开关影响」,而默认状态下它产出零条事件。这不是降级,是功能在
    默认状态下静默消失(见 core issue:玩家对话是否参与世界演化)。

    引擎里已经有同一条先例:`__main__` 在 mock 档给反思器装了一个确定性的
    一行式实现,好让反思离线也能跑、也能测。判定器照办。

    **漂移规则:按剩余空间衰减。** `delta = STEP × (1 - |当前值|)`,所以
    - 确定性,不掷骰子(世界的可重放性不能靠随机数);
    - 越熟越难再进一步,渐近而永不饱和 —— 反复寒暄不会把一段关系顶到 +1.0;
    - 与真判定同量级(远小于 ±0.2 的上限),日内重复还要再吃 0.5^(N-1) 阻尼。

    它**不假装是判断**:方向恒为正(聊过的人彼此稍微熟一点),幅度只看剩余
    空间,不看说了什么。要真正的判断,配一个 key。
    """

    STEP = 0.04

    def _drift(self, current: Any) -> float:
        try:
            value = float(current)
        except (TypeError, ValueError):
            value = 0.0
        return round(_clamp(self.STEP * (1.0 - min(1.0, abs(value)))), 4)

    def _result(self, summary: str, a_to_b: Any, b_to_a: Any) -> JudgeResult:
        forward, backward = self._drift(a_to_b), self._drift(b_to_a)
        # 三轴走**和真判定器同一份**派生规则(`derive_axes`)。此前这里自己写了
        # 一份(`{"trust": d/2, "affection": d, "respect": 0}`),而真判定器那边
        # 一份都没有 —— 于是配没配 key 的世界长成两个形状,而且**掩护了那条
        # bug**:所有测试跑在这个替身上,三轴看上去一直在动。
        return JudgeResult(
            summary=summary,
            delta_a_to_b=forward,
            delta_b_to_a=backward,
            axes_a_to_b=derive_axes(forward, CHAT_SHARES),
            axes_b_to_a=derive_axes(backward, CHAT_SHARES),
        )

    def judge(
        self,
        a: dict[str, Any],
        b: dict[str, Any],
        relation: dict[str, Any],
        memories_a: list[str],
        memories_b: list[str],
        location: str,
    ) -> JudgeResult:
        a_name, b_name = a.get("name", "甲"), b.get("name", "乙")
        where = f"在{location}" if location else ""
        return self._result(
            f"{a_name}和{b_name}{where}说了会儿话",
            relation.get("a_to_b", 0.0), relation.get("b_to_a", 0.0),
        )

    def judge_user(
        self,
        a: dict[str, Any],
        player_name: str,
        relation: dict[str, Any],
        transcript: str,
        location: str,
    ) -> JudgeResult:
        a_name = a.get("name", "甲")
        where = f"在{location}" if location else ""
        return self._result(
            f"{a_name}{where}和{player_name or '访客'}聊了一段",
            relation.get("a_to_b", 0.0), relation.get("b_to_a", 0.0),
        )

    def judge_invite(self, *_args: Any, **_kwargs: Any) -> None:
        """始终 None —— 调用方退回 `together.decide_alone`。

        这里**故意**不给一个确定性替身,而 `judge` 那一格给了 —— 差别不在难易,
        在**替身归谁写**:好感度漂移的替身只能由引擎写(它是引擎自己的机制),
        而"他肯不肯"的替身早就写好了,写在**世界里** —— 关系有多近、作者给他
        声明的「随和」是多少、他上一轮对这个人的姿态是什么。

        所以这一格返回 `None` 不是缺席,是**把判断让回给那三样**(全文见
        `together.py` 的模块说明)。在这儿再写一份"关系够近就答应",就成了引擎
        手里的第二份判断,而两份判断迟早给出不同的答案。
        """
        return None

    def judge_hearsay(self, *_args: Any, **_kwargs: Any) -> None:
        """始终 None —— 没有模型时**她听过就算了**,这也是设计好的下限。

        和 `relabel` 同一条理由,而且更硬:好感度漂移有个像样的替身(方向恒为正、
        幅度只看剩余空间),因为"聊过的人彼此稍微熟一点"这件事**与说了什么无关**,
        所以一个不看内容的近似还站得住。吃醋正相反 —— 它的全部内容就是
        "**这个人**听到**这句话**的反应"。任何不看这两样的替身都只能是
        "听到关于亲近的人的八卦就扣 0.05",而那恰恰是这条机制存在要否定的东西:
        白霜和「零」会得到同一个数字,世界照跑,日志干净,而差别没了。

        宁可这一层在没配 key 的世界里整个缺席 —— 缺席看得见(`contact_stats` /
        `note_subsystem` 会点名),假装判断看不见。
        """
        return None

    def relabel(self, *_args: Any, **_kwargs: Any) -> None:
        """始终 None —— 旧 r_type 保持不变,这是设计好的下限。

        这里**故意**不给确定性替代品,与上面的漂移相反,理由是两者性质不同:
        好感度是一个数,机制要靠它继续走,给个小步长是合理的替身;而 r_type 是
        作者写的自由文本(「有点好奇的新面孔」这种),用一个机械标签盖掉它是
        **把有的东西换成更差的东西**,比让它冻住更糟。数值有像样的替身,散文没有。
        """
        return None
