"""她真有可以选择的行动(issue #15)。

dogfooding 观察到的"假"不在提示词,在**结构**:100% 响应率、零主动、说"我走了"也
没真走。这里每一条都盯**世界里真发生了什么**,而不是她嘴上说了什么 —— 一个声明过
却没人兑现的能力,比没有更坏。
"""
from __future__ import annotations

from _worldfile import open_world_at

import pytest

from anima_world.api import AgentUnavailable, World

A_DAY = 288


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


def _world(tmp_path, *replies: str, tools: bool = True) -> tuple[World, ScriptedLLM]:
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    llm = ScriptedLLM(*replies)
    world.chat_service._llm = llm
    world.config_set("chat.tools.enabled", tools)
    world.player_move("p1", _where(world, "夏"))
    return world, llm


def _where(world: World, agent_id: str) -> str:
    brain = world.scheduler.agents[agent_id]
    return brain.agent.blackboard.read("loc") or brain.agent.location


def _say(world: World, text: str, meta: dict | None = None) -> str:
    return world.chat_reply(
        "夏", [{"role": "user", "content": text}],
        player_id="p1", display_name="阿檀", meta=meta,
    )


def test_the_tool_menu_lists_every_registered_capability(tmp_path):
    world, llm = _world(tmp_path, "好。")
    with world:
        _say(world, "在吗")
        menu = llm.system_prompts[0]
        for tool_id in ("mute", "end_conversation", "delay_reply", "walk_away",
                        "refuse_topic", "broadcast", "wait_for_user"):
            assert tool_id in menu, f"{tool_id} 声明了却没进菜单 —— 她看不见就等于没有"
        assert {t["id"] for t in world.tools()} >= {"mute", "walk_away"}


def test_a_mute_actually_stops_the_next_message(tmp_path):
    """软静音:玩家还能发,世界当场拒。抛异常而不是回一句空话 ——
    空回复在宿主那边和"LLM 挂了"长得一模一样。"""
    world, _ = _world(tmp_path, "〔tool:mute {\"minutes\": 5, \"reason\": \"越界\"}〕")
    with world:
        meta: dict = {}
        _say(world, "（越界的话）", meta)

        assert meta["tool_calls"][0]["tool"] == "mute"
        assert meta["tool_calls"][0]["ok"] is True
        assert world.is_muted("夏", "p1")["kind"] == "mute"

        with pytest.raises(AgentUnavailable) as raised:
            _say(world, "别这样")
        assert raised.value.kind == "mute"
        assert raised.value.seconds_left > 0

        # 宿主要能把它显示成"她五分钟后再理你" —— 所以静音要在事件流里。
        started = [
            event for event in world.events()
            if event["type"] == "state_change"
            and (event["payload"] or {}).get("kind") == "mute_started"
        ]
        assert started and started[0]["payload"]["target"] == "p1"

        world.unmute("夏", "p1")
        assert world.is_muted("夏", "p1") is None


def test_walking_away_moves_her_in_the_world_not_in_the_prose(tmp_path):
    """"我走了"必须让她真的不在这儿了 —— 这条是整个 issue 的核心。"""
    world, _ = _world(tmp_path, "（夏站起来。）我先走了。〔tool:walk_away〕")
    with world:
        before = _where(world, "夏")
        meta: dict = {}
        reply = _say(world, "你到底想不想聊", meta)

        assert "我先走了" in reply
        call = meta["tool_calls"][0]
        assert call["tool"] == "walk_away" and call["ok"] is True
        destination = call["detail"]["to"]
        assert destination != before

        # 要么已经在路上(travel 事件),要么已经落地 —— 两条都算真的动了。
        in_transit = "夏" in world.scheduler._transit
        assert in_transit or _where(world, "夏") == destination
        if in_transit:
            # **只推到她走完那一刻。** 再往后推推的是排班:她本来就有一段在店里的
            # 勤务,下一段勤务会把她叫回去 —— 那是她的作息,不是"她没走"。推一整天
            # 之后再断言,验到的是"一天结束时她在哪",而这条要验的是她说走真的起了
            # 程、而且真的走到了。世界从几点开始如今是作者的意见,两者更不该搅在一起。
            for _ in range(A_DAY):
                world.tick(1)
                if "夏" not in world.scheduler._transit:
                    break
            assert "夏" not in world.scheduler._transit, "整整一天她都没走到"
            assert _where(world, "夏") == destination


def test_delay_reply_comes_back_on_its_own(tmp_path):
    """"等会儿再说"要真的回来 —— 只落一行托辞的话,玩家永远在等。"""
    world, _ = _world(tmp_path, "〔tool:delay_reply {\"minutes\": 30, \"reason\": \"手上有活\"}〕")
    with world:
        meta: dict = {}
        _say(world, "现在方便吗", meta)

        detail = meta["tool_calls"][0]["detail"]
        assert world.followups("夏")[0]["due_tick"] == detail["due_tick"]
        assert world.is_muted("夏", "p1")["kind"] == "delay"

        # 到点之前她不理人,到点之后她自己回来敲门。
        world.player_move("p1", _where(world, "夏"))
        for _ in range(4):
            world.tick(max(1, detail["due_tick"] - world.scheduler.clock))
            if world.inbox("p1"):
                break
        hails = [
            event for event in world.inbox("p1")
            if event["payload"].get("reason") == "delayed_reply"
        ]
        assert hails, "到点了她也没回来 —— 那句「等会儿」就是空话"
        assert hails[0]["payload"]["note"] == "手上有活"
        assert not world.followups("夏"), "兑现过的约不该留在队列里"
        assert world.is_muted("夏", "p1") is None, "回来了就该重新理人"


def test_nobody_comes_back_to_a_player_who_left(tmp_path):
    """给不在的人写一场对话是这个仓库最在意的那类错。"""
    world, _ = _world(tmp_path, "〔tool:delay_reply {\"minutes\": 10}〕")
    with world:
        meta: dict = {}
        _say(world, "等会儿聊?", meta)
        world.player_leave("p1")

        world.tick(A_DAY)
        assert world.inbox("p1") == []
        assert world.followups("夏"), "人不在场,那条约该留着等他回来"


def test_an_overdue_promise_expires_instead_of_haunting_the_player(tmp_path):
    """也不能永远留着:下周登录收到上周那句"等我一下"同样是错的。"""
    world, _ = _world(tmp_path, "〔tool:delay_reply {\"minutes\": 10}〕")
    with world:
        _say(world, "等会儿聊?")
        world.player_leave("p1")

        world.tick(A_DAY * 2)
        assert not world.followups("夏"), "过期一个世界日该作罢"
        assert world.inbox("p1") == []


def test_a_refused_topic_comes_back_as_pressure_not_as_silence(tmp_path):
    """拦下来玩家只看到沉默;她自己表明态度才是那个角色该有的反应。"""
    world, llm = _world(
        tmp_path,
        "〔tool:refuse_topic {\"keyword\": \"彩票\"}〕这事别再提。",
        "（夏皱眉。）我说了不谈这个。",
    )
    with world:
        _say(world, "买彩票的事")
        assert [row["keyword"] for row in world.refused_topics("夏")] == ["彩票"]

        _say(world, "彩票中了吗")
        assert "彩票" in llm.system_prompts[1]
        assert "岔开话题" in llm.system_prompts[1]


def test_a_broadcast_is_actually_heard_by_the_people_standing_there(tmp_path):
    """当众说一句话,**在场的人真的会记住** —— 而不只是日志里多一行。

    这条测试原来只断言 `world.broadcasts()` 里有这条(名字还叫"whole world 可见"),
    而那正是没被兑现的那一半:`agent_broadcast` 事件当时**没有任何角色消费**,于是她
    "当众宣布"的后果是世界里谁也不知道,菜单却告诉她"世界里的人都能看到"。
    CLAUDE.md 的硬不变量是**她的选择必须在世界里兑现**,这条补上了它。
    """
    world, _ = _world(tmp_path, "〔tool:broadcast {\"text\": \"店里今天不开门\"}〕")
    with world:
        speaker = "夏"
        here = world._tool_runtime.agent_location(speaker)
        listener = next(
            a for a in sorted(world.scheduler.agents) if a != speaker
        )
        world._tool_runtime.move_agent(listener, here)
        for _ in range(60):  # move_agent 是起程,不是瞬移:在途的人不在任何地方
            world.tick(1)
            if world._tool_runtime.agent_location(listener) == here:
                break
        assert world._tool_runtime.agent_location(listener) == here, "没走到,这条测试没在验它想验的"
        before = {m.get("summary") for m in world.memories(listener)}

        _say(world, "今天营业吗")

        published = world.broadcasts()
        assert published and published[0]["payload"]["text"] == "店里今天不开门"
        assert published[0]["payload"]["heard_by"] == [listener]

        heard = [
            m.get("summary") for m in world.memories(listener)
            if m.get("summary") not in before
        ]
        assert any("店里今天不开门" in (s or "") for s in heard), (
            f"在场的人没记住这句话 —— 广播又变成一行没人读的日志:{heard}"
        )
        # 而且是**能被检索到的**记忆,不是只躺在库里的一行。
        # 不断言它必然出现在下一句提示词里:检索是三因子竞争的 top-K,一条
        # importance 0.45 的见闻在无关话题上挤不进去 —— 那是记忆系统该有的样子。
        recalled = world.retrieve_memories(listener, query="店里 开门")
        assert any("店里今天不开门" in (m.get("summary") or "") for m in recalled), (
            f"存进去了却检索不到,等于没进他的脑子:{[m.get('summary') for m in recalled]}"
        )


def test_a_broadcast_does_not_reach_people_somewhere_else(tmp_path):
    """一句喊话不传遍地图 —— "客观存在 = 人人皆知"正是 §2.9.4 立规矩要防的错。"""
    world, _ = _world(tmp_path, "〔tool:broadcast {\"text\": \"店里今天不开门\"}〕")
    with world:
        here = world._tool_runtime.agent_location("夏")
        elsewhere = [
            a for a in sorted(world.scheduler.agents)
            if a != "夏" and world._tool_runtime.agent_location(a) != here
        ]
        assert elsewhere, "所有人都在同一个地方,这条测试验不到"
        before = {a: {m.get("summary") for m in world.memories(a)} for a in elsewhere}

        _say(world, "今天营业吗")

        assert world.broadcasts()[0]["payload"]["heard_by"] == []
        for agent_id in elsewhere:
            fresh = [
                m.get("summary") for m in world.memories(agent_id)
                if m.get("summary") not in before[agent_id]
            ]
            assert not any("店里今天不开门" in (s or "") for s in fresh), (
                f"{agent_id} 不在场却听见了"
            )


def test_end_conversation_closes_the_session_but_does_not_mute(tmp_path):
    world, _ = _world(tmp_path, "到这儿吧。〔tool:end_conversation〕")
    with world:
        meta: dict = {}
        _say(world, "再多说点", meta)

        assert meta["end_conversation"] is True
        assert world.is_muted("夏", "p1") is None, "结束对话不等于屏蔽"


def test_with_the_flag_off_a_tool_call_is_ignored_but_never_silently(tmp_path):
    """开关关着时她不该看到菜单;万一还是编了一个调用,不许静默丢掉 ——
    "她说要走却没走"和"她没想走"在产物上一模一样。"""
    world, llm = _world(tmp_path, "我走了。〔tool:walk_away〕", tools=False)
    with world:
        before = _where(world, "夏")
        meta: dict = {}
        reply = _say(world, "你走吧", meta)

        assert "walk_away" not in llm.system_prompts[0], "关着还给菜单等于在骗模型"
        assert meta["ignored_tool_calls"] == ["walk_away"]
        assert "tool_calls" not in meta
        assert _where(world, "夏") == before and "夏" not in world.scheduler._transit
        assert "〔" not in reply


def test_an_unknown_tool_is_recorded_as_a_failed_call(tmp_path):
    world, _ = _world(tmp_path, "〔tool:teleport {\"to\": \"月球\"}〕")
    with world:
        meta: dict = {}
        _say(world, "你能瞬移吗", meta)
        call = meta["tool_calls"][0]
        assert call["ok"] is False and "teleport" in call["error"]


def test_a_bad_parameter_is_refused_with_a_reason(tmp_path):
    world, _ = _world(tmp_path, "〔tool:mute {\"minutes\": -5}〕")
    with world:
        meta: dict = {}
        _say(world, "……", meta)
        call = meta["tool_calls"][0]
        assert call["ok"] is False and "minutes" in call["error"]
        assert world.is_muted("夏", "p1") is None


def test_an_over_long_mute_is_capped_and_says_so(tmp_path, caplog):
    """静默截断是错的:她说"一个月别理我"而世界只执行一天,那是两回事。"""
    world, _ = _world(tmp_path, "〔tool:mute {\"minutes\": 100000}〕")
    with world:
        world.config_set("chat.tools.max_mute_minutes", 60.0)
        with caplog.at_level("WARNING"):
            meta: dict = {}
            _say(world, "……", meta)
        assert meta["tool_calls"][0]["detail"]["minutes"] == 60.0
        assert any("上限" in record.getMessage() for record in caplog.records)


def test_walking_away_from_a_phone_call_hangs_up_instead_of_a_pointless_trip(tmp_path):
    """对一个不在你面前的人"走开"是个空动作。

    真模型跑出来的样子:她在咖啡店被骂,walk_away 走去工作室 —— 对的。但玩家还在
    咖啡店,下一句照旧发得到(手机私聊),于是她又走开一次、再一次,连着四趟行程,
    而每一趟在世界里都是真事件。在场的语义得由引擎守住。
    """
    world, _ = _world(tmp_path, "（夏站起来。）不聊了。〔tool:walk_away〕")
    with world:
        world.player_leave("p1")           # 玩家不在场了 = 手机私聊
        before = _where(world, "夏")
        meta: dict = {}
        _say(world, "你到底聊不聊", meta)

        call = meta["tool_calls"][0]
        assert call["ok"] is True and call["end_conversation"] is True
        assert call["detail"]["degraded_to"] == "end_conversation"
        assert _where(world, "夏") == before, "隔着手机不该真的起一趟行程"
        assert "夏" not in world.scheduler._transit


def test_a_tool_only_step_does_not_erase_the_stance_she_just_declared(tmp_path):
    """她声明了 provoke、摔门走人,紧接着那一步只有一个 tool_call、没有台词 ——
    兜底的 neutral 不许把"她刚跟你翻脸"改写成"她对你很平淡"。"""
    world, _ = _world(tmp_path, "〔stance:provoke〕（夏冷笑。）你算什么东西。",
                      "〔tool:end_conversation〕")
    with world:
        world.config_set("chat.stance.enabled", True)
        world.config_set("chat.loop.enabled", True)
        list(world.chat_burst("夏", [{"role": "user", "content": "你就是个端咖啡的"}],
                              player_id="p1", display_name="阿檀"))

        stored = world.stance("夏", "p1")
        assert stored["stance"] == "provoke" and stored["declared"] is True
