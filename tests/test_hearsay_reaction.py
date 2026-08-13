"""吃醋 —— 听到之后有反应,而**反应由她自己给,不由引擎给**。

八卦早就在角色之间传话了,`relation_shift` 记忆本来就可传。缺的是两样:

1. **摘要里写的是 id**。玩家那一头的 id 是一串 uuid,于是传出去的那句
   「她对 8f3c-… 的关系进入「亲近」」没有一个人看得懂 —— 包括读它的模型。
2. **听到之后没有反应**。吃醋就是那个反应。

第二条最容易写错的版本是"听到关于亲近的人的八卦就自动扣分"。那又是一处引擎替
角色做主,而且是最坏的那种:同一句「他跟楚夭夭走得近」,别扭的人闷着、坦荡的人
一笑而过、占有欲强的人才记恨 —— 写成自动机的话三个人得到同一个数字,而**世界
照跑、日志一行不错**。所以这个文件里钉的是"引擎只搬运判定,不自己产生结论"。
"""
from __future__ import annotations

import json

import pytest
from _worldfile import open_world_at

from anima_world.gossip import PRIVATE_KINDS, pick_gossip
from anima_world.memory_triggers import TriggerEngine
from anima_world.projection import project_events
from anima_world.relationship_judge import (
    DeterministicRelationshipJudge,
    RelationshipJudge,
)
from anima_world.types import Projection


class _FakeLLM:
    """记下它收到的提示词 —— 判定的输入是这一层的一半契约。"""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete_sync(self, messages):
        self.prompts.append("\n".join(m["content"] for m in messages))
        return self.replies.pop(0) if self.replies else ""


def _verdict(summary: str, *reactions: dict) -> str:
    return json.dumps({"summary": summary, "reactions": list(reactions)}, ensure_ascii=False)


_ROSTER = {"阿檀": 0.62, "柔": 0.30, "沈遥": -0.10}


# ── ① 摘要里写名字,不写 id ─────────────────────────────────────────────────


def test_a_relation_shift_summary_uses_names_not_ids():
    """这条记忆会被八卦**原样转述**,所以它必须是一句人话。"""
    engine = TriggerEngine()
    projection = Projection()
    descriptor = engine.process(
        {"seq": 7, "ts": 40, "type": "state_change", "who": "夏",
         "payload": {"kind": "sentiment_delta", "as": "夏",
                     "target": "8f3c-4a11-9d2e-0000", "delta": 0.35,
                     "as_name": "苏晚夏", "target_name": "阿檀"}},
        projection,
    )
    assert descriptor is not None, "跨了一档却没生成记忆"
    assert "阿檀" in descriptor.summary
    assert "8f3c" not in descriptor.summary, f"uuid 漏进了人话:{descriptor.summary!r}"


def test_她在自己的记忆里是我_而不是她自己的名字():
    """这条记忆的主人就是变了心的那个人(`agent_id=as_id`)。写成第三人称的话
    她在自己的记忆里读到自己的名字,而八卦转述出来还是对的 ——「听苏晚夏说:我
    和阿檀成了挚交」正是一个人转述另一个人的样子。和 `_on_agent_state` 同一课。
    """
    engine = TriggerEngine()
    descriptor = engine.process(
        {"seq": 7, "ts": 40, "type": "state_change", "who": "夏",
         "payload": {"kind": "sentiment_delta", "as": "夏", "target": "檀",
                     "delta": 0.35, "as_name": "苏晚夏", "target_name": "阿檀"}},
        Projection(),
    )
    assert descriptor is not None
    assert descriptor.summary.startswith("我")
    assert "苏晚夏" not in descriptor.summary, f"她读到了自己的名字:{descriptor.summary!r}"


def test_关系记忆里不许出现引擎的数字():
    """线上读到的原文:「周叔 对 何师傅 的关系从「亲近」进入「挚交」(+0.80→+0.85)」。
    人不会记得自己对谁的好感度从 0.80 涨到 0.85 —— 那是引擎的账,不是她的记忆,
    而八卦会把它**原样**转述给下一个人。两条路都验:跨档那条和剧变那条。
    """
    engine = TriggerEngine()
    band_cross = engine.process(
        {"seq": 7, "ts": 40, "type": "state_change", "who": "夏",
         "payload": {"kind": "sentiment_delta", "as": "夏", "target": "檀",
                     "delta": 0.35, "target_name": "阿檀"}},
        Projection(),
    )
    swing = engine.process(
        {"seq": 8, "ts": 41, "type": "state_change", "who": "夏",
         "payload": {"kind": "sentiment", "as": "夏", "target": "檀",
                     "sentiment": -0.8, "target_name": "阿檀"}},
        Projection(),
    )
    for descriptor in (band_cross, swing):
        assert descriptor is not None
        text = descriptor.summary
        assert not any(ch.isdigit() for ch in text), f"引擎的数字漏进了记忆:{text!r}"
        for junk in ("Δ", "+", "→"):
            assert junk not in text, f"引擎的记号漏进了记忆:{text!r}"


def test_an_old_log_without_names_still_reads_as_well_as_it_used_to():
    """名字是后加的字段。老日志重放不许因此**变得更差**,退回 id 就行。"""
    engine = TriggerEngine()
    projection = Projection()
    descriptor = engine.process(
        {"seq": 3, "ts": 10, "type": "state_change", "who": "夏",
         "payload": {"kind": "sentiment_delta", "as": "夏", "target": "遥", "delta": 0.35}},
        projection,
    )
    assert descriptor is not None
    assert "遥" in descriptor.summary


def test_the_projection_supplies_the_name_when_the_event_does_not():
    """事件没带名字、但投影认识这个角色时,用人话名字而不是 id。"""
    from anima_world.types import Event

    engine = TriggerEngine()
    projection = Projection()
    project_events([
        Event(seq=1, ts=0, type="agent_join", loc=None, who="夏",
              payload={"spec": {"name": "苏晚夏"}, "location": "cafe"}),
        Event(seq=2, ts=0, type="agent_join", loc=None, who="遥",
              payload={"spec": {"name": "沈遥"}, "location": "workshop"}),
    ], base=projection)
    descriptor = engine.process(
        {"seq": 9, "ts": 12, "type": "state_change", "who": "夏",
         "payload": {"kind": "sentiment_delta", "as": "夏", "target": "遥", "delta": 0.35}},
        projection,
    )
    assert descriptor is not None
    # id 是「遥」,投影里的名字是「沈遥」—— 后者出现才说明这一跳真的走到了
    assert "沈遥" in descriptor.summary


# ── ② 判定:引擎只搬运,不产生结论 ──────────────────────────────────────────


def test_the_judge_sees_her_personality_the_rumor_and_the_roster():
    """判定要能因人而异,它就得**收得到那个人** —— 三样缺一样都判不出差别来。"""
    llm = _FakeLLM(_verdict("她没往心里去"))
    judge = RelationshipJudge(llm)
    judge.judge_hearsay(
        a={"name": "白霜", "personality": "别扭,嘴上不认,心里记得清清楚楚"},
        rumor="听沈遥说:阿檀最近老往柔那儿跑",
        roster=_ROSTER,
        memories=["昨天阿檀说要来看我"],
        location="cafe",
    )
    prompt = llm.prompts[0]
    assert "白霜" in prompt and "别扭" in prompt, "判定读不到她是谁,那就判不出因人而异"
    assert "阿檀最近老往柔那儿跑" in prompt, "判定读不到那句话本身"
    for name in _ROSTER:
        assert name in prompt, f"名单里少了 {name},她就没法对他起反应"
    assert "空数组" in prompt, "少了「不在乎也是一个真实的反应」,每句闲话都会变成心事"


def test_two_personalities_can_reach_opposite_conclusions_on_the_same_rumor():
    """这一层的**全部意义**:同一句话,两个人的结论可以是反的。

    引擎侧能钉的是"它照搬,不加工" —— 两个不同的判定进来,出去的就是两个不同的
    结果。**引擎里一行按亲疏、按幅度的加工都没有**,所以差别只可能来自那个人。
    """
    rumor = "听沈遥说:阿檀最近老往柔那儿跑"
    jealous = RelationshipJudge(_FakeLLM(
        _verdict("她心里堵得慌,面上什么也没说",
                 {"about": "阿檀", "delta": -0.12, "axes": {"trust": -0.15}},
                 {"about": "柔", "delta": -0.08})
    ))
    generous = RelationshipJudge(_FakeLLM(
        _verdict("她替他高兴,柔那个人是不错", {"about": "柔", "delta": 0.06})
    ))
    kwargs = dict(rumor=rumor, roster=_ROSTER, memories=[], location="cafe")

    a = jealous.judge_hearsay(a={"name": "白霜", "personality": "别扭"}, **kwargs)
    b = generous.judge_hearsay(a={"name": "零", "personality": "坦荡"}, **kwargs)

    assert {r.about: r.delta for r in a.reactions} == {"阿檀": -0.12, "柔": -0.08}
    assert {r.about: r.delta for r in b.reactions} == {"柔": 0.06}
    assert a.reactions[0].delta < 0 < b.reactions[0].delta, "同一句话没能得出相反的结论"
    assert a.reactions[0].axes == {"trust": -0.15}


def test_not_caring_is_a_verdict_and_not_a_failure():
    """空的 `reactions` 和 `None` **是两件事**,合并了就再也分不出
    "她听了不在乎"和"这一层根本没接上"。"""
    judge = RelationshipJudge(_FakeLLM(_verdict("闲话罢了,她没往心里去")))
    verdict = judge.judge_hearsay(
        a={"name": "夏"}, rumor="听说下雨了", roster=_ROSTER, memories=[], location="",
    )
    assert verdict is not None, "读得懂的回包不许当成失败"
    assert verdict.reactions == ()
    assert verdict.summary


def test_a_name_outside_the_roster_is_dropped():
    """名单是一道闸。放行的话,编出来的名字会翻不回任何 id —— 而"翻不回去就
    静默丢弃"和"根本没判定"在产物上一模一样。"""
    judge = RelationshipJudge(_FakeLLM(
        _verdict("她想起了一个不存在的人",
                 {"about": "楚夭夭", "delta": -0.2},
                 {"about": "阿檀", "delta": -0.05})
    ))
    verdict = judge.judge_hearsay(
        a={"name": "夏"}, rumor="…", roster=_ROSTER, memories=[], location="",
    )
    assert [r.about for r in verdict.reactions] == ["阿檀"]


def test_关系可以从一句闲话里长出来():
    """闸问的是「这个世界里真有这么个人吗」,不是「她认不认识他」。

    线上真踩:她听到一句关于林迟的闲话,而她跟林迟还没来往过 —— 于是林迟不在
    她的 `roster` 里,整条反应被丢掉,日志上写着「林迟不在名单上」,**而林迟就在
    这个世界里站着**。合着的时候这一层只能让已经认识的人之间的关系动,一段关系
    永远不可能从一句闲话里长出来 —— 而"我还没见过他,但我已经听说了他的事"
    恰恰是这个品类里最值钱的那一刻。
    """
    judge = RelationshipJudge(_FakeLLM(
        _verdict("她把这个没见过的名字记住了", {"about": "林迟", "delta": -0.12})
    ))
    verdict = judge.judge_hearsay(
        a={"name": "夏"}, rumor="听说林迟昨晚陪着谁到很晚",
        roster=_ROSTER, memories=[], location="",
        known=[*_ROSTER, "林迟"],
    )
    assert [r.about for r in verdict.reactions] == ["林迟"], (
        "她还没来往过的人被当成模型编的丢掉了"
    )


def test_放宽的只是谁算真人_编出来的名字照旧当场丢掉():
    """闸一个字没松 —— `known` 给全了,不在里面的仍旧翻不回任何 id。"""
    judge = RelationshipJudge(_FakeLLM(
        _verdict("她想起了一个不存在的人",
                 {"about": "楚夭夭", "delta": -0.2},
                 {"about": "林迟", "delta": -0.05})
    ))
    verdict = judge.judge_hearsay(
        a={"name": "夏"}, rumor="…", roster=_ROSTER, memories=[], location="",
        known=[*_ROSTER, "林迟"],
    )
    assert [r.about for r in verdict.reactions] == ["林迟"]


def test_a_zero_delta_does_not_become_an_event():
    judge = RelationshipJudge(_FakeLLM(
        _verdict("无所谓", {"about": "阿檀", "delta": 0.0})
    ))
    verdict = judge.judge_hearsay(
        a={"name": "夏"}, rumor="…", roster=_ROSTER, memories=[], location="",
    )
    assert verdict.reactions == (), "判了个 0 等于没判,不该为它发一条事件"


def test_the_reaction_count_is_capped():
    """一个话痨模型不许把整张名单写一遍 —— 那是一次全世界范围的关系重排。"""
    judge = RelationshipJudge(_FakeLLM(_verdict(
        "她把所有人都想了一遍",
        *({"about": name, "delta": -0.05} for name in _ROSTER),
    )))
    verdict = judge.judge_hearsay(
        a={"name": "夏"}, rumor="…",
        roster={**_ROSTER, "多余": 0.1}, memories=[], location="",
    )
    assert len(verdict.reactions) <= 3


def test_garbage_and_a_dead_llm_both_degrade_to_none():
    for reply in ("", "抱歉，我不能回答", '{"reactions": []}'):
        judge = RelationshipJudge(_FakeLLM(reply))
        assert judge.judge_hearsay(
            a={"name": "夏"}, rumor="…", roster=_ROSTER, memories=[], location="",
        ) is None, f"{reply!r} 不该被当成一次有效判定"


def test_without_a_model_she_simply_hears_it_and_moves_on():
    """**故意没有确定性替身。** 有的话它只能是"听到亲近的人的八卦就扣 0.05",
    而那正是这条机制存在要否定的东西 —— 白霜和「零」会得到同一个数字。"""
    assert DeterministicRelationshipJudge().judge_hearsay(
        a={"name": "夏"}, rumor="…", roster=_ROSTER, memories=[], location="",
    ) is None


# ── ③ 反应不许自己变成新的八卦 ─────────────────────────────────────────────


def test_an_inner_reaction_never_becomes_a_rumor():
    """`reaction` 是内心活动,和 `reflection` 同一条 —— 而这里它还承重。

    可传的话:她的反应变成一条 hop=0 的新记忆,再传一手又归零,于是「三手消亡」
    那条设计整个绕过去,一句闲话可以永远活下去 —— 每转一手还要花一次判定,
    也就是一次真金白银的模型调用。
    """
    assert "reaction" in PRIVATE_KINDS and "reflection" in PRIVATE_KINDS

    class _AlwaysRoll:
        def random(self) -> float:
            return 0.0

    memories = [
        {"kind": "reaction", "summary": "她心里堵得慌", "importance": 0.9},
        {"kind": "reflection", "summary": "内心独白", "importance": 0.95},
    ]
    assert pick_gossip(_AlwaysRoll(), "苏晚夏", memories, "柔") is None, "内心活动传出去了"


# ── ④ 端到端:世界里真的动了 ───────────────────────────────────────────────


class _ScriptedJudge:
    """只实现 `judge_hearsay` 的判定器 —— 其余判定这条测试不关心。"""

    def __init__(self, verdict) -> None:
        self.verdict = verdict
        self.calls: list[dict] = []

    def judge_hearsay(self, **kwargs):
        self.calls.append(kwargs)
        return self.verdict


def _run_hearsay(world, listener: str, rumor: str, judge) -> None:
    """走引擎真正那条路(`_submit_hearsay_judgment`),不是直接调 worker ——
    判定池、开关、名单快照全在那条路上。

    **等 Future,别关池。** 关掉池的话判定回来时那条 relabel 提交不进去,而那是
    这条测试自己造出来的形状 —— 生产里 `stop()` 是在锁里把池置空的。
    """
    world.scheduler.relationship_judge = judge
    future = world.scheduler._submit_hearsay_judgment(listener, rumor)
    if future is not None:
        future.result(timeout=10)


def _world_with_relations(tmp_path):
    world = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    world.config_set("social.enabled", True)
    world.config_set("social.hearsay_reaction.enabled", True)
    return world


def test_her_reaction_lands_as_a_real_relationship_change(tmp_path):
    """落点必须是**既有的那套机制**(`sentiment_delta`):跨档、图谱边、planner
    读到的东西全挂在它上面。另起一条路等于让她吃的醋只活在一个没人读的字段里。"""
    from anima_world.relationship_judge import HearsayReaction, HearsayVerdict

    world = _world_with_relations(tmp_path)
    with world:
        # 判定那一层只认识**名字**;名字从世界里读,不写死 —— 写死的话演示世界
        # 改一次名字,这条测试就变成在验一个不存在的人。
        yao_name = world.scheduler.agents["遥"].agent.name
        before = world.scheduler._memory_projection.relations.get(("夏", "遥"))
        before_value = before.sentiment if before else 0.0
        judge = _ScriptedJudge(HearsayVerdict(
            summary="夏心里堵得慌,面上什么也没说",
            reactions=(HearsayReaction(about=yao_name, delta=-0.15, axes={"trust": -0.1}),),
        ))
        _run_hearsay(world, "夏", f"听柔说:{yao_name}昨晚跟人在港街喝到很晚", judge)
        world.tick(1)

        assert judge.calls, "判定压根没被调用"
        roster = judge.calls[0]["roster"]
        assert yao_name in roster, f"名单里没有她认识的人:{roster}"
        assert "遥" not in roster, f"名单里漏进了 id —— 判定那一层不该碰得到 id:{roster}"

        after = world.scheduler._memory_projection.relations[("夏", "遥")]
        assert after.sentiment == pytest.approx(before_value - 0.15), "关系一点没动"
        assert after.trust == pytest.approx(-0.1)

        shifts = [
            e for e in world.events()
            if e["type"] == "state_change"
            and (e.get("payload") or {}).get("cause") == "hearsay"
        ]
        assert shifts, "事件日志上看不出这次变化是听人说的"
        assert shifts[0]["payload"]["target_name"] == yao_name
        assert shifts[0]["payload"]["target"] == "遥", "名字翻回 id 那一步没走"

        reactions = [m for m in world.memories("夏") if m["kind"] == "reaction"]
        assert len(reactions) == 1, "数字动了,而她说不出为什么 —— 下一轮就成了莫名其妙的冷淡"
        assert "堵得慌" in reactions[0]["summary"]


def test_判定器手里的地点也是人话_不是键名(tmp_path):
    """名单那一头早就不许漏 id 了(上一条的 `"遥" not in roster`),地点这一头漏着。

    判定器写出来的那句 `summary` **原样进她的长期记忆**,而喂给它的 `地点：{location}`
    一直是键名。线上读到的原文:「舒白回江渡录电台选题,两人在 cart 碰面」——
    `cart` 是键,那地方叫小念咖啡车。她从此记得自己在一个叫 cart 的地方见过人,
    八卦还会把这句话原样转述出去。
    """
    from anima_world.relationship_judge import HearsayVerdict

    world = _world_with_relations(tmp_path)
    with world:
        here = world.scheduler._where_is("夏")
        place = (world.scheduler.location_store.get(here) or {}).get("name")
        assert place and place != here, "演示世界的地点得有个跟 id 不一样的名字,否则这条验不出东西"

        judge = _ScriptedJudge(HearsayVerdict(summary="没什么感觉", reactions=()))
        _run_hearsay(world, "夏", "听柔说:昨晚港街很热闹", judge)

        assert judge.calls, "判定压根没被调用"
        assert judge.calls[0]["location"] == place, (
            f"判定器拿到的是键名:{judge.calls[0]['location']!r}"
        )


def test_not_caring_changes_nothing_at_all(tmp_path):
    from anima_world.relationship_judge import HearsayVerdict

    world = _world_with_relations(tmp_path)
    with world:
        before = dict(world.scheduler._memory_projection.relations)
        _run_hearsay(world, "夏", "听柔说:今天下雨了",
                     _ScriptedJudge(HearsayVerdict(summary="她没往心里去")))
        world.tick(1)
        assert world.scheduler._memory_projection.relations == before
        assert [m for m in world.memories("夏") if m["kind"] == "reaction"] == []


def test_the_switch_is_off_by_default_and_costs_nothing(open_world, bare_seed):
    """引擎默认值全关。关着时**连判定都不该被调用** —— 调了就是白花一次模型钱。

    ⚠️ 用 `bare_seed`(把橱窗剥回毛坯),不是演示世界:内置种子**点亮了**这个开关
    (橱窗的职责就是展示做过的东西)。拿它去验默认值等于在验橱窗的布置 ——
    第一版就是这么写的,而且它红得很对。
    """
    world = open_world(world_file=bare_seed)
    assert world.config_get("social.hearsay_reaction.enabled") is False
    judge = _ScriptedJudge(None)
    _run_hearsay(world, "夏", "听柔说:沈遥昨晚喝到很晚", judge)
    assert judge.calls == [], "开关关着还是调了判定"


def test_she_can_be_jealous_about_a_player_who_is_not_online(tmp_path):
    """**这条是这一层的主场景,而它最容易漏。**

    「秘密恋爱」里她最该吃醋的那句话是"他跟别人走得近",而"他"是玩家 —— 多半
    **不在线**。名字只从在场名单读的话,他一下线就从名单上消失,于是她永远吃不了
    关于他的醋,而世界照跑、日志干净:失效的样子和"她这次没往心里去"一模一样。

    `contact` 表在他每次跟她说话时就把名字记下来了,正是为这种场合准备的。
    """
    from anima_world.relationship_judge import HearsayReaction, HearsayVerdict

    world = _world_with_relations(tmp_path)
    with world:
        player = "8f3c-4a11-9d2e-b0c7"
        # 他来过、说过话、然后走了 —— 世界里只剩落库的那一行。
        world.scheduler.contact_store.note_contact(
            "夏", player, tick=world.scheduler.clock, name="阿檀",
        )
        world.scheduler._record_and_deliver({
            "type": "state_change", "who": "夏",
            "payload": {"kind": "sentiment_delta", "as": "夏", "target": player,
                        "delta": 0.5, "as_name": "苏晚夏", "target_name": "阿檀"},
        })
        assert player not in world.who_is_present(), "夹具前提没成立:他还在线"

        judge = _ScriptedJudge(HearsayVerdict(
            summary="夏盯着那杯凉掉的咖啡,没说话",
            reactions=(HearsayReaction(about="阿檀", delta=-0.13),),
        ))
        _run_hearsay(world, "夏", "听柔说:阿檀这几天天天往别处跑", judge)
        world.tick(1)

        assert judge.calls, "他不在线,判定压根没被提交"
        assert "阿檀" in judge.calls[0]["roster"], (
            f"他从名单上消失了 —— 她永远吃不了关于他的醋:{judge.calls[0]['roster']}"
        )
        after = world.scheduler._memory_projection.relations[("夏", player)]
        assert after.sentiment == pytest.approx(0.5 - 0.13), "名字翻不回他的 id"


def test_a_closing_judge_pool_does_not_abort_the_event_fold(tmp_path):
    """这一条是**吃醋这条路把它逼出来的**,而它本身与吃醋无关。

    跨档会顺手提交一次"改标签",而那次提交是从 `_record_event` **里面**发出去的 ——
    后半截才是把事件折进投影、写进记忆的地方。池正在关时 `submit` 抛
    `RuntimeError`,于是:事件已经发出去了,投影却没折 —— **日志和世界当场分叉,
    一声不吭**。标签本来就写着"跨档之上的点缀,失败了保持旧文本",所以它没有资格
    掀翻那条事件。
    """
    world = _world_with_relations(tmp_path)
    with world:
        pool = world.scheduler._judge_pool
        assert pool is not None
        pool.shutdown(wait=True)      # 池还挂在 scheduler 上,但已经不收活了

        before = world.scheduler._memory_projection.relations[("夏", "遥")].sentiment
        world.scheduler._record_and_deliver({
            "type": "state_change", "who": "夏",
            "payload": {"kind": "sentiment_delta", "as": "夏", "target": "遥",
                        "delta": 0.2, "as_name": "苏晚夏", "target_name": "陆知遥"},
        })
        after = world.scheduler._memory_projection.relations[("夏", "遥")].sentiment
        assert after == pytest.approx(before + 0.2), (
            "事件发出去了,投影却没折 —— 日志和世界分叉了"
        )
        assert any(m["kind"] == "relation_shift" for m in world.memories("夏")), (
            "记忆那一半也被同一个异常带掉了"
        )


def test_两份名单分得开_认识的进好感度_在场的只进翻译表(tmp_path):
    """`weights` 是给模型看的(名字配好感度),`ids` 是闸(这儿真有这么个人)。

    合成一份的话,她只可能对已经认识的人吃醋。分开之后:没来往过的人不进
    `weights`(她说不出好感度,编一个出来是引擎替她做主),但进 `ids` ——
    别人说起他不是编的。
    """
    world = _world_with_relations(tmp_path)
    with world:
        yao = world.scheduler.agents["遥"].agent.name
        rou = world.scheduler.agents["柔"].agent.name
        for key in [k for k in world.scheduler._memory_projection.relations if k == ("夏", "遥")]:
            del world.scheduler._memory_projection.relations[key]

        weights, ids = world.scheduler._hearsay_roster("夏")
        assert yao not in weights, "还没来往过的人不该带着一个编出来的好感度进提示词"
        assert ids.get(yao) == "遥", "他就在这个世界里站着,别人说起他不是编的"
        assert weights.get(rou) is not None and ids.get(rou) == "柔", "认识的那一半没了"
        assert "夏" not in ids.values(), "她自己不该在名单上"


def test_她对一个还没见过的人动了心思_而世界真的记下了(tmp_path):
    """端到端:反应落成真的 `sentiment_delta`,一段关系从此存在。"""
    from anima_world.relationship_judge import HearsayReaction, HearsayVerdict

    world = _world_with_relations(tmp_path)
    with world:
        yao = world.scheduler.agents["遥"].agent.name
        for key in [k for k in world.scheduler._memory_projection.relations if k == ("夏", "遥")]:
            del world.scheduler._memory_projection.relations[key]
        assert ("夏", "遥") not in world.scheduler._memory_projection.relations

        judge = _ScriptedJudge(HearsayVerdict(
            summary="夏没见过这个人,却先记住了这个名字",
            reactions=(HearsayReaction(about=yao, delta=-0.12),),
        ))
        _run_hearsay(world, "夏", f"听柔说:{yao}昨晚陪着谁到很晚", judge)
        world.tick(1)

        assert judge.calls, "判定压根没被提交"
        assert yao in (judge.calls[0].get("known") or ()), (
            f"闸那一半没把他算成真人:{judge.calls[0].get('known')}"
        )
        after = world.scheduler._memory_projection.relations.get(("夏", "遥"))
        assert after is not None, "一段关系没能从一句闲话里长出来"
        assert after.sentiment == pytest.approx(-0.12)


def test_a_stranger_hears_nothing_worth_reacting_to(tmp_path):
    """谁都不认识的人听不出弦外之音 —— 没有落点就不必花那次调用。"""
    from anima_world.relationship_judge import HearsayVerdict

    world = _world_with_relations(tmp_path)
    with world:
        world.scheduler._memory_projection.relations.clear()
        judge = _ScriptedJudge(HearsayVerdict(summary="…"))
        _run_hearsay(world, "夏", "听柔说:沈遥昨晚喝到很晚", judge)
        assert judge.calls == []
