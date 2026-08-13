"""Fold an event stream into an in-memory Projection."""

from __future__ import annotations

from typing import Any

from anima_world.types import AgentState, Capability, Event, Projection, Relation


def project_events(events: list[Event], base: Projection | None = None) -> Projection:
    """Fold events into a Projection, optionally on top of a base projection.

    M1 supports 4 event types: agent_join, agent_move, state_change, location_join.
    Unknown event types are silently ignored (projection is lossy by design;
    full history lives in the events table).
    """
    proj = base if base is not None else Projection()
    for e in events:
        _apply_event(proj, e)
    return proj


def _apply_event(proj: Projection, e: Event) -> None:
    if e.type == "agent_join":
        _apply_agent_join(proj, e)
    elif e.type == "agent_move":
        _apply_agent_move(proj, e)
    elif e.type == "state_change":
        _apply_state_change(proj, e)
    elif e.type == "location_join":
        _apply_location_join(proj, e)
    elif e.type == "agent_action":
        _apply_agent_action(proj, e)
    elif e.type == "agent_idle":
        _apply_agent_idle(proj, e)
    elif e.type == "narrative":
        _apply_narrative(proj, e)
    elif e.type == "user_message":
        _apply_user_message(proj, e)
    elif e.type == "capability_registered":
        _apply_capability_registered(proj, e)
    elif e.type == "agent_leave":
        _apply_agent_leave(proj, e)
    elif e.type == "agent_return":
        _apply_agent_return(proj, e)
    elif e.type == "payment":
        _apply_payment(proj, e)
    elif e.type == "item_transfer":
        _apply_item_transfer(proj, e)
    elif e.type == "item_consume":
        _apply_item_consume(proj, e)
    elif e.type == "player_departed":
        _apply_player_departed(proj, e)


def _apply_player_departed(proj: Projection, e: Event) -> None:
    """「这个人离开了这个世界」—— 折掉他和所有人之间的关系,两个方向都折。

    **为什么这是一条事件而不是一次删除。** 关系不是一张表,是
    `state_change/sentiment_delta` 折出来的投影。手删投影里那一行,下一次重放
    (换个进程、重启一次、`catch_up_projection` 一次)会原样把它折回来 —— 世界
    照跑、日志干净,而"她还惦记着一个不存在的人"这件事一天之内自己长回来。
    追加一条事实,重放才收敛;**对账即重放**这条不变量因此仍然成立。

    **它不碰历史。** 那些 `sentiment_delta` 一条不少地留在日志里,记忆、转录、
    账本也一样 —— 她记得这个人来过,只是不再等他。走的是**朝前看**的那一半。
    """
    player_id = str(e.payload.get("player_id") or "").strip()
    if not player_id:
        return
    for key in [k for k in proj.relations if player_id in k]:
        proj.relations.pop(key, None)


# ── economy-v4: the ledger is a projection — audit = replay ────────────────


def _apply_payment(proj: Projection, e: Event) -> None:
    """一笔钱从一头挪到另一头。**进位在这儿,不在显示那一层。**

    二进制浮点存不下 0.1,所以 `60 − 5.23 − 1.16 − …` 折下来是
    `0.3799999999999921`。前端 `toFixed(2)` 把它盖住了,而账本是这个引擎对"钱"
    的定义、不是屏幕上那一行:门禁读的是这个数,一笔"正好够"的交易迟早会被它
    拒掉,而那一次不报错也不留痕。钱有最小单位,所以折账的时候就进位。

    **两头一起进** —— 单边进的话玩家少的和镇上多的对不上,账本不再守恒。
    """
    payload = e.payload
    src, dst = payload.get("from"), payload.get("to")
    try:
        amount = float(payload.get("amount", 0.0))
    except (TypeError, ValueError):
        return
    if not src or not dst or amount <= 0:
        return
    proj.balances[src] = round(proj.balances.get(src, 0.0) - amount, 2)
    proj.balances[dst] = round(proj.balances.get(dst, 0.0) + amount, 2)
    # 见面礼发过没有,记在账本这一侧 —— 理由见 `Projection.allowances`。
    if payload.get("reason") == "allowance":
        proj.allowances.add(str(dst))


def _apply_item_transfer(proj: Projection, e: Event) -> None:
    payload = e.payload
    src, dst, item_id = payload.get("from"), payload.get("to"), payload.get("item_id")
    try:
        qty = int(payload.get("qty", 1))
    except (TypeError, ValueError):
        return
    if not item_id or qty <= 0 or not dst:
        return
    if src:
        holding = proj.inventories.setdefault(src, {})
        holding[item_id] = holding.get(item_id, 0) - qty
        if holding[item_id] <= 0:
            holding.pop(item_id, None)
    receiving = proj.inventories.setdefault(dst, {})
    receiving[item_id] = receiving.get(item_id, 0) + qty


def _apply_item_consume(proj: Projection, e: Event) -> None:
    payload = e.payload
    who, item_id = payload.get("who") or e.who, payload.get("item_id")
    if not who or not item_id:
        return
    try:
        qty = int(payload.get("qty", 1))
    except (TypeError, ValueError):
        qty = 1
    if qty <= 0:
        return
    holding = proj.inventories.get(who)
    if holding and item_id in holding:  # 直接吃货架上的餐不经过随身库存,no-op
        holding[item_id] -= qty
        if holding[item_id] <= 0:
            holding.pop(item_id, None)


def _doing(state: Any) -> dict[str, Any]:
    """`state` 装的是她**在干什么**,不装她**在哪**。

    "在哪"只有一个家:`location_join` 折出来的 `agent.location`。而 `state` 是个
    自由字典,谁都能往里塞一个 `location` —— 塞进去之后它只增不减(`work` 写,
    `sleep` 和 `walk` 都不清),于是 `World.state()` 对同一个问题给出两个答案,
    宿主读哪个全凭运气。线上量出来是 21 个人里 13 个对不上。

    **过滤要留在读事件这一侧**,光修 `actions.py` 不够:投影是重放事件折出来的,
    历史里每一条老 `work` 事件都会把那份陈旧的拷贝原样带回来。
    """
    if not isinstance(state, dict):
        return {}
    # 拷贝之后再删 —— 事件那个 dict 是**已经发生过的事实**,就地改它等于改写历史
    # 的显示(见 `_apply_agent_join` 里 spec 那段的教训)。
    return {k: v for k, v in state.items() if k != "location"}


def _apply_agent_join(proj: Projection, e: Event) -> None:
    payload = e.payload
    agent_id = e.who
    if agent_id is None:
        return
    spec = payload.get("spec", {})
    location = payload.get("location")
    proj.agents[agent_id] = AgentState(
        # **拷一份,别拿事件那个 dict 当自己的状态。** 投影是从事件折出来的**派生
        # 数据**,而事件是已经发生过的**事实** —— 共用一个可变 dict 的话,后来的
        # 一条 `persona_update` 会顺着 `agent.spec.update(...)` 就地改写那条创世
        # `agent_join` 在内存里的样子。于是"那条事件说了什么"有两个答案:
        # `World.events()`(内存窗口)说有 goals,`events export` 与任何一次重放说
        # 没有 —— 而日志才是对的。
        #
        # 没造成过数据损失(goals 自己有一条 persona_update 事件),但这是个地雷:
        # 投影往后加的任何一处写,都会静默地改写"历史显示成什么样"。
        spec=dict(spec) if isinstance(spec, dict) else spec,
        state=_doing(payload.get("state")),
        location=location,
        joined_at=e.ts,
        updated_at=e.ts,
    )


def _apply_agent_move(proj: Projection, e: Event) -> None:
    agent_id = e.who
    if agent_id is None:
        return
    # payload to_loc wins over event.loc (loc is only a fallback for current location)
    to_loc = e.payload.get("to_loc") or e.loc
    if to_loc is None:
        return
    agent = proj.agents.get(agent_id)
    if agent is None:
        # §3.2 constraint: system does not question event authenticity; create placeholder
        agent = AgentState(spec={}, joined_at=e.ts)
        proj.agents[agent_id] = agent
    agent.location = to_loc
    agent.updated_at = e.ts


def _apply_state_change(proj: Projection, e: Event) -> None:
    """Apply state_change events. Supported sub-kinds via payload['kind']:
    - 'sentiment': set relations[(as, target).sentiment] = payload['sentiment']
    - 'r_type': set relations[(as, target).r_type/r_type_back]
    - 'agent_state': merge payload['state'] into agents[who].state
    - 'location_join': walk-emitted, no-ops without payload['id'] (see actions.py)
    - 'agent_join_fallback': create agents[who] if missing (for injected memory)
    - 'persona_update': merge payload['spec'] into agents[who].spec (M6)
    - 'location_desc_update': set locations[loc_id].description (M6)
    """
    payload = e.payload
    kind = payload.get("kind", "sentiment")

    if kind == "sentiment":
        as_id = payload.get("as") or e.who
        target_id = payload.get("target")
        if as_id is None or target_id is None:
            return
        key = (as_id, target_id)
        rel = proj.relations.get(key)
        if rel is None:
            rel = Relation()
            proj.relations[key] = rel
        rel.sentiment = float(payload["sentiment"])

    elif kind == "sentiment_delta":
        # llm-relationship-judge: relationship as an INTEGRAL of history.
        # The absolute `sentiment` kind assigns (genesis injection keeps it);
        # deltas accumulate — one small talk can no longer erase a seeded
        # -0.7 enmity by overwriting it with +0.1 (w1 Round-3 smoke).
        as_id = payload.get("as") or e.who
        target_id = payload.get("target")
        if as_id is None or target_id is None:
            return
        key = (as_id, target_id)
        rel = proj.relations.get(key)
        if rel is None:
            rel = Relation()
            proj.relations[key] = rel
        try:
            delta = float(payload.get("delta", 0.0))
        except (TypeError, ValueError):
            return
        rel.sentiment = max(-1.0, min(1.0, rel.sentiment + delta))
        # relations-v5: optional finer axes ride the same event; absent or
        # malformed axes leave the single-axis fold untouched (old events
        # replay identically).
        axes = payload.get("axes")
        if isinstance(axes, dict):
            for axis in ("trust", "affection", "respect"):
                if axis in axes:
                    try:
                        step = float(axes[axis])
                    except (TypeError, ValueError):
                        continue
                    value = getattr(rel, axis) + step
                    setattr(rel, axis, max(-1.0, min(1.0, value)))
        # 出处:上一次改变它的就是**这一条**。和上面几行同一次折叠 ——
        # 一句"你们更亲近了"如果说不出出处,和一根进度条没有区别。
        conversation_id = payload.get("conversation_id")
        try:
            conversation_id = None if conversation_id is None else int(conversation_id)
        except (TypeError, ValueError):
            conversation_id = None
        rel.last_change = {
            "seq": e.seq,
            "tick": e.ts,
            "delta": delta,
            "direction": "up" if delta > 0 else ("down" if delta < 0 else "flat"),
            "conversation_id": conversation_id,
            "summary": str(payload.get("conversation_summary") or ""),
            "as_name": str(payload.get("as_name") or ""),
            "target_name": str(payload.get("target_name") or ""),
        }

    elif kind == "r_type":
        as_id = payload.get("as") or e.who
        target_id = payload.get("target")
        if as_id is None or target_id is None:
            return
        key = (as_id, target_id)
        rel = proj.relations.get(key)
        if rel is None:
            rel = Relation()
            proj.relations[key] = rel
        rel.r_type = payload.get("r_type", rel.r_type)
        rel.r_type_back = payload.get("r_type_back", rel.r_type_back)

    elif kind == "agent_state":
        agent_id = e.who
        if agent_id is None or agent_id not in proj.agents:
            return
        agent = proj.agents[agent_id]
        agent.state.update(_doing(payload.get("state")))
        agent.updated_at = e.ts

    elif kind == "location_join":
        # 角色走进一个地点(actions.py 的 walk 发的就是这条)。**这是"谁在哪"
        # 的唯一持续来源** —— agent_join 只说得出出生地。位置必须落进投影:
        # 开机 roster 读的正是 `projected.location`(__main__.py:334),投影不
        # 更新就等于每次重开把所有人传送回出生地,而世界照跑、没有任何报错。
        location = payload.get("location")
        if location:
            agent = proj.agents.get(e.who) if e.who else None
            if agent is not None:  # 未知角色不造占位:名册的权威是 agent_join
                agent.location = location
                agent.updated_at = e.ts
            return
        # 老分支:没有 location 的 location_join 是"注册一个地点"。地点表的
        # 权威是 `locations` 表,这里只兜底任何历史里已有的这类事件。
        loc_id = payload.get("id")
        if loc_id is None:
            return
        from anima_world.types import Location
        proj.locations[loc_id] = Location(
            id=loc_id,
            name=payload.get("name", loc_id),
            description=payload.get("description", ""),
        )

    elif kind == "agent_join_fallback":
        agent_id = e.who
        if agent_id is None:
            return
        if agent_id not in proj.agents:
            proj.agents[agent_id] = AgentState(
                spec=payload.get("spec", {}),
                joined_at=e.ts,
                updated_at=e.ts,
            )
        if payload.get("location"):
            proj.agents[agent_id].location = payload["location"]

    elif kind == "persona_update":
        agent_id = e.who
        if agent_id is None:
            return
        agent = proj.agents.get(agent_id)
        if agent is None:
            agent = AgentState(joined_at=e.ts)
            proj.agents[agent_id] = agent
        agent.spec.update(payload.get("spec", {}))
        agent.updated_at = e.ts

    # nested-map D7: `location_desc_update` is retired — the map is
    # configuration (the `locations` table), not history. Events of that
    # sub-kind survive in old logs and fall through here, ignored, like any
    # other unknown kind.


def _apply_location_join(proj: Projection, e: Event) -> None:
    payload = e.payload
    loc_id = payload.get("id") or e.loc
    if loc_id is None:
        return
    from anima_world.types import Location
    proj.locations[loc_id] = Location(
        id=loc_id,
        name=payload.get("name", loc_id),
        description=payload.get("description", ""),
    )


def _apply_agent_action(proj: Projection, e: Event) -> None:
    """M2: record agent_action — update state with last_action + bt_node_path."""
    agent_id = e.who
    if agent_id is None or agent_id not in proj.agents:
        return
    agent = proj.agents[agent_id]
    action_name = e.payload.get("action", "unknown")
    agent.state["last_action"] = action_name
    agent.state["bt_node_path"] = e.payload.get("path", "")
    agent.updated_at = e.ts


def _apply_agent_idle(proj: Projection, e: Event) -> None:
    """M2: record agent_idle — update state with last_idle_ts."""
    agent_id = e.who
    if agent_id is None or agent_id not in proj.agents:
        return
    agent = proj.agents[agent_id]
    agent.state["last_idle_ts"] = e.ts
    agent.state["last_action"] = "idle"
    agent.updated_at = e.ts


def _speaker_name(proj: Projection, speaker: str | None, payload: dict[str, Any]) -> str:
    """发言人的**人话名**,永远非空。

    三级回落:事件自己带的 → 名册(`agent_join` 那条事件里的 spec)→ id 本身。

    **中间那一级是为了老日志。** 线上世界的库里已经躺着几千条只有 `speaker`
    的 `narrative`,而重放是这个引擎的真相模型 —— 只在发射端补名字,等于说
    "这个 bug 修好了,但你已经有的历史永远是坏的"。名册就在同一条日志里
    (`agent_join.payload.spec.name`),重放到这一条时它必然已经折进来了。
    """
    given = str(payload.get("speaker_name") or "").strip()
    if given:
        return given
    if speaker:
        agent = proj.agents.get(speaker)
        if agent is not None:
            name = str(agent.spec.get("name") or "").strip()
            if name:
                return name
        return speaker
    return ""


def _apply_narrative(proj: Projection, e: Event) -> None:
    """M2: append narrative entry to projection.narrative_log."""
    agent_id = e.who
    speaker = e.payload.get("speaker", agent_id)
    text = e.payload.get("text", "")
    entry: dict[str, Any] = {
        "agent": agent_id,
        "speaker": speaker,
        # **加一个字段,不改 `speaker` 的含义。** `speaker` 是宿主已经在用的
        # 机器可读键(去重、按人过滤都靠它),把它换成名字等于跨仓库破坏。
        # 而它同时被当成"发言人"渲染在玩家脸上 —— 那一半由这个字段接手。
        "speaker_name": _speaker_name(proj, speaker, e.payload) or (agent_id or ""),
        "text": text,
        "ts": e.ts,
    }
    proj.narrative_log.append(entry)


def _apply_agent_leave(proj: Projection, e: Event) -> None:
    """agent-leave-return: mark the agent off-stage. Replay/observation only —
    the running world's presence truth is scheduler.agents membership (D2)."""
    agent = proj.agents.get(e.who) if e.who else None
    if agent is None:
        return
    agent.state["away"] = True
    agent.updated_at = e.ts


def _apply_agent_return(proj: Projection, e: Event) -> None:
    if e.who is None:
        return
    agent = proj.agents.get(e.who)
    if agent is None:
        agent = AgentState(joined_at=e.ts)
        proj.agents[e.who] = agent
    agent.state["away"] = False
    loc = e.payload.get("location") or e.loc
    if loc:
        agent.location = loc
    agent.updated_at = e.ts


def _apply_capability_registered(proj: Projection, e: Event) -> None:
    """M6: register/replace a capability catalog entry. Ignores events missing 'id'."""
    payload = e.payload
    cap_id = payload.get("id")
    if cap_id is None:
        return
    proj.capabilities[cap_id] = Capability(
        id=cap_id,
        kind=payload.get("kind", ""),
        description=payload.get("description", ""),
        params_schema=payload.get("params_schema", {}),
    )


def _apply_user_message(proj: Projection, e: Event) -> None:
    """M3: append user_message to narrative_log with speaker='user'."""
    text = e.payload.get("text", "")
    payload = e.payload
    # `speaker` 是字面量 `"user"` —— 它同样会被渲染成发言人,所以这条也要有名字。
    # 宿主给了就用宿主的(玩家的名字只有宿主知道),没给退回一句人话。
    name = str(
        payload.get("speaker_name") or payload.get("player_name") or ""
    ).strip() or "玩家"
    proj.narrative_log.append({
        "agent": e.who or "user",
        "speaker": "user",
        "speaker_name": name,
        "text": text,
        "ts": e.ts,
    })
