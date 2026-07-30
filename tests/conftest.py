"""测试共用的夹具。

**为什么需要一份"素配种子"。** 内置的 `world_seed.json` 是**产品橱窗**:它替这个
世界点亮了一堆开关(needs / economy / social / stance / tools / intent / autonomy)、
播了关系、记忆、钱和货架 —— 因为一个展示不了自己特性的内置世界没人会用。

但引擎测试里有一整类问的是**引擎自己的默认行为**:"开关不点亮时,行为逐 tick 和上
一版一致吗"、"创世安家费是多少"、"关系从零开始爬一档要多久"。拿橱窗世界去验这些,
验的其实是橱窗的布置,不是引擎的默认值 —— 而且橱窗以后每加一件展品,这些测试就会
莫名其妙地红一次。

所以这里把橱窗**剥回毛坯**:同样的角色、地点、作息(测试到处引用 `夏`/`cafe`),
但不带任何富化字段。要验橱窗本身的测试另有其人(`test_flagship_seed.py`)。
"""
from __future__ import annotations

import json
from importlib import resources

import pytest

# 橱窗独有的顶层字段 —— 剥掉它们就回到毛坯。
# **橱窗每加一件展品,这里就要跟一行** —— 不跟的话,一堆"验引擎默认行为"的测试会
# 因为世界变富了而莫名其妙地红(1.3.0 里已经发生过两次:先是 config/relations,
# 再是 stocks/rules)。
_SHOWCASE_TOP_LEVEL = (
    "config", "relations", "memories", "items",
    "stocks", "rules", "stock_visibility", "stock_places",
)
# 角色/地点身上的富化字段。
_SHOWCASE_AGENT_FIELDS = ("money", "inventory", "goals")
_SHOWCASE_LOCATION_FIELDS = ("stock",)


def _bundled_seed() -> dict:
    return json.loads(
        (resources.files("anima_world") / "world_seed.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def bare_seed(tmp_path) -> str:
    """内置世界剥掉富化之后的样子,返回种子文件路径。

    从**内置种子派生**而不是另写一份:角色 id、地点、作息一旦变了,这里跟着变,
    不会悄悄和被测世界脱节。
    """
    seed = _bundled_seed()
    for field in _SHOWCASE_TOP_LEVEL:
        seed.pop(field, None)
    for agent in seed.get("agents", []):
        for field in _SHOWCASE_AGENT_FIELDS:
            agent.pop(field, None)
    for location in seed.get("locations", []):
        for field in _SHOWCASE_LOCATION_FIELDS:
            location.pop(field, None)

    path = tmp_path / "bare_seed.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return str(path)
