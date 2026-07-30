"""劳动与食物有没有后果。

经济和需求各自都有机制,但它们之间没有闭环 —— 两处"存了却没人读":

1. **工资跟上没上班无关。** 日切时每个角色都拿一份 `economy.daily_wage`,金库允许
   无限负债(ARCHITECTURE:324 明写"行为树会去打工"那部分尚未实现)。一个整天睡觉的
   人和一个开了十小时店的人,到手一样多 —— 那"经济"就只是一个每天加数的计数器。
2. **`item_defs.restores` 存了从来没人读。** schema 里有这一列、创世时写进去,而
   `needs.RESTORE_PER_TICK["eat"]` 是个跟吃什么无关的常数。作者认真写的"这碗面很顶饱"
   在世界里没有任何差别。

两条都不改 db 格式:工资按已有的 `payment` 事件发,进食读已有的 `item_defs` 列。
"""
from __future__ import annotations

import json

import pytest

from anima_world.api import World

A_DAY = 288


def _wages(world) -> dict[str, float]:
    rows = world.scheduler.event_log.conn.execute(
        "SELECT who, SUM(json_extract(payload, '$.amount')) FROM events"
        " WHERE type = 'payment' AND json_extract(payload, '$.reason') = 'daily_wage'"
        " GROUP BY who"
    ).fetchall()
    return {who: float(total) for who, total in rows}


def test_wages_follow_the_hours_actually_worked(tmp_path):
    """开了十小时店的人和整天睡觉的人,不该拿一样多。"""
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.config_set("economy.enabled", "true")
        world.fast_forward(A_DAY * 3)

        paid = _wages(world)
        assert paid, "三天了一分工资都没发?"
        assert len(set(paid.values())) > 1, (
            f"所有人拿的一样多,工资和上不上班无关:{paid}"
        )


def test_nobody_is_paid_for_a_day_they_did_not_work(tmp_path):
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.config_set("economy.enabled", "true")
        # 把所有人钉在睡觉上:没人上班,就不该有人拿工资
        for brain in world.scheduler.agents.values():
            brain.agent.blackboard.write("_forced_idle", True)
        world.fast_forward(A_DAY)

        for who, amount in _wages(world).items():
            worked = world.scheduler.event_log.conn.execute(
                "SELECT COUNT(*) FROM events WHERE who = ? AND type = 'state_change'"
                " AND json_extract(payload, '$.state.status') = 'working'",
                (who,),
            ).fetchone()[0]
            if not worked:
                assert amount == 0, f"{who} 没上过班却拿了 {amount}"


def test_what_you_eat_is_what_you_get(tmp_path):
    """`item_defs.restores` 必须真的生效 —— 不然作者写的"这碗面很顶饱"是句空话。"""
    from anima_world import economy, needs

    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.config_set("economy.enabled", "true")
        world.config_set("needs.enabled", "true")
        world.tick(1)

        conn = world.scheduler.event_log.conn
        conn.execute(
            "INSERT OR REPLACE INTO item_defs (id, name, kind, base_price, restores)"
            " VALUES ('big_bowl', '大碗面', 'consumable', 12.0, ?)",
            (json.dumps({"hunger": 0.5}),),
        )

        brain = world.scheduler.agents["夏"]
        brain.agent.blackboard.write("need.hunger", 0.1)
        world.scheduler._record_event({
            "type": "item_consume", "who": "夏",
            "payload": {"who": "夏", "item_id": "big_bowl", "source": "test"},
        })

        after = brain.agent.blackboard.read("need.hunger")
        assert after >= 0.55, f"吃了一碗回 0.5 的面,饱腹只到 {after}"


def test_an_item_with_no_restores_changes_nothing(tmp_path):
    """不是食物的东西吃下去不该回血 —— 也不该报错。"""
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.config_set("needs.enabled", "true")
        world.tick(1)
        brain = world.scheduler.agents["夏"]
        brain.agent.blackboard.write("need.hunger", 0.3)

        world.scheduler._record_event({
            "type": "item_consume", "who": "夏",
            "payload": {"who": "夏", "item_id": "从来没定义过的东西", "source": "test"},
        })
        assert brain.agent.blackboard.read("need.hunger") == pytest.approx(0.3)


def test_eating_does_nothing_when_needs_are_off(tmp_path, bare_seed):
    """开关关着时行为逐 tick 不变 —— 这是每个开关的既有承诺。

    素配种子:内置橱窗**替这个世界点亮了** needs,拿它来验"关着会怎样"是自相矛盾。
    """
    with World.open(str(tmp_path / "w.db"), seed_path=bare_seed, force_mock_llm=True) as world:
        world.tick(1)
        world.scheduler._record_event({
            "type": "item_consume", "who": "夏",
            "payload": {"who": "夏", "item_id": "meal", "source": "test"},
        })
        assert world.needs("夏") == {}
