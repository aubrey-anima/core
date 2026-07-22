"""需求系统(v3.0 / db 3):曲线结算、紧急需求带、开关语义、持久化。"""
from __future__ import annotations

import pytest

from anima_world.api import World
from anima_world.bt_nodes import Blackboard, NeedAction, Status
from anima_world.needs import NEEDS, settle


def test_settle_decays_and_restores():
    values = {"energy": 1.0, "hunger": 1.0, "social": 1.0}
    after = settle(values, 96, None)  # 8 世界小时无所事事
    assert after["energy"] < 1.0 and after["hunger"] < 1.0 and after["social"] < 1.0
    slept = settle({"energy": 0.2, "hunger": 1.0, "social": 1.0}, 96, "sleep")
    assert slept["energy"] > 0.9, "睡 8 小时应基本回满精力"
    assert 0.0 <= min(v for k, v in slept.items()) and max(slept.values()) <= 1.0


def test_mood_is_the_weakest_need():
    low_social = settle({"energy": 1.0, "hunger": 1.0, "social": 0.1}, 0, None)
    fine = settle({"energy": 0.9, "hunger": 0.9, "social": 0.9}, 0, None)
    assert low_social["mood"] < fine["mood"]


def test_need_action_leaf_fires_below_threshold_and_is_inert_without_values():
    bb = Blackboard()
    leaf = NeedAction("hunger", 0.15, "eat")
    assert leaf.tick(bb) == Status.FAILURE, "无 need.* 值(未点亮)必须完全惰性"
    bb.write("need.hunger", 0.5)
    assert leaf.tick(bb) == Status.FAILURE
    bb.write("need.hunger", 0.1)
    assert leaf.tick(bb) == Status.SUCCESS
    assert bb.read("_selected_action_id") == "eat"


@pytest.fixture
def world(tmp_path):
    w = World.open(str(tmp_path / "w.db"), force_mock_llm=True)
    yield w
    w.close()


def test_disabled_by_default_blackboard_stays_clean(world):
    world.tick(3)
    assert world.needs("夏") == {}, "needs.enabled 默认关,行为与 v2 逐 tick 一致"


def test_urgent_hunger_overrides_duty(world):
    world.config_set("needs.enabled", "true")
    world.tick(1)  # 结算一次,need.* 上黑板
    brain = world.scheduler.agents["夏"]
    brain.agent.blackboard.write("need.hunger", 0.05)
    # 把时间拨到夏的值班窗口内,证明吃饭压过 duty
    world.scheduler.clock = 8 * 60 // 5 + 288  # 第二天 08:00
    world.tick(1)
    action = world.scheduler._current_action.get("夏")
    assert action is not None and action.kind == "eat", "饿到紧急线必须先吃饭再上班"
    hunger_before = world.needs("夏")["hunger"]
    world.tick(6)
    assert world.needs("夏")["hunger"] > hunger_before, "吃着饭饥饿度必须回升"


def test_needs_persist_across_reopen(tmp_path):
    db = str(tmp_path / "w.db")
    with World.open(db, force_mock_llm=True) as world:
        world.config_set("needs.enabled", "true")
        world.tick(1)
        world.scheduler.agents["夏"].agent.blackboard.write("need.hunger", 0.33)
    with World.open(db, force_mock_llm=True) as reopened:
        reopened.tick(1)
        hunger = reopened.needs("夏")["hunger"]
        assert hunger == pytest.approx(0.33, abs=0.02), "关闭时落盘,重开接着曲线走"


def test_needs_show_up_in_state(world):
    world.config_set("needs.enabled", "true")
    world.tick(1)
    agent_state = world.state()["agents"]["夏"]
    assert set(NEEDS) <= set(agent_state.get("needs", {}))
