"""表重建迁移的原子性回归。

历史 bug:迁移用 executescript() 重建表,而它会先隐式 COMMIT——改名和空新表
提前落盘,中途崩溃后新表已存在,下次启动的检测(grid_x 列 / CHECK 子串)误判
"已迁移"直接跳过,legacy 数据永久变成孤儿。修复后整段迁移在单个事务里:
失败即回滚到 legacy 原样,下次启动重试。
"""
from __future__ import annotations

import sqlite3

import pytest

from anima_world.db import open_db


def _make_legacy_grid_db(path, rows):
    raw = sqlite3.connect(path)
    raw.execute(
        """
        CREATE TABLE locations (
          id TEXT PRIMARY KEY,
          name TEXT,
          description TEXT,
          grid_x INTEGER,
          grid_y INTEGER,
          exits TEXT,
          updated_at TEXT
        )
        """
    )
    raw.executemany(
        "INSERT INTO locations (id, name, description, grid_x, grid_y, exits, updated_at) "
        "VALUES (?, ?, ?, ?, ?, '', '2025-01-01')",
        rows,
    )
    raw.commit()
    raw.close()


def test_legacy_grid_locations_migrate_fully(tmp_path):
    path = tmp_path / "w.db"
    _make_legacy_grid_db(path, [("cafe", "咖啡店", "", 2, 3), ("home", "家", "", 5, 0)])

    conn = open_db(path)
    try:
        rows = {
            r[0]: r
            for r in conn.execute("SELECT id, kind, x, y FROM locations ORDER BY id")
        }
        assert set(rows) == {"cafe", "home"}
        assert rows["cafe"][1] == "point"
        assert rows["cafe"][2] == pytest.approx((2 + 0.5) / 6)
        assert rows["cafe"][3] == pytest.approx((3 + 0.5) / 6)
        # legacy 表不残留
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "locations_legacy" not in tables
    finally:
        conn.close()


def test_failed_location_migration_rolls_back_and_retries(tmp_path):
    path = tmp_path / "w.db"
    # name 为 NULL 的行会撞上新 schema 的 NOT NULL,迁移中途失败
    _make_legacy_grid_db(path, [("cafe", "咖啡店", "", 2, 3), ("broken", None, "", 1, 1)])

    with pytest.raises(sqlite3.IntegrityError):
        open_db(path)

    # 失败后必须回滚到 legacy 原样:grid_x 列还在、两行数据都在、无孤儿表
    raw = sqlite3.connect(path)
    cols = {r[1] for r in raw.execute("PRAGMA table_info(locations)")}
    assert "grid_x" in cols, "迁移失败后 locations 应回滚为 legacy 形态"
    count = raw.execute("SELECT COUNT(*) FROM locations").fetchone()[0]
    assert count == 2
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "locations_legacy" not in tables
    # 修好坏行后,下次启动的迁移必须重试并成功
    raw.execute("UPDATE locations SET name='修好了' WHERE id='broken'")
    raw.commit()
    raw.close()

    conn = open_db(path)
    try:
        rows = dict(conn.execute("SELECT id, name FROM locations"))
        assert rows == {"cafe": "咖啡店", "broken": "修好了"}
    finally:
        conn.close()


def test_bt_nodes_check_rebuild_survives_reopen(tmp_path):
    """老 CHECK 约束的 bt_nodes 表被整表重建,行原样保留。"""
    path = tmp_path / "w.db"
    raw = sqlite3.connect(path)
    raw.execute(
        """
        CREATE TABLE bt_nodes (
          tree TEXT NOT NULL,
          node_id TEXT NOT NULL,
          type TEXT NOT NULL CHECK (type IN ('selector','sequence','condition','action')),
          parent TEXT,
          sort INTEGER NOT NULL DEFAULT 0,
          params TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY (tree, node_id)
        )
        """
    )
    raw.execute(
        "INSERT INTO bt_nodes (tree, node_id, type, parent, sort, params) "
        "VALUES ('default', 'root', 'selector', NULL, 0, '{}')"
    )
    raw.commit()
    raw.close()

    conn = open_db(path)
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='bt_nodes'"
        ).fetchone()[0]
        assert "'time_window'" in sql and "'plan'" in sql
        rows = conn.execute("SELECT tree, node_id, type FROM bt_nodes").fetchall()
        assert rows == [("default", "root", "selector")]
        conn.execute(
            "INSERT INTO bt_nodes (tree, node_id, type, parent, sort) "
            "VALUES ('default', 'tw', 'time_window', 'root', 1)"
        )
    finally:
        conn.close()
