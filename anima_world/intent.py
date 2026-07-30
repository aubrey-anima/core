"""意图分类器 + 三条 handler(issue #16)。

在这之前,**用户的每条消息都被当成 in-character dialogue 处理**。于是:

- "让林素也进来" —— 用户在**导演场景**,角色只能"想象林素在场";林素本人不在
  agents 里,不走一 tick,世界里根本没有这个人在场。
- "以后叫我霜霜" —— 用户在**改对话本身的规则**,角色应一两轮就忘。

三类走三条不同的路:`dialogue` 照旧(不动),`style_adjust` 写 persona override
(按 (角色, 玩家) 永久),`narrative_direction` 交给 director —— 真改世界,通过
**世界事件流**让所有人看见,而不是往提示词里塞一句"想象林素在场"。

#15 是她的**出**(她能做什么),这里是她的**入**(她怎么听你说话);两个叠起来
才是"真 agent 化"。

**分类往 dialogue 上偏(开放问题 1)。** 两种错的代价不对称:该 narrative 判成
dialogue,你看着她"想象化"很别扭;该 dialogue 判成 narrative,你正说的话被吞掉,
只回一句系统确认。后者更贵,所以低置信度一律退回 dialogue,并且把置信度和退回
原因一起交出去 —— 降级不许无声。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

logger = logging.getLogger(__name__)

INTENTS = ("dialogue", "narrative_direction", "style_adjust")
DEFAULT_INTENT = "dialogue"
DEFAULT_MIN_CONFIDENCE = 0.6

DEFAULT_CLASSIFIER_PROMPT = (
    "你是一个意图分类器,不是角色。判断玩家这条消息属于哪一类,只输出 JSON。\n"
    "类别:\n"
    "- dialogue:玩家在和角色说话(提问、回应、闲聊、调情、争吵都算)\n"
    "- narrative_direction:玩家在导演场景,要求世界里的某个人做某事、出场或离场\n"
    "- style_adjust:玩家在改对话本身的规则(怎么称呼他、要不要括号描写、语气偏好)\n"
    "输出格式(不要解释、不要代码块):\n"
    '{{"intent": "...", "confidence": 0.0~1.0, "params": {{}}}}\n'
    "narrative_direction 的 params:"
    '{{"target": "被指挥的人的名字", "action": "come_here|leave|act", "detail": "要他做什么"}}\n'
    "style_adjust 的 params:"
    '{{"kind": "address_form|description_style|tone_preference|forbidden_topics|nickname_for_player",'
    ' "value": "具体规则"}}\n'
    "拿不准就判 dialogue 并给一个低 confidence。\n"
    "这个场景里在场的人:{present}\n"
    "最近的对话:\n{recent}"
)

# 一次分类的结果。`reason` 只在退回 dialogue 时有值 —— 它是"为什么按对话处理"。
@dataclass
class Intent:
    intent: str = DEFAULT_INTENT
    confidence: float = 0.0
    params: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "intent": self.intent,
            "confidence": round(float(self.confidence), 3),
        }
        if self.params:
            payload["params"] = dict(self.params)
        if self.reason:
            payload["reason"] = self.reason
        return payload


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_classification(text: str, *, min_confidence: float = DEFAULT_MIN_CONFIDENCE) -> Intent:
    """把分类器的回包收敛成一个 Intent。读不懂 = dialogue + 说明原因。"""
    raw = (text or "").strip()
    match = _JSON_BLOCK.search(raw)
    if not match:
        return Intent(reason="分类器没给出 JSON,按对话处理", raw=raw)
    try:
        loaded = json.loads(match.group(0))
    except ValueError:
        return Intent(reason="分类器的 JSON 解析失败,按对话处理", raw=raw)
    if not isinstance(loaded, dict):
        return Intent(reason="分类器给的不是对象,按对话处理", raw=raw)

    intent = str(loaded.get("intent") or "").strip()
    try:
        confidence = float(loaded.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    params = loaded.get("params")
    params = dict(params) if isinstance(params, dict) else {}

    if intent not in INTENTS:
        return Intent(
            confidence=confidence, params=params, raw=raw,
            reason=f"分类器报了一个不认识的类别 {intent!r},按对话处理",
        )
    if intent != DEFAULT_INTENT and confidence < min_confidence:
        return Intent(
            confidence=confidence, params=params, raw=raw,
            reason=f"意图不明({intent} 只有 {confidence:.2f}),按对话处理",
        )
    return Intent(intent=intent, confidence=confidence, params=params, raw=raw)


def build_classifier_messages(
    template: str,
    text: str,
    *,
    present: Sequence[str],
    recent: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    recent_text = "\n".join(
        f"{m.get('role', 'user')}: {m.get('content', '')}" for m in list(recent)[-5:]
    ) or "(没有)"
    system = template.format(
        present="、".join(present) or "(只有你们两个)",
        recent=recent_text,
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]


# ── director:narrative_direction 的兑现(v1)───────────────────────────────


@dataclass
class DirectorOutcome:
    """导演一次的结果。`text` 是给玩家的一句回话(v1 的拒绝也走这里)。"""

    ok: bool
    text: str
    detail: dict[str, Any] = field(default_factory=dict)


class Director:
    """把 narrative_direction 变成世界里真发生的事。

    **v1 只对已经存在的角色动手。** 不认识的人一律拒绝并说清楚下一步 —— 自然语言
    造人(`author_agent`)是 v2,那里要有每日上限、作者 opt-in、`authored_by_user`
    标记与冲突处理;没有那些守卫就开这道门,等于让一句话往世界里塞进不可回滚的人。

    关键:**不进提示词,进世界。** 让林素过来 = 一次真的行程事件,于是白霜下一次读
    `world_context` 时会真的看到"林素在场"。往提示词里塞"想象林素在场"正是要修的病。
    """

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def _resolve(self, target: str) -> str | None:
        target = str(target or "").strip()
        if not target:
            return None
        names = self._runtime.agent_names()
        if target in names:
            return target
        for agent_id, name in names.items():
            if str(name).strip() == target:
                return agent_id
        return None

    def direct(self, *, agent_id: str, params: dict[str, Any]) -> DirectorOutcome:
        target = str(params.get("target") or "").strip()
        action = str(params.get("action") or "come_here").strip()
        detail = str(params.get("detail") or "").strip()
        resolved = self._resolve(target)
        if resolved is None:
            # v1 的边界,而且要给出下一步(开放问题 2):纯拒绝会让人以为这条路坏了。
            return DirectorOutcome(
                ok=False,
                text=f"(我不认识{target or '这个人'}。这个世界里现在只有"
                     f"{'、'.join(self._runtime.agent_names().values())};"
                     f"要让新的人进来得先把 ta 造出来。)",
                detail={"target": target, "reason": "unknown_agent"},
            )
        if resolved == agent_id:
            return DirectorOutcome(
                ok=False,
                text="(这句是在指挥你正在说话的人本人 —— 直接跟她说就好。)",
                detail={"target": resolved, "reason": "target_is_speaker"},
            )

        here = self._runtime.agent_location(agent_id)
        name = self._runtime.agent_names().get(resolved, resolved)
        if action == "leave":
            options = [pid for pid in self._runtime.point_ids() if pid != here]
            if not options:
                return DirectorOutcome(
                    ok=False, text="(这个世界只有一个地方,走不掉。)",
                    detail={"target": resolved, "reason": "nowhere_to_go"},
                )
            moved = self._runtime.move_agent(resolved, options[0])
            return DirectorOutcome(
                ok=True, text=f"({name}离开了。)",
                detail={"target": resolved, "action": "leave", **moved},
            )
        if action == "act":
            if not detail:
                return DirectorOutcome(
                    ok=False, text="(要他做什么?说具体一点。)",
                    detail={"target": resolved, "reason": "empty_detail"},
                )
            self._runtime.emit({
                "type": "agent_action",
                "who": resolved,
                "loc": self._runtime.agent_location(resolved) or None,
                "payload": {"action": "directed", "detail": detail, "directed_by": "player"},
            })
            return DirectorOutcome(
                ok=True, text=f"({name}照做了。)",
                detail={"target": resolved, "action": "act", "detail": detail},
            )

        # come_here(缺省):把人真的挪到这场对话发生的地方
        if not here:
            return DirectorOutcome(
                ok=False, text="(不知道你们这会儿在哪,叫不过来。)",
                detail={"target": resolved, "reason": "unknown_location"},
            )
        if self._runtime.agent_location(resolved) == here:
            return DirectorOutcome(
                ok=True, text=f"({name}本来就在这儿。)",
                detail={"target": resolved, "action": "come_here", "already_here": True},
            )
        moved = self._runtime.move_agent(resolved, here)
        return DirectorOutcome(
            ok=True, text=f"({name}过来了。)",
            detail={"target": resolved, "action": "come_here", **moved},
        )
