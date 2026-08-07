"""Shared validation for authored world seed data.

**镜像契约只有一件事**:`is_valid_world_seed` 的裁决(运维台 `lib/worldSeed.js` 镜像
了它)。裁决只看 `agents` / `locations` 的必填键 —— 未知顶层字段一律忽略,所以往
种子里加可选字段(`relations` / `memories` / `items` / `config` …)**不改跨仓库契约**,
不需要同步改镜像端。这条是有意的:世界的丰富度会一直长,而裁决面必须稳。
"""

from __future__ import annotations

from typing import Any

WORLD_SEED_AGENT_KEYS = frozenset({"id", "name", "location", "personality"})
WORLD_SEED_LOCATION_KEYS = frozenset({"id", "name", "description"})

# 种子可以在创世时点亮的开关(`"config": {...}`)。**密文键一律不许**:种子是要
# 分发的东西(`.cyberworld` 里就带着它),一个能携带 `llm.api_key` 的种子等于把
# 作者的钥匙寄给了每一个拿到这个世界的人。密钥永远只在本机的 `world.db.key` 里。
WORLD_SEED_CONFIG_FORBIDS_SECRETS = True


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


def world_seed_errors(data: Any, *, complete: bool = True) -> list[str]:
    """Say what is wrong with *data*, one line per problem (empty = valid).

    The mirrored contract is `is_valid_world_seed`'s verdict, which is derived
    from this list so the two can never drift. This function only explains it.

    **`complete=False` 是"这是一次编辑,不是一个世界"。**

    v3 起,把一份只含 `author` 记录的文件装进一个**已有**的世界就是一次编辑
    (创作台要的"增删改查创世态"由此免费得到)。而这道闸原先不管目标世界空不空,
    一律要求 `agents` 和 `locations` —— 于是那条能力**在文档里写着,实际用不了**:
    想给一个跑着的世界补一层 `kinds`,会被"'agents' must be a list (missing)" 挡回来,
    而那个世界的名册明明就在它自己的库里。

    写了的段照旧逐条查(写错的 `agents` 仍然不许进);**只是不再要求你把没打算改的
    那些段也抄一遍** —— 抄一遍才是真正危险的事:那份抄件迟早和世界里的不一致。
    """
    if not isinstance(data, dict):
        return [f"seed must be a JSON object, got {type(data).__name__}"]

    errors: list[str] = []
    for field, keys in (("agents", WORLD_SEED_AGENT_KEYS), ("locations", WORLD_SEED_LOCATION_KEYS)):
        entries = data.get(field)
        if entries is None and not complete:
            continue      # 这一段这次不改
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


def apply_seed_config(config_store: Any, world_seed: dict[str, Any] | None) -> dict[str, Any]:
    """把种子的 `"config": {...}` 写进这个新世界(**创世时一次**,调用方保证空库)。

    这是"出厂内置世界能展示自己"的那一半:开关此前只能建完世界再一条条
    `config set`,于是**一个内置的演示世界无法点亮自己的任何特性** —— 装上包、
    `anima-world start`,看到的是毛坯,而这个引擎大半的能力开箱不可见。

    宽容方向是**跳过而不是拒绝**,但绝不无声(返回值就是给调用方点名用的):

    - **未知键跳过。** 种子会比引擎活得久:一个 1.4 的种子写了 1.3 没有的开关,
      正确的行为是开机并少点亮一项,而不是让整个世界打不开。
    - **密文键拒绝。** 种子是分发物,不许携带 `llm.api_key`(见
      `WORLD_SEED_CONFIG_FORBIDS_SECRETS`)。
    - **类型转不过去的跳过。** 按声明类型强转(和 `World.config_set` 共用同一份规则),
      转不了就是作者把值写错了。

    返回 `{"applied": {...}, "skipped": [(键, 原因), …]}`。
    """
    from anima_world.config_store import coerce_to_declared_type

    report: dict[str, Any] = {"applied": {}, "skipped": []}
    if not world_seed:
        return report
    raw = world_seed.get("config")
    if raw is None:
        return report
    if not isinstance(raw, dict):
        report["skipped"].append(("config", f"必须是一个对象,收到 {type(raw).__name__}"))
        return report

    for key, value in raw.items():
        key = str(key)
        if not config_store.has(key):
            report["skipped"].append((key, "这个引擎版本没有这个配置键"))
            continue
        meta = config_store.meta(key)
        if meta.get("is_secret"):
            report["skipped"].append((key, "密文键不许写在种子里 —— 种子是要分发的"))
            continue
        try:
            coerced = coerce_to_declared_type(value, meta.get("value_type", "str"))
        except (TypeError, ValueError) as exc:
            report["skipped"].append((key, f"值转不成 {meta.get('value_type')}:{exc}"))
            continue
        config_store.set(key, coerced)
        report["applied"][key] = coerced
    return report
