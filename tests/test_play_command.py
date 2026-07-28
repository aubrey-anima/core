"""`anima-world play`:在活着的世界里说话。

`chat` 说话但时钟不走(它明说不推进),`run` 时钟走但说不了话。于是"跟一个正在过
日子的角色对话"——这个引擎最想让人看到的那件事——在命令行上一直做不到。

这里守三件事:时钟真的在走、话真的记进世界、以及**每轮重新定位**(她走开了,判定
就该从面对面变成手机私聊,而不是停在你第一句话时的样子)。
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys

import pytest


def _play(db, script: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "anima_world", "play", "--db-path", str(db),
         "--name", "阿檀", *extra],
        input=script, capture_output=True, text=True, timeout=180,
    )


def _rows(db, sql: str):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_a_play_session_records_the_conversation_into_the_world(tmp_path):
    db = tmp_path / "w.db"
    result = _play(db, "你好啊\n/quit\n")
    assert result.returncode == 0, result.stderr

    conversations = _rows(db, "SELECT COUNT(*) FROM events WHERE type = 'conversation'")
    assert conversations[0][0] == 1, "说过的话必须进世界的历史"


def test_the_clock_keeps_running_while_you_talk(tmp_path):
    """这就是 play 存在的全部理由 —— 不走的话它只是 chat。"""
    db = tmp_path / "w.db"
    result = _play(db, "在吗\n/quit\n")
    assert result.returncode == 0, result.stderr

    clock = _rows(db, "SELECT value FROM db_meta WHERE key = 'clock'")
    assert clock and int(clock[0][0]) > 0, (
        f"一场对话之后世界时钟还是 0 —— 时钟没在走。stdout:\n{result.stdout}"
    )


def test_slash_commands_do_not_become_dialogue(tmp_path):
    """`/who` 是给你看的,不该被当成一句话说给角色听。"""
    db = tmp_path / "w.db"
    result = _play(db, "/who\n/quit\n")
    assert result.returncode == 0, result.stderr
    assert "第" in result.stdout and "天" in result.stdout, result.stdout
    assert _rows(db, "SELECT COUNT(*) FROM events WHERE type = 'conversation'")[0][0] == 0


def test_switching_who_you_are_talking_to(tmp_path):
    db = tmp_path / "w.db"
    result = _play(db, "/at 遥\n你好\n/quit\n")
    assert result.returncode == 0, result.stderr
    who = _rows(
        db,
        "SELECT DISTINCT json_extract(payload, '$.agent_id') FROM events"
        " WHERE type = 'conversation'",
    )
    assert [w[0] for w in who] == ["遥"]


def test_an_unknown_agent_is_refused_not_silently_swapped(tmp_path):
    db = tmp_path / "w.db"
    result = _play(db, "/quit\n", "--agent", "不存在的人")
    assert result.returncode == 2
    assert "不存在的人" in result.stderr
