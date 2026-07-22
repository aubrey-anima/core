"""Built-in default map — the store-less fallback only.

The map's real source of truth is the `locations` table (`world_store.
LocationStore`): an adjacency tree of regions and points with parent-relative
0~1 geometry (nested-map D7). This module holds nothing but a minimal default
point set, used where no DB is available: seeding a fresh world when
`world_seed.json` is unreadable, and `World.state()` on a scheduler built without
a `LocationStore` (the headless `story` path).

The flat 6×6 `GRID` dict, `get_coords`, `get_exits` and `agents_positions` are
gone: coordinates are no longer integers on a grid, and exits never had a
reader (`walk` has always been unconstrained).
"""

from __future__ import annotations

from typing import Any

# Rows in the `locations` table's shape: top-level points, each centered in
# what used to be its cell on the old 6×6 grid.
DEFAULT_POINTS: list[dict[str, Any]] = [
    {"id": "cafe", "name": "cafe", "description": "", "kind": "point",
     "parent": None, "x": 0.25, "y": 0.25},
    {"id": "workshop", "name": "workshop", "description": "", "kind": "point",
     "parent": None, "x": 0.5833, "y": 0.4167},
    {"id": "home", "name": "home", "description": "", "kind": "point",
     "parent": None, "x": 0.9167, "y": 0.9167},
]

DEFAULT_POINT_IDS: list[str] = [p["id"] for p in DEFAULT_POINTS]
