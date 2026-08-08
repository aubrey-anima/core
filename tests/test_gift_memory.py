"""礼物被珍藏 —— 她记得是谁给的,而账本上的其余流水一条都不记。

这一条真正难的地方不是"落一条记忆",是**那道闸**。`item_transfer` 是账本上的一条
大路:买东西走它、创世注入走它、节拍发货走它。给每一条都记一笔的话,一个跑着经济
的世界一天几十条,而记忆表是**有界的** —— 她真正的过去会被"从货架上拿了一杯咖啡"
挤出去,一声不吭。所以这个文件里"不该记的那几条"和"该记的那一条"同样重要。
"""
from __future__ import annotations

import json

from _worldfile import open_world_at

from anima_world.api import World


class ScriptedLLM:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)

    def _next(self, messages) -> str:
        return self.replies.pop(0) if self.replies else ""

    async def stream(self, messages):
        yield self._next(messages)

    async def complete(self, messages) -> str:
        return self._next(messages)


def _classification(intent: str, confidence: float, **params: object) -> str:
    return json.dumps(
        {"intent": intent, "confidence": confidence, "params": params}, ensure_ascii=False
    )


def _where(world: World, agent_id: str) -> str:
    brain = world.scheduler.agents[agent_id]
    return brain.agent.blackboard.read("loc") or brain.agent.location


def _gift_world(tmp_path, obj: str = "速写本"):
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    world.chat_service._llm = ScriptedLLM("谢谢。")
    world.chat_service._background_llm = ScriptedLLM(
        _classification("narrative_direction", 0.95, target="夏", action="give", object=obj)
    )
    world.config_set("chat.intent.enabled", True)
    world.player_move("p1", _where(world, "夏"))
    return world


def _stock_player(world: World, item_id: str, qty: int = 1) -> None:
    """给玩家塞一件东西 —— 走账本(`__town__` 出货,不是人给的)。"""
    world._record_and_fan({
        "type": "item_transfer", "who": "player:p1",
        "payload": {"from": "__town__", "to": "player:p1", "item_id": item_id, "qty": qty},
    })
    world.tick(1)


def _memories(world: World, agent_id: str, kind: str) -> list[dict]:
    return [m for m in world.memories(agent_id) if m.get("kind") == kind]


# ── 该记的那一条 ────────────────────────────────────────────────────────────


def test_a_gift_becomes_a_memory_that_names_the_giver(tmp_path):
    """她收下之后**记得是谁给的什么** —— 从前收下就完了。"""
    world = _gift_world(tmp_path)
    with world:
        _stock_player(world, "sketchbook")
        world.chat_reply(
            "夏", [{"role": "user", "content": "我把这个速写本给你"}],
            player_id="p1", display_name="阿檀",
        )
        world.tick(1)

        gifts = _memories(world, "夏", "gift")
        assert len(gifts) == 1, f"礼物没进她的记忆:{world.memories('夏')}"
        summary = gifts[0]["summary"]
        assert "阿檀" in summary, f"记不得是谁给的:{summary!r}"
        assert "速写本" in summary, f"记不得给的是什么:{summary!r}"
        # id 不许漏进人话里 —— 这条记忆会进她的提示词,也会被八卦原样转述。
        assert "sketchbook" not in summary and "p1" not in summary, summary


def test_the_gift_memory_reaches_the_prompt_she_actually_reads(tmp_path):
    """"进了记忆表"要能验成"进了她读到的那份提示词",否则又是零个读取点。"""
    world = _gift_world(tmp_path)
    with world:
        _stock_player(world, "sketchbook")
        world.chat_reply(
            "夏", [{"role": "user", "content": "我把这个速写本给你"}],
            player_id="p1", display_name="阿檀",
        )
        world.tick(1)

        blocks = world.debug_prompt("夏", player_id="p1")["blocks"]
        text = "\n".join(b["text"] for b in blocks)
        assert "速写本" in text, "礼物进了记忆表,却进不了她真正读到的提示词"


def test_a_gift_is_heavy_enough_to_be_a_contact_reason(tmp_path):
    """"附赠"那一条:0.6 是 `contact` 判"强记忆"的线,礼物要过得去。

    钉的是**数值关系**而不是常数本身 —— 哪天有人把 gift 的重要度调到 0.55,
    这一层不会报错,只会安静地再也不把送礼当由头。
    """
    from anima_world import contact
    from anima_world.memory_triggers import _GIFT_IMPORTANCE

    world = _gift_world(tmp_path)
    with world:
        _stock_player(world, "sketchbook")
        world.chat_reply(
            "夏", [{"role": "user", "content": "我把这个速写本给你"}],
            player_id="p1", display_name="阿檀",
        )
        world.tick(1)

        gift = _memories(world, "夏", "gift")[0]
        assert float(gift["importance"]) >= 0.6, "够不上强记忆,contact 那边永远看不见它"
        assert _GIFT_IMPORTANCE < 0.7, "别重过「他当面交代我一件事」"

        reasons = world._contact_reasons(
            "夏", "p1", "阿檀", now_tick=world.scheduler.clock, last_contact_tick=None
        )
        kinds = {r.kind for r in reasons}
        assert "strong_memory" in kinds, f"礼物没成为她想起他的由头:{[r.to_dict() for r in reasons]}"
        top = next(r for r in reasons if r.kind == "strong_memory")
        assert "速写本" in top.note, top.note
        assert contact.REASON_WEIGHTS["strong_memory"] > 0


# ── 不该记的那几条(这一半才是重点)────────────────────────────────────────


def test_buying_from_a_shelf_leaves_no_memory(tmp_path):
    """买卖走同一条事件。一天几十条,记下来就把她真正的过去挤出去了。"""
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        world.player_move("p1", "cafe")
        world.player_topup("p1", 100.0)
        # 创世那几条记忆本来就提到咖啡店 —— 判据只能是**这三杯咖啡新添了什么**。
        before = {aid: {m["id"] for m in world.memories(aid)} for aid in world.scheduler.agents}
        for _ in range(3):
            world.player_buy("p1", "cafe", "coffee")
        world.tick(1)
        assert _memories(world, "夏", "gift") == [], "买东西被记成礼物了"
        for agent_id in world.scheduler.agents:
            traces = [m for m in world.memories(agent_id)
                      if m["id"] not in before[agent_id]
                      and ("coffee" in m["summary"] or "咖啡" in m["summary"])]
            assert traces == [], f"买卖在 {agent_id} 的记忆里留下了痕迹:{traces}"


def test_the_town_handing_someone_something_is_not_a_gift(tmp_path):
    """创世注入 / 排班领料的形状:`__town__`、`__world__` 都不是人。"""
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        for source in ("__town__", "__world__", "shop:cafe"):
            world._record_and_fan({
                "type": "item_transfer", "who": "夏",
                "payload": {"from": source, "to": "夏", "item_id": "coffee", "qty": 1},
            })
        world.tick(1)
        assert _memories(world, "夏", "gift") == [], "非人给的东西被记成礼物了"


def test_a_transfer_with_no_source_is_not_a_gift(tmp_path):
    """无中生有(创世注入)不是谁给的 —— 那是她一开始就有的。"""
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        world._record_and_fan({
            "type": "item_transfer", "who": "夏",
            "payload": {"to": "夏", "item_id": "coffee", "qty": 1},
        })
        world.tick(1)
        assert _memories(world, "夏", "gift") == []


def test_a_transfer_with_a_ledger_reason_is_not_a_gift(tmp_path):
    """带账目理由的转移是交易/领料 —— 它有对家、有代价,礼物没有。"""
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        world._record_and_fan({
            "type": "item_transfer", "who": "遥",
            "payload": {"from": "遥", "to": "夏", "item_id": "coffee", "qty": 1,
                        "reason": "purchase:coffee"},
        })
        world.tick(1)
        assert _memories(world, "夏", "gift") == [], "一笔买卖被记成了心意"


def test_giving_to_a_player_writes_nothing(tmp_path):
    """玩家没有记忆表。往那边写等于往一个不存在的收件人写。"""
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        world._record_and_fan({
            "type": "item_transfer", "who": "夏",
            "payload": {"from": "夏", "to": "player:p1", "item_id": "coffee", "qty": 1},
        })
        world.tick(1)
        for agent_id in world.scheduler.agents:
            assert _memories(world, agent_id, "gift") == [], (
                f"送给玩家的东西在 {agent_id} 名下记了一条 —— 收礼那一头压根没有记忆表"
            )


# ── 角色送角色 ──────────────────────────────────────────────────────────────


def test_one_character_giving_another_is_a_gift_too(tmp_path):
    """判据是「人给人的」,不是「玩家给的」—— 两个角色之间同样成立。"""
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    with world:
        world._record_and_fan({
            "type": "item_transfer", "who": "遥",
            "payload": {"from": "遥", "to": "夏", "item_id": "sketchbook", "qty": 1},
        })
        world.tick(1)
        gifts = _memories(world, "夏", "gift")
        assert len(gifts) == 1
        # 老日志没带名字,退回投影里的人话名字(不是 id)。
        assert "沈遥" in gifts[0]["summary"] or "遥" in gifts[0]["summary"], gifts[0]["summary"]
        assert _memories(world, "遥", "gift") == [], "送出去那一头不该也记一条"


def test_a_gift_survives_a_memory_rebuild(tmp_path):
    """记忆是**事件的投影**:空库重放必须折出同一条,否则重启一次礼物就没了。"""
    from anima_world.memory_triggers import TriggerEngine
    from anima_world.projection import project_events
    from anima_world.types import Event, Projection

    engine = TriggerEngine()
    projection = Projection()
    events = [
        Event(seq=1, ts=0, type="agent_join", loc=None, who="夏",
              payload={"spec": {"name": "苏晚夏"}, "location": "cafe"}),
        Event(seq=2, ts=3, type="item_transfer", loc=None, who="player:p1",
              payload={"from": "player:p1", "to": "夏", "item_id": "sketchbook",
                       "qty": 1, "from_name": "阿檀", "item_name": "速写本"}),
    ]
    found = []
    for event in events:
        descriptor = engine.process(
            {"seq": event.seq, "ts": event.ts, "type": event.type,
             "who": event.who, "loc": event.loc, "payload": event.payload},
            projection,
        )
        if descriptor is not None:
            found.append(descriptor)
        project_events([event], base=projection)

    assert len(found) == 1
    assert found[0].kind == "gift"
    assert found[0].agent_id == "夏"
    assert found[0].summary == "阿檀把速写本给了我"
    assert found[0].tick == 3, "记忆的 tick 该是那件事发生的那一刻"
