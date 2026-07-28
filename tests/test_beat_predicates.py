"""导演能观察到多少世界。

谓词曾经只有两个:关系值、同地。于是节拍脚本对世界的绝大部分状态是瞎的 —— 需求、
钱、物品、关系描述、记忆一律看不见,剧情只能靠"到点了"和"两个人碰上了"来推。而
这些量投影和黑板里全都有,不进事件日志、不改 db 格式。

三条口径:
- 读不到的东西一律读作"未满足"(宁可晚触发,不可错触发,下 tick 还会再试)。
- `co_located` 继续读活黑板,**不是**因为投影不追落地(1.1.1 起它追了),而是投影
  不知道"在途" —— 两个正在赶路的人按投影算就成了同处一室。
- `memory` 谓词是**纯读**:它不加固记忆。观察不该改变被观察的东西。
"""
from __future__ import annotations

import pytest

from anima_world.beats import (
    PREDICATE_REQUIRED_FIELDS,
    _VALID_PREDICATES,
    _eval_predicate,
    _validate_predicate,
)
from anima_world.types import Projection, Relation


class _Reader:
    def __init__(self, needs=None, memories=None):
        self._needs = needs or {}
        self._memories = memories or {}

    def need(self, agent_id, need):
        return self._needs.get((agent_id, need))

    def memories(self, agent_id):
        return self._memories.get(agent_id, [])


def _projection(**kwargs) -> Projection:
    proj = Projection()
    for key, value in kwargs.items():
        setattr(proj, key, value)
    return proj


def test_every_predicate_declares_its_required_fields():
    assert set(PREDICATE_REQUIRED_FIELDS) == _VALID_PREDICATES, (
        "谓词表与必填字段表对不上,作者照文档写会被拒"
    )


def test_r_type_predicate_reads_the_authored_label():
    proj = _projection(relations={("夏", "遥"): Relation(sentiment=0.4, r_type="有点在意的人")})
    pred = {"pred": "r_type", "as": "夏", "target": "遥", "contains": "在意"}
    assert _eval_predicate(pred, proj, {}) is True
    assert _eval_predicate({**pred, "contains": "宿敌"}, proj, {}) is False


def test_money_and_item_predicates_read_the_ledger_projection():
    proj = _projection(balances={"夏": 42.0}, inventories={"夏": {"umbrella": 2}})
    assert _eval_predicate(
        {"pred": "money", "agent": "夏", "op": "gte", "value": 40}, proj, {}) is True
    assert _eval_predicate(
        {"pred": "money", "agent": "夏", "op": "gte", "value": 100}, proj, {}) is False
    assert _eval_predicate(
        {"pred": "has_item", "agent": "夏", "item": "umbrella"}, proj, {}) is True
    assert _eval_predicate(
        {"pred": "has_item", "agent": "夏", "item": "umbrella", "min": 3}, proj, {}) is False
    assert _eval_predicate(
        {"pred": "has_item", "agent": "遥", "item": "umbrella"}, proj, {}) is False


def test_need_and_memory_predicates_use_the_reader():
    reader = _Reader(needs={("夏", "hunger"): 0.1}, memories={"夏": ["阿檀说他在找一把旧伞"]})
    proj = _projection()
    assert _eval_predicate(
        {"pred": "need", "agent": "夏", "need": "hunger", "op": "lte", "value": 0.2},
        proj, {}, reader) is True
    assert _eval_predicate(
        {"pred": "memory", "agent": "夏", "contains": "旧伞"}, proj, {}, reader) is True
    assert _eval_predicate(
        {"pred": "memory", "agent": "夏", "contains": "从没说过的事"}, proj, {}, reader) is False


def test_a_predicate_with_nothing_to_read_is_not_met_rather_than_true():
    """needs 没点亮 / 没有 MemoryStore 时,不许把"读不到"当成"满足"。"""
    proj = _projection()
    assert _eval_predicate(
        {"pred": "need", "agent": "夏", "need": "hunger", "op": "lte", "value": 0.9},
        proj, {}, None) is False
    assert _eval_predicate(
        {"pred": "memory", "agent": "夏", "contains": "什么"}, proj, {}, None) is False


@pytest.mark.parametrize("pred,expected_fragment", [
    ({"pred": "need", "agent": "夏", "need": "不存在的需求", "op": "lte", "value": 0.1},
     "unknown need"),
    ({"pred": "need", "agent": "夏", "need": "hunger", "op": "eq", "value": 0.1},
     "must be 'gte' or 'lte'"),
    ({"pred": "money", "agent": "夏", "op": "gte"}, "'value'"),
    ({"pred": "has_item", "agent": "夏"}, "'item'"),
    ({"pred": "r_type", "as": "夏", "target": "遥"}, "contains"),
])
def test_a_malformed_predicate_is_refused_at_load_time(pred, expected_fragment):
    """加载期严格 —— 坏谓词不能流到世界启动。"""
    errors = _validate_predicate(pred, "beat 'x'")
    assert errors, f"{pred} 应当被拒"
    assert any(expected_fragment in e for e in errors), errors


def test_the_old_two_predicates_still_behave_exactly_as_before():
    proj = _projection(relations={("夏", "遥"): Relation(sentiment=0.5, r_type="")})
    assert _eval_predicate(
        {"pred": "sentiment", "as": "夏", "target": "遥", "op": "gte", "value": 0.2},
        proj, {}) is True
    assert _eval_predicate(
        {"pred": "co_located", "agents": ["夏", "遥"]}, proj,
        {"夏": "cafe", "遥": "cafe"}) is True
    assert _eval_predicate(
        {"pred": "co_located", "agents": ["夏", "遥"]}, proj,
        {"夏": "cafe"}) is False, "在途/位置未知的人不算在场"


# ── op 能改世界的物质 ──────────────────────────────────────────────────────
#
# op 曾经只能改"她怎么想",改不了"她有什么"。作者写不出"父亲的怀表在这一幕里丢了",
# 只能写一条"她觉得很难过"的记忆去暗示。`pay` / `grant_item` 展开成账本已有的事件
# 类型,余额与库存本来就是它们的投影 —— 不新增 schema,不改 db 格式。

from anima_world.beats import BEAT_WORLD_HOLDER, VALID_OPS, expand_event_op  # noqa: E402
from anima_world.projection import project_events  # noqa: E402
from anima_world.types import Event  # noqa: E402


def _apply(events):
    proj = Projection()
    project_events(
        [Event(seq=i, ts=0, type=e["type"], who=e.get("who"), loc=None,
               payload=e.get("payload") or {})
         for i, e in enumerate(events, 1)],
        base=proj,
    )
    return proj


def test_pay_moves_money_through_the_ledger_projection():
    events = expand_event_op(
        {"op": "pay", "from": "__town__", "to": "夏", "amount": 30, "reason": "遗产"},
        agent_locs={}, known_agents={"夏"},
    )
    proj = _apply(events)
    assert proj.balances["夏"] == 30.0
    assert proj.balances["__town__"] == -30.0, "金库允许负债 —— 与工资同一条规矩"


def test_grant_item_puts_something_in_a_pocket():
    events = expand_event_op(
        {"op": "grant_item", "agent_id": "夏", "item_id": "pocket_watch"},
        agent_locs={}, known_agents={"夏"},
    )
    assert _apply(events).inventories["夏"] == {"pocket_watch": 1}


def test_a_negative_quantity_takes_the_thing_away_instead_of_doing_nothing():
    """投影只认正数 qty,所以负数必须调换两端 —— 否则是一条什么也不做的事件。"""
    proj = _apply(
        expand_event_op({"op": "grant_item", "agent_id": "夏", "item_id": "watch", "qty": 2},
                        agent_locs={}, known_agents={"夏"})
        + expand_event_op({"op": "grant_item", "agent_id": "夏", "item_id": "watch", "qty": -2},
                          agent_locs={}, known_agents={"夏"})
    )
    assert proj.inventories.get("夏", {}) == {}, "怀表应该已经不在她身上了"


def test_a_non_positive_payment_is_refused_rather_than_silently_doing_nothing(caplog):
    """投影对 amount<=0 是 no-op:作者以为钱转了,其实没有。"""
    import logging
    with caplog.at_level(logging.WARNING):
        events = expand_event_op(
            {"op": "pay", "from": "夏", "to": "遥", "amount": -5},
            agent_locs={}, known_agents={"夏", "遥"},
        )
    assert events == []
    assert any("amount" in r.getMessage() for r in caplog.records)


def test_material_ops_refuse_unknown_holders():
    assert expand_event_op(
        {"op": "pay", "from": "查无此人", "to": "夏", "amount": 5},
        agent_locs={}, known_agents={"夏"}) == []
    assert expand_event_op(
        {"op": "grant_item", "agent_id": "查无此人", "item_id": "watch"},
        agent_locs={}, known_agents={"夏"}) == []


def test_the_world_can_be_the_source_of_a_prop():
    """一件道具"本来就在她口袋里"不需要有人先失去它。"""
    events = expand_event_op(
        {"op": "grant_item", "agent_id": "夏", "item_id": "watch", "from": BEAT_WORLD_HOLDER},
        agent_locs={}, known_agents={"夏"},
    )
    assert events and events[0]["payload"]["from"] == BEAT_WORLD_HOLDER


def test_the_new_ops_are_in_the_contract_surface():
    from anima_world.beats import OP_REQUIRED_FIELDS
    assert {"pay", "grant_item"} <= set(VALID_OPS) <= set(OP_REQUIRED_FIELDS)
