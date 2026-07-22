"""Core dataclasses for the anima_world event engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    seq: int
    ts: int
    type: str
    loc: str | None
    payload: dict[str, Any]
    who: str | None = None

    def to_row(self) -> tuple:
        """Convert to a tuple for SQLite insertion (seq omitted—AUTOINCREMENT)."""
        import json

        return (self.ts, self.type, self.who, self.loc, json.dumps(self.payload))

    @classmethod
    def from_row(cls, row: tuple) -> "Event":
        """Build Event from a SQLite row (seq, ts, type, who, loc, payload)."""
        import json

        seq, ts, type_, who, loc, payload_json = row
        return cls(seq=seq, ts=ts, type=type_, who=who, loc=loc, payload=json.loads(payload_json))


@dataclass
class AgentState:
    spec: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    location: str | None = None
    joined_at: int = 0
    updated_at: int = 0


@dataclass
class Relation:
    r_type: str = "acquaintance"
    r_type_back: str = "acquaintance"
    sentiment: float = 0.0


@dataclass
class Location:
    id: str = ""
    name: str = ""
    description: str = ""


@dataclass
class Capability:
    id: str = ""
    kind: str = ""
    description: str = ""
    params_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class Projection:
    agents: dict[str, AgentState] = field(default_factory=dict)
    relations: dict[tuple[str, str], Relation] = field(default_factory=dict)
    locations: dict[str, Location] = field(default_factory=dict)
    narrative_log: list[dict[str, Any]] = field(default_factory=list)
    """M2: accumulated narrative entries (agent, text, ts) — backward compat default empty."""
    capabilities: dict[str, Capability] = field(default_factory=dict)
    """M6: capability catalog, folded from capability_registered events."""
