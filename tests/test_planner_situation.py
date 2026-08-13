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


def test_别人在哪儿也是人话_不是键名(tmp_path, bare_seed):
    """线上晚潮的规划提示词里逐字躺着:「林迟在studio上班;苏念在cart上班」。

    名字翻了、地点没翻 —— 半句人话半句键名,而这一句是她排一天事情的依据。
    上面那条 `test_a_live_world_fills_the_block_in` 盯不住它:它只问了
    `ctx["others"]` 是不是非空。
    """
    from anima_world.__main__ import _planner_situation

    with open_world_at(str(tmp_path / "w.db"), seed_path=bare_seed,
                       force_mock_llm=True) as world:
        world.tick(200)
        place_ids = {str(row["id"]) for row in world.scheduler.location_store.all()}
        names = {
            pid: world.scheduler.place_name(pid) for pid in place_ids
        }
        assert any(n != pid for pid, n in names.items()), \
            "这个种子的地点得有跟 id 不一样的名字,否则这条验不出东西"

        others = _planner_situation(world.scheduler, "夏").get("others") or []
        assert others, "别人在做什么应该读得出来"
        for line in others:
            for pid, name in names.items():
                if name == pid:
                    continue   # 没起名字的地点,翻不翻都是那几个字
                assert pid not in line, f"规划提示词里漏了地点键名 {pid!r}:{line!r}"


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


def test_能做的事那份清单也说人话(tmp_path, bare_seed):
    """处境块翻成人话只是半个修法,而半个修法比不修更难看。

    线上真提示词里,上半句是「江晚在江堤闲着」,下半句的菜单是
    `walk（location 可选：alley/barber/…/levee/…）` —— 两半之间没有任何桥,
    她得自己猜哪个 id 是江堤。抓到的现场是她把 id 当人话用了:一条计划的
    `note` 里写着「早点到studio等她」,而 note 是给人读的那半句。

    问的是**她真收到的那份提示词**,不是 `_format_space` 单独拿出来渲染一次 ——
    单独问它的话,`_build_messages` 忘了把名册传下去这一整类漏法一条都验不出来
    (第一版就是这么写的,把修法抹掉它照样绿)。
    """
    with open_world_at(str(tmp_path / "w.db"), seed_path=bare_seed,
                       force_mock_llm=True) as world:
        scheduler = world.scheduler
        planner = Planner(
            llm=None,
            bt_store=scheduler.bt_store,
            location_store=scheduler.location_store,
            persona_provider=lambda aid: {
                "name": scheduler.agent_display_name(aid), "personality": ""
            },
            agent_ids=lambda: list(scheduler.agents),
            duty_windows=lambda aid: [],
        )
        windows, space = planner.plan_inputs("夏")
        prompt = planner._build_messages("夏", 1, windows, space)[-1]["content"]

        named = {
            pid: scheduler.place_name(pid)
            for pid in (str(r["id"]) for r in scheduler.location_store.all())
        }
        assert any(n != pid for pid, n in named.items()), \
            "这个种子的地点得有跟 id 不一样的名字,否则这条验不出东西"
        checked = 0
        for pid, name in named.items():
            if name == pid or pid not in space.get("walk", {}).get("location", []):
                continue
            checked += 1
            assert f"{name}({pid})" in prompt, \
                f"清单上的 {pid!r} 没带名字,她读到的只有键名:{prompt}"
        assert checked, "接线断了:清单里一个有名字的地点都没有"

        for other in scheduler.agents:
            if other == "夏" or other not in space.get("chat", {}).get("target", []):
                continue
            assert f"{scheduler.agent_display_name(other)}({other})" in prompt, \
                f"能跟谁说话那份清单还是裸 id:{prompt}"


def test_空窗那一行自己带着分钟数(tmp_path, bare_seed):
    """上半句印钟点、下半句要分钟数,而两半之间没有桥。

    线上抓到的现场:`yu` 的末窗印作 `23:30 到 24:00`,她写回 `start_min: 2330` ——
    就是我印给她的那四个数字去掉冒号。三步全被判成"今天以外"丢掉,她傍晚安静地
    空了。和动作清单那条同一个教训,所以修法也同一个:把桥印出来。

    问的是**她真收到的那份提示词**,不是 `_format_windows` 单独渲染一次 ——
    上一条的教训是接线漏了单测也照绿。
    """
    with open_world_at(str(tmp_path / "w.db"), seed_path=bare_seed,
                       force_mock_llm=True) as world:
        scheduler = world.scheduler
        planner = Planner(
            llm=None,
            bt_store=scheduler.bt_store,
            location_store=scheduler.location_store,
            persona_provider=lambda aid: {"name": aid, "personality": ""},
            agent_ids=lambda: list(scheduler.agents),
            duty_windows=lambda aid: [(0, 600)],
        )
        windows, space = planner.plan_inputs("夏")
        assert windows, "这条要有空窗才验得出东西"
        prompt = planner._build_messages("夏", 1, windows, space)[-1]["content"]

        for start, end in windows:
            assert f"{start} 到 {end - 1}" in prompt, \
                f"空窗 {start}-{end} 那行没带分钟数,她只能照着钟点猜:{prompt}"


def test_她把字段写成_start_也算数():
    """线上 867 步里 9 步写的是 `start` —— 而 `bai` 三步全这么写,一整天没了计划。

    字段叫什么只是形状;闸(在不在今天以内)一个字没动。
    """
    from anima_world.planner import validate_steps

    space = {"walk": {"location": ["levee"]}}
    steps = validate_steps(
        [{"kind": "walk", "start": 600, "params": {"location": "levee"}}], space
    )
    assert [s.start_min for s in steps] == [600], "她只是把字段名写短了一截"

    assert validate_steps(
        [{"kind": "walk", "start": 2330, "params": {"location": "levee"}}], space
    ) == (), "换个字段名不该把'今天以内'那道闸也一起绕过去"


def test_她照抄整串也算数_而清单外的照旧丢掉():
    """菜单改成「名字(id)」之后模型照抄整串是常态。

    收下它**不是把闸放松了**:判据从头到尾还是"这个 id 在不在我给她的清单里"。
    丢掉一条的代价是她那几步安静地没了 —— 和 `start_min` 上界那条同一个教训。
    """
    from anima_world.planner import validate_steps

    space = {"walk": {"location": ["levee"]}}
    steps = validate_steps(
        [{"kind": "walk", "start_min": 600, "params": {"location": "江堤(levee)"}}], space
    )
    assert [s.params["location"] for s in steps] == ["levee"], \
        "她照抄了我自己印给她的那个写法,而它被当成了世界里没有的地方"

    assert validate_steps(
        [{"kind": "walk", "start_min": 600, "params": {"location": "月球(moon)"}}], space
    ) == (), "括号里是什么都收的话,这道闸就没了"


def test_她把那个地方写在哪一格都算数():
    """线上 13 步这么写:提到了 `params` 外面、或者换了个字段名叫 `target`。

    `start` / `start_min` 那条的续 —— 四种写法说的是同一件事,而只认第一种的话
    后三种整步被扔掉。扔掉一步 = 她那个钟点安静地没事做,玩家看到的是一个站着
    不动的人,那是这个产品里最要命的一种沉默。
    """
    from anima_world.planner import validate_steps

    space = {"walk": {"location": ["studio"]}}
    for step in (
        {"kind": "walk", "start_min": 600, "location": "studio", "params": {}},
        {"kind": "walk", "start_min": 600, "target": "studio"},
        {"kind": "walk", "start_min": 600, "params": {"target": "studio"}},
        {"kind": "walk", "start_min": 600, "params": {"location": "回声(studio)"}},
    ):
        steps = validate_steps([step], space)
        assert [s.params["location"] for s in steps] == ["studio"], f"这一种没收下:{step}"


def test_放宽的只是她把答案放在哪儿_闸一个字没动():
    """编出来的地方照旧当场丢掉 —— 不管她把它写在哪一格。"""
    from anima_world.planner import validate_steps

    space = {"walk": {"location": ["studio"]}}
    for step in (
        {"kind": "walk", "start_min": 600, "params": {"location": "哈尔滨"}},
        {"kind": "walk", "start_min": 600, "location": "哈尔滨", "params": {}},
        {"kind": "walk", "start_min": 600, "params": {"target": "哈尔滨"}},
        {"kind": "walk", "start_min": 600, "params": {}},
    ):
        assert validate_steps([step], space) == (), f"这一种漏进来了:{step}"


def test_两格参数的时候不猜():
    """一个动作有两格参数时,「哪个值该填哪一格」没有答案。

    猜错了不报错:猜出来的计划和她说的是两件事,她照着走一天,没有任何一处会发现。
    所以换名字那一种只在**只有一格**的时候认。
    """
    from anima_world.planner import validate_steps

    space = {"meet": {"location": ["studio"], "target": ["wan"]}}
    assert validate_steps(
        [{"kind": "meet", "start_min": 600, "params": {"a": "studio", "b": "wan"}}], space
    ) == (), "两格的时候按值去认,等于替她把计划编了一份"

    steps = validate_steps(
        [{"kind": "meet", "start_min": 600, "location": "studio", "target": "wan"}], space
    )
    assert len(steps) == 1, "按名字认的那两种和参数有几格无关"
