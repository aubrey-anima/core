"""记忆 2.0(v2.0 / db 2):三因子检索、加固、遗忘曲线、按强度淘汰、反思。"""
from __future__ import annotations

from _worldfile import open_world_at

import pytest

from anima_world.memory_retrieval import similarity
from anima_world.redis_state import RedisMemoryStore


@pytest.fixture
def store():
    import fakeredis

    return RedisMemoryStore(fakeredis.FakeStrictRedis(decode_responses=True), "t")


def test_bigram_similarity_works_on_chinese():
    assert similarity("咖啡店的事", "今天在咖啡店打碎了杯子") > 0.0
    assert similarity("咖啡店的事", "港口的轮渡晚点了") == 0.0


def test_retrieval_ranks_by_three_factors(store):
    now = 1000
    store.add("夏", tick=990, kind="obs", summary="刚才在咖啡店和遥聊了很久", importance=0.4)
    store.add("夏", tick=10, kind="obs", summary="很久以前的港口散步", importance=0.4)
    store.add("夏", tick=500, kind="obs", summary="一件极重要的大事", importance=1.0)

    top = store.retrieve("夏", now_tick=now, query="咖啡店", k=2)
    summaries = [m["summary"] for m in top]
    assert summaries[0] == "刚才在咖啡店和遥聊了很久", "时近+相关必须赢过久远无关"
    assert "很久以前的港口散步" not in summaries


def test_retrieval_reinforces_strength_and_access(store):
    mid = store.add("夏", tick=0, kind="obs", summary="被想起的事", importance=0.9)
    store.retrieve("夏", now_tick=100, k=1)
    row = store.query("夏")[0]
    assert row["id"] == mid
    assert row["strength"] > 1.0
    assert row["access_count"] == 1
    assert row["last_access"] == 100


def test_decay_weakens_idle_memories_but_not_anchors(store):
    store.add("夏", tick=0, kind="obs", summary="闲置的记忆", importance=0.5)
    store.add("夏", tick=0, kind="seed", summary="锚定的记忆", importance=0.5, anchor=True)
    store.decay_pass("夏", now_tick=288 * 5, ticks_per_day=288)
    rows = {m["summary"]: m for m in store.query("夏")}
    assert rows["闲置的记忆"]["strength"] < 1.0
    assert rows["锚定的记忆"]["strength"] == 1.0


def test_eviction_removes_weakest_not_oldest(tmp_path):
    import fakeredis

    store = RedisMemoryStore(
        fakeredis.FakeStrictRedis(decode_responses=True), "t", capacity=2)
    store.add("夏", tick=0, kind="obs", summary="旧但常被想起", importance=0.5)
    store.retrieve("夏", now_tick=10, k=1)  # 加固最旧那条
    store.add("夏", tick=50, kind="obs", summary="新但没人记得", importance=0.5)
    for mid, row in store._rows.all().items():
        if row["summary"] == "新但没人记得":
            row["strength"] = 0.1
            store._rows.put(mid, row)
    store.add("夏", tick=100, kind="obs", summary="触发淘汰的第三条", importance=0.5)
    summaries = {m["summary"] for m in store.query("夏")}
    assert "旧但常被想起" in summaries, "淘汰按强度,不再按最旧"
    assert "新但没人记得" not in summaries


def test_reflection_emerges_from_accumulated_importance(tmp_path):
    """重要度累计过阈值 → mock 反思器产出洞察 → 以 memory_seed(kind=reflection)
    事件落地 —— LLM 只提案,事件日志记录,重放可重建。"""
    from anima_world.api import World

    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.config_set("memory.reflection_threshold", 2.0)
        for i in range(3):
            world.scheduler._record_event({
                "type": "memory_seed", "who": "夏",
                "payload": {"agent_id": "夏", "kind": "obs",
                            "summary": f"第{i}件大事发生了", "importance": 0.9},
            })
        world.scheduler.stop(wait=True)  # 排干判官池,反思落地
        insights = world.reflections("夏")
        assert insights, "累计重要度过阈值必须产出反思"
        assert "在心里过了一遍" in insights[0]["summary"]
        assert insights[0]["source_ids"], "反思必须带证据链"


def test_reflection_watermark_stays_off_the_tick_hot_path(tmp_path):
    """回归:水位线曾在**每次记忆写入**时做 INSERT + SELECT + COMMIT。

    那是在世界唯一的锁里、tick 帧上的 db 往返,只为维护一个丢了也不要紧的
    计数器(丢了最多让一次反思晚点到)。现在它活在内存里,只在三个时刻碰库:
    每个角色的首次读取、反思触发、日切/关闭的检查点。
    """
    from anima_world.api import World

    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        sched = world.scheduler
        if sched.reflector is None or sched._judge_pool is None:
            pytest.skip("这个世界没接反思器")
        reads: list[str] = []
        real_store = sched.reflection_store

        class CountingStore:
            def get(self, agent_id):
                reads.append(agent_id)
                return real_store.get(agent_id)

            def __getattr__(self, name):
                return getattr(real_store, name)

        sched.reflection_store = CountingStore()
        try:
            with sched._lock:
                for i in range(5):
                    sched._note_memory_written("夏", 0.1, "obs")
        finally:
            sched.reflection_store = real_store

        touched = reads
        assert len(touched) <= 1, (
            f"每条记忆最多允许首次读一次水位,实际 {len(touched)} 次:{touched}"
        )
        assert sched._reflection_watermark["夏"] == pytest.approx(0.5)
        assert "夏" in sched._reflection_dirty


def test_reflection_watermark_survives_reopen(tmp_path):
    """水位线在内存里,但关闭时要落盘 —— 重开必须接着累计,而不是从零开始。"""
    from anima_world.api import World

    db = str(tmp_path / "w.db")
    world = open_world_at(db, force_mock_llm=True)
    if world.scheduler.reflector is None:
        world.close()
        pytest.skip("这个世界没接反思器")
    with world.scheduler._lock:
        world.scheduler._note_memory_written("夏", 1.2, "obs")
    world.close()

    with open_world_at(db, force_mock_llm=True) as reopened:
        with reopened.scheduler._lock:
            reopened.scheduler._note_memory_written("夏", 0.1, "obs")
        assert reopened.scheduler._reflection_watermark["夏"] == pytest.approx(1.3), (
            "重开应当从落盘的 1.2 接着走"
        )


def test_retrieve_can_be_a_pure_read(store):
    """`retrieve` 默认加固(检索就是复习),但 reinforce=False 时必须一个字不写。

    它是个名字像"读"的写接口——宿主每轮聊天经 `agent_context` 调用一次,
    就会写 5 行。调试、后台分析、只读视图需要一条不留痕的路径。
    """
    store.add("夏", tick=10, kind="obs", summary="在咖啡店遇见了遥", importance=0.6)
    store.add("夏", tick=20, kind="obs", summary="港口的轮渡晚点了", importance=0.4)

    before = {m["id"]: m["strength"] for m in store.query(agent_id="夏")}
    assert before, "夹具是空库,得先造数据"
    store.retrieve("夏", now_tick=100, query="咖啡店", k=3, reinforce=False)
    after = {m["id"]: m["strength"] for m in store.query(agent_id="夏")}
    assert after == before, "reinforce=False 不许改 strength"

    store.retrieve("夏", now_tick=100, query="咖啡店", k=3)
    reinforced = {m["id"]: m["strength"] for m in store.query(agent_id="夏")}
    assert any(reinforced[i] > before[i] for i in before), "默认 reinforce=True 必须加固"


def test_decay_uses_no_sql_math_functions(store):
    """回归:遗忘曾用 SQL 的 pow()。

    那个函数要 SQLite 编译时打开 SQLITE_ENABLE_MATH_FUNCTIONS 才有,缺了会抛
    OperationalError —— 而调用方(日切)把异常吞成 warning,于是遗忘静默地
    永不发生。算术改在 Python 里做,SQL 里不许再出现数学函数。
    """
    store.add("夏", tick=0, kind="obs", summary="很久以前的事", importance=0.5)
    store.add("夏", tick=900, kind="obs", summary="刚发生的事", importance=0.5)
    store.add("夏", tick=0, kind="obs", summary="锚点不衰减", importance=0.9, anchor=True)

    store.decay_pass("夏", now_tick=1000, ticks_per_day=288)

    rows = {m["summary"]: m["strength"] for m in store.query(agent_id="夏")}
    assert rows["锚点不衰减"] == 1.0
    assert rows["很久以前的事"] < rows["刚发生的事"] < 1.0, "闲置越久掉得越多"
