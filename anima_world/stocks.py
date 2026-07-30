"""存量(stock):世界里会随时间变化的数值,以及推动它们的规律引擎。

**量 = (owner, key, value)。** owner 是任意字符串,前缀即种类:

    world                 季节、天气这类全局的量
    tree:oak_01           一棵树的 size / growth_rate / max_size
    agent:夏              功力 / 修为 —— 挂在角色身上
    location:cafe         这个地方自己的量

不发明新的实体系统是有意的:和账本的 holder(角色 id / `player:x` / `__town__`)
完全同构,而那套东西已经被证明够用了。**一个"实体"就是共用一个 owner 的一组量。**

`evaluate_due` 是引擎那一步:挑出到点的规律、按 owner 求值、写回、必要时发事件。
它是**纯算术 + SQL,没有 LLM**,所以和 needs/economy 一样跑在 tick 线程上 ——
这一点和 autonomy 正相反(那条要打网络,必须丢到别的线程去)。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Any, Callable, Iterable, Sequence

from anima_world.expressions import ExpressionError
from anima_world.rules import WORLD_PREFIX, Rule

logger = logging.getLogger(__name__)

WORLD_OWNER = "world"


def owner_kind(owner: str) -> str:
    """`tree:oak_01` → `tree`;`world` → `world`。前缀即种类。"""
    return owner.split(":", 1)[0] if ":" in owner else owner


class StockStore:
    """存量的持久层。所有访问经 `lock` 串行化(和 ChatStore 同一条纪律)。"""

    def __init__(self, conn: sqlite3.Connection, lock: Any | None = None) -> None:
        self._conn = conn
        self._lock = lock if lock is not None else threading.RLock()

    # ── 读写 ────────────────────────────────────────────────────────────────

    def get(self, owner: str, key: str, default: float = 0.0) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM stocks WHERE owner = ? AND key = ?", (owner, key)
            ).fetchone()
        return float(row[0]) if row is not None else default

    def of(self, owner: str) -> dict[str, float]:
        """这个 owner 身上所有的量。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM stocks WHERE owner = ?", (owner,)
            ).fetchall()
        return {key: float(value) for key, value in rows}

    def set(self, owner: str, key: str, value: float, tick: int = 0) -> None:
        with self._lock:
            self._write(owner, key, value, tick)
            self._conn.commit()

    def set_many(self, owner: str, values: dict[str, float], tick: int = 0) -> None:
        with self._lock:
            for key, value in values.items():
                self._write(owner, key, value, tick)
            self._conn.commit()

    def _write(self, owner: str, key: str, value: float, tick: int) -> None:
        self._conn.execute(
            "INSERT INTO stocks (owner, key, value, updated_tick) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(owner, key) DO UPDATE SET value=excluded.value,"
            " updated_tick=excluded.updated_tick",
            (owner, key, float(value), int(tick)),
        )

    def delete(self, owner: str, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._conn.execute("DELETE FROM stocks WHERE owner = ?", (owner,))
            else:
                self._conn.execute(
                    "DELETE FROM stocks WHERE owner = ? AND key = ?", (owner, key)
                )
            self._conn.commit()

    def owners(self, kind: str | None = None) -> list[str]:
        with self._lock:
            if kind is None:
                rows = self._conn.execute(
                    "SELECT DISTINCT owner FROM stocks ORDER BY owner"
                ).fetchall()
            else:
                # 主键是 (owner, key),所以前缀匹配走得到索引。
                rows = self._conn.execute(
                    "SELECT DISTINCT owner FROM stocks WHERE owner = ? OR owner LIKE ?"
                    " ORDER BY owner",
                    (kind, f"{kind}:%"),
                ).fetchall()
        return [row[0] for row in rows]

    def snapshot(self, owner: str) -> dict[str, Any]:
        """带 `updated_tick` 的快照 —— `dt` 要从这里算。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value, updated_tick FROM stocks WHERE owner = ?", (owner,)
            ).fetchall()
        return {key: (float(value), int(tick)) for key, value, tick in rows}

    def snapshot_kind(self, kind: str) -> dict[str, dict[str, tuple[float, int]]]:
        """**一次查询**拿到某一类所有 owner 的快照 → `{owner: {key: (值, tick)}}`。

        存在的理由是实测:逐个 owner 查一次(2000 棵树 = 2000 次查询 + 2000 次
        commit)让每 tick 涨到 72ms,而"一万棵树也扛得住"是这一层写在文档里的承诺。
        主键是 (owner, key),所以前缀匹配走得到索引。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT owner, key, value, updated_tick FROM stocks"
                " WHERE owner = ? OR owner LIKE ?",
                (kind, f"{kind}:%"),
            ).fetchall()
        out: dict[str, dict[str, tuple[float, int]]] = {}
        for owner, key, value, tick in rows:
            out.setdefault(owner, {})[key] = (float(value), int(tick))
        return out

    def snapshot_many(self, owners: Sequence[str]) -> dict[str, dict[str, tuple[float, int]]]:
        """指定若干 owner 的快照。分批发 —— SQLite 的参数个数有上限。"""
        out: dict[str, dict[str, tuple[float, int]]] = {}
        chunk = 400
        with self._lock:
            for start in range(0, len(owners), chunk):
                batch = list(owners[start : start + chunk])
                if not batch:
                    continue
                placeholders = ",".join("?" * len(batch))
                rows = self._conn.execute(
                    "SELECT owner, key, value, updated_tick FROM stocks"
                    f" WHERE owner IN ({placeholders})",
                    batch,
                ).fetchall()
                for owner, key, value, tick in rows:
                    out.setdefault(owner, {})[key] = (float(value), int(tick))
        return out

    def write_round(self, pending: dict[str, dict[str, float]], tick: int) -> int:
        """一轮的所有写入,**一次 commit**。返回写了几个量。

        每个 owner 一次 commit 是典型的 SQLite 性能杀手 —— 那是 72ms/tick 的主因。
        """
        written = 0
        with self._lock:
            for owner, values in pending.items():
                for key, value in values.items():
                    self._write(owner, key, value, tick)
                    written += 1
            self._conn.commit()
        return written


def evaluate_due(
    store: StockStore,
    rules: Iterable[Rule],
    now: int,
    *,
    last_run: dict[str, int],
    action_owners: Callable[[str], list[str]] | None = None,
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """把到点的规律跑一遍。返回一份"这一轮干了什么"的小报告。

    `last_run` 由调用方持有(内存态):它只决定**要不要现在算**,不影响算出来的
    结果 —— 结果由 `dt` 决定,而 `dt` 来自量自己的 `updated_tick`。所以重启把
    `last_run` 清空是安全的:最多是多算一次,值不会错。
    """
    report: dict[str, Any] = {"evaluated": 0, "written": 0, "emitted": 0, "skipped": []}

    # 先挑出到点的规律,并按选择器分组 —— 分组是为了**按类批量快照**,而不是
    # 逐个 owner 查一次(那是 72ms/tick 的另一半原因)。
    due_rules: list[Rule] = []
    kinds: set[str] = set()
    explicit: set[str] = set()
    action_owners_by_rule: dict[str, list[str]] = {}
    for rule in rules:
        since = last_run.get(rule.id)
        if since is not None and now - since < rule.interval_ticks:
            continue
        last_run[rule.id] = now
        due_rules.append(rule)
        if rule.selector_kind == "kind":
            kinds.add(rule.selector_value)
        elif rule.selector_kind == "owner":
            explicit.add(rule.selector_value)
        else:   # action:此刻正在做这个动作的角色
            owners = list(action_owners(rule.selector_value)) if action_owners else []
            action_owners_by_rule[rule.id] = owners
            explicit.update(owners)
    if not due_rules:
        return report

    # **双缓冲**:这一轮开始前把要碰的量全部快照下来,所有表达式都读这份快照,
    # 写入攒到最后一次性落库。于是"规律 A 先算还是 B 先算"不再是隐藏的语义 ——
    # 与顺序无关、可预测,代价是连锁反应要等下一轮(对模拟通常反而更对)。
    snapshots: dict[str, dict[str, tuple[float, int]]] = {}
    owners_by_kind: dict[str, list[str]] = {}
    for kind in kinds:
        batch = store.snapshot_kind(kind)         # 每一类一次查询
        snapshots.update(batch)
        owners_by_kind[kind] = sorted(batch)
    if explicit:
        snapshots.update(store.snapshot_many(sorted(explicit)))

    # 世界的全局量只在真有规律读它时才查(表达式里写成 `world_季节`)。
    world_stocks: dict[str, float] = {}
    if any(name.startswith(WORLD_PREFIX) for rule in due_rules for name in rule.reads()):
        world_stocks = {
            f"{WORLD_PREFIX}{key}": value
            for key, (value, _) in (
                snapshots.get(WORLD_OWNER) or {}
            ).items()
        } or {f"{WORLD_PREFIX}{k}": v for k, v in store.of(WORLD_OWNER).items()}

    due = [
        (
            rule,
            owners_by_kind.get(rule.selector_value, [])
            if rule.selector_kind == "kind"
            else [rule.selector_value]
            if rule.selector_kind == "owner"
            else action_owners_by_rule.get(rule.id, []),
        )
        for rule in due_rules
    ]

    pending: dict[str, dict[str, float]] = {}
    events: list[dict[str, Any]] = []
    for rule, owners in due:
        for owner in owners:
            try:
                updates, fired = _apply(rule, snapshots.get(owner, {}), owner, now, world_stocks)
            except ExpressionError as exc:
                # 运行期降级:一条算不出来的规律不该掀翻整个 tick(节拍脚本同一条
                # 纪律)。但绝不无声 —— 它会出现在报告里,也会打一条日志。
                logger.warning("规律 %s 在 %s 上算不出来:%s", rule.id, owner, exc)
                report["skipped"].append((rule.id, owner, str(exc)))
                continue
            report["evaluated"] += 1
            if not updates:
                continue
            slot = pending.setdefault(owner, {})
            for key in updates:
                if key in slot:
                    # 两条规律抢同一个量:后写的赢,而且谁也看不见谁(双缓冲)。
                    # 几乎一定是设计错误,所以要说出来。
                    logger.warning(
                        "%s 上的量 %s 被不止一条规律写,规律 %s 覆盖了前一条",
                        owner, key, rule.id,
                    )
            slot.update(updates)
            events.extend(fired)

    if pending:
        report["written"] += store.write_round(pending, tick=now)   # 整轮一次 commit
    for event in events:
        report["emitted"] += 1
        if emit is not None:
            emit(event)
    return report


def _owners_for(
    store: StockStore, rule: Rule, action_owners: Callable[[str], list[str]] | None
) -> list[str]:
    if rule.selector_kind == "owner":
        return [rule.selector_value]
    if rule.selector_kind == "kind":
        return store.owners(rule.selector_value)
    if rule.selector_kind == "action":
        # 行为驱动:此刻正在做这个动作的角色。修炼、采矿、耕种都是这一类 ——
        # 投入的是**时间**,而速率由行为者自己的量决定。
        return list(action_owners(rule.selector_value)) if action_owners else []
    return []


def _apply(
    rule: Rule,
    snapshot: dict[str, tuple[float, int]],
    owner: str,
    now: int,
    world_stocks: dict[str, float],
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """算一个 owner。**只算不写** —— 写入由调用方攒到一轮末尾(双缓冲)。

    返回 (要写的量, 要发的事件)。
    """
    if not snapshot:
        return ({}, [])

    values = {key: value for key, (value, _) in snapshot.items()}
    # dt = 这条规律要写的那些量,上一次被写是多久以前。新量按 0 算(刚出生,
    # 还没有"流逝"可言),否则一棵刚种下的树会按世界年龄一次性长成参天大树。
    ticks = [snapshot[key][1] for key in rule.outputs if key in snapshot]
    dt = max(0, now - max(ticks)) if ticks else 0

    # 双缓冲:这一轮所有表达式读到的都是**这一轮开始前**的值。规律之间因此
    # 与顺序无关 —— 否则"A 先算还是 B 先算"会变成隐藏的语义。
    namespace: dict[str, Any] = {**world_stocks, **values, "dt": dt, "now": now}

    for condition in rule.conditions:
        if not condition.evaluate(namespace):
            return ({}, [])

    # 门槛在**算之前**是什么状态 —— 边沿触发要用它做对照。
    before_flags = [bool(emit.when.evaluate(namespace)) for emit in rule.emits]

    updates = {key: float(expression.evaluate(namespace)) for key, expression in rule.outputs.items()}
    after_namespace = {**namespace, **updates}
    events: list[dict[str, Any]] = []
    for emit_spec, was_true in zip(rule.emits, before_flags):
        # **边沿触发**:算之前不满足、算之后满足才发。没有这一条,一棵长满了的树
        # 会每 12 tick 发一次"我长成了",直到世界末日。
        if was_true or not emit_spec.when.evaluate(after_namespace):
            continue
        events.append({
            "type": emit_spec.type,
            # `who` 在这个库里一直是**角色 id** 的语义。一棵树不是角色,所以
            # 只有 owner 真的是个角色时才填 —— 往里塞 `oak_01` 是个埋着的谎,
            # 任何假设"who 是角色"的读者都会被它骗。owner 一律在 payload 里。
            "who": owner.split(":", 1)[1] if owner.startswith("agent:") else None,
            "payload": {
                "rule": rule.id,
                "owner": owner,
                **{key: updates[key] for key in rule.outputs},
                **emit_spec.payload,
            },
        })
    return (updates, events)
