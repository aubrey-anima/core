"""World definition stores: locations, action table, and BT structure in DB.

Data-plane principle: definition data (location geometry, the node→action
mapping, tree shape) lives in tables — seeded idempotently on first boot,
current value owned by the DB row (M5 "DB row wins"). The event log remains
the world's history; nothing here emits or replays events.

`LocationStore` owns the `locations` table — the map's single source of truth
(nested-map D7): an adjacency tree of regions and points with parent-relative
0~1 geometry. Nothing about the map goes through the event log.
`BTStore` owns `bt_actions` (the DB-backed `ActionTable`) and `bt_nodes`
(behavior tree shape, one row per node: children link via `parent`, ordered
by `sort`), and can rebuild live `bt_nodes.py` objects from those rows.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from anima_world.actions import ActionDescriptor, ActionTable
from anima_world.db import BT_NODE_TYPES
from anima_world.bt_nodes import (
    Action,
    Condition,
    NeedAction,
    Node,
    PlanAction,
    Selector,
    Sequence,
    Status,
    TimeWindow,
    default_bt,
)
from anima_world.world_time import parse_hhmm


logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_LOCATION_FIELDS = ("name", "description", "kind", "parent", "x", "y", "w", "h")

# Depth cap for tree assembly — dirty data (a cycle written via raw SQL) must
# degrade, never recurse forever.
_MAX_TREE_DEPTH = 32


class LocationStore:
    """SQLite-backed location definitions — the map's single source of truth.

    Rows form an adjacency tree: `parent` self-refs another row (NULL = top
    level), `kind` is 'region' (may nest) or 'point' (agents stand on these).
    Geometry is relative to the parent region, normalized 0~1. Access is
    serialized through `lock` (the scheduler's RLock in the server — same
    pattern as ChatStore).
    """

    def __init__(self, conn: sqlite3.Connection, lock: Any | None = None) -> None:
        self._conn = conn
        self._lock = lock if lock is not None else threading.RLock()

    def _rows(self, cur: sqlite3.Cursor) -> list[dict[str, Any]]:
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._rows(self._conn.execute("SELECT * FROM locations ORDER BY id"))

    def get(self, loc_id: str) -> dict[str, Any] | None:
        with self._lock:
            rows = self._rows(
                self._conn.execute("SELECT * FROM locations WHERE id = ?", (loc_id,))
            )
            return rows[0] if rows else None

    def tree(self) -> list[dict[str, Any]]:
        """Assemble the adjacency rows into nested dicts (each row gains a
        `children` list). Defensive: a row whose parent is dangling or part of
        a cycle is logged and treated as top-level, so hand-written SQL can
        never make the map un-renderable."""
        rows = {r["id"]: {**r, "children": []} for r in self.all()}
        roots: list[dict[str, Any]] = []
        for loc_id, row in rows.items():
            parent = row["parent"]
            if parent is None:
                roots.append(row)
                continue
            if parent not in rows:
                logger.warning("location %r has dangling parent %r — treating as top-level", loc_id, parent)
                roots.append(row)
                continue
            if self._ancestry_is_cyclic(loc_id, rows):
                logger.warning("location %r is part of a parent cycle — treating as top-level", loc_id)
                roots.append(row)
                continue
            rows[parent]["children"].append(row)
        return roots

    @staticmethod
    def _ancestry_is_cyclic(loc_id: str, rows: dict[str, dict[str, Any]]) -> bool:
        seen = {loc_id}
        cur = rows[loc_id]["parent"]
        for _ in range(_MAX_TREE_DEPTH):
            if cur is None or cur not in rows:
                return False
            if cur in seen:
                return True
            seen.add(cur)
            cur = rows[cur]["parent"]
        return True  # deeper than the cap — treat as cyclic rather than recurse

    def absolute_xy(self, loc_id: str) -> tuple[float, float] | None:
        """A location's position on the world canvas, in 0~1 absolute terms.

        Geometry is stored relative to the parent (nested-map D2), so walk up
        the parent chain composing each level's offset and scale. Pure: nothing
        is stored, which is why `distance` can never drift out of step with the
        map. Returns None when the chain is broken or a row has no coordinates —
        the caller then degrades (a walk without a distance is a teleport, i.e.
        the behavior that existed before travel time).
        """
        rows = {r["id"]: r for r in self.all()}
        row = rows.get(loc_id)
        if row is None or row.get("x") is None or row.get("y") is None:
            return None

        x, y = float(row["x"]), float(row["y"])
        parent = row["parent"]
        for _ in range(_MAX_TREE_DEPTH):
            if parent is None:
                return (x, y)
            prow = rows.get(parent)
            if prow is None or prow.get("x") is None or prow.get("y") is None:
                return None
            pw = float(prow.get("w") or 0.0)
            ph = float(prow.get("h") or 0.0)
            x = float(prow["x"]) + x * pw
            y = float(prow["y"]) + y * ph
            parent = prow["parent"]
        return None  # deeper than the cap — dirty data, degrade

    def distance(self, a: str, b: str) -> float | None:
        """Straight-line distance across the canvas between two locations.

        The map has no walls and no doors (nested-map D3 deleted `exits`), so a
        straight line IS the route — this is exactly the "use distance, don't
        add an edge table" call that nested-map discovery D8 made. None when
        either end can't be placed; the caller falls back to instant travel.
        """
        pa, pb = self.absolute_xy(a), self.absolute_xy(b)
        if pa is None or pb is None:
            return None
        return math.hypot(pa[0] - pb[0], pa[1] - pb[1])

    def _validate(self, loc_id: str, merged: dict[str, Any]) -> None:
        kind = merged.get("kind") or "point"
        if kind not in ("region", "point"):
            raise ValueError(f"unknown location kind: {kind!r}")
        if kind == "point" and (merged.get("w") is not None or merged.get("h") is not None):
            raise ValueError(f"point {loc_id!r} must not carry region geometry (w/h)")

        parent = merged.get("parent")
        if parent is None:
            return
        if parent == loc_id:
            raise ValueError(f"location {loc_id!r} cannot be its own parent")
        prow = self.get(parent)
        if prow is None:
            raise ValueError(f"unknown parent location: {parent!r}")
        if prow["kind"] != "region":
            raise ValueError(f"parent {parent!r} is a point; only regions may contain locations")
        # Walk up from the prospective parent: reaching loc_id means a cycle.
        cur, hops = prow["parent"], 0
        while cur is not None and hops < _MAX_TREE_DEPTH:
            if cur == loc_id:
                raise ValueError(f"parent {parent!r} would make {loc_id!r} its own ancestor")
            row = self.get(cur)
            if row is None:
                return
            cur, hops = row["parent"], hops + 1

    def upsert(self, loc_id: str, **fields: Any) -> None:
        """Create or partially update a location; omitted fields are untouched.
        Invalid hierarchy (unknown/point parent, cycle) or a point carrying
        region geometry raises and leaves the table unchanged."""
        unknown = set(fields) - set(_LOCATION_FIELDS)
        if unknown:
            raise ValueError(f"unknown location fields: {sorted(unknown)}")
        with self._lock:
            existing = self.get(loc_id)
            base: dict[str, Any] = existing or {
                "name": loc_id, "description": "", "kind": "point",
                "parent": None, "x": None, "y": None, "w": None, "h": None,
            }
            merged = {**base, **fields}
            self._validate(loc_id, merged)
            values = tuple(merged[f] for f in _LOCATION_FIELDS)
            if existing is None:
                cols = ", ".join(_LOCATION_FIELDS)
                marks = ", ".join("?" for _ in _LOCATION_FIELDS)
                self._conn.execute(
                    f"INSERT INTO locations (id, {cols}, updated_at) VALUES (?, {marks}, ?)",
                    (loc_id, *values, _now()),
                )
            else:
                assignments = ", ".join(f"{f}=?" for f in _LOCATION_FIELDS)
                self._conn.execute(
                    f"UPDATE locations SET {assignments}, updated_at=? WHERE id=?",
                    (*values, _now(), loc_id),
                )
            self._conn.commit()

    def seed_defaults(self, entries: list[dict[str, Any]]) -> None:
        """Insert `entries` only when the table is empty — a populated table
        is user data and must never be clobbered by re-seeding. Parents are
        seeded before their children so hierarchy validation can see them."""
        with self._lock:
            (count,) = self._conn.execute("SELECT COUNT(*) FROM locations").fetchone()
            if count:
                return
            for e in _parents_first(entries):
                self.upsert(
                    e["id"],
                    **{f: e[f] for f in _LOCATION_FIELDS if f in e},
                )


def _parents_first(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order seed entries so every parent precedes its children (the seed file
    is hand-written; it must not have to be topologically sorted by hand)."""
    by_id = {e["id"]: e for e in entries}
    ordered: list[dict[str, Any]] = []
    placed: set[str] = set()

    def place(entry: dict[str, Any], depth: int = 0) -> None:
        if entry["id"] in placed or depth > _MAX_TREE_DEPTH:
            return
        parent = entry.get("parent")
        if parent is not None and parent in by_id and parent not in placed:
            place(by_id[parent], depth + 1)
        placed.add(entry["id"])
        ordered.append(entry)

    for e in entries:
        place(e)
    return ordered


class BTStore:
    """SQLite-backed action table (`bt_actions`) + tree shape (`bt_nodes`)."""

    def __init__(self, conn: sqlite3.Connection, lock: Any | None = None) -> None:
        self._conn = conn
        self._lock = lock if lock is not None else threading.RLock()

    # ── action table ────────────────────────────────────────────────────────

    def actions(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute("SELECT node_id, kind, params FROM bt_actions ORDER BY node_id")
            return [
                {"node_id": n, "kind": k, "params": json.loads(p or "{}")}
                for n, k, p in cur.fetchall()
            ]

    def set_action(self, node_id: str, kind: str, params: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO bt_actions (node_id, kind, params, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(node_id) DO UPDATE SET kind=excluded.kind, "
                "params=excluded.params, updated_at=excluded.updated_at",
                (node_id, kind, json.dumps(params or {}, ensure_ascii=False), _now()),
            )
            self._conn.commit()

    def action_table(self) -> ActionTable:
        """Build a live `ActionTable` from the rows (lookup fallback stays
        `idle_wander`, as in `ActionTable.lookup`)."""
        return ActionTable(
            {
                r["node_id"]: ActionDescriptor(r["kind"], r["params"])
                for r in self.actions()
            }
        )

    # ── tree structure ──────────────────────────────────────────────────────

    def add_node(
        self,
        tree: str,
        node_id: str,
        node_type: str,
        parent: str | None,
        sort: int = 0,
        params: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO bt_nodes (tree, node_id, type, parent, sort, params) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tree, node_id) DO UPDATE SET type=excluded.type, "
                "parent=excluded.parent, sort=excluded.sort, params=excluded.params",
                (tree, node_id, node_type, parent, sort, json.dumps(params or {}, ensure_ascii=False)),
            )
            self._conn.commit()

    def build_tree(self, tree: str = "default") -> Node:
        """Rebuild a `bt_nodes.py` tree from rows.

        bt-duties D5: each agent has its own tree (`tree == agent_id`). A tree
        with no rows falls back to the shared `"default"` tree, and a missing
        or structurally broken `"default"` falls back to the built-in
        `default_bt()` — a bad tree definition must never leave an agent
        brainless (worst case it behaves exactly like today: idle_wander).
        """
        rows = self._tree_rows(tree)
        if not rows and tree != "default":
            logger.warning("no BT rows for tree %r — falling back to the 'default' tree", tree)
            rows = self._tree_rows("default")
        roots = [r for r in rows if r["parent"] is None]
        if len(roots) != 1:
            return default_bt()
        children: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            if r["parent"] is not None:
                children.setdefault(r["parent"], []).append(r)
        try:
            return self._build_node(roots[0], children)
        except ValueError as exc:
            # 名字要说出来。塌回默认树 = 整个世界的行为被换掉,而"unknown node
            # type"这五个字不告诉作者去改哪一行。
            logger.warning(
                "BT tree %r 用不了(%s)—— 整棵树已回退到 default_bt(),"
                "这个世界的角色现在只会 idle_wander",
                tree, exc,
            )
            return default_bt()

    def _tree_rows(self, tree: str) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT node_id, type, parent, sort, params FROM bt_nodes WHERE tree = ? ORDER BY sort",
                (tree,),
            )
            return [
                {"node_id": n, "type": t, "parent": p, "sort": s, "params": json.loads(pr or "{}")}
                for n, t, p, s, pr in cur.fetchall()
            ]

    def _build_node(
        self, row: dict[str, Any], children: dict[str, list[dict[str, Any]]]
    ) -> Node:
        node_type = row["type"]
        if node_type == "action":
            return Action(lambda bb: Status.SUCCESS, row["node_id"])
        if node_type == "condition":
            p = row["params"]
            return Condition(p.get("key", ""), p.get("expected"))
        if node_type == "time_window":
            p = row["params"]
            return TimeWindow(int(p.get("start", 0)), int(p.get("end", 0)))
        if node_type == "plan":
            return PlanAction()
        if node_type == "need_action":
            # `bt_nodes` 的 CHECK 一直放行这个类型,而这里一直不认它 —— 于是作者
            # 写一行完全合法的 SQL,`_build_node` 抛 ValueError,调用方兜成一行
            # warning,**整棵作者树塌回 default_bt()**(只会 idle_wander)。
            # 世界照跑,角色什么也不干。
            p = row["params"]
            release = p.get("release")
            return NeedAction(
                str(p.get("need", "")),
                float(p.get("threshold", 0.0)),
                str(p.get("action_id", "")),
                None if release is None else float(release),
            )
        kids = [self._build_node(c, children) for c in children.get(row["node_id"], [])]
        if node_type == "selector":
            return Selector(kids)
        if node_type == "sequence":
            return Sequence(kids)
        raise ValueError(f"unknown bt node type: {node_type}")

    # ── seeding ─────────────────────────────────────────────────────────────

    def seed_defaults(self, agent_ids: list[str], location_ids: list[str]) -> None:
        """Seed the action table from the live roster (`go_to_<loc>` per
        location, `chat_with_<agent>` per agent — no hardcoded ghosts) plus
        the fixed kinds, and the 'default' tree (root selector → idle_wander,
        byte-for-byte the old `default_bt()` behavior). Empty-table-only,
        same contract as `LocationStore.seed_defaults`."""
        with self._lock:
            (count,) = self._conn.execute("SELECT COUNT(*) FROM bt_actions").fetchone()
            if not count:
                for loc in location_ids:
                    self.set_action(f"go_to_{loc}", "walk", {"location": loc})
                for aid in agent_ids:
                    self.set_action(f"chat_with_{aid}", "chat", {"target": aid})
                self.set_action("do_work", "work", {"location": "workshop"})
                self.set_action("go_sleep", "sleep", {})
                self.set_action("idle_wander", "idle_wander", {})
                self.set_action("idle_social", "idle_social", {})
            (count,) = self._conn.execute("SELECT COUNT(*) FROM bt_nodes").fetchone()
            if not count:
                self.add_node("default", "root", "selector", parent=None, sort=0)
                self.add_node("default", "idle_wander", "action", parent="root", sort=0)

    def seed_tree(self, agent_id: str, nodes: list[dict[str, Any]]) -> bool:
        """用种子里写死的节点表播一棵树(`agents[].behavior_tree`)。

        `duties` 只能表达"时间窗 → 动作"这一种形状 —— 作者要写条件分支、需求带、
        嵌套选择器,就够不着了。这条路把 `bt_nodes` 的表达力整个交出去。

        规矩与其它播种一致:**空表才播**(手改过的树是用户数据),**坏条目跳过并
        警告、绝不阻塞开机**(坏种子让世界少一个分支,不该让世界开不了机)。
        返回是否真的播了 —— 调用方据此决定要不要退回 `duties`。
        """
        if not nodes:
            return False
        with self._lock:
            (count,) = self._conn.execute(
                "SELECT COUNT(*) FROM bt_nodes WHERE tree = ?", (agent_id,)
            ).fetchone()
            if count:
                return True  # 已有树:视作"种子这条路走过了",别再叠 duties 上去

            planted = 0
            for index, node in enumerate(nodes):
                try:
                    node_type = str(node["type"])
                    if node_type not in BT_NODE_TYPES:
                        raise ValueError(f"unknown node type {node_type!r}")
                    self.add_node(
                        agent_id,
                        str(node["node_id"]),
                        node_type,
                        parent=node.get("parent"),
                        sort=int(node.get("sort", index)),
                        params=dict(node.get("params") or {}),
                    )
                    planted += 1
                except (KeyError, TypeError, ValueError, sqlite3.DatabaseError) as exc:
                    logger.warning(
                        "agent %r 的 behavior_tree[%d] 写坏了(%s)—— 跳过这个节点",
                        agent_id, index, exc,
                    )
            return planted > 0

    def seed_duties(self, agent_id: str, duties: list[dict[str, Any]]) -> None:
        """Seed one agent's tree from its seed-file `duties` (bt-duties D6).

        Shape — a priority Selector, duties first, idle last:

            root (selector)
              ├─ <duty> (sequence) ─┬─ <duty>_when (time_window)
              │                     └─ <duty>     (action → bt_actions row)
              ├─ …
              └─ idle_wander (action)            ← always the last resort

        Seeds only when this agent's tree is empty (same empty-only contract as
        the other stores — a hand-edited tree is user data). A malformed duty is
        skipped with a warning rather than raising: a bad seed file must never
        block boot, it just leaves the agent with fewer duties (worst case none,
        i.e. today's idle-only behavior).
        """
        with self._lock:
            (count,) = self._conn.execute(
                "SELECT COUNT(*) FROM bt_nodes WHERE tree = ?", (agent_id,)
            ).fetchone()
            if count:
                return

            self.add_node(agent_id, "root", "selector", parent=None, sort=0)
            sort = 0
            for duty in duties or []:
                try:
                    name = str(duty["name"])
                    start = parse_hhmm(duty["start"])
                    end = parse_hhmm(duty["end"])
                    kind = str(duty["kind"])
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning(
                        "agent %r has a malformed duty %r (%s) — skipping it", agent_id, duty, exc
                    )
                    continue
                self.add_node(agent_id, name, "sequence", parent="root", sort=sort)
                self.add_node(
                    agent_id, f"{name}_when", "time_window", parent=name, sort=0,
                    params={"start": start, "end": end},
                )
                self.add_node(agent_id, f"{name}_do", "action", parent=name, sort=1)
                # The leaf's node_id is what Brain looks up in the action table.
                self.set_action(f"{name}_do", kind, duty.get("params") or {})
                sort += 1
            # Free time: the planner's leaf sits BELOW every duty and ABOVE the
            # idle fallback. Selector order is the whole arbitration rule — a
            # duty in its window always wins, and with no plan this leaf fails
            # and the tree falls through to idle_wander.
            self.add_node(agent_id, "follow_plan", "plan", parent="root", sort=sort)
            self.add_node(agent_id, "idle_wander", "action", parent="root", sort=sort + 1)

    def ensure_plan_node(self, agent_id: str) -> None:
        """Add the `follow_plan` leaf to a tree seeded before this change.

        Idempotent. Slots it directly above `idle_wander` (which keeps the last
        sort), so an existing duty tree gains free-time planning without being
        rebuilt.
        """
        with self._lock:
            rows = {r["node_id"]: r for r in self._tree_rows(agent_id)}
            if not rows or "follow_plan" in rows:
                return
            idle = rows.get("idle_wander")
            idle_sort = int(idle["sort"]) if idle is not None else len(rows)
            self.add_node(agent_id, "follow_plan", "plan", parent="root", sort=idle_sort)
            if idle is not None:  # push idle below the new leaf
                self.add_node(
                    agent_id, "idle_wander", "action", parent="root", sort=idle_sort + 1
                )

    def duty_windows(self, agent_id: str) -> list[tuple[int, int]]:
        """The agent's duty time windows, read straight off its `time_window`
        nodes. The planner derives free time from these — deliberately NOT
        stored separately, or the two would drift apart."""
        return [
            (int(r["params"].get("start", 0)), int(r["params"].get("end", 0)))
            for r in self._tree_rows(agent_id)
            if r["type"] == "time_window"
        ]
