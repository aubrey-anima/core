"""存量(stock):世界里会随时间变化的数值,以及推动它们的规律引擎。

**量 = (owner, key, value)。** owner 是任意字符串,前缀即种类:

    world                 季节、天气这类全局的量
    tree:oak_01           一棵树的 size / growth_rate / max_size
    agent:夏              功力 / 修为 —— 挂在角色身上
    location:cafe         这个地方自己的量

不发明新的实体系统是有意的:和账本的 holder(角色 id / `player:x` / `__town__`)
完全同构,而那套东西已经被证明够用了。**一个"实体"就是共用一个 owner 的一组量。**

`evaluate_due` 是引擎那一步:挑出到点的规律、按 owner 求值、写回、必要时发事件。
它是**纯算术,没有 LLM**,所以和 needs/economy 一样跑在 tick 线程上 ——
这一点和 autonomy 正相反(那条要打网络,必须丢到别的线程去)。

存储在别处:SQLite 版 `StockStore` 已退役,`store` 参数是鸭子类型
(`anima_world.redis_state.RedisStockStore`),要求的接口是
`of` / `snapshot_kind` / `snapshot_many` / `write_round`。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from anima_world.expressions import ExpressionError, world_dice
from anima_world.rules import WORLD_PREFIX, Rule
from anima_world.world_time import DEFAULT_MINUTES_PER_TICK, clock_names

logger = logging.getLogger(__name__)

WORLD_OWNER = "world"
AGENT_KIND = "agent"   # `not_action` 的候选池:这个引擎里只有角色"在做一件事"


def owner_kind(owner: str) -> str:
    """`tree:oak_01` → `tree`;`world` → `world`。前缀即种类。"""
    return owner.split(":", 1)[0] if ":" in owner else owner


def evaluate_due(
    store: Any,
    rules: Iterable[Rule],
    now: int,
    *,
    last_run: dict[str, int],
    action_owners: Callable[[str], list[str]] | None = None,
    emit: Callable[[dict[str, Any]], None] | None = None,
    minutes_per_tick: int = DEFAULT_MINUTES_PER_TICK,
    world_id: str = "",
) -> dict[str, Any]:
    """把到点的规律跑一遍。返回一份"这一轮干了什么"的小报告。

    `last_run` 由调用方持有(内存态):它只决定**要不要现在算**,不影响算出来的
    结果 —— 结果由 `dt` 决定,而 `dt` 来自量自己的 `updated_tick`。所以重启把
    `last_run` 清空是安全的:最多是多算一次,值不会错。
    ⚠️ 「最多多算一次,值不会错」这句只对**按 `dt` 折算**的规律成立。一条
    `{"every": {"days": 1}, "set": {"雨天数": "雨天数 + 1"}}` 的常数步长规律,
    多算一次就真的多走一整天 —— 加载期的 `rules.drift_warnings` 就是点这个的名。

    `minutes_per_tick` 只用来把 `now` 折成 `day`/`hour`/`minute`/`minute_of_day`
    (见 `rules.BUILTIN_NAMES`)。**它是每个世界自己的配置**,所以必须由调用方传 ——
    在这儿写死一个默认值等于让"半夜"在两个世界里指不同的时刻。

    `world_id` 只喂给 `rand()`(`expressions.world_dice` 的第一个坐标)。缺省是
    空串,好让老调用点一个字不改地行为不变 —— **但那样两个世界会摇同一副骰子**
    (同名规律、同名 owner、同一 tick 下同一个数)。宿主/调度器该把真实的世界名
    传进来:世界的名字本来就是这个引擎里世界的身份。
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
        elif rule.selector_kind == "action":   # 此刻正在做这个动作的角色
            owners = list(action_owners(rule.selector_value)) if action_owners else []
            action_owners_by_rule[rule.id] = owners
            explicit.update(owners)
        else:   # not_action:此刻**没在**做这个动作的角色。名单要等快照之后才算得出
            kinds.add(AGENT_KIND)               # 借 `agent` 那一次批量查询,不额外查
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

    # `not_action` 的名单只能在这里算:它是"所有角色"减去"正在做这件事的角色",
    # 而前者要等 `snapshot_kind("agent")` 回来才知道。**扣的是补集,不是差集的
    # 反面** —— 一个此刻没有任何动作的角色(刚出生、树还没跑过一轮)算"没在睡",
    # 因为她确实没在睡。
    for rule in due_rules:
        if rule.selector_kind != "not_action":
            continue
        busy = set(action_owners(rule.selector_value)) if action_owners else set()
        action_owners_by_rule[rule.id] = [
            owner for owner in owners_by_kind.get(AGENT_KIND, []) if owner not in busy
        ]

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

    # 日历折一次,这一轮所有 owner 共用 —— 它只依赖 `now`,逐个 owner 再折一遍
    # 只是把同一个答案算一万次。
    clock = clock_names(now, minutes_per_tick)

    pending: dict[str, dict[str, float]] = {}
    events: list[dict[str, Any]] = []
    for rule, owners in due:
        for owner in owners:
            try:
                updates, fired = _apply(
                    rule, snapshots.get(owner, {}), owner, now, world_stocks, clock,
                    world_id,
                )
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


def _apply(
    rule: Rule,
    snapshot: dict[str, tuple[float, int]],
    owner: str,
    now: int,
    world_stocks: dict[str, float],
    clock: dict[str, int] | None = None,
    world_id: str = "",
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """算一个 owner。**只算不写** —— 写入由调用方攒到一轮末尾(双缓冲)。

    返回 (要写的量, 要发的事件)。
    """
    if not snapshot:
        return ({}, [])

    # 这一刻、这条规律、这个 owner 的骰子。**一次求值里只有一个数**:条件、`set`、
    # `emit` 的门槛读到的 `rand()` 全是它 —— 否则同一轮里"下不下雨"能有两个答案。
    def roll() -> float:
        return world_dice(world_id, rule.id, owner, now)

    values = {key: value for key, (value, _) in snapshot.items()}
    # dt = 这条规律要写的那些量,上一次被写是多久以前。新量按 0 算(刚出生,
    # 还没有"流逝"可言),否则一棵刚种下的树会按世界年龄一次性长成参天大树。
    ticks = [snapshot[key][1] for key in rule.outputs if key in snapshot]
    dt = max(0, now - max(ticks)) if ticks else 0

    # 双缓冲:这一轮所有表达式读到的都是**这一轮开始前**的值。规律之间因此
    # 与顺序无关 —— 否则"A 先算还是 B 先算"会变成隐藏的语义。
    # 内置名放最后 —— 它们**盖过**同名的量。同名本来就被 `parse_kinds` 当加载期
    # 错误拒了,这里的顺序是那道闸的第二重保险:万一漏进来一个,读到的至少是
    # 一个说得清的东西(钟点),而不是"有时是量、有时是钟点"。
    namespace: dict[str, Any] = {
        **world_stocks, **values,
        **(clock or clock_names(now)),
        "dt": dt, "now": now,
    }

    for condition in rule.conditions:
        if not condition.evaluate(namespace, dice=roll):
            return ({}, [])

    # 门槛在**算之前**是什么状态 —— 边沿触发要用它做对照。
    before_flags = [bool(emit.when.evaluate(namespace, dice=roll)) for emit in rule.emits]

    updates = {
        key: float(expression.evaluate(namespace, dice=roll))
        for key, expression in rule.outputs.items()
    }
    after_namespace = {**namespace, **updates}
    events: list[dict[str, Any]] = []
    for emit_spec, was_true in zip(rule.emits, before_flags):
        # **边沿触发**:门槛被跨过去那一下才发。没有这一条,一棵长满了的树会每
        # 12 tick 发一次"我长成了",直到世界末日。
        #
        # 哪个方向算数由作者的 `on` 说了算(缺省 `rise` = 从前的行为)。**这里
        # 一个字节的状态都不留**:两个值都在这一轮的双缓冲快照里,所以重启之后
        # 既不会补发一件从没发生过的事,也不会因为"上次是什么"丢了而永远沉默。
        is_true = bool(emit_spec.when.evaluate(after_namespace, dice=roll))
        if is_true == was_true:
            continue                      # 没跨越
        edge = "rise" if is_true else "fall"
        if not emit_spec.fires_on(edge):
            continue
        events.append({
            "type": emit_spec.type,
            # `who` 在这个库里一直是**角色 id** 的语义。一棵树不是角色,所以
            # 只有 owner 真的是个角色时才填 —— 往里塞 `oak_01` 是个埋着的谎,
            # 任何假设"who 是角色"的读者都会被它骗。owner 一律在 payload 里。
            # 事件落在**哪儿**同理:这一层不认识地点,`loc` 由消费端从 owner 反查。
            "who": owner.split(":", 1)[1] if owner.startswith("agent:") else None,
            "payload": {
                "rule": rule.id,
                "owner": owner,
                **{key: updates[key] for key in rule.outputs},
                **emit_spec.payload,
                # 契约的三个键放在作者的 `payload` **之后**:它们是引擎的回答
                # (这次是哪个方向、她该多当回事、她记住的那句话),不该被一个
                # 手滑的同名键盖掉。没声明 importance 的话后两个一个都不出现 ——
                # 那样的世界事件形状和从前逐位相同。
                "edge": edge,
                **emit_spec.memory_fields(),
            },
        })
    return (updates, events)
