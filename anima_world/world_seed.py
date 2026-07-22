"""Shared validation for authored world seed data."""

from __future__ import annotations

from typing import Any

WORLD_SEED_AGENT_KEYS = frozenset({"id", "name", "location", "personality"})
WORLD_SEED_LOCATION_KEYS = frozenset({"id", "name", "description"})


def is_valid_world_seed(data: Any) -> bool:
    """Return whether *data* satisfies the stable minimum seed contract."""
    if not isinstance(data, dict):
        return False
    agents, locations = data.get("agents"), data.get("locations")
    if not isinstance(agents, list) or not isinstance(locations, list):
        return False
    if not all(isinstance(agent, dict) and WORLD_SEED_AGENT_KEYS.issubset(agent) for agent in agents):
        return False
    return all(
        isinstance(location, dict) and WORLD_SEED_LOCATION_KEYS.issubset(location)
        for location in locations
    )
