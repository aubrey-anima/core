"""三轴关系必须**真的落地** —— 模型只吐 headline 的时候也要落。

## 这条测试文件为什么存在

2026-08-11 在线上那个唯一有真玩家的世界(「晚潮 · 江渡镇的雨季」,3095 条事件、
19 个世界日)对了一遍账:**二十条关系的 `trust` / `affection` / `respect` 全是
0.0**,只有 headline 的 `sentiment` 在动。

后果不是"少了三个数字",是整条特性链死了:`contact.closeness` 是
`0.6·sentiment + 0.25·affection + 0.15·trust`,三轴不动的话它最高只有
0.6 倍 sentiment —— 江晚对玩家的分数是 **0.15,门槛 0.25**,"他 9.5 天没出现了"
这条由头白攒着。**2.1.0 的招牌特性「她自己想起你」,在唯一有真玩家的世界上
一次都没发生过**,而表现和"今天没人想你"逐字相同。

## 而当年单测全绿

因为测试全跑在 `DeterministicRelationshipJudge` 上(`force_mock_llm=True` 的
默认判定器),**而它一直在派生三轴**(`{"trust": d/2, "affection": d,
"respect": 0}`)。真模型那条路上 `RelationshipJudge` 只誊抄模型给的 `axes_*`,
LongCat-2.0 从来不吐这两个键,于是 `_clamp_axes` 每次返回 `{}`,
`scheduler` 那侧 `if axes:` 一路跳过 —— 世界照跑,日志一行不错。

**"照跑但给错东西"的标本。** 所以这里的每一条都用一个**只吐 headline** 的假
模型(真模型的样子),而不是用默认的确定性判定器 —— 拿那个去验,验的是替身。
"""
from __future__ import annotations

import logging
import time

import pytest

from anima_world import contact
from anima_world.relationship_judge import (
    MAX_DELTA,
    DeterministicRelationshipJudge,
    RelationshipJudge,
)
from anima_world.types import Relation


# ── 假模型:真模型的样子 ────────────────────────────────────────────────────

class _ScriptedLLM:
    """按脚本回话。`complete_sync` 是判定器认识的唯一一个方法。"""

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies) or [""]
        self.calls = 0

    def complete_sync(self, messages):  # noqa: ARG002
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return reply


def _headline_only(a_to_b: float = 0.12, b_to_a: float = 0.08) -> str:
    """LongCat-2.0 在线上真的吐出来的形状:摘要 + 两个 delta,没有 axes_*。"""
    return (
        '{"summary": "他们聊了这场雨什么时候停", '
        f'"delta_a_to_b": {a_to_b}, "delta_b_to_a": {b_to_a}}}'
    )


def _judge(reply: str, **kwargs) -> RelationshipJudge:
    return RelationshipJudge(_ScriptedLLM(reply), **kwargs)


def _verdict(reply: str, **kwargs):
    result = _judge(reply, **kwargs).judge({}, {}, {}, [], [], "码头")
    assert result is not None, "这个回包本身该是可解析的,不然后面的断言没有意义"
    return result


# ── 一、模型不吐 axes 时,三轴也要动 ────────────────────────────────────────

def test_a_headline_only_verdict_still_moves_all_three_axes():
    """**线上那个世界失败的那一处。**

    模型只给了 summary 和两个 delta —— 这是真模型的常态,不是异常。三轴必须
    自己长出来,而不是留一个空 dict 让 `scheduler` 那侧 `if axes:` 跳过去。
    """
    result = _verdict(_headline_only(a_to_b=0.18, b_to_a=0.06))

    for axes in (result.axes_a_to_b, result.axes_b_to_a):
        assert set(axes) == {"trust", "affection", "respect"}, (
            "三个轴都要有结论 —— 缺一个键在下游就是「这个轴没动过」，而那正是这条 bug"
        )
    assert result.axes_a_to_b["affection"] > 0
    assert result.axes_a_to_b["trust"] > 0
    # 反方向也要落:玩家那一半走的是同一条路(`p1|夏` 这条关系)。
    assert result.axes_b_to_a["affection"] > 0


def test_no_axis_outruns_the_headline():
    """**别让三轴比 sentiment 跑得还快。**

    一次对话把 trust 拉满和三轴不动一样假。轴是骑在 headline 上的加细,
    所以每一轴的幅度都严格小于它 —— 这条是"幅度过得了脑子"的可执行形式。
    """
    for a_to_b in (0.2, 0.12, 0.03, -0.05, -0.2):
        result = _verdict(_headline_only(a_to_b=a_to_b, b_to_a=0.01))
        for axis, value in result.axes_a_to_b.items():
            assert abs(value) < abs(a_to_b) + 1e-9, f"{axis} 跑得比 headline 还快"
            assert abs(value) <= MAX_DELTA


def test_the_three_axes_are_not_the_headline_copied_three_times():
    """**派生必须讲得通,不能是把 headline 抄三份。**

    三个轴意思不同:trust 靠可靠/守诺/交底,affection 靠亲近/共处/示好,
    respect 靠本事/担当/见识。同一次聊天对三者的推动不该一样。
    """
    axes = _verdict(_headline_only(a_to_b=0.2)).axes_a_to_b
    assert len({round(v, 6) for v in axes.values()}) == 3, (
        f"三个轴给出了同一个数,那不是判断是复制:{axes}"
    )


def test_trust_falls_faster_than_it_grows():
    """**信任毁得比长得快。** 这是三轴之间第一条真正的不对称。

    一次交底只让人信你一点,一次失信却能一次性收回去 —— 而喜爱不是这个形状
    (讨厌一个人和喜欢一个人大体是同一种直感),所以只有 trust 有这条。
    """
    up = _verdict(_headline_only(a_to_b=0.16)).axes_a_to_b
    down = _verdict(_headline_only(a_to_b=-0.16)).axes_a_to_b

    assert abs(down["trust"]) > abs(up["trust"]), "信任该掉得比长得快"
    assert abs(down["affection"]) == pytest.approx(abs(up["affection"])), (
        "喜爱是对称的 —— 把每个轴都做成不对称就成了三条一样的规则"
    )


def test_respect_needs_a_conversation_that_mattered():
    """**寒暄长不出敬重。** 这是第二条不对称,而且是个门槛不是个系数。

    敬重靠本事、担当、见识 —— 一句"今天雨真大"里没有这三样中的任何一样。
    做成线性系数的话,一百次寒暄能把敬重堆到和一次救命之恩一样高。
    """
    small_talk = _verdict(_headline_only(a_to_b=0.02)).axes_a_to_b
    that_mattered = _verdict(_headline_only(a_to_b=0.2)).axes_a_to_b

    assert small_talk["respect"] == 0.0, "寒暄不该长敬重"
    assert small_talk["affection"] > 0, "但寒暄该长一点喜爱 —— 门槛只在敬重上"
    assert that_mattered["respect"] > 0, "触及双方在意的事之后,敬重才开始动"


# ── 二、派生是回落,不是覆盖 ────────────────────────────────────────────────

def test_the_model_wins_when_it_speaks():
    """模型愿意吐 axes 时**以模型的为准**,一个字都不改。

    连显式的 0 也作数:模型写 `"respect": 0` 是在说"敬重这次没动",那是一个
    判断;而整个键缺席才是"它没表态"。两者分得开,是这条回落不越权的全部依据。
    """
    result = _verdict(
        '{"summary": "他把账本摊开给她看了", "delta_a_to_b": 0.15, "delta_b_to_a": 0.1,'
        ' "axes_a_to_b": {"trust": 0.19, "affection": 0.01, "respect": 0.0},'
        ' "axes_b_to_a": {"trust": 0.05, "affection": 0.05, "respect": 0.05}}'
    )
    assert result.axes_a_to_b == {"trust": 0.19, "affection": 0.01, "respect": 0.0}
    assert result.axes_b_to_a == {"trust": 0.05, "affection": 0.05, "respect": 0.05}


def test_a_garbage_axes_block_falls_back_rather_than_landing_nothing():
    """模型把 axes 写坏了 —— 那正是最该回落的时候。

    ⚠️ 这条改了一个旧契约(`test_social.py::test_judge_parses_and_clamps_axes`
    原本钉的是"垃圾轴降级为无"):**降级成无就是这条 bug 的机制本身**。
    解析层照旧拒收坏值(不炸、不瞎猜),但一个轴都没落地时判定器要自己给一份。
    """
    result = _verdict(
        '{"summary": "聊了展览的事", "delta_a_to_b": 0.1, "delta_b_to_a": 0.05,'
        ' "axes_a_to_b": {"trust": 0.9, "affection": 0.02, "junk": 1},'
        ' "axes_b_to_a": "garbage"}'
    )
    # 这一路模型开了口:整份听它的(裁剪到 ±0.2、丢掉不认识的轴)。
    assert result.axes_a_to_b == {"trust": 0.2, "affection": 0.02}
    # 那一路一个轴都没落地:派生。
    assert set(result.axes_b_to_a) == {"trust", "affection", "respect"}
    assert result.axes_b_to_a["affection"] > 0


def test_a_dead_llm_still_produces_no_verdict_at_all():
    """回落补的是**轴**,不是判定。模型挂了照旧一个字都不写。"""
    assert _judge("这不是 JSON").judge({}, {}, {}, [], [], "码头") is None


# ── 三、听来的一句话推动的是另外两个轴 ──────────────────────────────────────

def test_hearsay_moves_what_you_think_he_is_more_than_how_close_you_feel():
    """八卦和聊天推的**不是同一组轴**,这是"派生讲得通"的第二个落点。

    别人嘴里的他改变的是"我以为他是个什么人"(信不信得过、看不看得起);
    而喜爱要靠共处 —— 一句闲话推不动多少。反过来写的话,一条八卦就能让人
    "更喜欢"一个从没见过的人。
    """
    verdict = _judge(
        '{"summary": "他听完心里咯噔一下", '
        '"reactions": [{"about": "阿檀", "delta": -0.12}]}'
    ).judge_hearsay({"name": "白霜"}, "阿檀昨天把定金卷走了", {"阿檀": 0.4}, [], "码头")

    assert verdict is not None and verdict.reactions
    axes = verdict.reactions[0].axes
    assert set(axes) == {"trust", "affection", "respect"}
    assert abs(axes["trust"]) > abs(axes["affection"]), "闲话动的是信任,不是亲近"
    assert abs(axes["respect"]) > abs(axes["affection"])
    assert axes["trust"] < 0, "方向跟着 delta 走"


def test_the_hearsay_model_still_wins_when_it_speaks():
    """和聊天那条同一条纪律 —— 模型给了轴就整份听它的。"""
    verdict = _judge(
        '{"summary": "他没往心里去，但记下了", '
        '"reactions": [{"about": "阿檀", "delta": -0.12, "axes": {"trust": -0.15}}]}'
    ).judge_hearsay({"name": "白霜"}, "闲话", {"阿檀": 0.4}, [], "码头")
    assert verdict is not None
    assert verdict.reactions[0].axes == {"trust": -0.15}


# ── 四、降级不许无声 ────────────────────────────────────────────────────────

def test_the_fallback_reports_itself_to_subsystem_health():
    """回落发生时要有痕迹,而且走的是这个仓库现成的那套(`note_subsystem`)。

    一条只在 stderr 刷一行的降级,和"这个机制没接上"在产物上长得一模一样 ——
    而线上那个世界正是这么无声地死了 19 个世界日。
    """
    seen: list[tuple[str, bool, str]] = []
    judge = _judge(_headline_only(), health=lambda *args: seen.append(args))
    judge.judge({}, {}, {}, [], [], "码头")

    assert seen, "回落了却没吭声"
    subsystem, ok, reason = seen[-1]
    assert subsystem == "relationship_axes"
    assert ok is False and reason, "降级要说出是哪一档、为什么"


def test_a_model_that_does_speak_reports_healthy():
    """反过来也要报 —— 只在降级时说话的观察窗看不出"它什么时候好了"。"""
    seen: list[tuple[str, bool, str]] = []
    judge = _judge(
        '{"summary": "s", "delta_a_to_b": 0.1, "delta_b_to_a": 0.1,'
        ' "axes_a_to_b": {"trust": 0.05, "affection": 0.05, "respect": 0.0},'
        ' "axes_b_to_a": {"trust": 0.05, "affection": 0.05, "respect": 0.0}}',
        health=lambda *args: seen.append(args),
    )
    judge.judge({}, {}, {}, [], [], "码头")
    assert seen and seen[-1][1] is True


def test_a_real_world_actually_wires_the_health_hook(tmp_path):
    """`health` 不是个没人接的钩子 —— 开一个配了 key 的世界,它必须已经接上
    `Scheduler.note_subsystem`。

    判定器建在 scheduler 之前(它不认识存储层),所以那一笔在
    `build_serve_scheduler` 里补;不钉这条的话,那一行哪天挪没了也没人知道,
    而后果正是这个仓库最怕的:**降级恢复成无声**。
    """
    from _worldfile import open_world_at

    db = str(tmp_path / "w.db")
    with open_world_at(db, force_mock_llm=True) as world:
        # 替身没有 `health` 这个属性,那一行必须认得出来并跳过(别给它硬塞一个)。
        assert not hasattr(world.scheduler.relationship_judge, "health")
        world.config_set("llm.api_key", "sk-not-a-real-key")
    with open_world_at(db) as world:
        judge = world.scheduler.relationship_judge
        assert isinstance(judge, RelationshipJudge)
        assert judge.health == world.scheduler.note_subsystem, (
            "真判定器上没接上健康钩子 —— 三轴回落又变回无声的了"
        )


def test_the_fallback_logs_once_not_every_call(caplog):
    """没接 `health` 时退回日志,而且**按边沿**报 —— 一个持续降级的子系统
    每次刷一行,日志会被自己的健康报告淹掉(`note_subsystem` 同一条)。"""
    judge = _judge(_headline_only())
    with caplog.at_level(logging.WARNING, logger="anima_world.relationship_judge"):
        for _ in range(5):
            judge.judge({}, {}, {}, [], [], "码头")
    hits = [r for r in caplog.records if "relationship_axes" in r.getMessage()]
    assert len(hits) == 1, f"降级要吭声,但只在档位切换那一下:{[r.getMessage() for r in hits]}"


# ── 五、确定性替身和真判定器用同一条规则 ────────────────────────────────────

def test_the_deterministic_stand_in_and_the_real_judge_derive_the_same_way():
    """两条路上的世界不该因为"配没配 key"而长成两个形状。

    此前替身自己写了一份(`{"trust": d/2, "affection": d, "respect": 0}`),
    而真判定器一份都没有 —— 两份判断迟早给出不同的答案,这次给出的是
    "有 key 的世界三轴全死"。
    """
    from anima_world.relationship_judge import CHAT_SHARES, derive_axes

    stand_in = DeterministicRelationshipJudge()._result("x", 0.0, 0.0)
    assert stand_in.axes_a_to_b == derive_axes(stand_in.delta_a_to_b, CHAT_SHARES)


# ── 六、端到端:这正是线上那个世界失败的地方 ────────────────────────────────

_A_DAY = 288  # tick,minutes_per_tick=5


def _await(condition, timeout: float = 5.0):
    """等一个跑在判定线程池上的结果;超时只是保险丝。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.01)
    return condition()


class _HeadlineOnlyLLM:
    """真模型的样子:摘要 + 两个 delta,**从不吐 axes_***。"""

    def complete_sync(self, messages):  # noqa: ARG002
        return (
            '{"summary": "他们在码头上站着说了会儿话", '
            '"delta_a_to_b": 0.14, "delta_b_to_a": 0.1}'
        )


def _talk(world, text, *, agent="夏", player="p1"):
    reply = "".join(world.chat(agent, [{"role": "user", "content": text}],
                               player_id=player, display_name="阿檀"))
    world.record_chat_turn(agent, player, [
        {"role": "user", "content": text},
        {"role": "assistant", "content": reply},
    ])


def test_a_player_conversation_lands_all_three_axes_end_to_end(open_world, bare_seed):
    """**这条最重要。** 走 `World.open` + `World.chat` 的真路径,判定器接的是
    一个只吐 headline 的模型 —— 也就是线上那个世界的配置。

    当年的单测全绿,是因为它们都直接调确定性替身或 `judge()`;没有一条走过
    "真判定器 + 真模型形状 + 真事件 + 真投影"这条全程。这条走。

    用毛坯世界而不是橱窗:验的是引擎自己的关系链,橱窗那一堆量与规律只会
    让这条测试因为别处的改动而莫名其妙地红。
    """
    world = open_world(world_file=bare_seed)
    world.scheduler.relationship_judge = RelationshipJudge(_HeadlineOnlyLLM())
    world.player_move("p1", "cafe")

    for day in range(3):
        _talk(world, f"第{day}天，我又来了")
        _await(lambda: world.state()["relations"].get("夏|p1", {}).get("affection"))
        world.scheduler.clock += _A_DAY

    rel = world.state()["relations"]["夏|p1"]
    assert rel["sentiment"] > 0, "headline 本来就没坏 —— 坏的是下面三条"
    assert rel["affection"] > 0, "线上那个世界的 affection 是 0.0,整整 19 个世界日"
    assert rel["trust"] > 0, "线上那个世界的 trust 是 0.0"
    # 反方向同样落(玩家不是特例,走同一套机制)。
    assert world.state()["relations"]["p1|夏"]["affection"] > 0
    # 而三轴仍然慢于 headline —— 聊三次不该把信任堆到和总好感一样高。
    assert rel["trust"] < rel["sentiment"] and rel["affection"] < rel["sentiment"]


def test_the_axes_ride_the_event_log_and_survive_a_reopen(open_world, bare_seed):
    """轴是骑在 `sentiment_delta` 事件上的,所以重开世界要能重折出来。

    投影是进程内的派生数据(有意的),真相在事件日志里 —— 如果轴只活在这个
    进程的内存里,换一次镜像它们就回 0,而且不报错。
    """
    world = open_world("axes_replay", world_file=bare_seed)
    world.scheduler.relationship_judge = RelationshipJudge(_HeadlineOnlyLLM())
    world.player_move("p1", "cafe")
    _talk(world, "我叫阿檀")
    _await(lambda: world.state()["relations"].get("夏|p1", {}).get("trust"))
    before = world.state()["relations"]["夏|p1"]
    world.close()

    again = open_world("axes_replay", world_file=bare_seed)
    after = again.state()["relations"]["夏|p1"]
    assert after["trust"] == pytest.approx(before["trust"]), "重开之后信任回零了"
    assert after["affection"] == pytest.approx(before["affection"])
    assert after["trust"] > 0


# ── 七、算给线上那个世界看:门槛过得了吗 ────────────────────────────────────

def test_the_live_world_shape_gets_much_closer_to_the_contact_gate():
    """**照线上那条关系的真数据算一遍。**

    `test_contact.py::test_a_world_where_only_sentiment_moves_can_still_reach_the_gate`
    钉的是"三轴全死时也要过得了 `min_closeness`",那是**止血**;这条钉的是
    止血之后伤口有没有长上 —— 同一个 sentiment 下,closeness 该实打实地涨,
    因为 `contact` 的三轴权重里有 0.4 是给 affection + trust 的。
    """
    from anima_world.relationship_judge import CHAT_SHARES

    sentiment = 0.668           # 线上最高的那一条:bai-shuang → <玩家>
    was = contact.closeness(Relation(sentiment=sentiment))
    # 派生对 delta 是线性的(respect 那道门槛之外),所以整段历史攒下来的轴
    # = 份额 × 攒下来的 sentiment。
    now = contact.closeness(Relation(
        sentiment=sentiment,
        affection=sentiment * CHAT_SHARES.affection,
        trust=sentiment * CHAT_SHARES.trust_gain,
    ))
    assert now > was * 1.35, f"closeness 只从 {was:.3f} 涨到 {now:.3f} —— 不够抵事"
    assert now < sentiment, "也不该涨到「三轴就是 sentiment 本身」那种程度"
    print(f"\n线上那条关系(sentiment={sentiment}):closeness {was:.4f} → {now:.4f} "
          f"(×{now / was:.3f});contact 的分数按同一倍数放大")


# ── 只推一个轴的判定,不许被 headline 那道闸连轴一起丢掉 ──────────────────────


def test_只动信任的判定不会被_headline_的闸丢掉():
    """判定器现在会说"没多喜欢他,但信了他一点"(守约、交底、还钱)。

    落库前那道闸原本只看 headline:`abs(delta) < 0.01 → continue`。三轴落地之后
    它就站错了地方 —— 一次 sentiment 几乎不动、trust 动 0.15 的判定会被**整行
    连轴一起**丢掉,而且一声不吭。世界里"守约"于是等于没发生过。
    """
    from anima_world.scheduler import _is_noise

    assert _is_noise(0.004, {}), "两边都可忽略,才算噪声"
    assert _is_noise(0.004, {"trust": 0.002, "affection": 0.0}), "轴也小,仍是噪声"
    assert not _is_noise(0.004, {"trust": 0.15}), "只动信任的一次互动被丢掉了"
    assert not _is_noise(0.004, {"respect": -0.2}), "只掉敬重的一次互动被丢掉了"
    assert not _is_noise(0.05, {}), "headline 够大照旧要写"
