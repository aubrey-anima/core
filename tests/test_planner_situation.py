"""规划器看得见多少世界。

它此前只知道四件事:我是谁、有哪些空窗、能做什么、记得什么。看不见自己站在哪、饿不
饿、有没有钱、别人这会儿在忙什么 —— 于是它排出来的一天,依据比世界实际拥有的信息
少得多:让一个在家的人"继续在咖啡店待着",让身无分文的人去买东西,让两个人各自
计划去找对方。

处境块与 `goals` 同一个套路:渲染成一个自洽的块(没内容就是空串),模板不必写 if
分支;老模板没有 `{situation}` 占位符也照样能渲染(`str.format` 忽略多余 kwarg)。
"""
from __future__ import annotations

from _worldfile import open_world_at

import pytest

from anima_world.api import World
from anima_world.planner import _DEFAULT_PROMPT, Planner


def _planner(situation=None) -> Planner:
    return Planner(
        llm=None, bt_store=None, location_store=None,
        persona_provider=lambda aid: {"name": aid, "personality": ""},
        agent_ids=lambda: ["夏"],
        duty_windows=lambda aid: [],
        situation_provider=situation,
    )


def test_no_provider_means_an_empty_block_not_a_crash():
    assert _planner()._situation_block("夏") == ""


def test_a_failing_provider_degrades_to_planning_blind():
    """规划永远不该死于一次世界读。"""
    def _explode(_):
        raise RuntimeError("db 炸了")

    assert _planner(_explode)._situation_block("夏") == ""


def test_the_block_reads_like_something_a_person_would_say():
    block = _planner(lambda _: {
        "location": "咖啡店",
        "needs": {"hunger": 0.12, "energy": 0.9},
        "money": 42.0,
        "others": ["陆知遥在workshop上班"],
    })._situation_block("夏")

    assert "咖啡店" in block
    assert "饱腹 12%" in block and "精力 90%" in block, block
    assert "42" in block
    assert "陆知遥" in block
    assert block.endswith("\n\n"), "块要自洽,模板里不该再补空行"


def test_an_empty_situation_renders_as_nothing():
    assert _planner(lambda _: {})._situation_block("夏") == ""


def test_the_default_prompt_uses_the_block():
    assert "{situation}" in _DEFAULT_PROMPT


def test_an_old_template_without_the_placeholder_still_renders():
    """老库里存着的模板没有 {situation} —— 不能因此让规划整个失效。"""
    old = "你是{name}。第 {day} 天。空窗:{free_windows} 能做:{action_space} {memories}{goals}"
    rendered = old.format(
        name="夏", personality="", day=1, free_windows="-", action_space="-",
        memories="", goals="", situation="你此刻的处境：\n- 你现在在咖啡店\n\n",
    )
    assert "夏" in rendered


def test_a_live_world_fills_the_block_in(tmp_path, bare_seed):
    """接线检查:真世界里位置、需求、别人在忙什么都得读得出来。

    素配种子:这条里还有一句"经济没点亮就不该谈钱",而内置橱窗是点亮了经济的。
    """
    from anima_world.__main__ import _planner_situation

    with open_world_at(str(tmp_path / "w.db"), seed_path=bare_seed, force_mock_llm=True) as world:
        world.config_set("needs.enabled", "true")
        world.tick(200)
        ctx = _planner_situation(world.scheduler, "夏")

        assert ctx.get("location")
        assert set(ctx.get("needs", {})) == {"energy", "hunger", "social"}
        assert ctx.get("others"), "别人在做什么应该读得出来"
        assert "money" not in ctx, "经济没点亮就不该谈钱"


def test_money_shows_up_only_when_the_economy_is_on(tmp_path):
    from anima_world.__main__ import _planner_situation

    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.config_set("economy.enabled", "true")
        world.tick(5)
        assert "money" in _planner_situation(world.scheduler, "夏")


def test_the_prompt_says_how_late_a_step_may_start():
    """空窗的末端印作 `24:00`,而 `24:00` 不是一个能起头的时刻。

    真跑一轮的样子:模型照着窗口写出 `start_min` 1440 / 1500 / 1530,
    `validate_steps` 一条条丢掉,只在日志里留一行 "starts outside the day"。
    世界照跑、计划照落,只是她傍晚那几步没了 —— 而不会有人去看那行日志。
    丢弃那道闸必须留着(LLM 说的东西不许不经核对就进世界),这里钉的是
    **它得先被告知范围**。
    """
    from anima_world.planner import MINUTES_PER_DAY, validate_steps

    assert str(MINUTES_PER_DAY - 1) in _DEFAULT_PROMPT, (
        "提示词没说 start_min 的上界,模型只能照着 24:00 猜"
    )
    # 闸还在:说了范围不代表就信它。
    assert validate_steps(
        [{"kind": "walk", "start_min": MINUTES_PER_DAY, "params": {"location": "cafe"}}],
        {"walk": {"location": ["cafe"]}},
    ) == ()
