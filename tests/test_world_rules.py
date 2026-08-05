"""世界的规律作为数据(world-rules):树会长、矿会枯、修炼会涨功力。

这是引擎"把硬编码变成数据"那条线的最后一段:needs 的衰减曲线、economy 的价格漂移
都写死在 Python 里,而"树怎么长"因世界而异,不该由引擎替所有世界决定。

盯的是五条设计承诺,每一条错了都是很难查的那类错:

- **`dt` 让求值频率无关**:一万棵树不必每 tick 算 —— 算得稀不影响结果
- **边沿触发**:长满了的树不会每 12 tick 喊一次"我长成了"
- **双缓冲**:规律之间与顺序无关
- **加载时严格**:坏公式不许流到运行期
- **运行期降级**:一条算不出来的规律不掀翻整个世界
"""
from __future__ import annotations

from _worldfile import open_world_at

import json

import pytest

from anima_world.api import World
from anima_world.rules import RuleError, parse_rules


def _seed(tmp_path, *, stocks=None, rules=None, name="seed.json") -> str:
    from importlib import resources

    seed = json.loads(
        (resources.files("anima_world") / "world_seed.json").read_text(encoding="utf-8")
    )
    if stocks is not None:
        seed["stocks"] = stocks
    if rules is not None:
        seed["rules"] = rules
    if stocks is not None or rules is not None:
        # 换掉量与规律就等于换了一个世界,橱窗的本体声明(那棵中文名的树)对它不再
        # 成立。剥掉它 = 这些微型世界**不声明本体**,于是走"警告不拒绝"那条老路 ——
        # 这一整套测试盯的是规律引擎本身,本体闸在 test_ontology 那边。
        seed.pop("kinds", None)
        seed.pop("entities", None)
    path = tmp_path / name
    path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return str(path)


TREE = {"owner": "tree:oak_01", "values": {"size": 0.5, "growth_rate": 0.02, "max_size": 10}}
GROWTH = {
    "id": "tree_growth",
    "every": {"ticks": 12},
    "for_each": {"kind": "tree"},
    "set": {"size": "min(size + growth_rate * dt, max_size)"},
}


# ── 基本:树真的长 ──────────────────────────────────────────────────────────


def test_a_tree_grows_on_its_own(tmp_path):
    path = _seed(tmp_path, stocks=[TREE], rules=[GROWTH])
    with open_world_at(str(tmp_path / "w.db"), seed_path=path, force_mock_llm=True) as world:
        assert world.stock("tree:oak_01", "size") == pytest.approx(0.5)
        world.tick(120)
        assert world.stock("tree:oak_01", "size") > 0.5, "时钟走了,树却没长"


def test_growth_stops_at_the_cap(tmp_path):
    path = _seed(tmp_path, stocks=[TREE], rules=[GROWTH])
    with open_world_at(str(tmp_path / "w.db"), seed_path=path, force_mock_llm=True) as world:
        world.tick(2000)
        assert world.stock("tree:oak_01", "size") == pytest.approx(10.0)


def test_evaluation_frequency_causes_no_drift(tmp_path):
    """**`dt` 的全部意义所在:算得稀不会让结果偏掉。**

    准确的说法是"没有累积漂移",不是"任何时刻都一样" —— 节流意味着任一瞬间可能有
    最多一个 `every` 的变化还没结算(滞后)。所以这里在**三种频率共同的求值点**上
    比:tick 577 对 1 / 12 / 144 都正好是求值点(577-1 = 576 = 12×48 = 144×4)。

    这条错了整套东西就没法 scale:要么每 tick 硬算(贵),要么算得稀但结果悄悄偏掉。
    """
    tall = {"owner": "tree:oak_01",
            "values": {"size": 0.5, "growth_rate": 0.02, "max_size": 1000}}  # 不让封顶掺进来
    results = []
    for index, interval in enumerate((1, 12, 144)):
        rule = {**GROWTH, "every": {"ticks": interval}}
        path = _seed(tmp_path, stocks=[tall], rules=[rule], name=f"s{index}.json")
        with open_world_at(str(tmp_path / f"w{index}.db"), seed_path=path,
                        force_mock_llm=True) as world:
            world.tick(577)
            results.append(round(world.stock("tree:oak_01", "size"), 6))
    assert len(set(results)) == 1, f"求值频率让结果漂了:{results}"


def test_throttling_costs_lag_but_never_accuracy(tmp_path):
    """节流的代价是**滞后**,不是错。算得稀的那个只是还没结算,不是少长了。"""
    tall = {"owner": "tree:oak_01",
            "values": {"size": 0.5, "growth_rate": 0.02, "max_size": 1000}}
    path = _seed(tmp_path, stocks=[tall], rules=[{**GROWTH, "every": {"ticks": 144}}])
    with open_world_at(str(tmp_path / "w.db"), seed_path=path, force_mock_llm=True) as world:
        world.tick(500)                       # 落在两次求值之间
        lagging = world.stock("tree:oak_01", "size")
        world.tick(77)                        # 推到下一个求值点(577)
        caught_up = world.stock("tree:oak_01", "size")
        assert caught_up > lagging, "滞后的那部分没有被补回来"
        assert caught_up == pytest.approx(0.5 + 0.02 * 577), "补回来的量不对"


def test_a_freshly_planted_tree_does_not_jump_to_full_size(tmp_path):
    """`dt` 从量**自己**的 updated_tick 算,不是从世界年龄算。

    否则在一个跑了半年的世界里种一棵树,它会在下一次求值时一次性长成参天大树。
    """
    path = _seed(tmp_path, stocks=[], rules=[GROWTH])
    with open_world_at(str(tmp_path / "w.db"), seed_path=path, force_mock_llm=True) as world:
        world.tick(2000)                                    # 世界先跑很久
        world.set_stocks("tree:sapling", {"size": 0.5, "growth_rate": 0.02, "max_size": 10})
        world.tick(12)
        assert world.stock("tree:sapling", "size") < 1.0, "新种的树按世界年龄暴长了"


# ── 边沿触发 ────────────────────────────────────────────────────────────────


def test_a_threshold_event_fires_once_not_every_evaluation(tmp_path):
    """没有边沿触发,一棵长满的树会每 12 tick 喊一次"我长成了",直到世界末日 ——
    而那正是把事件日志淹掉的方式(needs 有过 19.7 倍事件量的教训)。"""
    rule = {**GROWTH, "emit": [{"when": "size >= max_size", "type": "tree_matured"}]}
    path = _seed(tmp_path, stocks=[TREE], rules=[rule])
    with open_world_at(str(tmp_path / "w.db"), seed_path=path, force_mock_llm=True) as world:
        world.tick(3000)   # 早就长满了,而且之后又过了很久
        matured = [e for e in world.history()["events"] if e["type"] == "tree_matured"]
        assert len(matured) == 1, f"门槛事件发了 {len(matured)} 次"
        assert matured[0]["payload"]["owner"] == "tree:oak_01"


def test_continuous_change_writes_no_events_at_all(tmp_path):
    """连续变化不发事件 —— 这是这套东西能 scale 的前提。"""
    path = _seed(tmp_path, stocks=[TREE], rules=[GROWTH])
    with open_world_at(str(tmp_path / "w.db"), seed_path=path, force_mock_llm=True) as world:
        before = len(world.history()["events"])
        world.tick(600)
        after = world.history()["events"]
        assert world.stock("tree:oak_01", "size") > 1.0, "前提:树得真的长了"
        rule_events = [e for e in after[before:] if e["payload"].get("rule")]
        assert rule_events == [], "连续变化逐条发了事件"


# ── 条件 ────────────────────────────────────────────────────────────────────


def test_a_condition_can_stop_the_world_from_evolving(tmp_path):
    """`when` 不满足就整条不算 —— 冬天树不长。"""
    rule = {**GROWTH, "when": ["world_season != 3"]}
    path = _seed(tmp_path, stocks=[TREE, {"owner": "world", "values": {"season": 3}}],
                 rules=[rule])
    with open_world_at(str(tmp_path / "w.db"), seed_path=path, force_mock_llm=True) as world:
        world.tick(600)
        assert world.stock("tree:oak_01", "size") == pytest.approx(0.5), "冬天不该长"

        world.set_stock("world", "season", 1)   # 开春
        world.tick(600)
        assert world.stock("tree:oak_01", "size") > 0.5, "开春了还不长"


# ── 行为驱动(修炼) ────────────────────────────────────────────────────────


def test_the_same_hour_of_work_pays_differently_by_skill(tmp_path):
    """同样修炼一小时,功法高的涨得多 —— 速率来自**行为者自己的量**。"""
    rule = {
        "id": "cultivate", "every": {"ticks": 1}, "for_each": {"action": "work"},
        "set": {"power": "power + dt * level"},
    }
    path = _seed(tmp_path, rules=[rule], stocks=[
        {"owner": "agent:夏", "values": {"power": 0, "level": 1}},
        {"owner": "agent:遥", "values": {"power": 0, "level": 3}},
    ])
    with open_world_at(str(tmp_path / "w.db"), seed_path=path, force_mock_llm=True) as world:
        world.tick(600)
        low, high = world.stock("agent:夏", "power"), world.stock("agent:遥", "power")
        assert low > 0 and high > 0, "没人修炼成功(前提:得有人在 work)"
        assert high > low * 2, f"功法等级没起作用:{low} vs {high}"


# ── 双缓冲与顺序无关 ────────────────────────────────────────────────────────


def test_rules_do_not_see_each_others_writes_within_a_round(tmp_path):
    """同一轮里读到的都是这一轮**开始前**的值。

    否则"A 先算还是 B 先算"就成了隐藏的语义,而那个顺序是字典序决定的 ——
    改个 id 就悄悄改了世界的物理。
    """
    rules = [
        {"id": "a_writes", "for_each": {"owner": "box"}, "set": {"x": "x + 1"}},
        # b 读 x:如果它看见 a 写的新值,结果会是 x+1;双缓冲下它只该看见旧的 x
        {"id": "b_reads", "for_each": {"owner": "box"}, "set": {"y": "x"}},
    ]
    path = _seed(tmp_path, stocks=[{"owner": "box", "values": {"x": 0, "y": -1}}], rules=rules)
    with open_world_at(str(tmp_path / "w.db"), seed_path=path, force_mock_llm=True) as world:
        world.tick(1)
        assert world.stock("box", "x") == pytest.approx(1.0)
        assert world.stock("box", "y") == pytest.approx(0.0), (
            "b 读到了 a 这一轮写的值 —— 双缓冲没生效,规律之间变成了顺序相关"
        )


# ── 加载时严格 ──────────────────────────────────────────────────────────────


def test_a_broken_formula_refuses_to_boot(tmp_path):
    """坏公式不许流到运行期 —— 和节拍脚本同一条硬要求。"""
    bad = {**GROWTH, "set": {"size": "size + + *"}}
    path = _seed(tmp_path, stocks=[TREE], rules=[bad])
    with pytest.raises(RuleError):
        open_world_at(str(tmp_path / "w.db"), seed_path=path, force_mock_llm=True)


def test_dangerous_expressions_are_refused(tmp_path):
    """设计者写的字符串会被求值 —— 这是这个包里唯一有安全含义的地方。"""
    for source in ("__import__('os').system('ls')", "open('/etc/passwd').read()",
                   "self.__class__", "[x for x in range(9)]"):
        with pytest.raises(RuleError):
            parse_rules([{**GROWTH, "set": {"size": source}}])


def test_every_bad_rule_is_reported_at_once(tmp_path):
    """一次报完,不是修一个冒一个。"""
    with pytest.raises(RuleError) as raised:
        parse_rules([
            {"id": "no_set", "for_each": {"kind": "tree"}},
            {"id": "bad_selector", "for_each": {"nope": "x"}, "set": {"a": "1"}},
            {"id": "bad_every", "every": {"weeks": 1}, "for_each": {"kind": "t"}, "set": {"a": "1"}},
        ])
    assert len(raised.value.errors) >= 3, raised.value.errors


def test_duplicate_rule_ids_are_refused():
    with pytest.raises(RuleError):
        parse_rules([GROWTH, dict(GROWTH)])


# ── 运行期降级 ──────────────────────────────────────────────────────────────


def test_a_rule_that_reads_a_missing_stock_skips_itself_and_says_so(tmp_path):
    """读了一个不存在的量:跳过这条,世界照跑,但**留下痕迹**。

    量可以在世界跑起来之后才被创建,所以这不能是加载期错误;但静默跳过会让
    设计者以为规律在跑 —— 那正是最难查的那类错。
    """
    rule = {**GROWTH, "set": {"size": "size + 未定义的量"}}
    path = _seed(tmp_path, stocks=[TREE], rules=[rule])
    with open_world_at(str(tmp_path / "w.db"), seed_path=path, force_mock_llm=True) as world:
        world.tick(60)
        assert world.world_time().minute_of_day >= 0, "世界被一条坏规律停掉了"
        stats = world.rule_stats()
        assert stats["skipped"] > 0
        assert "未定义的量" in str(stats["last_error"])


def test_dividing_by_zero_is_survivable(tmp_path):
    rule = {**GROWTH, "set": {"size": "size / (max_size - max_size)"}}
    path = _seed(tmp_path, stocks=[TREE], rules=[rule])
    with open_world_at(str(tmp_path / "w.db"), seed_path=path, force_mock_llm=True) as world:
        world.tick(60)
        assert world.rule_stats()["skipped"] > 0
        assert world.stock("tree:oak_01", "size") == pytest.approx(0.5), "坏规律不该写坏值"


# ── 持久化 ──────────────────────────────────────────────────────────────────


def test_stocks_and_rules_survive_a_reopen(tmp_path):
    db = str(tmp_path / "w.db")
    path = _seed(tmp_path, stocks=[TREE], rules=[GROWTH])
    with open_world_at(db, seed_path=path, force_mock_llm=True) as world:
        world.tick(600)
        grown = world.stock("tree:oak_01", "size")
        assert grown > 0.5

    with open_world_at(db, force_mock_llm=True) as reopened:
        assert reopened.stock("tree:oak_01", "size") == pytest.approx(grown)
        assert [r["id"] for r in reopened.rules()] == ["tree_growth"], "规律没跟着 db 走"


def test_a_world_without_rules_is_unaffected(tmp_path, bare_seed):
    """没有规律的世界,这一层是一次判空 —— 常开也不花钱。

    素配种子:内置橱窗**自己带了规律**(门口那棵树在长),拿它来验"没有规律会怎样"
    是自相矛盾(见 conftest)。
    """
    with open_world_at(str(tmp_path / "w.db"), seed_path=bare_seed,
                    force_mock_llm=True) as world:
        world.tick(50)
        assert world.rules() == []
        assert world.rule_stats()["evaluated"] == 0


def test_a_rule_reading_an_unreachable_name_is_named_at_boot(tmp_path, caplog):
    """开机就点名"这条规律读了它够不着的量"。

    由来是一次真实事故:内置橱窗把 `world_季节` 写成了 `季节`,于是那条规律每次求值
    都被静默跳过 —— 树永远不长,而世界看起来一切正常。运行期的 `rule_stats()` 抓到了
    它,但那要等有人去看;开机点名让它在第一秒现形。

    要害是**按选择器算够不着**:world 自己有个 `季节`,不代表作用在树上的规律能裸着
    读到它(第一版 helper 把所有 owner 的量名混成一个集合,于是对这个 bug 视而不见)。
    """
    stocks = [{"owner": "world", "values": {"季节": 1}},
              {"owner": "tree:t1", "values": {"高": 1, "速": 0.1}}]
    rules = [
        {"id": "bad", "for_each": {"kind": "tree"}, "when": ["季节 != 4"],
         "set": {"高": "高 + 速 * dt"}},
        {"id": "good", "for_each": {"kind": "tree"}, "when": ["world_季节 != 4"],
         "set": {"高": "高 + 速 * dt"}},
    ]
    path = _seed(tmp_path, stocks=stocks, rules=rules)
    with caplog.at_level("WARNING"):
        open_world_at(str(tmp_path / "w.db"), seed_path=path, force_mock_llm=True).close()

    warned = [r.getMessage() for r in caplog.records if "够不着" in r.getMessage()]
    assert len(warned) == 1, f"该点名一条(bad),实际 {len(warned)} 条:{warned}"
    assert "bad" in warned[0] and "季节" in warned[0]
    assert "good" not in warned[0], "写对了的那条被误报了"


def test_the_engine_scales_to_many_entities(tmp_path):
    """"一万棵树也扛得住"是这一层写在文档里的承诺 —— 这条测的就是那句话。

    由来:第一版实现**逐个 owner 查一次快照、逐个 owner 一次 commit**,2000 棵树
    就涨到 72ms/tick(快进一年要两小时),而文档里已经写着"能 scale"。改成按类批量
    快照 + 整轮一次 commit 之后是 1.1ms/tick,66 倍。

    阈值取得很松(比实测宽一个数量级),因为这条要在别人的机器和 CI 上也稳 ——
    它防的是**回归到逐个 commit** 那种量级的退化,不是守着某个具体毫秒数。
    """
    import time

    rule = {"id": "g", "every": {"ticks": 12}, "for_each": {"kind": "tree"},
            "set": {"size": "min(size + rate * dt, cap)"}}
    path = _seed(tmp_path, stocks=[], rules=[rule])
    with open_world_at(str(tmp_path / "w.db"), seed_path=path, force_mock_llm=True) as world:
        for index in range(1000):
            world.set_stocks(f"tree:t{index}", {"size": 0.5, "rate": 0.01, "cap": 20})

        started = time.monotonic()
        world.tick(120)          # 10 次求值 × 1000 棵
        elapsed = time.monotonic() - started

        assert world.stock("tree:t0", "size") > 0.5, "前提:树得真的长了"
        assert world.rule_stats()["skipped"] == 0
        assert elapsed < 3.0, (
            f"1000 棵树 120 tick 花了 {elapsed:.1f}s —— 大概退回到"
            "逐个 owner 查询/提交了(那一版是这个数的几十倍)"
        )


def test_batching_did_not_change_the_numbers(tmp_path):
    """批量化是纯优化 —— 算出来的值必须和逐个算的一模一样。

    性能改动最危险的地方就是顺手改了语义,而那种错在压测里看不见。
    """
    rule = {"id": "g", "every": {"ticks": 12}, "for_each": {"kind": "tree"},
            "set": {"size": "min(size + rate * dt, cap)"}}
    path = _seed(tmp_path, stocks=[], rules=[rule])
    with open_world_at(str(tmp_path / "w.db"), seed_path=path, force_mock_llm=True) as world:
        for index in range(5):
            world.set_stocks(f"tree:t{index}", {"size": 0.5, "rate": 0.01, "cap": 20})
        world.tick(120)
        sizes = {world.stock(f"tree:t{i}", "size") for i in range(5)}
        assert len(sizes) == 1, f"同样的树算出了不同的值:{sizes}"
        # 0.5 + 0.01 × 已结算的 tick 数(最后一次求值在 tick 109,dt 累计 109)
        assert sizes.pop() == pytest.approx(0.5 + 0.01 * 109)


# ---- 写不到的地方,开机就拒 -------------------------------------------------


def test_writing_a_global_is_refused_at_load(tmp_path):
    """`set` 到 `world_x` 曾经是**静默写错地方**,而仪表报的是成功。

    一条规律只能写它自己那个 owner 的量。而 `"set": {"world_总产量": …}` 此前被
    照单全收,在那个角色名下建了一个叫 `world_总产量` 的量 —— 世界的量一动没动,
    `rule_stats()` 却报 `written: 5, skipped: 0`。专门用来回答"这层跑通了吗"的仪表
    说的是成功,这正是"照跑但给错东西"。
    `world_x` 尤其毒:**读**它是对的,于是作者理所当然假设写也对称。
    """
    with pytest.raises(RuleError) as caught:
        parse_rules([{
            "id": "acc", "for_each": {"owner": "agent:夏"},
            "set": {"world_总产量": "world_总产量 + 1"},
        }])
    message = "\n".join(caught.value.errors)
    assert "写不到全局量" in message
    # 光说"不行"不够,得说清怎么写才对
    assert '"for_each": {"owner": "world"}' in message


def test_writing_another_entity_is_refused_at_load(tmp_path):
    """跨实体的相互作用(挖矿让矿脉减少)v1 表达不了 —— 那就当场说出来。

    不悄悄放行的理由:双缓冲下扇入没有意义。一条作用在一百棵树上的规律,每棵读到的
    全局量都是这一轮开始前的同一个值,"每棵 +1"的结果是 +1 而不是 +100 —— 一个
    看起来对、算出来错的语义,比当场报错坏得多。
    """
    for target in ("mine:north.储量", "agent:夏.体力"):
        with pytest.raises(RuleError) as caught:
            parse_rules([{
                "id": "mining", "for_each": {"owner": "agent:夏"},
                "set": {target: "矿石 - 1"},
            }])
        assert "写不到别的实体身上" in "\n".join(caught.value.errors)


def test_a_rule_writing_its_own_owner_still_loads():
    """闸门不许误伤正常写法 —— 出厂种子的两条规律都是这一类。"""
    rules = parse_rules([
        {"id": "rain", "for_each": {"owner": "world"}, "set": {"雨天数": "雨天数 + 1"}},
        {"id": "grow", "for_each": {"kind": "tree"},
         "when": ["world_季节 != 4"],
         "set": {"树高": "min(树高 + 生长速度 * dt, 最大树高)"}},
    ])
    assert [r.id for r in rules] == ["rain", "grow"]


def test_the_bundled_seed_survives_the_gate():
    """出厂世界必须开得了机 —— 这道闸是加在**加载期**的,误伤等于装上包就打不开。"""
    import json
    from importlib import resources

    seed = json.loads(
        (resources.files("anima_world") / "world_seed.json").read_text(encoding="utf-8")
    )
    assert parse_rules(seed.get("rules")), "内置种子的规律被自己的闸门拒了"
