"""玩家的对话是否真的参与世界演化 —— 在**没有配 key 的默认状态**下。

这条链路一直是完整的:`record_chat_turn` → `conversation` 事件 → 记忆(0.8,
角色能有的最重要一类)→ 关系判定 → 跨档 → relation_shift 记忆 + 图谱边 →
八卦源 + planner 的 prompt。玩家走的是和 NPC 完全相同的关系机制。

但它整条**断在第一环**:Mock LLM 给不出可解析的判定,于是判定器每次返回 None,
关系一动不动 —— 不是"变化小一点",是关系数据一条都不产生。而没有 key 是默认
状态,所以 README 承诺"会记住你的角色"的那一屏,恰恰是聊天完全不参与演化的
那一屏,并且只在 stderr 刷一行 `dropping`,角色照常回话。

这些测试盯的就是"默认状态下这条链通不通"。
"""
from __future__ import annotations

import time

import pytest

from anima_world.api import World
from anima_world.relationship_judge import DeterministicRelationshipJudge

A_DAY = 288  # tick,minutes_per_tick=5


def _await(condition, timeout: float = 5.0):
    """等一个跑在判定线程池上的结果。确定性判定器是微秒级的,超时只是保险丝。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.01)
    return condition()


def _say(world, text, *, agent="夏", player="p1"):
    reply = world.chat_reply(agent, [{"role": "user", "content": text}],
                             player_id=player, display_name="阿檀")
    world.record_chat_turn(agent, player, [{"role": "user", "content": text},
                                           {"role": "assistant", "content": reply}])


def test_a_player_conversation_moves_the_relationship_with_no_api_key(tmp_path):
    """默认状态下聊一次,关系必须动。此前它恒为零 —— 而且是无声的。"""
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.player_move("p1", "cafe")
        _say(world, "我叫阿檀,以后常来")

        sentiment = _await(lambda: world.state()["relations"].get("夏|p1", {}).get("sentiment"))
        assert sentiment and sentiment > 0, "没有 key 时,聊天必须仍然让关系发生变化"
        # 玩家不是特例:反方向同样记着,走的是同一套三轴机制。
        back = world.state()["relations"]["p1|夏"]
        assert back["sentiment"] > 0 and back["affection"] > 0

        kinds = {m["kind"] for m in world.memories("夏")}
        assert "user_conversation" in kinds, "对话本身要落成记忆"


def test_a_regular_visitor_eventually_crosses_a_band_and_grows_an_edge(tmp_path):
    """常来的人会真的从「淡漠」变成「熟识」,并在图谱上长出一条边。

    这是"参与世界演化"的分界线:跨档才生 relation_shift 记忆、才长图谱边、
    才进小团体计算 —— 只涨数字不跨档,等于什么都没发生。

    时钟是手推的,不是靠跑世界 —— 世界逐次不确定,靠跑 N tick 来断言"第几天
    跨档"必然假绿。同日阻尼(0.5^(N-1))按世界日重置,所以推日是必须的。
    """
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.player_move("p1", "cafe")
        crossed_on = None
        for day in range(1, 13):
            _say(world, f"第{day}天,又来了")
            _await(lambda: world.state()["relations"].get("夏|p1", {}).get("sentiment"))
            if crossed_on is None and any(
                m["kind"] == "relation_shift" for m in world.memories("夏")
            ):
                crossed_on = day
            world.scheduler.clock += A_DAY

        assert crossed_on is not None, "常来的访客最终必须跨过一个档位"
        assert 3 <= crossed_on <= 10, (
            f"第 {crossed_on} 天跨档 —— 太快像儿戏,太慢等于没有(按剩余空间衰减的步长)"
        )
        shift = next(m for m in world.memories("夏") if m["kind"] == "relation_shift")
        assert "熟识" in shift["summary"]
        edges = world.graph("夏")
        assert any(e["predicate"] == "friendship" and e["object"] == "agent:p1" for e in edges), (
            "跨档之后图谱上必须有这条边 —— 小团体与「听说过你」都建立在它上面"
        )


def test_a_player_memory_is_gossip_eligible(tmp_path):
    """没见过你的人也可能听说过你:对话记忆(0.8)是全场最重要的一条,
    正是八卦优先挑走的那条。"""
    from anima_world.gossip import pick_gossip

    class _AlwaysRolls:
        def random(self):
            return 0.0

    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.player_move("p1", "cafe")
        _say(world, "我叫阿檀")
        picked = pick_gossip(_AlwaysRolls(), "夏", world.memories("夏"), "遥")
        assert picked is not None and picked["agent_id"] == "遥"
        assert picked["kind"] == "hearsay1"
        assert picked["importance"] == pytest.approx(0.8 * 0.85, abs=1e-3)


# ── 确定性判定器本身 ────────────────────────────────────────────────────────


def test_the_stand_in_drifts_by_headroom_so_it_never_saturates():
    """反复寒暄不该把一段关系顶到 +1.0。按剩余空间衰减:渐近,永不饱和。"""
    judge = DeterministicRelationshipJudge()
    value = 0.0
    steps = []
    for _ in range(60):
        result = judge.judge_user(a={"name": "夏"}, player_name="阿檀",
                                  relation={"a_to_b": value, "b_to_a": value},
                                  transcript="", location="cafe")
        steps.append(result.delta_a_to_b)
        value = min(1.0, value + result.delta_a_to_b)
    assert steps == sorted(steps, reverse=True), "步长必须单调递减"
    assert value < 1.0, "永远到不了满值"
    assert value > 0.5, "但也要走得动 —— 不然等于没修"
    # 与真判定同量级:单次远小于 ±0.2 的上限。
    assert max(steps) <= 0.05


def test_the_stand_in_is_deterministic():
    """世界要可重放,所以判定不许掷骰子。"""
    judge = DeterministicRelationshipJudge()
    call = lambda: judge.judge(  # noqa: E731
        a={"name": "夏"}, b={"name": "遥"},
        relation={"a_to_b": 0.13, "b_to_a": -0.4},
        memories_a=[], memories_b=[], location="cafe",
    )
    first, second = call(), call()
    assert first == second


def test_the_stand_in_refuses_to_rewrite_an_authored_r_type():
    """r_type 是作者写的自由文本(「有点好奇的新面孔」这种)。

    好感度是一个数,机制要靠它继续走,给个小步长是合理的替身;散文没有像样的
    替身,用机械标签盖掉作者的字是**把有的东西换成更差的东西**,比冻住更糟。
    """
    assert DeterministicRelationshipJudge().relabel(
        old_r_type="有点好奇的新面孔", old_band="淡漠", new_band="熟识",
        a={"name": "夏"}, b={"name": "遥"}, memories=[],
    ) is None


def test_a_real_key_still_gets_the_real_judge(tmp_path):
    """替身只在 mock 档上场 —— 配了 key 就必须是真判定,不能被悄悄顶掉。"""
    from anima_world.relationship_judge import RelationshipJudge

    db = str(tmp_path / "w.db")
    with World.open(db, force_mock_llm=True) as world:
        assert isinstance(world.scheduler.relationship_judge, DeterministicRelationshipJudge)
        world.config_set("llm.api_key", "sk-not-a-real-key")
    with World.open(db) as world:
        assert isinstance(world.scheduler.relationship_judge, RelationshipJudge)


# ── graph(agent_id) 从来没对过 ──────────────────────────────────────────────


def test_graph_finds_an_agents_edges_by_bare_agent_id(tmp_path):
    """`World.graph("夏")` 此前永远返回空列表。

    图谱里的 subject 带 `agent:` 前缀,而这个参数收的是裸 id,查不到。它不
    报错,返回空 —— 宿主读成"这个角色没有任何关系",而不是"我调错了"。
    """
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        graph = world.scheduler.knowledge_graph
        graph.add("agent:夏", "friendship", "agent:遥")

        assert world.graph("夏"), "裸 id 必须查得到"
        assert world.graph("agent:夏"), "已经带前缀的也照单全收"
        assert world.graph("夏") == world.graph("agent:夏")
        assert world.graph("遥") == [], "反方向的边没写就是没有,不许瞎凑"
        assert len(world.graph()) == 1, "不带参数仍然是全量"


# ── 她记得你,但检索不出你 ──────────────────────────────────────────────────
#
# world_context 用 interlocutor_id 当检索 query,而那是宿主给的不透明 id('p1'、
# 一个 uuid)。记忆文本里写的是你的**名字**,两者字符二元组交集恒空 → relevance
# 恒 0 → 三因子检索退化成 recency+importance。角色确实记得你(记忆写进去了),
# 但每次聊天召回的都不是关于你的那几条。

def _remember(world, agent, text, *, tick):
    world.scheduler.memory_store.add(
        agent_id=agent, tick=tick, kind="user_conversation",
        summary=text, importance=0.8, anchor=False,
    )


def test_recall_uses_the_players_name_not_the_opaque_id(tmp_path):
    """检索 query 必须是玩家的显示名 —— id 在记忆文本里根本不出现。"""
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.player_move("p1", "cafe")
        # 关于阿檀的两条是**旧**的,无关的六条是新的 —— 这样 recency 帮不上忙,
        # 只有 relevance 能把它们捞上来。query 用不透明 id 时 relevance 恒 0。
        _remember(world, "夏", "阿檀说他在找一把旧伞", tick=1)
        _remember(world, "夏", "阿檀第二次来,还是在找那把伞", tick=2)
        for i in range(6):
            _remember(world, "夏", f"和别人聊了第{i}件无关的事", tick=100 + i)

        world.chat_reply("夏", [{"role": "user", "content": "在吗"}],
                         player_id="p1", display_name="阿檀")
        memories = world.world_context("夏", "p1").get("memories", [])
        assert any("阿檀" in m for m in memories), (
            f"召回里一条关于阿檀的都没有:{memories}"
        )


def test_recall_order_is_deterministic_when_everything_ties(tmp_path):
    """完全同分时的次序必须是确定的 —— 世界要可重放。

    并列不是假想:创世注入的 memory_seed 全部 tick=0,importance 也常常相同,而
    query 命中名字时 relevance 一样是 1.0。`ORDER BY tick DESC` 到此为止,余下的
    次序就交给 SQLite 的物理布局了 —— 同一个世界在两台机器上可能召回不同的记忆,
    而且不报错。次序键补到 id 为止。

    (注:这里**不是**在修"只召回最早三条"。`query()` 已经是 tick DESC,而
    `rows.sort` 是稳定排序,所以并列本来就解析成最新优先。)
    """
    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        for i in range(6):
            _remember(world, "夏", f"创世记忆 {i}:阿檀", tick=0)
        runs = [
            [m["id"] for m in world.scheduler.memory_store.retrieve(
                "夏", now_tick=0, query="阿檀", k=3, reinforce=False)]
            for _ in range(3)
        ]
        assert runs[0] == runs[1] == runs[2], f"同一份数据召回了不同的记忆:{runs}"
        assert runs[0] == sorted(runs[0], reverse=True), (
            f"完全同分时应当按 id 降序(晚写的先召回),实际 {runs[0]}"
        )


def test_a_display_name_too_short_to_match_says_so(tmp_path, caplog):
    """单字中文名的 bigram 与记忆文本交集恒空 —— 静默退化成今天的行为,
    正是"降级不许无声"这条纪律要挡的。"""
    import logging

    with World.open(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        _remember(world, "夏", "夕说她明天不来了", tick=1)
        world.chat_reply("夏", [{"role": "user", "content": "在吗"}],
                         player_id="p1", display_name="夕")
        with caplog.at_level(logging.WARNING, logger="anima_world.api"):
            world.world_context("夏", "p1")
        assert any("夕" in r.getMessage() for r in caplog.records), (
            f"单字名检索不出东西,至少得说一声。实际日志:{[r.getMessage() for r in caplog.records]}"
        )
