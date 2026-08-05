"""social-v5:小团体 —— friendship 边的连通分量。

刻意用最朴素的图算法(≥2 人的 friendship 连通分量),不用 LLM:
50 角色毫秒级,结果确定、可测试。小团体是派生缓存(随时可重算,
存储只为省重算),日切重算一次;存取在 `anima_world.redis_state.RedisCliqueStore`,
SQLite 版已随 world.db 层退役 —— 这里只剩纯算法。
"""

from __future__ import annotations

from typing import Any


def compute_cliques(edges: list[dict[str, Any]]) -> list[list[str]]:
    """friendship 边 → 按成员排序的连通分量列表(≥2 人)。确定性输出。"""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for edge in edges:
        if edge.get("predicate") != "friendship":
            continue
        a, b = edge.get("subject"), edge.get("object")
        if isinstance(a, str) and isinstance(b, str) and a and b:
            union(a, b)

    groups: dict[str, list[str]] = {}
    for node in parent:
        groups.setdefault(find(node), []).append(node)
    return sorted(
        (sorted(members) for members in groups.values() if len(members) >= 2),
        key=lambda m: m[0],
    )
