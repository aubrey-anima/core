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
