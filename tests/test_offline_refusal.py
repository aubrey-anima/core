"""世界搬去 Redis 之后,离线命令**不许再撒谎**。

这是"世界不再是一个文件"的真实账单。`doctor` / `events export` / `report` /
`.cyberworld` 打包都是**离线看文件**的:它们直接开一个 `world.db`,不经过 `World`。
世界跑在 Redis 上时,那个文件里只有 schema 没有数据 —— 于是它们会:

- `doctor` 报"0 条事件,0 个角色",一切正常
- `events export` 导出一个**空的** JSONL
- `report` 出一份"这三天什么也没发生"的摘要
- **打包产出一个能装能开、里面什么都没有的 `.cyberworld`,然后它被发给别人**

四条全都不报错。那正是这个仓库最怕的那类坏,而最后一条最严重:一个空壳包比一次失败
的打包坏得多。

所以世界一搬去别的后端,就在 `db_meta` 上盖一个戳(和格式版本、schema 修订一家),
离线路读到它就**当场停下并说清去哪儿看**。
"""
from __future__ import annotations

import subprocess
import sys

import pytest

fakeredis = pytest.importorskip("fakeredis")

from anima_world.api import World  # noqa: E402
from anima_world.db import offline_refusal, open_db, read_storage  # noqa: E402


@pytest.fixture()
def redis():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def _cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "anima_world", *argv], capture_output=True, text=True
    )


@pytest.fixture()
def world_on_redis(tmp_path, redis) -> str:
    db = str(tmp_path / "world.db")
    w = World.open(db, force_mock_llm=True, redis=redis, world_id="gone")
    w.tick(60)
    w.close()
    return db


def test_a_plain_world_is_not_refused(tmp_path):
    """数据就在文件里的世界,一切照旧 —— 这道闸不许误伤。"""
    db = str(tmp_path / "plain.db")
    w = World.open(db, force_mock_llm=True)
    w.tick(20)
    w.close()
    conn = open_db(db)
    try:
        assert read_storage(conn) is None
        assert offline_refusal(conn) is None
    finally:
        conn.close()
    assert _cli("doctor", "--db-path", db, "--skip-probe").returncode in (0, 1)


def test_moving_to_redis_stamps_the_file(world_on_redis):
    """戳和格式版本、schema 修订住在一起 —— 它们回答的是同一类问题:
    这个文件是什么、能不能照字面读。"""
    conn = open_db(world_on_redis)
    try:
        assert read_storage(conn) == ("redis", "gone")
        message = offline_refusal(conn)
        assert message and "redis" in message and "gone" in message
        assert "World.open" in message, "只说不行不够,得说清去哪儿看"
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("argv", "tag"),
    [
        (("doctor", "--skip-probe"), "doctor"),
        (("events", "export"), "events"),
        (("report",), "report"),
    ],
)
def test_the_offline_commands_stop_instead_of_reporting_an_empty_world(
    world_on_redis, tmp_path, argv, tag
):
    extra = ["--output", str(tmp_path / "out.jsonl")] if tag == "events" else []
    done = _cli(argv[0], *argv[1:], "--db-path", world_on_redis, *extra)
    assert done.returncode != 0, f"{tag} 照常返回了 —— 它刚给了一个空世界的答案"
    blob = done.stdout + done.stderr
    assert "redis" in blob and "gone" in blob
    assert "0 条事件" not in blob, "还是把空表当成了真答案"


def test_packaging_refuses_to_ship_an_empty_shell(world_on_redis, tmp_path):
    """**最严重的一条。**

    一个能装能开、里面什么都没有的 `.cyberworld` 会被发给别人,而收到的人要跑起来
    才发现世界是空的。空壳包比一次失败的打包坏得多。
    """
    from importlib import resources

    seed = tmp_path / "seed.json"
    seed.write_text(
        (resources.files("anima_world") / "world_seed.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    out = tmp_path / "shell.cyberworld"
    done = _cli(
        "world", "export", "--db-path", world_on_redis, "--seed", str(seed),
        "--output", str(out), "--world-id", "x", "--name", "x", "--mode", "snapshot",
    )
    assert done.returncode != 0
    assert not out.exists(), "空壳包已经产出来了 —— 它会被发出去"
    assert "redis" in (done.stdout + done.stderr)
