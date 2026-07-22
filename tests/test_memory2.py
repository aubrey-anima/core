"""记忆 2.0(v2.0 / db 2):三因子检索、加固、遗忘曲线、按强度淘汰、反思。"""
from __future__ import annotations

import pytest

from anima_world.db import open_db
from anima_world.memory_retrieval import score, similarity
from anima_world.memory_store import MemoryStore


@pytest.fixture
def store(tmp_path):
    conn = open_db(tmp_path / "w.db")
    yield MemoryStore(conn)
    conn.close()


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
    conn = open_db(tmp_path / "w.db")
    store = MemoryStore(conn, capacity=2)
    old_but_strong = store.add("夏", tick=0, kind="obs", summary="旧但常被想起", importance=0.5)
    store.retrieve("夏", now_tick=10, k=1)  # 加固最旧那条
    store.add("夏", tick=50, kind="obs", summary="新但没人记得", importance=0.5)
    conn.execute("UPDATE memories SET strength = 0.1 WHERE summary = '新但没人记得'")
    conn.commit()
    store.add("夏", tick=100, kind="obs", summary="触发淘汰的第三条", importance=0.5)
    summaries = {m["summary"] for m in store.query("夏")}
    assert "旧但常被想起" in summaries, "淘汰按强度,不再按最旧"
    assert "新但没人记得" not in summaries
    conn.close()


def test_reflection_emerges_from_accumulated_importance(tmp_path):
    """重要度累计过阈值 → mock 反思器产出洞察 → 以 memory_seed(kind=reflection)
    事件落地 —— LLM 只提案,事件日志记录,重放可重建。"""
    from anima_world.api import World

    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
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
