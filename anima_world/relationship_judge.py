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
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# One chat may nudge a relationship, never swing it — an LLM having a
# dramatic moment must not do in one call what the novel took 50 chapters
# to do. The projection additionally clamps the accumulated value to [-1, 1].
MAX_DELTA = 0.2

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
    '"axes_a_to_b": {{"trust": <信任变化>, "affection": <喜爱变化>, "respect": <敬重变化>}}, '
    '"axes_b_to_a": {{"trust": …, "affection": …, "respect": …}}}}\n'
    "变化幅度要克制：一次寒暄通常只有 ±0.05 以内；只有触及双方在意的事才可能更大。"
    "关系糟糕的两个人客套一次不代表和解。只输出 JSON，不要解释。"
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
    '"axes_a_to_b": {{"trust": <信任变化>, "affection": <喜爱变化>, "respect": <敬重变化>}}, '
    '"axes_b_to_a": {{"trust": …, "affection": …, "respect": …}}}}\n'
    "变化要克制：寒暄通常 ±0.05 以内。刻意的讨好、奉承或索取不应比真诚的交流得到更多"
    "——按{a_name}的性格判断他吃不吃这一套。只输出 JSON，不要解释。"
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
    "只输出 JSON，不要解释。"
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
    "{a_name}眼下对{inviter}的观感：{a_to_b}（范围 0 到 1，0 是形同陌路）\n"
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
    # relations-v5: optional finer axes (trust/affection/respect deltas,
    # clamped like the headline). Empty dicts when the LLM omits them —
    # single-axis verdicts stay fully valid.
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


def _clamp_axes(value: Any) -> dict[str, float]:
    """relations-v5: keep only known axes with numeric values, clamped —
    garbage axes degrade to absent, never to an exception."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for axis in ("trust", "affection", "respect"):
        if axis in value:
            try:
                out[axis] = _clamp(value[axis])
            except (TypeError, ValueError):
                continue
    return out


class RelationshipJudge:
    """Builds the judgment prompt, calls the LLM, validates the verdict."""

    def __init__(self, llm: Any, prompt_store: Any | None = None) -> None:
        self._llm = llm
        self._prompt_store = prompt_store

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
        return JudgeResult(
            summary=summary.strip(),
            delta_a_to_b=delta_ab,
            delta_b_to_a=delta_ba,
            axes_a_to_b=_clamp_axes(data.get("axes_a_to_b")),
            axes_b_to_a=_clamp_axes(data.get("axes_b_to_a")),
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
    ) -> HearsayVerdict | None:
        """她听到一句闲话之后的反应(吃醋那一条)。

        `roster` 是**她认识的人 → 她此刻对他的好感度**,按名字。它同时是给模型的
        输入和一道闸:回包里名单外的人一律丢掉。让模型自由指认第三方的话,它编出
        的名字会翻不回任何 id,而"翻不回去就静默丢弃"和"根本没判定"在产物上一模
        一样 —— 于是这一层坏掉的样子,和它没接上长得完全相同。

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

        reactions: list[HearsayReaction] = []
        for item in data.get("reactions") or ():
            if not isinstance(item, dict):
                continue
            about = str(item.get("about") or "").strip()
            if about not in roster:      # 名单外的人:模型编的,丢掉
                if about:
                    logger.warning("hearsay judge named %r, who is not on the roster", about)
                continue
            try:
                delta = _clamp(item.get("delta"))
            except (TypeError, ValueError):
                continue
            if abs(delta) < 0.01:
                continue                 # 判了个 0 等于没判 —— 别为它发一条事件
            reactions.append(HearsayReaction(
                about=about, delta=delta, axes=_clamp_axes(item.get("axes")),
            ))
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
    ) -> InviteVerdict | None:
        """有人叫她一起做件事,她答不答应(一起做事那条)。

        `None` 表示这次判定没产出可用的东西(模型挂了、回包读不懂)——
        和 `accept=False`(她不想去)**是两件事**,调用方要分得开:前者要吭声并
        退回 `together.decide_alone`,后者是这个世界里最正常的一种结果。

        ⚠️ **这条路上没有"世界说不行"**。同地、睡没睡、手上有没有事、做不做得了,
        全在调用方那一段(`Scheduler.joint_gate`)判完了 —— 拿一个睡着的人去问
        模型"你想不想去",它一定给得出一句像话的回答,而那句话是编的。
        """
        template = _DEFAULT_INVITE_PROMPT
        if self._prompt_store is not None:
            template = self._prompt_store.get("judge.invite", default=_DEFAULT_INVITE_PROMPT)
        variables = {
            "a_name": a.get("name", "他"),
            "a_personality": a.get("personality", ""),
            "inviter": inviter or "有人",
            "invitation": invitation or "（没说清楚）",
            "a_to_b": f"{float(relation.get('a_to_b', 0.0) or 0.0):.2f}",
            "memories": "；".join(memories) or "（无）",
            "location": location or "（未知地点）",
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
        # 三轴跟着主轴走,量级递减:说过话最先长出的是喜爱与信任,敬重要靠别的。
        axes = lambda d: {"trust": round(d / 2, 4), "affection": d, "respect": 0.0}  # noqa: E731
        return JudgeResult(
            summary=summary,
            delta_a_to_b=forward,
            delta_b_to_a=backward,
            axes_a_to_b=axes(forward),
            axes_b_to_a=axes(backward),
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
