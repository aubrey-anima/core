"""「你去哈尔滨」—— 玩家对着正在跟他说话的那个人下的指令。

这一类是玩家嘴里最常见的指令,而在这之前它是**唯一一类被明确挡掉的**:
`Director.direct` 认出 target 就是说话人本人,回一句"(直接跟她说就好。)"并且
`handled=True` —— 玩家那句话连生成都没走,被一句系统提示顶掉了。

三条要一起成立,少一条这层就还是假的:

1. **指令在世界里兑现** —— 真发 travel,真在途,`state.location` 真的变
2. **她同时也开口** —— 而且读得到"你已经动身了"这个事实,所以"一边答应一边走"
   是同一件事的两面,不是提示词里的一句想象
3. **地点是玩家给的参数** —— 不是 `point_ids()[0]`。对不上就拒绝并列出有的是哪些
   地方;对得上好几个也拒绝 —— 随便挑一个的话她真的会走过去,而没有一行日志说这
   不是玩家要的那个地方

外加 `act` 那条:它从前一个写入点、零个读取点,却回"(X 照做了。)"。**什么都没做
却报成功**比不支持更坏,所以它要么真接上消费方,要么老实说。
"""
from __future__ import annotations

import json

from _worldfile import open_world_at

from anima_world.api import World
from anima_world.intent import places_menu, resolve_place


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
        {"intent": intent, "confidence": confidence, "params": params}, ensure_ascii=False
    )


def _where(world: World, agent_id: str) -> str:
    brain = world.scheduler.agents[agent_id]
    return brain.agent.blackboard.read("loc") or brain.agent.location


def _world(tmp_path, *classifications: str, replies: tuple[str, ...] = ()):
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    chat = ScriptedLLM(*replies)
    world.chat_service._llm = chat
    world.chat_service._background_llm = ScriptedLLM(*classifications)
    world.config_set("chat.intent.enabled", True)
    world.player_move("p1", _where(world, "夏"))
    return world, chat


def _say(world: World, text: str, meta: dict | None = None) -> str:
    return world.chat_reply(
        "夏", [{"role": "user", "content": text}],
        player_id="p1", display_name="阿檀", meta=meta,
    )


def _elsewhere(world: World, agent_id: str) -> str:
    here = _where(world, agent_id)
    return next(p for p in world._tool_runtime.point_ids() if p != here)


# ── ② 指挥对话方本人 ────────────────────────────────────────────────────────


def test_directing_the_person_you_are_talking_to_really_moves_her(tmp_path):
    """核心那条:她真的起程,而且**她的话没有被顶掉**。"""
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        target = _elsewhere(world, "夏")
        chat = ScriptedLLM("（夏解下围裙。）好，这就走。")
        world.chat_service._llm = chat
        world.chat_service._background_llm = ScriptedLLM(
            _classification("narrative_direction", 0.95, target="夏", action="go", place=target)
        )
        world.config_set("chat.intent.enabled", True)
        world.player_move("p1", _where(world, "夏"))

        meta: dict = {}
        reply = _say(world, f"你去{target}", meta)

        # 1. 世界真的动了
        trip = world.scheduler._transit.get("夏")
        assert (trip and trip["to"] == target) or _where(world, "夏") == target, (
            "指挥说话人本人,世界里一动没动 —— 这条路又回到只在提示词里答应了"
        )
        # 2. 她自己开的口,不是一句系统确认
        assert chat.prompts, "in-character 生成被跳过了 —— 玩家那句话又被顶掉了"
        assert "这就走" in reply
        assert meta.get("handled_by") is None, "自指的导演不该标成 handled"
        # 3. 她读得到"已经动身了"这个事实
        assert "刚刚发生的事" in chat.system_prompts[0]
        assert "正在路上" in chat.system_prompts[0] or "已经到了" in chat.system_prompts[0]
        assert meta["intent_detail"]["action"] == "go"


def test_the_word_you_resolves_to_the_person_being_spoken_to(tmp_path):
    """真模型第一次实测就撞上的那个:分类器把 target 填成字符串「你」。

    判得完全正确(narrative_direction / confidence 1.0),而 `_resolve("你")` 找不到
    这个人 —— 回一句"(我不认识你。这个世界里现在只有…)",世界一动不动。**提示词
    那一头要教,但引擎不许依赖它**:分类器是一次 LLM 往返,它有权抽风。
    """
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        target = _elsewhere(world, "夏")
        chat = ScriptedLLM("（夏解围裙。）好。")
        world.chat_service._llm = chat
        world.chat_service._background_llm = ScriptedLLM(
            _classification("narrative_direction", 1.0, target="你", action="go", place=target)
        )
        world.config_set("chat.intent.enabled", True)
        world.player_move("p1", _where(world, "夏"))

        meta: dict = {}
        _say(world, f"你去{target}", meta)

        assert meta["intent_detail"].get("reason") != "unknown_agent", "「你」又没认出来"
        assert meta["intent_detail"]["target"] == "夏"
        trip = world.scheduler._transit.get("夏")
        assert (trip and trip["to"] == target) or _where(world, "夏") == target


def test_a_direction_with_no_target_at_all_falls_to_the_speaker(tmp_path):
    """分类器把 target 整个漏了。我们已经在 narrative 分支里,而这句话里能确定的
    只有一个人 —— 正在跟他说话的那个。猜错她走一趟,拒绝则是一句莫名其妙的
    "我不认识这个人"(玩家根本没提过谁)。"""
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        target = _elsewhere(world, "夏")
        world.chat_service._llm = ScriptedLLM("（夏。）嗯。")
        world.chat_service._background_llm = ScriptedLLM(
            _classification("narrative_direction", 0.9, action="go", place=target)
        )
        world.config_set("chat.intent.enabled", True)
        world.player_move("p1", _where(world, "夏"))

        meta: dict = {}
        _say(world, f"去{target}吧", meta)
        assert meta["intent_detail"]["target"] == "夏"


def test_the_classifier_is_told_who_you_means(tmp_path):
    """引擎那道闸是兜底,提示词那一头也要教 —— 只留兜底等于每次都多绕一圈。"""
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        classifier = ScriptedLLM(_classification("dialogue", 0.9))
        world.chat_service._llm = ScriptedLLM("（夏。）嗯。")
        world.chat_service._background_llm = classifier
        world.config_set("chat.intent.enabled", True)
        world.player_move("p1", _where(world, "夏"))
        _say(world, "在吗")

        prompt = classifier.system_prompts[0]
        assert "苏晚夏" in prompt, "分类器不知道玩家嘴里的「你」是谁"
        assert "咖啡店(cafe)" in prompt, "分类器不知道这个世界有哪些地方"
        # 「带我去X」是 together 不是 go —— 菜单上没有的东西它点不出来。
        # 真模型实测:「你能带我过去吗」判成 `go`(0.95),于是**她一个人走了**,
        # 而引擎这一版明明已经能把两个人一起送过去(`_go_together`)。
        # 能力加在引擎里、没加进菜单,等于没加。
        assert "带我去" in prompt, "分类器的菜单上没有「带我去某个地方」这一项"


def test_the_grounding_never_orders_her_to_obey(tmp_path):
    """服从走性格,不走机制 —— 塞进提示词的那句只陈述事实,不下命令。

    写成"你必须照办"就是在判定逻辑外面开了第二道后门:引擎强加的服从。那样一来
    「零」听话和白霜听话就分不出来了,而这一层的全部意义正是那个差别。
    """
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        target = _elsewhere(world, "夏")
        chat = ScriptedLLM("（夏没动。）不去。")
        world.chat_service._llm = chat
        world.chat_service._background_llm = ScriptedLLM(
            _classification("narrative_direction", 0.95, target="夏", action="go", place=target)
        )
        world.config_set("chat.intent.enabled", True)
        world.player_move("p1", _where(world, "夏"))
        _say(world, f"你去{target}")

        grounding = chat.system_prompts[0]
        for order in ("必须照办", "必须答应", "不得拒绝", "你要服从"):
            assert order not in grounding, f"grounding 里出现了命令:{order}"


def test_a_place_the_world_does_not_have_is_refused_and_she_still_answers(tmp_path):
    """回执保留(玩家得知道世界里没有哈尔滨),但**不许再吞掉她的话**。"""
    world, chat = _world(
        tmp_path,
        _classification("narrative_direction", 0.95, target="夏", action="go", place="哈尔滨"),
        replies=("（夏挑眉。）哈尔滨是哪儿？",),
    )
    with world:
        meta: dict = {}
        reply = _say(world, "你去哈尔滨", meta)

        assert "没有 哈尔滨 这个地方" in reply and "有的是" in reply, "那条好回执丢了"
        assert "哈尔滨是哪儿" in reply, "回执把她的话又顶掉了"
        assert meta["intent_detail"]["reason"] == "unknown_place"
        assert world.scheduler._transit.get("夏") is None, "地名对不上却起程了"
        # 她读到的是"没能兑现",不是"你已经在路上了"
        assert "没有" in chat.system_prompts[0] and "没能在世界里兑现" in chat.system_prompts[0]


def test_directing_someone_else_still_works_exactly_as_before(tmp_path):
    """别把「指挥别人」那条路弄坏:照旧一句系统回执,照旧不走 in-character。"""
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        target = next(
            aid for aid in world.scheduler.agents
            if aid != "夏" and _where(world, aid) != _where(world, "夏")
        )
        chat = ScriptedLLM()
        world.chat_service._llm = chat
        world.chat_service._background_llm = ScriptedLLM(
            _classification("narrative_direction", 0.95, target=target, action="come_here")
        )
        world.config_set("chat.intent.enabled", True)
        world.player_move("p1", _where(world, "夏"))
        here = _where(world, "夏")

        meta: dict = {}
        reply = _say(world, f"让{target}过来", meta)

        assert meta["handled_by"] == "narrative_direction"
        assert "过来了" in reply
        assert chat.prompts == [], "指挥别人不该顺手再生成一段 in-character"
        trip = world.scheduler._transit.get(target)
        assert (trip and trip["to"] == here) or _where(world, target) == here


# ── ③ 地点是参数 ───────────────────────────────────────────────────────────


def test_leave_no_longer_silently_picks_the_first_point(tmp_path):
    """玩家说了去哪就去哪 —— `leave` 带着 place 不许再走"排序第一个地点"。"""
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        points = sorted(world._tool_runtime.point_ids())
        here = _where(world, "夏")
        wanted = next(p for p in reversed(points) if p != here)
        assert wanted != points[0], "这个世界里 wanted 恰好就是第一个,这条测试没在验它想验的"

        world.chat_service._llm = ScriptedLLM("（夏。）嗯。")
        world.chat_service._background_llm = ScriptedLLM(
            _classification("narrative_direction", 0.95, target="夏", action="leave", place=wanted)
        )
        world.config_set("chat.intent.enabled", True)
        world.player_move("p1", here)

        meta: dict = {}
        _say(world, f"你走吧，去{wanted}", meta)
        assert meta["intent_detail"]["place"] == wanted
        trip = world.scheduler._transit.get("夏")
        assert (trip and trip["to"] == wanted) or _where(world, "夏") == wanted


def test_a_short_place_name_resolves_to_the_long_one():
    """玩家嘴里的地名永远比世界里的短:「哈尔滨」→「哈尔滨·冰雪大世界」。"""
    points = {
        "harbin-icecity": "哈尔滨·冰雪大世界",
        "hangzhou-westlake": "杭州·西湖断桥",
        "generated-region": "世界区域",
    }
    assert resolve_place("哈尔滨", points)[0] == "harbin-icecity"
    assert resolve_place("哈尔滨·冰雪大世界", points)[0] == "harbin-icecity"
    assert resolve_place("harbin-icecity", points)[0] == "harbin-icecity"
    assert resolve_place("西湖", points)[0] == "hangzhou-westlake"


def test_给玩家的清单说人话_给模型的那份带_id():
    """同一份清单,两个读者,两种写法 —— 而从前只按模型那一种写。

    线上她那一轮的开头是二十个拉丁字母的 id 铺在一个中文世界的第一行
    (`铁匠巷(alley)、剃头铺(barber)、……`)。玩家打的是人话,人话在
    `resolve_place` 第一层就命中了;那些 id 一个字都没告诉他这个世界的事,
    还看着像要他照着打的东西。而同一个函数的成功回执从来只说人话。

    模型那半反过来:`walk` 只收 id,不给它就等于让它接着猜。
    """
    points = {"alley": "铁匠巷", "cart": "小念咖啡车"}
    assert places_menu(points, with_ids=False) == "铁匠巷、小念咖啡车"
    assert places_menu(points) == "铁匠巷(alley)、小念咖啡车(cart)"


def test_重名的那几个照旧带_id_而不是把整份清单打回原形():
    """`小院、小院;说准一点` 是一句没法照着做的回执。

    但重名是少数:一个重名不该让其余十九个也退回 id。
    """
    points = {"yard-n": "小院", "yard-s": "小院", "alley": "铁匠巷"}
    menu = places_menu(points, with_ids=False)
    assert menu == "铁匠巷、小院(yard-n)、小院(yard-s)", menu


def test_没有名字的地方给出_id_而不是空白():
    """名字缺席时 id 就是它在世界里唯一的称呼 —— 印一个空字符串等于把它藏起来。"""
    assert places_menu({"attic": "", "alley": "铁匠巷"}, with_ids=False) == "铁匠巷、attic"


def test_an_ambiguous_place_is_refused_not_guessed():
    """**绝不随便挑一个** —— 挑错了她真的会走过去,而世界里一行日志都不报错。"""
    points = {"north-gate": "北门", "north-market": "北市"}
    where, candidates = resolve_place("北", points)
    assert where is None
    assert candidates == ["north-gate", "north-market"]

    # 而精确命中时,包含匹配不许把它搅成歧义
    assert resolve_place("北门", points)[0] == "north-gate"

    # 压根没有的地方:两种"没解析出来"要分得开 —— 下一步不一样
    assert resolve_place("南门", points) == (None, [])


def test_an_ambiguous_place_says_which_ones(tmp_path):
    world, _ = _world(
        tmp_path,
        _classification("narrative_direction", 0.95, target="夏", action="go", place="工"),
        replies=("（夏。）嗯？",),
    )
    with world:
        meta: dict = {}
        reply = _say(world, "你去工那边", meta)
        detail = meta["intent_detail"]
        # 这个世界里 "工" 只对得上 workshop 一个,所以这条走的是成功那支;
        # 真正的歧义在上面那条纯函数测试里验。这里只钉住"解析用的是名字,不是 id"。
        assert detail.get("place") == "workshop", detail
        assert "建筑工作室" in reply or "建筑工作室" in str(detail.get("place_name"))


# ── ③ act:要么真接上,要么老实说 ──────────────────────────────────────────


def test_act_lands_in_the_targets_memory_instead_of_claiming_success(tmp_path):
    """从前:一个写入点、零个读取点,却回"(X 照做了。)"。

    现在它走 `memory_seed` —— 记忆本来就是这个引擎里"角色知道一件事"的表示,而它
    有真的消费方(聊天提示词的记忆块、planner 排明天那一天)。于是玩家这句话进得了
    他的决定,而回执也只敢说"他知道了",不敢说"他做了"。
    """
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        target = next(aid for aid in world.scheduler.agents if aid != "夏")
        world.chat_service._llm = ScriptedLLM()
        world.chat_service._background_llm = ScriptedLLM(
            _classification("narrative_direction", 0.95,
                            target=target, action="act", detail="把灯关了")
        )
        world.config_set("chat.intent.enabled", True)
        world.player_move("p1", _where(world, "夏"))

        before = len(world.memories(target))
        meta: dict = {}
        reply = _say(world, f"让{target}把灯关了", meta)

        assert "照做了" not in reply, "引擎又在替他答应了"
        assert "知道" in reply
        assert meta["intent_detail"]["delivered_as"] == "memory"
        after = world.memories(target)
        assert len(after) == before + 1, "这条指令没有任何落点 —— 又是零个读取点"
        assert any("把灯关了" in m["summary"] for m in after)
        assert any("阿檀" in m["summary"] for m in after), "记忆里没说是谁要求的"


def test_a_directive_reaches_the_prompt_the_target_actually_reads(tmp_path):
    """"进了记忆"要能验成"进了他的提示词" —— 否则又是一个没有读取点的写入点。"""
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        target = next(aid for aid in world.scheduler.agents if aid != "夏")
        world.chat_service._llm = ScriptedLLM()
        world.chat_service._background_llm = ScriptedLLM(
            _classification("narrative_direction", 0.95,
                            target=target, action="act", detail="去把船票买了")
        )
        world.config_set("chat.intent.enabled", True)
        world.player_move("p1", _where(world, "夏"))
        _say(world, f"让{target}去把船票买了")

        blocks = world.debug_prompt(target, player_id="p1")["blocks"]
        text = "\n".join(b["text"] for b in blocks)
        assert "船票" in text, "指令进了记忆表,却进不了他真正读到的那份提示词"


def test_参数不全的指令退回对话_而不是当面责怪玩家(tmp_path):
    """回执只说**世界的事**;"我没读懂你的话"不是世界的事。

    线上抓到的现场:玩家问「我想去看看你说的那个地方,能带我去吗?路上顺便聊聊」——
    一句再清楚不过的人话。分类器判成 `together`(0.85)却把 `object` 留空
    (`detail` 里倒是写着"带玩家去潮汐里3号,路上聊天"),于是她那一轮的**第一行**是

        (一起做什么?说具体一点 —— 得有个东西。)

    引擎越过她,当面责怪玩家没说清楚。而紧接着她的散文回答好得很,还真答应了下楼——
    那句回执是纯多出来的噪音,并且它一个字都没告诉玩家这个世界的事。

    留下的那些回执**都告诉他世界的事**:世界里没有哈尔滨、你在这头他在那头、
    他手上没有那样东西、果子还没熟。这一条不是,所以它该走 `style_adjust` 少了
    kind/value 时那条早就存在的路:记一行日志,按对话处理。
    """
    world, chat = _world(
        tmp_path,
        _classification("narrative_direction", 0.95, target="遥", action="act"),
        replies=("你想让他干嘛?",),
    )
    with world:
        meta: dict = {}
        reply = _say(world, "让遥做点什么", meta)
        assert "说具体一点" not in reply, f"引擎越过她责怪了玩家:{reply}"
        assert reply == "你想让他干嘛?", \
            f"这一轮该原样走对话,她的话一个字都不该被顶掉或加料:{reply}"
        assert meta.get("intent") == "dialogue", \
            f"少了必填字段的导演指令该退回对话:{meta}"


def test_而世界说的那半照旧要露出来(tmp_path):
    """**这不是把回执整个关掉了。** 分界是"这句话告诉玩家世界的事吗"。

    「你去哈尔滨」—— 世界里没有哈尔滨,玩家必须知道;这一句照旧加在她的话前面。
    抹掉它就等于让玩家对着一个他永远猜不透边界的世界瞎试。
    """
    world, _ = _world(
        tmp_path,
        _classification("narrative_direction", 0.95, target="夏", action="go",
                        place="哈尔滨"),
        replies=("（夏没动。）",),
    )
    with world:
        reply = _say(world, "你去哈尔滨")
        assert "哈尔滨" in reply, f"世界里没有这个地方,得让玩家知道:{reply}"


def test_引擎的回执不会变成她说过的话(tmp_path):
    """**世界的记账不是她的台词。**

    回执是塞在她的回复**前面**流出去的,而宿主原样把整段流回传给
    `record_chat_turn` —— 于是那句话作为**她的**台词落进转录。线上现场:林迟那条
    消息的第一行是

        (咖啡车的雨棚不能被「站」;它能被一起躲会儿雨、端详、补一补)

    下一轮它跟着转录进了邀请判定的提示词、进了会话摘要、再进她的长期记忆;他还
    真照着演了一句「我刚被人纠正过,雨棚不能「站」」—— 一个角色在转述引擎的语法
    纠错。转录是这条链的第一环,所以摘在这儿。

    **摘的是这一轮引擎自己刚发出去的那句原文**,不靠"括号开头"猜形状:那样会把
    她自己写的动作块也删掉。

    ⚠️ **两条路都要验。** 第一版只把闸装在 `record_chat_turn` 上,而真宿主(世界壳)
    **有意绕开那个方法** —— 它是"记一轮并当场关闭",每轮都关会话等于每句话生成一次
    摘要、跑一次关系判定;它直接调 `chat_store.add_message`。于是那一版在线上一次都
    没生效,而单测全绿。闸现在装在两条路的交汇处(落库那一下)。
    """
    world, _ = _world(
        tmp_path,
        _classification("narrative_direction", 0.95, target="夏", action="go",
                        place="哈尔滨"),
        replies=("（夏没动。）她那句话我可听不懂。",),
    )
    with world:
        reply = _say(world, "你去哈尔滨")
        assert "哈尔滨" in reply, "夹具前提没成立:这一轮本该有一句回执"

        # ① 引擎自带的那条门
        world.record_chat_turn("夏", "p1", [
            {"role": "user", "content": "你去哈尔滨"},
            {"role": "assistant", "content": reply},
        ])
        # ② 真宿主那条:直接往开着的会话里追加,不关会话(世界壳的 `_append_chat_turn`)
        import time as _time

        conversation_id = world.chat_store.active_or_start(
            "夏", int(_time.time()), player_id="p1",
        )
        world.chat_store.add_message(conversation_id, "assistant", reply, int(_time.time()))

        for conversation in world.conversations("夏"):
            rows = world.chat_store.recent_messages(int(conversation["id"]), 10)
            for row in rows:
                if row["role"] != "assistant":
                    continue
                said = row["content"]
                assert "哈尔滨" not in said, f"引擎的回执落成了她的台词:{said}"
                # 她自己那半一个字都不能少(动作块开头的名字由 `_ActionNameNormalizer`
                # 补,与这条无关)。
                assert said.endswith("她那句话我可听不懂。"), f"把她自己的话也删掉了:{said}"
                assert said.startswith("（"), f"她的动作块被削掉了:{said}"


def test_指挥别人那条路的回执也不许变成她说过的话(tmp_path):
    """同一道闸上的一个洞:**登记回执的只有一半的路**。

    上面那条验的是"指挥她本人"(`intent_receipt`)—— 那一半一直登记着。而"指挥
    **别人**"走的是另一条:`handled=True` + `text`,整轮回复**就是**那句回执,一个字
    都不是她说的,而它从来没进过 `_remember_receipt`。于是 `_strip_receipt` 认不出它,
    「（遥过来了。）」原样落进转录 —— 和上面那条事故逐字同构,只是换了个入口。

    两个入口共用一道闸而只有一个登记,是这类洞最典型的样子:另一半永远绿。
    """
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        target = next(a for a in world.scheduler.agents if a != "夏")
        chat = ScriptedLLM()
        world.chat_service._llm = chat
        world.chat_service._background_llm = ScriptedLLM(
            _classification("narrative_direction", 0.95, target=target, action="come_here")
        )
        world.config_set("chat.intent.enabled", True)
        world.player_move("p1", _where(world, "夏"))

        meta: dict = {}
        reply = _say(world, f"让{target}也过来", meta)
        assert meta["handled_by"] == "narrative_direction", "夹具前提没成立"
        assert reply.strip(), "夹具前提没成立:这一轮的整段回复就是那句回执"

        # 真宿主那条路:直接往开着的会话里追加(世界壳的 `_append_chat_turn`)
        import time as _time

        conversation_id = world.chat_store.active_or_start(
            "夏", int(_time.time()), player_id="p1",
        )
        world.chat_store.add_message(
            conversation_id, "assistant", reply, int(_time.time()),
        )

        rows = world.chat_store.recent_messages(int(conversation_id), 10)
        said = [r["content"] for r in rows if r["role"] == "assistant"]
        assert said and not said[-1].strip(), f"引擎的记账落成了她的台词:{said[-1]!r}"


def test_一个地名都没说时_没有世界的事可报(tmp_path):
    """`go` 少了 place 是同一件事,漏在了 `empty_object` 旁边。

    线上现场:「带我去个安静点的地方吧,我想跟你说会儿话」—— 玩家问的本来就是
    "你挑一个"。分类器判了 `go` 却把提示词里写着**必填**的 place 留空,于是玩家
    读到的第一行是

        (没有 你说的那个地方 这个地方;有的是 铁匠巷、剃头铺、……、念姐的小院。)

    这句话本身就不通:他一个地名都没说,那就没有"世界里没有它"这回事可报。
    而它还把二十个地名铺在她开口之前 —— 顶掉的正是他真正要的那个答案(她挑一个)。
    """
    world, _ = _world(
        tmp_path,
        _classification("narrative_direction", 0.95, target="夏", action="go"),
        replies=("去我家吧,那儿清静。",),
    )
    with world:
        meta: dict = {}
        reply = _say(world, "带我去个安静点的地方吧", meta)
        assert "没有" not in reply and "有的是" not in reply, \
            f"他一个地名都没说,却收到一句「世界里没有它」:{reply}"
        assert reply == "去我家吧,那儿清静。", \
            f"这一轮该原样走对话:{reply}"
        assert meta.get("intent") == "dialogue", meta


# ── ④ 测试角色「零」──────────────────────────────────────────────────────


def test_zero_is_pure_personality_with_no_engine_backdoor(tmp_path):
    """库主的第二条硬要求:**走性格,不走机制**。

    她的服从必须整个住在 personality 这一个字段里 —— 不加 `obedience` 字段、判定
    逻辑里不为她开分支。这条测试就钉这个:引擎里搜不到她的名字。
    """
    import json as _json
    import pathlib

    script = _json.loads(pathlib.Path("docs/examples/零.beats.json").read_text(encoding="utf-8"))
    agent = script["beats"][0]["payload"][0]["agent"]
    assert set(agent) <= {"id", "name", "location", "personality", "duties", "goals"}, (
        "「零」身上出现了引擎不认识的字段 —— 服从又变成机制了"
    )
    assert not agent["goals"], "一个有自己目标的测试角色不叫言听计从"

    # 她的落脚点必须是**演示世界真有的地方**。这条闸是踩出来的:这份例子最早写的是
    # `harbin-icecity`(线上那个世界的地点),演示世界里没有 —— `agent_join` 照收不误
    # (它不校验地点),于是她站在一个不存在的地方,直到下面那条测试 `player_move`
    # 过去才炸成一句 KeyError。**测试与示例都不许依赖线上世界的数据**:那个世界不在
    # 仓库里,谁也没法照着它跑一遍。
    world = open_world_at(str(tmp_path / "probe.db"), force_mock_llm=True)
    with world:
        points = world._tool_runtime.point_ids()
    assert agent["location"] in points, (
        f"例子里的落脚点 {agent['location']!r} 不在演示世界里(有的是 {sorted(points)})"
    )

    # 引擎里既不许按她的 id 分支,也不许有一个"服从度"字段。搜的是**代码里的字面量**
    # (`"零"` / `'零'`),不是注释里那个当量词用的"零" —— 后者满仓库都是。
    engine = pathlib.Path("anima_world")
    hits = sorted(
        path.name for path in engine.rglob("*.py")
        for text in [path.read_text(encoding="utf-8")]
        if '"零"' in text or "'零'" in text or "obedien" in text.lower()
    )
    assert hits == [], f"引擎代码里为「零」开了后门:{hits}"


def test_zeros_personality_is_appended_to_the_template_not_replacing_it(tmp_path):
    """库主的第三条:**只写 personality,绝不覆盖 prompt 模板**。

    真覆盖了模板,她连自己能做哪些动作都不知道,反而更动不了 —— 所以这条同时验
    "服从那段在"和"行为清单还在"。
    """
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        world.close()
    world = open_world_at(
        str(tmp_path / "w.db"), force_mock_llm=True,
        beats_path="docs/examples/零.beats.json",
    )
    with world:
        world.tick(2)
        assert "零" in world.scheduler.agents, "节拍没把她带进世界"
        world.config_set("chat.tools.enabled", True)
        world.player_move("p1", _where(world, "零"))

        blocks = world.debug_prompt("零", player_id="p1")["blocks"]
        by_label = {b["label"]: b["text"] for b in blocks}
        persona = by_label["persona"]

        assert "你就照办" in persona, "服从那段没进提示词"
        assert persona.startswith("你是零。"), "人设模板的开头被顶掉了"
        assert "回复格式硬性规则" in persona, "回复格式那一段被覆盖掉了"
        assert "能力" in by_label.get("tools", ""), "她读不到自己能做哪些动作了"
        assert "walk_away" in by_label.get("tools", ""), "能力清单只剩个标题"


# ── ⑤ 过日子的动作:玩家说得动的不该只有走路 ────────────────────────────────
#
# 拿真模型跑一遍盘点时逮出来的:「你去睡觉」被规规矩矩判成 narrative_direction
# (置信度 1.0),而分类器的动词表里**只有走路那几个**,于是它落进 `act` ——
# 一条 memory_seed,世界里没有任何人睡下。她那一轮的回话是"（零找了个地方躺下，
# 闭上眼睛。）"。**照跑,报成功,给错东西**:玩家看到的是她睡了,世界里她站着。
# 「你去吃点东西」同理,而且她还顺手调了 `walk_away` 想走去"小吃摊"——一个不存在
# 的地方,被引擎挡下。三条测试各钉一个动作,少一条就等于把那扇门重新开一半。


def _events(world: World, kind: str) -> list[dict]:
    """世界里真发生了什么 —— 判据只看事件。

    **别拿 `_current_action` 当判据**:那是行为树自己的记账,`emit_action` 不碰它。
    动作有没有落地,写在事件上(`sleep` → `agent_state{status:sleeping}`)。
    """
    return [e for e in world.events()
            if e["type"] == kind or (e.get("payload") or {}).get("kind") == kind]


def _give_to(world: World, holder: str, item_id: str, qty: int = 1) -> None:
    """给某人塞一件东西 —— 走账本那条唯一的路(库存是 `item_transfer` 的投影)。"""
    world._record_and_fan({
        "type": "item_transfer", "who": holder,
        "payload": {"from": "__town__", "to": holder, "item_id": item_id, "qty": qty},
    })
    world.tick(1)


def _body_world(tmp_path, action: str, **params):
    return _world(
        tmp_path,
        _classification("narrative_direction", 0.95, target="夏", action=action, **params),
        replies=("好。",),
    )


def test_telling_her_to_sleep_actually_puts_her_to_sleep(tmp_path):
    world, _ = _body_world(tmp_path, "sleep")
    with world:
        meta: dict = {}
        _say(world, "你去睡觉", meta)
        assert meta["intent_detail"]["kind"] == "sleep"
        assert meta["intent_detail"]["took"] is True, "又只落了一条记忆"
        slept = [e for e in _events(world, "agent_state")
                 if (e["payload"].get("state") or {}).get("status") == "sleeping"]
        assert slept, "世界里没有人睡下 —— 回话说她躺下了,而这正是要修的那种假"


def test_telling_her_to_eat_and_to_work_go_through_the_same_door(tmp_path):
    """`eat` / `work` 和 `sleep` 共用一张表 —— 分开写三份迟早只有一份跟着代码走。"""
    for action, kind in (("eat", "eat"), ("work", "work")):
        world, _ = _body_world(tmp_path / action, action)
        with world:
            meta: dict = {}
            _say(world, f"你去{action}", meta)
            assert meta["intent_detail"]["kind"] == kind
            assert meta["intent_detail"]["took"] is True, f"{action} 没在世界里发生"


def test_a_body_action_grounds_her_reply_in_what_already_happened(tmp_path):
    """兑现了还不够:她这一轮得**读得到**它,否则又是"世界动了、她说没动"。"""
    world, chat = _body_world(tmp_path, "sleep")
    with world:
        _say(world, "你去睡觉")
        system = "\n".join(chat.system_prompts)
        assert "已经动手了" in system, "刚发生的事没进她这一轮的提示词"
        assert "别说「我这就去」然后还杵在原地" in system


def test_telling_her_to_go_talk_to_someone_really_sends_her(tmp_path):
    """`talk_to`:被指挥的是她,而说话的**对象是另一个人** —— 两个人都要认出来。"""
    world, _ = _world(
        tmp_path,
        _classification("narrative_direction", 0.95,
                        target="夏", action="talk_to", object="柔"),
        replies=("嗯。",),
    )
    with world:
        world._tool_runtime.move_agent("柔", _where(world, "夏"))
        for _ in range(200):
            world.tick(1)
            if not world.scheduler._transit.get("柔"):
                break
        meta: dict = {}
        _say(world, "你去找柔说话", meta)
        assert meta["intent_detail"]["object"] == "柔"
        assert meta["intent_detail"]["took"] is True
        chats = [e for e in _events(world, "agent_action")
                 if e["payload"].get("action") == "chat"
                 and e["payload"].get("target") == "柔" and e.get("who") == "夏"]
        assert chats, "世界里没有这次搭话"


def test_带我去某个地方_两个人真的一起上路(tmp_path):
    """「带我去江堤走走」—— 玩家最先想得到的那件"一起做的事",而世界里一直没有它。

    线上两次现场,同一个样子:她答应得好好的(「行，走吧。潮汐里三号不远，过两条
    巷子就到」「走吧，从这儿过去十来分钟。你跟紧点」),然后**她一个人走了**,
    玩家还站在原地 —— 或者她压根没动。分类器早就把地方填进了 `place`
    (`{"action":"together","place":"江堤","object":"江堤"}`),而 `_together` 只认
    `object`:拿「江堤」去实体表里查,查出长椅、路灯、斜坡阶、老樟树四样,回给玩家
    一句「对得上好几样东西;说准一点」—— 而他要的是一个**地方**,那个地方就在
    这个世界里。

    两个人各走各的那一段(她 `move_agent`,他 `player_walk`),都要花时间:
    瞬移一个、走路一个,才是把"一起"两个字写成谎。
    """
    world, _ = _world(
        tmp_path,
        _classification("narrative_direction", 0.95, target="夏", action="together",
                        place="建筑工作室", object="建筑工作室", verb="走走"),
        replies=("（夏点头。）",),
    )
    with world:
        destination = next(
            pid for pid, name in world._tool_runtime.point_names().items()
            if name == "建筑工作室"
        )
        assert _where(world, "夏") != destination, "夹具前提没成立"
        meta: dict = {}
        reply = _say(world, "带我去建筑工作室走走", meta)

        assert meta["intent_detail"]["action"] == "go_together", \
            f"「带我去」被当成了对一样东西动手:{meta.get('intent_detail')}"
        assert "对得上好几样东西" not in reply, f"拿地名去查实体表了:{reply}"
        # 她上路了
        assert world.scheduler._transit.get("夏", {}).get("to") == destination, \
            "她没起程"
        # 他也上路了 —— 只挪她一个的话,回执写着"带你去"而世界里是他被落下
        assert world._player_transit.get("p1", {}).get("to") == destination, \
            "回执说带上了他,而世界里他还站在原地"


def test_已经在那儿了就不发一次假的行程(tmp_path):
    """「带我去这儿」—— 谁也不用动,那就别说"往那儿去了"。"""
    world, _ = _world(
        tmp_path,
        _classification("narrative_direction", 0.95, target="夏", action="together",
                        place="咖啡店", object="咖啡店"),
        replies=("（夏笑了。）",),
    )
    with world:
        here = _where(world, "夏")
        assert world._tool_runtime.point_names()[here] == "咖啡店", "夹具前提没成立"
        meta: dict = {}
        _say(world, "带我去咖啡店", meta)
        assert meta["intent_detail"]["reason"] == "already_there"
        assert "夏" not in world.scheduler._transit, "凭空起了一段路"


def test_隔着半个镇子带不上他(tmp_path):
    """「带我去」隔着手机不成立 —— 兑现它等于让她在两条街外把他领走。

    这一条**不挂在 `presence.enforce_colocation` 上**(那个开关默认关):它是新路,
    没有旧行为可打断,而这里放行的样子是世界里凭空多出一段"他跟着走"的行程。
    """
    world, _ = _world(
        tmp_path,
        _classification("narrative_direction", 0.95, target="夏", action="together",
                        place="建筑工作室", object="建筑工作室"),
        replies=("（夏在电话那头笑了。）",),
    )
    with world:
        world.player_move("p1", _elsewhere(world, "夏"))
        meta: dict = {}
        reply = _say(world, "带我去建筑工作室", meta)
        assert meta["intent_detail"]["reason"] == "not_colocated"
        assert "带不上你" in reply
        assert "p1" not in world._player_transit, "世界里多出了一段他没走过的路"


def test_talking_to_someone_who_is_not_here_says_so_instead_of_pretending(tmp_path):
    """在场语义由引擎守住 —— 隔着半个地图"去搭话"是空动作,不许报成功。"""
    world, _ = _world(
        tmp_path,
        _classification("narrative_direction", 0.95,
                        target="夏", action="talk_to", object="遥"),
        replies=("嗯。",),
    )
    with world:
        assert _where(world, "遥") != _where(world, "夏"), "夹具前提没成立"
        meta: dict = {}
        reply = _say(world, "你去找遥说话", meta)
        assert meta["intent_detail"]["reason"] == "not_colocated"
        assert "搭不上话" in reply


def test_talking_to_a_stranger_is_refused_with_the_roster(tmp_path):
    world, _ = _world(
        tmp_path,
        _classification("narrative_direction", 0.95,
                        target="夏", action="talk_to", object="林素"),
        replies=("嗯。",),
    )
    with world:
        reply = _say(world, "你去找林素说话")
        assert "我不认识林素" in reply, "拒绝了却没说下一步该找谁"


def test_directing_an_interaction_changes_the_world_quantity(tmp_path):
    """`interact` 走本体层那条统一路径 —— 兑现的判据是**世界的量真的变了**。"""
    world, _ = _world(
        tmp_path,
        _classification("narrative_direction", 0.95, target="夏", action="interact",
                        object="tree:harbor_oak", verb="tend"),
        replies=("好。",),
    )
    with world:
        world.set_stocks("agent:夏", {"体力": 100, "手艺": 1.0})
        _give_to(world, "夏", "garden_shears")
        before = world.stock("tree:harbor_oak", "树高")
        meta: dict = {}
        _say(world, "你去照料那棵老橡树", meta)
        assert meta["intent_detail"].get("ok") is True, meta["intent_detail"]
        assert world.stock("tree:harbor_oak", "树高") > before, "世界的量一点没动"


def test_an_interaction_refusal_is_passed_through_word_for_word(tmp_path):
    """三类拒绝(等一会儿 / 你做不了 / 讲不通)不许合并成一句"没成"。"""
    world, _ = _world(
        tmp_path,
        _classification("narrative_direction", 0.95, target="夏", action="interact",
                        object="tree:harbor_oak", verb="tend"),
        replies=("好。",),
    )
    with world:
        world.set_stocks("agent:夏", {"体力": 0, "手艺": 1.0})   # 累坏了 → incapable
        world.tick(1)
        meta: dict = {}
        reply = _say(world, "你去照料那棵老橡树", meta)
        assert meta["intent_detail"]["reason"] in ("refused", "invalid")
        # 导演层的词表(`refused`)和能力层的词表(`incapable`)各占一格 ——
        # 合成一个键的话,宿主照着 `reason` 分支时"她累坏了"会掉进 else。
        assert meta["intent_detail"].get("refusal_kind") == "incapable", meta["intent_detail"]
        assert "没能动手" in reply or "(" in reply


# ── ⑥ 把东西给她:施动者是玩家,所以它走账本而不是行为树 ──────────────────


def test_giving_her_something_moves_it_in_the_ledger(tmp_path):
    world, _ = _world(
        tmp_path,
        _classification("narrative_direction", 0.95, target="夏", action="give",
                        object="速写本"),
        replies=("谢谢。",),
    )
    with world:
        _give_to(world, "player:p1", "sketchbook")
        assert world.inventory("player:p1").get("sketchbook") == 1
        meta: dict = {}
        _say(world, "我把这个速写本给你", meta)
        assert meta["intent_detail"]["item_id"] == "sketchbook"
        assert world.inventory("夏").get("sketchbook") == 1, "她手上没拿到"
        assert not world.inventory("player:p1").get("sketchbook"), "玩家那边没扣 —— 复制品"


def test_you_cannot_give_away_what_you_do_not_have(tmp_path):
    """不挡的话,一句话就能凭空造出任何东西,而库存扣不到负数、账面看不出来。"""
    world, _ = _world(
        tmp_path,
        _classification("narrative_direction", 0.95, target="夏", action="give",
                        object="一辆车"),
        replies=("嗯?",),
    )
    with world:
        _give_to(world, "player:p1", "coffee")
        before = dict(world.inventory("夏"))   # 演示世界的角色开局就带着东西
        meta: dict = {}
        reply = _say(world, "我把一辆车给你", meta)
        assert meta["intent_detail"]["reason"] == "not_held"
        assert "你手上没有" in reply
        assert "咖啡" in reply, "拒绝了却没说他手上到底有什么"
        assert world.inventory("夏") == before, "凭空造出来了"


def test_the_gift_is_a_fact_in_her_prompt_not_a_request(tmp_path):
    world, chat = _world(
        tmp_path,
        _classification("narrative_direction", 0.95, target="夏", action="give",
                        object="速写本"),
        replies=("谢谢。",),
    )
    with world:
        _give_to(world, "player:p1", "sketchbook")
        _say(world, "我把这个速写本给你")
        system = "\n".join(chat.system_prompts)
        assert "已经拿在手上了" in system
        # **只看 grounding 那一段**:整份提示词里本来就有别的硬性规则(回复格式、
        # stance 标记),拿整份去搜"必须"搜到的是它们,那条断言等于没断言。
        grounding = system.split("【刚刚发生的事")[-1].split("\n\n")[0]
        assert "必须" not in grounding and "不许" not in grounding, (
            f"grounding 又在下命令了 —— 它只该陈述刚发生的事:{grounding!r}"
        )


def test_idle_actions_are_in_the_vocabulary_so_the_classifier_stops_reaching_for_leave(tmp_path):
    """「你去找个人待着」实测被判成 `leave` —— 于是她真的动身去了另一座城。

    **一次错误但真实的世界改动,比什么都不做坏得多**:玩家看得见她走了,而那不是他
    要的;世界里也没有一行日志说这不对劲。补法不是在 Director 里挡,是**给分类器一个
    对的选项** —— 它挑 `leave` 是因为那是当时最像的一个。
    """
    for action, kind in (("seek_company", "idle_social"), ("wander", "idle_wander")):
        world, _ = _body_world(tmp_path / action, action)
        with world:
            here = _where(world, "夏")
            meta: dict = {}
            _say(world, "你去找个人待着", meta)
            assert meta["intent_detail"]["kind"] == kind
            assert meta["intent_detail"]["took"] is True
            assert _where(world, "夏") == here, "「待着」把她挪走了"
            assert not world.scheduler._transit.get("夏"), "「待着」让她上了路"


def test_an_entity_is_found_by_the_name_the_player_actually_says(tmp_path):
    """玩家说「老橡树」,世界里那样东西的 id 是 `tree:harbor_oak`。

    实测里这一条的下场是"(这儿没有 天鹅冰雕 这个东西;有的是 ['icesculpture:swan'])"
    —— 技术上没错,而玩家读起来是句谎:那个东西就叫「半成的天鹅冰雕」。和地名走
    **同一个** `resolve_place`,另写一份匹配迟早给出两套答案。
    """
    world, _ = _world(
        tmp_path,
        _classification("narrative_direction", 0.95, target="夏", action="interact",
                        object="老橡树", verb="tend"),
        replies=("好。",),
    )
    with world:
        world.set_stocks("agent:夏", {"体力": 100, "手艺": 1.0})
        _give_to(world, "夏", "garden_shears")
        before = world.stock("tree:harbor_oak", "树高")
        meta: dict = {}
        _say(world, "你去照料那棵老橡树", meta)
        assert meta["intent_detail"].get("ok") is True, meta["intent_detail"]
        assert world.stock("tree:harbor_oak", "树高") > before
