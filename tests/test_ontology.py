"""本体层:声明能不能拦住"照跑但给错东西"。

这一层唯一的价值就是**拒绝**。它不让世界多做任何事,只让一类特定的错从"零报错、
零日志、安静地少跑一半"变成"开不了机"。所以这份测试几乎全是 `pytest.raises` ——
每条都对着一个具体的、真发生过的坏法。
"""

from __future__ import annotations

import pytest

from anima_world.ontology import (
    BUILTIN_KINDS,
    apply_affordance,
    OntologyError,
    parse_entities,
    parse_kinds,
    resolve,
    seed_quantities,
    visibility_declarations,
)
from anima_world.rules import parse_rules

TREE = {
    "id": "tree",
    "gloss": "一棵树",
    "quantities": {
        "size": {"default": 1.0, "visibility": "here", "label": "树高", "unit": "米"},
        "growth_rate": 0.05,
    },
    "affordances": ["look", "tend", "harvest"],
    "prompt": {"budget": 3},
}


def _kinds(*entries):
    return parse_kinds(list(entries))


def test_空声明得到的是只有内置种类的符号表():
    kinds = parse_kinds(None)
    assert set(kinds) == set(BUILTIN_KINDS)
    assert all(kind.builtin for kind in kinds.values())


def test_种类声明编译出量与能力():
    kind = _kinds(TREE)["tree"]
    assert kind.quantity_names() == {"size", "growth_rate"}
    assert kind.quantities["size"].visibility == "here"
    assert kind.quantities["size"].render_label() == "树高"
    # 简写形式:只给默认值,可见性回落到 hidden。
    assert kind.quantities["growth_rate"].default == 0.05
    assert kind.quantities["growth_rate"].visibility == "hidden"
    assert tuple(kind.affordances) == ("look", "tend", "harvest")
    assert kind.prompt is not None and kind.prompt.budget == 3


def test_继承在加载期展开_运行期看不见父类():
    kinds = _kinds(TREE, {"id": "oak", "parent": "tree", "quantities": {"acorns": 0.0}})
    oak = kinds["oak"]
    assert oak.quantity_names() == {"size", "growth_rate", "acorns"}
    assert tuple(oak.affordances) == ("look", "tend", "harvest")   # copy-down
    assert oak.gloss == "一棵树"                             # gloss 也继承
    assert not hasattr(oak, "parent")


def test_子类可以覆盖父类的同名量():
    kinds = _kinds(TREE, {"id": "oak", "parent": "tree", "quantities": {"size": 5.0}})
    assert kinds["oak"].quantities["size"].default == 5.0
    assert kinds["tree"].quantities["size"].default == 1.0


@pytest.mark.parametrize(
    "entry, fragment",
    [
        ({"id": "location"}, "内置种类"),
        # `agent` 是唯一开了口子的内置种类,而口子**只开在 quantities 上**。
        ({"id": "agent", "affordances": ["tend"]}, "只能声明 quantities"),
        ({"id": "agent", "gloss": "一个人"}, "只能声明 quantities"),
        ({"id": "t", "parent": "agent"}, "不能当父类"),
        ({"id": "tree:oak"}, "冒号"),
        ({"id": "t", "quantities": {"world_季节": 1.0}}, "world_"),
        ({"id": "t", "quantities": {"size": {"visibility": "everyone"}}}, "可见档"),
        ({"id": "t", "affordances": ["fly"]}, "affordance"),
        ({"id": "t", "parent": "nope"}, "引用不到"),
        ({"id": "t", "quantities": {"a": 1.0}, "prompt": {"select": "here"}}, "不认识的字段"),
        ({"id": "t", "quantities": {"a": 1.0}, "prompt": {"budget": 0}}, "budget"),
    ],
)
def test_坏种类声明当场拒绝(entry, fragment):
    with pytest.raises(OntologyError) as caught:
        parse_kinds([entry])
    assert fragment in str(caught.value)


def test_声明了prompt就必须有gloss():
    # 进提示词的是那一行人话。没有它,渲染出来只有一张属性表。
    with pytest.raises(OntologyError, match="gloss"):
        parse_kinds([{"id": "t", "quantities": {"a": 1.0}, "prompt": {"budget": 2}}])


def test_继承成环被拒():
    with pytest.raises(OntologyError, match="成环"):
        parse_kinds([
            {"id": "a", "parent": "b"},
            {"id": "b", "parent": "a"},
        ])


def test_坏声明一次报完而不是报第一条():
    with pytest.raises(OntologyError) as caught:
        parse_kinds([{"id": "location"}, {"id": "tree:oak"}])
    assert len(caught.value.errors) == 2


# ── 实例 ────────────────────────────────────────────────────────────────────


def test_实例的种类引用当场解析():
    kinds = _kinds(TREE)
    entities = parse_entities([{"id": "tree:oak_01", "name": "老橡树"}], kinds)
    assert entities["tree:oak_01"].kind == "tree"
    assert entities["tree:oak_01"].local_id == "oak_01"


def test_实例引用不到种类时错误里带三元组():
    """`trees:` 对 `tree` —— 这是这一整层的立身之本。"""
    kinds = _kinds(TREE)
    with pytest.raises(OntologyError) as caught:
        parse_entities([{"id": "trees:oak_01"}], kinds)
    message = str(caught.value)
    assert "entities[0] (trees:oak_01)" in message   # 谁要的
    assert "kind" in message                          # 要什么类型
    assert "'trees'" in message                       # 要的名字
    assert "'tree'" in message                        # 拼写候选


def test_实例id必须是种类冒号名字的形状():
    with pytest.raises(OntologyError, match="种类:名字"):
        parse_entities([{"id": "oak_01"}], _kinds(TREE))


def test_内置种类的实例不在这里登记():
    with pytest.raises(OntologyError, match="内置种类"):
        parse_entities([{"id": "agent:夏"}], _kinds(TREE))


# ── 第二阶段:解析全部引用 ──────────────────────────────────────────────────


def _rule(**overrides):
    entry = {
        "id": "长大",
        "every": {"ticks": 12},
        "for_each": {"kind": "tree"},
        "set": {"size": "size + growth_rate * dt"},
    }
    entry.update(overrides)
    return parse_rules([entry])


def test_一条好规律解析得过():
    ontology = resolve(_kinds(TREE), {}, rules=_rule())
    assert ontology.kinds["tree"].gloss == "一棵树"


def test_规律指向不存在的种类当场报错():
    """这就是那个零报错的洞:规律写 `tree`,世界里是 `trees:`,一条都不跑。"""
    with pytest.raises(OntologyError) as caught:
        resolve(_kinds(TREE), {}, rules=_rule(for_each={"kind": "trees"}))
    assert "rules (长大).for_each.kind" in str(caught.value)
    assert "'trees'" in str(caught.value)


def test_规律写到没声明的量上被拒():
    # 写下去会凭空造出一个没人知道、也没有可见性声明的量。
    with pytest.raises(OntologyError, match="凭空造出"):
        resolve(_kinds(TREE), {}, rules=_rule(set={"高度": "size + 1"}))


def test_规律读了没声明的量被拒():
    # 读到的会恒为 0,于是这条规律静默地永不触发。
    with pytest.raises(OntologyError, match="恒为 0"):
        resolve(_kinds(TREE), {}, rules=_rule(set={"size": "size + 阳光"}))


def test_dt与全局量前缀不算未声明():
    rules = _rule(set={"size": "size + growth_rate * dt * world_日照"})
    resolve(_kinds(TREE), {}, rules=rules)


def test_规律的owner选择器要指向真实存在的实例():
    kinds = _kinds(TREE)
    entities = parse_entities([{"id": "tree:oak_01"}], kinds)
    resolve(kinds, entities, rules=_rule(for_each={"owner": "tree:oak_01"}))
    with pytest.raises(OntologyError) as caught:
        resolve(kinds, entities, rules=_rule(for_each={"owner": "tree:elm_09"}))
    assert "entity" in str(caught.value)


def test_作用在角色身上的规律不被本体层拦():
    """内置种类的实例名单不在这一层 —— 它只认得命名空间。"""
    resolve(_kinds(TREE), {}, rules=_rule(for_each={"owner": "agent:夏"}, set={"功力": "功力 + dt"}))


def test_动作选择器不查_因为动作是开集():
    resolve(_kinds(TREE), {}, rules=_rule(for_each={"action": "打坐"}, set={"功力": "功力 + dt"}))


def test_实例的location引用不到地点时报错():
    kinds = _kinds(TREE)
    entities = parse_entities([{"id": "tree:oak_01", "location": "森林"}], kinds)
    resolve(kinds, entities, locations=["森林", "cafe"])
    with pytest.raises(OntologyError) as caught:
        resolve(kinds, entities, locations=["cafe"])
    assert "location" in str(caught.value)


# ── 交给别的层的东西 ────────────────────────────────────────────────────────


def test_出生时带上种类声明的默认值():
    kinds = _kinds(TREE)
    entities = parse_entities([{"id": "tree:oak_01"}], kinds)
    ontology = resolve(kinds, entities)
    assert seed_quantities(ontology, entities["tree:oak_01"]) == {"size": 1.0, "growth_rate": 0.05}


def test_种类声明同时就是可见性声明():
    """量的可见性是量声明的一部分,不必在认知层再写一遍。"""
    ontology = resolve(_kinds(TREE), {})
    declared = {(kind, key): (vis, label) for kind, key, vis, label in visibility_declarations(ontology)}
    assert declared[("tree", "size")] == ("here", "树高")
    # hidden 就是"没有行" —— 显式声明它和不声明行为相同,不该往表里塞噪音。
    assert ("tree", "growth_rate") not in declared
    # 内置种类的量不归这一层声明。
    assert not any(kind in BUILTIN_KINDS for kind, _ in declared)


def test_owner到量的外键():
    kinds = _kinds(TREE)
    entities = parse_entities([{"id": "tree:oak_01"}], kinds)
    ontology = resolve(kinds, entities)
    assert ontology.is_declared_owner("tree:oak_01")
    assert ontology.is_declared_owner("agent:夏")      # 内置命名空间
    assert not ontology.is_declared_owner("tree:elm_09")
    assert not ontology.is_declared_owner("boat:小舟")
    assert ontology.declared_quantities("tree:oak_01") == {"size", "growth_rate"}


# ── Redis 存储 ──────────────────────────────────────────────────────────────


def _store(fresh_redis):
    from anima_world.redis_state import RedisOntologyStore

    return RedisOntologyStore(fresh_redis, "w")


def test_种类与实例住在物理分开的两张表(fresh_redis):
    store = _store(fresh_redis)
    store.seed([TREE], [{"id": "tree:oak_01", "name": "老橡树"}], "now")
    assert fresh_redis.hkeys("anima:w:kinds") == ["tree"]
    assert fresh_redis.hkeys("anima:w:entities") == ["tree:oak_01"]


def test_播种只填缺不覆盖(fresh_redis):
    store = _store(fresh_redis)
    store.seed([TREE], [{"id": "tree:oak_01"}], "now")
    store.seed([{"id": "boat", "quantities": {"hull": 1.0}}], [], "later")
    assert [d["id"] for d in store.kind_definitions()] == ["tree"]


def test_读的时候编译_坏声明当场报错(fresh_redis):
    store = _store(fresh_redis)
    store.seed([TREE], [{"id": "trees:oak_01"}], "now")
    with pytest.raises(OntologyError, match="引用不到"):
        store.load()


def test_读回来的本体和直接编译的一样(fresh_redis):
    store = _store(fresh_redis)
    store.seed([TREE], [{"id": "tree:oak_01", "name": "老橡树"}], "now")
    ontology = store.load(rules=_rule())
    assert ontology.entities["tree:oak_01"].name == "老橡树"
    assert ontology.kinds["tree"].quantities["size"].default == 1.0


def test_运行期能种一棵树_但种类照样要解析得到(fresh_redis):
    store = _store(fresh_redis)
    store.seed([TREE], [], "now")
    entity = store.add_entity({"id": "tree:elm_09", "name": "小榆树"}, "later")
    assert entity.kind == "tree"
    assert "tree:elm_09" in store.load().entities
    with pytest.raises(OntologyError, match="引用不到"):
        store.add_entity({"id": "boat:小舟"}, "later")


def test_种类没有运行期入口_因为规律按种类校验(fresh_redis):
    """种类冻结是事件溯源逼的:运行期新增种类 = 规律的合法性随时间变化。"""
    store = _store(fresh_redis)
    assert not hasattr(store, "add_kind")


# ── 能力的效果:她说得出,也做得到 ──────────────────────────────────────────

TENDABLE = {
    "id": "tree",
    "gloss": "一棵树",
    "quantities": {
        "size": {"default": 1.0, "visibility": "here"},
        "max_size": 12.0,
    },
    "affordances": {
        "look": {},
        "tend": {"when": ["size < max_size"], "set": {"size": "min(size + 0.5, max_size)"}},
    },
}


def test_列表写法与对象写法编译出同一批动词():
    """列表是对象的退化,不是"旧格式" —— 一个只能看的东西没有效果可写。"""
    listed = _kinds({"id": "t", "affordances": ["look", "tend"]})["t"]
    assert tuple(listed.affordances) == ("look", "tend")
    assert not listed.affordances["tend"].changes_world


def test_效果编译成受限表达式():
    tend = _kinds(TENDABLE)["tree"].affordances["tend"]
    assert tend.changes_world
    assert not _kinds(TENDABLE)["tree"].affordances["look"].changes_world


def test_照料真的改到量():
    tend = _kinds(TENDABLE)["tree"].affordances["tend"]
    outcome = apply_affordance(tend, values={"size": 1.0, "max_size": 12.0})
    assert outcome.ok and outcome.updates == {"size": 1.5}


def test_条件不成立时是拒绝而不是无事发生():
    """**"没成"和"成了但没变"必须分得开。** 混成一种,她就永远学不会这棵树已经封顶
    —— 她会一直照料一棵长不动的树,而每次都收到"好的"。"""
    tend = _kinds(TENDABLE)["tree"].affordances["tend"]
    outcome = apply_affordance(tend, values={"size": 12.0, "max_size": 12.0})
    assert not outcome.ok
    assert "size < max_size" in outcome.refusal   # 拒绝要说出是哪一条不成立
    assert not outcome.updates


def test_能力读得到全局量():
    kind = _kinds({
        "id": "t", "gloss": "x", "quantities": {"size": 1.0},
        "affordances": {"tend": {"set": {"size": "size + world_雨天数"}}},
    })["t"]
    outcome = apply_affordance(kind.affordances["tend"], values={"size": 1.0},
                               world_values={"雨天数": 3.0})
    assert outcome.updates == {"size": 4.0}


@pytest.mark.parametrize(
    "affordances, fragment",
    [
        # 写到没声明的量上:会在它身上凭空造出一个没人知道、没有可见性的量
        ({"tend": {"set": {"高度": "1"}}}, "凭空造出"),
        # 读没声明的量:恒为 0,于是这个能力静默地永远做同一件事
        ({"tend": {"set": {"size": "size + 阳光"}}}, "恒为 0"),
        # 跨实体写:和规律那一层同一条闸
        ({"tend": {"set": {"world_季节": "1"}}}, "写不到全局量"),
        ({"tend": {"set": {"mine:north.储量": "1"}}}, "写不到别的实体"),
        # 门槛事件归规律那一层,能力只管这一下改了什么
        ({"tend": {"emit": [{"type": "x"}]}}, "不认识的字段"),
        ({"tend": {"when": "size > 1"}}, "when 必须是表达式列表"),
        ({"tend": {"set": {"size": "size +"}}}, "size +"),
    ],
)
def test_坏效果声明当场拒绝(affordances, fragment):
    with pytest.raises(OntologyError) as caught:
        parse_kinds([{"id": "tree", "quantities": {"size": 1.0}, "affordances": affordances}])
    assert fragment in str(caught.value)


def test_子类继承父类的能力连效果一起():
    kinds = _kinds(TENDABLE, {"id": "oak", "parent": "tree"})
    outcome = apply_affordance(kinds["oak"].affordances["tend"],
                               values={"size": 1.0, "max_size": 12.0})
    assert outcome.updates == {"size": 1.5}


# ── 创世那一刻:声明与种子里的显式值怎么合 ──────────────────────────────────
#
# 这两条守的是同一个坏法的两副面孔:**种子里的 `stocks` 和种类声明各说各的**。
# 它们都不报错、不打日志,只是让世界安静地少跑一半 —— 而这正是本体层存在的理由。


_DECLARED = {
    "id": "tree",
    "gloss": "一棵树",
    "quantities": {
        "树高": {"default": 1.5, "visibility": "here"},
        "最大树高": 12.0,
        "生长速度": 0.004,
    },
}


def _seeded(tmp_path, open_world, stocks):
    import json

    seed = {
        "locations": [{"id": "cafe", "name": "咖啡店", "description": "拐角那家"}],
        "agents": [{"id": "甲", "name": "甲", "location": "cafe", "personality": "安静"}],
        "kinds": [_DECLARED],
        "entities": [{"id": "tree:a", "name": "那棵", "location": "cafe"}],
        "stocks": stocks,
    }
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return open_world(seed_path=str(path))


def test_种子写了一个量_声明的其余量照样落地(tmp_path, open_world):
    """**逐个量填,不是逐个实体填。**

    按实体跳的坏法:作者在种子里给那棵树写了个 `树高`,于是 `最大树高` 与
    `生长速度` 一个都不落地 —— `tend` 的条件 `树高 < 最大树高` 求不出值、生长
    规律算不动。两件事都只是安静地不发生,而那棵树看上去好好地立在那儿。
    """
    world = _seeded(tmp_path, open_world, [{"owner": "tree:a", "values": {"树高": 3.0}}])
    assert world.stocks("tree:a") == {"树高": 3.0, "最大树高": 12.0, "生长速度": 0.004}


def test_种子里的显式值不被声明的默认值盖掉(tmp_path, open_world):
    """只填缺 —— 作者写的 3.0 赢过声明的 1.5,否则种子那一行等于白写。"""
    world = _seeded(tmp_path, open_world, [{"owner": "tree:a", "values": {"树高": 3.0}}])
    assert world.stocks("tree:a")["树高"] == 3.0


def test_一个都不写时全套默认值落地(tmp_path, open_world):
    world = _seeded(tmp_path, open_world, [])
    assert world.stocks("tree:a") == {"树高": 1.5, "最大树高": 12.0, "生长速度": 0.004}


def test_种子把量名拼错时开不了机(tmp_path, open_world):
    """`树髙` 和 `树高` 差一个字。

    不拦的样子:`树髙` 安静地建成第二个量,`树高` 停在声明的默认值上,规律照跑、
    日志干净,而作者要到某天发现那棵树三个月没长过才知道。声明过 `kinds` 的世界
    里引擎有资格判断这是笔误 —— 作者已经说过"我声明了这个世界有什么"。
    """
    with pytest.raises(OntologyError) as caught:
        _seeded(tmp_path, open_world, [{"owner": "tree:a", "values": {"树髙": 9.0}}])
    # 报的是**声明过哪些**,不是一句"不认识" —— 差一个字的名字要摆在一起才看得出。
    assert "树髙" in str(caught.value) and "树高" in str(caught.value)


def test_没声明kinds的世界照旧不拦(tmp_path, open_world):
    """**声明本身就是开关。** 没有 `kinds` 时 owner 和量名都是任意字符串,

    引擎无从判断 `树髙` 是笔误还是作者新造的量 —— 这时拦下来才是错的。
    """
    import json

    seed = {
        "locations": [{"id": "cafe", "name": "咖啡店", "description": "拐角那家"}],
        "agents": [{"id": "甲", "name": "甲", "location": "cafe", "personality": "安静"}],
        "stocks": [{"owner": "tree:a", "values": {"随便什么名字": 9.0}}],
    }
    path = tmp_path / "bare.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    world = open_world(seed_path=str(path))
    assert world.stocks("tree:a") == {"随便什么名字": 9.0}


def test_内置种类身上的量不走闸(tmp_path, open_world):
    """`world` / `agent` 的量不在本体里声明(角色在投影里,全局量按世界而异)——

    拿一个空的声明集去拦它们,等于把 `世界.季节` 也判成笔误。
    """
    import json

    seed = {
        "locations": [{"id": "cafe", "name": "咖啡店", "description": "拐角那家"}],
        "agents": [{"id": "甲", "name": "甲", "location": "cafe", "personality": "安静"}],
        "kinds": [_DECLARED],
        "entities": [{"id": "tree:a", "name": "那棵", "location": "cafe"}],
        "stocks": [{"owner": "world", "values": {"季节": 2.0}},
                   {"owner": "agent:甲", "values": {"功力": 1.0}}],
    }
    path = tmp_path / "builtin.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    world = open_world(seed_path=str(path))
    assert world.stocks("world") == {"季节": 2.0}
    assert world.stocks("agent:甲") == {"功力": 1.0}
