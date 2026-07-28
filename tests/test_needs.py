"""需求系统(v3.0 / db 3):曲线结算、紧急需求带、开关语义、持久化。"""
from __future__ import annotations

import pytest

from anima_world.api import World
from anima_world.bt_nodes import Blackboard, NeedAction, Status
from anima_world.needs import NEEDS, RELEASE, URGENT, settle


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


# ── 迟滞:一顿饭是一顿饭,不是咬一口 ──────────────────────────────────────────
#
# 单阈值的后果实测过:hunger 永远卡在 0.155–0.171,300 个 tick 里只有**两个**不同
# 取值 —— 掉到 0.15 就补一 tick 跨回线上,立刻回去干活,十来 tick 后再饿回来。
# 角色一顿饱饭都没吃过,而每一次切换都发一条 agent_action + 一条 narrative:
# 事件量 19.7×、narrative 32×(配了真 key 就是 32 倍 LLM 账单)、耗时 7×。


def test_a_meal_is_a_meal_not_a_bite():
    """开始吃就吃到饱:跨过触发线不等于收工,要吃到释放线。"""
    bb = Blackboard()
    leaf = NeedAction("hunger", URGENT, "eat", release=RELEASE["hunger"])

    bb.write("need.hunger", 0.10)
    assert leaf.tick(bb) == Status.SUCCESS, "饿到触发线,开吃"

    bb.write("need.hunger", 0.30)          # 已经高于触发线
    bb.write("need._restoring", ("hunger",))  # 但这一口还在吃
    assert leaf.tick(bb) == Status.SUCCESS, "正在吃就该吃到释放线"

    bb.write("need.hunger", RELEASE["hunger"] + 0.01)
    assert leaf.tick(bb) == Status.FAILURE, "吃饱了就该收工"


def test_the_release_line_only_applies_to_the_need_being_restored():
    """睡觉不该顺便把"没那么饿"也算成正在吃 —— 否则任何低于释放线的需求都抢 duty。"""
    bb = Blackboard()
    leaf = NeedAction("hunger", URGENT, "eat", release=RELEASE["hunger"])
    bb.write("need.hunger", 0.30)
    bb.write("need._restoring", ("energy",))
    assert leaf.tick(bb) == Status.FAILURE


def test_a_node_without_a_release_line_behaves_exactly_as_before():
    """老库里的作者树没有 release:行为必须逐 tick 不变。"""
    bb = Blackboard()
    leaf = NeedAction("hunger", 0.15, "eat")
    bb.write("need.hunger", 0.30)
    bb.write("need._restoring", ("hunger",))
    assert leaf.tick(bb) == Status.FAILURE


def _hunger_samples(world, ticks: int = 300) -> set[float]:
    seen = set()
    for _ in range(ticks):
        world.tick(1)
        seen.add(round(world.needs("夏")["hunger"], 2))
    return seen


def test_an_agent_actually_gets_full_instead_of_hovering_at_the_trigger_line(tmp_path):
    """跑进稳态之后,饥饿度必须走出一条真的曲线,而不是钉在触发线上方。"""
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.config_set("needs.enabled", "true")
        world.tick(288 * 3)  # 先进稳态

        seen = _hunger_samples(world)
        assert max(seen) > 0.5, (
            f"300 tick 内 hunger 最高只到 {max(seen)} —— 她一顿饱饭都没吃过:{sorted(seen)}"
        )
        assert len(seen) > 5, f"取值只有 {sorted(seen)},这是钉在阈值上抖,不是曲线"


def test_hysteresis_does_not_let_anyone_sleep_through_their_shift(tmp_path):
    """释放线定太高会让角色睡穿整个班 —— 省下的事件不能是拿"再也不上班"换的。"""
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.config_set("needs.enabled", "true")
        world.tick(288 * 4)

        # 直接读事件表:World.events() 是个 200 格的内存窗口,四天的历史装不下。
        # 上班发的是 state_change/agent_state(actions.py:70),不是 agent_action。
        worked = {
            who
            for (who,) in world.scheduler.event_log.conn.execute(
                "SELECT DISTINCT who FROM events WHERE type = 'state_change'"
                " AND json_extract(payload, '$.state.status') = 'working'"
            )
        }
        assert worked >= set(world.scheduler.agents), (
            f"四天里只有 {worked or '没有人'} 上过班 —— 迟滞把作息压死了"
        )


def test_hysteresis_cuts_the_event_churn(tmp_path):
    """口径断言:needs 打开不该让世界贵一个数量级。

    实测单阈值下 12 世界日 narrative 从 125 涨到 3989(32×)—— 那是配了真 key 之后
    的 32 倍账单,而世界并没有变得 32 倍有趣。这里断的是"同一个数量级",不是某个
    具体数字:世界逐次不确定,钉死数字会假绿。
    """
    import sqlite3

    def narrative_count(enabled: bool) -> int:
        db = tmp_path / f"w{enabled}.db"
        with World.open(str(db), force_mock_llm=True) as w:
            if enabled:
                w.config_set("needs.enabled", "true")
            w.tick(288 * 6)
        # **必须关闭之后再数**:叙事跑在线程池上(永不进 tick 线程),close 才排干
        # 它。在 with 块里数会漏掉还在队列里的那些 —— 实测会把 3989 数成 24,
        # 于是这条测试变成一条永远绿的假测试。
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM events WHERE type = 'narrative'"
            ).fetchone()[0]
        finally:
            conn.close()

    off, on = narrative_count(False), narrative_count(True)
    assert on < off * 5, f"needs 打开后 narrative {off} → {on}({on/max(off,1):.1f}×)"


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
