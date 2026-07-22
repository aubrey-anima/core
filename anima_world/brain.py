"""Brain: assembles Agent + ActionTable + BT and chooses actions from events."""

from __future__ import annotations

from typing import Any

from anima_world.actions import ActionDescriptor, ActionTable
from anima_world.agent import Agent
from anima_world.bt_nodes import Status, tick

# The BT leaf id that means "do whatever the planner scheduled for right now".
FOLLOW_PLAN_ID = "follow_plan"


class Brain:
    """Per-agent decision engine.

    Combines:
    - Agent (state + blackboard + BT root)
    - ActionTable (node id → action descriptor)
    """

    def __init__(self, agent: Agent, action_table: ActionTable) -> None:
        self.agent = agent
        self.action_table = action_table

    def tick(self, events: list[dict[str, Any]]) -> ActionDescriptor | None:
        """Process events, optionally generate idle, run BT, return chosen action.

        Returns None when no action is taken (not idle, or BT has no applicable node).
        """
        if events:
            self._apply_events(events)
            self.agent.mark_active()

        # Idle path: if agent has been idle too long, fabricate an idle event
        if not events and self.agent.is_idle():
            self.agent.mark_active()
            # Idle events don't carry data — they just wake the BT
            return self.tick_direct()

        return None

    def tick_direct(self) -> ActionDescriptor | None:
        """Run the BT against the current blackboard and resolve an action.

        Does NOT check idle timeout — used directly by tests or after
        events are already applied.
        """
        blackboard = self.agent.blackboard
        status = tick(self.agent.bt_root, blackboard)

        if status != Status.SUCCESS:
            return None

        # The BT configuration embeds "what action" inside the successful Action node.
        # We expose a helper: store the selected action node id on the blackboard
        # during traversal (see Action._selected_node fallback).
        node_id = blackboard.read("_selected_action_id")
        if node_id is None:
            # Fallback: BT succeeded without recording a specific action id — return idle
            return ActionDescriptor("idle_wander")

        if node_id == FOLLOW_PLAN_ID:
            # The planner's step is dynamic (walk to X, chat with Y), so it
            # cannot be a static row in the action table — the descriptor is
            # built from what the scheduler wrote on the blackboard this tick.
            kind = blackboard.read("plan.kind")
            if not kind:
                return ActionDescriptor("idle_wander")
            return ActionDescriptor(kind, dict(blackboard.read("plan.params") or {}))

        return self.action_table.lookup(node_id)

    def _apply_events(self, events: list[dict[str, Any]]) -> None:
        """Update blackboard from incoming events.

        An agent's own action events are broadcast to every mailbox (that is
        how agents notice each other), so `who` — not mere delivery — decides
        whose state an event changes. Applying someone else's `location_join`
        to my own blackboard would teleport me wherever they walked; that bug
        lay dormant only because nothing ever moved (100% idle_wander).
        """
        for ev in events:
            payload = ev.get("payload", {})
            kind = payload.get("kind")
            # Make event data available to BT conditions (mine or not).
            self.agent.blackboard.write("last_event", ev)
            is_own = ev.get("who") == self.agent.id

            if kind == "location_join" and is_own:
                self.agent.blackboard.write("loc", payload.get("location"))
            elif kind == "agent_state" and is_own:
                for k, v in payload.get("state", {}).items():
                    self.agent.blackboard.write(f"state.{k}", v)
            elif kind and not is_own:
                self.agent.blackboard.write(f"kind_{kind}", payload)
