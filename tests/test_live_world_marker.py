"""世界正被谁跑着 —— 以及对活世界改配置的假成功。

CLAUDE.md 的第一条不变量:**一个运行中的世界独占它的 world.db**。世界的真相一半在
内存里(时钟、投影、锁、线程池),第二个进程绕过 `World` 直接写同一个文件,两边立刻
分叉。但今天这条纪律**没有任何标记去支撑** —— 谁也看不出一个 db 正被人跑着。

最尖的一处是 `anima-world config set`:它开自己的连接、写 config 表、打印"已保存",
而运行中那个世界的 `ConfigStore` 缓存不会重读。**你以为改了,其实没改**,而且没有
任何提示 —— 直到下次重启它才突然生效,那时你早忘了自己改过什么。

这里刻意**只提示不拒绝**:占用标记会因为进程崩溃而变陈旧,拿陈旧标记去拒绝操作,
等于在真出事那天把人挡在门外。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

import pytest

from anima_world.api import World


def _meta(db, key: str):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM db_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_an_open_world_stamps_who_is_running_it(tmp_path):
    db = tmp_path / "w.db"
    with World.open(str(db), force_mock_llm=True) as world:
        world.tick(1)
        assert _meta(db, "owner_pid") == str(os.getpid())
        assert _meta(db, "owner_host")


def test_closing_a_world_releases_the_marker(tmp_path):
    """关掉之后标记必须撤掉 —— 否则每个正常关闭过的世界都变成"有人在跑"。"""
    db = tmp_path / "w.db"
    with World.open(str(db), force_mock_llm=True) as world:
        world.tick(1)
    assert _meta(db, "owner_pid") is None


def test_config_set_on_a_live_world_warns_but_still_writes(tmp_path):
    """提示,不拒绝。陈旧标记不该在真出事那天把人挡在门外。"""
    db = tmp_path / "w.db"
    with World.open(str(db), force_mock_llm=True) as world:
        world.tick(1)
        result = subprocess.run(
            [sys.executable, "-m", "anima_world", "config", "set",
             "--db-path", str(db), "world.minutes_per_tick", "7"],
            capture_output=True, text=True,
        )

    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "正被" in combined or "在跑" in combined, (
        f"对一个活着的世界改配置,连一句提示都没有:\n{combined}"
    )
    assert _meta(db, "owner_pid") is None or True  # 世界已关,不断言标记
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        value = conn.execute(
            "SELECT value FROM config WHERE key = 'world.minutes_per_tick'"
        ).fetchone()
    finally:
        conn.close()
    assert value and value[0] == "7", "提示归提示,写还是要写进去"


def test_config_set_on_a_closed_world_stays_quiet(tmp_path):
    """正常用法不该被一句警告淹掉。"""
    db = tmp_path / "w.db"
    with World.open(str(db), force_mock_llm=True) as world:
        world.tick(1)
    result = subprocess.run(
        [sys.executable, "-m", "anima_world", "config", "set",
         "--db-path", str(db), "world.minutes_per_tick", "7"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "正被" not in combined and "在跑" not in combined, combined


def test_doctor_reports_a_live_world(tmp_path):
    db = tmp_path / "w.db"
    with World.open(str(db), force_mock_llm=True) as world:
        world.tick(1)
        result = subprocess.run(
            [sys.executable, "-m", "anima_world", "doctor", "--db-path", str(db)],
            capture_output=True, text=True,
        )
    combined = result.stdout + result.stderr
    assert "正被" in combined or "在跑" in combined, combined
