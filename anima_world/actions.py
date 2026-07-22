"""Action table: node id → action descriptor, and action → M1 event mapping."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ActionDescriptor:
    """A concrete action: kind + params (target, location, sentiment, …)."""

    kind: str
    params: dict[str, Any] = field(default_factory=dict)


class ActionTable:
    """Maps BT node ids to ActionDescriptors; fallback = idle_wander."""

    def __init__(self, table: dict[str, ActionDescriptor] | None = None) -> None:
        self._table: dict[str, ActionDescriptor] = table or {}

    def lookup(self, node_id: str) -> ActionDescriptor:
        return self._table.get(node_id, ActionDescriptor("idle_wander"))

    @classmethod
    def default(cls) -> ActionTable:
        """Default action table covering the autonomous M2 action kinds.

        Chat is handled by the standalone M3.5 chat subsystem, not the BT, so
        there is no user-reply action here.
        """
        return cls(
            {
                "go_to_cafe": ActionDescriptor("walk", {"location": "cafe"}),
                "go_to_workshop": ActionDescriptor("walk", {"location": "workshop"}),
                "go_to_warehouse": ActionDescriptor("walk", {"location": "warehouse"}),
                "go_home": ActionDescriptor("walk", {"location": "home"}),
                "chat_with_bob": ActionDescriptor("chat", {"target": "bob"}),
                "chat_with_alice": ActionDescriptor("chat", {"target": "alice"}),
                "do_work": ActionDescriptor("work", {"location": "workshop"}),
                "go_sleep": ActionDescriptor("sleep", {}),
                "idle_wander": ActionDescriptor("idle_wander", {}),
                "idle_social": ActionDescriptor("idle_social", {}),
            }
        )


def to_event(
    action: ActionDescriptor,
    agent_id: str,
) -> list[dict[str, Any]]:
    """Convert one ActionDescriptor to one or more M1-compatible events.

    Returns a list (chat emits 2 events: A's initiative + B's receive).
    """
    kind = action.kind
    p = action.params

    if kind == "walk":
        return [
            {
                "type": "state_change",
                "who": agent_id,
                "payload": {"kind": "location_join", "location": p.get("location")},
            }
        ]

    if kind == "work":
        return [
            {
                "type": "state_change",
                "who": agent_id,
                "payload": {
                    "kind": "agent_state",
                    "state": {"status": "working", "location": p.get("location")},
                },
            }
        ]

    if kind == "sleep":
        return [
            {
                "type": "state_change",
                "who": agent_id,
                "payload": {"kind": "agent_state", "state": {"status": "sleeping"}},
            }
        ]

    # llm-relationship-judge: chat no longer hardcodes a sentiment pair —
    # a seeded -0.7 enmity was overwritten to +0.1 by one small talk (w1
    # Round-3 smoke). The chat falls through to the generic agent_action
    # shape below; relationship deltas are judged asynchronously by the
    # RelationshipJudge after the chat lands. The legacy `sentiment` param
    # on ActionDescriptor("chat") is deprecated and ignored.

    # chat, idle_wander, idle_social, or any custom action
    return [
        {
            "type": "agent_action",
            "who": agent_id,
            "payload": {"action": kind, **p},
        }
    ]
