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
from importlib import metadata
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


@dataclass(frozen=True)
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


@dataclass(frozen=True)
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
    try:
        return metadata.version("anima-world")
    except metadata.PackageNotFoundError:
        from anima_world import __version__

        return __version__


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.match(value)
    if match is None:
        raise PackageValidationError(f"invalid engine version: {value}")
    return tuple(int(part) for part in match.groups())


def _engine_min_for(engine_version: str) -> str:
    """The lower bound a package gets stamped with: current engine 截到次版本。

    v2 只有 snapshot。状态是 JSON 快照,它的形状随**次版本**演进(键是引擎写进
    Redis 的键),补丁版本按定义不改形状 —— 所以下限是 `major.minor.0`,上限照旧
    是下一个主版本。
    """
    major, minor, _patch = _version_tuple(engine_version)
    return f"{major}.{minor}.0"


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
    - `meta` 里的 `owner_pid` / `owner_host` **剥掉**:装进新世界等于让一个还没人跑过
      的世界自称"有人在跑"。
    - `config` 里 `is_secret` 的行 **剥掉**:包是**分发物**,带着作者的钥匙发出去
      是不可挽回的。
    """
    prefix = _world_prefix(world_id)
    growing: dict[str, dict[str, Any]] = {}
    for key in sorted(_scan_world_keys(redis, world_id)):
        short = key[len(prefix):]
        if short == "lock":
            continue
        ktype = _text(redis.type(key))
        value: Any
        if ktype == "hash":
            value = {_text(f): _text(v) for f, v in (redis.hgetall(key) or {}).items()}
            if short == "config":
                value = _strip_secret_config_rows(value)
            elif short == "meta":
                for transient in ("owner_pid", "owner_host"):
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
            #    转义 JSON,`grep '"type":"entity_spawn"'` 找不到(它在字符串里是
            #    `\"type\"`),`diff` 也变成整块变。而"能 grep、能 diff、能流式"
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
) -> Any:
    """把一个世界文件装进一个**空的** `world_id`。返回它的 manifest。

    **导入不许覆盖。** 目标前缀下有键就当场拒绝 —— 半个旧世界叠上半个新世界,
    跑起来两边都对不上,而且没有任何地方会报错。

    这和 `World.open(world_file=)` 是同一条装载路径,区别只在这里多一道
    "目标必须空"的闸:`World.open` 允许往一个已有的世界补作者层(那是编辑),
    而 `import` 说的是"把这个世界搬过来"。
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
    install_world_records(records, redis=redis, world_id=world_id, mysql=mysql)
    return manifest


def inspect_world_file(path: str | Path) -> dict[str, Any]:
    """不装载,只读封皮:这个世界要哪个引擎、叫什么、多大。

    **答案,不是拒绝。** 管着多个引擎版本的启动器正是那个还跑不了它的调用方 ——
    在这里因为"当前引擎跑不了"而抛错,就违背了这个格式存在的意义。
    `runnable` 是一个字段,不是一个异常。

    只读第一行,所以一个 5 GB 的世界也是一次 open + 一次 readline。
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
        "format_version": manifest.version,
        "engine_min": manifest.engine_min,
        "source_engine_version": manifest.source_engine_version,
        "created_at": manifest.created_at,
        "current_engine_version": current,
        "reader_format_version": WORLD_FILE_VERSION,
        "runnable": runnable,
        "size_bytes": Path(path).stat().st_size,
    }
