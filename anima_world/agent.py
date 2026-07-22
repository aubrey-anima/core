"""Agent entity: id, name, blackboard, behavior tree root, location, idle tracking."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from anima_world.bt_nodes import Blackboard, Node


@dataclass
class Agent:
    """An autonomous agent carrying its own blackboard, BT root, location, and idle state."""

    id: str
    name: str
    bt_root: Node
    location: str
    blackboard: Blackboard = field(default_factory=Blackboard)
    idle_timeout: float = 30.0  # seconds before idle triggers
    last_active_ts: float = 0.0  # epoch seconds of last event arrival

    def is_idle(self) -> bool:
        """True if no event has arrived for idle_timeout seconds."""
        if self.last_active_ts == 0.0:
            return True
        return (time.monotonic() - self.last_active_ts) >= self.idle_timeout

    def mark_active(self) -> None:
        """Reset idle timer (call when an event arrives)."""
        self.last_active_ts = time.monotonic()
