"""`anima-world report` —— 对着一个 world.db 直接出摘要,不跑世界。

`simulate --report` 只在**你自己跑这一趟**时给得出摘要。一个已经存在的 `world.db`
(玩家跑出来的、别人给你的、从 `.cyberworld` 导进来的)想问"这个世界里发生过什么",
今天只能自己写 Python。而事件日志是唯一真相、`sim_report` 是纯函数 —— 这本来就该是
一条只读命令。

两个坑必须避开:
- **绝不能用 `open_db`**:路径打错会当场**建一个空 world.db**,然后喜气洋洋地报告
  "0 事件、世界健康"。要 `mode=ro`,文件不存在就退出码 2。
- **绝不能碰 `load_or_create_key`**:它会顺手在旁边生成一把 `.key`。
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from anima_world.api import World
from anima_world.sim_report import REPORT_FORMAT_VERSION


def _report(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "anima_world", "report", *args],
        capture_output=True, text=True,
    )


@pytest.fixture
def lived_in(tmp_path):
    db = tmp_path / "w.db"
    with World.open(str(db), force_mock_llm=True) as world:
        world.fast_forward(288 * 2)
    return db


def test_it_reads_a_world_that_is_not_running(lived_in):
    result = _report("--db-path", str(lived_in), "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["report_format_version"] == REPORT_FORMAT_VERSION
    assert payload["events"]["total"] > 0
    assert len(payload["world"]["agents"]) == 3


def test_a_missing_file_is_refused_and_nothing_is_created(tmp_path):
    """路径打错时最坏的结局不是报错,是"建一个空世界然后说它很健康"。"""
    missing = tmp_path / "nope.db"
    result = _report("--db-path", str(missing), "--json")

    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert not missing.exists(), "读一个不存在的世界不该把它创建出来"
    assert not (tmp_path / "nope.db.key").exists(), "更不该顺手生成一把密钥"


def test_it_does_not_write_to_the_world_it_reads(lived_in):
    before = lived_in.stat().st_mtime_ns
    assert _report("--db-path", str(lived_in), "--json").returncode == 0
    assert lived_in.stat().st_mtime_ns == before, "只读命令不该动那个文件"


def test_the_human_output_says_the_things_a_three_day_trial_asks(lived_in):
    result = _report("--db-path", str(lived_in))
    assert result.returncode == 0, result.stderr
    assert "事件" in result.stdout
    assert "世界日" in result.stdout


def test_it_uses_the_worlds_own_minutes_per_tick(tmp_path):
    """口径要跟着世界走,不是跟着默认值走 —— 不然"第几天"整个错位。"""
    db = tmp_path / "w.db"
    with World.open(str(db), force_mock_llm=True) as world:
        world.config_set("world.minutes_per_tick", 10)
        world.fast_forward(288)

    payload = json.loads(_report("--db-path", str(db), "--json").stdout)
    assert payload["world"]["minutes_per_tick"] == 10
    assert payload["world"]["ticks_per_day"] == 144
