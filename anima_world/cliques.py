"""social-v5:小团体 —— friendship 边的连通分量。

刻意用最朴素的图算法(≥2 人的 friendship 连通分量),不用 LLM:
50 角色毫秒级,结果确定、可测试。cliques 表是派生缓存(随时可重算,
存表只为省重算),日切重算一次。
"""

from __future__ import annotations

import json
import sqlite3
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


def store_cliques(conn: sqlite3.Connection, cliques: list[list[str]], tick: int) -> None:
    """全量重写派生缓存(它不是历史,重算即真相)。"""
    conn.execute("DELETE FROM cliques")
    for members in cliques:
        conn.execute(
            "INSERT INTO cliques (member_ids, computed_tick) VALUES (?, ?)",
            (json.dumps(members, ensure_ascii=False), tick),
        )
    conn.commit()


def load_cliques(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id, member_ids, computed_tick FROM cliques ORDER BY id").fetchall()
    return [
        {"id": int(r[0]), "member_ids": json.loads(r[1]), "computed_tick": int(r[2])}
        for r in rows
    ]
