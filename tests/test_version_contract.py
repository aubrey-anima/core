"""版本规则的机器强制。

**主版本号 = db 格式 = 可挂载性。** 规则(仓库所有者定):让老引擎读不了世界文件的
改动才升第一位。编辑器只看主版本第一位就能判断两个版本的世界文件互不互通。

1.3.0 起多一层:**加法修订**(`SCHEMA_REVISION`)。纯新增的表与可空列不改可挂载性
—— 老引擎照样能开这个文件,只是不读新表,于是新能力缺席。这种改动跟着**次版本号**
走,不再逼一次主版本号跳跃(那会把已有的世界全作废)。破的是"schema 一变就升主版本"
那条,守的是"主版本 = 可挂载性"这条 —— 后者才是那个联锁真正在保护的东西。

于是这里要钉三件事,少一件都会让加法修订变成一句空话:
- 主版本仍然等于 db 格式(旧联锁不许松)
- 硬钉窗口仍然关着(不做跨格式兼容窗口)
- 修订号只增不减,而且高修订的世界在低修订引擎上**能挂**(不然它就不是加法)
"""
from __future__ import annotations

import sqlite3

from anima_world import __version__
from anima_world.db import (
    DB_FORMAT_VERSION,
    MIN_SUPPORTED_DB_FORMAT,
    SCHEMA_REVISION,
    init_schema,
    open_db,
    read_db_format,
    read_schema_revision,
)


def test_major_version_equals_db_format():
    major = int(__version__.split(".")[0])
    assert major == DB_FORMAT_VERSION, (
        f"主版本 {major} 必须等于 DB_FORMAT_VERSION {DB_FORMAT_VERSION} —— "
        "db 格式变才升第一位,第二/三位是程序优化与加法修订"
    )


def test_hard_pin_window_is_closed():
    """硬钉版模型:MIN_SUPPORTED == DB_FORMAT_VERSION,不做跨格式兼容窗口。"""
    assert MIN_SUPPORTED_DB_FORMAT == DB_FORMAT_VERSION


def test_a_fresh_world_is_stamped_with_both_numbers(tmp_path):
    conn = open_db(str(tmp_path / "w.db"))
    try:
        assert read_db_format(conn) == DB_FORMAT_VERSION
        assert read_schema_revision(conn) == SCHEMA_REVISION
    finally:
        conn.close()


def test_a_pre_revision_world_is_read_as_revision_one(tmp_path):
    """1.2.x 及更早的库没有这个戳。读作 1,而不是"读不到就当最新"。"""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE db_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO db_meta VALUES ('format_version', '1')")
    conn.commit()
    assert read_schema_revision(conn) == 1
    conn.close()

    # 而 1.3 引擎打开它就把新表补齐,并把戳升到当前修订 —— 老世界照常开。
    upgraded = open_db(str(path))
    try:
        assert read_schema_revision(upgraded) == SCHEMA_REVISION
        tables = {
            row[0] for row in upgraded.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"agent_stance", "agent_mutes", "persona_overrides"} <= tables
    finally:
        upgraded.close()


def test_a_higher_revision_world_still_mounts(tmp_path, caplog):
    """加法修订的定义就在这条:更高修订的世界**能挂**,只是能力缺席。

    如果它当场拒绝,那这个改动就不是加法,应该去升主版本号 —— 这条测试是那道闸。
    """
    path = tmp_path / "future.db"
    conn = open_db(str(path))
    conn.execute(
        "INSERT INTO db_meta(key, value) VALUES('schema_revision', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_REVISION + 7),),
    )
    conn.commit()
    conn.close()

    with caplog.at_level("WARNING"):
        reopened = open_db(str(path))
    try:
        # 挂上了(没有异常),而且不许无声:降级要在日志里说出来。
        assert read_schema_revision(reopened) == SCHEMA_REVISION + 7, "修订号不许被改小"
        assert any("修订" in record.getMessage() for record in caplog.records), \
            "打开一个更高修订的世界必须留下警告 —— 那些能力这次运行会整个缺席"
    finally:
        reopened.close()


def test_schema_revision_advances_only_forward(tmp_path):
    """低修订的引擎开过高修订的库之后,戳不许被改小(那等于替未来的引擎撒谎)。"""
    path = tmp_path / "w.db"
    conn = open_db(str(path))
    conn.close()
    raw = sqlite3.connect(str(path))
    raw.execute("UPDATE db_meta SET value='99' WHERE key='schema_revision'")
    raw.commit()
    init_schema(raw)  # 相当于本引擎又开了一次
    assert read_schema_revision(raw) == 99
    raw.close()
