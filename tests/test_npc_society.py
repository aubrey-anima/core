"""角色之间会不会长出关系。

README 讲的是"会记住你的角色",但一个世界之所以像世界,是因为**别人之间也有事**。
三轴关系是常开机制,`social.enabled` 还带来八卦与小团体 —— 然而实测跑七天,NPC 之间
的关系值几乎不动、`cliques()` 恒为 `[]`。

原因不在机制,在**内置演示世界**:柔 每天 15:00 走到咖啡店、15:30–17:30 在那儿待着,
而 夏 08:00–18:30 一直在店里 —— 两个人每天同处一室两小时。但柔那段的动作是
`idle_social`(「想找个人说说话」),它**不指名道姓**,所以只传八卦、不触发关系判定
(REFERENCE §2.9 第二条边界)。于是演示世界看起来是"社交机制没用",实际是"没有人
真的开口"。

这条测试盯的是结果不是实现:同处一室的两个人,跑几天之后关系必须动过。
"""
from __future__ import annotations

import time

import pytest

from anima_world.api import World

A_DAY = 288


def _npc_relations(world) -> dict[tuple[str, str], float]:
    roster = set(world.scheduler.agents)
    return {
        pair: rel.sentiment
        for pair, rel in world.scheduler._memory_projection.relations.items()
        if pair[0] in roster and pair[1] in roster and abs(rel.sentiment) > 1e-9
    }


def _run_until_relations(world, *, max_days: int = 6) -> dict[tuple[str, str], float]:
    """跑到 NPC 之间的关系动起来为止。轮询而不是写死 tick —— 判定跑在线程池上,
    而世界逐次不确定。"""
    for _ in range(max_days):
        world.tick(A_DAY)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            found = _npc_relations(world)
            if found:
                return found
            time.sleep(0.02)
    return _npc_relations(world)


def test_two_npcs_who_share_a_room_every_afternoon_form_a_relationship(tmp_path):
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        relations = _run_until_relations(world)
        assert relations, (
            "跑了好几个世界日,NPC 之间一条关系都没有 —— 演示世界里没有人真的开口"
        )


def test_the_demo_world_puts_two_people_in_the_same_room_on_purpose(tmp_path):
    """前提检查:如果这两个人根本碰不到面,上面那条测的就不是同一件事。"""
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        together = False
        for _ in range(A_DAY * 2):
            world.tick(1)
            locs = [
                brain.agent.blackboard.read("loc")
                for aid, brain in world.scheduler.agents.items()
                if aid not in world.scheduler._transit
            ]
            if len(locs) != len(set(locs)):
                together = True
                break
        assert together, "演示世界里没有任何两个人同处一室 —— 那社交无从谈起"


def test_a_named_chat_duty_is_what_makes_the_judge_fire(tmp_path):
    """口径:`idle_social` 只传八卦,带对象的 `chat` 才产生关系判定。

    这不是实现细节,是 REFERENCE §2.9 明写的边界 —— 演示世界必须站在能兑现标题的
    那一侧。
    """
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        # 只看**作息表的叶子**(`<duty>_do`)。动作表里本来就有 `chat_with_<某人>`
        # 供 planner 挑,但没有 key 就没有 planner —— 而没有 key 是默认状态。
        duty_leaves = world.scheduler.event_log.conn.execute(
            "SELECT node_id, kind, params FROM bt_actions WHERE node_id LIKE '%\\_do' ESCAPE '\\'"
        ).fetchall()
        assert duty_leaves, "演示世界一条作息都没有?"
        chats = [(n, p) for n, k, p in duty_leaves if k == "chat"]
        assert chats, (
            "没有任何一段作息是带对象的 chat —— 于是关系判定在默认状态下永不触发,"
            f"作息叶子只有:{[(n, k) for n, k, _ in duty_leaves]}"
        )
        assert any("target" in (p or "") for _, p in chats), (
            f"chat 作息没写 target,判定同样不会触发:{chats}"
        )
