"""世界能自己长出新东西,也能把东西抹掉(`spawn` / `destroys_target`)。

在这之前 `entities` 是**创世时钉死的闭集**:树不能被种下,杯子不能被打碎,而
`make` 这个动词在词表里却没有任何办法造出一个实体。一个不能长出新东西的世界是个
西洋镜,不是世界 —— 而"按规则铺开实体"正是"生成一个世界"这件事本身。

放开它的难处不在实现,在**怎么不让它跑飞**。这一层的答案是:

- **生成必须要代价,而代价由作者写。** 不是引擎发配额 —— 配额是引擎的天花板,
  撞上去时她收到的拒绝在世界里没有意义("这个世界最多一百棵树"不是她能理解、
  能应对的东西,她也永远学不会)。代价是世界的理由:她知道自己为什么做不到、
  要做到得先补什么。
- **生和灭同一轮加。** 代价只封得住速率,封不住存量:体力天天回满的世界里,
  一百天就是一百个孩子。真实世界靠的是会生的东西都会死。
- **出生自检是出生的一部分,不是事后的工具。** 运行期生出来的东西走的不是创世
  那条路,而创世那条路上的闸(一次列全、当场开不了机)在这里一条都不在。
"""

from __future__ import annotations

import json

import pytest

from anima_world.ontology import (
    OntologyError,
    check_entity,
    parse_entities,
    parse_kinds,
    resolve,
)

ACTOR = {"id": "agent", "quantities": {"体力": {"default": 100, "visibility": "self"}}}
CHAIR = {
    "id": "chair", "gloss": "一把椅子",
    "quantities": {"成色": {"default": 1.0, "visibility": "here"}},
    "affordances": {"look": {}},
}
MAKE = {
    "duration": 4,
    "requires": ["me_体力 >= 20"],
    "costs": {"体力": "me_体力 - 20"},
    "spawn": {"kind": "chair", "name": "新打的椅子", "quantities": {"成色": 0.8}},
}
BENCH = {
    "id": "bench", "gloss": "一条长凳",
    "quantities": {"完好": {"default": 1.0, "visibility": "here"}},
    "affordances": {
        "look": {},
        "打椅子": MAKE,
        "拆掉": {"costs": {"体力": "me_体力 - 5"}, "destroys_target": True},
    },
}


def _kinds(**bench_affordances):
    bench = {**BENCH, "affordances": {**BENCH["affordances"], **bench_affordances}}
    return parse_kinds([ACTOR, CHAIR, bench])


def _bad(**bench_affordances):
    with pytest.raises(OntologyError) as excinfo:
        _kinds(**bench_affordances)
    return str(excinfo.value)


def _bad_resolve(**bench_affordances):
    with pytest.raises(OntologyError) as excinfo:
        resolve(_kinds(**bench_affordances), {}, locations=["cafe"])
    return str(excinfo.value)


# ── 声明:生成必须要代价 ───────────────────────────────────────────────────


def test_spawn_声明得出来():
    make = _kinds()["bench"].affordances["打椅子"]
    assert make.spawn is not None and make.spawn.kind == "chair"
    assert make.spawn.quantities == {"成色": 0.8}
    assert make.changes_world, "会让世界多一样东西,当然算改变世界"


def test_不要代价的生成开不了机():
    """**这是这一格唯一的硬约束。** 一个白给的生成,作者写下去的第二天世界里就有
    一百万棵树 —— 而挡住它的正确办法不是引擎发配额:配额是引擎的天花板,撞上去时
    她收到的拒绝在世界里没有意义,她也永远学不会。代价是世界的理由。
    """
    message = _bad(白造={"spawn": {"kind": "chair"}})
    assert "代价" in message
    # **三种代价都算数,引擎不替作者挑哪一种。** 只认时间的话,"用三根木料换一把
    # 椅子"这种完全正当的世界就写不出来了。
    for price in ({"costs": {"体力": "me_体力 - 1"}},
                  {"duration": 3},
                  {"consumes": {"木料": 1}}):
        affordance = _kinds(白造={"spawn": {"kind": "chair"}, **price})["bench"].affordances["白造"]
        assert affordance.spawn.kind == "chair", price


def test_不要代价的抹掉也开不了机():
    """一个白给的"抹掉",一 tick 就能把世界清空。"""
    assert "代价" in _bad(白拆={"destroys_target": True})


def test_抹掉和写量不能一起写():
    """写到一个正要被抹掉的东西身上。两条里必有一条是作者没想清楚的,而引擎挑哪条
    都是猜:落库再删,那几个值一秒都没人读到;删了再落库,写到一个不存在的 owner 上。
    """
    message = _bad(拆={"costs": {"体力": "me_体力 - 1"},
                      "destroys_target": True, "set": {"完好": "完好 - 1"}})
    assert "destroys_target" in message and "set" in message


@pytest.mark.parametrize("bad", ["yes", 1, "true"])
def test_destroys_target_只收真假(bad):
    assert "destroys_target" in _bad(拆={"costs": {"体力": "me_体力 - 1"},
                                        "destroys_target": bad})


def test_spawn_少了种类():
    assert "kind" in _bad(白造={"duration": 2, "spawn": {"name": "什么"}})


def test_spawn_的量只收常数():
    """新生的东西身上还没有任何值可读,所以这里不收表达式 —— 收了就得先回答
    "读的是起头那一刻还是收尾那一刻",没想清楚之前不开这个口。"""
    message = _bad(白造={"duration": 2,
                        "spawn": {"kind": "chair", "quantities": {"成色": "成色 + 1"}}})
    assert "不收表达式" in message


def test_spawn_的地方不能是空串():
    """写了个空串和不写不是一回事:不写是"生在当场",而空串是作者以为自己指定了
    地方。静默当成当场的话,他会以为那句话生效了。"""
    assert "location" in _bad(白造={"duration": 2,
                                   "spawn": {"kind": "chair", "location": "  "}})


# ── 声明:跨表引用在 resolve 那一阶段查 ────────────────────────────────────


def test_生一个不存在的种类开不了机():
    """闸在创世上,不在第一次调用上 —— 后者要等到某个人某天真去打一把椅子,
    而那时她已经白付过很多次代价了。"""
    message = _bad_resolve(白造={"duration": 2, "spawn": {"kind": "沙发"}})
    assert "沙发" in message


def test_生不出内置种类():
    """角色和地点各有各的生命周期(她有 Brain / 黑板 / 行为树),让能力生一个
    `agent` 出来,生出来的是一个没有脑子的空壳。"""
    message = _bad_resolve(白造={"duration": 2, "spawn": {"kind": "agent"}})
    assert "内置" in message


def test_给新生的东西写一个它没声明过的量():
    message = _bad_resolve(白造={"duration": 2,
                                "spawn": {"kind": "chair", "quantities": {"色号": 1}}})
    assert "色号" in message and "成色" in message


def test_生在一个不存在的地方():
    message = _bad_resolve(白造={"duration": 2,
                                "spawn": {"kind": "chair", "location": "月球"}})
    assert "月球" in message


# ── 出生自检 ──────────────────────────────────────────────────────────────


def _ontology():
    kinds = _kinds()
    entities = parse_entities([{"id": "bench:a", "name": "那条", "location": "cafe"}], kinds)
    return resolve(kinds, entities, locations=["cafe"])


def test_量没落地查得出来():
    """**这是这一层最要紧的一条。** 一个新生的东西可以是:`entities` 里看着好好的,
    量却一个都没落地 —— 于是它的能力条件对着 0 求值、规律算不动,而两件事都只是
    安静地不发生,作者要到三个月后发现那棵树没长才知道。
    """
    problems = check_entity(_ontology(), "bench:a", values={}, place="cafe")
    assert problems and "完好" in problems[0]


def test_量全落地了就没话说():
    assert check_entity(_ontology(), "bench:a", values={"完好": 1.0}, place="cafe") == []


def test_算不出来的能力查得出来():
    kinds = parse_kinds([ACTOR, {
        "id": "chair", "gloss": "椅子",
        "quantities": {"成色": {"default": 0.0, "visibility": "here"}},
        "affordances": {"look": {"when": ["1 / 成色 > 0"]}},
    }])
    entities = parse_entities([{"id": "chair:x", "location": "cafe"}], kinds)
    ontology = resolve(kinds, entities, locations=["cafe"])
    problems = check_entity(ontology, "chair:x", values={"成色": 0.0}, place="cafe")
    assert problems and "look" in problems[0]


def test_做不了不算病():
    """判据是"算得出一个叫得出名字的结论",不是"能成功"。`conditions`(果子还没熟)
    和 `incapable`(她做不了)都是世界在正常说话 —— 把它们算成病的话,自检会对着
    一个完全健康的世界一直报警,然后没人再看它。"""
    kinds = parse_kinds([ACTOR, {
        "id": "chair", "quantities": {"成色": {"default": 1.0, "visibility": "here"}},
        "affordances": {"修": {"when": ["成色 < 0.5"], "requires": ["me_体力 >= 999"],
                              "set": {"成色": "成色 + 0.1"}}},
    }])
    entities = parse_entities([{"id": "chair:x", "location": "cafe"}], kinds)
    ontology = resolve(kinds, entities, locations=["cafe"])
    assert check_entity(ontology, "chair:x", values={"成色": 1.0}, place="cafe") == []


def test_不在任何地方而又有人看得见的量():
    """存在,而没有任何人碰得到 —— 比不存在更糟:它进得了 `entities`、算得进
    统计,却永远不出现在任何人面前。"""
    problems = check_entity(_ontology(), "bench:a", values={"完好": 1.0}, place="")
    assert problems and "不在任何地方" in problems[0]


def test_本体里没有的东西直接报出身():
    assert "没有这个东西" in check_entity(_ontology(), "chair:不存在", values={})[0]


# ── 世界里跑起来 ──────────────────────────────────────────────────────────


TIMBER = {"id": "木料", "name": "一根木料", "kind": "durable", "base_price": 3.0}


def _world(tmp_path, open_world, *, name="birth", bench=None, world_id=None, timber=9):
    seed = {
        "locations": [{"id": "cafe", "name": "咖啡店", "description": "拐角那家"},
                      {"id": "yard", "name": "院子", "description": "后头"}],
        "agents": [{"id": "夏", "name": "夏", "location": "cafe", "personality": "安静",
                    "inventory": [{"item": "木料", "qty": timber}]}],
        "items": [TIMBER],
        "kinds": [ACTOR, CHAIR, bench or BENCH],
        "entities": [{"id": "bench:a", "name": "那条长凳", "location": "cafe"}],
    }
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return open_world(world_id, seed_path=str(path))


def test_到点了才生下来(tmp_path, open_world):
    """代价当场付,而**孩子生在收尾那一刻** —— 生在起头的话,"十月怀胎"就只是
    一句话,那十个月一天也不用过。"""
    world = _world(tmp_path, open_world)
    assert world.scheduler.perform_affordance("夏", "bench:a", "打椅子")["started"]
    assert [e["id"] for e in world.entities()] == ["bench:a"], "四个 tick 还没过"
    assert world.stocks("agent:夏")["体力"] == 80.0, "代价倒是当场付了"
    world.tick(4)
    ids = [e["id"] for e in world.entities()]
    assert len(ids) == 2 and any(i.startswith("chair:") for i in ids)


def test_生下来的东西带着名字位置和量(tmp_path, open_world):
    world = _world(tmp_path, open_world)
    world.scheduler.perform_affordance("夏", "bench:a", "打椅子")
    world.tick(4)
    chair = next(e for e in world.entities("chair"))
    assert chair["name"] == "新打的椅子"
    assert chair["location"] == "cafe", "没写 location 就生在这件事发生的地方"
    # **逐个量填,不是逐个实体填**:作者在 spawn 里写了 成色,不该让这个种类
    # 声明过的其余量一个都不落地(创世那边踩过这个坑)。
    assert chair["values"] == {"成色": 0.8}


def test_出生进世界的历史(tmp_path, open_world):
    world = _world(tmp_path, open_world)
    world.scheduler.perform_affordance("夏", "bench:a", "打椅子")
    world.tick(4)
    birth = [e for e in world.events() if e["type"] == "entity_spawn"][-1]
    assert birth["payload"]["kind"] == "chair"
    assert birth["payload"]["from"] == "bench:a", "是哪一下造出了它,重放的人得读得出"
    assert birth["payload"]["values"] == {"成色": 0.8}


def test_生下来的东西马上就能被交互(tmp_path, open_world):
    """生完不进本体的话,她面前那样东西会被回一句"这儿没有它" —— 而这正是
    "世界里多了一样东西"这句话的全部内容。"""
    world = _world(tmp_path, open_world)
    world.scheduler.perform_affordance("夏", "bench:a", "打椅子")
    world.tick(4)
    chair = world.entities("chair")[0]["id"]
    assert world.scheduler.perform_affordance("夏", chair, "look")["ok"]
    assert world.check_entity(chair)[0]["ok"]


def test_每一个都是新的一个(tmp_path, open_world):
    """id 撞车的后果是后来的那个**覆盖**前一个 —— 世界里少一样东西,没有任何
    地方报错。所以号只增不减,连死者的号也不让出来。"""
    world = _world(tmp_path, open_world)
    for _ in range(3):
        world.scheduler.perform_affordance("夏", "bench:a", "打椅子")
        world.tick(4)
    ids = {e["id"] for e in world.entities("chair")}
    assert len(ids) == 3, ids
    assert world.stocks("agent:夏")["体力"] == 40.0
    assert world.scheduler._memory_projection.inventories["夏"] == {"木料": 9}


def test_抹掉的东西三张表上都没了(tmp_path, open_world):
    """少收一样各有各的安静后果:量留着 → 一个不存在的东西还有高度;位置留着 →
    她的提示词里有一样走过去也摸不到的东西。"""
    world = _world(tmp_path, open_world)
    assert world.scheduler.perform_affordance("夏", "bench:a", "拆掉")["destroyed"]
    assert world.entities() == []
    assert world.stocks("bench:a") == {}
    assert world.scheduler.visibility_store.place_of("bench:a") is None
    gone = [e for e in world.events() if e["type"] == "entity_destroy"][-1]
    assert gone["payload"]["entity"] == "bench:a"


def test_抹掉时挂在它身上的长过程一起解掉(tmp_path, open_world):
    """留着的话,她被一件永远收不了尾的事占着,直到原定的结束 tick 才解脱 ——
    而这中间她什么也做不了,也说不出为什么。"""
    bench = {**BENCH, "affordances": {
        **BENCH["affordances"],
        "拆掉": {"costs": {"体力": "me_体力 - 5"}, "destroys_target": True},
    }}
    world = _world(tmp_path, open_world, name="rip", bench=bench)
    world.scheduler.perform_affordance("夏", "bench:a", "打椅子")
    assert world.engagements("夏")
    # 她被占着,所以拆不动 —— 直接从调度器那一层抹掉(等价于别的进程干的)。
    world.scheduler._destroy_entity("夏", "bench:a", "拆掉", "cafe")
    assert not world.engagements("夏"), "人还挂在一个不存在的东西上"
    gone = [e for e in world.events() if e["type"] == "entity_disengage"][-1]
    assert gone["payload"]["reason"] == "gone"


def test_活不了的东西不许留在世界里(tmp_path, open_world):
    """出生自检**是出生的一部分**。留一个半死不活的东西在世界里,比根本没生出来
    更难查:它进得了 `entities`、算得进统计,而它身上的规律一条也不跑。

    代价不退是有意的:她确实付过了,而这一次失败是**作者的声明坏了** ——
    退给她只会让那个 bug 从账面上也消失。
    """
    broken_chair = {
        "id": "chair", "gloss": "一把椅子",
        "quantities": {"成色": {"default": 0.0, "visibility": "here"}},
        "affordances": {"look": {"when": ["1 / 成色 > 0"]}},   # 除以零,运行期才炸
    }
    seed = {
        "locations": [{"id": "cafe", "name": "咖啡店", "description": "拐角"}],
        "agents": [{"id": "夏", "name": "夏", "location": "cafe", "personality": "静"}],
        "kinds": [ACTOR, broken_chair, {
            **BENCH,
            "affordances": {"打椅子": {"duration": 2, "costs": {"体力": "me_体力 - 20"},
                                     "spawn": {"kind": "chair"}}},
        }],
        "entities": [{"id": "bench:a", "name": "那条", "location": "cafe"}],
    }
    path = tmp_path / "stillborn.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    world = open_world(seed_path=str(path))

    world.scheduler.perform_affordance("夏", "bench:a", "打椅子")
    world.tick(2)
    assert world.entities("chair") == [], "一个算不出自己能力的东西留在了世界里"
    dead = [e for e in world.events() if e["type"] == "entity_stillborn"]
    assert dead and "look" in " ".join(dead[-1]["payload"]["problems"])
    assert world.stocks("agent:夏")["体力"] == 80.0, "代价不退 —— 退了那个 bug 就没痕迹了"
    # 撤回要撤干净:量和位置一样不能留。
    entity_id = dead[-1]["payload"]["entity"]
    assert world.stocks(entity_id) == {}
    assert world.scheduler.visibility_store.place_of(entity_id) is None


def test_别的进程种下的树这个进程也看得见(tmp_path, open_world, fresh_redis):
    """实例表住 Redis(数据是共享的),而每个进程手里的 `Ontology` 是一份缓存。
    不同步的话,A 生下来的东西在 B 眼里根本不存在:B 的 `interact` 回一句
    "这儿没有这个东西",而那东西就在她面前。**两份真相里有一份不更新。**
    """
    first = _world(tmp_path, open_world, name="shared", world_id="shared")
    second = open_world("shared", redis=fresh_redis)
    assert second.entities("chair") == []

    first.scheduler.perform_affordance("夏", "bench:a", "打椅子")
    first.tick(4)
    chair = first.entities("chair")[0]["id"]

    # 另一个进程手里那份还是旧的 —— 直到它下一次真的去问世界。
    assert second.scheduler.perform_affordance("夏", chair, "look")["ok"], \
        "另一个进程看不见刚生下来的东西"


def test_生下来的东西经得起重开(tmp_path, open_world, fresh_redis):
    """它得真的落库,而不是只活在这个进程的内存里。"""
    world = _world(tmp_path, open_world, name="persist", world_id="persist")
    world.scheduler.perform_affordance("夏", "bench:a", "打椅子")
    world.tick(4)
    chair = world.entities("chair")[0]["id"]
    world.close()

    reopened = open_world("persist", redis=fresh_redis)
    assert [e["id"] for e in reopened.entities("chair")] == [chair]
    assert reopened.entities("chair")[0]["values"] == {"成色": 0.8}
    assert reopened.check_entity(chair)[0]["ok"]


def test_生下来的东西进得了_cyberworld(tmp_path, open_world, fresh_redis):
    """`.cyberworld` 是**分发物**,而运行期长出来的东西和创世时写下的东西在里面
    必须一样真。少带走的话,作者跑了三十天养出来的一片林子,打包发出去就只剩
    创世那一棵 —— 而包能正常打开、能正常跑,谁也不会去数。

    连号码机一起带走是这条的第二半:导入后接着数(`sapling:2`),不回头捡
    `sapling:1` 那个号。id 会进事件、进提示词,复用一个死者的号等于让历史指向
    另一样东西。
    """
    from anima_world.world_package import import_world_file

    world = _world(tmp_path, open_world, name="pack", world_id="pack")
    world.scheduler.perform_affordance("夏", "bench:a", "打椅子")
    world.tick(4)
    chair = world.entities("chair")[0]
    out = tmp_path / "x.cyberworld"
    world.export_snapshot(str(out), world_id="pkg", name="打包过的世界")
    world.close()

    target = type(fresh_redis)(decode_responses=True)
    import_world_file(str(out), redis=target, world_id="landed")
    landed = open_world("landed", redis=target)
    rows = landed.entities("chair")
    assert [r["id"] for r in rows] == [chair["id"]]
    assert rows[0]["values"] == chair["values"] and rows[0]["name"] == chair["name"]
    assert landed.check_entity(chair["id"])[0]["ok"]
    assert landed.scheduler.ontology_store.mint_id("chair") != chair["id"], \
        "号码机没跟着走 —— 下一个新生的东西会顶掉这一个"
