"""熬夜有代价 —— 而它**整个住在数据里**,引擎一个字都不知道什么叫「睡眠债」。

这一条本来最容易写成"在 `needs.settle()` 里加一条曲线",而那样一来每个世界都得吃
同一条:熬夜在一个修真世界里可能根本不是代价。所以走的是既有的那条路 ——
**声明一个量 + 写一条规律**,引擎只提供两样它自己没有的东西:

1. **规律读得到时间**(`day`/`hour`/`minute`/`minute_of_day`)。这个洞此前把整类
   昼夜规律挡在门外:手上只有 `now`(单调 tick)和 `dt`,而 `now % 288` 这种手算
   解决不了 —— 一天多少 tick 是每个世界自己的配置。
2. **规律选得中"没在做某件事的人"**(`for_each.not_action`)。"半夜还醒着"在这个
   引擎里的写法只能是"没在睡"。

外加一个消费方:`needs.mood_penalty_stock` 让**世界自己声明的**一笔债把心气儿拖
下去。少了它,睡眠债就是又一个"声明过但没人读"的量 —— 那正是这几轮在治的病。
"""
from __future__ import annotations

import pytest
from _worldfile import open_world_at

from anima_world import needs as needs_mod
from anima_world.expressions import compile_expression
from anima_world.ontology import OntologyError, parse_kinds
from anima_world.rules import BUILTIN_NAMES, RuleError, parse_rules
from anima_world.stocks import evaluate_due
from anima_world.world_time import clock_names


# ── ① 规律读得到时间 ───────────────────────────────────────────────────────


def test_the_calendar_names_are_builtins():
    for name in ("day", "hour", "minute", "minute_of_day"):
        assert name in BUILTIN_NAMES, f"{name} 不是内置名,写它的规律会被当成读了没声明的量"


def test_clock_names_follow_this_worlds_minutes_per_tick():
    """**不是 `now % 288`。** 一天多少 tick 取决于 `world.minutes_per_tick`,
    那是每个世界自己的配置 —— 写死一个数等于让「半夜」在两个世界里指不同的时刻。"""
    assert clock_names(0) == {"day": 0, "hour": 0, "minute": 0, "minute_of_day": 0}
    # 5 分钟/tick(默认):288 tick = 一天
    assert clock_names(288)["day"] == 1
    assert clock_names(288 // 2)["hour"] == 12
    # 1 分钟/tick:同一个 tick 数落在完全不同的时刻
    assert clock_names(288, minutes_per_tick=1)["hour"] == 4
    assert clock_names(90, minutes_per_tick=10)["hour"] == 15


class _Store:
    """够 `evaluate_due` 用的最小存量后端。"""

    def __init__(self, data: dict[str, dict[str, float]]) -> None:
        self.data = {o: dict(v) for o, v in data.items()}
        self.tick = {o: {k: 0 for k in v} for o, v in data.items()}

    def of(self, owner):
        return dict(self.data.get(owner, {}))

    def _snap(self, owner):
        return {k: (v, self.tick[owner][k]) for k, v in self.data.get(owner, {}).items()}

    def snapshot_kind(self, kind):
        return {o: self._snap(o) for o in self.data if o.split(":", 1)[0] == kind}

    def snapshot_many(self, owners):
        return {o: self._snap(o) for o in owners if o in self.data}

    def write_round(self, pending, tick):
        for owner, values in pending.items():
            self.data.setdefault(owner, {}).update(values)
            for key in values:
                self.tick.setdefault(owner, {})[key] = tick
        return sum(len(v) for v in pending.values())


def _run(store, rules, now, *, last_run=None, action_owners=None, minutes_per_tick=5):
    return evaluate_due(
        store, rules, now,
        last_run=last_run if last_run is not None else {},
        action_owners=action_owners,
        minutes_per_tick=minutes_per_tick,
    )


def test_a_rule_can_branch_on_the_hour():
    """这是那个洞本身:此前这条规律写不出来。"""
    rules = parse_rules([{
        "id": "夜里长",
        "every": {"ticks": 1},
        "for_each": {"kind": "tree"},
        "set": {"树高": "树高 + (1 if (hour >= 23 or hour < 6) else 0)"},
    }])
    store = _Store({"tree:a": {"树高": 0.0}})

    _run(store, rules, 12 * 12)              # 12:00 —— 白天
    assert store.data["tree:a"]["树高"] == 0.0

    _run(store, rules, 2 * 12)               # 02:00 —— 夜里
    assert store.data["tree:a"]["树高"] == 1.0


def test_the_hour_a_rule_sees_respects_minutes_per_tick():
    rules = parse_rules([{
        "id": "记下钟点", "every": {"ticks": 1},
        "for_each": {"kind": "tree"}, "set": {"树高": "hour"},
    }])
    store = _Store({"tree:a": {"树高": 0.0}})
    _run(store, rules, 288, minutes_per_tick=5)
    assert store.data["tree:a"]["树高"] == 0.0, "5 分钟/tick 时 288 tick 是第二天 0 点"
    _run(store, rules, 288, minutes_per_tick=1, last_run={})
    assert store.data["tree:a"]["树高"] == 4.0, "1 分钟/tick 时同一个 tick 是 04:48"


def test_a_quantity_may_not_be_named_after_a_builtin():
    """内置名**盖过**同名的量,所以声明一个叫 `hour` 的量是个静默的陷阱:
    量照存、规律照跑、日志干净,只有算出来的数是钟点。当场拒。"""
    with pytest.raises(OntologyError) as caught:
        parse_kinds([{"id": "钟", "quantities": {"hour": 1.0}}])
    assert any("内置名" in e for e in caught.value.errors), caught.value.errors

    # 换个名字就正常 —— 这道闸拦的是撞车,不是"量名不许是英文"。
    assert "钟" in parse_kinds([{"id": "钟", "quantities": {"钟点": 1.0}}])


# ── ② 选得中"没在做某件事的人" ─────────────────────────────────────────────


def test_not_action_is_the_complement_of_action():
    rules = parse_rules([{
        "id": "醒着的", "every": {"ticks": 1},
        "for_each": {"not_action": "sleep"}, "set": {"体力": "体力 - 1"},
    }])
    store = _Store({"agent:夏": {"体力": 10.0}, "agent:遥": {"体力": 10.0},
                    "agent:柔": {"体力": 10.0}})
    _run(store, rules, 5, action_owners=lambda kind: ["agent:遥"] if kind == "sleep" else [])
    assert store.data["agent:夏"]["体力"] == 9.0
    assert store.data["agent:柔"]["体力"] == 9.0
    assert store.data["agent:遥"]["体力"] == 10.0, "睡着的人被算进了「醒着的」"


def test_an_agent_with_no_current_action_counts_as_not_doing_it():
    """刚出生、还没跑过一轮的角色确实**没在睡** —— 漏掉她的话,新角色会
    安静地免疫掉所有 `not_action` 规律。"""
    rules = parse_rules([{
        "id": "醒着的", "every": {"ticks": 1},
        "for_each": {"not_action": "sleep"}, "set": {"体力": "体力 - 1"},
    }])
    store = _Store({"agent:新": {"体力": 10.0}})
    _run(store, rules, 5, action_owners=lambda kind: [])
    assert store.data["agent:新"]["体力"] == 9.0


def test_action_and_not_action_never_write_the_same_owner_in_one_round():
    """这一条是 `not_action` 存在的**理由**。

    没有补集时,「攒债」只能写成 `{"kind": "agent"}`,于是它和「还债」那条
    `{"action": "sleep"}` 会在同一轮里抢同一个量 —— 后写的赢(引擎会 warning,
    但值已经错了),而两条 `every` 不同的话它们只是**有时**相撞,错得断断续续。
    """
    rules = parse_rules([
        {"id": "攒", "every": {"ticks": 1}, "for_each": {"not_action": "sleep"},
         "set": {"债": "债 + 1"}},
        {"id": "还", "every": {"ticks": 1}, "for_each": {"action": "sleep"},
         "set": {"债": "max(债 - 1, 0)"}},
    ])
    store = _Store({"agent:夏": {"债": 5.0}, "agent:遥": {"债": 5.0}})
    report = _run(store, rules, 3,
                  action_owners=lambda kind: ["agent:遥"] if kind == "sleep" else [])
    assert store.data["agent:夏"]["债"] == 6.0
    assert store.data["agent:遥"]["债"] == 4.0
    assert report["written"] == 2, "同一个 owner 被写了两次 —— 补集没起到隔离作用"


def test_an_unknown_selector_is_still_refused():
    with pytest.raises(RuleError):
        parse_rules([{"id": "x", "for_each": {"not_a_thing": "sleep"},
                      "set": {"a": "a + 1"}}])


# ── ③ 消费方:债把心气儿拖下去 ─────────────────────────────────────────────


def test_drag_mood_subtracts_and_clamps():
    assert needs_mod.drag_mood(0.8, 0.3) == pytest.approx(0.5)
    assert needs_mod.drag_mood(0.8, 0.0) == pytest.approx(0.8)
    assert needs_mod.drag_mood(0.2, 0.9) == 0.0, "拖到底就是 0,不许是负数"
    assert needs_mod.drag_mood(0.8, 5.0) == 0.0, "债写漏了上限也不该把 mood 顶穿"
    assert needs_mod.drag_mood(0.8, -1.0) == pytest.approx(0.8), "负债不许反过来加心气儿"
    assert needs_mod.drag_mood(0.8, None) == pytest.approx(0.8), "读不出来就当没有"


def test_the_penalty_is_off_unless_the_world_declares_one(open_world, bare_seed):
    """**声明本身就是开关。** 没配那个键的世界这一层一次都不会被调到。

    ⚠️ 用 `bare_seed`(把橱窗剥回毛坯):演示世界**点亮了**这个键,拿它去验引擎
    默认值等于在验橱窗的布置。
    """
    world = open_world(world_file=bare_seed)
    assert world.config_get("needs.mood_penalty_stock") == "", "引擎默认值该是空的"
    world.config_set("needs.enabled", True)
    world.set_stocks("agent:夏", {"睡眠债": 0.9})
    world.tick(2)
    mood = world.scheduler.agents["夏"].agent.blackboard.read("need.mood")
    assert mood > 0.3, f"关着的开关还是把心气儿拖下去了:{mood}"


def test_the_debt_drags_her_mood_and_she_can_feel_it(tmp_path):
    """两个消费方一起验:**mood**(她连着说几句、想不想起人都读它)与
    **她自己的感知**(声明成 `self`,所以那个数字真的进她读到的提示词)。

    少了这一半,睡眠债就是又一个"声明过但没人读"的量。
    """
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        assert world.config_get("needs.mood_penalty_stock") == "睡眠债", "橱窗没点亮它"
        world.tick(2)
        rested = world.scheduler.agents["夏"].agent.blackboard.read("need.mood")

        world.set_stocks("agent:夏", {"睡眠债": 0.5})
        world.tick(1)
        tired = world.scheduler.agents["夏"].agent.blackboard.read("need.mood")
        assert tired == pytest.approx(rested - 0.5, abs=0.05), (
            f"欠了一夜的觉,心气儿一点没动({rested} → {tired})"
        )

        world.player_move("p1", "cafe")
        blocks = {b["label"]: b["text"] for b in world.debug_prompt("夏", player_id="p1")["blocks"]}
        assert "睡眠债" in (blocks.get("perception") or ""), (
            "她自己感觉不到 —— 那她起床气从哪儿来"
        )


def test_a_grumpy_character_says_less(tmp_path):
    """mood 的下游之一:连着说几句的预算(#17)。**同一个人**,只有心气儿不同。"""
    from anima_world.chat_service import compute_budget

    rested = compute_budget(personality="开朗热情", mood=0.7, relation={"r_type": "朋友"})
    grumpy = compute_budget(personality="开朗热情", mood=0.15, relation={"r_type": "朋友"})
    assert grumpy["budget"] < rested["budget"], "困成那样了还一样话多"
    assert any("心情" in r for r in grumpy["reasons"]), grumpy["reasons"]


# ── ④ 橱窗里真的展示了它 ───────────────────────────────────────────────────


def test_the_flagship_world_actually_accrues_a_debt_overnight(tmp_path):
    """做了却开箱看不见等于没做。判据是**世界跑一夜之后账上真有数**。"""
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        peak = {aid: 0.0 for aid in world.scheduler.agents}
        # 跑到第二天早上:半夜谁醒着谁欠觉,睡下了再还回去。
        while world.scheduler.clock < 400:
            world.tick(1)
            for aid in peak:
                peak[aid] = max(peak[aid], float(world.stock(f"agent:{aid}", "睡眠债") or 0.0))
        assert any(v > 0 for v in peak.values()), (
            f"跑了一整夜,没有一个人欠过觉 —— 这条规律没在动:{peak}"
        )
        assert all(v <= 1.0 for v in peak.values()), f"债没有上界:{peak}"


def test_the_flagship_rules_parse_and_are_the_two_we_meant(tmp_path):
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        rules = {r["id"]: r for r in world.rules()}
        assert "熬夜攒睡眠债" in rules and "补觉还债" in rules
        assert rules["熬夜攒睡眠债"]["for_each"] == {"not_action": "sleep"}
        assert rules["补觉还债"]["for_each"] == {"action": "sleep"}
        # 攒债那条**不带 `when`**,而这是有意的:`when` 会让它在白天完全不写,
        # 于是 `dt` 一路攒到几百 tick,23:00 那一下把债一次顶满。
        assert not rules["熬夜攒睡眠债"].get("when"), (
            "加了 when 的话 dt 会攒到几百,夜里第一次求值就把债顶满"
        )
        assert "hour" in compile_expression(
            rules["熬夜攒睡眠债"]["set"]["睡眠债"]
        ).names, "这条规律根本没读时间"
