"""用户的每条消息都被当成 in-character dialogue —— 那三类里另外两类去哪了(issue #16)。

- "以后叫我霜霜" 是在**改对话规则**,不是在说话:要写进 persona,跨会话跨天不忘
- "让林素也过来" 是在**导演场景**:要真把人挪过来,而不是让她"想象林素在场"

分类往 dialogue 上偏:该 dialogue 判成 narrative,玩家正说的话会被吞掉 —— 那种错
更贵。所以低置信度、参数不全、分类器抽风,一律退回对话并说明原因。
"""
from __future__ import annotations

import json

import pytest

from anima_world.api import World


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


def _classification(intent: str, confidence: float, **params: object) -> str:
    return json.dumps(
        {"intent": intent, "confidence": confidence, "params": params},
        ensure_ascii=False,
    )


def _world(tmp_path, *classifications: str, replies: tuple[str, ...] = ()) -> tuple:
    world = World.open(str(tmp_path / "w.db"), force_mock_llm=True)
    chat = ScriptedLLM(*replies)
    classifier = ScriptedLLM(*classifications)
    world.chat_service._llm = chat
    world.chat_service._background_llm = classifier
    world.config_set("chat.intent.enabled", True)
    world.player_move("p1", _where(world, "夏"))
    return world, chat, classifier


def _where(world: World, agent_id: str) -> str:
    brain = world.scheduler.agents[agent_id]
    return brain.agent.blackboard.read("loc") or brain.agent.location


def _say(world: World, text: str, meta: dict | None = None) -> str:
    return world.chat_reply(
        "夏", [{"role": "user", "content": text}],
        player_id="p1", display_name="阿檀", meta=meta,
    )


def test_a_style_rule_is_remembered_across_sessions(tmp_path):
    """"以后叫我霜霜" 的核心是"以后" —— 应一两轮就忘正是要修的病。"""
    world, chat, _ = _world(
        tmp_path,
        _classification("style_adjust", 0.9, kind="address_form", value="叫他霜霜"),
        replies=("（夏点头。）好。",),
    )
    with world:
        meta: dict = {}
        reply = _say(world, "以后叫我霜霜", meta)

        assert meta["handled_by"] == "style_adjust"
        assert "霜霜" in reply and "记下了" in reply
        assert [r["value"] for r in world.persona_overrides("夏", "p1")] == ["叫他霜霜"]
        # 这一句是"规则已记下",不是她在说话 —— 所以根本没走生成。
        assert chat.prompts == []

    # 换一个进程重开同一个世界:规则还在,而且真的进了提示词。
    reopened = World.open(str(tmp_path / "w.db"), force_mock_llm=True)
    chat = ScriptedLLM("（夏笑了。）霜霜。")
    reopened.chat_service._llm = chat
    with reopened:
        reopened.player_move("p1", _where(reopened, "夏"))
        _say(reopened, "在吗")
        assert "叫他霜霜" in chat.system_prompts[0], "重开之后那条规则没有回到提示词里"


def test_a_style_rule_survives_without_the_flag_once_it_is_written(tmp_path):
    """规则是玩家教的,不是运维点亮的能力:分类器关掉也照样生效。"""
    world = World.open(str(tmp_path / "w.db"), force_mock_llm=True)
    chat = ScriptedLLM("（夏抬头。）嗯。")
    world.chat_service._llm = chat
    with world:
        world.set_persona_override("夏", "p1", "description_style", "别用括号写动作")
        world.player_move("p1", _where(world, "夏"))
        _say(world, "在吗")
        assert "别用括号写动作" in chat.system_prompts[0]


def _someone_elsewhere(world: World, speaker: str) -> str:
    """找一个此刻不在说话人身边的角色。轮询而不是写死 tick —— 世界逐次不确定。"""
    for _ in range(60):
        here = _where(world, speaker)
        for agent_id in world.scheduler.agents:
            if agent_id != speaker and _where(world, agent_id) != here:
                return agent_id
        world.tick(1)
    raise AssertionError("这个世界里所有人一直挤在同一个地方,没法验导演")


def test_directing_a_character_moves_them_in_the_world(tmp_path):
    """narrative 不进提示词,进世界 —— 否则又是"她想象里的林素"。

    断言紧跟在导演那一句之后,中间不走 tick:世界自己也会让人走动,一个"过了几百
    tick 之后他们碰上了"的断言分不出是导演生效了还是他们本来就要碰上。
    """
    world = World.open(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        target = _someone_elsewhere(world, "夏")
        chat = ScriptedLLM()
        world.chat_service._llm = chat
        world.chat_service._background_llm = ScriptedLLM(
            _classification("narrative_direction", 0.95, target=target, action="come_here")
        )
        world.config_set("chat.intent.enabled", True)
        world.player_move("p1", _where(world, "夏"))

        here = _where(world, "夏")
        meta: dict = {}
        reply = _say(world, f"让{target}也过来", meta)

        assert meta["handled_by"] == "narrative_direction"
        assert "过来了" in reply
        # 要么已经在路上,要么已经落地 —— 两条都是"世界里真的动了"。
        trip = world.scheduler._transit.get(target)
        assert (trip and trip["to"] == here) or _where(world, target) == here, "人没真的过来"
        assert chat.prompts == [], "narrative 不该顺手再生成一段 in-character"


def test_directing_someone_who_does_not_exist_is_refused_with_a_next_step(tmp_path):
    """v1 只对已存在的角色动手。纯拒绝会让人以为这条路坏了 —— 要给下一步。"""
    world, _, _ = _world(
        tmp_path,
        _classification("narrative_direction", 0.95, target="林素", action="come_here"),
    )
    with world:
        before = dict(world.state()["agents"])
        meta: dict = {}
        reply = _say(world, "让林素进来", meta)

        assert "我不认识林素" in reply
        assert "造出来" in reply, "拒绝要指出下一步(自然语言造人是 v2)"
        assert meta["intent_detail"]["reason"] == "unknown_agent"
        assert dict(world.state()["agents"]).keys() == before.keys()


def test_a_low_confidence_classification_falls_back_to_dialogue_and_says_why(tmp_path):
    world, chat, _ = _world(
        tmp_path,
        _classification("narrative_direction", 0.2, target="遥", action="come_here"),
        replies=("（夏擦杯子。）你说谁?",),
    )
    with world:
        meta: dict = {}
        reply = _say(world, "让遥过来吧", meta)

        assert meta["intent"] == "dialogue"
        assert "意图不明" in meta["intent_reason"]
        assert "你说谁" in reply, "退回对话就要真的走生成"


def test_a_broken_classifier_never_swallows_the_players_message(tmp_path):
    """分类器抽风时,最坏的结果是"这句按对话处理",不是"这句没了"。"""
    world, chat, _ = _world(
        tmp_path, "我觉得他大概是在导演吧,不太确定",
        replies=("（夏笑。）嗯?",),
    )
    with world:
        meta: dict = {}
        reply = _say(world, "让遥过来", meta)

        assert meta["intent"] == "dialogue" and meta["intent_reason"]
        assert "嗯" in reply


def test_a_style_adjust_missing_its_parameters_degrades_to_dialogue(tmp_path):
    world, chat, _ = _world(
        tmp_path, _classification("style_adjust", 0.99),
        replies=("（夏点头。）好。",),
    )
    with world:
        meta: dict = {}
        _say(world, "以后语气软一点", meta)

        assert meta["intent"] == "dialogue"
        assert "kind/value" in meta["intent_reason"]
        assert world.persona_overrides("夏", "p1") == []


def test_the_intent_lands_on_the_user_message_row_not_on_the_reply(tmp_path):
    """意图是对**那条消息**的判定。挂错行,运维台上的 tag 就挂在错的气泡上。"""
    world, chat, _ = _world(
        tmp_path, _classification("dialogue", 0.9), replies=("（夏抬头。）在。",),
    )
    with world:
        meta: dict = {}
        reply = _say(world, "在吗", meta)
        conversation_id = world.record_chat_turn(
            "夏", "p1",
            [{"role": "user", "content": "在吗"},
             {"role": "assistant", "content": reply}],
            meta=meta,
        )
        rows = world.scheduler.event_log.conn.execute(
            "SELECT role, intent, intent_confidence FROM messages "
            "WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        assert rows[0][0] == "user" and rows[0][1] == "dialogue"
        assert rows[0][2] == pytest.approx(0.9)
        assert rows[1][1] is None, "assistant 那行不该带意图"

        closing = [
            event for event in world.events()
            if event["type"] == "conversation"
            and event["payload"]["conversation_id"] == conversation_id
        ]
        assert closing[0]["payload"]["intents"] == {"dialogue": 1}


def test_the_classifier_runs_on_the_burst_path_too(tmp_path):
    """两条路上的分派必须是同一份。

    只在 `chat()` 上分类的话,点亮 `chat.intent.enabled` 的世界只要也点亮了
    `chat.loop.enabled`,玩家教的"以后叫我霜霜"就永远不落库 —— 而表面上一切照跑。
    这条是装上 wheel 演一遍用户故事时发现的。
    """
    world, chat, _ = _world(
        tmp_path,
        _classification("style_adjust", 0.95, kind="address_form", value="叫他霜霜"),
    )
    with world:
        world.config_set("chat.loop.enabled", True)
        steps = list(world.chat_burst(
            "夏", [{"role": "user", "content": "以后叫我霜霜"}],
            player_id="p1", display_name="阿檀",
        ))

        messages = [step for step in steps if step["kind"] == "message"]
        assert messages and "霜霜" in messages[0]["text"]
        assert steps[-1]["reason"] == "handled_by_intent"
        assert [r["value"] for r in world.persona_overrides("夏", "p1")] == ["叫他霜霜"]
        assert chat.prompts == [], "已经处理掉的这条不该再走一遍 in-character 生成"
        tagged = next(step for step in steps if step["kind"] == "intent")
        assert tagged["intent"] == "style_adjust" and tagged["handled"] is True


def test_the_burst_path_reports_the_classification_even_when_it_changes_nothing(tmp_path):
    """判过了就得让宿主看得见("这条为什么被那样处理"是 #16 的要求之一)。

    分类结果只写进一个内部 dict 然后丢掉的话,运维台上那个 tag 永远是空的 ——
    分类了,但没人知道。
    """
    world, chat, _ = _world(
        tmp_path, _classification("dialogue", 0.88), replies=("（夏抬头。）在。",),
    )
    with world:
        world.config_set("chat.loop.enabled", True)
        steps = list(world.chat_burst(
            "夏", [{"role": "user", "content": "在吗"}],
            player_id="p1", display_name="阿檀",
        ))
        tagged = next(step for step in steps if step["kind"] == "intent")
        assert tagged["intent"] == "dialogue"
        assert tagged["confidence"] == pytest.approx(0.88)
        assert tagged["handled"] is False
        assert [s["text"] for s in steps if s["kind"] == "message"], "对话那条路照旧要生成"


def test_with_the_flag_off_the_classifier_is_never_called(tmp_path):
    world = World.open(str(tmp_path / "w.db"), force_mock_llm=True)
    chat = ScriptedLLM("（夏抬头。）嗯。")
    classifier = ScriptedLLM(_classification("style_adjust", 0.99, kind="address_form", value="霜霜"))
    world.chat_service._llm = chat
    world.chat_service._background_llm = classifier
    with world:
        world.player_move("p1", _where(world, "夏"))
        meta: dict = {}
        _say(world, "以后叫我霜霜", meta)

        assert classifier.prompts == [], "开关关着还去调分类器 = 白花一次 LLM"
        assert "intent" not in meta
        assert world.persona_overrides("夏", "p1") == []
