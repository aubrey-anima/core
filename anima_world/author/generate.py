"""LLM-driven world seed generation — the creator surface's first real tool.

`generate_world_seed` asks any `LLMClientProtocol` for a complete world seed
(agents + nested-map locations) from a one-line concept, then holds it to the
same contract every other consumer of a seed relies on: `is_valid_world_seed`
plus referential integrity (every agent stands on a seeded location). One retry
carries the validation error back to the model; after that the failure is the
caller's to see (`SeedGenerationError`) — a creator tool must never silently
ship a broken world.
"""

from __future__ import annotations

import json
import re
from typing import Any

from anima_world.world_seed import is_valid_world_seed

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n|\n?```$", re.MULTILINE)

_PROMPT = """你是一个多智能体生活模拟世界的世界设计师。根据世界概念，生成一份世界种子 JSON。

世界概念：{concept}

严格输出一个 JSON 对象（不要任何解释文字），字段契约：
- "agents": {n_agents} 个角色，每个含 id（小写英文/数字/连字符）、name（中文名）、location（必须是下方某个地点的 id）、personality（80~160 字中文人设：身份、性格、一段往事、一个未了的心愿；角色之间要有暗线互相牵连）
- "locations": {n_locations} 个地点，每个含 id、name（中文）、description（一句中文场景描写）、kind（固定 "point"）、x、y（0~1 之间的小数，彼此错开）

只输出 JSON。"""

_RETRY_SUFFIX = """

你上一次的输出未通过校验：{error}
请修正后重新只输出 JSON 对象。"""


class SeedGenerationError(ValueError):
    """The LLM could not produce a valid world seed."""


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text.strip()).strip()


def _validate(seed: Any) -> str | None:
    """Return a human-readable defect, or None when the seed is sound."""
    if not is_valid_world_seed(seed):
        return "不满足最小种子契约（agents/locations 字段或键缺失）"
    location_ids = {loc["id"] for loc in seed["locations"]}
    for agent in seed["agents"]:
        if agent["location"] not in location_ids:
            return f"角色 {agent['id']} 站在未定义的地点 {agent['location']!r}"
    return None


async def generate_world_seed(
    llm: Any,
    concept: str,
    *,
    n_agents: int = 4,
    n_locations: int = 5,
) -> dict[str, Any]:
    """Generate a validated world seed from a concept via the given LLM client."""
    prompt = _PROMPT.format(concept=concept.strip(), n_agents=n_agents, n_locations=n_locations)
    error: str | None = None
    for _ in range(2):  # one attempt + one retry with error feedback
        content = prompt if error is None else prompt + _RETRY_SUFFIX.format(error=error)
        try:
            reply = await llm.complete([{"role": "user", "content": content}])
        except Exception as exc:  # noqa: BLE001 - a flaky LLM call is a retryable defect
            error = f"LLM 调用失败：{exc}"
            continue
        try:
            seed = json.loads(_strip_fences(str(reply)))
        except json.JSONDecodeError as exc:
            error = f"输出不是合法 JSON：{exc}"
            continue
        error = _validate(seed)
        if error is None:
            return seed
    raise SeedGenerationError(error or "LLM 未能生成合法的世界种子")
