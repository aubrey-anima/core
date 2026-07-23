"""world.db format stamping + boot-time compatibility check.

The live database carries an explicit format version (db_meta table). Opening
a db from a NEWER engine refuses cleanly instead of silently writing into a
format this engine does not understand — the number-one data-corruption door
in a multi-version world fleet.
"""
from __future__ import annotations

import sqlite3

import pytest

from anima_world.db import (
    DB_FORMAT_VERSION,
    DBFormatError,
    open_db,
    read_db_format,
)


def test_fresh_db_is_stamped_with_current_format(tmp_path):
    conn = open_db(tmp_path / "w.db")
    try:
        assert read_db_format(conn) == DB_FORMAT_VERSION
    finally:
        conn.close()


def test_reopen_keeps_stamp(tmp_path):
    path = tmp_path / "w.db"
    open_db(path).close()
    conn = open_db(path)
    try:
        assert read_db_format(conn) == DB_FORMAT_VERSION
    finally:
        conn.close()


def test_legacy_db_without_stamp_is_migrated_and_stamped(tmp_path):
    # A pre-stamp db: schema tables exist but no db_meta.
    path = tmp_path / "w.db"
    conn = open_db(path)
    conn.execute("DROP TABLE db_meta")
    conn.commit()
    conn.close()
    conn = open_db(path)
    try:
        assert read_db_format(conn) == DB_FORMAT_VERSION
    finally:
        conn.close()


def test_future_format_refuses_to_open_and_leaves_db_untouched(tmp_path):
    path = tmp_path / "future.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE db_meta (key TEXT PRIMARY KEY, value TEXT)")
    raw.execute(
        "INSERT INTO db_meta(key, value) VALUES('format_version', ?)",
        (str(DB_FORMAT_VERSION + 1),),
    )
    raw.commit()
    raw.close()

    with pytest.raises(DBFormatError) as exc:
        open_db(path)
    message = str(exc.value)
    assert str(DB_FORMAT_VERSION + 1) in message
    assert str(DB_FORMAT_VERSION) in message

    # Refusal must happen BEFORE any schema write: no engine tables created.
    raw = sqlite3.connect(path)
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    raw.close()
    assert "events" not in tables


def _db_stamped(path, fmt: int):
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE db_meta (key TEXT PRIMARY KEY, value TEXT)")
    raw.execute("INSERT INTO db_meta(key, value) VALUES('format_version', ?)", (str(fmt),))
    raw.commit()
    raw.close()
    return path


def test_refusal_names_the_engine_that_can_open_it(tmp_path):
    """回归:拒绝时要说清该装哪个引擎,而不是只报支持区间。

    主版本号就是 db 格式号,所以库上的戳本身已经指明了该装什么 —— 让读者
    自己从版本策略里推导出这一步,是不该让人走的。
    """
    future = DB_FORMAT_VERSION + 1
    with pytest.raises(DBFormatError) as exc:
        open_db(_db_stamped(tmp_path / "future.db", future))
    assert f"{future}.x" in str(exc.value), str(exc.value)


def test_cli_reports_a_format_mismatch_as_one_line_not_a_traceback(tmp_path, capsys):
    """回归:DBFormatError 是版本模型的正常产物,不该长得像崩溃。

    BeatScriptError 和 WorldSeedError 早就在 CLI 边界被接住并打成一行,
    DBFormatError 是同一类用户可见的前置失败,却是三个里唯一漏掉的。
    """
    from anima_world.__main__ import main

    db = _db_stamped(tmp_path / "future.db", DB_FORMAT_VERSION + 1)
    assert main(["simulate", "--db-path", str(db), "--ticks", "1", "--llm", "mock"]) == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err + captured.out
    assert f"{DB_FORMAT_VERSION + 1}.x" in captured.err


def test_cli_reports_its_own_version_and_formats(capsys):
    """回归:`anima-world --version` 必须存在。

    引擎的头号契约就是"版本即兼容性承诺",而外部工具(anima-studio 这类
    同时持有多个引擎版本的宿主)问的第一个问题就是"你是哪个版本"。
    """
    from anima_world.__main__ import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "anima-world" in out and str(DB_FORMAT_VERSION) in out
