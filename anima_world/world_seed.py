"""Shared validation for authored world seed data."""

from __future__ import annotations

from typing import Any

WORLD_SEED_AGENT_KEYS = frozenset({"id", "name", "location", "personality"})
WORLD_SEED_LOCATION_KEYS = frozenset({"id", "name", "description"})


class WorldSeedError(ValueError):
    """A seed the caller explicitly asked for cannot be used.

    Only ever raised for an explicit `--seed` / `seed_path`: the bundled seed
    still degrades to hardcoded defaults, because a world must be able to boot
    even with a damaged install. An authored seed is the opposite case — the
    caller named the world they want, and quietly handing them the built-in
    demo world instead is unrecoverable (a seed is read once, into an empty
    database).
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid world seed:\n" + "\n".join(f"- {e}" for e in errors))


def world_seed_errors(data: Any) -> list[str]:
    """Say what is wrong with *data*, one line per problem (empty = valid).

    The mirrored contract is `is_valid_world_seed`'s verdict, which is derived
    from this list so the two can never drift. This function only explains it.
    """
    if not isinstance(data, dict):
        return [f"seed must be a JSON object, got {type(data).__name__}"]

    errors: list[str] = []
    for field, keys in (("agents", WORLD_SEED_AGENT_KEYS), ("locations", WORLD_SEED_LOCATION_KEYS)):
        entries = data.get(field)
        if not isinstance(entries, list):
            found = "missing" if field not in data else type(entries).__name__
            errors.append(f"'{field}' must be a list ({found})")
            continue
        for index, entry in enumerate(entries):
            label = f"{field}[{index}]"
            if not isinstance(entry, dict):
                errors.append(f"{label} must be an object, got {type(entry).__name__}")
                continue
            missing = sorted(keys - entry.keys())
            if missing:
                name = entry.get("id") or entry.get("name")
                where = f"{label} ({name!r})" if name else label
                errors.append(f"{where} is missing {', '.join(repr(k) for k in missing)}")
    return errors


def is_valid_world_seed(data: Any) -> bool:
    """Return whether *data* satisfies the stable minimum seed contract."""
    return not world_seed_errors(data)
