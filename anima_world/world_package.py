"""Portable, data-only Cyberworld bundle export and import."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any

from anima_world.world_seed import is_valid_world_seed

PACKAGE_FORMAT_VERSION = 1
DEFAULT_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_FILES = 128
DEFAULT_MAX_COMPRESSION_RATIO = 200

_WORLD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
_FIXED_MEMBERS = {"manifest.json", "checksums.json", "world_seed.json", "world.db", "beats.json"}
_ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".json", ".txt"}


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
        manifest.validate()
        return manifest

    def validate(self) -> None:
        if self.package_format_version != PACKAGE_FORMAT_VERSION:
            raise PackageValidationError(
                f"unsupported package format version: {self.package_format_version}"
            )
        if not _WORLD_ID_RE.fullmatch(self.world_id):
            raise PackageValidationError("world_id must be a safe lowercase identifier")
        if self.export_mode not in {"snapshot", "template"}:
            raise PackageValidationError("export_mode must be snapshot or template")
        if not self.name.strip() or not self.revision_id.strip() or not self.created_at.strip():
            raise PackageValidationError("manifest identity fields cannot be empty")
        _validate_engine_range(self.engine_min, self.engine_max_exclusive)
        expected = {"seed": "world_seed.json"}
        if self.export_mode == "snapshot":
            expected["database"] = "world.db"
        for role, path in expected.items():
            if self.files.get(role) != path:
                raise PackageValidationError(f"manifest files.{role} must be {path}")
        allowed_roles = {"seed", "database", "beats"}
        if set(self.files) - allowed_roles:
            raise PackageValidationError("manifest contains an unknown file role")
        if self.export_mode == "template" and "database" in self.files:
            raise PackageValidationError("template packages cannot declare a database")
        if "beats" in self.files and self.files["beats"] != "beats.json":
            raise PackageValidationError("manifest files.beats must be beats.json")

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
    world_id: str
    instance_id: str
    path: Path
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


def _validate_engine_range(minimum: str, maximum_exclusive: str) -> None:
    current = _version_tuple(_engine_version())
    lower = _version_tuple(minimum)
    upper = _version_tuple(maximum_exclusive)
    if lower >= upper or not lower <= current < upper:
        raise PackageValidationError(
            f"package requires engine >= {minimum}, < {maximum_exclusive}; current is {_engine_version()}"
        )


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


def _verify_checksums(
    archive: zipfile.ZipFile, infos: dict[str, zipfile.ZipInfo]
) -> None:
    if "checksums.json" not in infos:
        raise PackageValidationError("checksums.json is missing")
    checksums = _read_json_bytes(archive.read("checksums.json"), "checksums.json")
    entries = checksums.get("files") if isinstance(checksums, dict) else None
    if not isinstance(entries, dict) or checksums.get("algorithm") != "sha256":
        raise PackageValidationError("checksum manifest is invalid")
    expected_names = set(infos) - {"checksums.json"}
    if set(entries) != expected_names:
        raise PackageValidationError("checksum file list does not match archive")
    for name in sorted(expected_names):
        entry = entries[name]
        if not isinstance(entry, dict) or entry.get("size") != infos[name].file_size:
            raise PackageValidationError(f"checksum size mismatch for {name}")
        with archive.open(infos[name]) as stream:
            actual = _sha256_stream(stream)
        if entry.get("sha256") != actual:
            raise PackageValidationError(f"checksum mismatch for {name}")


def _check_sqlite_file(path: Path) -> None:
    try:
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
    except sqlite3.Error as exc:
        raise PackageValidationError("world.db is not a valid SQLite database") from exc
    if result != ("ok",):
        raise PackageValidationError("world.db failed SQLite integrity_check")


def inspect_world_package(
    package_path: str | Path,
    *,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
    max_compression_ratio: int = DEFAULT_MAX_COMPRESSION_RATIO,
) -> WorldPackageManifest:
    """Validate a package without installing it and return its manifest."""
    package_path = Path(package_path)
    try:
        size = package_path.stat().st_size
    except OSError as exc:
        raise PackageValidationError("package cannot be read") from exc
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
            if not required.issubset(infos):
                raise PackageValidationError("archive is missing required files")
            _verify_checksums(archive, infos)
            manifest = WorldPackageManifest.from_dict(
                _read_json_bytes(archive.read("manifest.json"), "manifest.json")
            )
            seed = _read_json_bytes(archive.read("world_seed.json"), "world_seed.json")
            if not is_valid_world_seed(seed):
                raise PackageValidationError("world_seed.json failed schema validation")
            declared = set(manifest.files.values())
            if not declared.issubset(infos):
                raise PackageValidationError("manifest references a missing file")
            if manifest.export_mode == "snapshot" and "world.db" not in infos:
                raise PackageValidationError("snapshot package is missing world.db")
            if manifest.export_mode == "template" and "world.db" in infos:
                raise PackageValidationError("template package contains world.db")
            if "beats.json" in infos:
                _read_json_bytes(archive.read("beats.json"), "beats.json")
            if "world.db" in infos:
                with tempfile.TemporaryDirectory(prefix="anima_world-inspect-") as temp_dir:
                    db_path = Path(temp_dir) / "world.db"
                    with archive.open(infos["world.db"]) as source, db_path.open("wb") as target:
                        shutil.copyfileobj(source, target)
                    _check_sqlite_file(db_path)
            return manifest
    except (OSError, zipfile.BadZipFile) as exc:
        raise PackageValidationError("package is not a readable ZIP archive") from exc


def _snapshot_database(source_path: Path, target_path: Path) -> None:
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    source = sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True, timeout=30)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
        has_config = target.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='config'"
        ).fetchone()
        if has_config:
            target.execute("DELETE FROM config WHERE is_secret=1")
        target.commit()
        if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise PackageValidationError("snapshot failed SQLite integrity_check")
    finally:
        target.close()
        source.close()


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
    seed_path: str | Path,
    output_path: str | Path,
    world_id: str,
    name: str,
    mode: str = "snapshot",
    db_path: str | Path | None = None,
    beats_path: str | Path | None = None,
    summary: str = "",
    genre: str = "",
    setting: str = "",
    theme: str = "default",
) -> WorldPackageManifest:
    """Export authored content and optionally a consistent live DB snapshot."""
    output_path = Path(output_path)
    if output_path.exists():
        raise FileExistsError(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        seed = json.loads(Path(seed_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageValidationError("seed file is not valid JSON") from exc
    if not is_valid_world_seed(seed):
        raise PackageValidationError("seed file failed schema validation")
    if mode not in {"snapshot", "template"}:
        raise PackageValidationError("mode must be snapshot or template")
    if mode == "snapshot" and db_path is None:
        raise PackageValidationError("snapshot export requires db_path")
    if not _WORLD_ID_RE.fullmatch(world_id):
        raise PackageValidationError("world_id must be a safe lowercase identifier")

    engine_version = _engine_version()
    engine_major = _version_tuple(engine_version)[0]
    files = {"seed": "world_seed.json"}
    if mode == "snapshot":
        files["database"] = "world.db"
    if beats_path is not None:
        files["beats"] = "beats.json"
    manifest = WorldPackageManifest(
        package_format_version=PACKAGE_FORMAT_VERSION,
        engine_min=engine_version,
        engine_max_exclusive=f"{engine_major + 1}.0.0",
        source_engine_version=engine_version,
        world_id=world_id,
        revision_id=str(uuid.uuid4()),
        export_mode=mode,
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
        }
        if mode == "snapshot":
            snapshot = staging / "world.db"
            _snapshot_database(Path(db_path), snapshot)
            members["world.db"] = snapshot
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


def import_world_package(
    package_path: str | Path,
    instances_root: str | Path,
    **validation_limits: int,
) -> ImportedWorld:
    """Validate and install a package into a new backend-managed instance."""
    package_path = Path(package_path)
    max_archive_bytes = validation_limits.get("max_archive_bytes", DEFAULT_MAX_ARCHIVE_BYTES)
    try:
        if package_path.stat().st_size > max_archive_bytes:
            raise PackageValidationError("archive compressed size exceeds limit")
    except OSError as exc:
        raise PackageValidationError("package cannot be read") from exc
    root = Path(instances_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    instance_id = str(uuid.uuid4())
    staging = root / f".import-{instance_id}"
    content = staging / "content"
    staging.mkdir(mode=0o700)
    content.mkdir(mode=0o700)
    staged_archive = staging / "source.cyberworld"
    target: Path | None = None
    try:
        shutil.copyfile(package_path, staged_archive)
        manifest = inspect_world_package(staged_archive, **validation_limits)
        target = root / f"{manifest.world_id}-{instance_id}"
        if target.exists():
            raise PackageValidationError("generated instance path already exists")
        with zipfile.ZipFile(staged_archive) as archive:
            infos = _validate_zip_members(
                archive,
                max_uncompressed_bytes=validation_limits.get(
                    "max_uncompressed_bytes", DEFAULT_MAX_UNCOMPRESSED_BYTES
                ),
                max_files=validation_limits.get("max_files", DEFAULT_MAX_FILES),
                max_compression_ratio=validation_limits.get(
                    "max_compression_ratio", DEFAULT_MAX_COMPRESSION_RATIO
                ),
            )
            for name in sorted(infos):
                if name == "checksums.json":
                    continue
                destination = content.joinpath(*PurePosixPath(name).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(infos[name]) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)
        instance_record = {
            "instance_id": instance_id,
            "world_id": manifest.world_id,
            "revision_id": manifest.revision_id,
            "imported_at": datetime.now(timezone.utc).isoformat(),
        }
        (content / "instance.json").write_bytes(_json_bytes(instance_record))
        os.replace(content, target)
        return ImportedWorld(
            world_id=manifest.world_id,
            instance_id=instance_id,
            path=target,
            manifest=manifest,
        )
    except (OSError, zipfile.BadZipFile) as exc:
        raise PackageValidationError("package import failed") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
