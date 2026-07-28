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


def world_seed_warnings(data: Any) -> list[str]:
    """不阻止开机、但作者八成写错了的地方(引用完整性),一行一条。

    **只警告、绝不拒绝**,这是有意的。引擎没有"合法值全集"这种东西:自定义动作、
    节拍脚本里中途 `agent_join` 进来的角色、只被引用就自动补定义的物品 —— 全都合法。
    把 advisory 升级成拒绝,会让一个设计正确的世界在一次小版本升级之后开不了机,
    那是把"照跑但给错东西"换成"本来能跑却不让跑",后者更糟。

    所以这里只查**在这份文件内部就能确定是错的**那几类。
    """
    if not isinstance(data, dict):
        return []
    warnings: list[str] = []

    def _entries(field: str) -> list[dict[str, Any]]:
        value = data.get(field)
        return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []

    agents, locations = _entries("agents"), _entries("locations")
    location_ids = {str(loc.get("id")) for loc in locations if loc.get("id")}
    agent_ids = {str(a.get("id")) for a in agents if a.get("id")}

    for field, entries in (("agents", agents), ("locations", locations)):
        seen: set[str] = set()
        for entry in entries:
            entry_id = str(entry.get("id") or "")
            if entry_id and entry_id in seen:
                # 后一条静默覆盖前一条,而两条都是作者写的。
                warnings.append(f"{field}: id {entry_id!r} 出现了不止一次,后面那条会覆盖前面")
            seen.add(entry_id)
        if not entries:
            warnings.append(f"{field}: 一条都没有")

    for index, agent in enumerate(agents):
        where = str(agent.get("location") or "")
        name = agent.get("id") or agent.get("name") or f"agents[{index}]"
        if where and where not in location_ids:
            # 地点表只从 seed 的 locations 播种(__main__._seed_world_defs),
            # 不会因为被引用就自动补 —— 这个角色会站在一个没有定义的地方。
            warnings.append(
                f"agents[{index}] ({name}): location {where!r} 不在 locations 里,"
                f"已定义的是 {sorted(location_ids)}"
            )
        for relation in agent.get("relations") or []:
            if not isinstance(relation, dict):
                continue
            target = str(relation.get("with") or "")
            if target and target not in agent_ids:
                warnings.append(
                    f"agents[{index}] ({name}): relations.with {target!r} 不是这份种子里的角色"
                    "(如果他由节拍脚本中途入场,忽略这条)"
                )
    return warnings
