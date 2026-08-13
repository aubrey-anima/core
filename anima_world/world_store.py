"""World definition stores: locations, action table, and BT structure.

Data-plane principle: definition data (location geometry, the node→action
mapping, tree shape) is rows — seeded idempotently on first boot, current
value owned by the storage row (M5 "row wins"). The event log remains the
world's history; nothing here emits or replays events.

这一层是**纯逻辑基类**:存储原语(增删查行)归子类
(`redis_state.RedisLocationStore` / `redis_state.RedisBTStore`),这里只放
从行**算出来**的东西 —— 树的组装、相对坐标折算、距离、播种次序。派生逻辑
只依赖存储原语,所以任何后端接上那几个方法就得到整套行为,而"这棵树怎么
组装"永远只有一份实现。

`LocationStore` owns the location rows — the map's single source of truth
(nested-map D7): an adjacency tree of regions and points with parent-relative
0~1 geometry. Nothing about the map goes through the event log.
`BTStore` owns the action rows (the row-backed `ActionTable`) and the BT node
rows (behavior tree shape, one row per node: children link via `parent`,
ordered by `sort`), and can rebuild live `bt_nodes.py` objects from those rows.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from anima_world.actions import ActionDescriptor, ActionTable
from anima_world.bt_nodes import (
    Action,
    Condition,
    NeedAction,
    Node,
    PlanAction,
    Selector,
    Sequence,
    Status,
    StockCondition,
    TimeWindow,
    default_bt,
)
from anima_world.world_time import parse_hhmm


logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_LOCATION_FIELDS = ("name", "description", "kind", "parent", "x", "y", "w", "h")

# `seed_tree` 认识的全部节点类型 —— 也是 `_build_node` 能构造的全集。
# (历史上住在 `anima_world.db` 的 CHECK 约束里;SQLite 层移除后,这里是权威。
# 加类型时两处一起动:这行 + `_build_node`,`tests/test_bt_authoring.py` 盯着。)
BT_NODE_TYPES = (
    "selector", "sequence", "condition", "action", "time_window", "plan",
    "need_action", "stock_condition",
)

# Depth cap for tree assembly — dirty data (a cycle written via a raw backend
# write) must degrade, never recurse forever.
_MAX_TREE_DEPTH = 32


class LocationStore:
    """Location definitions — the map's single source of truth(纯逻辑基类).

    Rows form an adjacency tree: `parent` self-refs another row (None = top
    level), `kind` is 'region' (may nest) or 'point' (agents stand on these).
    Geometry is relative to the parent region, normalized 0~1.

    **存储归子类**:`all` / `get` / `upsert` 是存储原语,子类负责实现
    (`redis_state.RedisLocationStore`)。子类还要自备 `self._lock`
    (进程内线程锁;跨进程那把在 `World` 上)—— 派生方法在它下面串行化。
    没有 `__init__`:这一层不持有任何存储状态。
    """

    def _rows(self, cur: Any) -> list[dict[str, Any]]:
        """(历史上是 SQLite 游标→dict 适配器。)存储归子类。"""
        raise NotImplementedError("存储归子类(redis_state.RedisLocationStore)")

    def all(self) -> list[dict[str, Any]]:
        """All location rows, ordered by id. 存储归子类。"""
        raise NotImplementedError("存储归子类(redis_state.RedisLocationStore)")

    def get(self, loc_id: str) -> dict[str, Any] | None:
        """One location row, or None. 存储归子类。"""
        raise NotImplementedError("存储归子类(redis_state.RedisLocationStore)")

    def tree(self) -> list[dict[str, Any]]:
        """Assemble the adjacency rows into nested dicts (each row gains a
        `children` list). Defensive: a row whose parent is dangling or part of
        a cycle is logged and treated as top-level, so a hand-written row can
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

    def absolute_box(self, loc_id: str) -> tuple[float, float, float, float] | None:
        """一个 region 在世界画布上的绝对矩形 `(x, y, w, h)`,0~1。

        **`w` / `h` 和 `x` / `y` 一样是相对父级的**(nested-map D2):一个
        `w=0.55` 的 region 占的是父级宽度的 55%,不是画布的 55%。照原始值画出来的
        图看上去完全合理 —— 只是每个东西都在错的地方,而且没有任何东西会报错。

        点没有矩形(`_validate` 拒绝给 point 写 w/h),返回 None。
        """
        rows = {r["id"]: r for r in self.all()}
        row = rows.get(loc_id)
        if row is None or row.get("w") is None or row.get("h") is None:
            return None
        origin = self.absolute_xy(loc_id)
        if origin is None:
            return None
        w, h = float(row["w"]), float(row["h"])
        parent = row["parent"]
        for _ in range(_MAX_TREE_DEPTH):
            if parent is None:
                return (origin[0], origin[1], w, h)
            prow = rows.get(parent)
            if prow is None or prow.get("w") is None or prow.get("h") is None:
                return None
            w *= float(prow["w"])
            h *= float(prow["h"])
            parent = prow["parent"]
        return None

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
        region geometry raises and leaves the rows unchanged. 存储归子类
        (子类实现里要调用 `self._validate(loc_id, merged)` 保住这些约束)。"""
        raise NotImplementedError("存储归子类(redis_state.RedisLocationStore)")

    def seed_defaults(self, entries: list[dict[str, Any]], *,
                      merge: bool = False) -> list[str]:
        """Insert `entries` only when the store is empty — populated rows are
        user data and must never be clobbered by re-seeding. Parents are
        seeded before their children so hierarchy validation can see them.

        只依赖 `all` / `upsert`,所以任何后端接上原语就能用;子类想换判空
        姿势(如 Redis 版)可以覆盖,但语义必须保持"空才播"。

        `merge=True` 把粒度从**整张表**降到**每个地点**:已有的 id 一个字都不动,
        文件里多出来的补进去。**默认必须是 False**,而且只有"作者指名了
        `--world-file`"那一条路才准传 True —— 没给文件时引擎手里是内置橱窗,
        它每次开机都在手上,逐项合并等于把橱窗的地图塞进别人的世界(世界照跑、
        日志干净)。返回真的写下去的那些 id。"""
        with self._lock:
            have = {str(row.get("id")) for row in self.all()}
            if have and not merge:
                return []
            written: list[str] = []
            for e in _parents_first(entries):
                if merge and str(e["id"]) in have:
                    continue
                self.upsert(
                    e["id"],
                    **{f: e[f] for f in _LOCATION_FIELDS if f in e},
                )
                written.append(str(e["id"]))
            return written


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
    """Action table + tree shape(纯逻辑基类).

    **存储归子类**:`actions` / `set_action` / `add_node` / `_tree_rows` 是
    存储原语,子类负责实现(`redis_state.RedisBTStore`)。子类还要自备
    `self._lock`(进程内线程锁)。`action_table` / `build_tree` / `seed_*` /
    `duty_windows` 全建在那四个原语之上 —— 组装逻辑只此一份。
    没有 `__init__`:这一层不持有任何存储状态。
    """

    # ── action table ────────────────────────────────────────────────────────

    def actions(self) -> list[dict[str, Any]]:
        """All action rows `{node_id, kind, params}`, ordered by node_id.
        存储归子类。"""
        raise NotImplementedError("存储归子类(redis_state.RedisBTStore)")

    def set_action(
        self,
        node_id: str,
        kind: str,
        params: dict[str, Any] | None = None,
        *,
        tree: str | None = None,
    ) -> None:
        """Upsert one action binding. 存储归子类。

        `tree=None` 是**共享**绑定(`go_to_*` / `chat_with_*` / 需求动作):它们
        没有人称,谁都能去那儿、谁都能找他说话。给了 `tree` 就是**这个人自己的**
        绑定,只有他查得到。
        """
        raise NotImplementedError("存储归子类(redis_state.RedisBTStore)")

    def action_table(self, tree: str | None = None) -> ActionTable:
        """Build a live `ActionTable` for one agent's tree(fallback 仍是
        `idle_wander`,见 `ActionTable.lookup`)。

        **两层**:没有人称的共享绑定打底,这个 tree 自己的绑定盖在上面。

        分层是被一次重名逼出来的:`bt_nodes` 一直按 `(tree, node_id)` 存,而动作表
        原先是**全世界一张**。于是两个人只要给自己班表上的某件事起了同一个名字
        (「回铺子」「收摊」「去码头」—— 一个世界里这本来就是好几个人都会做的事),
        后播种的那个人就把先播的那条整行改写掉。坏法是最难发现的那种:树建得起来、
        动作查得到、日志一行不错,只是**人走错了门**。
        """
        shared: dict[str, ActionDescriptor] = {}
        mine: dict[str, ActionDescriptor] = {}
        for r in self.actions():
            owner = r.get("tree") or None
            if owner is None:
                shared[r["node_id"]] = ActionDescriptor(r["kind"], r["params"])
            elif tree is not None and owner == tree:
                mine[r["node_id"]] = ActionDescriptor(r["kind"], r["params"])
        return ActionTable({**shared, **mine})

    def shared_action_ids(self) -> set[str]:
        """没有人称的那些绑定的 node_id。

        播种时的"这行有没有"一律问它,别问 `actions()` —— 那份现在还装着别人
        名下的行,拿它判会把"张三有一个叫 X 的班"读成"X 已经播过了"。
        """
        return {r["node_id"] for r in self.actions() if not r.get("tree")}

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
        """Upsert one BT node row (keyed on (tree, node_id)). 存储归子类。"""
        raise NotImplementedError("存储归子类(redis_state.RedisBTStore)")

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
        """One tree's node rows `{node_id, type, parent, sort, params}`,
        ordered by sort(次序决定 Selector 的优先级). 存储归子类。"""
        raise NotImplementedError("存储归子类(redis_state.RedisBTStore)")

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
            # 这个类型曾经在存储侧放行、在这里不认 —— 于是作者写一行完全合法的
            # 节点,`_build_node` 抛 ValueError,调用方兜成一行 warning,
            # **整棵作者树塌回 default_bt()**(只会 idle_wander)。
            # 世界照跑,角色什么也不干。
            p = row["params"]
            release = p.get("release")
            return NeedAction(
                str(p.get("need", "")),
                float(p.get("threshold", 0.0)),
                str(p.get("action_id", "")),
                None if release is None else float(release),
            )
        if node_type == "stock_condition":
            p = row["params"]
            # 比较符不认识时 `StockCondition` 自己抛 ValueError,消息里带着那个符号。
            # 调用方兜成一行 warning —— 打错一个符号让整棵树塌回 idle_wander 是重了,
            # 但比"这条分支永不触发而日志干净"轻得多。
            return StockCondition(
                str(p.get("owner", "")), str(p.get("key", "")),
                str(p.get("op", "<")), float(p.get("value", 0.0)),
            )
        kids = [self._build_node(c, children) for c in children.get(row["node_id"], [])]
        if node_type == "selector":
            return Selector(kids)
        if node_type == "sequence":
            return Sequence(kids)
        raise ValueError(f"unknown bt node type: {node_type}")

    # ── seeding ─────────────────────────────────────────────────────────────

    def seed_defaults(self, agent_ids: list[str], location_ids: list[str], *,
                      merge: bool = False) -> None:
        """Seed the action table from the live roster (`go_to_<loc>` per
        location, `chat_with_<agent>` per agent — no hardcoded ghosts) plus
        the fixed kinds, and the 'default' tree (root selector → idle_wander,
        byte-for-byte the old `default_bt()` behavior). Empty-store-only,
        same contract as `LocationStore.seed_defaults`.

        `merge=True`(作者指名的那份世界文件加了角色/地点时)只补**缺的那几行**,
        已有的绑定一个字都不动。少了这一条,新来的人没有 `chat_with_他`、新开的
        地方没有 `go_to_那儿` —— `ActionTable.lookup` 查不到就回落 idle_wander,
        树成功、世界不动、日志干净。"""
        with self._lock:
            # 走 `self.actions()` 而不是直读存储:这个方法要能在任何后端的子类上
            # 照跑(`redis_state.RedisBTStore`)。基类里留一处直读,子类就得把整个
            # 方法重写一遍 —— 而重写的那份迟早和这份不一样。
            existing = self.shared_action_ids()
            if not existing or merge:
                bindings = [(f"go_to_{loc}", "walk", {"location": loc}) for loc in location_ids]
                bindings += [(f"chat_with_{aid}", "chat", {"target": aid}) for aid in agent_ids]
                bindings += [
                    ("do_work", "work", {"location": "workshop"}),
                    ("go_sleep", "sleep", {}),
                    ("idle_wander", "idle_wander", {}),
                    ("idle_social", "idle_social", {}),
                ]
                for node_id, kind, params in bindings:
                    if node_id in existing:
                        continue      # 只增不改:手改过的绑定是用户数据
                    self.set_action(node_id, kind, params)
            if not self._tree_rows("default"):
                self.add_node("default", "root", "selector", parent=None, sort=0)
                self.add_node("default", "idle_wander", "action", parent="root", sort=0)

    def seed_tree(self, agent_id: str, nodes: list[dict[str, Any]]) -> bool:
        """用种子里写死的节点表播一棵树(`agents[].behavior_tree`)。

        `duties` 只能表达"时间窗 → 动作"这一种形状 —— 作者要写条件分支、需求带、
        嵌套选择器,就够不着了。这条路把 BT 节点行的表达力整个交出去。

        规矩与其它播种一致:**空表才播**(手改过的树是用户数据),**坏条目跳过并
        警告、绝不阻塞开机**(坏种子让世界少一个分支,不该让世界开不了机)。
        返回是否真的播了 —— 调用方据此决定要不要退回 `duties`。

        `action` 节点可以随身带一个 `{"action": {"kind": ..., "params": {...}}}`。
        没有它,一个作者写的动作叶子跑起来**只会 idle_wander**:节点 id 在动作表里
        查不到,`ActionTable.lookup` 回落到闲逛,树成功、世界不动、日志干净 ——
        `duties` 那条路一直替作者调 `set_action`,这条路以前根本没有出口。
        带在节点上而不是另开一张表,是为了不可能出现孤儿行。
        """
        if not nodes:
            return False
        with self._lock:
            # 走 `_tree_rows` 而不是直读存储 —— 见 `seed_defaults` 里那条注释。
            if self._tree_rows(agent_id):
                return True  # 已有树:视作"种子这条路走过了",别再叠 duties 上去

            planted = 0
            leaves: list[str] = []
            for index, node in enumerate(nodes):
                try:
                    node_type = str(node["type"])
                    if node_type not in BT_NODE_TYPES:
                        raise ValueError(f"unknown node type {node_type!r}")
                    node_id = str(node["node_id"])
                    self.add_node(
                        agent_id,
                        node_id,
                        node_type,
                        parent=node.get("parent"),
                        sort=int(node.get("sort", index)),
                        params=dict(node.get("params") or {}),
                    )
                    if node_type == "action":
                        leaves.append(node_id)
                        spec = node.get("action")
                        if spec is not None:
                            self.set_action(
                                node_id, str(spec["kind"]), dict(spec.get("params") or {}),
                                tree=agent_id,
                            )
                    planted += 1
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning(
                        "agent %r 的 behavior_tree[%d] 写坏了(%s)—— 跳过这个节点",
                        agent_id, index, exc,
                    )
            known = self.shared_action_ids() | {
                row["node_id"] for row in self.actions() if row.get("tree") == agent_id
            }
            for node_id in leaves:
                if node_id not in known:
                    logger.warning(
                        "agent %r 的动作叶子 %r 在动作表里没有对应的行 —— 它跑起来"
                        "只会 idle_wander。给这个节点加一个 "
                        '"action": {"kind": ..., "params": {...}}',
                        agent_id, node_id,
                    )
            return planted > 0

    def seed_duties(self, agent_id: str, duties: list[dict[str, Any]]) -> None:
        """Seed one agent's tree from its seed-file `duties` (bt-duties D6).

        Shape — a priority Selector, duties first, idle last:

            root (selector)
              ├─ <duty> (sequence) ─┬─ <duty>_when  (time_window)
              │                     ├─ <duty>_stock (stock_condition,可选)
              │                     └─ <duty>       (action → action row)
              ├─ …
              └─ idle_wander (action)            ← always the last resort

        `when_stock` 是可选的第二道闸:**到点了,而且世界的量到了那个数**。
        钟点排班表达不了"面粉见底了才去进货" —— 而人正是那样活的。缺席时这个
        节点不存在,树逐字和以前一样。

        Seeds only when this agent's tree is empty (same empty-only contract as
        the other stores — a hand-edited tree is user data). A malformed duty is
        skipped with a warning rather than raising: a bad seed file must never
        block boot, it just leaves the agent with fewer duties (worst case none,
        i.e. today's idle-only behavior).
        """
        with self._lock:
            # 走 `_tree_rows` 而不是直读存储 —— 见 `seed_defaults` 里那条注释。
            if self._tree_rows(agent_id):
                return

            self.add_node(agent_id, "root", "selector", parent=None, sort=0)
            sort = 0
            for duty in duties or []:
                try:
                    name = str(duty["name"])
                    start = parse_hhmm(duty["start"])
                    end = parse_hhmm(duty["end"])
                    kind = str(duty["kind"])
                    gate = duty.get("when_stock")
                    if gate is not None:
                        gate = {
                            "owner": str(gate["owner"]), "key": str(gate["key"]),
                            "op": str(gate.get("op", "<")), "value": float(gate["value"]),
                        }
                        # 就地造一个,把打错的比较符在**播种的时候**就顶回来。
                        # 留到 `build_tree` 才炸,炸掉的是整棵树。
                        StockCondition(gate["owner"], gate["key"], gate["op"], gate["value"])
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
                if gate is not None:
                    self.add_node(
                        agent_id, f"{name}_stock", "stock_condition", parent=name,
                        sort=1, params=gate,
                    )
                self.add_node(agent_id, f"{name}_do", "action", parent=name, sort=2)
                # The leaf's node_id is what Brain looks up in the action table.
                # 按人存:两个人都写「回铺子」时,后播的那个不许改写先播的那条。
                self.set_action(f"{name}_do", kind, duty.get("params") or {}, tree=agent_id)
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
