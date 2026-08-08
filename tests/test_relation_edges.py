"""关系图的边:什么该冻住,什么该撤销。

两件事长得很像,判断相反:

- `(subject, predicate, object)` 重复写**不覆盖**(INSERT OR IGNORE),于是
  `source_event_seq` 冻在第一次 —— **这是设计**。边记的是"这两人是朋友"这个
  *事实*,不是"他们又亲近了一次"这个*事件*。关系在「亲近」「挚交」之间来回
  跨档时事实没变;重写出处只会让"这条边是哪件事立起来的"每次都换个答案。
  值本身的变化在 relations 那一层,边不是它的第二份拷贝。
- 边**只增不减**是缺陷,而且正是被上面那条挡住看不见的那个:一对从「亲近」
  跌进「交恶」的人身上会同时挂着 friendship 与 rivalry,永远。
  `cliques.compute_cliques` 只看 friendship,于是那个小团体里坐着两个此刻互相
  看不顺眼的人 —— 算得出来、画得出来、一条日志都不报错。
"""
from __future__ import annotations

import pytest

from anima_world.memory_store import MemoryDescriptor


@pytest.fixture
def graph():
    import fakeredis

    from anima_world.redis_state import RedisKnowledgeGraph

    return RedisKnowledgeGraph(fakeredis.FakeStrictRedis(decode_responses=True), "t")


def test_repeated_add_keeps_the_first_provenance(graph):
    """同一条边写两次,出处冻在第一次 —— 设计,不是缺陷。"""
    graph.add("agent:夏", "friendship", "agent:遥", source_event_seq=713, created_at=100)
    graph.add("agent:夏", "friendship", "agent:遥", source_event_seq=1360, created_at=3264)

    rows = graph.query(subject="agent:夏")
    assert len(rows) == 1, "同一个三元组只有一条边"
    assert rows[0]["source_event_seq"] == 713, "第一次被证实的那一刻说了算"
    assert rows[0]["created_at"] == 100


def test_drop_reports_whether_the_edge_was_there(graph):
    graph.add("agent:夏", "friendship", "agent:遥")
    assert graph.drop("agent:夏", "friendship", "agent:遥") is True
    assert graph.drop("agent:夏", "friendship", "agent:遥") is False
    assert graph.query(subject="agent:夏") == []


def _shift(scheduler, who, target, value, *, tick, seq):
    """走 `_on_relation_shift` 那条真路,而不是直接调 knowledge_graph。"""
    scheduler._on_relation_shift(
        {"type": "state_change", "who": who,
         "payload": {"kind": "sentiment", "as": who, "target": target, "sentiment": value}},
        MemoryDescriptor(agent_id=who, tick=tick, kind="relation_shift",
                         summary="", importance=0.5, event_seq=seq),
    )


def test_a_reversal_retracts_the_opposite_edge(open_world, bare_seed):
    """反目要撤掉 friendship,否则小团体里坐着两个仇人。"""
    world = open_world(world_file=bare_seed)
    scheduler = world.scheduler
    graph = scheduler.knowledge_graph

    _shift(scheduler, "夏", "遥", 0.6, tick=100, seq=1)
    assert {r["predicate"] for r in graph.query(subject="agent:夏")} == {"friendship"}
    assert graph.query(subject="agent:夏")[0]["created_at"] == 100, (
        "出处得说得出哪一刻 —— 此前默认 0,每条边都自称生于创世"
    )

    _shift(scheduler, "夏", "遥", -0.6, tick=400, seq=2)
    predicates = {r["predicate"] for r in graph.query(subject="agent:夏")}
    assert predicates == {"rivalry"}, f"friendship 必须被撤掉,实得 {predicates}"
    # 反向的那条也要撤 —— 边是成对加的,只撤一半等于留一条单向的假朋友。
    assert {r["predicate"] for r in graph.query(subject="agent:遥")} == {"rivalry"}


def test_a_reversal_takes_them_out_of_the_same_clique(open_world, bare_seed):
    """病本身:撤销之前,`cliques` 会把这对仇人算成一个小团体。"""
    from anima_world.cliques import compute_cliques

    world = open_world(world_file=bare_seed)
    scheduler = world.scheduler

    _shift(scheduler, "夏", "遥", 0.6, tick=100, seq=1)
    assert compute_cliques(scheduler.knowledge_graph.query()), "先得真有一个小团体"

    _shift(scheduler, "夏", "遥", -0.6, tick=400, seq=2)
    assert compute_cliques(scheduler.knowledge_graph.query()) == [], (
        "反目之后不该还在同一个小团体里"
    )


def test_drifting_into_the_neutral_band_does_not_retract(open_world, bare_seed):
    """淡下来不等于反目 —— 撤了的话边会随数字的小幅摆动来回闪。"""
    world = open_world(world_file=bare_seed)
    scheduler = world.scheduler

    _shift(scheduler, "夏", "遥", 0.6, tick=100, seq=1)
    _shift(scheduler, "夏", "遥", 0.0, tick=400, seq=2)

    assert {r["predicate"] for r in scheduler.knowledge_graph.query(subject="agent:夏")} \
        == {"friendship"}
