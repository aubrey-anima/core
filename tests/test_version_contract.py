"""版本规则的机器强制(world.db 退役后剩下的部分)。

db 格式联锁(主版本 = 可挂载性、SCHEMA_REVISION 加法修订、表集合钉扎)随
SQLite 一起退役:世界不再是一个可挂载的文件,键前缀就是格式。留下的两条:
版本号仍然只有一个来源;包格式版本仍然要和引擎自报的契约一致。
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from anima_world import __version__


def test_version_has_one_source():
    """pyproject 动态读 `anima_world.__version__` —— 两处各写一份迟早分家。"""
    data = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert "version" in data["project"].get("dynamic", []), (
        "pyproject 必须声明 version 为 dynamic(唯一版本源是 anima_world/__init__.py)"
    )


def test_contract_reports_the_package_format():
    from anima_world.__main__ import contract_payload
    from anima_world.world_package import PACKAGE_FORMAT_VERSION

    payload = contract_payload()
    assert payload["package"]["format_version"] == PACKAGE_FORMAT_VERSION
    assert payload["storage"]["backend"] == "redis"
