"""用户的每条消息都被当成 in-character dialogue —— 那三类里另外两类去哪了(issue #16)。

- "以后叫我霜霜" 是在**改对话规则**,不是在说话:要写进 persona,跨会话跨天不忘
- "让林素也过来" 是在**导演场景**:要真把人挪过来,而不是让她"想象林素在场"

分类往 dialogue 上偏:该 dialogue 判成 narrative,玩家正说的话会被吞掉 —— 那种错
更贵。所以低置信度、参数不全、分类器抽风,一律退回对话并说明原因。
"""
from __future__ import annotations

from _worldfile import open_world_at

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
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
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
    reopened = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    chat = ScriptedLLM("（夏笑了。）霜霜。")
    reopened.chat_service._llm = chat
    with reopened:
        reopened.player_move("p1", _where(reopened, "夏"))
        _say(reopened, "在吗")
        assert "叫他霜霜" in chat.system_prompts[0], "重开之后那条规则没有回到提示词里"


def test_a_style_rule_survives_without_the_flag_once_it_is_written(tmp_path):
    """规则是玩家教的,不是运维点亮的能力:分类器关掉也照样生效。"""
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
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
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
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
        rows = [
            (r.get("role"), r.get("intent"), r.get("intent_confidence"))
            for r in world.chat_store._message_rows(conversation_id)
        ]
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


def test_with_the_flag_off_the_classifier_is_never_called(tmp_path, bare_seed):
    # 素配种子:验的是**引擎默认值**是关的。内置橱窗替世界点亮了 intent
    # (那是产品决定),拿它来验"默认关不关"是在验橱窗的布置(见 conftest)。
    world = open_world_at(str(tmp_path / "w.db"), seed_path=bare_seed, force_mock_llm=True)
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


# ── 自报家门:引擎自己认得,不经分类器 ──────────────────────────────────────

@pytest.mark.parametrize("said, want", [
    ("我叫林越", {"player_name": "林越"}),
    ("你好，我叫林越。很高兴认识你", {"player_name": "林越"}),
    ("我的名字是林越", {"player_name": "林越"}),
    ("我就叫林越了", {"player_name": "林越"}),
    ("my name is Lin", {"player_name": "Lin"}),
    ("你可以叫我小林", {"address_form": "小林"}),
    ("叫我老板就行", {"address_form": "老板"}),
    ("你叫我「小林」吧", {"address_form": "小林"}),
    ("call me Lin", {"address_form": "Lin"}),
    ("我叫林越，你叫我小林就行", {"player_name": "林越", "address_form": "小林"}),
])
def test_他自报家门那几句引擎自己认得(said, want):
    """不交给分类器,三条理由:分类器默认不跑(`chat.intent.enabled` 默认关)而身份块
    每个世界都在;判成 style_adjust 会把他这一轮的话整个吞掉;而且它是一次 LLM 往返,
    有权抽风 —— 和 `SECOND_PERSON` 同一条纪律(引擎手上认得的东西不许依赖它)。"""
    from anima_world.intent import read_self_introduction

    assert read_self_introduction(said) == want


@pytest.mark.parametrize("said", [
    "我叫他小林",          # 我怎么称呼别人
    "我叫苏晚夏做一杯咖啡",  # 同上,而且没人叫这个名字
    "我叫了一杯咖啡",       # 「叫」的另一个动词义
    "我今天叫了外卖",
    "我叫你一声哥",
    "别叫我先生",          # 反过来的意思
    "我不叫小林",
    "你叫我什么？",         # 他在问,不是在报
    "我叫什么",
    "我是来喝咖啡的",       # 「我是X」太松,一律不认
    "外面雨好大啊",
])
def test_不是自报家门的一律空手而归(said):
    """**空手比记错强**:记错的那个会进身份块、进转录、进她 0.8 重要度的长期记忆,
    而玩家永远不会知道是哪一句让她这么叫他的(`night-tide` 那条链的教训)。"""
    from anima_world.intent import read_self_introduction

    assert read_self_introduction(said) == {}


def test_分类器开着时自报家门也不许换成一张收条(tmp_path):
    """`test_报名字不吃掉他这一轮的话` 的**真世界版**:那一条跑在分类器关掉的世界里,
    于是它验的只有正则那一层 —— 而内置的橱窗世界 `chat.intent.enabled` 是**开着的**。

    真世界(`finalD`)重演逮到的:「我叫林越，你叫我小林就行」判成 `style_adjust(0.95)`,
    她回了一句「（记下了:玩家的昵称 —— 小林。）」。名字确实记下了(`_note_self_introduction`
    早一步就落了库),但**他开口说的第一句话一个字都没得到回应** —— 自我介绍换来一张
    系统收条,比不记还伤。分类器给的是同一件事的第二个答案,而第二个答案在这里不是重复,
    是吞掉。
    """
    world, chat, _ = _world(
        tmp_path,
        _classification("style_adjust", 0.95, kind="address_form", value="小林"),
        replies=("（夏抬起头。）林越。记住了。",),
    )
    reply = _say(world, "我叫林越，你叫我小林就行")

    assert "记下了" not in reply, "分类器把他的自我介绍换成了一张收条"
    assert reply.strip() and chat.prompts, "这一轮她根本没被叫醒 —— 话被吞了"

    kinds = {r["kind"]: r["value"] for r in world.persona_overrides("夏", "p1")}
    assert kinds.get("player_name") == "林越"
    assert kinds.get("address_form") == "小林"


def test_自报家门里夹着的导演指令照旧兑现(tmp_path):
    """闸只挡 `style_adjust` 那一支,不是"这句话里有名字就绕开分类器"。

    「我叫林越，让遥也过来」两件事都要发生:名字记下、人真的挪过来。整句跳过分类的
    写法会让后半句安静地不生效 —— 世界照跑、日志干净,而玩家等的人永远不来。
    """
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        target = _someone_elsewhere(world, "夏")
        world.chat_service._llm = ScriptedLLM()
        world.chat_service._background_llm = ScriptedLLM(
            _classification("narrative_direction", 0.95, target=target, action="come_here")
        )
        world.config_set("chat.intent.enabled", True)
        world.player_move("p1", _where(world, "夏"))

        here = _where(world, "夏")
        _say(world, f"我叫林越，让{target}也过来")

        kinds = {r["kind"]: r["value"] for r in world.persona_overrides("夏", "p1")}
        assert kinds.get("player_name") == "林越", "前半句没记下"
        trip = world.scheduler._transit.get(target)
        assert (trip and trip["to"] == here) or _where(world, target) == here, "后半句没兑现"
