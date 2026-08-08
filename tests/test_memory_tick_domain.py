"""记忆的 `tick` 只有一种时基:世界时钟。

这是一次真事故的回归闸。2.0 之前 `chat_session.close_conversation` 给
`conversation` 事件盖的是 `int(time.time())` —— 全引擎唯一一处给活事件盖墙钟的
地方(别处都走 `Scheduler._record_event` 的 `setdefault("ts", self.clock)`)。
`TriggerEngine` 把它照抄进记忆的 `tick`,而 `MemoryStore.query` 按 `(tick, id)`
DESC 排序,于是:

    线上一个世界:382 条记忆,`user_conversation` 占 20 条(5%),
    而每个角色召回列表的**前 20 条 100% 是它们**。

planner 的上下文、反思的源、八卦的源、叙事,吃的都是这个列表。没有一处报错。

所以这里钉的不是"某个字段等于某个数",是**量级**:关会话写下的记忆,必须和同期
其他记忆落在同一个数轴上。写成 `== world.tick()` 也能过,但那样只钉住了今天的
实现;`WALL_CLOCK_FLOOR` 那一条钉的才是这个 bug 本身。
"""
from __future__ import annotations

import json

import pytest

from anima_world.world_time import WALL_CLOCK_FLOOR


@pytest.fixture
def world(open_world, bare_seed):
    return open_world(world_file=bare_seed)


def _memories(world, agent_id="夏"):
    return world.memories(agent_id)


def _kind(rows, kind):
    return [r for r in rows if r["kind"] == kind]


def test_conversation_memory_tick_is_world_time_not_wall_clock(world, caplog):
    """关会话写的记忆,tick 与同期其他记忆同量级。"""
    world.tick(40)          # 让世界时钟离开 0,并攒下一批普通记忆
    tick_now = world.scheduler.clock
    assert 0 < tick_now < WALL_CLOCK_FLOOR

    with caplog.at_level("WARNING", logger="anima_world.memory_triggers"):
        world.record_chat_turn("夏", "p1", [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ])

    # `TriggerEngine._tick_of` 兜得住墙钟 ts,但**兜住不等于没事**:活路径上用到
    # 那条兜底,就说明源头又在盖墙钟了。这一句是这个 bug 的真正闸门 —— 少了它,
    # 下游修好之后源头怎么坏都测不出来。
    assert "是墙钟不是世界时钟" not in caplog.text, (
        "活路径不该需要墙钟兜底 —— 事件的 ts 应该由 Scheduler 盖世界时钟"
    )

    rows = _memories(world)
    conversations = _kind(rows, "user_conversation")
    assert conversations, "关会话必须写下一条 user_conversation 记忆"

    for row in conversations:
        assert row["tick"] < WALL_CLOCK_FLOOR, (
            f"tick={row['tick']} 是墙钟,不是世界时钟 —— "
            "它会让这条记忆永远排在召回列表最前面"
        )
        assert row["created_at"] < WALL_CLOCK_FLOOR
        assert row["tick"] == tick_now, "就是关会话那一刻的世界时钟"

    # 同量级:和同期别的记忆比,不该差出一个数量级以上。
    others = [r for r in rows if r["kind"] != "user_conversation"]
    if others:
        span = max(r["tick"] for r in rows) - min(r["tick"] for r in rows)
        assert span <= tick_now, "所有记忆必须落在同一根世界时间轴上"


def test_conversation_event_carries_world_tick(world):
    """事件本身也得是世界时钟 —— 记忆只是它的下游。"""
    world.tick(30)
    tick_now = world.scheduler.clock
    world.record_chat_turn("夏", "p1", [
        {"role": "user", "content": "在吗"},
        {"role": "assistant", "content": "在的"},
    ])
    events = [e for e in world.scheduler.event_log.replay() if e.type == "conversation"]
    assert events, "整场会话关闭时必须发一个 conversation 事件"
    assert events[-1].ts == tick_now
    assert events[-1].ts < WALL_CLOCK_FLOOR

    # 而 payload 里的那两个**照旧是墙钟**:它们是从转录行上抄下来的,转录那一层
    # (`chat.idle_timeout` 是秒)本来就按秒记账。把其中一个换成 tick,才是把两套
    # 时基混进同一个 payload。
    payload = events[-1].payload
    assert payload["closed_at"] >= WALL_CLOCK_FLOOR
    assert payload["started_at"] >= WALL_CLOCK_FLOOR


def test_conversation_memory_does_not_hijack_recall(world):
    """病本身:它不许把召回列表整个占掉。"""
    world.tick(60)
    for i in range(3):
        world.record_chat_turn("夏", "p1", [
            {"role": "user", "content": f"第{i}次搭话"},
            {"role": "assistant", "content": "嗯"},
        ])
        world.tick(20)

    top = world.memories("夏")[:10]
    assert top, "这个世界得先有记忆,不然这条测试什么也没验"
    kinds = {r["kind"] for r in top}
    assert kinds != {"user_conversation"}, (
        "前 10 条不该 100% 是跟玩家的对话 —— 那正是墙钟 tick 造成的样子"
    )


# ── 迁移:已经写下的那批脏行 ────────────────────────────────────────────────


def test_trigger_engine_folds_legacy_wall_clock_events_back(world):
    """老日志重放:墙钟事件按"上一条正常事件的 tick"折回去。

    没有这一条的话,`MemoryStore.rebuild` 会把同一批脏 tick 原样再造一遍 ——
    修完再重放一次就又坏了。
    """
    from anima_world.memory_triggers import TriggerEngine
    from anima_world.types import Projection

    engine = TriggerEngine()
    projection = Projection()

    engine.process({"seq": 1, "ts": 800, "type": "narrative", "payload": {}}, projection)
    descriptor = engine.process({
        "seq": 2, "ts": 1_785_253_112, "type": "conversation",
        "who": "夏", "payload": {"agent_id": "夏", "summary": "一次对话"},
    }, projection)

    assert descriptor is not None
    assert descriptor.tick == 800, "折回上一条正常事件的世界时钟,不是墙钟"


def test_repair_memory_ticks_is_idempotent_and_leaves_the_log_alone(world):
    """迁移:就地换算,只动记忆,不动事件日志;跑两遍结果一样。"""
    world.tick(50)
    world.record_chat_turn("夏", "p1", [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好呀"},
    ])
    store = world.scheduler.memory_store
    row = _kind(world.memories("夏"), "user_conversation")[0]

    # 手工把它做脏,重现 2.0 之前那批行的样子。
    store.retick(int(row["id"]), 1_785_253_112)
    dirty = store.query("夏")
    assert dirty[0]["kind"] == "user_conversation", "脏 tick 就是这么霸占前排的"

    log_before = [(e.seq, e.ts) for e in world.scheduler.event_log.replay()]

    dry = world.repair_memory_ticks(dry_run=True)
    assert dry["scanned"] == 1 and dry["repaired"] == 1
    assert store.query("夏")[0]["kind"] == "user_conversation", "dry-run 不许动库"

    result = world.repair_memory_ticks()
    assert result["repaired"] == 1 and result["unresolved"] == 0
    fixed = [r for r in store.query("夏") if int(r["id"]) == int(row["id"])][0]
    assert fixed["tick"] == row["tick"], "折回它本来那一刻"
    assert fixed["created_at"] == row["tick"]

    again = world.repair_memory_ticks()
    assert again["scanned"] == 0 and again["repaired"] == 0, "幂等"

    assert [(e.seq, e.ts) for e in world.scheduler.event_log.replay()] == log_before, (
        "事件日志一个字都不许动 —— 它记的是发生过什么"
    )


def test_the_migration_has_a_cli_way_out(tmp_path):
    """库里有而 CLI 上没有,对创作台/运维台等于不存在(FOR-STUDIO 的判据)。

    而迁移偏偏是最需要 CLI 的那种东西:出事的世界跑在别人的机器上,修它的人手边
    只有一个终端。
    """
    from _worldfile import open_world_at, run_cli

    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    try:
        world.tick(50)
        world.record_chat_turn("夏", "p1", [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ])
        row = _kind(world.memories("夏"), "user_conversation")[0]
        world.scheduler.memory_store.retick(int(row["id"]), 1_785_253_112)
    finally:
        world.close()

    dry = run_cli("memory", "repair-ticks", "--world-id", "w", "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert "扫到 1 条" in dry.stdout, dry.stdout

    done = run_cli("memory", "repair-ticks", "--world-id", "w", "--json")
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    assert payload["repaired"] == 1 and payload["unresolved"] == 0
    assert payload["rows"][0]["repaired_to"] == row["tick"]

    again = run_cli("memory", "repair-ticks", "--world-id", "w", "--json")
    assert json.loads(again.stdout)["scanned"] == 0, "幂等"


def test_repair_never_invents_a_tick_for_an_unsourced_row(world):
    """查不到出处的行一律不动 —— 编一个出来比留着更坏,因为它从此看不出来了。"""
    store = world.scheduler.memory_store
    store.add("夏", tick=1_785_253_112, kind="user_conversation",
              summary="没有出处的一条", importance=0.8, event_seq=None)

    result = world.repair_memory_ticks()
    assert result["scanned"] == 1
    assert result["repaired"] == 0
    assert result["unresolved"] == 1
    assert store.query("夏")[0]["tick"] == 1_785_253_112, "没动"
