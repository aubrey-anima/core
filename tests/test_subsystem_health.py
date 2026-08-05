"""降级要在世界里留下痕迹。

引擎的降级纪律是"加载严格 / 运行降级":LLM 挂了世界照跑,叙事退回模板,规划退回
作息表。这是对的。问题在于**它只在 stderr 刷一行 warning** —— 日志会滚掉,而"这个
世界当时跑在什么档位上"恰恰是解释它为什么长成这样的关键:一个整整三天没有 planner
的世界,和一个角色确实无所事事的世界,产物看起来一模一样。

所以两件事:计数进 `state()`(现在怎么样),档位切换落一条事件(一路上怎么样)。
**只在切换时发事件**,不是每次都发 —— 一个持续降级的子系统会每 tick 触发一次,那样
事件日志会被自己的健康报告淹掉(needs 抖动那个教训)。
"""
from __future__ import annotations

from _worldfile import open_world_at

import pytest

from anima_world.api import World


@pytest.fixture
def world(tmp_path):
    w = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    yield w
    w.close()


def _health_events(world):
    return [
        e for e in world.history(kind="subsystem_health", limit=1000)["events"]
    ]


def test_a_working_subsystem_does_not_announce_itself(world):
    """开机就正常的东西不该发事件 —— 健康报告不是噪音源。"""
    world.tick(30)
    assert _health_events(world) == []


def test_a_degrading_subsystem_leaves_an_event_and_a_count(world):
    world.tick(1)
    world.scheduler.note_subsystem("probe", False, "provider exploded")

    events = _health_events(world)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["subsystem"] == "probe"
    assert payload["status"] == "degraded"
    assert "exploded" in payload["reason"]

    health = world.state()["runtime"]["subsystems"]["probe"]
    assert health["status"] == "degraded" and health["degraded"] == 1


def test_a_persistent_degradation_does_not_flood_the_log(world):
    """持续降级只发一条 —— 计数照常涨。"""
    world.tick(1)
    for _ in range(20):
        world.scheduler.note_subsystem("probe2", False, "timeout")

    assert len(_health_events(world)) == 1, "每次降级都发事件的话,日志会被自己淹掉"
    assert world.state()["runtime"]["subsystems"]["probe2"]["degraded"] == 20


def test_recovery_is_announced_too(world):
    """只报坏消息的话,读日志的人没法知道它什么时候好的。"""
    world.tick(1)
    world.scheduler.note_subsystem("probe2", False, "timeout")
    world.scheduler.note_subsystem("probe2", True)

    statuses = [e["payload"]["status"] for e in _health_events(world)]
    assert statuses == ["degraded", "ok"]
    assert world.state()["runtime"]["subsystems"]["probe2"]["status"] == "ok"


def test_the_health_record_survives_a_restart(tmp_path):
    """"当时跑在什么档位上"必须是历史,不是内存里的一个数。"""
    db = str(tmp_path / "w.db")
    with open_world_at(db, force_mock_llm=True) as world:
        world.tick(1)
        world.scheduler.note_subsystem("probe", False, "no api key")

    with open_world_at(db, force_mock_llm=True) as reopened:
        events = _health_events(reopened)
        assert [e["payload"]["subsystem"] for e in events] == ["probe"]
        assert "no api key" in events[0]["payload"]["reason"]


def test_a_real_narrative_run_counts_successes(world):
    """接线检查:真跑一段,叙事的成功计数必须真的在涨(这条盯的是接线本身)。"""
    world.tick(120)
    world.close(wait=True)  # 叙事在线程池上,排干才数得准
    health = world.state()["runtime"]["subsystems"]
    assert health.get("narrative", {}).get("ok", 0) > 0, health
