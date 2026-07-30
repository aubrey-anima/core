"""`anima-world contract --json`:引擎自报它的线格式。

这个仓库是跨语言契约的**权威**,别人持有镜像(运维台的 `lib/worldPackage.js` /
`lib/worldSeed.js`)。今天镜像端要知道"我对齐的是哪一版"只有一个办法:读 Python 源码。
于是镜像会悄悄落后 —— 而落后的镜像不会报错,它只是对新格式给出旧答案。

这条命令是那份数据的机器可读出口:不用装世界、不用有 db,`--json` 一把梭。
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

import anima_world
from anima_world.beats import OP_REQUIRED_FIELDS, VALID_OPS
from anima_world.db import DB_FORMAT_VERSION, MIN_SUPPORTED_DB_FORMAT, SCHEMA_REVISION
from anima_world.tools import tools_for
from anima_world.sim_report import REPORT_FORMAT_VERSION
from anima_world.world_package import PACKAGE_FORMAT_VERSION
from anima_world.world_seed import WORLD_SEED_AGENT_KEYS, WORLD_SEED_LOCATION_KEYS


def _contract(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "anima_world", "contract", *args],
        capture_output=True, text=True,
    )


def test_contract_needs_no_world_and_no_database():
    """跑不了世界也要能回答 —— 与 `world inspect` 同一类只读命令。"""
    result = _contract("--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["engine_version"] == anima_world.__version__


def test_contract_reports_every_wire_format_version():
    payload = json.loads(_contract("--json").stdout)
    assert payload["db"] == {
        "format_version": DB_FORMAT_VERSION,
        "min_supported": MIN_SUPPORTED_DB_FORMAT,
        # 加法修订也要报:一个 1.3 的世界在 1.2 引擎上"照跑"而 stance/静音整套
        # 缺席,镜像端只有读到这个数才看得出来(1.3.0)。
        "schema_revision": SCHEMA_REVISION,
    }
    assert payload["package"]["format_version"] == PACKAGE_FORMAT_VERSION
    assert payload["report"]["format_version"] == REPORT_FORMAT_VERSION


def test_contract_reports_the_chat_tools_a_host_will_see_events_from():
    """她在聊天里能调的能力。宿主要显示"她走开了",得先知道哪些能力会产生它。"""
    payload = json.loads(_contract("--json").stdout)
    reported = {entry["id"] for entry in payload["chat_tools"]}
    assert reported == {spec.id for spec in tools_for("*")}
    assert {"mute", "walk_away", "wait_for_user"} <= reported


def test_contract_reports_the_seed_and_beat_shapes_a_mirror_must_match():
    """种子 schema 与节拍脚本没有版本号 —— 那就报形状,让镜像能 diff。"""
    payload = json.loads(_contract("--json").stdout)

    assert payload["seed"]["agent_keys"] == sorted(WORLD_SEED_AGENT_KEYS)
    assert payload["seed"]["location_keys"] == sorted(WORLD_SEED_LOCATION_KEYS)
    assert payload["beats"]["ops"] == sorted(VALID_OPS)
    assert payload["beats"]["op_required_fields"] == {
        op: sorted(fields) for op, fields in sorted(OP_REQUIRED_FIELDS.items())
    }


def test_the_op_table_and_its_required_fields_cannot_drift_apart():
    """VALID_OPS 里有、_OP_REQUIRED_FIELDS 里没有的 op,校验会放行任何字段。"""
    undocumented = sorted(set(VALID_OPS) - set(OP_REQUIRED_FIELDS))
    assert not undocumented, (
        f"这些 op 没有必填字段表,写错字段名不会被拒:{undocumented}"
    )


def test_contract_is_readable_by_a_human_too():
    result = _contract()
    assert result.returncode == 0, result.stderr
    assert anima_world.__version__ in result.stdout
    assert "db" in result.stdout.lower()
