"""一起做事 —— 三条红线,一条一节。

`interact` 一直是单人的("一个人、一个东西、一个瞬间")。于是两个站在同一个地方的人
只能各干各的,而世界记不下"他们一起吃了顿饭" —— 而**共同经历正是关系的主要来源**。
在这之前关系只有两条来路:说了多少句话、听说了什么,两条都是语言。

守的三条:

1. **别人凭什么答应。** 必须有一条**由性格决定**的拒绝路径。写成"关系够近就答应"的话,
   白霜和「零」会得到同一个答案,而世界照跑、日志一行不错。
2. **代价对每个人各扣一次,顺序不许有意义。** 名单顺序决定谁做得成的话,那是一条没有
   任何人写下过的规则。
3. **关系变化是这段经历的效果,不是再调一次判定。** 再调一次的话,"一起过了一夜"和
   "多聊了两句"又回到同一个入口上 —— 而这一层存在的全部理由就是它们不该一样。
"""
from __future__ import annotations

import json

import pytest

from anima_world import together
from anima_world.ontology import OntologyError, parse_kinds
from anima_world.relationship_judge import DeterministicRelationshipJudge, InviteVerdict


# ── ① 别人凭什么答应 ────────────────────────────────────────────────────────


class _Relation:
    def __init__(self, sentiment=0.0, trust=0.0, affection=0.0, respect=0.0):
        self.sentiment, self.trust = sentiment, trust
        self.affection, self.respect = affection, respect


def test_同一个邀请两个人给出不同答案_而差别只在性格上():
    """这一节就是红线 ①。**两条路都要能分开他们**,因为默认状态是没有 key。

    没有判定器时,性格通过**世界长出来的关系**和**作者声明的量**进来 —— 别扭的人
    好感爬得慢,而「随和」是作者写在 `kinds.agent.quantities` 里的。
    """
    别扭 = together.Invitee(
        id="白霜", relation=_Relation(sentiment=0.30), agreeableness=0.35,
    )
    坦荡 = together.Invitee(
        id="零", relation=_Relation(sentiment=0.30), agreeableness=1.6,
    )
    a = together.decide_alone(别扭, min_willingness=0.25)
    b = together.decide_alone(坦荡, min_willingness=0.25)
    assert not a.accepted and b.accepted, (
        "同样的关系、同样的邀请,两个人给出同一个答案 —— 那就是引擎替角色做主了"
    )
    assert a.reason == together.DECLINE_REASON and a.source == "willingness"


def test_硬闸判在性格之前_而且说得出是哪一条():
    """**世界的状态不是她的意思。** 一句笼统的"她没答应"会让玩家以为被拒绝的是这个
    人,而真正的原因可能只是他在赶路 —— 那种误解在世界里是改不回来的。"""
    睡着了 = together.Invitee(
        id="遥", gate="asleep", relation=_Relation(sentiment=0.9), agreeableness=2.0,
    )
    decided = together.decide_alone(睡着了)
    assert not decided.accepted
    assert decided.source == "gate" and decided.reason == "asleep"
    assert "睡" in decided.explain(), "说不出是哪一条硬闸,和「他不想」就分不开了"


def test_三个因子各自都挡得住_所以是乘法不是加法():
    """一个跟你毫无来往的人,再随和也不会跟你坐下来吃饭。"""
    assert together.willingness(None, agreeableness=2.0) == 0.0
    close = _Relation(sentiment=1.0, affection=1.0, trust=1.0)
    assert together.willingness(close, agreeableness=0.0) == 0.0
    assert together.willingness(close, agreeableness=1.0, stance="avoid") == pytest.approx(
        together.STANCE_FACTORS["avoid"], rel=1e-6
    )


def test_stance_关着的世界这一格是_1_0():
    """和 perception / ontology 逐字同构:声明本身就是开关,不声明行为逐位不变。"""
    close = _Relation(sentiment=0.8)
    assert together.willingness(close, stance="") == together.willingness(
        close, stance="neutral"
    )


def test_没有判定器时不假装判断_而是把判断让回给世界():
    """`DeterministicRelationshipJudge.judge_invite` 恒为 None。

    这里**故意**不给替身,而 `judge` 那一格给了 —— 差别不在难易,在替身归谁写:
    "他肯不肯"的替身早就写在世界里了(关系、声明的量、姿态)。在判定器里再写一份
    "关系够近就答应",就成了引擎手里的第二份判断。
    """
    assert DeterministicRelationshipJudge().judge_invite(a={}, inviter="夏") is None


def test_判定器读得到人设_而且不肯就是不肯():
    """有 key 时走的是这一条:读她是谁,给一个 accept + 一句理由。"""
    seen = {}

    class _LLM:
        def complete_sync(self, messages):
            seen["prompt"] = messages[0]["content"]
            return json.dumps({"accept": False, "reason": "这会儿不想跟人待着"},
                              ensure_ascii=False)

    from anima_world.relationship_judge import RelationshipJudge

    verdict = RelationshipJudge(_LLM()).judge_invite(
        a={"name": "白霜", "personality": "别扭,嘴上不认"},
        inviter="苏晚夏",
        invitation="苏晚夏叫你一起在门口那棵老橡树下小坐",
        relation={"a_to_b": 0.4}, memories=[], location="cafe",
    )
    assert verdict == InviteVerdict(accept=False, reason="这会儿不想跟人待着")
    assert "白霜" in seen["prompt"] and "别扭" in seen["prompt"], (
        "判定读不到他是谁,那就判不出因人而异"
    )
    assert "邀请是" in seen["prompt"]


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("是", True), ("false", False), ("不", False),
])
def test_判定器把字符串的_accept_也认下来(raw, expected):
    """模型写 `"accept": "true"` 的概率不低,而严格拒绝的话这一次判定会静默退回
    确定性那条路 —— 她的性格白读了,**而且看不出来**。"""
    class _LLM:
        def complete_sync(self, messages):
            return json.dumps({"accept": raw, "reason": "x"}, ensure_ascii=False)

    from anima_world.relationship_judge import RelationshipJudge

    verdict = RelationshipJudge(_LLM()).judge_invite(
        a={"name": "零"}, inviter="夏", invitation="一起吃饭",
        relation={}, memories=[], location="",
    )
    assert verdict is not None and verdict.accept is expected


def test_读不懂的回包退回_None_而不是猜一个():
    class _LLM:
        def complete_sync(self, messages):
            return "我觉得可以吧"

    from anima_world.relationship_judge import RelationshipJudge

    assert RelationshipJudge(_LLM()).judge_invite(
        a={}, inviter="", invitation="", relation={}, memories=[], location="",
    ) is None


# ── ② 顺序不许有意义 ────────────────────────────────────────────────────────


def test_名单顺序不影响关系账():
    """红线 ② 的一半。`pair_deltas` 只读快照,谁先算谁后算都一样,而且**返回值有序** ——
    重放两次要得到逐字相同的历史。"""
    current = {("a", "b"): 0.2, ("b", "a"): -0.1, ("a", "c"): 0.0,
               ("c", "a"): 0.5, ("b", "c"): 0.3, ("c", "b"): 0.0}
    one = together.pair_deltas(["a", "b", "c"], current, duration_ticks=6)
    two = together.pair_deltas(["c", "a", "b"], current, duration_ticks=6)
    assert one == two


def test_每一对有序对各一条_而且是有向的():
    deltas = together.pair_deltas(
        ["a", "b"], {("a", "b"): 0.0, ("b", "a"): 0.9}, duration_ticks=12,
    )
    assert {(d.who, d.about) for d in deltas} == {("a", "b"), ("b", "a")}
    forward = next(d for d in deltas if d.who == "a")
    backward = next(d for d in deltas if d.who == "b")
    assert forward.delta > backward.delta, (
        "按剩余空间衰减:已经很熟的那一头该涨得更少,不然一起吃一百顿饭能顶到 +1.0"
    )


# ── ③ 关系变化是经历的效果 ──────────────────────────────────────────────────


def test_一起做过的事比说过的话更算数():
    """**这一条就是这一批需求要治的病。** 哪天有人把这两个数字调反,这里会红。"""
    assert together.DEFAULT_RELATION_STEP > DeterministicRelationshipJudge.STEP, (
        "一起经历过的事,应该比说过多少句话更能改变关系"
    )


def test_人数越多每一对越淡():
    two = together.experience_delta(current=0.0, party_size=2, duration_ticks=12)
    ten = together.experience_delta(current=0.0, party_size=10, duration_ticks=12)
    assert two > ten * 2, "十个人的宴会不该让每一对都长出一顿双人晚饭的分量"


def test_一下子的事拿一半_不是零():
    """一起看一眼雪也是一起看过。归零的话,所有 `duration: 0` 的共同活动在关系上
    等于没发生 —— 而作者会以为自己声明了一件共同的事。"""
    assert together.duration_factor(0) == pytest.approx(0.5)
    assert together.duration_factor(9999) == pytest.approx(1.0)
    assert together.experience_delta(current=0.0, party_size=2, duration_ticks=0) > 0


def test_敬重只有一起把事做完才长得出来():
    """这个引擎里 `respect` 从来没动过一次(线上二十条关系全是 0),不是因为它不重要,
    是因为此前没有任何机制会写它。聊天那条给的是 `respect: 0`。"""
    axes = together.experience_axes(0.06)
    assert axes["respect"] > 0
    chat_axes = DeterministicRelationshipJudge()._result("x", 0.0, 0.0).axes_a_to_b
    assert chat_axes["respect"] == 0.0


def test_判了个零就别发那条事件():
    """和 `judge_hearsay` 里 `abs(delta) < 0.01` 那一句同一条。"""
    assert together.pair_deltas(
        ["a", "b"], {("a", "b"): 1.0, ("b", "a"): 1.0}, duration_ticks=12,
    ) == []


# ── schema:声明本身就是开关,坏声明当场开不了机 ──────────────────────────


def _kind(participants):
    return [{
        "id": "bench", "quantities": {"x": 1.0},
        "affordances": {"同坐": {"label": "一起坐会儿", "participants": participants}},
    }]


def test_不写_participants_的能力和从前逐位相同():
    kinds = parse_kinds([{"id": "bench", "affordances": {"look": {}}}])
    assert kinds["bench"].affordances["look"].participants is None
    assert kinds["bench"].affordances["look"].is_joint is False


def test_声明了就编译出来():
    kinds = parse_kinds(_kind({"min": 1, "max": 3}))
    spec = kinds["bench"].affordances["同坐"].participants
    assert (spec.minimum, spec.maximum) == (1, 3)
    assert spec.accepts(1) and spec.accepts(3) and not spec.accepts(4)


def test_max_不写就等于_min():
    assert parse_kinds(_kind({"min": 2}))["bench"].affordances["同坐"].participants.maximum == 2


def test_没有_consent_这个开关_而且要点它的名():
    """给作者一个"不用问对方"等于把红线 ① 整个交回去关掉。**静默忽略更坏** ——
    作者会以为自己关掉了同意,而世界照旧一个个去问。"""
    with pytest.raises(OntologyError) as exc:
        parse_kinds(_kind({"min": 1, "consent": False}))
    assert "consent" in str(exc.value)
    assert "取消对方的意志" in str(exc.value)


def test_min_写_0_开不了机():
    """一个人做不成「一起」。静默当成单人的话,作者会以为自己已经表达过了。"""
    with pytest.raises(OntologyError) as exc:
        parse_kinds(_kind({"min": 0}))
    assert "至少是 1" in str(exc.value)


def test_人数有上限_因为关系事件是平方进去的():
    with pytest.raises(OntologyError) as exc:
        parse_kinds(_kind({"min": 1, "max": 99}))
    assert "平方" in str(exc.value)


def test_坏声明一次列全_不是报一条就停():
    with pytest.raises(OntologyError) as exc:
        parse_kinds([{
            "id": "bench",
            "affordances": {
                "a": {"label": "甲", "participants": {"min": 0}},
                "b": {"label": "乙", "participants": {"min": 1, "max": 99}},
            },
        }])
    text = str(exc.value)
    assert "affordances.a" in text and "affordances.b" in text


def test_她的提示词里就说得出这件事得有人一起():
    """不说的话她会一个人去试,每次都收到"这件事得有人一起做" —— 而提示词里那几个字
    正是引擎自己写给她的。一个只在调用之后才说得出前提的能力,和声明了没人兑现同一种坏。"""
    from anima_world.ontology import Entity, Ontology

    kinds = parse_kinds(_kind({"min": 1}))
    ontology = Ontology(kinds=kinds, entities={
        "bench:a": Entity(id="bench:a", kind="bench", name="长椅"),
    })
    _, verbs = ontology.describe("bench:a")
    assert verbs == ["一起坐会儿(得有人一起)"]
    # 她照着念出来的就是那几个字 —— 引擎自己写下的注解,不该由她负责剥掉。
    assert ontology.affordance_of("bench:a", "一起坐会儿(得有人一起)") is not None
    assert ontology.affordance_of("bench:a", "一起坐会儿") is not None


def test_判定模板里_reason_排在_accept_前面():
    """**JSON 的字段顺序就是推理顺序**,而这一条只有接真模型才看得见。

    第一版把回包写成 `{"accept": …, "reason": …}`。本机 gemma4:26b 实测:

        白霜(人设:被邀请的第一反应永远是推掉)
          → accept=true, reason=「虽然很麻烦，但又想弄清楚发生了什么。」

    **理由分明是拒绝,布尔值却是同意** —— 自回归生成先写哪个字段,哪个字段就是
    "想都没想就填的那个"。调个个儿之后同一个模型、同一份人设,三次跑全是
    `accept=false`。所以模板里"两个字段的顺序不要换"那句是承重的,不是格式洁癖。

    单测全绿、只有真模型才暴露 —— 这一条钉的就是那种坏法的门。
    """
    from anima_world.relationship_judge import _DEFAULT_INVITE_PROMPT

    assert _DEFAULT_INVITE_PROMPT.index('"reason"') < _DEFAULT_INVITE_PROMPT.index('"accept"')
    assert "顺序不要换" in _DEFAULT_INVITE_PROMPT
    assert "推掉是一个完全正常的回答" in _DEFAULT_INVITE_PROMPT, (
        "少了这一句,模型会觉得自己被要求成全一件事 —— 而这条机制的全部意义"
        "就是它可以被拒绝"
    )
