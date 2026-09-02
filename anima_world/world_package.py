"""把一个世界装进 / 取出 `.cyberworld`(v3)。

**线格式在 `world_file.py`,这里只管落库那一层。** 分工是刻意的:格式模块不认识
Redis,落库模块不认识 gzip —— 两边各自能被单独测,而"换一种容器"不必碰存储语义。

v3 与 v2 的差别不只是 zip → gzip JSONL:

- **种子这个概念没有了。** v2 的包里有 `world_seed.json`(人写的世界描述)和
  `world_state.json`(机器的 dump),它们是同一件东西的两种写法。v3 把它们合成
  一个文件、两层记录,于是**创世和还原是同一个动作**。
- **导出与导入都是流式的。** v2 的 `_dump_mysql_section` 是全量 `replay()` 再
  `SELECT *`,没有任何上限 —— 一个跑了两年的世界导一次包要把整段历史塞进内存。
- **没有成员白名单、没有解压炸弹比率。** 一条流,三个上限(见 `world_file`)。

带走什么、不带走什么没变,那几条是安全条款不是格式细节:包里零 secret、
不带 `lock`、不带占用标记。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

logger = logging.getLogger(__name__)

PACKAGE_FORMAT_VERSION = 3   # = world_file.WORLD_FILE_VERSION

_WORLD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

# 随时间无限增长的四样(判据是"她带不带得进上下文",见 CLAUDE.md)。文件里它们
# 可能是 `redis` 记录(没接 MySQL 的世界)或 `event`/`mysql` 记录 —— 装载时
# **按目标后端改道**,这张清单是两边共同的名字。
_GROWING_TABLES = ("events", "memories", "conversations", "messages")

# 三张行表在 MySQL 里的列 —— 与 `mysql_state.SCHEMA` 逐列相同,也与
# RedisMemoryStore / RedisChatStore 的行字段逐字相同(那正是两边能互相转写的前提)。
_MYSQL_COLUMNS = {
    "memories": (
        "id", "agent_id", "tick", "kind", "summary", "importance", "anchor",
        "event_seq", "created_at", "strength", "last_access", "access_count", "source_ids",
    ),
    "conversations": (
        "id", "agent_id", "status", "started_at", "last_activity_at", "closed_at",
        "summary", "message_count", "participants", "location", "player_id",
    ),
    "messages": (
        "id", "conversation_id", "role", "content", "created_at",
        "stance", "intent", "intent_confidence", "tool_calls",
    ),
}
_JSON_COLUMNS = {"source_ids", "participants", "tool_calls"}
_STATE_REDIS_VALUE_TYPES = {"hash": dict, "list": list, "string": str, "set": list}


class PackageValidationError(ValueError):
    """Raised when a world archive fails the portable package contract."""


class WorldPackageManifest:
    package_format_version: int
    engine_min: str
    engine_max_exclusive: str
    source_engine_version: str
    world_id: str
    revision_id: str
    export_mode: str
    name: str
    summary: str
    genre: str
    setting: str
    theme: str
    created_at: str
    files: dict[str, str]

    @classmethod
    def from_dict(cls, value: Any) -> WorldPackageManifest:
        if not isinstance(value, dict):
            raise PackageValidationError("manifest must be an object")
        compat = value.get("engine_compat")
        files = value.get("files")
        if not isinstance(compat, dict) or not isinstance(files, dict):
            raise PackageValidationError("manifest engine_compat and files must be objects")
        try:
            manifest = cls(
                package_format_version=int(value["package_format_version"]),
                engine_min=str(compat["minimum"]),
                engine_max_exclusive=str(compat["maximum_exclusive"]),
                source_engine_version=str(value["source_engine_version"]),
                world_id=str(value["world_id"]),
                revision_id=str(value["revision_id"]),
                export_mode=str(value["export_mode"]),
                name=str(value["name"]),
                summary=str(value.get("summary", "")),
                genre=str(value.get("genre", "")),
                setting=str(value.get("setting", "")),
                theme=str(value.get("theme", "default")),
                created_at=str(value["created_at"]),
                files={str(key): str(path) for key, path in files.items()},
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PackageValidationError(f"manifest field is missing or invalid: {exc}") from exc
        # Envelope-only: parsing a manifest must never depend on the running
        # engine being able to RUN the package (#3). The engine-range check is
        # a separate, explicitly-invoked step — see `validate_engine_range`.
        manifest.validate_structure()
        return manifest

    def validate(self) -> None:
        """Full validation: the manifest is well-formed AND this engine can run it."""
        self.validate_structure()
        self.validate_engine_range()

    def validate_structure(self) -> None:
        """Version-neutral checks: shape, identity, file roles, range sanity.

        Everything here holds for every engine that can read the envelope at
        all, so it is safe to run at parse time. `package_format_version` is
        the one thing allowed to hard-fail parsing — it is the envelope's own
        version, and an envelope we cannot parse is not a package.
        """
        if self.package_format_version != PACKAGE_FORMAT_VERSION:
            raise PackageValidationError(
                f"unsupported package format version: {self.package_format_version}"
                f" (this engine reads {PACKAGE_FORMAT_VERSION})"
            )
        if not _WORLD_ID_RE.fullmatch(self.world_id):
            raise PackageValidationError("world_id must be a safe lowercase identifier")
        if self.export_mode == "template":
            raise PackageValidationError(
                "export_mode 'template' was retired with package format v2: "
                "since v2 the only export mode is 'snapshot'"
            )
        if self.export_mode != "snapshot":
            raise PackageValidationError("export_mode must be snapshot")
        if not self.name.strip() or not self.revision_id.strip() or not self.created_at.strip():
            raise PackageValidationError("manifest identity fields cannot be empty")
        # An empty interval is malformed regardless of who is reading it; which
        # side of the interval the READER falls on is `validate_engine_range`.
        if _version_tuple(self.engine_min) >= _version_tuple(self.engine_max_exclusive):
            raise PackageValidationError(
                f"engine range is empty: [{self.engine_min}, {self.engine_max_exclusive})"
            )
        expected = {"seed": "world_seed.json", "state": "world_state.json"}
        for role, path in expected.items():
            if self.files.get(role) != path:
                raise PackageValidationError(f"manifest files.{role} must be {path}")
        allowed_roles = {"seed", "state", "beats"}
        if set(self.files) - allowed_roles:
            raise PackageValidationError("manifest contains an unknown file role")
        if "beats" in self.files and self.files["beats"] != "beats.json":
            raise PackageValidationError("manifest files.beats must be beats.json")

    def validate_engine_range(self) -> None:
        """Raise unless the RUNNING engine falls inside the package's interval."""
        current = _engine_version()
        if not self.runs_on(current):
            raise PackageValidationError(
                f"package requires engine >= {self.engine_min}, "
                f"< {self.engine_max_exclusive}; current is {current}"
            )

    def runs_on(self, engine_version: str) -> bool:
        """Whether *engine_version* falls inside the interval. Answers, never raises."""
        try:
            lower = _version_tuple(self.engine_min)
            upper = _version_tuple(self.engine_max_exclusive)
            current = _version_tuple(engine_version)
        except PackageValidationError:
            return False
        return lower <= current < upper

    def compatibility(self) -> dict[str, Any]:
        """What this package needs vs what is running — as DATA, not an exception.

        The whole point of the format is travelling between machines whose
        engines do not match yet, so the caller who most needs this answer is
        precisely the one that cannot run the package (#3).
        """
        current = _engine_version()
        return {
            "current_engine_version": current,
            "engine_min": self.engine_min,
            "engine_max_exclusive": self.engine_max_exclusive,
            "runnable": self.runs_on(current),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "package_format_version": self.package_format_version,
            "engine_compat": {
                "minimum": self.engine_min,
                "maximum_exclusive": self.engine_max_exclusive,
            },
            "source_engine_version": self.source_engine_version,
            "world_id": self.world_id,
            "revision_id": self.revision_id,
            "export_mode": self.export_mode,
            "name": self.name,
            "summary": self.summary,
            "genre": self.genre,
            "setting": self.setting,
            "theme": self.theme,
            "created_at": self.created_at,
            "files": dict(self.files),
        }


class ImportedWorld:
    """v2 起没有实例目录了:世界装进 Redis(可选 MySQL)。

    - `world_id`:装进去的目标世界名(Redis 键前缀里那个)。
    - `instance_id`:与 `world_id` 相同 —— 一个 Redis 上一个名字就是一个实例。
    - `path`:描述串 `redis:{world_id}`,不再是文件路径。
    """

    world_id: str
    instance_id: str
    path: str
    manifest: WorldPackageManifest


def _engine_version() -> str:
    """这个引擎是哪一版。**只有一个来源:`anima_world.__version__`。**

    从前这里先问已安装包的元数据、问不到才回落模块。那是**第二个真相**:pyproject
    本来就是动态读 `__version__` 的,所以打成 wheel 时两者恒等 —— 唯一能分叉的场合
    是 editable 安装下改了版本号还没重装,而那时元数据给的是**过期的那个**。
    症状很难看:世界文件的 `engine_min` 盖的是新版本(模块读的),而"我跑不跑得了"
    问的是旧版本(元数据读的),于是引擎判定自己导出的包自己跑不了。
    """
    from anima_world import __version__

    return __version__


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.match(value)
    if match is None:
        raise PackageValidationError(f"invalid engine version: {value}")
    return tuple(int(part) for part in match.groups())


_STATE_REDIS_VALUE_TYPES = {"hash": dict, "list": list, "string": str, "set": list}


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _glob_escape(text: str) -> str:
    """Redis SCAN MATCH 是 glob:world_id 里的 `*?[]` 必须按字面匹配。"""
    return "".join(f"[{ch}]" if ch in "*?[]" else ch for ch in text)


def _world_prefix(world_id: str) -> str:
    from anima_world.redis_state import KEY_PREFIX

    return f"{KEY_PREFIX}:{world_id}:"


def _scan_world_keys(redis: Any, world_id: str) -> list[str]:
    prefix = _world_prefix(world_id)
    pattern = _glob_escape(prefix) + "*"
    return [_text(key) for key in redis.scan_iter(match=pattern, count=500)]


def _strip_secret_config_rows(rows: dict[str, str]) -> dict[str, str]:
    """分发纪律:包里零 secret。

    如今引擎不会把 secret 写进世界(密钥住机器配置),这是对旧数据的保险 ——
    剥除时点名,静默剥除和静默泄漏一样是"照跑但给错东西"。
    """
    kept: dict[str, str] = {}
    for key, raw in rows.items():
        row: Any = None
        try:
            row = json.loads(raw)
        except (TypeError, ValueError):
            pass
        if isinstance(row, dict) and row.get("is_secret"):
            logger.warning(
                "配置键 %s 标着 is_secret,已从包里剥除 —— .cyberworld 是分发物,包里零 secret",
                key,
            )
            continue
        kept[key] = raw
    return kept


def _parse_json_column(value: Any) -> Any:
    if isinstance(value, (bytes, str)):
        try:
            return json.loads(_text(value))
        except ValueError:
            return _text(value)
    return value


def _select_all_rows(conn: Any, table: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM `{table}` ORDER BY id ASC")
        columns = [d[0] for d in cur.description]
        rows = []
        for raw in cur.fetchall():
            row = dict(raw) if isinstance(raw, dict) else dict(zip(columns, raw))
            for col in _JSON_COLUMNS & set(row):
                if row[col] is not None:
                    row[col] = _parse_json_column(row[col])
            rows.append(row)
    return rows


def _is_presence_key(short: str) -> bool:
    """这个去前缀键装的是不是"谁这会儿在场"。**导出与导入都跳过它。**

    理由见 `dump_world_records` 的 docstring:带 TTL 的东西进不了 JSON,而在场
    是会话态、不是世界的内容。导入侧也要拦 —— 老包里没有这些键,但手改过的包有,
    而"装回一份永不过期的在场"这个坏法从哪个入口进来都一样坏。
    """
    return short == "players" or short.startswith("player:")


def _is_volatile_key(short: str) -> bool:
    """这个去前缀键**带 TTL 或纯属进程态**吗 —— 带走它一次都不对。

    这一条和 `contract --json` 的 `storage.volatile_keys` 是同一份清单,
    三类各有各的坏法,但坏的方式都一样安静:

    - `lock` —— 跨进程互斥的钥匙。JSON 存不了 TTL,装回去是一把永不过期的死锁。
    - `players` / `player:*` —— 在场。装回去是一屋子永不散场的幽灵访客,
      而且包是分发物,不该带着别人的玩家此刻站在哪儿。
    - `erasure:*`(3.5.0)—— **一趟没做完的法务抹除的进度,里面装着正要被抹掉的
      那些名字**。把它打进包发出去,比不抹还糟;而装进另一个世界则会让那边的
      下一趟抹除从一个跟它毫无关系的水位接着做(见 `RedisErasureProgress`)。

    - `config_rev`(3.10.0)—— **配置表改过几次**,`ConfigStore` 拿它判断
      "我手里那份还新不新"。它是一个纯粹的协调计数器:装进另一个世界毫无意义,
      而每一份发出去的包都会带着一个没人读得懂的数(总图那张三态表:
      **进程态不进 `.cyberworld`**)。

    **导出与导入两侧都用它。** 老包里没有这些键,但手改过的包有 —— 而"装回一份
    永不过期的假东西"这个坏法从哪个入口进来都一样坏。
    """
    return (short in ("lock", "config_rev")
            or _is_presence_key(short) or short.startswith("erasure:"))


def _is_growing_key(short: str) -> bool:
    """这个去前缀键装的是不是"随时间无限增长的四样"之一(含其计数器/索引)。"""
    return short in {
        "events",
        "memories", "memories:id",
        "conversations", "conversations:id",
        "messages", "messages:id",
    } or short.startswith("conv_msgs:")


def _restore_redis_entries(redis: Any, world_id: str, entries: dict[str, dict[str, Any]]) -> None:
    prefix = _world_prefix(world_id)
    for short in sorted(entries):
        if _is_volatile_key(short):
            continue
        entry = entries[short]
        key = prefix + short
        ktype, value = entry["type"], entry["value"]
        if ktype == "hash":
            if value:
                redis.hset(key, mapping=dict(value))
        elif ktype == "list":
            if value:
                redis.rpush(key, *value)
        elif ktype == "string":
            redis.set(key, value)
        elif ktype == "set":
            if value:
                redis.sadd(key, *value)
        else:  # _validate_world_state 已挡;双保险
            raise PackageValidationError(
                f"world_state.json redis entry {short!r} has unknown type {ktype!r}"
            )


def _row_dumps(row: Any) -> str:
    # 与 redis_state._dumps 同一形状(RedisRows 的行是这么编码的)。
    return json.dumps(row, ensure_ascii=False, default=str)


def _append_events(log: Any, events: list[dict[str, Any]]) -> None:
    """逐条 append 保 seq 连续:目标必须是空日志,原 seq 必须无洞。

    `seq` 是跨仓库看得见的东西(投影、分页游标、`events export` 的每一行),
    错位一格就是整段历史指错人 —— 所以逐条断言,不一致当场抛。
    """
    for event in sorted(events, key=lambda e: int(e["seq"])):
        appended = log.append({
            "ts": int(event["ts"]),
            "type": event["type"],
            "who": event.get("who"),
            "loc": event.get("loc"),
            "payload": event.get("payload") or {},
        })
        if int(appended.seq) != int(event["seq"]):
            raise PackageValidationError(
                f"导入事件时 seq 对不上:包里是 {event['seq']},落库成 {appended.seq} —— "
                "目标事件日志不是空的,或包里的 seq 有洞"
            )


def _insert_rows(conn: Any, table: str, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    sql = (
        f"INSERT INTO `{table}` ({', '.join(f'`{c}`' for c in columns)})"
        f" VALUES ({', '.join(['%s'] * len(columns))})"
    )
    with conn.cursor() as cur:
        for row in sorted(rows, key=lambda r: int(r["id"])):
            values = []
            for column in columns:
                value = row.get(column)
                if column in _JSON_COLUMNS and value is not None and not isinstance(value, str):
                    value = json.dumps(value, ensure_ascii=False, default=str)
                values.append(value)
            cur.execute(sql, tuple(values))
    conn.commit()


def _growth_rows_from_redis_section(
    growing: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """把 redis 段里的四样读成行的形状(给"包只有 redis 段、目标有 MySQL"改道用)。

    `conv_msgs:*` 与 `:id` 计数器在 MySQL 侧不需要:消息顺序由 `conversation_id`
    + 自增 `id` 导出,计数器就是 AUTO_INCREMENT。
    """
    out: dict[str, list[dict[str, Any]]] = {"events": []}
    events_entry = growing.get("events")
    if events_entry is not None:
        for index, raw in enumerate(events_entry["value"]):
            try:
                data = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise PackageValidationError(
                    f"world_state.json redis events[{index}] is not valid JSON"
                ) from exc
            out["events"].append({"seq": index + 1, **data})
    for table in ("memories", "conversations", "messages"):
        rows: list[dict[str, Any]] = []
        entry = growing.get(table)
        if entry is not None:
            for field, raw in entry["value"].items():
                try:
                    row = json.loads(raw) if isinstance(raw, str) else raw
                except ValueError as exc:
                    raise PackageValidationError(
                        f"world_state.json redis {table}[{field}] is not a valid row"
                    ) from exc
                if not isinstance(row, dict) or "id" not in row:
                    raise PackageValidationError(
                        f"world_state.json redis {table}[{field}] is not a row object"
                    )
                rows.append(row)
            rows.sort(key=lambda r: int(r["id"]))
        out[table] = rows
    return out


def _restore_growth_into_mysql(
    mysql: Any, world_id: str, section: dict[str, list[dict[str, Any]]]
) -> None:
    """无限增长的四样落进 MySQL。v2 的 import 与 v3 的装载共用这一条 ——
    两处各写一遍的话,迟早有一条忘了 `_append_events` 那道 seq 连续性断言。"""
    from anima_world.mysql_state import MySQLEventLog, as_connection, ensure_schema

    conn = as_connection(mysql)
    prefix = f"{world_id}_"
    ensure_schema(conn, prefix)
    _append_events(MySQLEventLog(conn, prefix), section.get("events") or [])
    for table in ("memories", "conversations", "messages"):
        _insert_rows(conn, f"{prefix}{table}", _MYSQL_COLUMNS[table], section.get(table) or [])


def _restore_growth_into_redis(
    redis: Any, world_id: str, section: dict[str, list[dict[str, Any]]]
) -> None:
    """把 mysql 段转写成 Redis 的键形状(见 `redis_state.RedisChatStore` 的 docstring)。"""
    from anima_world.redis_state import RedisEventLog, events_key

    prefix = _world_prefix(world_id)
    _append_events(RedisEventLog(redis, events_key(world_id)), section.get("events") or [])

    memories = sorted(section.get("memories") or [], key=lambda r: int(r["id"]))
    if memories:
        redis.hset(
            f"{prefix}memories",
            mapping={str(int(r["id"])): _row_dumps(r) for r in memories},
        )
        redis.set(f"{prefix}memories:id", max(int(r["id"]) for r in memories))

    conversations = sorted(section.get("conversations") or [], key=lambda r: int(r["id"]))
    if conversations:
        redis.hset(
            f"{prefix}conversations",
            mapping={str(int(r["id"])): _row_dumps(r) for r in conversations},
        )
        redis.set(f"{prefix}conversations:id", max(int(r["id"]) for r in conversations))

    messages = sorted(section.get("messages") or [], key=lambda r: int(r["id"]))
    if messages:
        redis.hset(
            f"{prefix}messages",
            mapping={str(int(r["id"])): _row_dumps(r) for r in messages},
        )
        redis.set(f"{prefix}messages:id", max(int(r["id"]) for r in messages))
        for row in messages:  # 已按 id 排序 = 发言顺序,rpush 保序
            redis.rpush(f"{prefix}conv_msgs:{int(row['conversation_id'])}", int(row["id"]))


def dump_world_records(
    *, redis: Any, world_id: str, mysql: Any = None
) -> Iterator[dict[str, Any]]:
    """把一个活世界流式 dump 成记录。**纯读。**

    带走什么、不带走什么,和 v2 逐条相同(那几条是安全纪律,不是格式细节):

    - `lock` **不带走**:跨进程互斥的临时钥匙(带 TTL)。JSON 存不了 TTL,原样装回去
      就是一把没有过期时间的死锁,新世界第一次 `act()` 就撞上它。
    - `players` / `player:*` **不带走**:同一条理由再加一条。在场是带 TTL 的
      (`RedisPlayerPresence`),JSON 存不了 TTL,装回去就是一份**永不过期的假在场** ——
      新世界一开机就有一屋子谁也找不到的幽灵访客。而且包是**分发物**:把一个世界
      发给别人,不该带着别人的玩家此刻站在哪儿。它落 Redis 是为了扛重启,不是为了
      被打包发出去。
    - `erasure:*` **不带走**(3.5.0):一趟没做完的法务抹除的进度,而它记着的正是
      **要被抹掉的那些名字**。把它打进一份分发物里,比不抹还糟。
    - `meta` 里的 `owner_pid` / `owner_host` / `owner_token` **剥掉**:装进新世界等于让一个
      还没人跑过的世界自称"有人在跑"。3.7.0 起 `run_since_seq` 同理(`doctor` 的
      "本次开机以来"水位):它是**进程态**,而进程态不进 `.cyberworld` ——
      带着走的话,一个刚装好、一 tick 没跑过的世界会拿着别人那一趟的水位,
      于是 `doctor` 把上一个世界的一段历史当成"本次开机以来"。
    - `config` 里 `is_secret` 的行 **剥掉**:包是**分发物**,带着作者的钥匙发出去
      是不可挽回的。
    """
    prefix = _world_prefix(world_id)
    growing: dict[str, dict[str, Any]] = {}
    for key in sorted(_scan_world_keys(redis, world_id)):
        short = key[len(prefix):]
        if _is_volatile_key(short):
            continue
        ktype = _text(redis.type(key))
        value: Any
        if ktype == "hash":
            value = {_text(f): _text(v) for f, v in (redis.hgetall(key) or {}).items()}
            if short == "config":
                value = _strip_secret_config_rows(value)
            elif short == "meta":
                for transient in ("owner_pid", "owner_host", "owner_token",
                                  "run_since_seq"):
                    value.pop(transient, None)
        elif ktype == "list":
            value = [_text(v) for v in (redis.lrange(key, 0, -1) or [])]
        elif ktype == "string":
            value = _text(redis.get(key))
        elif ktype == "set":
            value = sorted(_text(v) for v in (redis.smembers(key) or ()))
        else:
            raise PackageValidationError(
                f"打不成包:键 {key} 的类型 {ktype!r} 不在世界文件格式里"
                f"(hash/list/string/set)"
            )
        if _is_growing_key(short):
            # **无限增长的那四样一律按语义记录导出,不看它们此刻住在哪个后端。**
            # 两个理由,都是硬的:
            #
            # ① 一行一条 —— 事件塞进一个 `redis` list 记录里就是一整行几万字节的
            #    转义 JSON,`grep '"type": "entity_spawn"'` 找不到(它在字符串里是
            #    `\"type\"`),`diff` 也变成整块变。⚠️ **那个示范里冒号后的空格是
            #    承重的**:记录用 `json.dumps` 的默认分隔符写出去,所以
            #    `'"type":"entity_spawn"'` 连一条都匹配不到 —— 而 grep 找不到时
            #    退出码 1、屏幕上什么都没有,和"这个世界确实没生过东西"长得一模
            #    一样。这两句丑话说的是两件事:少了空格是**这一行本来就该写对**,
            #    转义是**这一层要防的那种坏法**。而"能 grep、能 diff、能流式"
            #    正是换成文本格式的全部理由 —— 在最常见的那种世界(没接 MySQL)上
            #    不成立的话,那三条就是空话。
            # ② 同一个世界换个后端导出来必须长得一样。按后端分叉的话,一份包能不能
            #    被 grep 取决于导出它的那台机器接没接 MySQL,而那和世界无关。
            growing[short] = {"type": ktype, "value": value}
            continue
        yield {"kind": "redis", "key": short, "type": ktype, "value": value}

    if mysql is None:
        section = _growth_rows_from_redis_section(growing) if growing else {}
        for event in section.get("events") or []:
            yield {"kind": "event", **event}
        for table in ("memories", "conversations", "messages"):
            for row in section.get(table) or []:
                yield {"kind": "mysql", "table": table, "row": row}
        return
    from anima_world.mysql_state import MySQLEventLog, as_connection, ensure_schema

    conn = as_connection(mysql)
    mprefix = f"{world_id}_"
    ensure_schema(conn, mprefix)     # 幂等;空世界(还没写过一行)也能导出
    for event in MySQLEventLog(conn, mprefix).replay():
        yield {
            "kind": "event", "seq": int(event.seq), "ts": int(event.ts),
            "type": event.type, "who": event.who, "loc": event.loc,
            "payload": event.payload,
        }
    for table in ("memories", "conversations", "messages"):
        for row in _select_all_rows(conn, f"{mprefix}{table}"):
            yield {"kind": "mysql", "table": table, "row": row}


def install_world_records(
    records: Iterable[dict[str, Any]], *, redis: Any, world_id: str, mysql: Any = None
) -> dict[str, Any]:
    """把记录流装进一个世界。返回聚合好的**作者层** section 字典。

    两层分开走,这是 v3 的全部结构:

    - `redis` / `event` / `mysql` 记录 —— **直接落**,那是一个跑过的世界的状态;
    - `author` 记录 —— 这里不编译,只聚合成 section 字典交回调用方,
      由既有的那条编译管线去落(`duties` → 行为树、`money` → `payment` 事件……)。
      **编译器还在**,只是它的输入现在和 dump 住在同一个文件里。

    落键在编译之前:装完状态之后世界就不是空的了,于是"这是不是创世"这个判断
    自己会给出正确答案 —— 一份只有状态的文件不会再被当成新世界播一遍种。
    """
    from anima_world.world_file import author_records_to_seed

    entries: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    rows: dict[str, list[dict[str, Any]]] = {t: [] for t in ("memories", "conversations", "messages")}
    authored: list[dict[str, Any]] = []

    for record in records:
        kind = record.get("kind")
        if kind == "author":
            authored.append(record)
        elif kind == "redis":
            short = str(record.get("key") or "")
            expected = _STATE_REDIS_VALUE_TYPES.get(record.get("type"))
            if not short or expected is None:
                raise PackageValidationError(
                    f"redis 记录坏了:key={short!r} type={record.get('type')!r} —— "
                    f"只认 {sorted(_STATE_REDIS_VALUE_TYPES)}"
                )
            if not isinstance(record.get("value"), expected):
                raise PackageValidationError(
                    f"redis 记录 {short!r} 的值和它声明的类型对不上"
                )
            entries[short] = {"type": str(record["type"]), "value": record.get("value")}
        elif kind == "event":
            events.append({
                "seq": record.get("seq"), "ts": record.get("ts"), "type": record.get("type"),
                "who": record.get("who"), "loc": record.get("loc"),
                "payload": record.get("payload") or {},
            })
        elif kind == "mysql":
            table = str(record.get("table") or "")
            if table not in rows:
                raise PackageValidationError(
                    f"mysql 记录指向不认识的表 {table!r} —— 只认 {sorted(rows)}"
                )
            rows[table].append(dict(record.get("row") or {}))

    # 无限增长的那四样**按目标后端改道**,不按它们在文件里躺在哪儿(v2 的
    # `import_world_package` 也是这条):一份没接 MySQL 的世界导出来,装进一个
    # 接了 MySQL 的世界时照样该进 MySQL。文件说的是"有什么",不是"住哪儿"。
    bounded = {k: v for k, v in entries.items() if not _is_growing_key(k)}
    growing = {k: v for k, v in entries.items() if _is_growing_key(k)}
    if bounded:
        _restore_redis_entries(redis, world_id, bounded)

    section = {"events": events, **rows}
    if not any(section.values()) and growing:
        section = _growth_rows_from_redis_section(growing)
        growing = {}
    if mysql is not None:
        if any(section.values()):
            _restore_growth_into_mysql(mysql, world_id, section)
    elif any(section.values()):
        _restore_growth_into_redis(redis, world_id, section)
    elif growing:
        _restore_redis_entries(redis, world_id, growing)

    return author_records_to_seed(authored)


# ── 公开出口:装载与探查 ─────────────────────────────────────────────────────


def import_world_file(
    path: str | Path, *, redis: Any, world_id: str, mysql: Any = None,
    receipt: dict[str, Any] | None = None,
) -> Any:
    """把一个世界文件装进一个**空的** `world_id`。返回它的 manifest。

    **导入不许覆盖。** 目标前缀下有键就当场拒绝 —— 半个旧世界叠上半个新世界,
    跑起来两边都对不上,而且没有任何地方会报错。

    这和 `World.open(world_file=)` 是同一条装载路径,区别只在这里多一道
    "目标必须空"的闸:`World.open` 允许往一个已有的世界补作者层(那是编辑),
    而 `import` 说的是"把这个世界搬过来"。

    🆕 给了 `receipt`(一个 dict)就往里填一格 `authored_sections_skipped`:
    这份包里**有、而这条路不编译**的作者层段名(3.8.0,收件箱 D32)。
    混装两层的包走这条路时会丢掉作者层那一半,今天那句话只说给人听
    (`logger.warning`),而**机器读的是退出码和那份 JSON** —— D30 与 D32 是
    同一个病灶的两格,治法也是同一条:让"我没做那件事"机器读得到。
    """
    from anima_world.world_file import read_world_file

    prefix = _world_prefix(world_id)
    existing = next(iter(redis.scan_iter(match=f"{_glob_escape(prefix)}*", count=1)), None)
    if existing is not None:
        raise PackageValidationError(
            f"world_id {world_id!r} 下已经有键了 —— 导入只进空世界。"
            f"换一个名字,或者先把那个世界清掉"
        )
    manifest, records = read_world_file(path)
    authored = install_world_records(records, redis=redis, world_id=world_id, mysql=mysql)
    # ⚠️ **作者层不在这里编译,而它的返回值此前是被直接丢掉的。**
    #
    # 导入只做"落键":把记录写进这个前缀。而作者层要**编译**(种类要解析、规律要
    # 校验、实体的量要落地),那要一整套 store 和一次开机 —— 只有 `World.open`
    # 那条路走得了。
    #
    # 一个只有作者层的文件走这条路,状态层是空的,所以世界仍然是"空"的,首启时
    # 照常编译 —— 那是常态,没问题。**出问题的是混装两层的文件**:状态记录一落键,
    # 这个前缀就不空了,于是首启不给 `--world-file` 时作者层再也编译不了 ——
    # 而这中间**没有任何一处会说一句**。
    #
    # 这里不替它编译(那要把半个 `build_serve_scheduler` 搬过来),但**必须说**:
    # 少装一半世界而文件看上去完全正常,正是这个格式最怕的那种坏法。
    if authored:
        # ⚠️ **这句话曾经只写对了一半的情形,而写错的那一半后果更重**(2026-08-21 实测)。
        # 它原先一律说"状态记录已经落键,这个世界从此不是空的了" —— 对**混装两层**的
        # 文件是真的,对一份**只有作者层**的文件是**假的**:那种文件一个键都不落
        # (作者记录不是键),世界仍然是空的。而"空"在首启那一刻不是中性的:
        # 不给 `--world-file` 的话,空世界会去装**内置那份橱窗**,于是
        # `world import 我的世界.cyberworld` + 启动容器 = **屏幕上住着夏、遥、柔,
        # 而不是作者写的那些人**,退出码全是 0,日志干净。
        # 一句诊断错了病因的警告,会把人推向错的下一步 —— 而这一格的下一步
        # 恰好是"那就这样跑吧"。
        landed = next(
            iter(redis.scan_iter(match=f"{_glob_escape(prefix)}*", count=1)), None
        )
        if landed is None:
            # 🆕 3.8.0(收件箱 D32):**这里从今天起是拒绝,不是一句日志。**
            #
            # 上面那句警告一个字都没写错,而它写在 `logger.warning` 上 ——
            # 退出码是 0、stdout 照样打那份 `{"operation": "import", …}` 的 JSON。
            # 于是**机器读到的是"成功"**:`world import 我的世界.cyberworld` 退 0,
            # 容器起来,屏幕上住着夏、遥、柔。**丢的不是一段节拍,是整个世界。**
            #
            # 为什么不是"那就让 import 编译作者层":那会造出**第二条创世路径**。
            # 编译要一整套 store、一次预检、一次投影重放 —— 也就是半个
            # `build_serve_scheduler`。而"别另写一份判断"是这个仓库最贵的一条纪律:
            # 两份判断迟早给出不同答案,而那种不一致会表现成"import 装得进,
            # 开机还是失败"。`--world-file` 那条路已经是创世,它不需要一个孪生兄弟。
            #
            # 所以这一格的形状是:**这次操作完全无效,就别报成成功。**
            # ⚠️ 这是一次**行为变更**(rc 0 → rc 2),不是纯增量,契约表与
            # FOR-STUDIO §3.46 都记了。它的下游代价实测为零:运维台 v3 装载走的是
            # `--world-file`(`deploy/world-image/entrypoint.sh`),创作台一次都不调
            # `world import`。会被它拦下的调用方,今天拿到的本来就是一个空世界。
            # ⚠️ 强调用「」不用 `**`:这句话从今天起**印在人的终端上**(CLI 把它
            # 打到 stderr),而屏幕上 `**` 就是两个星号。上一版它只进 `logger`,
            # 所以带着 markdown 也没人看见 —— **把一句日志升成一句人话,它的排版
            # 规矩也跟着换了**,这一步很容易漏。
            raise PackageValidationError(
                f"{path} 「只有作者层」({'、'.join(sorted(authored))}),"
                f"而导入不编译它 —— 这一趟一个键都没落,`{world_id}` 仍然是一个空世界。"
                f"⚠️ 空世界首启时装的是「内置橱窗」,不是你这份 —— 世界会照常跑起来、"
                f"住着橱窗里那几个人,而且一处不报错。所以这里拒绝,而不是报成成功。"
                f"要装这份世界,别用 import,首启直接把这份文件指回来 —— 整条命令是:\n"
                f"  anima-world run --world-id {world_id} --world-file {path}"
            )
        else:
            if receipt is not None:
                receipt["authored_sections_skipped"] = sorted(authored)
            logger.warning(
                "%s 里有作者层(%s),而「导入不编译它」—— 状态记录已经落键,这个世界从此"
                "不是空的了。要让作者层生效,首启时把同一份文件指回来:"
                "`--world-file %s`(那是一次编辑,只填缺不覆盖)。",
                path, "、".join(sorted(authored)), path,
            )
    if receipt is not None:
        receipt.setdefault("authored_sections_skipped", [])
    return manifest


def inspect_world_file(path: str | Path) -> dict[str, Any]:
    """不装载,只读封皮:这个世界要哪个引擎、叫什么、多大。

    **答案,不是拒绝。** 管着多个引擎版本的启动器正是那个还跑不了它的调用方 ——
    在这里因为"当前引擎跑不了"而抛错,就违背了这个格式存在的意义。
    `runnable` 是一个字段,不是一个异常。

    只读第一行,所以一个 5 GB 的世界也是一次 open + 一次 readline。

    **封皮上的字要报全。** 这里报的是 manifest,而 manifest 上的
    `name/summary/genre/setting/theme` 是**作者填的店面栏** —— 它们经运维台的
    注册表直通玩家看到的世界卡片(platform `docs/工作台-运维台契约.md` §4)。
    v3 之前运维台是从解包出来的 `manifest.json` 读它们的;v3 不再解包成目录,
    于是它唯一的来源就是这个函数。少报一栏的下场不是报错,是**世界卡片上那一栏
    空着**,而两边的日志都干净 —— 所以这里的规矩是:manifest 上有的封皮字段,
    一个不漏地报出来。
    """
    from anima_world.world_file import WORLD_FILE_VERSION, read_world_file

    manifest, _ = read_world_file(path)
    current = _engine_version()
    engine_min = manifest.engine_min or "0.0.0"
    runnable = _version_tuple(current) >= _version_tuple(engine_min)
    return {
        "world_id": manifest.world_id,
        "name": manifest.name,
        "summary": manifest.summary,
        "genre": manifest.genre,
        "setting": manifest.setting,
        "theme": manifest.theme,
        "format_version": manifest.version,
        "engine_min": manifest.engine_min,
        "source_engine_version": manifest.source_engine_version,
        "created_at": manifest.created_at,
        "current_engine_version": current,
        "reader_format_version": WORLD_FILE_VERSION,
        "runnable": runnable,
        "size_bytes": Path(path).stat().st_size,
    }


def drop_world(redis: Any, world_id: str, *, confirm: bool = False, mysql: Any = None) -> int:
    """把一个世界从 Redis 上整个抹掉。返回它有多少个键(`confirm=False` 时只数不删)。

    **为什么这是引擎的活。** 键前缀是这个引擎定义的形状(`anima:{world_id}:*`),
    "抹掉一个世界"就是"抹掉那个前缀下的一切" —— 让调用方自己去 `SCAN` + `DEL`,
    等于让每个宿主都持有一份对键形状的猜测,而键形状是跨仓库契约。

    **为什么调用方需要它。** 创作台跑试炼要一个用完即弃的世界(它的纪律是"演化过程
    不落盘":那次运行是预览,不是交付物)。没有这道出口,它要么把垃圾世界永久留在
    Redis 上,要么自己去删键。

    `confirm=False` 是默认:先数给你看。一个打错的 `--world-id` 在这里的代价是
    抹掉另一个世界,而那是不可逆的。
    """
    prefix = _world_prefix(world_id)
    keys = list(redis.scan_iter(match=f"{_glob_escape(prefix)}*", count=500))
    if not confirm:
        return len(keys)
    for chunk in (keys[i:i + 500] for i in range(0, len(keys), 500)):
        if chunk:
            redis.delete(*chunk)
    if mysql is not None:
        # 无限增长的那四样住在 `{world_id}_` 前缀的表里。**drop 掉整张表**,
        # 而不是 DELETE 行:一个世界没了,它的表也就没有主人了。
        from anima_world.mysql_state import as_connection

        conn = as_connection(mysql)
        with conn.cursor() as cur:
            for table in _GROWING_TABLES:
                cur.execute(f"DROP TABLE IF EXISTS `{world_id}_{table}`")
        conn.commit()
    return len(keys)
