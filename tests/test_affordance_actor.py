"""能力是**施动者与对象之间**的关系,不是对象单方面的属性。

## 缺口原本长什么样

本体层落地之后,一个世界可以声明"树能被照料,照料一次长 0.3 米"。它是对的,但它
把一件事漏在外面:**照料的人**。于是这个世界里:

- 谁来照料都一样 —— 一个八十岁的人和一个壮劳力对树的作用完全相同;
- 她可以连着照料一百棵树,不累、不饿、什么都不少;
- 她永远不会因为"我今天干不动了"而改主意,因为世界里没有任何东西记着这件事。

Gibson 的原话是 affordance 存在于 **animal-environment 这一对**里:同一把斧头对
有力气的人是"能砍",对没力气的人不是。缺了施动者那一半,能力就退化成一个"谁按
谁灵"的按钮 —— 而**一个总能成功的动作产生不了任何决策**:她没有理由挑先做哪件、
没有理由休息、没有理由变强。丰富的决策是从"做不到"里长出来的。

## 补上的三样,以及各自守的东西

1. **她身上也有量。** `kinds` 里可以写 `{"id": "agent", "quantities": {...}}` ——
   内置种类里唯一开的口子,而且只开在 `quantities` 上(她不是一样可以被 `tend`
   的东西)。量住 `agent:{id}`,和树的量同一个后端、同一套可见性。
2. **`requires` / `costs`,以及 `me_` 前缀。** 前者是"她做不做得了",后者是
   "做完从她身上扣什么"。
3. **拒绝理由分开。** `conditions`(树还没长好,等)和 `incapable`(你没力气了,
   去歇着)是两件事,她该做的事相反。

## 这个文件守的

每一条都对着一个"不拦就会静默地错"的坏法,而不是对着一个功能。最要紧的是最后
一组:`me_力气` 拼错时读到 0,那道门于是要么永远开着要么永远关着 —— 世界照跑、
日志干净,和量名拼错那条完全同源。
"""

from __future__ import annotations

import json

import pytest

from anima_world.ontology import OntologyError, apply_affordance, parse_kinds

# 一个会累的人,和一棵要花力气照料的树。
ACTOR = {"id": "agent", "quantities": {
    "体力": {"default": 100.0, "visibility": "self", "unit": "点"},
    "手艺": {"default": 1.0, "visibility": "here"},
}}
TREE = {
    "id": "tree",
    "gloss": "一棵树",
    "quantities": {"树高": {"default": 1.0, "visibility": "here", "unit": "米"},
                   "最大树高": 12.0},
    "affordances": {
        "look": {},
        "tend": {
            "when": ["树高 < 最大树高"],
            "requires": ["me_体力 >= 20"],
            "set": {"树高": "min(树高 + 0.3 * me_手艺, 最大树高)"},
            "costs": {"体力": "me_体力 - 20"},
        },
    },
}


def _tend(*entries):
    return parse_kinds([ACTOR, TREE, *entries])["tree"].affordances["tend"]


# ── 声明:她身上的量 ─────────────────────────────────────────────────────────


def test_agent_是唯一能被数据声明的内置种类():
    kinds = parse_kinds([ACTOR])
    assert kinds["agent"].builtin                       # 仍然是内置的
    assert kinds["agent"].quantity_names() == {"体力", "手艺"}
    assert not kinds["agent"].affordances               # 她不是可以被 tend 的东西


@pytest.mark.parametrize("entry, fragment", [
    ({"id": "agent", "affordances": ["tend"]}, "只能声明 quantities"),
    ({"id": "agent", "prompt": {"budget": 3}}, "只能声明 quantities"),
    ({"id": "location", "quantities": {"面积": 1.0}}, "内置种类"),
    ({"id": "world", "quantities": {"季节": 1.0}}, "内置种类"),
])
def test_口子只开在agent的quantities上(entry, fragment):
    """开大一格的代价:`agent` 长出 affordances 就等于允许"照料一个人",
    而角色的交互整套在别处(聊天、行为树、工具)—— 那会是第二份真相。"""
    with pytest.raises(OntologyError) as caught:
        parse_kinds([entry])
    assert fragment in str(caught.value)


def test_她身上的量也要声明可见性(fresh_redis):
    """声明即开关,这一半也一样:`self` 的量只有她自己知道,`here` 的别人也看得出。

    没有这条,"她累了"就只是数据库里一个数字 —— 进不了任何人的提示词。
    """
    from anima_world.ontology import visibility_declarations, resolve

    ontology = resolve(parse_kinds([ACTOR, TREE]), {})
    declared = {(kind, key): level for kind, key, level, _ in visibility_declarations(ontology)}
    assert declared[("agent", "体力")] == "self"
    assert declared[("agent", "手艺")] == "here"


# ── requires:她做不做得了 ───────────────────────────────────────────────────


def test_力气够就做得了_不够就做不了():
    tend = _tend()
    strong = apply_affordance(tend, values={"树高": 1.0, "最大树高": 12.0},
                              me_values={"体力": 100.0, "手艺": 1.0})
    assert strong.ok and strong.updates == {"树高": 1.3}

    tired = apply_affordance(tend, values={"树高": 1.0, "最大树高": 12.0},
                             me_values={"体力": 5.0, "手艺": 1.0})
    assert not tired.ok
    assert not tired.updates and not tired.me_updates


def test_做不了和这会儿不行是两个理由():
    """**她接下来该做的事相反。**

    合成一个的样子:一个累坏了的人会挨棵树轮着试过去,每一棵都回她"再等等" ——
    而她需要的那句话是"你该歇着了",一句都没收到。
    """
    tend = _tend()
    ripe = apply_affordance(tend, values={"树高": 12.0, "最大树高": 12.0},
                            me_values={"体力": 100.0, "手艺": 1.0})
    assert ripe.reason == "conditions" and "树高 < 最大树高" in ripe.refusal

    tired = apply_affordance(tend, values={"树高": 1.0, "最大树高": 12.0},
                             me_values={"体力": 5.0, "手艺": 1.0})
    assert tired.reason == "incapable" and "me_体力 >= 20" in tired.refusal


def test_两条都不成立时先说做不了():
    """她此刻唯一能行动的那条信息是"你做不了" —— 说"树还没长好"只会把她
    支去下一棵树,收到同一句,一棵一棵试到天亮。"""
    outcome = apply_affordance(_tend(), values={"树高": 12.0, "最大树高": 12.0},
                               me_values={"体力": 0.0, "手艺": 1.0})
    assert outcome.reason == "incapable"


def test_requires只准读她自己():
    """不拦的代价是这一层唯一的收益消失:`requires` 一旦能读树身上的量,它就和
    `when` 是同一样东西,而拒绝理由再也分不出"等一会儿"和"换件事做"。"""
    with pytest.raises(OntologyError) as caught:
        parse_kinds([ACTOR, {
            "id": "tree", "quantities": {"树高": 1.0},
            "affordances": {"tend": {"requires": ["树高 > 1"]}},
        }])
    assert "requires 只准读" in str(caught.value)


# ── costs:做完从她身上扣 ─────────────────────────────────────────────────────


def test_代价真的从她身上扣():
    outcome = apply_affordance(_tend(), values={"树高": 1.0, "最大树高": 12.0},
                               me_values={"体力": 100.0, "手艺": 1.0})
    assert outcome.updates == {"树高": 1.3}       # 树上的
    assert outcome.me_updates == {"体力": 80.0}   # 她身上的


def test_代价和效果读同一份旧值():
    """双缓冲,和规律那一层同一条纪律。顺序敏感的话,"扣体力"和"树长高"谁先算
    就成了写声明时看不见的语义 —— 而作者手里没有任何东西能告诉他答案。"""
    tend = parse_kinds([
        {"id": "agent", "quantities": {"体力": 100.0}},
        {"id": "tree", "quantities": {"树高": 1.0},
         "affordances": {"tend": {
             "set": {"树高": "树高 + me_体力 / 100"},     # 读她的体力
             "costs": {"体力": "me_体力 - 50"},           # 同时扣掉一半
         }}},
    ])["tree"].affordances["tend"]
    outcome = apply_affordance(tend, values={"树高": 1.0}, me_values={"体力": 100.0})
    # 先扣后算的话树只长 0.5 —— 而声明里看不出该是哪个。
    assert outcome.updates == {"树高": 2.0} and outcome.me_updates == {"体力": 50.0}


def test_costs写到没声明的量上时开不了机():
    with pytest.raises(OntologyError) as caught:
        parse_kinds([ACTOR, {
            "id": "tree", "quantities": {"树高": 1.0},
            "affordances": {"tend": {"costs": {"心情": "me_体力 - 1"}}},
        }])
    assert "凭空造出" in str(caught.value) and "体力" in str(caught.value)


def test_没声明过agent就一个costs都写不了():
    """先有量,才有代价。反过来放行的话,`agent` 身上会长出一批只在某一条能力里
    存在的属性 —— 没有默认值、没有可见性、谁也不知道它们在。"""
    with pytest.raises(OntologyError, match="没声明过"):
        parse_kinds([{
            "id": "tree", "quantities": {"树高": 1.0},
            "affordances": {"tend": {"costs": {"体力": "1"}}},
        }])


# ── me_ 拼错:这一层存在的那个坏法 ───────────────────────────────────────────


@pytest.mark.parametrize("spec", [
    {"requires": ["me_休力 >= 20"]},                       # 门永远关着
    {"set": {"树高": "树高 + me_手忆"}},                    # 恒 +0
    {"costs": {"体力": "me_休力 - 20"}},                    # 扣成负的
])
def test_她身上的量名拼错时开不了机(spec):
    """`me_休力` 和 `me_体力` 差一个字。不拦的样子和量名拼错那条完全同源:
    读到 0,于是这道关于她的门要么永远开着、要么永远关着,而世界照跑、日志干净。"""
    with pytest.raises(OntologyError) as caught:
        parse_kinds([ACTOR, {
            "id": "tree", "quantities": {"树高": 1.0}, "affordances": {"tend": spec},
        }])
    message = str(caught.value)
    assert "恒为 0" in message
    # 报的是**她身上声明过哪些**,差一个字的名字要摆在一起才看得出。
    assert "me_体力" in message and "me_手艺" in message


def test_不看施动者的能力照旧不看():
    """**声明本身就是开关**,这一半也一样:没写 `requires` / `costs` 的能力
    连她身上的量都不去读 —— 一个没声明 `agent` 的世界一比特都没变。"""
    tend = parse_kinds([{
        "id": "tree", "quantities": {"树高": 1.0, "最大树高": 12.0},
        "affordances": {"tend": {"when": ["树高 < 最大树高"],
                                 "set": {"树高": "树高 + 0.5"}}},
    }])["tree"].affordances["tend"]
    assert not tend.needs_actor
    outcome = apply_affordance(tend, values={"树高": 1.0, "最大树高": 12.0})
    assert outcome.ok and outcome.updates == {"树高": 1.5} and not outcome.me_updates


# ── 接到真世界上 ────────────────────────────────────────────────────────────


def _world(tmp_path, open_world, *, 体力=100.0):
    seed = {
        "locations": [{"id": "cafe", "name": "咖啡店", "description": "拐角那家"}],
        "agents": [{"id": "甲", "name": "甲", "location": "cafe", "personality": "安静"}],
        "kinds": [ACTOR, TREE],
        "entities": [{"id": "tree:a", "name": "那棵", "location": "cafe"}],
        "stocks": [{"owner": "agent:甲", "values": {"体力": 体力}}],
    }
    path = tmp_path / f"seed{体力:g}.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return open_world(seed_path=str(path))


def test_她声明过的量在世界里真的长出来(tmp_path, open_world):
    """角色不是 `ontology.entities` 的成员(元数据归 Brain / 黑板),所以这一填
    落在 `register` 上 —— 中途加入的人走的也是那条路。逐个量填:种子写了
    `体力`,没写的 `手艺` 照样落地,否则 `me_手艺` 恒为 0。"""
    world = _world(tmp_path, open_world, 体力=40.0)
    assert world.stocks("agent:甲") == {"体力": 40.0, "手艺": 1.0}


def test_照料一次_树长了她也累了(tmp_path, open_world):
    world = _world(tmp_path, open_world)
    result = world.scheduler.perform_affordance("甲", "tree:a", "tend")
    assert result["ok"], result
    assert result["changed"] == {"树高": 1.3}
    assert result["cost"] == {"体力": 80.0}
    assert world.stocks("agent:甲")["体力"] == 80.0
    assert world.stocks("tree:a")["树高"] == 1.3


def test_累到做不动时世界当场说做不了(tmp_path, open_world):
    """**照实报,不假装成功。** 而 `reason` 必须是 `incapable`:行为树那条路
    "下一 tick 再试"对它没用 —— 体力不会因为她又试了一次而回来。"""
    world = _world(tmp_path, open_world, 体力=5.0)
    result = world.scheduler.perform_affordance("甲", "tree:a", "tend")
    assert not result["ok"] and result["reason"] == "incapable"
    assert world.stocks("tree:a")["树高"] == 1.0        # 树一点没变
    assert world.stocks("agent:甲")["体力"] == 5.0      # 她也没白扣


def test_做不成时两边一个字都不写(tmp_path, open_world):
    """半成功是这一层最坏的形状:扣了体力而树没长,或者树长了而没扣体力。
    两个 `set_many` 都在拒绝之后,所以"没成"就是真的什么都没发生。"""
    world = _world(tmp_path, open_world)
    world.scheduler.stock_store.set_many("tree:a", {"树高": 12.0}, tick=0)
    result = world.scheduler.perform_affordance("甲", "tree:a", "tend")
    assert result["reason"] == "conditions"
    assert world.stocks("agent:甲")["体力"] == 100.0


def test_代价也进事件(tmp_path, open_world):
    """账上只有"树高了"没有"她累了"的话,历史里就找不出她为什么第二天没干活。"""
    world = _world(tmp_path, open_world)
    world.scheduler.perform_affordance("甲", "tree:a", "tend")
    rows = [e for e in world.events() if e["type"] == "entity_interaction"]
    assert rows and rows[-1]["payload"]["cost"] == {"体力": 80.0}


def test_她自己的量进得了她的感知(tmp_path, open_world):
    """扣了体力却不让她知道,等于让她在一个"她以为自己精神得很"的世界里做决定。

    `self` 档走的是认知层原本就有的那条路 —— 这里验的是它真的接上了。
    """
    world = _world(tmp_path, open_world)
    world.scheduler.perform_affordance("甲", "tree:a", "tend")
    assert world.perception("甲")["own"]["体力"] == 80.0


def _two(tmp_path, open_world):
    seed = {
        "locations": [{"id": "cafe", "name": "咖啡店", "description": "拐角那家"}],
        "agents": [
            {"id": "甲", "name": "甲", "location": "cafe", "personality": "安静"},
            {"id": "乙", "name": "乙", "location": "cafe", "personality": "热络"},
        ],
        "kinds": [ACTOR, TREE],
        "entities": [{"id": "tree:a", "name": "那棵", "location": "cafe"}],
    }
    path = tmp_path / "two.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return open_world(seed_path=str(path))


def test_声明成here的那些量别人真的看得见(tmp_path, open_world):
    """`here` 档问的是可见性表里"她在哪",而角色的位置一直只住在黑板上。

    两边不通的样子是这一层最怕的:作者写了一句"别人看得出她手艺好",世界照跑、
    日志干净,而那句话**从来没有发生过**。
    """
    world = _two(tmp_path, open_world)
    world.scheduler.stock_store.set_many("agent:乙", {"手艺": 2.5}, tick=0)
    world.tick(1)
    seen = world.perception("甲")["here"]
    assert seen.get("agent:乙") == {"手艺": 2.5}
    assert "体力" not in seen.get("agent:乙", {}), "self 档的量泄露给了别人"


def test_她走了别人就看不见了(tmp_path, open_world):
    """位置要**跟着**走。定在创世那一刻的话,她人早走了,手艺还留在咖啡店里。"""
    world = _two(tmp_path, open_world)
    world.tick(1)
    assert "agent:乙" in world.perception("甲")["here"]
    world.scheduler.agents["乙"].agent.blackboard.write("loc", "别处")
    world.tick(1)
    assert "agent:乙" not in world.perception("甲")["here"]


def test_没声明过外人看得见的量时一次都不写(tmp_path, open_world):
    """**声明本身就是开关。** 全是 `self` 的世界(以及根本没声明 agent 的世界)
    里,位置同步整个不该发生 —— 否则每个世界都白付一张表的写。"""
    import json as _json

    seed = {
        "locations": [{"id": "cafe", "name": "咖啡店", "description": "拐角那家"}],
        "agents": [{"id": "甲", "name": "甲", "location": "cafe", "personality": "安静"}],
        "kinds": [{"id": "agent", "quantities": {"体力": {"default": 100.0,
                                                        "visibility": "self"}}}],
    }
    path = tmp_path / "selfonly.json"
    path.write_text(_json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    world = open_world(seed_path=str(path))
    world.tick(2)
    assert not world.scheduler._actor_is_visible_to_others()
    assert "agent:甲" not in world.scheduler.visibility_store.at("cafe")
    # 但她自己照旧知道自己的体力 —— `self` 走的不是位置那条路。
    assert world.perception("甲")["own"]["体力"] == 100.0


def test_kinds出口报出关于她的那一半(tmp_path, open_world):
    """宿主要把"这棵树还没长好"和"你没力气了"显示成两句不同的话,就得分开拿到。
    猜是猜不出来的:`sets` 里看不出哪些量落在她身上。"""
    world = _world(tmp_path, open_world)
    by_id = {k["id"]: k for k in world.kinds()}
    assert by_id["agent"]["quantities"], "她身上的量在出口上不见了"
    tend = next(a for a in by_id["tree"]["affordances"] if a["verb"] == "tend")
    assert tend["needs_actor"]
    assert tend["requires"] == ["me_体力 >= 20"]
    assert tend["costs"] == ["体力 = me_体力 - 20"]


# ── have_ / consumes:工具与材料 ─────────────────────────────────────────────
#
# 上面那一半补的是"她身上的量",而 Gibson 举的例子本身是**斧头** —— 一样随身
# 带着的东西。它此前根本没法出现在声明里:`requires` 只读得到量,而随身物品住在
# 经济那一层的库存(事件日志的投影)。绕开的写法是给 `agent` 声明一个 `斧头: 0/1`
# 的量,那等于把同一件事记在两个后端上,两份真相里有一份不更新是这个仓库最怕的坏法。


AXE_TREE = {
    "id": "tree",
    "quantities": {"树高": {"default": 4.0, "visibility": "here"}},
    "affordances": {
        "damage": {
            "requires": ["have_斧头 >= 1"],
            "set": {"树高": "树高 - 1"},
        },
    },
}


def _axe(**overrides):
    spec = dict(AXE_TREE)
    spec["affordances"] = {"damage": {**AXE_TREE["affordances"]["damage"], **overrides}}
    return parse_kinds([ACTOR, spec])["tree"].affordances["damage"]


def test_同一棵树对带斧头的人才是能砍():
    """Gibson 的原话。两次调用只差手上有没有那样东西,世界的量一个字没动。"""
    axe = _axe()
    assert apply_affordance(axe, values={"树高": 4.0}, held={"斧头": 1}).ok
    refused = apply_affordance(axe, values={"树高": 4.0}, held={})
    assert not refused.ok and refused.reason == "incapable"


def test_没带的东西读作零而不是名字不存在():
    """一个从没拿过斧头的人和一个刚把斧头放下的人,在"她现在砍不砍得动"上
    没有区别。读作"名字不存在"的话表达式当场炸,而炸出来的是 `unknown_verb`
    一类的胡话 —— 她做不了这件事,不是这件事讲不通。"""
    outcome = apply_affordance(_axe(), values={"树高": 4.0})   # held 压根没传
    assert not outcome.ok and outcome.reason == "incapable"
    assert "斧头" in outcome.refusal


def test_没接经济层的调用方是每道门都关着():
    """默认值只有这一个方向是对的。反过来默认"她什么都有"会让声明形同虚设,
    而且是**静默**的:世界照跑,门在日志里根本不出现。"""
    assert not apply_affordance(_axe(), values={"树高": 4.0}, held=None).ok


def test_consumes_自带一道你得有的门():
    """作者只写了 `consumes`,一条 `requires` 都没写 —— 引擎自己拦。

    不拦的话她会用一包不存在的肥料把活干完:库存扣不到负数,于是连账上都
    看不出来。让作者两处各写一遍则给了只写一遍的机会,而漏掉的那一遍恰好是
    没有任何症状的那一遍。"""
    axe = _axe(requires=[], consumes={"油": 2})
    assert apply_affordance(axe, values={"树高": 4.0}, held={"油": 2}).ok
    thin = apply_affordance(axe, values={"树高": 4.0}, held={"油": 1})
    assert not thin.ok and thin.reason == "incapable"
    assert thin.refusal == "你手上的 油 不够:要 2 个,你有 1 个"


def test_做成了才说要花掉什么():
    """`consumed` 是"该扣什么",不是"扣好了" —— 库存只有事件日志一个来源。
    拒绝的那一路必须是空的,否则调用方会照着发一个凭空的 `item_consume`。"""
    axe = _axe(consumes={"油": 1})
    assert apply_affordance(axe, values={"树高": 4.0},
                            held={"斧头": 1, "油": 1}).consumed == {"油": 1}
    assert apply_affordance(axe, values={"树高": 4.0}, held={"油": 1}).consumed == {}


def test_花不花东西也算改世界():
    """`changes_world` 决定它进不进"这里能做什么"的提示词。只花材料、不改量的
    动作(点一支烟)照样是改世界 —— 漏了的话她看不见自己能做这件事。"""
    assert _axe(set={}, consumes={"油": 1}).changes_world
    assert _axe(set={}, consumes={"油": 1}).needs_actor


@pytest.mark.parametrize("bad", [0, -1, 0.5, "1", True, None])
def test_consumes_只收正整数(bad):
    """花掉半包肥料没有意思,库存本来就是整数;收表达式则要多解释一遍"算出
    -1 会怎样"、"算出 0.5 会怎样",而这两个答案都只能是"不许"。少一个能问的
    问题比多一分表达力值钱。`True` 单列:它在 Python 里是 1,而作者写它的意思
    大概是"要有"。"""
    with pytest.raises(OntologyError):
        _axe(consumes={"油": bad})


def test_consumes_必须是对象():
    with pytest.raises(OntologyError) as excinfo:
        _axe(consumes=["油"])
    assert "consumes" in str(excinfo.value)


def test_requires_里_have_不算外人():
    """`requires` 只准读 `me_*` 那条闸得给 `have_*` 让路 —— 两者的拒绝理由是
    同一个:「你做不了」。而读对象的量仍然要被拦下。"""
    _axe(requires=["have_斧头 >= 1", "me_体力 >= 10"])       # 不炸
    with pytest.raises(OntologyError) as excinfo:
        _axe(requires=["树高 >= 2"])
    assert "requires" in str(excinfo.value)


# ── 拼错东西的名字:开不了机 ────────────────────────────────────────────────


def _resolve_items(*items, affordance=None):
    from anima_world.ontology import resolve
    kinds = parse_kinds([ACTOR, dict(AXE_TREE, affordances={
        "damage": affordance or AXE_TREE["affordances"]["damage"]})])
    return resolve(kinds, {}, items=items)


def test_have_里的东西查得起来():
    """物品是**闭集**(只在创世从种子里播),所以这条查得动。拼错一个字的后果是
    那道门永远关着,而世界照跑、日志干净 —— 和量名拼错完全同源。"""
    _resolve_items("斧头")
    with pytest.raises(OntologyError) as excinfo:
        _resolve_items("斧子")
    assert "斧头" in str(excinfo.value)


def test_consumes_里的东西也查():
    with pytest.raises(OntologyError):
        _resolve_items("斧头", affordance={"requires": ["have_斧头 >= 1"],
                                          "consumes": {"油": 1}})
    _resolve_items("斧头", "油", affordance={"requires": ["have_斧头 >= 1"],
                                            "consumes": {"油": 1}})


# ── 接到真世界上 ────────────────────────────────────────────────────────────


AXE = {"id": "斧头", "name": "斧头", "kind": "durable", "base_price": 30.0}
OIL = {"id": "油", "name": "一壶油", "kind": "consumable", "base_price": 2.0}
CHOP_TREE = {
    "id": "tree",
    "quantities": {"树高": {"default": 4.0, "visibility": "here"}},
    # 一次烧掉**两**壶油:量词写死成 1 的话,"扣了"和"按数扣"这两件事分不开。
    "affordances": {"damage": {"requires": ["have_斧头 >= 1"],
                             "consumes": {"油": 2},
                             "set": {"树高": "树高 - 1"}}},
}


def _tool_world(tmp_path, open_world, *, 甲=(), 乙=()):
    def _inv(spec):
        return [{"item": i, "qty": q} for i, q in spec]

    seed = {
        "locations": [{"id": "cafe", "name": "咖啡店", "description": "拐角那家"}],
        "agents": [
            {"id": "甲", "name": "甲", "location": "cafe", "personality": "安静",
             "inventory": _inv(甲)},
            {"id": "乙", "name": "乙", "location": "cafe", "personality": "安静",
             "inventory": _inv(乙)},
        ],
        "items": [AXE, OIL],
        "kinds": [ACTOR, CHOP_TREE],
        "entities": [{"id": "tree:a", "name": "那棵", "location": "cafe"}],
    }
    path = tmp_path / f"tools{len(甲)}{len(乙)}.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return open_world(seed_path=str(path))


def test_有工具的砍得动没工具的当场被拦(tmp_path, open_world):
    """同一棵树、同一个动词、同一时刻,两个人两种答案 —— 这就是 affordance
    住在"这一对"里的全部意思。"""
    world = _tool_world(tmp_path, open_world, 甲=[("斧头", 1), ("油", 2)])
    assert world.scheduler.perform_affordance("甲", "tree:a", "damage")["ok"]
    refused = world.scheduler.perform_affordance("乙", "tree:a", "damage")
    assert not refused["ok"] and refused["reason"] == "incapable"
    assert world.stocks("tree:a")["树高"] == 3.0        # 乙 什么也没砍掉


def test_花掉的材料真的从库存里少了(tmp_path, open_world):
    """走事件,不直接改投影。直接改的话"重启一次她的油就回来了"会成为可能,
    而账上什么也看不出来。"""
    world = _tool_world(tmp_path, open_world, 甲=[("斧头", 1), ("油", 5)])
    world.scheduler.perform_affordance("甲", "tree:a", "damage")
    rows = [e for e in world.events() if e["type"] == "item_consume"]
    assert rows and rows[-1]["payload"]["item_id"] == "油"
    assert rows[-1]["payload"]["qty"] == 2, "事件得说清是几个,不是只说发生过一次"
    # 5 - 2:少一个的话说明投影把 qty 当成了 1 —— 一件"扣了,但扣少了"的事,
    # 账面上一切正常,只有几十天之后油才对不上。
    assert world.scheduler._memory_projection.inventories["甲"]["油"] == 3


def test_材料用光就砍不动了(tmp_path, open_world):
    """"做得到 → 做不到"必须真的会发生。永远做得到的动作产生不了任何决策,
    而这条正是让她有理由去买一壶油的那个转折。"""
    world = _tool_world(tmp_path, open_world, 甲=[("斧头", 1), ("油", 3)])
    assert world.scheduler.perform_affordance("甲", "tree:a", "damage")["ok"]
    dry = world.scheduler.perform_affordance("甲", "tree:a", "damage")
    assert not dry["ok"] and dry["reason"] == "incapable"
    assert "油" in dry["refusal"]
    assert world.stocks("tree:a")["树高"] == 3.0


def test_做不成时材料一个不少(tmp_path, open_world):
    """半成功的另一种形状:扣了油而树没倒。拒绝那一路一个事件都不该发。"""
    world = _tool_world(tmp_path, open_world, 乙=[("油", 5)])
    world.scheduler.perform_affordance("乙", "tree:a", "damage")   # 没斧头
    assert not [e for e in world.events() if e["type"] == "item_consume"]
    assert world.scheduler._memory_projection.inventories["乙"]["油"] == 5


def test_花掉了什么也进事件(tmp_path, open_world):
    """"树矮了 1 米"和"为此烧掉一壶油"在历史里得连着,否则后来的人对不上账。"""
    world = _tool_world(tmp_path, open_world, 甲=[("斧头", 1), ("油", 2)])
    world.scheduler.perform_affordance("甲", "tree:a", "damage")
    rows = [e for e in world.events() if e["type"] == "entity_interaction"]
    assert rows[-1]["payload"]["consumed"] == {"油": 2}


def test_种子里拼错东西的名字开不了机(tmp_path, open_world):
    """闸在创世上,不在第一次调用上 —— 后者要等到某个人某天真去砍那棵树,
    而那时世界已经跑了几十天。"""
    seed = {
        "locations": [{"id": "cafe", "name": "咖啡店", "description": "拐角"}],
        "agents": [{"id": "甲", "name": "甲", "location": "cafe", "personality": "静"}],
        "items": [AXE],
        "kinds": [ACTOR, CHOP_TREE],       # consumes 里的 "油" 没定义过
        "entities": [{"id": "tree:a", "name": "那棵", "location": "cafe"}],
    }
    path = tmp_path / "typo.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(OntologyError) as excinfo:
        _tool = open_world(seed_path=str(path))
        _tool.close()
    assert "油" in str(excinfo.value)
