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


# ── economy-v4: the ledger is a projection — audit = replay ────────────────


def _apply_payment(proj: Projection, e: Event) -> None:
    payload = e.payload
    src, dst = payload.get("from"), payload.get("to")
    try:
        amount = float(payload.get("amount", 0.0))
    except (TypeError, ValueError):
        return
    if not src or not dst or amount <= 0:
        return
    proj.balances[src] = proj.balances.get(src, 0.0) - amount
    proj.balances[dst] = proj.balances.get(dst, 0.0) + amount


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
    holding = proj.inventories.get(who)
    if holding and item_id in holding:  # 直接吃货架上的餐不经过随身库存,no-op
        holding[item_id] -= 1
        if holding[item_id] <= 0:
            holding.pop(item_id, None)


def _apply_agent_join(proj: Projection, e: Event) -> None:
    payload = e.payload
    agent_id = e.who
    if agent_id is None:
        return
    spec = payload.get("spec", {})
    location = payload.get("location")
    proj.agents[agent_id] = AgentState(
        spec=spec,
        state=payload.get("state", {}),
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
        agent.state.update(payload.get("state", {}))
        agent.updated_at = e.ts

    elif kind == "location_join":
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


def _apply_narrative(proj: Projection, e: Event) -> None:
    """M2: append narrative entry to projection.narrative_log."""
    agent_id = e.who
    speaker = e.payload.get("speaker", agent_id)
    text = e.payload.get("text", "")
    entry: dict[str, Any] = {
        "agent": agent_id,
        "speaker": speaker,
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
    proj.narrative_log.append({
        "agent": e.who or "user",
        "speaker": "user",
        "text": text,
        "ts": e.ts,
    })
