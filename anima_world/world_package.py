"""Portable, data-only Cyberworld bundle export and import.

v2(去 SQLite):世界状态是一份 JSON 快照(`world_state.json`),不再有 `world.db`。
一个包 = 信封(manifest + checksums)+ 种子(创作契约)+ 状态(Redis 全量 dump,
可选 MySQL 四表)+ 可选节拍脚本。只有 snapshot 一种模式 —— template 随 v1 退役
(分发全走 snapshot 环;真要复活它属于线格式变更,升主版本再谈)。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any

from anima_world.world_seed import world_seed_errors

logger = logging.getLogger(__name__)

PACKAGE_FORMAT_VERSION = 2
STATE_FORMAT_VERSION = 2
DEFAULT_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_FILES = 128
DEFAULT_MAX_COMPRESSION_RATIO = 200

_WORLD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
_FIXED_MEMBERS = {
    "manifest.json", "checksums.json", "world_seed.json", "world_state.json", "beats.json",
}
_ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".json", ".txt"}

# 随时间无限增长的四样(判据是"她带不带得进上下文",见 CLAUDE.md)。它们在包里
# 可能出现在 redis 段(没给 mysql= 的世界)或 mysql 段 —— 导入时按目标后端改道,
# 这张清单是两边共同的名字。
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


def _seed_schema_error(seed: Any) -> str | None:
    """The seed's schema problems as one line, or None when it is valid.

    `world_seed_errors` already names the offending entry and key — the
    package layer used to call the boolean `is_valid_world_seed` and throw
    that away, leaving an author who cannot open the archive with nothing to
    act on (#10).
    """
    errors = world_seed_errors(seed)
    if not errors:
        return None
    shown = "; ".join(errors[:3])
    if len(errors) > 3:
        shown += f" (+{len(errors) - 3} more)"
    return f"world_seed.json failed schema validation: {shown}"


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _read_json_bytes(raw: bytes, label: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageValidationError(f"{label} is not valid UTF-8 JSON") from exc


def _safe_member_name(name: str) -> str:
    if not name or "\\" in name or "\x00" in name:
        raise PackageValidationError("archive contains an unsafe path")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or str(path) != name or name.endswith("/"):
        raise PackageValidationError("archive contains an unsafe path")
    if name not in _FIXED_MEMBERS:
        if not name.startswith("assets/") or path.suffix.lower() not in _ASSET_EXTENSIONS:
            raise PackageValidationError(f"archive member is not allowed: {name}")
    return name


def _validate_zip_members(
    archive: zipfile.ZipFile,
    *,
    max_uncompressed_bytes: int,
    max_files: int,
    max_compression_ratio: int,
) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > max_files:
        raise PackageValidationError("archive has too many files")
    result: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        name = _safe_member_name(info.filename)
        if name in result:
            raise PackageValidationError("archive contains duplicate filenames")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise PackageValidationError("archive contains a symbolic link")
        if info.flag_bits & 0x1:
            raise PackageValidationError("encrypted archive members are not supported")
        total += info.file_size
        if total > max_uncompressed_bytes:
            raise PackageValidationError("archive uncompressed size exceeds limit")
        if info.file_size and (
            info.compress_size == 0 or info.file_size / info.compress_size > max_compression_ratio
        ):
            raise PackageValidationError("archive compression ratio exceeds limit")
        result[name] = info
    return result


def _sha256_stream(stream: Any) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _verify_checksum_index(
    archive: zipfile.ZipFile,
    infos: dict[str, zipfile.ZipInfo],
    *,
    only: set[str] | None = None,
) -> None:
    """Check `checksums.json` describes exactly this archive, then hash members.

    The index check (algorithm, one entry per member, no extras) is always
    full — it is cheap and it is what makes a partial digest pass meaningful.
    `only` narrows which members get hashed; `None` means all of them.
    """
    if "checksums.json" not in infos:
        raise PackageValidationError("checksums.json is missing")
    checksums = _read_json_bytes(archive.read("checksums.json"), "checksums.json")
    entries = checksums.get("files") if isinstance(checksums, dict) else None
    if not isinstance(entries, dict):
        raise PackageValidationError("checksum manifest is invalid: 'files' must be an object")
    if checksums.get("algorithm") != "sha256":
        raise PackageValidationError(
            f"checksum manifest is invalid: algorithm must be sha256, "
            f"got {checksums.get('algorithm')!r}"
        )
    expected_names = set(infos) - {"checksums.json"}
    if set(entries) != expected_names:
        detail = []
        unlisted = sorted(expected_names - set(entries))
        phantom = sorted(set(entries) - expected_names)
        if unlisted:
            detail.append(f"in archive but unlisted: {', '.join(unlisted)}")
        if phantom:
            detail.append(f"listed but not in archive: {', '.join(phantom)}")
        raise PackageValidationError(
            f"checksum file list does not match archive ({'; '.join(detail)})"
        )
    for name in sorted(expected_names if only is None else expected_names & only):
        entry = entries[name]
        if not isinstance(entry, dict) or entry.get("size") != infos[name].file_size:
            raise PackageValidationError(f"checksum size mismatch for {name}")
        with archive.open(infos[name]) as stream:
            actual = _sha256_stream(stream)
        if entry.get("sha256") != actual:
            raise PackageValidationError(f"checksum mismatch for {name}")


def read_package_manifest(
    package_path: str | Path,
    *,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
    max_compression_ratio: int = DEFAULT_MAX_COMPRESSION_RATIO,
) -> WorldPackageManifest:
    """Read the outer envelope: what does this package say it is and needs? (#3)

    Deliberately weaker than `inspect_world_package`: it checks the archive is
    safe to open, that `checksums.json` describes exactly this archive, and
    that `manifest.json` is the one that was signed — then parses the manifest
    structurally. It does NOT check the engine range, the seed schema, or the
    world state, because a launcher managing several engine versions has to be
    able to ask "which engine does this need?" *before* it has that engine.

    Member digests other than `manifest.json` are left to
    `inspect_world_package`, so answering this question never costs a hash of
    a multi-hundred-megabyte snapshot.
    """
    package_path = Path(package_path)
    try:
        size = package_path.stat().st_size
    except OSError as exc:
        raise PackageValidationError(f"package cannot be read: {exc}") from exc
    if size > max_archive_bytes:
        raise PackageValidationError("archive compressed size exceeds limit")
    try:
        with zipfile.ZipFile(package_path) as archive:
            infos = _validate_zip_members(
                archive,
                max_uncompressed_bytes=max_uncompressed_bytes,
                max_files=max_files,
                max_compression_ratio=max_compression_ratio,
            )
            required = {"manifest.json", "checksums.json", "world_seed.json"}
            missing = sorted(required - set(infos))
            if missing:
                raise PackageValidationError(
                    f"archive is missing required files: {', '.join(missing)}"
                )
            _verify_checksum_index(archive, infos, only={"manifest.json"})
            return WorldPackageManifest.from_dict(
                _read_json_bytes(archive.read("manifest.json"), "manifest.json")
            )
    except (OSError, zipfile.BadZipFile) as exc:
        raise PackageValidationError(f"package is not a readable ZIP archive: {exc}") from exc


# ── world_state.json 的形状 ──────────────────────────────────────────────────

_STATE_REDIS_VALUE_TYPES = {"hash": dict, "list": list, "string": str, "set": list}


def _validate_world_state(state: Any) -> None:
    """world_state.json 的结构校验 —— 装进 Redis/MySQL 之前把坏形状挡在门口。"""
    if not isinstance(state, dict):
        raise PackageValidationError("world_state.json must be an object")
    version = state.get("state_format_version")
    if version != STATE_FORMAT_VERSION:
        raise PackageValidationError(
            f"unsupported world state format version: {version!r}"
            f" (this engine reads {STATE_FORMAT_VERSION})"
        )
    if not isinstance(state.get("world_id"), str) or not state["world_id"].strip():
        raise PackageValidationError("world_state.json world_id must be a non-empty string")
    redis_section = state.get("redis")
    if not isinstance(redis_section, dict) or not redis_section:
        raise PackageValidationError("world_state.json redis section must be a non-empty object")
    for short, entry in redis_section.items():
        if not isinstance(short, str) or not short:
            raise PackageValidationError("world_state.json redis keys must be non-empty strings")
        if not isinstance(entry, dict) or "type" not in entry or "value" not in entry:
            raise PackageValidationError(
                f"world_state.json redis entry {short!r} must be {{type, value}}"
            )
        expected = _STATE_REDIS_VALUE_TYPES.get(entry["type"])
        if expected is None:
            raise PackageValidationError(
                f"world_state.json redis entry {short!r} has unknown type {entry['type']!r}"
            )
        if not isinstance(entry["value"], expected):
            raise PackageValidationError(
                f"world_state.json redis entry {short!r} value does not match its type"
            )
    mysql_section = state.get("mysql")
    if mysql_section is None:
        return
    if not isinstance(mysql_section, dict):
        raise PackageValidationError("world_state.json mysql section must be an object")
    if set(mysql_section) - set(_GROWING_TABLES):
        raise PackageValidationError(
            "world_state.json mysql section contains an unknown table "
            f"(allowed: {', '.join(_GROWING_TABLES)})"
        )
    for table, rows in mysql_section.items():
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise PackageValidationError(
                f"world_state.json mysql.{table} must be a list of row objects"
            )
    for event in mysql_section.get("events") or []:
        if not {"seq", "ts", "type"} <= set(event):
            raise PackageValidationError(
                "world_state.json mysql.events rows must carry seq/ts/type"
            )


def inspect_world_package(
    package_path: str | Path,
    *,
    check_engine_range: bool = True,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
    max_compression_ratio: int = DEFAULT_MAX_COMPRESSION_RATIO,
) -> WorldPackageManifest:
    """Fully validate a package without installing it and return its manifest.

    `check_engine_range=False` skips only the "can THIS engine run it" gate —
    every integrity and schema check still runs. Import keeps the gate on;
    `world inspect` turns it off so it can report the answer instead of
    refusing to speak (#3).
    """
    package_path = Path(package_path)
    try:
        size = package_path.stat().st_size
    except OSError as exc:
        raise PackageValidationError(f"package cannot be read: {exc}") from exc
    if size > max_archive_bytes:
        raise PackageValidationError("archive compressed size exceeds limit")
    try:
        with zipfile.ZipFile(package_path) as archive:
            infos = _validate_zip_members(
                archive,
                max_uncompressed_bytes=max_uncompressed_bytes,
                max_files=max_files,
                max_compression_ratio=max_compression_ratio,
            )
            required = {"manifest.json", "checksums.json", "world_seed.json", "world_state.json"}
            missing = sorted(required - set(infos))
            if missing:
                raise PackageValidationError(
                    f"archive is missing required files: {', '.join(missing)}"
                )
            _verify_checksum_index(archive, infos)
            manifest = WorldPackageManifest.from_dict(
                _read_json_bytes(archive.read("manifest.json"), "manifest.json")
            )
            if check_engine_range:
                manifest.validate_engine_range()
            seed = _read_json_bytes(archive.read("world_seed.json"), "world_seed.json")
            seed_error = _seed_schema_error(seed)
            if seed_error is not None:
                raise PackageValidationError(seed_error)
            undeclared = sorted(set(manifest.files.values()) - set(infos))
            if undeclared:
                raise PackageValidationError(
                    f"manifest references a missing file: {', '.join(undeclared)}"
                )
            _validate_world_state(
                _read_json_bytes(archive.read("world_state.json"), "world_state.json")
            )
            if "beats.json" in infos:
                _read_json_bytes(archive.read("beats.json"), "beats.json")
            return manifest
    except (OSError, zipfile.BadZipFile) as exc:
        raise PackageValidationError(f"package is not a readable ZIP archive: {exc}") from exc


# ── 导出:dump ───────────────────────────────────────────────────────────────


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


def _dump_redis_section(redis: Any, world_id: str) -> dict[str, dict[str, Any]]:
    """SCAN `anima:{world_id}:*` 全部键,按类型 dump。

    通用 dump 天然完整:计数器(`:id` 键)、黑板、全部 store 一个不漏。键名存
    **去前缀后的部分** —— 导入方装到自己的目标前缀下,世界因此可以换名字落地。

    有两样**故意不带走**,它们是进程协调状态,不是世界状态:

    - `lock`:跨进程互斥的临时钥匙(带 TTL)。JSON 快照存不了 TTL,原样装回去
      就是一把没有过期时间的死锁,新世界第一次 `act()` 就撞上它。
    - `meta` 里的 `owner_pid` / `owner_host`:导出那一刻源世界的占用标记。装进
      新世界等于让一个还没人跑过的世界自称"有人在跑"。
    """
    prefix = _world_prefix(world_id)
    out: dict[str, dict[str, Any]] = {}
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
                f"打不成包:键 {key} 的类型 {ktype!r} 不在打包格式里(hash/list/string/set)"
            )
        out[short] = {"type": ktype, "value": value}
    return out


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


def _dump_mysql_section(mysql: Any, world_id: str) -> dict[str, list[dict[str, Any]]]:
    from anima_world.mysql_state import MySQLEventLog, as_connection, ensure_schema

    conn = as_connection(mysql)
    prefix = f"{world_id}_"
    ensure_schema(conn, prefix)  # 幂等;空世界(还没写过一行)也能导出
    section: dict[str, list[dict[str, Any]]] = {
        "events": [
            {"seq": int(e.seq), "ts": int(e.ts), "type": e.type,
             "who": e.who, "loc": e.loc, "payload": e.payload}
            for e in MySQLEventLog(conn, prefix).replay()
        ]
    }
    for table in ("memories", "conversations", "messages"):
        section[table] = _select_all_rows(conn, f"{prefix}{table}")
    return section


def dump_world_state(*, redis: Any, world_id: str, mysql: Any = None) -> dict[str, Any]:
    """一份完整的世界状态快照(`world_state.json` 的内容),纯读。

    独立成函数是给活体导出用的:`World.export_snapshot` 在世界锁内只做这一步,
    打包(压缩、校验、落盘)在锁外进行 —— 锁只挡 dump 那一瞬。
    """
    redis_section = _dump_redis_section(redis, world_id)
    if not redis_section:
        # 打包一个空壳比打包失败坏得多:抄错 world_id 时 SCAN 安静地返回零个键,
        # 产出的包能装能开,但里面什么都没有 —— 而它会被发给别人。当场拒。
        raise PackageValidationError(
            f"打不成包:Redis 上没有 anima:{world_id}:* 的任何键 —— world_id 抄错了?"
        )
    state: dict[str, Any] = {
        "state_format_version": STATE_FORMAT_VERSION,
        "world_id": world_id,
        "redis": redis_section,
    }
    if mysql is not None:
        state["mysql"] = _dump_mysql_section(mysql, world_id)
    return state


def _resolve_seed(redis: Any, world_id: str, seed_path: str | Path | None) -> Any:
    """种子解析:显式 seed_path → 世界 `:meta` 的创世出生证明 → 内置种子(记警告)。"""
    if seed_path is not None:
        try:
            return json.loads(Path(seed_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PackageValidationError(f"seed file is not valid JSON: {exc}") from exc
    from anima_world.redis_state import meta_rows

    genesis = meta_rows(redis, world_id).get("world_seed")
    if genesis is not None:
        if isinstance(genesis, str):
            try:
                genesis = json.loads(genesis)
            except json.JSONDecodeError as exc:
                raise PackageValidationError(
                    "the world's genesis seed (meta.world_seed) is not valid JSON"
                ) from exc
        return genesis
    import anima_world

    logger.warning(
        "这个世界没有创世种子出生证明(meta.world_seed);包里带的是**内置种子** —— "
        "传 seed_path 可覆盖"
    )
    bundled = Path(anima_world.__file__).parent / "world_seed.json"
    return json.loads(bundled.read_text(encoding="utf-8"))


def _write_deterministic_zip(output: Path, files: dict[str, Path | bytes]) -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(files):
            value = files[name]
            data = value.read_bytes() if isinstance(value, Path) else value
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def export_world_package(
    *,
    redis: Any,
    world_id: str,
    seed_path: str | Path | None = None,
    beats_path: str | Path | None = None,
    output_path: str | Path,
    package_world_id: str,
    name: str,
    summary: str = "",
    genre: str = "",
    setting: str = "",
    theme: str = "default",
    mysql: Any = None,
    state: dict[str, Any] | None = None,
) -> WorldPackageManifest:
    """Export a world living on Redis (and optionally MySQL) as a v2 package.

    `world_id` 是**源世界在 Redis 上的名字**(键前缀里那个);`package_world_id`
    是**包的世系 id**(写进 manifest、跟着包走的那个)—— 两个身份故意分开:同一个
    运行中的世界可以打成不同世系的包,反之亦然。

    `state` 给了就直接用(活体导出在世界锁内先 `dump_world_state`,把锁的持有
    时间压到 dump 那一瞬);没给就在这里 dump。
    """
    output_path = Path(output_path)
    if output_path.exists():
        raise FileExistsError(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not _WORLD_ID_RE.fullmatch(package_world_id):
        raise PackageValidationError("package_world_id must be a safe lowercase identifier")

    seed = _resolve_seed(redis, world_id, seed_path)
    seed_error = _seed_schema_error(seed)
    if seed_error is not None:
        if seed_path is not None:
            seed_error = seed_error.replace("world_seed.json", str(seed_path), 1)
        raise PackageValidationError(seed_error)

    if state is None:
        state = dump_world_state(redis=redis, world_id=world_id, mysql=mysql)
    _validate_world_state(state)

    engine_version = _engine_version()
    engine_major = _version_tuple(engine_version)[0]
    files = {"seed": "world_seed.json", "state": "world_state.json"}
    if beats_path is not None:
        files["beats"] = "beats.json"
    manifest = WorldPackageManifest(
        package_format_version=PACKAGE_FORMAT_VERSION,
        engine_min=_engine_min_for(engine_version),
        engine_max_exclusive=f"{engine_major + 1}.0.0",
        source_engine_version=engine_version,
        world_id=package_world_id,
        revision_id=str(uuid.uuid4()),
        export_mode="snapshot",
        name=name,
        summary=summary,
        genre=genre,
        setting=setting,
        theme=theme,
        created_at=datetime.now(timezone.utc).isoformat(),
        files=files,
    )
    manifest.validate()

    with tempfile.TemporaryDirectory(prefix="anima_world-export-", dir=output_path.parent) as temp_dir:
        staging = Path(temp_dir)
        members: dict[str, Path | bytes] = {
            "manifest.json": _json_bytes(manifest.as_dict()),
            "world_seed.json": _json_bytes(seed),
            "world_state.json": _json_bytes(state),
        }
        if beats_path is not None:
            try:
                beats = json.loads(Path(beats_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PackageValidationError("beats file is not valid JSON") from exc
            members["beats.json"] = _json_bytes(beats)
        checksum_entries: dict[str, dict[str, Any]] = {}
        for member_name, value in members.items():
            raw = value.read_bytes() if isinstance(value, Path) else value
            checksum_entries[member_name] = {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }
        members["checksums.json"] = _json_bytes(
            {"algorithm": "sha256", "files": checksum_entries}
        )
        staged_archive = staging / "package.cyberworld"
        _write_deterministic_zip(staged_archive, members)
        inspect_world_package(staged_archive)
        os.replace(staged_archive, output_path)
    return manifest


# ── 导入:restore ────────────────────────────────────────────────────────────


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


def import_world_package(
    package_path: str | Path,
    *,
    redis: Any,
    world_id: str,
    mysql: Any = None,
    **validation_limits: int,
) -> ImportedWorld:
    """Validate a v2 package and install it into an EMPTY world on Redis.

    - 目标 `world_id` 在 Redis 上必须是空的(前缀下无键)—— 导入不许覆盖。
    - 四样无限增长的数据按**目标**后端落位,而不是按包里的段落位:给了 `mysql=`
      就进 MySQL(不管包里它们躺在哪个段),没给就全进 Redis。events 逐条 append
      保 seq 连续,对不上当场抛 —— 见 `_append_events`。
    """
    if not isinstance(world_id, str) or not world_id.strip():
        raise PackageValidationError("world_id 不能为空")
    manifest = inspect_world_package(package_path, **validation_limits)
    try:
        with zipfile.ZipFile(Path(package_path)) as archive:
            state = _read_json_bytes(archive.read("world_state.json"), "world_state.json")
    except (OSError, zipfile.BadZipFile) as exc:
        raise PackageValidationError(f"package is not a readable ZIP archive: {exc}") from exc
    _validate_world_state(state)

    if _scan_world_keys(redis, world_id):
        raise PackageValidationError(
            f"世界 {world_id!r} 已存在(Redis 上有它的键),导入不许覆盖 —— 换一个 --world-id"
        )

    redis_section: dict[str, dict[str, Any]] = state["redis"]
    mysql_section = state.get("mysql")
    bounded = {k: v for k, v in redis_section.items() if not _is_growing_key(k)}
    growing = {k: v for k, v in redis_section.items() if _is_growing_key(k)}

    _restore_redis_entries(redis, world_id, bounded)

    if mysql is not None:
        from anima_world.mysql_state import MySQLEventLog, as_connection, ensure_schema

        conn = as_connection(mysql)
        prefix = f"{world_id}_"
        ensure_schema(conn, prefix)
        growth = (
            mysql_section if mysql_section is not None
            else _growth_rows_from_redis_section(growing)
        )
        _append_events(MySQLEventLog(conn, prefix), growth.get("events") or [])
        for table in ("memories", "conversations", "messages"):
            _insert_rows(conn, f"{prefix}{table}", _MYSQL_COLUMNS[table], growth.get(table) or [])
    elif mysql_section is not None:
        _restore_growth_into_redis(redis, world_id, mysql_section)
    else:
        _restore_redis_entries(redis, world_id, growing)

    return ImportedWorld(
        world_id=world_id,
        instance_id=world_id,
        path=f"redis:{world_id}",
        manifest=manifest,
    )
