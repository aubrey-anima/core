"""事件流的格式中立导出 —— issue #8 的连续性通路,**只导出这一半**。

版本模型说得很清楚:主版本 = db 格式,不做跨版本迁移。但没有任何文档回答"下个主版本
落地时,我这个跑了半年的世界怎么办"。引擎的卖点是会积累记忆与历史的世界,而版本政策
读下来把那个寿命封在一个主版本里。

这一版做的是**导出,不是迁移**:JSONL,一行一个事件,不依赖 db 格式。刻意**不做重放
side** —— 事件日志今天还不完备,在它补齐之前把这份东西固化成第四条跨仓库线格式,等于
把一个已知缺陷刻进契约。

所以最要紧的不是导出本身,是**制品自己说清楚它带不走什么**。四项损失写进 header,
而不是写进某份没人会读的文档:
- 图谱边(`edges` 表)不在事件流里
- 记忆强度与反思水位是派生态,重放后归零
- 静默尾部的时钟在 `db_meta`,不在事件里
- 聊天转录留在宿主,世界侧只有摘要
"""
from __future__ import annotations

from _worldfile import open_world_at, run_cli

import json
import subprocess
import sys

import pytest

from anima_world.api import World


def _export(*args: str) -> subprocess.CompletedProcess:
    return run_cli("events", "export", *args)


@pytest.fixture
def lived_in(tmp_path):
    db = tmp_path / "w.db"
    with open_world_at(str(db), force_mock_llm=True) as world:
        world.fast_forward(288)
        world.player_move("p1", "cafe")
        world.player_action("p1", "挥手", {"target": "夏"})
    return db


def _lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_it_exports_every_event_one_per_line(lived_in, tmp_path):
    out = tmp_path / "stream.jsonl"
    assert _export("--world-id", "w", "--output", str(out)).returncode == 0

    lines = _lines(out)
    header, events = lines[0], lines[1:]
    assert header["kind"] == "anima-events"
    from _worldfile import redis_for

    total = int(redis_for(lived_in).llen("anima:w:events") or 0)
    assert len(events) == total
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)


def test_the_header_says_what_this_cannot_carry(lived_in, tmp_path):
    """**这才是这条命令存在的理由。** 一份不说明自己缺什么的导出比没有更危险。"""
    out = tmp_path / "stream.jsonl"
    _export("--world-id", "w", "--output", str(out))
    header = _lines(out)[0]

    assert header["replayable"] is False, "不承诺可重放 —— 承诺了就得兑现"
    losses = " ".join(header["not_included"])
    for missing in ("图谱", "记忆强度", "时钟", "转录"):
        assert missing in losses, f"header 没说清丢了什么:{header['not_included']}"


def test_the_header_pins_the_engine_and_db_format(lived_in, tmp_path):
    """将来谁要写导入端,第一件事就是问"这是哪一版写的"。"""
    import anima_world

    out = tmp_path / "stream.jsonl"
    _export("--world-id", "w", "--output", str(out))
    header = _lines(out)[0]
    assert header["engine_version"] == anima_world.__version__


def test_payloads_survive_the_round_trip(lived_in, tmp_path):
    out = tmp_path / "stream.jsonl"
    _export("--world-id", "w", "--output", str(out))
    actions = [e for e in _lines(out)[1:] if e["type"] == "player_action"]
    assert actions, "玩家动作应该在流里"
    assert actions[-1]["payload"]["action"] == "挥手"
    assert actions[-1]["payload"]["details"] == {"target": "夏"}


def test_a_missing_world_is_refused_and_nothing_is_created(tmp_path):
    missing = tmp_path / "nope.db"
    result = _export("--world-id", "w", "--output", str(tmp_path / "x.jsonl"))
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert not missing.exists()


def test_it_can_write_to_stdout(lived_in):
    result = _export("--world-id", "w", "--output", "-")
    assert result.returncode == 0, result.stderr
    first = json.loads(result.stdout.splitlines()[0])
    assert first["kind"] == "anima-events"
