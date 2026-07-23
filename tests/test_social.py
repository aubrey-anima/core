"""关系 2.0(原路线图 v5):三轴关系、判官轴裁定、八卦传播、小团体。"""
from __future__ import annotations

import pytest

from anima_world.api import World
from anima_world.cliques import compute_cliques
from anima_world.gossip import pick_gossip
from anima_world.projection import project_events
from anima_world.relationship_judge import RelationshipJudge
from anima_world.types import Event


class _FakeLLM:
    def __init__(self, reply: str) -> None:
        self._reply = reply

    def complete_sync(self, messages):  # noqa: ARG002
        return self._reply


def test_judge_parses_and_clamps_axes():
    judge = RelationshipJudge(_FakeLLM(
        '{"summary": "聊了展览的事", "delta_a_to_b": 0.1, "delta_b_to_a": 0.05,'
        ' "axes_a_to_b": {"trust": 0.9, "affection": 0.02, "junk": 1},'
        ' "axes_b_to_a": "garbage"}'
    ))
    result = judge.judge({}, {}, {}, [], [], "cafe")
    assert result is not None
    assert result.axes_a_to_b == {"trust": 0.2, "affection": 0.02}, "轴必须裁剪到 ±0.2 且丢弃未知轴"
    assert result.axes_b_to_a == {}, "垃圾轴降级为无,不炸"


def test_projection_folds_axes_and_stays_backward_compatible():
    proj = project_events([
        Event(seq=1, ts=1, type="state_change", loc=None, who="夏",
              payload={"kind": "sentiment_delta", "as": "夏", "target": "遥",
                       "delta": 0.1, "axes": {"trust": 0.2, "respect": "junk"}}),
        Event(seq=2, ts=2, type="state_change", loc=None, who="夏",
              payload={"kind": "sentiment_delta", "as": "夏", "target": "遥", "delta": 0.05}),
    ])
    rel = proj.relations[("夏", "遥")]
    assert rel.sentiment == pytest.approx(0.15)
    assert rel.trust == pytest.approx(0.2)
    assert rel.respect == 0.0, "坏轴值跳过,单轴 fold 不受影响"


class _Roll:
    def __init__(self, value: float) -> None:
        self._value = value

    def random(self) -> float:
        return self._value


def test_gossip_picks_most_important_and_distorts():
    memories = [
        {"kind": "obs", "summary": "小事", "importance": 0.3},
        {"kind": "chat", "summary": "夏和遥大吵了一架", "importance": 0.9},
        {"kind": "reflection", "summary": "内心独白", "importance": 0.95},
        {"kind": "hearsay3", "summary": "三手谣言", "importance": 0.8},
    ]
    picked = pick_gossip(_Roll(0.0), "苏晚夏", memories, "柔")
    assert picked is not None
    assert picked["kind"] == "hearsay1"
    assert "大吵了一架" in picked["summary"] and picked["summary"].startswith("听苏晚夏说")
    assert picked["importance"] == pytest.approx(0.9 * 0.85)
    assert pick_gossip(_Roll(0.99), "苏晚夏", memories, "柔") is None, "骰子没中不传"


def test_cliques_are_friendship_components():
    edges = [
        {"subject": "夏", "predicate": "friendship", "object": "遥"},
        {"subject": "遥", "predicate": "friendship", "object": "柔"},
        {"subject": "夏", "predicate": "rivalry", "object": "北"},
        {"subject": "岚", "predicate": "friendship", "object": "屿"},
    ]
    groups = {frozenset(g) for g in compute_cliques(edges)}
    assert groups == {frozenset({"夏", "遥", "柔"}), frozenset({"岚", "屿"})}, (
        "friendship 连通、rivalry 不算、单人不成团"
    )


def test_world_integration_gossip_and_cliques(tmp_path, monkeypatch):
    import anima_world.gossip as gossip_mod

    monkeypatch.setattr(gossip_mod, "GOSSIP_PROBABILITY", 1.0)
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.config_set("social.enabled", "true")
        # 给夏一条高重要度记忆,然后直接触发一次对柔的八卦
        world.scheduler._record_event({
            "type": "memory_seed", "who": "夏",
            "payload": {"agent_id": "夏", "kind": "obs",
                        "summary": "港口的仓库半夜着过火", "importance": 0.9},
        })
        agent = world.scheduler.agents["夏"].agent
        with world.scheduler._lock:
            world.scheduler._maybe_gossip(agent, "柔")
        hearsay = [m for m in world.memories("柔") if m["kind"].startswith("hearsay")]
        assert hearsay and "着过火" in hearsay[0]["summary"]
        # 同日第二次不再掷骰子
        with world.scheduler._lock:
            world.scheduler._maybe_gossip(agent, "柔")
        assert len([m for m in world.memories("柔") if m["kind"].startswith("hearsay")]) == 1

        # 小团体:种 friendship 边,日切重算
        world.scheduler.knowledge_graph.add("夏", "friendship", "遥")
        world.scheduler.clock = 287
        world.tick(1)
        groups = world.cliques()
        assert groups and set(groups[0]["member_ids"]) == {"夏", "遥"}
