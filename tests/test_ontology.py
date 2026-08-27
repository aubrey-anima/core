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
    unreachable_requirements,
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


def test_逐条写的初值当场开不了机(tmp_path, open_world):
    """`{"owner","key","value"}` —— 引擎只认 `{"owner","values"}` 那一种写法。

    从前是一条 warning 加跳过。灯塔湾那份世界文件就是这么丢的:11 行初值一个都没
    进世界,五个角色的 `initiative` / `agreeableness` 停在声明的默认值上,世界照常
    开机、日志干净,而引用它们的条件算出来的全是同一个数。和量名拼错是同一类错误 ——
    你写的这一行没有人读,而这个判断不需要知道任何世界的内容。
    """
    from anima_world.world_seed import WorldSeedError

    with pytest.raises((OntologyError, WorldSeedError)) as caught:
        _seeded(tmp_path, open_world, [{"owner": "agent:甲", "key": "功力", "value": 1.0}])
    assert "values" in str(caught.value)


def test_装载器自己也不肯丢掉读不懂的初值():
    """两道闸各自独立:校验在动任何一张表之前拦,装载器是最后一道。

    只留前一道的话,任何绕开校验的入口(库里直接调、状态记录混装的文件)照旧会
    安静地少装一批初值 —— 而那正是这个 bug 上一次发生的样子。
    """
    from anima_world.__main__ import _seed_stocks

    class _Store:
        def __init__(self):
            self.written = []

        def owners(self):
            return []

        def of(self, owner):
            return {}

        def set_many(self, owner, values, *, tick=0):
            self.written.append((owner, values))

    store = _Store()
    with pytest.raises(OntologyError, match="values"):
        _seed_stocks({"stocks": [{"owner": "agent:甲", "key": "功力", "value": 1.0}]}, store)

    # 值不是数也一样:那个量会停在声明的默认值上,而作者以为他给过初值了。
    with pytest.raises(OntologyError, match="不是一个数"):
        _seed_stocks({"stocks": [{"owner": "agent:甲", "values": {"功力": "很高"}}]}, store)


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


def test_重开一个世界不会被塞进别人的物理法则(tmp_path, open_world, fresh_redis):
    """**播种是创世那一刻的事,不是"这张表恰好还空着"的事。**

    按空表判断,只在第一次开机时和创世重合。之后每一次开机,手里这份种子(缺省是
    包自带的橱窗)都会去填当初作者**有意留空**的那几张表 —— 而规律是这个世界的
    物理法则:一个作者写了 `kinds` 却没写 `rules` 的世界,重开一次就会被塞进橱窗
    那条"树会长高"的规律,而它引用的 `tree` 这个种类在这个世界里根本不存在。

    下场不是算错,是**这个世界从此打不开**(`resolve` 当场拒绝整个本体)。
    创作台的整套流程都是自定义种子,所以这条一撞一个准。
    """
    import json

    seed = {
        "locations": [{"id": "cafe", "name": "咖啡店", "description": "拐角那家"}],
        "agents": [{"id": "甲", "name": "甲", "location": "cafe", "personality": "安静"}],
        # 声明了种类,**没写 rules** —— 这个世界的作者不要任何物理法则。
        "kinds": [{"id": "bench", "gloss": "长凳",
                   "quantities": {"完好": {"default": 1.0, "visibility": "here"}},
                   "affordances": {"look": {}}}],
        "entities": [{"id": "bench:a", "name": "那条", "location": "cafe"}],
    }
    path = tmp_path / "no_rules.json"
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    world = open_world("norules", seed_path=str(path))
    # ⚠️ **比的是"作者的规律",不是"一条规律都没有"**(2026-08-26 第 2 期 2e 改)。
    # 出厂插件也把自己那几条挂在同一张表上(`invitation.过期` 没有开关、永远装),
    # 而它们是**引擎声明的机制**,不是这个作者的物理法则 —— 两者的分界线就是
    # `scheduler.plugin_rule_ids`,那张表本来就是为这件事存在的。
    # **这条用例真正怕的那件事一格没松**:下面重开那一次,橱窗那条引用 `tree` 的
    # 生长规律照旧不许进来。
    def _authored_rules(w):
        return [r for r in w.scheduler.world_rules
                if r.id not in w.scheduler.plugin_rule_ids]

    assert _authored_rules(world) == [], "作者没写规律,世界就不该有规律"
    world.close()

    # 重开 —— 这次手里是包自带的橱窗种子,它的规律引用 `tree`,而这里没有 tree。
    reopened = open_world("norules", redis=fresh_redis)
    assert _authored_rules(reopened) == [], "别人的物理法则被塞了进来"
    assert [e["id"] for e in reopened.entities()] == ["bench:a"]


# ── 永远开不了的那道门 ──────────────────────────────────────────────────────
#
# 线上真有一条,而且它跑了几千条事件都没人发现:晚潮的 `poster.撕下来` 要
# `me_主动 >= 1.2`,「主动」默认 1.0,整个世界里唯一写它的表达式是
# `max(me_主动 - 0.02, 0)` —— 只减不增。那个按钮从开机第一秒起就不可能亮。
#
# 玩家看得见它、点得到它,每次收到「你的主动不够」。他会一直试。这是最伤的一类:
# 系统告诉他「你还差一点」,而那一点在数学上不存在。

_ACTOR_ONLY_SHRINKS = {
    "id": "agent",
    "quantities": {"主动": {"default": 1.0, "visibility": "self"}},
}
_POSTER = {
    "id": "poster",
    "gloss": "墙上那张",
    "quantities": {"完好": {"default": 1.0, "visibility": "here"}},
    "affordances": {
        "撕下来": {"label": "撕下来", "requires": ["me_主动 >= 1.2"],
                   "costs": {"主动": "max(me_主动 - 0.02, 0)"}},
    },
}


def test_门槛比这个量够得到的最高点还高_当场点名():
    said = " ".join(unreachable_requirements(_kinds(_ACTOR_ONLY_SHRINKS, _POSTER)))
    assert "撕下来" in said and "主动" in said
    assert "1.2" in said and "永远开不了" in said
    # 要说得出**下一步**:降门槛,或者给这个量一条涨上去的路。
    assert "涨上去" in said


def test_有一处抬得动它就不点名():
    """`min(me_主动 + 0.03, 3)` 顶得到 3,门槛 1.2 够得着 —— 一个字都不该说。"""
    lifts = {
        **_POSTER,
        "affordances": {
            **_POSTER["affordances"],
            "重描一遍": {"label": "重描一遍", "costs": {"主动": "min(me_主动 + 0.03, 3)"}},
        },
    }
    assert unreachable_requirements(_kinds(_ACTOR_ONLY_SHRINKS, lifts)) == []


def test_规律抬得动它也算():
    """写点不只在能力里 —— 少数一处就会多报一条,而多报的那条指着一道开得了的门。"""
    rules = parse_rules([{"id": "缓过来", "every": {"ticks": 12},
                          "for_each": {"kind": "agent"},
                          "set": {"主动": "min(主动 + 0.05, 2)"}}])
    assert unreachable_requirements(_kinds(_ACTOR_ONLY_SHRINKS, _POSTER), rules) == []


def test_认不出的写法一律闭嘴():
    """误报够多次的警告等于没有警告 —— `reachable_ceiling` 不认识的一律算「不知道」。"""
    murky = {
        **_POSTER,
        "affordances": {
            **_POSTER["affordances"],
            "说不好": {"label": "说不好", "costs": {"主动": "me_主动 * 完好"}},
        },
    }
    assert unreachable_requirements(_kinds(_ACTOR_ONLY_SHRINKS, murky)) == []


def test_max_的下限是一种抬升_不是保底():
    """这一条是这道 lint 唯一容易写反的地方。

    `max(me_主动 - 0.02, 5)` 直觉上是"只减不增再保个底",实际上它把一个 1.0 的量
    **顶到 5**。当成非增处理的话,这道 lint 会去报一道其实开得了的门。
    """
    floored = {
        **_POSTER,
        "affordances": {
            "撕下来": {"label": "撕下来", "requires": ["me_主动 >= 1.2"],
                       "costs": {"主动": "max(me_主动 - 0.02, 5)"}},
        },
    }
    assert unreachable_requirements(_kinds(_ACTOR_ONLY_SHRINKS, floored)) == []


def test_够得到的门槛不点名():
    ok = {
        **_POSTER,
        "affordances": {
            "撕下来": {"label": "撕下来", "requires": ["me_主动 >= 0.5"],
                       "costs": {"主动": "max(me_主动 - 0.02, 0)"}},
        },
    }
    assert unreachable_requirements(_kinds(_ACTOR_ONLY_SHRINKS, ok)) == []


def test_没有任何一处写它的量也算得出上界():
    """一个谁都不写的量,上界就是它的默认值 —— 这是最容易漏的一种「够不到」。"""
    frozen = {
        "id": "poster",
        "gloss": "墙上那张",
        "quantities": {"完好": {"default": 1.0, "visibility": "here"}},
        "affordances": {"撕下来": {"label": "撕下来", "requires": ["me_主动 >= 1.2"]}},
    }
    said = " ".join(unreachable_requirements(_kinds(_ACTOR_ONLY_SHRINKS, frozen)))
    assert "没有任何一处写它" in said


def test_have_不归这道闸管():
    """随身物品想买就买得到,没有"够不着"这回事 —— 报它就是误报。"""
    with_item = {
        **_POSTER,
        "affordances": {"撕下来": {"label": "撕下来", "requires": ["have_刀片 >= 3"]}},
    }
    assert unreachable_requirements(_kinds(_ACTOR_ONLY_SHRINKS, with_item)) == []


def test_按流逝折算的衰减也算得出上界():
    """`量 - 0.006 * dt` 是这个引擎**劝作者写**的那一种(`drift_warnings` 整条在劝它)。

    不认 `dt` 的话每一条正确写法都算成"不知道",这道 lint 在真实世界里永远不响 ——
    一个只在教科书写法上生效的 lint 等于没有。
    """
    rules = parse_rules([{"id": "耗着", "every": {"ticks": 12},
                          "for_each": {"kind": "agent"},
                          "set": {"主动": "max(主动 - 0.006 * dt, 0)"}}])
    said = " ".join(unreachable_requirements(_kinds(_ACTOR_ONLY_SHRINKS, _POSTER), rules))
    assert "撕下来" in said, "规律只往下走,门槛 1.2 仍然够不到"


def test_规律读光名字_能力读me_前缀_别搞混():
    """两边读自己的写法不一样,而搞混不会报错 —— 只会给出一个假的上界。

    `min(主动 + 0.06, 2)` 这条规律真的把上界抬到 2。要是拿 `me_主动` 去它的语法树上
    找,找不到、答 `inf`,**恰好也不报** —— 于是这个洞在"报不报"这一层看不出来。
    分得开它的只有反过来那条:一条封得住的衰减(`max(主动 - 0.006 * dt, 0)`),
    名字对得上才算得出 1.0,对不上就是 `inf`,而 `inf` 会把同一个量的上界顶穿。
    """
    lifts = parse_rules([{"id": "缓过来", "every": {"ticks": 12},
                          "for_each": {"kind": "agent"},
                          "set": {"主动": "min(主动 + 0.06, 2)"}}])
    assert unreachable_requirements(_kinds(_ACTOR_ONLY_SHRINKS, _POSTER), lifts) == []

    # 同一个量再加一条只往下走的规律:上界还是 2(抬得动那条说了算),仍然不报。
    both = parse_rules([
        {"id": "缓过来", "every": {"ticks": 12}, "for_each": {"kind": "agent"},
         "set": {"主动": "min(主动 + 0.06, 2)"}},
        {"id": "耗着", "every": {"ticks": 12}, "for_each": {"kind": "agent"},
         "set": {"主动": "max(主动 - 0.006 * dt, 0)"}},
    ])
    assert unreachable_requirements(_kinds(_ACTOR_ONLY_SHRINKS, _POSTER), both) == []


# ── `kind_keys`:这一版引擎读得到的那几格,契约上问得到(3.7.0)──────────────


def test_契约那一格和真读得到的格子对得上():
    """**这一格是回执,不是装饰。** `parent`(单继承 + 加载期 copy-down)从 2.0 起
    就能用,而 `docs/FOR-STUDIO.md` §3.7 到 2026-08-21 都没写过它 —— 于是创作台
    不认这一格,对着一份**完全合法**的声明产出过一条假红。

    钉的是"契约里报的每一格,引擎真的读它":一格报了而不读,消费方按它写出来的
    世界会被静默丢掉一半;一格读了而不报,就是上面那条假红本身。

    ⚠️ **这里不能靠"写一个不认识的键看它红不红"来反证** —— 种类级的不认识字段
    今天被**静默忽略**(和能力级当场开不了机不同,见 `KIND_KEYS` 的注释)。
    所以每一格都用**它自己的效果**来验。
    """
    from anima_world.__main__ import contract_payload
    from anima_world.ontology import KIND_KEYS

    assert contract_payload()["seed"]["kind_keys"] == sorted(KIND_KEYS)

    kinds = _kinds(
        {"id": "树", "gloss": "一棵树",
         "quantities": {"树高": {"default": 1.0, "visibility": "here"}},
         "affordances": {"照料": {"set": {"树高": "树高 + 1"}}},
         "prompt": {"budget": 3}},
        {"id": "橡树", "parent": "树", "quantities": {"年轮": 0.0}},
    )
    tree, oak = kinds["树"], kinds["橡树"]
    seen = {
        "id": tree.id == "树",
        "gloss": tree.gloss == "一棵树",
        "quantities": "树高" in tree.quantities,
        "affordances": "照料" in tree.affordances,
        "prompt": tree.prompt is not None and tree.prompt.budget == 3,
        # copy-down:父的量与能力都在,子自己那一格也在。
        "parent": {"树高", "年轮"} <= set(oak.quantities)
        and "照料" in oak.affordances,
    }
    assert set(seen) == set(KIND_KEYS), sorted(set(seen) ^ set(KIND_KEYS))
    assert all(seen.values()), [k for k, v in seen.items() if not v]


def test_继承是合并不是替换_而且父写在后面也认():
    """两条会被写错的:**合并的粒度是每个量名 / 每个动词**,不是整张表替换;
    **校验顺序不等于书写顺序**(父写在子后面照样认,和 `me_*` 那条同一理由)。"""
    kinds = _kinds(
        {"id": "橡树", "parent": "树",
         "quantities": {"树高": {"default": 3.0, "visibility": "here"}}},
        {"id": "树", "gloss": "一棵树",
         "quantities": {"树高": {"default": 1.0, "visibility": "here"},
                        "最大树高": 12.0},
         "affordances": {"照料": {"set": {"树高": "树高 + 1"}}, "端详": {}}},
    )
    oak = kinds["橡树"]
    assert oak.quantities["树高"].default == 3.0, "子覆盖同名那一格"
    assert oak.quantities["最大树高"].default == 12.0, "没提到的那一格照旧继承下来"
    assert sorted(oak.affordances) == sorted(["端详", "照料"]), "能力整张表继承"
    assert oak.gloss == "一棵树", "gloss 没写就继承父的"
