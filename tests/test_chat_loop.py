"""连着说到她自己想停(issue #17)。

玩家的原话:"我发一句她回一大段然后停,我又要发才有反应,像 completion 不像聊天。"
这里盯的是四类停下信号都真的存在、预算真的按性格/时间变、以及**硬上限兜得住** ——
一个不肯让位的模型不该让一次聊天无限跑下去。
"""
from __future__ import annotations

import pytest

from anima_world.api import World
from anima_world.chat_service import compute_budget, personality_traits


class ScriptedLLM:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[list[dict]] = []

    def _next(self, messages) -> str:
        self.prompts.append([dict(m) for m in messages])
        return self.replies.pop(0) if self.replies else ""

    async def stream(self, messages):
        text = self._next(messages)
        for index in range(0, len(text), 5):
            yield text[index : index + 5]

    async def complete(self, messages) -> str:
        return self._next(messages)

    @property
    def system_prompts(self) -> list[str]:
        return [
            "\n".join(m["content"] for m in prompt if m["role"] == "system")
            for prompt in self.prompts
        ]


def _world(tmp_path, *replies: str, loop: bool = True) -> tuple[World, ScriptedLLM]:
    world = World.open(str(tmp_path / "w.db"), force_mock_llm=True)
    llm = ScriptedLLM(*replies)
    world.chat_service._llm = llm
    world.config_set("chat.loop.enabled", loop)
    world.player_move("p1", "cafe")
    return world, llm


def _burst(world: World, text: str, **kwargs) -> list[dict]:
    return list(world.chat_burst(
        "夏", [{"role": "user", "content": text}],
        player_id="p1", display_name="阿檀", **kwargs,
    ))


def _messages(steps: list[dict]) -> list[str]:
    return [step["text"] for step in steps if step["kind"] == "message"]


def _stop(steps: list[dict]) -> dict:
    return steps[-1]


def _daytime(world: World, agent_id: str = "夏") -> None:
    """把世界推到白天。演示世界从 day 0 的 00:00 起跑,而深夜的预算是 1~2 句 ——
    在深夜验"连着说三句"验的其实是预算,不是让位。轮询到白天,不写死 tick 数。"""
    for _ in range(400):
        presence = (world.world_context(agent_id, "p1") or {}).get("presence") or {}
        if 8 <= int(presence.get("hh", 0)) <= 20:
            return
        world.tick(1)
    raise AssertionError("推不到白天")


# ── 预算 ────────────────────────────────────────────────────────────────────


def test_a_talkative_character_gets_a_bigger_budget_than_a_shy_one():
    chatty = compute_budget(personality="话痨,开朗热情", relation={"r_type": "老友"})
    quiet = compute_budget(personality="内向寡言,怕生", relation={"r_type": "老友"})
    assert chatty["budget"] > quiet["budget"]
    assert quiet["budget"] >= 1, "预算不许算到 0 —— 那等于她再也不说话"


def test_the_budget_explains_itself():
    """一个说不出理由的预算没法调参。"""
    plan = compute_budget(personality="内向", hour=23)
    assert "深夜 -2" in plan["reasons"] and "初次见面 -1" in plan["reasons"]
    assert "基准 3" in plan["reasons"][0]


def test_traits_are_read_from_the_personality_text_deterministically():
    """v1 从描述文本抽,不调 LLM:没有 key 也算得出来,而且跑两次一样。"""
    assert personality_traits("话痨") == personality_traits("话痨")
    assert personality_traits("")["talkative"] == 0.5, "看不出来就是 0.5,不猜"
    assert personality_traits("高冷")["shy"] > personality_traits("高冷")["talkative"]


def test_a_late_night_conversation_is_shorter_than_a_daytime_one():
    day = compute_budget(personality="开朗", hour=14)
    night = compute_budget(personality="开朗", hour=2)
    assert night["budget"] < day["budget"]


# ── 四类停下信号 ────────────────────────────────────────────────────────────


def test_she_keeps_going_until_she_yields(tmp_path):
    world, _ = _world(
        tmp_path,
        "你听我说。",
        "我今天见了她。",
        "结果她居然当我不在。〔wait〕",
    )
    with world:
        _daytime(world)
        steps = _burst(world, "怎么了")
        assert _messages(steps) == ["你听我说。", "我今天见了她。", "结果她居然当我不在。"]
        assert _stop(steps)["reason"] == "explicit_yield"
        assert _stop(steps)["messages"] == 3


def test_a_question_hands_the_turn_back_without_being_told_to(tmp_path):
    """隐式让位:问了对方一句,就是把话头递过去了。"""
    world, _ = _world(tmp_path, "我一直想问你一件事。", "你当时到底怎么想的?", "不该说的话。")
    with world:
        _daytime(world)  # 预算要宽于 2,否则停下的其实是预算不是让位
        steps = _burst(world, "说吧")
        assert _messages(steps) == ["我一直想问你一件事。", "你当时到底怎么想的?"]
        assert _stop(steps)["reason"] == "implicit_yield"


def test_the_budget_is_a_ceiling_even_if_she_never_yields(tmp_path):
    """模型不肯让位时,预算就是那道闸 —— 否则一次聊天能跑到天亮。"""
    # 每句都不同:重复那道闸是另一条测试的事,这里要验的是预算本身
    world, llm = _world(tmp_path, *[f"还有第 {i} 件事。" for i in range(20)])
    with world:
        steps = _burst(world, "接着说")
        budget = next(step for step in steps if step["kind"] == "budget")
        assert _stop(steps)["reason"] == "budget"
        assert len(_messages(steps)) == budget["effective"] <= 8


def test_the_hard_cap_wins_over_a_misconfigured_budget(tmp_path):
    world, _ = _world(tmp_path, *[f"还有第 {i} 件事。" for i in range(20)])
    with world:
        world.config_set("chat.loop.max_messages", 2)
        steps = _burst(world, "接着说")
        assert len(_messages(steps)) <= 2


def test_a_tool_that_ends_the_conversation_stops_the_loop(tmp_path):
    world, _ = _world(tmp_path, "我不想再说了。〔tool:walk_away〕", "还有话。")
    with world:
        world.config_set("chat.tools.enabled", True)
        steps = _burst(world, "你到底怎么了")
        assert _stop(steps)["reason"] == "end_conversation"
        assert len(_messages(steps)) == 1
        calls = [step for step in steps if step["kind"] == "tool_call"]
        assert calls[0]["tool"] == "walk_away"


def test_with_the_loop_off_it_is_exactly_one_step(tmp_path):
    """开关关着时形状不变(宿主不用写两套消费代码),但只跑一步。"""
    world, _ = _world(tmp_path, "第一句。", "第二句。", loop=False)
    with world:
        steps = _burst(world, "说吧")
        assert _messages(steps) == ["第一句。"]
        assert next(s for s in steps if s["kind"] == "budget")["effective"] == 1


def test_saying_the_same_thing_again_stops_the_loop(tmp_path):
    """又把说过的话说一遍 = 在原地绕圈,不是"还有话说"。

    没有这道闸,一个绕圈的模型会把同一段刷到预算耗尽 —— 玩家读到的是五条几乎一样
    的消息。没有 key 时(Mock 就是模板回声)一眼可见。
    """
    world, _ = _world(tmp_path, "我今天真的很不好过。", "我今天真的很不好过。", "还有别的事。")
    with world:
        _daytime(world)
        steps = _burst(world, "说吧")
        assert _messages(steps) == ["我今天真的很不好过。"]
        assert _stop(steps)["reason"] == "repeated_step"


def test_a_reworded_repeat_also_stops_the_loop(tmp_path):
    """真模型给的是**换个说法说同一段**:整句照抄,中间夹一句新的动作描写。

    一字不差的比对放过了它,而玩家读到的仍然是她在绕圈 —— 这一条来自 2026-07-29
    用真模型跑的那一局(第 6 轮)。
    """
    first = "（苏晚夏愣了一下。）霜霜,你今天到底怎么了?你这样突然说,我有点摸不着头脑。"
    reworded = "（苏晚夏把椅子挪近了一点。）你这样突然说,我有点摸不着头脑。霜霜,你今天到底怎么了?"
    world, _ = _world(tmp_path, first, reworded, "第三句。")
    with world:
        _daytime(world)
        steps = _burst(world, "说吧")
        assert _messages(steps) == [first]
        assert _stop(steps)["reason"] == "repeated_step"


def test_a_step_that_moves_forward_is_not_mistaken_for_a_repeat(tmp_path):
    """反过来也要成立:真的往下推进的第二句不许被当成复读掐掉。"""
    world, _ = _world(
        tmp_path,
        "（苏晚夏放下抹布。）你来得正好,我刚煮了一壶。",
        "（苏晚夏推过来一只杯子。）昨天那批豆子到了,我留了一点给你。〔wait〕",
    )
    with world:
        _daytime(world)
        steps = _burst(world, "在吗")
        assert len(_messages(steps)) == 2
        assert _stop(steps)["reason"] == "explicit_yield"


def test_an_empty_step_stops_instead_of_spinning(tmp_path):
    world, _ = _world(tmp_path, "", "", "")
    with world:
        steps = _burst(world, "在吗")
        assert _messages(steps) == []
        assert _stop(steps)["reason"] == "empty_step"


# ── 插话 ────────────────────────────────────────────────────────────────────


def test_an_interruption_reaches_her_and_she_decides_what_to_do_with_it(tmp_path):
    """选项 C:插话不是引擎的硬中断,是她要面对的一句话。"""
    world, llm = _world(tmp_path, "我今天很生气。", "反正就是这样。〔wait〕")
    with world:
        _daytime(world)
        pending = ["等等,你先听我说"]
        steps = _burst(world, "怎么了", interrupt_check=lambda: pending.pop(0) if pending else None)

        assert "等等,你先听我说" in llm.system_prompts[1], "她根本没收到那句插话"
        assert len(_messages(steps)) == 2, "插话不该把她的话腰斩"
        assert _stop(steps)["reason"] == "explicit_yield"


def test_stance_rides_along_with_every_step(tmp_path):
    """#18 与本 issue 叠起来:每一步是 (text, tool_call, stance) 的三元组。"""
    world, _ = _world(tmp_path, "〔stance:vent〕我受够了。", "〔stance:vent〕真的。〔wait〕")
    with world:
        world.config_set("chat.stance.enabled", True)
        steps = _burst(world, "你说")
        stances = [step["stance"] for step in steps if step["kind"] == "stance"]
        assert stances == ["vent", "vent"]
        assert world.stance("夏", "p1")["stance"] == "vent"


def test_the_muted_gate_holds_on_the_burst_path_too(tmp_path):
    """两条路上的静音判定必须是同一份 —— 一条守住另一条漏等于没守。"""
    from anima_world.api import AgentUnavailable

    world, _ = _world(tmp_path, "〔tool:mute {\"minutes\": 5}〕")
    with world:
        world.config_set("chat.tools.enabled", True)
        _burst(world, "越界的话")
        assert world.is_muted("夏", "p1") is not None
        with pytest.raises(AgentUnavailable):
            _burst(world, "还在吗")


def test_she_does_not_replay_a_paragraph_from_an_earlier_turn(tmp_path):
    """"不要重复自己"要跨整段对话,不只是这一轮。

    真模型跑出来的样子:第四轮里整段照抄第二轮说过的一段,一字不差。玩家读到的是
    她在同一场对话里把同一段话说了两遍 —— 而只看本轮的查重放过了它。
    """
    earlier = "（苏晚夏往椅子里一坐,翘起二郎腿。）行啊,你让我闭嘴我就闭嘴呗。"
    world, _ = _world(tmp_path, "（苏晚夏别过脸去。）没什么好说的了。", earlier, "第三句。")
    with world:
        _daytime(world)
        steps = list(world.chat_burst(
            "夏",
            [{"role": "user", "content": "你说话"},
             {"role": "assistant", "content": earlier},      # 上一轮她说过这一段
             {"role": "user", "content": "你怎么不说话"}],
            player_id="p1", display_name="阿檀",
        ))
        # 第一句是新的,第二句是上一轮那段的翻版 —— 到那里就该停。
        assert _messages(steps) == ["（苏晚夏别过脸去。）没什么好说的了。"]
        assert _stop(steps)["reason"] == "repeated_step"


def test_the_first_step_is_never_swallowed_even_if_it_repeats(tmp_path):
    """查重不许把一轮变成沉默:第一步照旧交出去。"""
    earlier = "（苏晚夏擦着杯子。）我说过了,这事别再提。"
    world, _ = _world(tmp_path, earlier, "还有别的话。")
    with world:
        _daytime(world)
        steps = list(world.chat_burst(
            "夏",
            [{"role": "assistant", "content": earlier},
             {"role": "user", "content": "那件事呢"}],
            player_id="p1", display_name="阿檀",
        ))
        assert _messages(steps)[0] == earlier, "第一步被吞了 —— 玩家会看到一片沉默"


def test_the_default_no_key_world_does_not_repeat_itself_three_times(tmp_path):
    """没有 key 是**默认状态**,而 Mock 就是模板回声 —— 同一句话刷三遍正是新用户
    看到的第一屏。句子级查重会漏掉它(回声太短),所以整段一字不差也得算。"""
    world = World.open(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        world.config_set("chat.loop.enabled", True)
        world.player_move("p1", "cafe")
        _daytime(world)
        steps = _burst(world, "在吗")
        assert len(_messages(steps)) == 1, _messages(steps)
        assert _stop(steps)["reason"] == "repeated_step"
