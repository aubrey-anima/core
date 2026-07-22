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
from dataclasses import dataclass
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
    '"delta_b_to_a": <乙对甲好感变化，同上>}}\n'
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
    '"delta_b_to_a": <访客对{a_name}好感变化，同上>}}\n'
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
        return JudgeResult(summary=summary.strip(), delta_a_to_b=delta_ab, delta_b_to_a=delta_ba)

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
