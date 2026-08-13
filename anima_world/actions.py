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
                "eat": ActionDescriptor("eat", {}),
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
        # **只说她在干什么,不说她在哪。** "她在哪"的权威是 `location_join`
        # 折出来的 `agent.location`,而这里曾经顺手在 `state` 里再写一份 ——
        # 于是 `World.state()` 对同一个问题给出两个答案,一个跟着她走、一个停在
        # 她上一次干活的地方。`sleep` 不写它,`walk` 也不清它,所以那份拷贝只增不减:
        # 线上 21 个人里 13 个的 `state.location` 和她真实的位置对不上。
        # 更糟的是这个位置本来就可能是假的 —— 行为树的 `do_work` 把它写死成
        # `workshop`,而她可能正在码头。
        return [
            {
                "type": "state_change",
                "who": agent_id,
                "payload": {"kind": "agent_state", "state": {"status": "working"}},
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
