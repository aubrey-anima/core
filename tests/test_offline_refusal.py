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


def test_the_file_becomes_an_honest_shell(world_on_redis):
    """**搬家一直是复制,不是移动。**

    不清的话那个 `.db` 既不是完整的世界,也不是干净的空壳,而是**一份过时的副本** ——
    而我们刚在它上面盖了"这里没数据"的戳。那个组合最危险:戳没撒谎(数据确实以
    Redis 为准),但文件里躺着一份看起来很像真世界的旧数据,谁手滑读一下都会读出
    一个几小时前的世界。
    """
    import sqlite3

    conn = sqlite3.connect(world_on_redis)
    try:
        names = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        with_rows = {
            name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in names
        }
        leftover = {n: c for n, c in with_rows.items() if c}
    finally:
        conn.close()

    # `db_meta` 留着(戳就在那儿);`config` 有意不搬(按 DB-SPLIT.md 它该搬**出**世界)
    assert set(leftover) <= {"db_meta", "config"}, (
        f"这些表还留着过时的副本:{sorted(set(leftover) - {'db_meta', 'config'})}"
    )
    assert with_rows.get("events") == 0, "事件的旧副本还在 —— 手滑读一下就是另一个世界"
    assert with_rows.get("memories") == 0
    assert "db_meta" in leftover, "戳被一起清掉了"


def test_chatting_does_not_leak_back_into_sqlite(tmp_path, redis):
    """**换 store 时要连拿走了引用的人一起换。**

    `ChatService` 在构造时就拿走了 `chat_state` 的引用,所以只换 `world` 和
    `scheduler` 上的属性它看不见 —— 于是 stance / 静音 / 拒谈会继续写进 SQLite,
    而这个世界的别的东西全在 Redis。**一半在这儿一半在那儿,而且不报错。**
    """
    import sqlite3

    db = str(tmp_path / "chat.db")
    w = World.open(db, force_mock_llm=True, redis=redis, world_id="chat")
    try:
        agent = sorted(w.scheduler.agents)[0]
        # **走聊天服务手里那个引用**,不是 `w.chat_state` —— 后者早就换过了,
        # 用它验等于绕开了要验的东西(我第一版就是这么写的,变异注入后照绿)。
        service_state = w.chat_service.state
        service_state.set_stance(agent, "p1", "test", tick=5)
        service_state.refuse_topic(agent, "彩票")
        service_state.set_quiet(agent, "p1", minutes=5)
    finally:
        w.close()

    conn = sqlite3.connect(db)
    try:
        for table in ("agent_stance", "agent_refused_topics", "agent_mutes"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, (
                f"{table} 还在往 SQLite 写 —— 世界一半在这儿一半在 Redis"
            )
    finally:
        conn.close()
    assert redis.hlen("anima:chat:stance") == 1
