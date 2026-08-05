"""stance:她这句话背后的关系性意图(issue #18)。

dogfooding 里 200+ 条聊天暴露的病:她的**关系性意图完全平坦** —— 始终"配合",
不会主动讨好,不会赌气,不会试探。这里盯四件事:

1. 她声明的意图真的落库、真的回到下一轮的提示词里(惯性)
2. 控制标记**一个字都不许漏给玩家**
3. 她没选的时候,产物上必须能和"她选了中性"分开(`declared`)
4. 开关关着时,这一整套连提示词都不进 —— 1.2 的行为逐字不变
"""
from __future__ import annotations

from _worldfile import open_world_at

import pytest

from anima_world.api import World
from anima_world.stance import STANCES


class ScriptedLLM:
    """按脚本一句句回,并留下它收到的 prompt(断言"她下一轮真的看见了")。"""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[list[dict]] = []

    def _next(self, messages) -> str:
        self.prompts.append([dict(m) for m in messages])
        return self.replies.pop(0) if self.replies else ""

    async def stream(self, messages):
        text = self._next(messages)
        for index in range(0, len(text), 5):  # 切碎:标记会跨 token,解析必须撑住
            yield text[index : index + 5]

    async def complete(self, messages) -> str:
        return self._next(messages)

    @property
    def system_prompts(self) -> list[str]:
        return [
            "\n".join(m["content"] for m in prompt if m["role"] == "system")
            for prompt in self.prompts
        ]


def _world(tmp_path, *replies: str, stance: bool = True) -> tuple[World, ScriptedLLM]:
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    llm = ScriptedLLM(*replies)
    world.chat_service._llm = llm
    world.config_set("chat.stance.enabled", stance)
    world.player_move("p1", "cafe")
    return world, llm


def _say(world: World, text: str, meta: dict | None = None) -> str:
    return world.chat_reply(
        "夏", [{"role": "user", "content": text}],
        player_id="p1", display_name="阿檀", meta=meta,
    )


def test_a_declared_stance_lands_in_the_world_and_never_in_the_reply(tmp_path):
    world, _ = _world(tmp_path, "〔stance:provoke〕（夏把杯子推回去。）你自己看着办。")
    with world:
        meta: dict = {}
        reply = _say(world, "帮我个忙", meta)

        assert "〔" not in reply and "stance" not in reply, f"控制标记漏给玩家了:{reply!r}"
        assert reply.strip().startswith("（"), reply  # 动作括号照旧是第一段散文
        assert meta["stance"] == "provoke" and meta["stance_declared"] is True
        stored = world.stance("夏", "p1")
        assert stored["stance"] == "provoke" and stored["declared"] is True
        assert stored["updated_at"] > 0, "落库要带时间,不然运维台排不了序"


def test_the_previous_stance_comes_back_as_inertia_not_as_a_dice_roll(tmp_path):
    """惯性是提示词压力:上一轮的意图要回到下一轮的提示词里。

    引擎侧摇骰子强行保留会**覆盖角色自己的选择**,而且同一句话跑两次给两个结果 ——
    那种"惯性"在日志上看不出为什么。
    """
    world, llm = _world(
        tmp_path,
        "〔stance:test〕你以前不是这样的。",
        "〔stance:test〕……说话。",
    )
    with world:
        _say(world, "怎么了")
        _say(world, "我在")

        inertia = "上一轮你对 ta 的意图是「试探」"
        assert inertia in llm.system_prompts[1], "第二轮的提示词里没有上一轮的 stance"
        assert inertia not in llm.system_prompts[0], "第一轮不该有惯性那句"


def test_an_undeclared_stance_is_distinguishable_from_a_chosen_neutral(tmp_path):
    """她没选 ≠ 她选了中性。产物上一模一样,只有 `declared` 能分开。"""
    world, _ = _world(tmp_path, "（夏擦着杯子。）来了。")
    with world:
        meta: dict = {}
        _say(world, "在吗", meta)

        assert meta["stance"] == "neutral" and meta["stance_declared"] is False
        assert world.stance("夏", "p1")["declared"] is False


def test_a_stance_outside_the_enum_falls_back_and_says_so(tmp_path, caplog):
    """开放字符串一开就散。不认识的值退回中性,并留下一条警告。"""
    world, _ = _world(tmp_path, "〔stance:讨好卖乖〕（夏笑了。）来啦。")
    with world:
        with caplog.at_level("WARNING"):
            meta: dict = {}
            reply = _say(world, "嗨", meta)

        assert meta["stance"] == "neutral" and meta["stance_declared"] is False
        assert "讨好卖乖" not in reply
        assert any("stance" in record.getMessage() for record in caplog.records)


def test_stance_is_per_target_not_per_agent(tmp_path):
    """她可以同时对你找茬、对别人讨好 —— 存储必须是 (角色, 对象) 的。"""
    world, llm = _world(
        tmp_path, "〔stance:provoke〕随你。", "〔stance:please〕当然可以呀。",
    )
    with world:
        _say(world, "在吗")
        world.chat_reply("夏", [{"role": "user", "content": "在吗"}],
                         player_id="p2", display_name="小林")

        assert world.stance("夏", "p1")["stance"] == "provoke"
        assert world.stance("夏", "p2")["stance"] == "please"


def test_with_the_flag_off_nothing_about_stance_reaches_the_prompt(tmp_path):
    """默认关闭意味着**连菜单都不进提示词** —— 否则她会照着念一个没人读的标记。"""
    world, llm = _world(tmp_path, "（夏擦着杯子。）来了。", stance=False)
    with world:
        meta: dict = {}
        _say(world, "在吗", meta)

        assert "stance" not in llm.system_prompts[0]
        assert "stance" not in meta
        assert world.stance("夏", "p1") is None


def test_a_stance_declared_by_the_model_is_stored_on_the_message_row(tmp_path):
    """观测量落在消息行上(不是每轮一个事件 —— 聊天与事件核解耦那条不变量)。"""
    world, _ = _world(tmp_path, "〔stance:avoid〕嗯。")
    with world:
        meta: dict = {}
        reply = _say(world, "你还好吗", meta)
        conversation_id = world.record_chat_turn(
            "夏", "p1",
            [{"role": "user", "content": "你还好吗"},
             {"role": "assistant", "content": reply}],
            meta=meta,
        )

        rows = world.chat_store.annotation_rows(conversation_id)
        assert ("assistant", "avoid") in [(r[0], r[1]) for r in rows]

        # 而整场会话的分布随关闭时那**一个** conversation 事件出去。
        closing = [
            event for event in world.events()
            if event["type"] == "conversation"
            and event["payload"]["conversation_id"] == conversation_id
        ]
        assert closing and closing[0]["payload"]["stances"] == {"avoid": 1}
        per_turn = [event for event in world.events() if event["type"] == "agent_stance"]
        assert not per_turn, "每轮一个事件等于把聊天转录搬进世界的历史"


def test_a_stance_she_never_chose_is_not_written_onto_the_message_row(tmp_path):
    """兜底的 neutral 不上消息行。

    写上去,下游的分布就成了"她 100% 中性" —— 而真相是"她一次都没选过"。
    这两件事差得很远,而它们在文本上一模一样(没有 key 的世界里全是后者)。
    """
    world, _ = _world(tmp_path, "（夏擦着杯子。）来了。")
    with world:
        meta: dict = {}
        reply = _say(world, "在吗", meta)
        assert meta["stance_declared"] is False
        conversation_id = world.record_chat_turn(
            "夏", "p1",
            [{"role": "user", "content": "在吗"},
             {"role": "assistant", "content": reply}],
            meta=meta,
        )
        rows = world.chat_store.annotation_rows(conversation_id)
        assert all(row[1] is None for row in rows)

        closing = next(
            event for event in world.events()
            if event["type"] == "conversation"
            and event["payload"]["conversation_id"] == conversation_id
        )
        assert "stances" not in closing["payload"], "她没选过,就不该有分布"
        # 而"她此刻的姿态"仍然记着 —— 那是世界状态,带着 declared=False。
        assert world.stance("夏", "p1") == {
            "stance": "neutral", "declared": False,
            "updated_tick": world.stance("夏", "p1")["updated_tick"],
            "updated_at": world.stance("夏", "p1")["updated_at"],
        }


def test_the_legacy_send_path_also_strips_the_markers(tmp_path):
    """`ChatService.send()` 是 M3.5 那条自己落转录的老路(引擎内已无调用者,但它是
    库的接口面)。它和 `respond()` 共用同一条解析流 —— 不然点亮 stance 之后,任何还
    在用这条路的宿主会把 `〔stance:…〕` 原样存进消息表并显示给玩家。
    """
    import asyncio

    world, _ = _world(tmp_path, "〔stance:please〕（夏赶紧擦干手。）来啦来啦。")
    with world:
        async def _run() -> str:
            parts = []
            async for chunk in world.chat_service.send("夏", "在吗", player_id="p1"):
                parts.append(chunk)
            return "".join(parts)

        reply = asyncio.run(_run())
        assert "〔" not in reply and "stance" not in reply, reply

        stored = None
        for aid in world.scheduler.agents:
            for conv in world.chat_store.list_conversations(aid):
                for msg in world.chat_store.messages_for(int(conv["id"])):
                    if msg["role"] == "assistant":
                        stored = msg
        assert stored is not None
        assert "〔" not in stored["content"], f"标记被存进了消息表:{stored['content']!r}"
        assert world.stance("夏", "p1")["stance"] == "please"


def test_every_stance_in_the_enum_is_accepted(tmp_path):
    """枚举是契约:表里有的每一个都必须能被声明,否则菜单在骗模型。"""
    world, _ = _world(tmp_path, *(f"〔stance:{name}〕好。" for name in STANCES))
    with world:
        for name in STANCES:
            meta: dict = {}
            _say(world, "嗯", meta)
            assert meta["stance"] == name, f"{name} 没被接受"
