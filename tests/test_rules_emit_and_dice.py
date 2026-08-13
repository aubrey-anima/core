"""规律的三件事:门槛事件说得出轻重、双边沿、可重放的骰子。

这三条补的是同一个洞的三个方向 —— **世界的历史和角色的经历原本是两个不相交的
集合**,而且世界里没有任何偶然性:

- 线上那个雨季世界的高潮「江水漫堤」发生了,四个角色关于它的记忆是 **0 条**。
  emit 出来的事件只进日志,没有轻重、没有一句她记得住的话。→ `importance` / `text`
- 汛期年复一年,而 emit 只在"由假变真"那一下发,于是「江水漫堤」一生只有一次
  机会,水位顶死上限 17 个世界日也不再吭声。→ `on: rise | fall | both`
- 雨势永远是常数 0.8:阵雨、意外、运气在这一层根本表达不出来,因为表达式禁随机
  (replay 纪律)。→ `rand()`,`(world_id, rule_id, owner, tick)` 折出来的骰子

三条**各有一条测试盯着"不写这个字段的世界逐位不变"** —— 声明本身就是开关,
和 perception / ontology 逐字同构。
"""
from __future__ import annotations

import json
import logging

import pytest

from anima_world.expressions import (
    ExpressionError, compile_expression, world_dice,
)
from anima_world.rules import RuleError, drift_warnings, parse_rules
from anima_world.stocks import evaluate_due


# ── 夹具:一个只有一个人、一个地方、几个量和几条规律的微型世界 ──────────────


def _world_file(tmp_path, *, stocks=(), rules=(), name="w.cyberworld") -> str:
    """手写 `author` 记录 —— 新测试的正路(见 `_worldfile.write_seed_file` 的告诫)。"""
    lines = [{"kind": "manifest", "version": 3, "world_id": "dice"}]
    lines.append({"kind": "author", "type": "agent", "body": {
        "id": "阿岚", "name": "阿岚", "location": "cafe", "personality": "安静"}})
    lines.append({"kind": "author", "type": "location", "body": {
        "id": "cafe", "name": "咖啡馆", "description": "临海"}})
    for stock in stocks:
        lines.append({"kind": "author", "type": "stock", "body": stock})
    for rule in rules:
        lines.append({"kind": "author", "type": "rule", "body": rule})
    path = tmp_path / name
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )
    return str(path)


RIVER = {"owner": "world", "values": {"江水位": 4.0}}
# 涨到 tick 10 为止,然后落回去 —— 一条规律里就能演完一个汛期,不必外部去写量
# (emit 的跨越是**这一次求值前后**的对照,外面改量改不出边沿来)。
TIDE = {
    "id": "汛期", "every": {"ticks": 1}, "for_each": {"owner": "world"},
    "set": {"江水位": "江水位 + (0.05 if now < 10 else -0.05) * dt"},
}
FLOOD = {"when": "江水位 >= 4.2", "type": "江水漫堤"}


def _events(world, event_type="江水漫堤"):
    return [e for e in world.history()["events"] if e["type"] == event_type]


# ── 一、emit 说得出"这件事有多重要、她记住的是哪句话" ────────────────────────


def test_不写_importance_的门槛事件_行为逐位不变(tmp_path, open_world):
    """**声明本身就是开关**:没写 importance 的世界只进日志,一个字都不多。

    (这一条在实现之前就是绿的 —— 它守的正是"新特性不许改老世界的行为"。)
    """
    path = _world_file(tmp_path, stocks=[RIVER], rules=[{**TIDE, "emit": [FLOOD]}])
    world = open_world(world_file=path)
    world.tick(20)

    fired = _events(world)
    assert len(fired) == 1, f"门槛事件发了 {len(fired)} 次"
    payload = fired[0]["payload"]
    assert payload["owner"] == "world" and payload["rule"] == "汛期"
    assert "importance" not in payload, "没声明 importance,却凭空多了一个"
    assert "text" not in payload, "没声明 importance,却凭空多了一句话"


def test_声明了_importance_的门槛事件_带着轻重和那句话(tmp_path, open_world):
    """江水漫堤是这个世界的高潮 —— 她该记得住,而且记得住的是**那句话**。"""
    emit = {**FLOOD, "importance": 0.8, "text": "江水漫过了码头第二级台阶"}
    path = _world_file(tmp_path, stocks=[RIVER], rules=[{**TIDE, "emit": [emit]}])
    world = open_world(world_file=path)
    world.tick(20)

    fired = _events(world)
    assert len(fired) == 1
    payload = fired[0]["payload"]
    assert payload["importance"] == pytest.approx(0.8)
    assert payload["text"] == "江水漫过了码头第二级台阶"
    # `who` / `loc` 仍由消费端从 owner 反查 —— 量这一层不认识地点。
    assert fired[0]["who"] is None


def test_有_importance_没_text_回落成事件类型(tmp_path, open_world):
    """她总得记住点什么。回落成 type 至少是一句真话,空着不是。"""
    emit = {**FLOOD, "importance": 0.5}
    path = _world_file(tmp_path, stocks=[RIVER], rules=[{**TIDE, "emit": [emit]}])
    world = open_world(world_file=path)
    world.tick(20)

    payload = _events(world)[0]["payload"]
    assert payload["importance"] == pytest.approx(0.5)
    assert payload["text"] == "江水漫堤"


def test_importance_写_0_等于没声明(tmp_path, open_world):
    """0 是作者明说的"这件事不值得记" —— 那就一个字都别往下发。"""
    emit = {**FLOOD, "importance": 0, "text": "无关紧要"}
    path = _world_file(tmp_path, stocks=[RIVER], rules=[{**TIDE, "emit": [emit]}])
    world = open_world(world_file=path)
    world.tick(20)

    payload = _events(world)[0]["payload"]
    assert "importance" not in payload and "text" not in payload


def test_坏的_importance_一次列全(tmp_path):
    """坏声明**加载时**当场报错,而且一次报完 —— 不是修一个冒一个。"""
    with pytest.raises(RuleError) as caught:
        parse_rules([{
            "id": "汛期", "for_each": {"owner": "world"}, "set": {"江水位": "江水位"},
            "emit": [
                {**FLOOD, "importance": "很重要"},
                {**FLOOD, "type": "b", "importance": 1.5},
                {**FLOOD, "type": "c", "importance": -0.1},
                {**FLOOD, "type": "d", "importance": 0.5, "text": 42},
            ],
        }])
    message = "\n".join(caught.value.errors)
    assert len(caught.value.errors) >= 4, caught.value.errors
    assert "importance" in message and "text" in message


def test_写了_text_却没写_importance_开不了机(tmp_path):
    """只写 text 的话,那句话一个人都读不到 —— 静默无效正是这个仓库最怕的坏法。"""
    with pytest.raises(RuleError) as caught:
        parse_rules([{
            "id": "汛期", "for_each": {"owner": "world"}, "set": {"江水位": "江水位"},
            "emit": [{**FLOOD, "text": "江水漫过了码头第二级台阶"}],
        }])
    assert "importance" in "\n".join(caught.value.errors)


# ── 二、双边沿:`on: rise | fall | both` ─────────────────────────────────────


def test_不写_on_的世界只在上升沿发_逐位不变(tmp_path, open_world):
    """缺省 `rise` = 现状。水涨上去发一次,落回来一个字都不说。"""
    path = _world_file(tmp_path, stocks=[RIVER], rules=[{**TIDE, "emit": [FLOOD]}])
    world = open_world(world_file=path)
    world.tick(20)

    assert world.stock("world", "江水位") < 4.2, "前提:水得真的落回去了"
    assert len(_events(world)) == 1, "落回去也发了 —— 缺省不再是 rise"


def test_on_both_涨上去和落回去各发一次(tmp_path, open_world):
    """汛期年复一年:一生只有一次机会的语义写不出"水退了"。"""
    emit = {**FLOOD, "on": "both"}
    path = _world_file(tmp_path, stocks=[RIVER], rules=[{**TIDE, "emit": [emit]}])
    world = open_world(world_file=path)
    world.tick(20)

    edges = [e["payload"]["edge"] for e in _events(world)]
    assert edges == ["rise", "fall"], f"两个方向没都发:{edges}"


def test_on_fall_只在落回去时发(tmp_path, open_world):
    emit = {**FLOOD, "type": "水退了", "on": "fall"}
    path = _world_file(tmp_path, stocks=[RIVER], rules=[{**TIDE, "emit": [emit]}])
    world = open_world(world_file=path)
    world.tick(20)

    fired = _events(world, "水退了")
    assert len(fired) == 1 and fired[0]["payload"]["edge"] == "fall"


def test_edge_说得出这次是哪个方向(tmp_path, open_world):
    path = _world_file(tmp_path, stocks=[RIVER], rules=[{**TIDE, "emit": [FLOOD]}])
    world = open_world(world_file=path)
    world.tick(20)
    assert _events(world)[0]["payload"]["edge"] == "rise"


def test_坏的_on_当场开不了机():
    with pytest.raises(RuleError) as caught:
        parse_rules([{
            "id": "汛期", "for_each": {"owner": "world"}, "set": {"江水位": "江水位"},
            "emit": [{**FLOOD, "on": "沿"}],
        }])
    message = "\n".join(caught.value.errors)
    assert "rise" in message and "fall" in message and "both" in message


def test_双边沿不记状态_重开世界不会补发也不会漏发(tmp_path, open_world, fresh_redis):
    """**保持无状态、重启安全。**

    边沿是"这一次求值前后"的对照,两个值都在双缓冲的快照里 —— 不该有任何持久化的
    边沿记忆。有的话,重开一个"门槛早就跨过去了"的世界要么补发一次(日志里多一件
    从没发生过的事),要么因为那份记忆丢了而永远沉默。
    """
    emit = {**FLOOD, "on": "both"}
    path = _world_file(tmp_path, stocks=[RIVER], rules=[{**TIDE, "emit": [emit]}])
    world = open_world("汛", redis=fresh_redis, world_file=path)
    world.tick(8)                       # 只涨,还没落
    assert [e["payload"]["edge"] for e in _events(world)] == ["rise"]
    world.close()

    reopened = open_world("汛", redis=fresh_redis)
    assert [e["payload"]["edge"] for e in _events(reopened)] == ["rise"], (
        "重开的世界把一件早就发生过的事又发了一遍(门槛此刻仍然满足,没有跨越)"
    )
    reopened.tick(12)                   # 推到落回去
    assert [e["payload"]["edge"] for e in _events(reopened)] == ["rise", "fall"], (
        "重开之后那一次下降沿没发出来 —— 边沿被记进了某个丢得掉的状态里"
    )


# ── 三、可重放的意外:`rand()` ───────────────────────────────────────────────


RAIN = {
    "id": "阵雨", "every": {"ticks": 1}, "for_each": {"owner": "world"},
    "set": {"雨势": "1 if rand() < 0.5 else 0"},
}


def test_没有_rand_的世界跑两遍一模一样(tmp_path, open_world):
    """**replay 纪律没有松**:不写 rand() 的世界照旧是逐位确定的。"""
    runs = []
    for index in (0, 1):
        path = _world_file(tmp_path, stocks=[RIVER], rules=[TIDE], name=f"w{index}.cyberworld")
        world = open_world(world_file=path)
        world.tick(20)
        runs.append(world.stock("world", "江水位"))
    assert runs[0] == runs[1]


def test_骰子是这个世界这一刻的_不是随机数():
    """同一个世界、同一 tick、同一条规律、同一个 owner —— 永远同一个值。"""
    a = world_dice("w", "阵雨", "world", 7)
    assert a == world_dice("w", "阵雨", "world", 7), "同一刻摇出了两个数 —— 不可重放"
    assert 0.0 <= a < 1.0


def test_换了世界_换了规律_换了人_换了时刻都是另一个数():
    base = world_dice("w", "阵雨", "world", 7)
    others = {
        world_dice("w2", "阵雨", "world", 7),      # 换世界
        world_dice("w", "别的规律", "world", 7),    # 换规律
        world_dice("w", "阵雨", "agent:阿岚", 7),   # 换人
        world_dice("w", "阵雨", "world", 8),       # 换时刻
    }
    assert base not in others, "四个自由度里有一个没进骰子"
    assert len(others) == 4, "不同的坐标摇出了同一个数"


def test_一万次不落在同一半边():
    """确定性不等于"其实是个常数" —— 值域要真的铺开。"""
    draws = [world_dice("w", "r", f"tree:{i}", 3) for i in range(2000)]
    assert 0.45 < sum(1 for d in draws if d < 0.5) / len(draws) < 0.55
    assert min(draws) < 0.01 and max(draws) > 0.99


def test_rand_不收参数():
    """同一条规律、同一个 owner、同一 tick 只投一次 —— 收参数会让人以为有第二次。"""
    with pytest.raises(ExpressionError):
        compile_expression("rand(2)")


def test_光秃秃的_rand_当场说清楚要写括号():
    """从前它只是个自由变量:要么运行期报"读了不存在的量",要么撞上一个真叫这个
    名字的量。名字被骰子占了,那就在加载期说。"""
    with pytest.raises(ExpressionError) as caught:
        compile_expression("雨势 + rand")
    assert "rand()" in str(caught.value)


def test_没有骰子的地方_rand_当场喊出来():
    """能力/条件那一层还没有骰子。静默给个 0 才是这个仓库最怕的坏法。"""
    with pytest.raises(ExpressionError) as caught:
        compile_expression("rand()").evaluate({})
    assert "骰子" in str(caught.value)


def _rain_sequence(tmp_path, open_world, world_id: str, ticks: int = 30) -> list[float]:
    """把一个叫 `world_id` 的世界从头跑一遍,记下每 tick 的雨势。**各跑各的库。**"""
    import fakeredis

    path = _world_file(tmp_path, stocks=[{"owner": "world", "values": {"雨势": 0}}],
                       rules=[RAIN], name=f"rain-{world_id}.cyberworld")
    world = open_world(world_id, redis=fakeredis.FakeStrictRedis(decode_responses=True),
                       world_file=path)
    seen = []
    for _ in range(ticks):
        world.tick(1)
        seen.append(world.stock("world", "雨势"))
    return seen


def test_世界里真的下起了阵雨_而且重放一模一样(tmp_path, open_world):
    """走 `World.open` + `tick()` 的真路 —— 雨势不再是常数,而重放逐位一致。"""
    first = _rain_sequence(tmp_path, open_world, "雨季")
    again = _rain_sequence(tmp_path, open_world, "雨季")   # 同一个世界,从头再来一遍

    assert set(first) == {0.0, 1.0}, f"雨势没随机起来:{first}"
    assert first == again, "同一个世界跑两遍下了不一样的雨 —— 不可重放"


def test_两个世界下的不是同一场雨(tmp_path, open_world):
    """`world_id` 真的进了骰子 —— 端到端那一条(`evaluate_due` 那一层另有一条)。

    没接上的话所有世界共用一副骰子:两个世界同一天下同一场雨,而且**看上去完全
    正常** —— 除非有人把两个世界并排放着看,否则永远不会有人发现。
    """
    assert (_rain_sequence(tmp_path, open_world, "江南")
            != _rain_sequence(tmp_path, open_world, "塞北"))


def test_两个世界摇的不是同一副骰子(tmp_path):
    """`evaluate_due(world_id=…)` 这条线接上了没有 —— 没接上的话所有世界共用一副。"""

    class _Store:
        def __init__(self):
            self.written: dict[str, dict[str, float]] = {}

        def snapshot_kind(self, kind):
            return {}

        def snapshot_many(self, owners):
            return {"world": {"雨势": (0.0, 0)}}

        def of(self, owner):
            return {}

        def write_round(self, pending, tick):
            self.written = {owner: dict(values) for owner, values in pending.items()}
            return len(pending)

    draws = []
    for world_id in ("甲", "乙"):
        rules = parse_rules([{**RAIN, "set": {"雨势": "rand()"}}])
        store = _Store()
        evaluate_due(store, rules, 5, last_run={}, world_id=world_id)
        draws.append(store.written["world"]["雨势"])
    assert draws[0] != draws[1], "两个世界摇出了同一个数 —— world_id 没进骰子"


# ── 四、加载期 lint:常数步长对"多算一次"不免疫 ──────────────────────────────


DRIFTY = {"id": "雨天数", "every": {"days": 1}, "for_each": {"owner": "world"},
          "set": {"雨天数": "雨天数 + 1"}}


def test_常数步长的自增规律被点名():
    """线上那个世界正因为这个:滚动重启每次多烧一整天的雨(56 天 vs 应为 ~50),
    洪水提前两天半。`雨天数 + 1` 不看流逝 —— 多算一次就是多加一整份,而"多算
    一次"不是它自己说了算的(重启、节流水位丢失、时钟跳变)。
    """
    said = " ".join(drift_warnings(parse_rules([DRIFTY])))
    assert "雨天数" in said and "dt" in said, f"常数步长没被点名:{said!r}"


def test_按流逝折算的规律不点名():
    assert not drift_warnings(parse_rules([{**DRIFTY, "set": {"雨天数": "雨天数 + dt / 288"}}]))


def test_每_tick_都算的规律不点名():
    """`every` 是 1 的话根本没有"多算一次"这回事。"""
    assert not drift_warnings(parse_rules([{**DRIFTY, "every": {"ticks": 1}}]))


# 世界的量读写不同名:**写**光秃秃的 `雨天数`(带前缀写会被 `bad_output_name` 拒),
# **读**必须写成 `world_雨天数`。所以上面那个 `DRIFTY` 虽然点得着名,却是作者几乎
# 不会写的那一半 —— 真正在跑的两个世界(晚潮的雨、灯塔湾的雾)写的都是下面这个形状。
DRIFTY_PREFIXED = {**DRIFTY, "set": {"雨天数": "world_雨天数 + 1"}}


def test_世界的量按它真正的读法也要被点名():
    """这道闸只按名字对得上判,而世界规律读自己要加 `world_` 前缀 —— 于是它对
    **整类**世界规律是瞎的,**而它被写出来针对的那次事故恰恰就是一条世界规律**。

    线上那个世界的雨天数写的就是 `world_雨天数 + 1`:多烧了 6 天雨、洪水提前
    两天半,而这道专为它写的 lint 从头到尾一声没吭。灯塔湾同理 —— 10 条漂着的
    规律里只有 7 条被点到名,少的 3 条全是 `owner: world` 那几条。
    """
    said = " ".join(drift_warnings(parse_rules([DRIFTY_PREFIXED])))
    assert "雨天数" in said and "dt" in said, f"世界的量按真正的读法没被点名:{said!r}"


def test_世界的量按流逝折算了就不点名():
    """修法照旧是乘上 `dt / interval` —— 认得出病也要认得出药,不然作者改完
    警告还在,下一次他就不看了。"""
    assert not drift_warnings(parse_rules([
        {**DRIFTY_PREFIXED, "set": {"雨天数": "world_雨天数 + dt / 288"}},
    ]))


def test_别人家的世界量不算自增():
    """前缀这条只对 `owner: world` 的规律成立。一条按角色跑的规律写自己的 `雨天数`、
    读世界的 `world_雨天数`,读写的是**两个**量 —— 那不是自增,点它就是误报,
    而误报够多次的警告等于没有警告。"""
    assert not drift_warnings(parse_rules([{
        "id": "淋雨", "every": {"days": 1}, "for_each": {"kind": "agent"},
        "set": {"雨天数": "world_雨天数 + 1"},
    }]))


def test_解析本身不打这条警告(caplog):
    """**一次开机解析三遍规律**(播种 / 装载 / 本体预检)。在 `parse_rules` 里打
    日志就是同一句话说三遍 —— 而说三遍的警告和没说过一样,人会开始略过它。
    点名归装载那一处(每次开机恰好一次)与 `validate`(作者会看的地方)。
    """
    with caplog.at_level(logging.WARNING, logger="anima_world.rules"):
        parse_rules([DRIFTY])
    assert not caplog.records, "解析这一层不该打警告,否则一次开机要说三遍"


def test_装载默认闭嘴要显式开口(caplog):
    """开口是 `warn=True`,而全仓库只有装载那一处写了它。

    默认关的理由是**将来新加的调用点默认闭嘴** —— 反过来(默认开、要静音的人
    自己去关)正是下面那条真路 bug 的形状:预检顺手复用了这个函数,谁都没注意到。
    """
    from anima_world.__main__ import _load_world_rules

    class _Store:
        def definitions(self):
            return [DRIFTY]

    with caplog.at_level(logging.WARNING, logger="anima_world.__main__"):
        _load_world_rules(_Store())
    assert not [r for r in caplog.records if "dt" in r.getMessage()], "默认不该开口"

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="anima_world.__main__"):
        _load_world_rules(_Store(), warn=True)
    said = [r.getMessage() for r in caplog.records if "dt" in r.getMessage()]
    assert len(said) == 1, f"开了口该说一次,实际 {len(said)} 次:{said!r}"


def test_一次真开机恰好点名一次(tmp_path, caplog):
    """**这条才是真路**:上面那条只证明了"这个函数说一次",而线上重启说了两遍。

    走的是容器里那条路(`build_serve_scheduler`,一次完整开机)。原因是
    `_precheck_ontology` 也调 `_load_world_rules` —— 同一条警告因此说两遍,而
    说两遍的警告和没说过一样。**只钉函数级的那条测试等于把这个门开着。**

    ⚠️ **必须验第二次开机**:预检读的是**库里那份**规律,首启时那张表还空着,
    于是它一个字都不说,bug 还在测试也是绿的。线上撞见它的场合正是重启。
    """
    import fakeredis

    from anima_world.__main__ import build_serve_scheduler

    from _worldfile import write_seed_file

    # ⚠️ **必须声明 `kinds`**:重复那一遍来自 `_precheck_ontology`,而它只在作者
    # 写了 `kinds` 时才跑。不带种类的世界只说一遍 —— 拿那样的夹具去钉,这条测试
    # 会在 bug 还在的时候就绿。线上那个世界是有种类的。
    path = write_seed_file(tmp_path / "drifty.cyberworld", {
        "agents": [{"id": "a", "name": "阿岚", "location": "cafe", "personality": "安静"}],
        "locations": [{"id": "cafe", "name": "咖啡馆", "description": "临海的小店"}],
        "stocks": [{"owner": "world", "values": {"雨天数": 0.0}}],
        "rules": [DRIFTY],
        "kinds": [{"id": "tree", "name": "树", "quantities": {"树高": {"default": 1.0}}}],
        "entities": [{"id": "tree:oak", "name": "橡树", "location": "cafe"}],
    })

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    build_serve_scheduler("w", client, world_file=path, force_mock_llm=True).stop()

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="anima_world.__main__"):
        scheduler = build_serve_scheduler(
            "w", client, world_file=path, force_mock_llm=True,
        )
    try:
        said = [r.getMessage() for r in caplog.records
                if "dt" in r.getMessage() and DRIFTY["id"] in r.getMessage()]
        assert len(said) == 1, f"一次开机该恰好说一次,实际 {len(said)} 次"
    finally:
        scheduler.stop()


def test_validate_也报得出这条():
    """开机那条警告落在服务器日志里,而作者手上只有 `validate world`。"""
    from anima_world.__main__ import _authored_drift_warnings

    said = " ".join(_authored_drift_warnings({"rules": [DRIFTY]}))
    assert "雨天数" in said and "dt" in said, f"validate 没报:{said!r}"


def test_橱窗自己不该带着这条毛病():
    """橱窗是新用户看到的第一眼,它示范的写法会被照抄。

    (这条真的逮到过:`rainy_days` 原本就是 `雨天数 + 1`,和线上那场雨同一个原型。)
    """
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "anima_world" / "demo.cyberworld"
    entries = [
        json.loads(line)["body"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("type") == "rule"
    ]
    assert not drift_warnings(parse_rules(entries)), "橱窗种子自己带着常数步长的毛病"


def test_不是自增式的规律不点名(caplog):
    """`季节 = floor(day/90) % 4` 不读它自己 —— 算几次都是同一个答案,没什么可漂的。

    这条是这道 lint 的误报闸:按"没有 dt"一刀切的话,每一条纯赋值的规律都会被冤枉,
    而被冤枉够多次的警告等于没有警告。
    """
    with caplog.at_level(logging.WARNING, logger="anima_world.rules"):
        parse_rules([{**DRIFTY, "set": {"季节": "floor(day / 90) % 4"}}])
    assert not [r for r in caplog.records if "dt" in r.getMessage()]


def test_lint_只是警告_世界照样开得起来(tmp_path, open_world):
    """点名不是拒绝:一个常数步长的世界仍然要开得了机(内置橱窗就有一条)。"""
    path = _world_file(tmp_path, stocks=[{"owner": "world", "values": {"雨天数": 0}}],
                       rules=[DRIFTY])
    world = open_world(world_file=path)
    world.tick(300)
    assert world.stock("world", "雨天数") >= 1
