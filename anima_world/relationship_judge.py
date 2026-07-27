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

    def relabel(self, *_args: Any, **_kwargs: Any) -> None:
        """始终 None —— 旧 r_type 保持不变,这是设计好的下限。

        这里**故意**不给确定性替代品,与上面的漂移相反,理由是两者性质不同:
        好感度是一个数,机制要靠它继续走,给个小步长是合理的替身;而 r_type 是
        作者写的自由文本(「有点好奇的新面孔」这种),用一个机械标签盖掉它是
        **把有的东西换成更差的东西**,比让它冻住更糟。数值有像样的替身,散文没有。
        """
        return None
