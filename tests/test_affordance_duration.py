"""做一件事要花的**那段时间**,以及作者自己声明的动词。

这一层补的洞很具体:在它之前,引擎能表达的代价只有两种 —— 扣一个量(`costs`)、
花掉几样东西(`consumes`)。两样都能靠"睡一觉就回来"绕开,所以**没有任何东西
拦得住她一天做一百件事**。而"十月怀胎"之所以是一道真的闸,不是因为它贵,
是因为它长:一段时间过不去就是过不去,没有哪个量能替她把它熬完。

三条纪律,每条都对着一种具体的坏法:

- **代价当场付,效果到点落。** 付在收尾的话,起个头再放弃就是免费的 ——
  一个可以随时反悔且不留痕的承诺不是承诺。
- **关口只在起头查。** 付了十个月的代价再被一句"这会儿不行"拒掉,她没有任何
  办法预防那次失败,而预防不了的失败教不会她任何东西。
- **占用是一件事的属性,不是她的状态。** 做椅子占用她,怀胎不占用 —— 两者都要
  花十个月,而"这期间她还能不能干别的"才是代价的真实形状。

顺带修掉的是一个真 bug:排班里一个 30 分钟窗口的 `interact`,在 5 分钟一 tick 的
世界里会**做 6 遍**(`_emit_on_transition` 有意把 `interact` 从当前动作里弹掉)。
给它一个覆盖那段窗口的 `duration`,她就只做一遍、做满整段时间。
"""

from __future__ import annotations

import json

import pytest

from anima_world.ontology import (
    BUILTIN_AFFORDANCE_LABELS,
    OntologyError,
    apply_affordance,
    finish_affordance,
    parse_kinds,
)

ACTOR = {"id": "agent", "quantities": {"体力": {"default": 100, "visibility": "self"}}}

BENCH = {
    "id": "bench",
    "gloss": "一条长凳",
    "quantities": {"成色": {"default": 1.0, "visibility": "here"}},
    "affordances": {
        "打磨": {
            "duration": 3,
            "requires": ["me_体力 >= 20"],
            "costs": {"体力": "me_体力 - 20"},
            "set": {"成色": "成色 + 1"},
        },
    },
}


def _bench(**overrides):
    spec = {**BENCH, "affordances": {"打磨": {**BENCH["affordances"]["打磨"], **overrides}}}
    return parse_kinds([ACTOR, spec])["bench"].affordances["打磨"]


def _bad(spec, verb="打磨"):
    """把一段能力声明喂进去,把报错的原文拿出来。"""
    with pytest.raises(OntologyError) as excinfo:
        parse_kinds([ACTOR, {**BENCH, "affordances": {verb: spec}}])
    return str(excinfo.value)


# ── 动词:从闭集退成默认词表 ───────────────────────────────────────────────
#
# 它一度是十个词的闭集,理由写着"效果终归由引擎实现"。那条理由在 set/costs/
# consumes 落地之后就不成立了 —— `apply_affordance` 从头到尾没有一处按动词分支。
# 于是闭集买到的只剩"拼错当场报错"和"一张中文词表",而这两样都不需要枚举。


def test_自造的动词声明得出来():
    """`酿` 不在引擎的词表里,而这个世界里它是一个真动词。"""
    kind = parse_kinds([ACTOR, {
        "id": "vat", "quantities": {"酒": {"default": 0.0}},
        "affordances": {"酿": {"set": {"酒": "酒 + 1"}}},
    }])["vat"]
    assert "酿" in kind.affordances
    assert kind.affordances["酿"].label == "酿", "中文动词自己就是人话"


def test_英文的自造动词必须给一行人话():
    """她提示词里读到的是那几个字。"你可以对它:端详、brew"里的 brew 是噪音,
    而她还得照着它行动 —— 这条闸就是那句注释的实现。"""
    message = _bad({}, verb="brew")
    assert "label" in message and "brew" in message


def test_给了人话的英文动词过得去():
    kind = parse_kinds([ACTOR, {
        "id": "vat", "affordances": {"brew": {"label": "酿酒"}},
    }])["vat"]
    assert kind.affordances["brew"].label == "酿酒"


def test_内置的十个词照旧自带中文():
    """放开动词不许把老世界的人话弄丢 —— 一个只写 `["tend"]` 的世界,
    她读到的仍然得是"照料"。"""
    kind = parse_kinds([ACTOR, {"id": "vat", "affordances": ["tend", "look"]}])["vat"]
    assert kind.affordances["tend"].label == "照料"
    assert set(BUILTIN_AFFORDANCE_LABELS) >= {"tend", "look"}


@pytest.mark.parametrize("verb", ["", "两个 词", "tree:oak", " "])
def test_动词的形状照样有闸(verb):
    """放开的是词,不是纪律。带冒号的尤其要挡:冒号是实例 id 的分隔符。"""
    assert "形状" in _bad({}, verb=verb)


def test_人话不能是空的():
    assert "空" in _bad({"label": "  "}, verb="brew")


def test_她读到的是声明里那几个字():
    """`describe()` 从前查的是引擎的一张表,于是作者造得出的动词那里一个字
    也读不到 —— 声明了却进不了提示词,等于没声明。"""
    from anima_world.ontology import parse_entities, resolve

    kinds = parse_kinds([ACTOR, {
        "id": "vat", "gloss": "一口大缸",
        "affordances": {"brew": {"label": "酿酒"}, "look": {}},
    }])
    entities = parse_entities([{"id": "vat:a", "name": "那口"}], kinds)
    gloss, verbs = resolve(kinds, entities).describe("vat:a")
    assert gloss == "一口大缸"
    assert set(verbs) == {"酿酒", "端详"}, "读到 brew 的话她会照着念一个英文词"


def test_人话也调得动():
    """她照着提示词说"照料",引擎回一句"不认识这个动词"是最蠢的一种失败 ——
    那几个字本来就是引擎写给她的。"""
    from anima_world.ontology import parse_entities, resolve

    kinds = parse_kinds([ACTOR, BENCH])
    entities = parse_entities([{"id": "bench:a", "name": "那条"}], kinds)
    ontology = resolve(kinds, entities)
    assert ontology.affordance_of("bench:a", "打磨") is not None
    kinds2 = parse_kinds([ACTOR, {"id": "vat", "affordances": {"brew": {"label": "酿酒"}}}])
    entities2 = parse_entities([{"id": "vat:a"}], kinds2)
    ontology2 = resolve(kinds2, entities2)
    assert ontology2.affordance_of("vat:a", "酿酒").verb == "brew"


# ── duration / occupies 的声明 ─────────────────────────────────────────────


def test_不声明时长的能力还是一下子的事():
    """默认必须是 0 —— 声明本身就是开关,不写的世界逐位如旧。"""
    plain = parse_kinds([ACTOR, {"id": "vat", "affordances": ["look"]}])["vat"]
    assert plain.affordances["look"].duration == 0
    assert plain.affordances["look"].is_process is False


def test_声明了时长就是个过程():
    assert _bench().duration == 3
    assert _bench().is_process is True
    assert _bench().occupies is True, "占用是默认 —— 不占用的那种才是特例"


@pytest.mark.parametrize("bad", [-1, 0.5, "3", True, None])
def test_时长只收非负整数(bad):
    """和 `consumes` 同一条:tick 是可数的。收 2.5 要么悄悄取整、要么引出一套
    半 tick 的语义,两条路都比不许坏。"""
    assert "duration" in _bad({**BENCH["affordances"]["打磨"], "duration": bad})


def test_占用只收真假():
    assert "occupies" in _bad({**BENCH["affordances"]["打磨"], "occupies": "yes"})


def test_一下子的事不许声明占用():
    """声明了却什么也不改 = 一句谎。作者写下 `occupies` 时想的是"这段时间她在忙",
    而没有 duration 就没有那段时间 —— 静默无效的话他会以为自己表达过了。"""
    message = _bad({"occupies": True})
    assert "occupies" in message and "duration" in message


def test_不认识的字段照旧一次列全():
    assert "durations" in _bad({"durations": 3})


# ── finish_affordance:收尾那一下 ───────────────────────────────────────────


def test_收尾不再查关口():
    """付了代价、占了十个月,到头来被一句"你做不了"拒掉的话,她没有任何办法
    预防那次失败 —— 而预防不了的失败教不会她任何东西,只会让长过程变成赌博。"""
    affordance = _bench()
    exhausted = {"体力": 0.0}          # requires 要 20,这会儿她一点力气都没有
    assert not apply_affordance(affordance, values={"成色": 1.0},
                                me_values=exhausted).ok
    done = finish_affordance(affordance, values={"成色": 1.0}, me_values=exhausted)
    assert done.ok and done.updates == {"成色": 2.0}


def test_收尾读的是此刻的值不是起头的快照():
    """这条凳子在这三个 tick 里被别人也动过 —— 拿起头的快照算等于把这段时间
    的世界整个抹掉,而那正是长过程唯一存在过的证据。"""
    done = finish_affordance(_bench(), values={"成色": 9.0}, me_values={"体力": 100.0})
    assert done.updates == {"成色": 10.0}


def test_收尾算不出来是报错不是崩():
    """运行期降级:一次算不出来的收尾不该掀翻这一轮,但绝不无声。"""
    # 运行期真炸得起来的样子:量的行还没落地,于是 `成色` 这个名字查不到。
    broken = finish_affordance(_bench(), values={}, me_values={"体力": 100.0})
    assert not broken.ok and broken.reason == "error"


def test_收尾不碰她身上的量也不碰东西():
    """代价在起头付过了。收尾再扣一遍的话,一件做了十个月的事会收两次费。"""
    done = finish_affordance(_bench(), values={"成色": 1.0}, me_values={"体力": 100.0})
    assert done.me_updates == {} and done.consumed == {}


# ── 世界里跑起来 ──────────────────────────────────────────────────────────


def _slow_world(tmp_path, open_world, *, duration=3, occupies=True, name="slow"):
    # `occupies` 只在有时长时写得下去 —— 一下子的事没有「这期间」可占用。
    timing = {"duration": duration} if duration <= 0 else {
        "duration": duration, "occupies": occupies,
    }
    bench = {
        **BENCH,
        "affordances": {
            "打磨": {**BENCH["affordances"]["打磨"], **timing},
            # 一件一下子就完的事,用来问"她这会儿腾不腾得出手"
            "look": {},
            "擦": {"set": {"成色": "成色 + 0.1"}},
        },
    }
    seed = {
        "locations": [{"id": "cafe", "name": "咖啡店", "description": "拐角那家"}],
        "agents": [{"id": "甲", "name": "甲", "location": "cafe", "personality": "安静"}],
        "kinds": [ACTOR, bench],
        "entities": [{"id": "bench:a", "name": "那条长凳", "location": "cafe"}],
    }
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return open_world(seed_path=str(path))


def test_起头时代价当场付而效果一点没落(tmp_path, open_world):
    """付在收尾的话,起个头再放弃就是免费的 —— 而一个可以随时反悔且不留痕的
    承诺不是承诺。"""
    world = _slow_world(tmp_path, open_world)
    started = world.scheduler.perform_affordance("甲", "bench:a", "打磨")
    assert started["ok"] and started["started"] is True
    assert started["duration"] == 3
    assert world.stocks("agent:甲")["体力"] == 80.0, "力气这会儿就该没了"
    assert world.stocks("bench:a")["成色"] == 1.0, "凳子还一下没被磨过"


def test_到点了效果才落地(tmp_path, open_world):
    world = _slow_world(tmp_path, open_world)
    world.scheduler.perform_affordance("甲", "bench:a", "打磨")
    world.tick(2)
    assert world.stocks("bench:a")["成色"] == 1.0, "还差一个 tick 就不该完"
    world.tick(1)
    assert world.stocks("bench:a")["成色"] == 2.0


def test_做完了才发那条交互事件(tmp_path, open_world):
    """历史里要分得出"她起了个头"和"这件事成了"。合成一条的话,一个做到一半
    的世界在账上看起来和做完了一模一样。"""
    world = _slow_world(tmp_path, open_world)
    world.scheduler.perform_affordance("甲", "bench:a", "打磨")
    types = [e["type"] for e in world.events()]
    assert "entity_engage" in types and "entity_interaction" not in types
    world.tick(3)
    done = [e for e in world.events() if e["type"] == "entity_interaction"][-1]
    assert done["payload"]["changed"] == {"成色": 2.0}
    assert done["payload"]["duration"] == 3


def test_代价只记一遍(tmp_path, open_world):
    """起头那条 `entity_engage` 记了力气,收尾那条就不能再记一遍 ——
    按事件重算"她今天花了多少力气"会得到两倍,而两份账里没有哪份看着不对。"""
    world = _slow_world(tmp_path, open_world)
    world.scheduler.perform_affordance("甲", "bench:a", "打磨")
    world.tick(3)
    charged = [e for e in world.events()
               if e.get("payload", {}).get("cost")]
    assert len(charged) == 1 and charged[0]["type"] == "entity_engage"


def test_忙着的时候别的事做不了而且理由自成一类(tmp_path, open_world):
    """`busy` 是**第四类**拒绝,不是硬塞进前三类里的一种。判据一直是"她接下来
    该干什么"不一样:conditions 该换一棵,incapable 该去补足,而 busy 两样都不该 ——
    她该等自己手上这件做完。塞进 conditions 的话,一个正在做椅子的人会挨棵树问
    过去,每棵都回她"这会儿不行",而真正的原因跟树一点关系没有。"""
    world = _slow_world(tmp_path, open_world)
    world.scheduler.perform_affordance("甲", "bench:a", "打磨")
    blocked = world.scheduler.perform_affordance("甲", "bench:a", "擦")
    assert not blocked["ok"] and blocked["reason"] == "busy"
    assert world.stocks("bench:a")["成色"] == 1.0
    world.tick(3)
    assert world.scheduler.perform_affordance("甲", "bench:a", "擦")["ok"]


def test_同一件事起不了两次头(tmp_path, open_world):
    world = _slow_world(tmp_path, open_world, occupies=False)
    assert world.scheduler.perform_affordance("甲", "bench:a", "打磨")["ok"]
    again = world.scheduler.perform_affordance("甲", "bench:a", "打磨")
    assert not again["ok"] and again["reason"] == "busy"
    assert world.stocks("agent:甲")["体力"] == 80.0, "被拒的那次不许再收一次费"


def test_不占用她的长过程期间她照样过日子(tmp_path, open_world):
    """十月怀胎和做一把椅子都要花十个月,而前者不该让她十个月不能干活。
    两者的区别不在时长上,在这一格上。"""
    world = _slow_world(tmp_path, open_world, occupies=False, name="free")
    world.scheduler.perform_affordance("甲", "bench:a", "打磨")
    assert world.scheduler.perform_affordance("甲", "bench:a", "擦")["ok"]
    world.tick(3)
    assert world.stocks("bench:a")["成色"] == pytest.approx(2.1)


def test_东西没了就收不了尾而且代价不退(tmp_path, open_world):
    """她那三个 tick 确实花掉了。退回去等于让"世界变了"成为一次免费的重来。"""
    world = _slow_world(tmp_path, open_world)
    world.scheduler.perform_affordance("甲", "bench:a", "打磨")
    world.scheduler.ontology_store.drop_entity("bench:a")
    world.scheduler.ontology = world.scheduler.ontology_store.load()
    world.tick(3)
    gone = [e for e in world.events() if e["type"] == "entity_disengage"]
    assert gone and gone[-1]["payload"]["reason"] == "gone"
    assert world.stocks("agent:甲")["体力"] == 80.0, "力气不退"


def test_在做的事写进了_redis_而不是进程里(tmp_path, open_world, fresh_redis):
    """一件事要花多久由作者决定,重启多少次由运维决定 —— 内存态会让后者
    决定前者,而且是静默地:每次重启都流产一次,账上什么也看不出来。"""
    world = _slow_world(tmp_path, open_world, duration=50)
    world.scheduler.perform_affordance("甲", "bench:a", "打磨")
    keys = [k for k in fresh_redis.keys("*") if k.endswith(":engaged")]
    assert keys, "在做的长过程没落进 Redis"
    assert fresh_redis.hlen(keys[0]) == 1


def test_排班窗口里同一件事只做一遍(tmp_path, open_world):
    """**这是那个 bug 的回归测试。**

    `_emit_on_transition` 有意把 `interact` 从"当前动作"里弹掉,理由是一次交互
    是一下子的事、不是一个状态。代价是一个 30 分钟的排班窗口会把同一件事做 6 遍
    (5 分钟一 tick),于是橱窗里那个 `tend` 一直不敢挂 `consumes` —— 一早上要
    烧掉六包肥。给它一个覆盖窗口的 duration 之后,占着她的那件事就**是**一个
    状态,重挑到同一个动作会被"和当前相同"挡住。
    """
    from anima_world.actions import ActionDescriptor

    world = _slow_world(tmp_path, open_world, duration=4, name="duty")
    agent = world.scheduler.agents["甲"].agent
    duty = ActionDescriptor("interact", {"target": "bench:a", "verb": "打磨"})
    for _ in range(4):                       # 树在窗口里每 tick 重挑同一个动作
        world.scheduler._emit_on_transition(agent, duty)
    assert len([e for e in world.events() if e["type"] == "entity_engage"]) == 1
    assert world.stocks("agent:甲")["体力"] == 80.0, "起了四次头的话力气会掉到 20"


def test_不声明时长的话重放照旧(tmp_path, open_world):
    """上一条改的是**声明了时长的世界**。没声明的必须逐位如旧 —— 那条弹掉
    `interact` 的规矩仍然是对的:一次浇水就是一下子的事。"""
    from anima_world.actions import ActionDescriptor

    world = _slow_world(tmp_path, open_world, duration=0, name="instant")
    agent = world.scheduler.agents["甲"].agent
    duty = ActionDescriptor("interact", {"target": "bench:a", "verb": "擦"})
    for _ in range(3):
        world.scheduler._emit_on_transition(agent, duty)
    assert world.stocks("bench:a")["成色"] == pytest.approx(1.3)


def test_中途走开的话这件事就没做完(tmp_path, open_world):
    """起头时查过一次同处一地(`perform_affordance` 的 `absent`),而**占着她身体
    的事,她走开之后当然就不在做了**。不查的话她可以起个头就动身去别的镇子,
    那棵树照样在十二个 tick 之后被磨完,世界一声不吭 —— 这是这个仓库最怕的
    那种坏法的教科书形状:代价付过了、日志干净、结果凭空发生。

    不占用她的那种正相反(怀胎不该要求她守在原地),所以只查这一半。
    """
    world = _slow_world(tmp_path, open_world, duration=3, name="away")
    world.scheduler.perform_affordance("甲", "bench:a", "打磨")
    # 她走了。`loc` 是位置的唯一真相(§ travel-and-colocation)。
    world.scheduler.agents["甲"].agent.blackboard.write("loc", "别处")
    world.tick(3)
    left = [e for e in world.events() if e["type"] == "entity_disengage"]
    assert left and left[-1]["payload"]["reason"] == "left"
    assert world.stocks("bench:a")["成色"] == 1.0, "人走了,凳子不该自己被磨"
    assert world.stocks("agent:甲")["体力"] == 80.0, "力气照样花了 —— 代价不退"
    assert not world.engagements("甲"), "没做完的事不能一直挂着"


def test_不占用她的长过程不要求她守在原地(tmp_path, open_world):
    """怀胎十月不该要求她十个月不出门。**在场是占用那一半的语义**,不是时长的。"""
    world = _slow_world(tmp_path, open_world, duration=3, occupies=False, name="roam")
    world.scheduler.perform_affordance("甲", "bench:a", "打磨")
    world.scheduler.agents["甲"].agent.blackboard.write("loc", "别处")
    world.tick(3)
    assert world.stocks("bench:a")["成色"] == 2.0
    assert not [e for e in world.events() if e["type"] == "entity_disengage"]
