"""插件系统 · 第 1 期:`plugin` 记录、命名空间、事实、规律、触发器、装/升/卸。

`docs/设计-插件系统.md` §2/§3/§4.1/§5.2/§7。这一层要守的东西,一条都不在"功能"上:

- **命名空间是硬边界。** 事实的存储键是 `<id>.<key>`,写别人的命名空间**开不了机**。
  放行的样子是安静的:两个插件各声明一个「灵力」,在同一个 hash 里撞成一个。
- **读别人的要声明。** 缺依赖开不了机 —— 放行的话它的规律每一轮都安静地跳过一次,
  而作者看到的是"我的机制不生效"。
- **队列快照 + drain 一遍。** 两个互相 emit 的触发器各跑一轮就停,不是把 tick
  线程转死 —— 而时钟卡住的样子是整个世界停了,没有一处报错。
- **没有插件的世界逐位不变。** 提示词逐字节相同,单独一条钉着。
"""
from __future__ import annotations

import json

import pytest

from _worldfile import open_world_at, redis_for, run_cli, write_seed_file

from anima_world.plugins import (
    PluginError, order_plugins, parse_plugins, version_tuple,
)


QI = {
    "id": "qi", "version": "1.0.0", "label": "灵力",
    "facts": {"灵力": {
        "bearer": "agent", "shape": "number", "default": 10.0, "range": [0, 100],
        "visibility": "self", "label": "灵力", "unit": "缕",
        "bands": [[0, "干涸", "你几乎调不动一丝气。"], [30, "尚可"],
                  [70, "充盈", "周身像有暖流,做什么都轻快。"]]}},
    "rules": [{"id": "回气", "every": {"ticks": 1}, "for_each": {"kind": "agent"},
               "set": {"qi.灵力": "clamp(qi.灵力 + 1.0 * dt, 0, 100)"}}],
    "triggers": [{"id": "干活耗气", "on": {"event": "entity_interaction"},
                  "effects": [{"set": {"qi.灵力": "clamp(qi.灵力 - 30, 0, 100)"}},
                              {"emit": {"type": "qi.耗尽", "text": "他脱了力"}}]}],
}
BARE = {
    "agents": [{"id": "阿岚", "name": "阿岚", "location": "cafe", "personality": "安静"}],
    "locations": [{"id": "cafe", "name": "咖啡馆", "description": "临海的小店"}],
}


def _world_with(tmp_path, *plugins, name="w"):
    path = write_seed_file(tmp_path / f"{name}.cyberworld",
                           {**BARE, "plugins": [dict(p) for p in plugins]})
    return open_world_at(str(tmp_path / f"{name}.db"), world_file=path,
                         force_mock_llm=True)


def _authored_plugins(world):
    """这个世界里**作者写的**那几个插件 —— 出厂那几个滤掉。

    🔴 **按 `FACTORY_PLUGINS` 那张权威表滤,不逐个写死名字**:
    2026-08-26 加第三个出厂插件(`invitation`,它没有开关、永远装)时,
    五条按名字写死的用例同时红了 —— 而它们红的原因和被测的东西一点关系都没有。
    """
    from anima_world.__main__ import FACTORY_PLUGINS

    return [p for p in world.scheduler.plugins if p.id not in FACTORY_PLUGINS]


def _authored_rows(rows):
    """`plugin list --json` 那几行里,作者写的那几行。"""
    from anima_world.__main__ import FACTORY_PLUGINS

    return [r for r in rows if r["id"] not in FACTORY_PLUGINS]



# ── 声明这一层 ──────────────────────────────────────────────────────────────


def test_id_不许带点号或减号_也不许是保留字():
    """`.` 是命名空间的分隔符,`-` 在表达式里是减法,保留字会让"这是插件还是内核"
    分不出来 —— 三种都当场拒,而且报错里说得出为什么。"""
    for bad, why in (("q.i", "`.`"), ("q-i", "`-`"), ("world", "保留字"),
                     ("Qi", "小写"), ("1qi", "小写字母开头")):
        with pytest.raises(PluginError) as raised:
            parse_plugins([{**QI, "id": bad}])
        assert raised.value.errors, bad


def test_写别人的命名空间开不了机():
    """插件只写得到 `<自己的id>.<事实名>`。放行的话它会安静地在别处建一个量,
    而这个插件的规律永远读不到它。"""
    with pytest.raises(PluginError) as raised:
        parse_plugins([{**QI, "rules": [{
            "id": "偷", "every": {"ticks": 1}, "for_each": {"kind": "agent"},
            "set": {"needs.energy": "1"}}]}])
    assert any("只写得到自己的命名空间" in e for e in raised.value.errors)


def test_读别人的没声明_开不了机而且点名():
    with pytest.raises(PluginError) as raised:
        parse_plugins([{**QI, "rules": [{
            "id": "蹭", "every": {"ticks": 1}, "for_each": {"kind": "agent"},
            "set": {"qi.灵力": "qi.灵力 + needs.energy"}}]}])
    assert any("reads" in e and "needs.energy" in e for e in raised.value.errors)


def test_缺依赖开不了机():
    plugins = parse_plugins([{**QI, "reads": ["needs.energy"], "rules": [{
        "id": "蹭", "every": {"ticks": 1}, "for_each": {"kind": "agent"},
        "set": {"qi.灵力": "qi.灵力 + needs.energy"}}]}])
    with pytest.raises(PluginError) as raised:
        order_plugins(plugins)
    assert any("没有装 `needs`" in e for e in raised.value.errors)


def test_依赖图定装载顺序():
    a = {"id": "aa", "version": "1.0.0",
         "facts": {"x": {"bearer": "world", "shape": "number"}}}
    b = {"id": "bb", "version": "1.0.0", "reads": ["aa.x"],
         "facts": {"y": {"bearer": "world", "shape": "number"}},
         "rules": [{"id": "r", "every": {"ticks": 1}, "for_each": {"owner": "world"},
                    "set": {"bb.y": "aa.x + 1"}}]}
    # 声明顺序反着写,装载顺序仍然是 aa 在前 —— 它读得到的那个量得先在库里。
    order = [p.id for p in order_plugins(parse_plugins([b, a]))]
    assert order == ["aa", "bb"], order


def test_依赖成环当场报():
    a = {"id": "aa", "version": "1.0.0", "reads": ["bb.y"],
         "facts": {"x": {"bearer": "world", "shape": "number"}}}
    b = {"id": "bb", "version": "1.0.0", "reads": ["aa.x"],
         "facts": {"y": {"bearer": "world", "shape": "number"}}}
    with pytest.raises(PluginError) as raised:
        order_plugins(parse_plugins([a, b]))
    assert any("成环" in e for e in raised.value.errors)


def test_这一版不收的两种形状_开不了机而且说得出为什么():
    """`timer` 与 `text` 声明得了、这一版收不了。**光秃秃的"不支持"会让作者以为
    自己写错了字**,所以报错里带理由。"""
    for shape in ("timer", "text"):
        with pytest.raises(PluginError) as raised:
            parse_plugins([{**QI, "facts": {"x": {"bearer": "agent", "shape": shape}}}])
        joined = "\n".join(raised.value.errors)
        assert shape in joined and ("两层点号" in joined or "存字符串" in joined), joined


def test_state_和字符串比大小_加载期就拦下来并说该写几():
    """放行的下场是运行期 TypeError,而那的样子是**这条规律安静地跳过了**。"""
    with pytest.raises(PluginError) as raised:
        parse_plugins([{
            "id": "sect", "version": "1.0.0",
            "facts": {"rank": {"bearer": "agent", "shape": "state",
                               "values": ["外门", "内门", "长老"]}},
            "rules": [{"id": "升", "every": {"ticks": 1}, "for_each": {"kind": "agent"},
                       "when": ["sect.rank == '内门'"],
                       "set": {"sect.rank": "sect.rank + 1"}}]}])
    joined = "\n".join(raised.value.errors)
    assert "序号" in joined and "第 1 档" in joined, joined


def test_订不到的事件_当场拒并报出可订的那张表():
    with pytest.raises(PluginError) as raised:
        parse_plugins([{**QI, "triggers": [{
            "id": "t", "on": {"event": "law_wanted"},
            "effects": [{"set": {"qi.灵力": "1"}}]}]}],
            subscribable={"entity_interaction"})
    assert any("订不到" in e for e in raised.value.errors)


def test_只发得出自己命名空间的事件():
    with pytest.raises(PluginError) as raised:
        parse_plugins([{**QI, "triggers": [{
            "id": "t", "on": {"event": "entity_interaction"},
            "effects": [{"emit": {"type": "payment"}}]}]}],
            subscribable={"entity_interaction"})
    assert any("只发得出自己命名空间" in e for e in raised.value.errors)


# ── 装 / 跑 / 卸 ────────────────────────────────────────────────────────────


def test_装上_规律跑_触发器跑_重开机还在(tmp_path):
    """1a 的验收主线,一条走完。"""
    with _world_with(tmp_path, QI) as world:
        owner = "agent:阿岚"
        assert world.stocks(owner) == {"qi.灵力": 10.0}, "默认值没种下去"

        world.tick(3)
        assert world.stocks(owner)["qi.灵力"] == 13.0, "规律没跑"

        world.scheduler._record_and_deliver({
            "type": "entity_interaction", "who": "阿岚", "loc": "cafe",
            "payload": {"target": "tree:x", "verb": "照料"}})
        assert len(world.scheduler._trigger_queue) == 1, "订了的事件没进队"
        world.tick(1)
        assert world.stocks(owner)["qi.灵力"] == 0.0, "触发器没跑"
        kinds = [e.type for e in world.scheduler.event_log.replay()]
        assert "qi.耗尽" in kinds, "触发器 emit 的事件没落库"

    # 重开机**不给 --world-file** —— 声明住在库里(和 `:kinds` / `:world_rules` 同一类)
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        assert [p.id for p in _authored_plugins(world)] == ["qi"]
        world.tick(1)
        assert world.stocks("agent:阿岚")["qi.灵力"] == 1.0, "重开机之后规律不跑了"


def test_触发器自己发的事件落进下一轮_没有同轮递归(tmp_path):
    """🔴 **两个互相 emit 的触发器各跑一轮就停。** 同轮递归的下场不是算错,
    是把 tick 线程转死 —— 而时钟卡住的样子是整个世界停了,没有一处报错。"""
    ping = {
        "id": "ping", "version": "1.0.0",
        "facts": {"n": {"bearer": "agent", "shape": "number", "default": 0.0}},
        "triggers": [
            {"id": "起", "on": {"event": "entity_interaction"},
             "effects": [{"set": {"ping.n": "ping.n + 1"}},
                         {"emit": {"type": "ping.回声"}}]},
            {"id": "回", "on": {"event": "ping.回声"},
             "effects": [{"set": {"ping.n": "ping.n + 10"}},
                         {"emit": {"type": "ping.回声"}}]},
        ],
    }
    with _world_with(tmp_path, ping, name="p") as world:
        owner = "agent:阿岚"
        world.scheduler._record_and_deliver({
            "type": "entity_interaction", "who": "阿岚", "loc": "cafe",
            "payload": {"target": "t", "verb": "v"}})
        world.tick(1)
        assert world.stocks(owner)["ping.n"] == 1.0, "第一轮该只跑「起」"
        world.tick(1)
        assert world.stocks(owner)["ping.n"] == 11.0, "第二轮该只跑一次「回」"
        world.tick(1)
        assert world.stocks(owner)["ping.n"] == 21.0, "每轮恰好一次,不是无限"


def test_升级裁剪掉声明里没了的事实(tmp_path):
    """升级 = 同 id 更高 version。**声明里没了的事实要裁掉** —— 留着的话它顶着
    旧 label 继续进提示词,和本体那一层「撤掉的量不裁剪」是同一个病,只是插件
    在自己的命名空间里裁得起。"""
    two = {**QI, "facts": {**QI["facts"],
                           "杂念": {"bearer": "agent", "shape": "number",
                                    "default": 3.0, "visibility": "self"}}}
    with _world_with(tmp_path, two, name="u") as world:
        assert "qi.杂念" in world.stocks("agent:阿岚")
        assert ("agent", "qi.杂念") in world.scheduler.visibility_store.rules_map()

    path = write_seed_file(tmp_path / "u2.cyberworld",
                           {**BARE, "plugins": [{**QI, "version": "1.1.0"}]})
    with open_world_at(str(tmp_path / "u.db"), world_file=path,
                       force_mock_llm=True) as world:
        stocks = world.stocks("agent:阿岚")
        assert "qi.杂念" not in stocks, "撤掉的事实没被裁剪 —— 它会继续进提示词"
        assert "qi.灵力" in stocks, "把还在声明里的那个也裁了"
        assert ("agent", "qi.杂念") not in world.scheduler.visibility_store.rules_map()


def test_不降级这道闸_只在pack_install那条路上(tmp_path, caplog):
    """🔴 **这条用例的断言在 3.11.2 反过来了一半,而反得对**(线上事故)。

    它原先钉的是「开机路上降级 = `WorldSeedError`」,而**舰队每次开机都带
    `--world-file`**:创世文件里的插件版本会比库里旧(库里那份是后来
    `pack install` 升上去的)—— 于是整支舰队起不来,**三版都有这道闸,
    回滚救不了**。

    分界照第 17 条那句「库里那份说了算」:
    **开机跳过并说一句(rc 0);`pack install` 照旧当场拒。**
    ⚠️ **两半都钉住** —— 只钉前一半的话,放宽会一路放宽到装包路上,
    而那儿"拿旧声明去盖新数据"仍然是一次真的、不可逆的降级。
    """
    import logging

    with _world_with(tmp_path, {**QI, "version": "2.0.0"}, name="d"):
        pass
    path = write_seed_file(tmp_path / "d2.cyberworld",
                           {**BARE, "plugins": [{**QI, "version": "1.0.0"}]})

    # ① 开机路:**起得来**,而且说一句
    with caplog.at_level(logging.INFO):
        open_world_at(str(tmp_path / "d.db"), world_file=path,
                      force_mock_llm=True).close()
    said = "\n".join(r.getMessage() for r in caplog.records)
    assert "库里那份说了算" in said, f"跳过了却一个字没说:{said[-400:]}"

    # 而库里那份没有被旧声明盖回去
    from anima_world.redis_state import RedisPluginStore
    from _worldfile import redis_for

    row = RedisPluginStore(redis_for(tmp_path / "d.db"), "w").get(QI["id"])
    assert str(row.get("version")) == "2.0.0", row

    # ② `pack install` 那条:**照旧当场拒**
    from anima_world.plugins import plugin_version_errors, parse_plugins

    parsed = parse_plugins([{**QI, "version": "1.0.0"}], ticks_per_day=288)
    problems = plugin_version_errors(parsed, RedisPluginStore(
        redis_for(tmp_path / "d.db"), "w"))
    assert problems and "不降级" in problems[0], problems


def test_version_tuple_读不懂的段按0算():
    assert version_tuple("3.8.0") > version_tuple("3.7.9")
    assert version_tuple("1.0") < version_tuple("1.0.1")
    assert version_tuple("") == (0,)


def test_卸掉之后键全无_再开机不复活(tmp_path):
    with _world_with(tmp_path, QI, name="r") as world:
        assert world.stocks("agent:阿岚")

    preview = run_cli("plugin", "remove", "qi", "--world-id", "w")
    assert preview.returncode == 0, preview.stderr
    assert "没带 --yes" in preview.stdout
    with open_world_at(str(tmp_path / "r.db"), force_mock_llm=True) as world:
        assert "qi.灵力" in world.stocks("agent:阿岚"), "预演动了世界"

    done = run_cli("plugin", "remove", "qi", "--world-id", "w", "--yes", "--json")
    assert done.returncode == 0, done.stderr
    receipt = json.loads(done.stdout)
    assert receipt["found"] and receipt["facts"] >= 1

    with open_world_at(str(tmp_path / "r.db"), force_mock_llm=True) as world:
        assert world.stocks("agent:阿岚") == {}, "卸完键还在"
        assert _authored_plugins(world) == [], "卸完再开机它又活了"
        assert ("agent", "qi.灵力") not in world.scheduler.visibility_store.rules_map()


def test_cli_plugin_list(tmp_path):
    with _world_with(tmp_path, QI, name="l"):
        pass
    out = run_cli("plugin", "list", "--world-id", "w", "--json")
    assert out.returncode == 0, out.stderr
    rows = _authored_rows(json.loads(out.stdout)["plugins"])
    assert [r["id"] for r in rows] == ["qi"]
    assert rows[0]["version"] == "1.0.0" and rows[0]["facts"] == ["灵力"]
    assert rows[0]["rules"] == 1 and rows[0]["triggers"] == 1
    # ⚠️ **`order` 是这个世界里的绝对序号,不是"作者写的那几个里第几个"** ——
    # 出厂插件也排在同一条依赖链上(`invitation` 没有开关、永远装)。
    # 钉"它是 0"等于钉"这个世界里没有别的插件",而那是另一件事。
    assert rows[0]["order"] == next(
        r["order"] for r in json.loads(out.stdout)["plugins"] if r["id"] == "qi"
    ), "装载顺序也是答案的一部分"

    human = run_cli("plugin", "list", "--world-id", "w")
    assert "qi 1.0.0" in human.stdout and "灵力" in human.stdout


def test_抹除把玩家身上的插件事实一起带走(tmp_path):
    """第 0 期那条抹除删的是**整个 hash**,所以插件的事实自动跟着走 ——
    这条用例钉住"自动"这两个字:哪天有人把它改成按键名删,这里当场红。"""
    with _world_with(tmp_path, QI, name="e") as world:
        world.player_move("ghost", "cafe", display_name="阿檀")
        owner = "agent:player:ghost"
        assert "qi.灵力" in world.stocks(owner), "玩家没拿到插件事实"
        receipt = world.erase_player("ghost")
        assert world.stocks(owner) == {}
        assert receipt["facts"] >= 1


def test_玩家和角色同一个命名空间(tmp_path):
    """`me_*` 读的是"一个人身上的量",而这件事对两种人是同一件。"""
    with _world_with(tmp_path, QI, name="m") as world:
        world.player_move("p1", "cafe", display_name="阿檀")
        assert world.stocks("agent:player:p1") == {"qi.灵力": 10.0}


# ── 提示词 ──────────────────────────────────────────────────────────────────


def test_描述进提示词_紧跟感知块(tmp_path):
    """老板原话:「数字可以加入别名,95 是亲密无间,然后加入描述」。"""
    from anima_world.perception import perceive

    with _world_with(tmp_path, QI, name="pp") as world:
        world.scheduler.stock_store.set_many("agent:阿岚", {"qi.灵力": 80.0}, tick=1)
        block = perceive(agent_id="阿岚", here="cafe",
                         stock_store=world.scheduler.stock_store,
                         visibility=world.scheduler.visibility_store,
                         ontology=world.scheduler.ontology).render()
    assert "灵力 充盈" in block, "档词没进去"
    assert "周身像有暖流,做什么都轻快。" in block, "描述没进去"
    # 数字**不上屏**(分过档的量那条老纪律,一个字没改)
    assert "80" not in block


def test_没写描述的量_一个字都不多(tmp_path):
    from anima_world.perception import perceive

    plain = {**QI, "facts": {"灵力": {**QI["facts"]["灵力"],
                                      "bands": [[0, "干涸"], [70, "充盈"]]}}}
    with _world_with(tmp_path, plain, name="np") as world:
        world.scheduler.stock_store.set_many("agent:阿岚", {"qi.灵力": 80.0}, tick=1)
        block = perceive(agent_id="阿岚", here="cafe",
                         stock_store=world.scheduler.stock_store,
                         visibility=world.scheduler.visibility_store,
                         ontology=world.scheduler.ontology).render()
    assert "灵力 充盈" in block
    assert "——" not in block, "没写描述的量多出了一节"


def test_无作者插件的世界_提示词里一点插件痕迹都没有(tmp_path):
    """🔴 **这一条是这一期最硬的那一条。** 一个不写 `plugin` 的世界,这一层整个
    缺席 —— 和 perception / ontology / beats 逐字同构(声明本身就是开关)。

    拿**内置橱窗**比:装插件系统之前之后,`prompt --agent` 的字节要相同。
    这里用的判据是它的两半 —— 感知块与整份提示词 —— 在"有没有插件层"这件事上
    一个字都不动:橱窗一个 `plugin` 记录都没有,所以 `plugins` 是空的,
    而空的那一层不许在任何一处留下痕迹。
    """
    db = tmp_path / "demo.db"
    redis_for(db)
    with open_world_at(str(db), force_mock_llm=True) as world:
        # 橱窗自己**没有一条作者写的 `plugin` 记录**;`needs` 是**出厂**那一个
        # (3.8.0 起它也是插件了,见 §9 的第一个搬家对象),而它三个事实全是
        # `hidden` —— 今天的 needs 一格都不进感知块,搬家没有动这一点。
        # `needs` 与 `economy` 都是**出厂**那两个(3.8.0;economy 只搬了钱包那一格),
        # 它们的事实全是 `hidden` —— 一格都不进感知块,搬家没有动这一点。
        authored = [p.id for p in _authored_plugins(world)]
        assert authored == [], f"橱窗带上了作者插件:{authored}"
        assert world.scheduler._triggers_by_event == {}, "橱窗一个触发器都不该有"
        agent_id = next(iter(world.scheduler.agents))
        blocks = world.debug_prompt(agent_id)
    rendered = json.dumps(blocks, ensure_ascii=False, sort_keys=True)
    # 插件那两格在一个没有作者插件的世界里**根本不出现**(不是"出现但是空的")
    assert "band_notes" not in rendered
    assert "plugin" not in rendered
    # 出厂 needs 搬家之后,她的提示词里**一个 `needs.` 都不许有** —— 那三个量
    # 是 `hidden`,而"没声明 = 感知不到"是这一层的默认值。
    assert "needs." not in rendered, "需求搬家把它自己搬进提示词里去了"
    # 🔴 **逐字节那一半不在这条用例里,而这个名字从前假装它在**
    # (2026-08-26 验收 A:「名不副实 —— 只断言几个字符串不在,没比 sha256」)。
    # 真正钉逐字节的是 `tests/test_needs_plugin_parity.py::test_提示词逐字节相同`:
    # 它拿的基线是插件系统落地**之前**旧路真跑出来的,比的是 sha256。
    # **一条名字承诺了逐字节、而身体只查了几个子串的测试,比没有这条测试更坏** ——
    # 它让人以为那件事被盯着。所以这条改了名,只说它真的在查的那件事。


# ── 契约 ────────────────────────────────────────────────────────────────────


def test_契约报得出这一版收什么_不是设计稿那张表():
    """🔴 **`fact_shapes` 是「这一版引擎收不收」,不是「这套架构装得下什么」。**
    照设计稿写探测器的人会以为 `timer` / `text` 能用,而它们写了开不了机。"""
    from anima_world.plugins import (
        BEARER_FORMS, DEFERRED_SHAPES, EFFECTS, FACT_SHAPES, PLUGIN_ID_PATTERN,
        RESERVED_IDS,
    )

    out = run_cli("contract", "--json")
    assert out.returncode == 0, out.stderr
    seg = json.loads(out.stdout)["plugins"]
    assert seg["author_type"] == "plugin"
    assert seg["fact_shapes"] == list(FACT_SHAPES) == ["number", "state"]
    assert set(seg["deferred_fact_shapes"]) == set(DEFERRED_SHAPES)
    for shape, why in seg["deferred_fact_shapes"].items():
        assert why.strip(), f"{shape} 只说了「不收」,没说为什么"
    assert seg["effects"] == list(EFFECTS)
    assert seg["bearer_forms"] == list(BEARER_FORMS)
    assert seg["id_pattern"] == PLUGIN_ID_PATTERN
    assert set(seg["reserved_ids"]) == set(RESERVED_IDS)
    assert seg["namespace_syntax"] == "<plugin>.<fact>"
    assert seg["state_in_expressions"] == "ordinal"
    assert seg["read_command"] == "plugin list"
    # 第 0 期那一格照旧在(只加不改)
    assert seg["subscribable_events"]


def test_带plugin记录的包_离线两扇门也查(tmp_path):
    """**新增一种开机失败,就必须同一轮补进离线那两扇门。** 3.7.0 收节拍时
    第一版只收了段没补门,于是 `world check` 对一份开不了机的文件照答绿灯。"""
    import gzip

    path = tmp_path / "bad.cyberworld"
    rows = [
        {"kind": "manifest", "version": 3, "world_id": "t", "engine_min": "3.8.0"},
        {"kind": "author", "type": "plugin",
         "body": {"id": "world", "version": "1.0.0", "facts": {}}},   # 保留字
    ]
    with gzip.GzipFile(path, "wb", mtime=0) as fh:
        fh.write(("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n").encode())

    check = json.loads(run_cli("world", "check", str(path), "--edit", "--json").stdout)
    assert check["loadable"] is False, "对一份开不了机的包答了绿灯"
    assert any("保留字" in e for e in check["errors"]), check["errors"]
    validate = json.loads(
        run_cli("validate", "world", str(path), "--edit", "--json").stdout)
    assert validate["valid"] is False
    assert check["errors"] == validate["errors"], "两扇门对同一份文件给了不同答案"


# ── 边(3.8.0 第 2 期)────────────────────────────────────────────────────────
#
# 边是"两个人之间"的家:师徒、婚约、欠情、心动、门派成员、物品归属。设计稿 §2.2
# 那句"**pair 不再是特殊概念:它就是 agent→agent 的边**"落在这一节。


SECT = {
    "id": "sect", "version": "1.0.0", "label": "师徒",
    "edges": {"apprentice_of": {
        "label": "师承", "from": "agent", "to": "agent", "exclusive": True,
        "facts": {
            "rank": {"shape": "state", "default": "新入门", "visibility": "connected",
                     "label": "身份",
                     "values": [{"name": "新入门"},
                                {"name": "亲传",
                                 "description": "师父把压箱底的东西教给你了。"}]},
            "门规": {"shape": "text", "default": "不得欺师灭祖",
                     "visibility": "connected", "label": "门规"},
            "私心": {"shape": "number", "default": 0.0, "visibility": "hidden"},
        }}},
    "triggers": [{
        "id": "拜师", "on": {"event": "conversation"},
        "when": ["event.message_count >= 99"],
        "effects": [{"link": {"type": "sect.apprentice_of",
                              "from": "self", "to": "agent:师父"}}],
    }],
}


def test_边建得起来_而且约束在link那一刻查(tmp_path):
    with _world_with(tmp_path, SECT, name="edge") as world:
        store = world.scheduler.edge_store
        ns = {}
        ok = world.scheduler.apply_edge_effect(
            {"op": "link", "type": "sect.apprentice_of",
             "from": "agent:阿岚", "to": "agent:师父"}, ns)
        assert ok and len(store.all("sect.apprentice_of")) == 1
        # 边上的事实**按声明种下默认值**(`state` 落序号,`text` 落那句话)。
        _src, _dst, facts = store.all("sect.apprentice_of")[0]
        assert facts["sect.rank"] == 0.0 and facts["sect.门规"] == "不得欺师灭祖"

        # `exclusive`:起点那一端唯一 —— 第二个师父连不上,而且**说出来**。
        again = world.scheduler.apply_edge_effect(
            {"op": "link", "type": "sect.apprentice_of",
             "from": "agent:阿岚", "to": "agent:另一个师父"}, ns)
        assert again is False, "exclusive 没拦住 —— 她同时拜了两个师父"
        assert len(store.all("sect.apprentice_of")) == 1


def test_断边与转移(tmp_path):
    with _world_with(tmp_path, SECT, name="edge2") as world:
        sch = world.scheduler
        sch.apply_edge_effect({"op": "link", "type": "sect.apprentice_of",
                               "from": "agent:阿岚", "to": "agent:师父"}, {})
        # transfer:把起点那一端换人,**事实跟着走**(设计稿 §4.1 的 `transfer`)。
        sch.apply_edge_effect({"op": "transfer", "type": "sect.apprentice_of",
                               "from": "agent:小竹", "to": "agent:师父",
                               "by_dst": True}, {})
        rows = sch.edge_store.all("sect.apprentice_of")
        assert [(r[0], r[1]) for r in rows] == [("agent:小竹", "agent:师父")]
        assert rows[0][2]["sect.门规"] == "不得欺师灭祖", "事实没跟着走"
        # unlink 只给一端 = 这一端上这个类型的边全断掉。
        assert sch.apply_edge_effect({"op": "unlink", "type": "sect.apprentice_of",
                                      "from": "agent:小竹"}, {})
        assert sch.edge_store.all("sect.apprentice_of") == []


def test_connected那一档_让边的两端都读得到(tmp_path):
    """**`connected` 补的是既不是「只有我」也不是「同一个地方」的那一档**
    (设计稿 §5.1)。门规对弟子可见、秘密对知情人可见,全是这一档。"""
    from anima_world.perception import perceive

    with _world_with(tmp_path, SECT, name="edge3") as world:
        world.scheduler.apply_edge_effect(
            {"op": "link", "type": "sect.apprentice_of",
             "from": "agent:阿岚", "to": "agent:师父",
             "facts": {"sect.rank": 1.0, "sect.私心": 0.9}}, {})
        block = perceive(agent_id="阿岚", here="cafe",
                         stock_store=world.scheduler.stock_store,
                         visibility=world.scheduler.visibility_store,
                         ontology=world.scheduler.ontology,
                         edges=world._edges_for("阿岚")).render()
    assert "你和" in block and "师承" in block, block
    assert "身份 亲传" in block, "边上的 state 没念成值名"
    assert "师父把压箱底的东西教给你了。" in block, "values 的描述没进提示词"
    assert "门规 不得欺师灭祖" in block, "边上的 text 没进提示词"
    # `hidden` 的那一格一个字都不许出去 —— 它是默认值,和量那一层逐字同构。
    assert "私心" not in block and "0.9" not in block


def test_一端是玩家的边_抹除时一条不留(tmp_path):
    """设计稿 §7 那条红字。插件把"两个人之间"表达成边,而边**两端都写着谁** ——
    抹除够不着它们的话,一个"已经抹掉"的人会以边的形式留在世界里。"""
    with _world_with(tmp_path, SECT, name="edge4") as world:
        world.player_move("ghost-edge", "cafe", display_name="阿檀")
        node = world.scheduler.stock_owner_of("player:ghost-edge")
        world.scheduler.apply_edge_effect(
            {"op": "link", "type": "sect.apprentice_of",
             "from": node, "to": "agent:阿岚"}, {})
        # 另一个人的边,一个字都不许动。
        world.scheduler.apply_edge_effect(
            {"op": "link", "type": "sect.apprentice_of",
             "from": "agent:小竹", "to": "agent:阿岚"}, {})
        assert len(world.scheduler.edge_store.all("sect.apprentice_of")) == 2

        receipt = world.erase_player("ghost-edge")

        rows = world.scheduler.edge_store.all("sect.apprentice_of")
        assert [(r[0], r[1]) for r in rows] == [("agent:小竹", "agent:阿岚")], (
            "抹除要么漏了他那条,要么误伤了别人那条"
        )
        assert receipt["edges"] == 1, f"回执那一格不对:{receipt['edges']}"


def test_边这一格也是三档(tmp_path):
    """缺席 = 这支引擎不认边;`null` = 我没查成;`0` = 查过了他身上没有边。
    和 `facts` 逐字同构 —— 两格用同一条纪律,读的人才不用记两套。"""
    with _world_with(tmp_path, SECT, name="edge5") as world:
        receipt = world.erase_player("nobody-here")
        assert receipt["edges"] == 0, "没有边要报 0(我查过了),不是 null"


def test_插件只连得动自己声明的边():
    with pytest.raises(PluginError) as raised:
        parse_plugins([{**SECT, "triggers": [{
            "id": "偷连", "on": {"event": "conversation"},
            "effects": [{"link": {"type": "economy.owns",
                                  "from": "self", "to": "x"}}]}]}])
    assert any("没声明过" in e for e in raised.value.errors)


def test_text只在边上收得下():
    """节点的事实住在量表里(`[float, tick]`),存不下字符串;边自己那一行本来就是
    一份 JSON。**这不是偏心,是存储的形状** —— 而报错要说出这句话。"""
    with pytest.raises(PluginError) as raised:
        parse_plugins([{"id": "tt", "version": "1.0.0",
                        "facts": {"x": {"bearer": "actor", "shape": "text"}}}])
    joined = "\n".join(raised.value.errors)
    assert "text" in joined and "量表" in joined, joined
    # 而边上它过得去。
    ok = parse_plugins([{"id": "tt", "version": "1.0.0",
                         "edges": {"e": {"facts": {"x": {"shape": "text"}}}}}])
    assert ok[0].edges["e"].facts["x"].shape == "text"


def test_bearer三个词_而agent读成actor():
    """老板 2026-08-26 自判的那一条。⚠️ **`agent` 这个词是第 1 期刚公布的**,
    那时它的意思是"角色和玩家" —— 收紧一个刚发出去的词而不留兼容,下场是第 1 期
    写的插件安静地少覆盖一半人。"""
    from anima_world.needs import PLUGIN_ID

    p = parse_plugins([{"id": "tt", "version": "1.0.0", "facts": {
        "a": {"bearer": "agent"}, "b": {"bearer": "actor"},
        "c": {"bearer": "player"}}}])[0]
    assert p.facts["a"].bearer == "actor", "老写法没被读成今天的语义"
    assert p.facts["c"].bearer == "player"
    # 三种落的是同一行可见性(可见性按**种类**声明,玩家的量 owner 是 agent:player:…)
    assert {f.owner_kind for f in p.facts.values()} == {"agent"}
    # 出厂 needs 声明的就是 `actor`(不改行为)。
    from anima_world.needs import factory_plugin

    needs = parse_plugins([factory_plugin()])[0]
    assert {f.bearer for f in needs.facts.values()} == {"actor"}
    assert needs.id == PLUGIN_ID


def test_只有玩家的事实不种给角色(tmp_path):
    only_player = {"id": "pp", "version": "1.0.0",
                   "facts": {"疲惫": {"bearer": "player", "default": 5.0}}}
    with _world_with(tmp_path, only_player, name="pl") as world:
        assert world.stocks("agent:阿岚") == {}, "`player` 的事实种到角色身上去了"
        world.player_move("p1", "cafe", display_name="阿檀")
        assert world.stocks("agent:player:p1") == {"pp.疲惫": 5.0}


def test_白名单里每一种事件都真的到得了触发器(tmp_path):
    """🔴 **白名单说"订得到",那就必须真的订得到 —— 逐种验,不是抽验。**

    2026-08-26 验收 C 实测出的第 1 期真 bug:入队挂在 `_record_and_deliver` 上,
    而**十种里有五种根本不走那条路**(`entity_interaction` / `entity_spawn` /
    `entity_destroy` / `item_consume` / `payment` 直接调 `_record_event`,
    `state_change` 一半一半)。于是订 `entity_interaction` 的触发器在 288 tick 里
    事件真发了 4 次、触发 **0** 次、值一格没动、**一处不报错**,而 `plugin list`
    照旧印着「触发器 1」—— **那正是 FOR-STUDIO §3.37 与 REFERENCE §10.1 唯一的例子。**

    修法是把入队挂到**落库**那一处(`_record_event`,事件真正变成"发生过"的地方)。
    而这条测试是那个修法的闸:**每往白名单里加一种事件,这里就必须跟着响一次**,
    否则第 2 期加的新事件又是死的 —— 而死的样子和这次一模一样,安静。
    """
    from anima_world.events import SUBSCRIBABLE_EVENTS

    counter = {
        "id": "probe", "version": "1.0.0",
        "facts": {"n": {"bearer": "world", "default": 0.0}},
        "triggers": [
            {"id": f"t_{index}", "on": {"event": name},
             "for_each": {"node": "world"},
             "effects": [{"set": {"probe.n": "probe.n + 1"}}]}
            for index, name in enumerate(sorted(SUBSCRIBABLE_EVENTS))
        ],
    }
    with _world_with(tmp_path, counter, name="probe") as world:
        sch = world.scheduler
        for name in sorted(SUBSCRIBABLE_EVENTS):
            before = float(world.stocks("world").get("probe.n", 0.0))
            # **走 `_record_event`,不走 `_record_and_deliver`** —— 那五种死掉的
            # 事件走的正是它,所以这条测试必须从最窄的那扇门进。
            sch._record_event({"type": name, "who": "阿岚", "loc": "cafe",
                               "payload": {"probe": 1}})
            world.tick(1)
            after = float(world.stocks("world").get("probe.n", 0.0))
            assert after == before + 1, (
                f"`{name}` 在白名单上,而订它的触发器一次都没响 —— "
                "白名单是一句公开契约,而这一格是它最容易安静地变成假话的地方"
            )


def test_出厂插件卸不动_而且指向那个开关(tmp_path):
    """🔴 **一次会掉数据的空操作**(2026-08-26 验收 C 实测):
    `plugin remove needs --yes` 答"卸了",redis 里真没了,**而下一次开机它装回来、
    三个值全变回 1.0** —— 回执说成功、屏幕不提一个字,而她的精力被悄悄补满。
    REFERENCE §10.9 自己刚写过这句话。

    为什么是**拒**而不是"真卸且下次不装回":后者要记一个"作者卸过"的标记,
    而那个标记和 `needs.enabled` 就是同一件事的第二份答案。**开关只有一个。**
    """
    db = tmp_path / "factory.db"
    redis_for(db)
    with open_world_at(str(db), force_mock_llm=True) as world:
        world.config_set("needs.enabled", True)
        world.tick(5)
        before = dict(world.stocks("agent:夏"))
        assert any(k.startswith("needs.") for k in before), "夹具前提没成立"

    done = run_cli("plugin", "remove", "needs", "--world-id", "w", "--yes")
    assert done.returncode == 2, "出厂插件被卸掉了"
    assert "出厂插件" in done.stderr and "needs.enabled" in done.stderr, done.stderr
    assert "不删数据" in done.stderr, "没告诉他关掉不会掉数据"

    with open_world_at(str(db), force_mock_llm=True) as world:
        assert world.stocks("agent:夏") == before, "被拒的那一趟动了世界"


# ── 插件声明的种类与动词(2a 下半)────────────────────────────────────────────
#
# 🔴 **它们编译成普普通通的本体种类与 affordance。** 这一处判断是这一期最省事、
# 也最该这么做的一个:种类那一层已经有出生自检、「生成必须要代价」、`prompt.budget`、
# 可见性、拒绝语、`resolve` 的跨引用闸 —— 插件另建一套的话,那几件要么重写一遍、
# 要么悄悄不生效,而"悄悄不生效"正是这个仓库最怕的形状。


FORGE = {
    "id": "forge", "version": "1.0.0", "label": "铸剑",
    "kinds": {
        "entity:sword": {"gloss": "一把剑", "budget": 3, "facts": {
            "锋利": {"shape": "number", "default": 1.0, "visibility": "here",
                     "label": "锋利", "bands": [[0, "钝"], [5, "锋利"]]}}},
        "entity:shard": {"gloss": "一块碎铁", "facts": {}},
    },
    "verbs": {
        "磨": {"target": "entity:sword", "label": "磨一磨",
               "description": "把剑磨快一点",
               "when": ["锋利 < 10"], "set": {"锋利": "锋利 + 1"},
               "requires": ["me_体力 >= 5"], "costs": {"体力": "me_体力 - 5"}},
        "砸碎": {"target": "entity:sword", "label": "砸碎它",
                 "costs": {"体力": "me_体力 - 3"},
                 "destroys_target": True},
        "打一把": {"target": "entity:shard", "label": "打一把剑",
                   "costs": {"体力": "me_体力 - 20"},
                   "spawn": {"kind": "forge.sword", "name": "新打的剑"},
                   "destroys_target": False},
    },
}
_ACTOR = {"id": "agent", "quantities": {
    "体力": {"default": 100.0, "visibility": "self", "label": "体力"}}}


def _forge_world(tmp_path, name="forge", plugin=None, entities=None):
    path = write_seed_file(tmp_path / f"{name}.cyberworld", {
        **BARE,
        "kinds": [dict(_ACTOR)],
        "entities": entities if entities is not None else [
            {"id": "forge.sword:青锋", "name": "青锋剑", "location": "cafe"}],
        "plugins": [dict(plugin or FORGE)],
    })
    return open_world_at(str(tmp_path / f"{name}.db"), world_file=path,
                         force_mock_llm=True)


def test_插件的种类就是本体的种类(tmp_path):
    with _forge_world(tmp_path) as world:
        kinds = {k["id"] for k in world.kinds()}
        assert "forge.sword" in kinds, "插件的种类没进本体"
        # 事实变成**量**,而且**不带命名空间前缀** —— 种类 id 本身就是命名空间。
        assert world.stocks("forge.sword:青锋") == {"锋利": 1.0}
        row = next(k for k in world.kinds() if k["id"] == "forge.sword")
        assert row["gloss"] == "一把剑" and row["budget"] == 3
        # 动词挂在**它的 target 那个种类**上 —— `打一把` 的 target 是碎铁,
        # 所以它在 `forge.shard` 上,不在剑上。
        assert {a["verb"] for a in row["affordances"]} == {"磨", "砸碎"}
        shard = next(k for k in world.kinds() if k["id"] == "forge.shard")
        assert {a["verb"] for a in shard["affordances"]} == {"打一把"}


def test_动词走的是真的那条能力路(tmp_path):
    """`act(她, interact, {target, verb})` —— 和作者写在 `kinds` 里的动词**同一条路**,
    所以 `requires` / `costs` 那一整摞一件都不用重写。"""
    with _forge_world(tmp_path, name="v") as world:
        out = world.act("阿岚", "interact",
                        {"target": "forge.sword:青锋", "verb": "磨"}, surface="body")
        assert out["ok"] is True, out
        assert world.stocks("forge.sword:青锋") == {"锋利": 2.0}
        assert world.stocks("agent:阿岚")["体力"] == 95.0, "代价没付"


def test_生成必须要代价那条纪律_对插件一样成立(tmp_path):
    """**声明了 `spawn` 却没写代价,开不了机。** 这一条是 2.0 就定下的,而插件
    编译成本体种类之后**免费继承**了它 —— 那正是不另起一套的全部理由。"""
    from anima_world.world_seed import WorldSeedError

    bad = {**FORGE, "verbs": {"白捡一把": {
        "target": "entity:shard", "label": "白捡",
        "spawn": {"kind": "forge.sword", "name": "白来的剑"}}}}
    with pytest.raises((WorldSeedError, Exception)) as raised:
        _forge_world(tmp_path, name="free", plugin=bad,
                     entities=[{"id": "forge.shard:铁块", "name": "铁块",
                                "location": "cafe"}])
    assert "代价" in str(raised.value), raised.value


def test_spawn与destroy走的是现成的机器(tmp_path):
    """生出来的东西过**出生自检**、id 由引擎发且只增不减、抹掉时四样一起走 ——
    这些一行都没重写。"""
    with _forge_world(tmp_path, name="sd",
                      entities=[{"id": "forge.shard:铁块", "name": "铁块",
                                 "location": "cafe"},
                                {"id": "forge.sword:青锋", "name": "青锋剑",
                                 "location": "cafe"}]) as world:
        born = world.act("阿岚", "interact",
                         {"target": "forge.shard:铁块", "verb": "打一把"},
                         surface="body")
        assert born["ok"] is True, born
        made = [e for e in world.entities() if e["kind"] == "forge.sword"]
        assert len(made) == 2, f"没生出来:{made}"
        # 新生的那一把**量落地了**(出生自检查的就是这个)。
        fresh = next(e for e in made if e["id"] != "forge.sword:青锋")
        assert world.stocks(fresh["id"]) == {"锋利": 1.0}
        kinds = [e.type for e in world.scheduler.event_log.replay()]
        assert "entity_spawn" in kinds

        world.act("阿岚", "interact",
                  {"target": "forge.sword:青锋", "verb": "砸碎"}, surface="body")
        assert not [e for e in world.entities() if e["id"] == "forge.sword:青锋"]
        assert world.stocks("forge.sword:青锋") == {}, "抹掉时量没跟着走"


def test_没有目标的动词_当场开不了机而不是装上去点不动(tmp_path):
    """「开宗立派」那种不对着任何东西做的动词,今天**没有一条调用路**
    (能力调用一律是 `act(她, interact, {target, verb})`)。
    **装上去让谁也点不动,比开不了机坏** —— 后者作者当场知道。"""
    with pytest.raises(PluginError) as raised:
        parse_plugins([{**FORGE, "verbs": {"开宗立派": {"label": "开宗立派"}}}])
    assert any("少了 target" in e and "调用路" in e for e in raised.value.errors)


def test_动词按tool_calling的schema报出来():
    """设计 §12.3:**NPC 挑动词和玩家点按钮读的是同一份定义** —— 它们从前是两份。"""
    verb = parse_plugins([FORGE])[0].verbs["磨"]
    schema = verb.schema()
    assert schema["name"] == "forge.磨"
    assert schema["description"] == "把剑磨快一点"
    assert schema["parameters"]["required"] == ["target"]


# ── 2a 收口:动词连得起边、东西没了边跟着走、边上的规律 ──────────────────────
#
# 这四条各钉一件"少了它照跑、而错法是安静的"事:
#
# - 动词只改得动量的话,`plugin.edges` 就只有触发器一条进得去的路 —— 而作者写
#   「入门」时想的是**他按一下**,不是"等某件事发生"。
# - 一样东西没了,挂在它身上的边留着:`for_each:{edge:…}` 每 tick 在一条指向
#   坟墓的边上求值,两端有一端读不到量 → 规律安静地跳过,`rule_stats` 说 skipped。
# - 边上的规律没有闸的话,`_evaluate_edge_rules` 整个函数可以被删掉而全绿。

MENPAI = {
    "id": "menpai", "version": "1.0.0", "label": "门派",
    "kinds": {"group:sect": {"gloss": "一个门派", "facts": {
        "库银": {"shape": "number", "default": 100.0, "visibility": "here",
                 "label": "库银", "bands": [[0, "空空如也"], [50, "尚可周转"]]}}}},
    "edges": {"member_of": {
        "label": "门籍", "from": "agent", "to": "group:sect", "exclusive": True,
        "facts": {
            "rank": {"shape": "state", "default": "外门", "visibility": "connected",
                     "label": "身份",
                     "values": [{"name": "外门"},
                                {"name": "内门", "description": "门规是你的规矩。"}]},
            "门规": {"shape": "text", "default": "不得欺师灭祖",
                     "visibility": "connected", "label": "门规"},
            "资历": {"shape": "number", "default": 0.0, "visibility": "connected",
                     "label": "资历", "bands": [[0, "新来的"], [3, "老人"]]},
        }}},
    "verbs": {
        "入门": {"target": "group:sect", "label": "拜入门下",
                 "description": "递上名帖,拜入这个门派",
                 "costs": {"体力": "me_体力 - 5"},
                 "effects": [{"link": {"type": "member_of",
                                       "from": "self", "to": "target"}}]},
        "退出": {"target": "group:sect", "label": "退出师门",
                 "costs": {"体力": "me_体力 - 1"},
                 "effects": [{"unlink": {"type": "member_of",
                                         "from": "self", "to": "target"}}]},
        "拆了它": {"target": "group:sect", "label": "拆了这个门派",
                   "costs": {"体力": "me_体力 - 9"}, "destroys_target": True},
    },
    "rules": [{"id": "熬资历", "every": {"ticks": 1}, "for_each": {"edge": "member_of"},
               "when": ["edge.menpai.rank >= 0"],
               "set": {"menpai.资历": "edge.menpai.资历 + 1"}}],
}
_TIRED = {"id": "agent", "quantities": {
    "体力": {"default": 100.0, "visibility": "self", "label": "体力"}}}


def _menpai_world(tmp_path, name="menpai", plugin=None, fresh=True):
    path = write_seed_file(tmp_path / f"{name}.cyberworld", {
        **BARE,
        "kinds": [dict(_TIRED)],
        "entities": [{"id": "menpai.sect:青云门", "name": "青云门", "location": "cafe"}],
        "plugins": [dict(plugin or MENPAI)],
    })
    return open_world_at(str(tmp_path / f"{name}.db"),
                         world_file=path if fresh else None, force_mock_llm=True)


def test_动词连得起边来_而不是只有触发器那一条路(tmp_path):
    """**「入门」是他按一下,不是等某件事发生。**

    动词只改得动量的话,`plugin.edges` 就只剩触发器一条进得去的路 —— 而设计稿
    §4.2 那四个例子(开宗立派 / 逐出 / 提拔 / 给东西)**没有一个**是"等某件事"。
    """
    with _menpai_world(tmp_path) as world:
        out = world.act("阿岚", "interact",
                        {"target": "menpai.sect:青云门", "verb": "入门"},
                        surface="body")
        assert out["ok"] is True, out
        rows = world.scheduler.edge_store.all("menpai.member_of")
        assert [(r[0], r[1]) for r in rows] == [("agent:阿岚", "menpai.sect:青云门")]
        # 边上的事实按声明种下默认值 —— 和触发器那条路**同一个** `apply_edge_effect`。
        assert rows[0][2]["menpai.门规"] == "不得欺师灭祖"
        assert world.stocks("agent:阿岚")["体力"] == 95.0, "动词的代价没付"

        # 连上了,`connected` 那一档就把门规念给他听(设计 §5.1)。
        from anima_world.perception import perceive

        block = perceive(agent_id="阿岚", here="cafe",
                         stock_store=world.scheduler.stock_store,
                         visibility=world.scheduler.visibility_store,
                         ontology=world.scheduler.ontology,
                         edges=world._edges_for("阿岚")).render()
        assert "门规 不得欺师灭祖" in block, block

        world.act("阿岚", "interact",
                  {"target": "menpai.sect:青云门", "verb": "退出"}, surface="body")
        assert world.scheduler.edge_store.all("menpai.member_of") == []


def test_一样东西没了_挂在它身上的边一条不留(tmp_path):
    """**`destroy` 连带 `unlink`**(任务单 2a 那句)。

    留着的下场是安静的:`for_each:{edge:…}` 每 tick 在一条指向坟墓的边上求值,
    两端有一端读不到量,于是这条规律**安静地跳过**,而 `rule_stats()` 报的是
    skipped —— 专门用来回答"这层跑通了吗"的仪表说的是"没什么可算的"。
    """
    with _menpai_world(tmp_path, name="gone") as world:
        world.act("阿岚", "interact",
                  {"target": "menpai.sect:青云门", "verb": "入门"}, surface="body")
        assert len(world.scheduler.edge_store.all("menpai.member_of")) == 1
        out = world.act("阿岚", "interact",
                        {"target": "menpai.sect:青云门", "verb": "拆了它"},
                        surface="body")
        assert out["ok"] is True and out["detail"].get("destroyed"), out
        assert world.scheduler.edge_store.all("menpai.member_of") == [], \
            "东西没了,边还挂着 —— 一条指向坟墓的边"


def test_边上的规律_读得到边自己也读得到两端(tmp_path):
    """`for_each: {"edge": …}`:表达式里 `edge.*` / `src.*` / `dst.*` 三个前缀,
    `set` 写的是**边自己的事实**(写两端是扇入,和 `bad_output_name` 挡的那件事
    逐字同一种)。"""
    with _menpai_world(tmp_path, name="rule") as world:
        world.act("阿岚", "interact",
                  {"target": "menpai.sect:青云门", "verb": "入门"}, surface="body")
        world.tick(3)
        facts = world.scheduler.edge_store.get(
            "menpai.member_of", "agent:阿岚", "menpai.sect:青云门")
        assert facts is not None and facts["menpai.资历"] >= 3.0, facts


def test_契约报得出种类与动词怎么写():
    """**消费方问契约,别照设计稿抄** —— 设计稿说的是这套架构装得下什么。"""
    out = run_cli("contract", "--json")
    assert out.returncode == 0, out.stderr
    plugins = json.loads(out.stdout)["plugins"]
    assert plugins["kind_prefixes"] == ["entity:", "group:"]
    # 动词按 tool-calling 的 schema 声明(设计 §12.3)。
    assert plugins["verb_declaration"] == "tool-calling"
    assert "target" in plugins["verb_keys"]
    assert set(plugins["verb_effects"]) == {"link", "unlink", "transfer"}
    # `for_each` 认哪几种选择器 —— 第 2 期多了 `edge`。
    assert "edge" in plugins["rule_selectors"]


# ── 2b:投影式事实 `mode: "projected"`(设计稿 §9.3)────────────────────────
#
# 钱包与随身库存今天都是**事件重放折出来的**,不是量。搬它们要先回答一个问题:
# 一个直接写的事实**丢掉了"可重放"** —— 而「你为什么只剩三块钱」的唯一答案,
# 正是那一串 `payment` 事件。声明了 `mode:"projected"` 的事实,存储里那个值只是
# **物化视图**;真相是日志里那一串 `<插件>.<事实>.delta`。

PURSE = {
    "id": "purse", "version": "1.0.0", "label": "钱袋",
    "facts": {"钱": {"bearer": "actor", "shape": "number", "mode": "projected",
                     "default": 0.0, "visibility": "self", "label": "钱",
                     "bands": [[0, "身无分文"], [5, "还有几个子儿"]]}},
    "triggers": [{"id": "干活挣钱", "on": {"event": "entity_interaction"},
                  "effects": [{"set": {"purse.钱": "purse.钱 + 3"}}]}],
}
_TREE = {"id": "tree", "quantities": {"树高": {"default": 1.0, "visibility": "here"}},
         "affordances": {"照料": {"set": {"树高": "树高 + 1"}}}}


def _purse_world(tmp_path, name="purse", plugin=None, fresh=True):
    path = write_seed_file(tmp_path / f"{name}.cyberworld", {
        **BARE, "kinds": [dict(_TREE)],
        "entities": [{"id": "tree:小树", "name": "小树", "location": "cafe"}],
        "plugins": [dict(plugin or PURSE)],
    })
    return open_world_at(str(tmp_path / f"{name}.db"),
                         world_file=path if fresh else None, force_mock_llm=True)


def _earn(world, times=1):
    for _ in range(times):
        world.act("阿岚", "interact", {"target": "tree:小树", "verb": "照料"},
                  surface="body")
        world.tick(1)          # 触发器订的那批事件在下一 tick 才 drain


def test_投影式事实_落的是一条delta而不是一次直写(tmp_path):
    """**真相在日志里,存储里那个值只是视图。**

    判据不是"值对不对"(直接写也对),是**日志里有没有那几条 delta** ——
    没有它们的话,「你为什么只剩三块钱」这个问题在这个世界里没有答案。
    """
    with _purse_world(tmp_path) as world:
        _earn(world, 3)
        assert world.stocks("agent:阿岚")["purse.钱"] == 9.0
        deltas = [e for e in world.scheduler.event_log.replay()
                  if e.type == "purse.钱.delta"]
        assert len(deltas) == 3, f"日志里只有 {len(deltas)} 条 delta"
        assert [d.payload["delta"] for d in deltas] == [3.0, 3.0, 3.0]
        assert {d.payload["owner"] for d in deltas} == {"agent:阿岚"}
        # `cause` 说的是"哪一条规律/触发器让它变的" —— 没有它,一串 delta 只是
        # 一串数字,而这一格正是玩家屏上那句"你为什么只剩三块"的下半句。
        assert deltas[0].payload["cause"] == "purse.干活挣钱"


def test_把物化视图抹掉_重开一次它从日志里长回来(tmp_path):
    """🔴 **这条是这一期的牙。** 把重放那一半删掉,它当场变 0。

    直接写的事实做不到这件事:抹掉就是抹掉了。投影式事实抹掉的只是一份缓存。
    """
    with _purse_world(tmp_path, name="rebuild") as world:
        _earn(world, 3)
        assert world.stocks("agent:阿岚")["purse.钱"] == 9.0
        # 物化视图抹掉 —— 模拟"换了个进程 / 缓存坏了 / 有人手抖删了一行"
        world.scheduler.stock_store.set_many(
            "agent:阿岚", {"purse.钱": 0.0}, tick=int(world.scheduler.clock))
    with _purse_world(tmp_path, name="rebuild", fresh=False) as world:
        assert world.stocks("agent:阿岚")["purse.钱"] == 9.0, \
            "重开之后没从日志折回来 —— 那这个事实根本不是投影式的"


def test_forget_player之后_重放里不再有他那几条delta(tmp_path):
    """和 `_apply_player_departed` 折掉关系逐字同一条:**追加一条事实,
    折叠端认它** —— 直接删投影里那一行,下一次重放会原样把它折回来。"""
    owner, other = "agent:player:p1", "agent:player:p2"
    with _purse_world(tmp_path, name="forget") as world:
        for pid, amount in (("p1", 7.0), ("p2", 4.0)):
            world.player_move(pid, "cafe", display_name=pid)
            who = f"agent:player:{pid}"
            world.scheduler.record_fact_delta(who, "purse.钱", amount, cause="test")
            world.scheduler.stock_store.set_many(who, {"purse.钱": amount}, tick=1)
        assert world.stocks(owner)["purse.钱"] == 7.0
        world.forget_player("p1", reason="注销")
    with _purse_world(tmp_path, name="forget", fresh=False) as world:
        assert world.stocks(owner).get("purse.钱", 0.0) == 0.0, \
            "他走了,而他那几条 delta 还在往回折"
        # **只折掉他那一行** —— 走的是一个人,不是这一格。折成全局的下场是
        # 一次注销把所有人的钱包清零,而世界照跑、日志干净。
        assert world.stocks(other).get("purse.钱") == 4.0, \
            "别人的那一行跟着一起没了"
        # ⚠️ **历史一个字没删**:那几条 delta 原样躺在日志里 —— 走的是朝前看的那一半。
        assert len([e for e in world.scheduler.event_log.replay()
                    if e.type == "purse.钱.delta"]) == 2, "delta 被删掉了 —— 那是伪造历史"


def test_这一版只有number收得下projected_而且说得出为什么():
    """`state` 与 `text` 加不出来:一个枚举名或一句话没有"差值"这回事。
    **一句光秃秃的"不支持"会让作者以为自己写错了字。**"""
    for shape, spec in (("state", {"shape": "state", "mode": "projected",
                                   "values": [{"name": "甲"}, {"name": "乙"}]}),):
        with pytest.raises(PluginError) as raised:
            parse_plugins([{"id": "pj", "version": "1.0.0",
                            "facts": {"x": {"bearer": "agent", **spec}}}])
        assert any("projected" in e and "差" in e for e in raised.value.errors), \
            raised.value.errors


def test_契约报得出投影式事实这一格():
    out = run_cli("contract", "--json")
    assert out.returncode == 0, out.stderr
    plugins = json.loads(out.stdout)["plugins"]
    assert plugins["fact_modes"] == ["stored", "projected"]
    assert plugins["projected_shapes"] == ["number"]
    assert plugins["projected_delta_event"] == "<plugin>.<fact>.delta"


# ── 三视角验收的 P1(2026-08-26)────────────────────────────────────────────
#
# 七条,而它们有一个共同的形状:**说过的话和跑出来的事对不上,且对不上时不报错。**


def test_边连不上时_代价不许照收_而且回执要说出来(tmp_path):
    """🔴 **A/C 双复现的那一条**:`exclusive` 拦下 `link` 之后,`apply_edge_effect`
    答 `False`,而 `_apply_verb_edges` 把这个返回值扔了 —— 于是**代价照收、
    `ok: true`、边没建**。

    `apply_edge_effect` 的 docstring 自己写着「返回**这次到底动了没有**。⚠️ 它是
    承重的」—— 承重的东西被扔掉,而扔掉它的是同一轮写的另一个函数。

    修法照本仓既有的那条纪律:**拦在收费之前**(`spawn`/`destroys_target` 那句
    「收了钱再发现生不出来,她付的那一次在世界里什么也没换到」逐字同一条)。
    """
    with _menpai_world(tmp_path, name="gate") as world:
        first = world.act("阿岚", "interact",
                          {"target": "menpai.sect:青云门", "verb": "入门"},
                          surface="body")
        assert first["ok"] is True
        stamina = world.stocks("agent:阿岚")["体力"]

        # 第二次:`member_of` 是 exclusive 的,连不上。
        again = world.act("阿岚", "interact",
                          {"target": "menpai.sect:青云门", "verb": "入门"},
                          surface="body")
        assert again["ok"] is False, f"边没建成,却答了成功:{again}"
        # 拒绝那条路上 `act()` 只给一句人话(既有形状,不为这一件改冻结面);
        # **机器读的那一格在 `perform_affordance` 上**,和别的四类拒绝并列。
        assert "门籍" in again["error"], again
        assert world.scheduler.perform_affordance(
            "阿岚", "menpai.sect:青云门", "入门")["reason"] == "edge_blocked"
        assert world.stocks("agent:阿岚")["体力"] == stamina, \
            "被拒的那一趟收了代价 —— 她付的那一次在世界里什么也没换到"
        assert len(world.scheduler.edge_store.all("menpai.member_of")) == 1
        # 做成的那一趟,回执里说得出边做没做成 —— 一个"什么都没做"的 `link`
        # 和一个"建成了"的 `link` 在日志上长得一样,所以这一格是承重的。
        assert first["detail"]["edges"] == [
            {"op": "link", "type": "menpai.member_of", "ok": True}]


def test_插件伪造不了别家的投影(tmp_path):
    """🔴 **A 逮到的那条**:折叠端只看 `payload.fact`,不看**这条事件是谁发的** ——
    于是一个 `thief` 插件 emit 一条 `thief.伪造.delta{fact: "bank.存款"}` 就改得动
    别家的钱包。

    **它运行期不显形**(物化视图没动),**重开那一刻才长出来**,而且零报错 ——
    这正是本仓最怕的那种坏法:两份真相里有一份在别人手上。
    """
    from anima_world.projection import _apply_fact_delta
    from anima_world.types import Event, Projection

    proj = Projection()
    forged = Event(seq=1, ts=0, type="thief.伪造.delta", who="", loc="",
                   payload={"owner": "agent:阿岚", "fact": "bank.存款",
                            "delta": 999999.0, "cause": "偷"})
    _apply_fact_delta(proj, forged)
    assert proj.plugin_facts == {}, "别家的事实被一条伪造的 delta 折进去了"

    honest = Event(seq=2, ts=0, type="bank.存款.delta", who="", loc="",
                   payload={"owner": "agent:阿岚", "fact": "bank.存款",
                            "delta": 3.0, "cause": "存"})
    _apply_fact_delta(proj, honest)
    assert proj.plugin_facts == {"agent:阿岚": {"bank.存款": 3.0}}


def test_离线两扇门也编译插件的种类(tmp_path):
    """🔴 **B/C 双复现**:`validate world` / `world check` 不编译 `plugin.kinds`,
    于是一份**开得起来**的世界被它们答成「引用不到 kind」退 2。

    而 tool 把退 2 当红灯 —— **第一个照着 FOR-STUDIO 写插件的作者,会先看到一盏
    假红灯。** 本仓那条老纪律的反面:比开机严是**假红**,比它松是假绿,两种都比
    没有校验器更坏。
    """
    path = write_seed_file(tmp_path / "menpai.cyberworld", {
        **BARE, "kinds": [dict(_TIRED)],
        "entities": [{"id": "menpai.sect:青云门", "name": "青云门", "location": "cafe"}],
        "plugins": [dict(MENPAI)],
    })
    for argv in (("validate", "world", path), ("world", "check", path)):
        out = run_cli(*argv)
        assert out.returncode == 0, f"{argv} 退 {out.returncode}:{out.stdout}{out.stderr}"


def test_动词的裸串target写错字_当场拒(tmp_path):
    """🔴 **C 逮到的那条**:`target: "swrd"`(少了个 o)两扇门全绿、开机全绿,
    然后**静默长出一个空种类**,永远不会有实例 —— 而作者看到的是「我的动词点不动」。

    `_parse_verb` 只查带前缀的那一支,裸串那一支一个字都没查过。
    """
    from anima_world.world_seed import WorldSeedError

    bad = {**MENPAI, "verbs": {"磨": {"target": "swrd", "label": "磨"}}}
    with pytest.raises((WorldSeedError, Exception)) as raised:
        _menpai_world(tmp_path, name="typo", plugin=bad).__enter__()
    assert "swrd" in str(raised.value), raised.value


def test_动词target写agent_是一句人话不是一段栈(tmp_path):
    """🔴 **C:今天是 29 行 Python 栈,而末行怪的是 `kinds` 不是插件。**

    裁决 ①:`agent` **永不**进 `verb_target_forms` —— 对人的动词走工具路 + 同意门
    (第 3 期)。所以这里要的不是"以后支持",是**加载期一句说得清的中文**。
    """
    bad = {**MENPAI, "verbs": {"拜师": {"target": "agent", "label": "拜师"}}}
    with pytest.raises(PluginError) as raised:
        parse_plugins([bad])
    joined = "\n".join(raised.value.errors)
    assert "agent" in joined and "工具" in joined, joined


def test_契约里的edge_ends报全_别让tool拒掉跑得起来的世界():
    """🔴 **C 实跑**:`edge_ends` 只列四个裸词,而 `_parse_edge` 真收
    `entity:` / `group:` 前缀。**照契约判的 tool 会拒掉一个引擎跑得起来的世界。**"""
    out = run_cli("contract", "--json")
    assert out.returncode == 0, out.stderr
    plugins = json.loads(out.stdout)["plugins"]
    ends = plugins["edge_ends"]
    assert "entity:<kind>" in ends and "group:<kind>" in ends, ends
    # 🔴 **而 P1.6 那一版只治了一半**(复核评审):两个模板混进一列字面值,
    # 却没有一格说它们是模板 —— 照旧做等值比较的 tool **仍然会拒 `entity:sword`**。
    assert plugins["edge_end_prefixes"] == ["entity:", "group:"], plugins
    assert "占位符" in plugins["edge_ends_gloss"]
    # 判据敲得动:一个真写法要么命中前缀、要么命中字面值。
    for value in ("entity:sword", "agent"):
        assert (any(value.startswith(p) for p in plugins["edge_end_prefixes"])
                or value in ends), value


def test_契约说边规律收什么_它就真的收什么():
    """🔴 **这一条是 P1.7 那道闸的继任者,守的是同一件事、方向反过来。**

    P1.7 逮到的病是:契约里 `effects` 含 `emit`、`rule_selectors` 含 `edge`,
    **没有一格说这个组合不成立**,而 `_evaluate_edge_rules` 一条 emit 都不发 ——
    开机不拦、零 warning。当时的修法是**加载期拒 + 契约说 `["set"]`**。

    2e 给了它使用者(邀请的过期规律),于是收进来 —— 而**收进来是加法**。
    这条用例因此从"拒得对不对"改成**"契约说的和跑出来的是不是同一件事"**:
    契约列了几种,就得有几种真的跑得动。**一句说宽了的契约,和一盏假绿灯是同一件事。**
    """
    out = run_cli("contract", "--json")
    assert out.returncode == 0, out.stderr
    listed = json.loads(out.stdout)["plugins"]["edge_rule_effects"]
    assert listed == ["set", "emit"], listed
    # `set` 由 `test_边上的规律_读得到边自己也读得到两端` 钉着;
    # `emit` 由 `test_边规律发得出事件_而且是边沿触发` 钉着。
    # **这儿钉的是"这张表里没有第三个词"** —— 加一个词就得同轮加一条用例,
    # 而"忘了加"在这一格上的样子正是 P1.7 那种静默无效。
    assert set(listed) == {"set", "emit"}, "契约多了一种效果,而它有没有用例?"


# ── 三视角验收的 P2 ─────────────────────────────────────────────────────────


def test_边规律也进rule_stats_水位也过得了重开(tmp_path):
    """🔴 **A 逮到的那条**:边规律一格都不进 `rule_stats()`,而**三处注释拿
    "`rule_stats` 报 skipped" 当那件事的信号** —— 那个信号根本不存在。

    连带一件同源的:水位(`_persist_rule_marks`)只在 `_evaluate_world_rules` 里落,
    而那个函数在**只有边规律**的世界里第一句就 `return` —— 于是节流的边规律
    每次重开都多烧一轮。
    """
    slow = {**MENPAI, "rules": [{
        "id": "熬资历", "every": {"ticks": 5}, "for_each": {"edge": "member_of"},
        "set": {"menpai.资历": "edge.menpai.资历 + 1"}}]}
    with _menpai_world(tmp_path, name="stats", plugin=slow) as world:
        world.act("阿岚", "interact",
                  {"target": "menpai.sect:青云门", "verb": "入门"}, surface="body")
        world.tick(6)
        stats = world.rule_stats()
        assert stats["evaluated"] >= 1, f"边规律一格都没进仪表:{stats}"
        assert stats["written"] >= 1, stats
        seniority = world.scheduler.edge_store.get(
            "menpai.member_of", "agent:阿岚", "menpai.sect:青云门")["menpai.资历"]
    # 重开:**节流水位要跟着世界走**,不然每次重开都多烧一轮。
    with _menpai_world(tmp_path, name="stats", plugin=slow, fresh=False) as world:
        world.tick(1)
        again = world.scheduler.edge_store.get(
            "menpai.member_of", "agent:阿岚", "menpai.sect:青云门")["menpai.资历"]
        assert again == seniority, f"重开多烧了一轮:{seniority} → {again}"


def test_边进提示词印的是名字不是裸id(tmp_path):
    """🔴 **A 逮到的那条**:「你和 menpai.sect:青云门」—— 一个引擎的 id 漏进了
    她读到的那句话。这是本仓反复修的同一类 bug(`place_name` 那条 docstring)。"""
    from anima_world.perception import perceive

    with _menpai_world(tmp_path, name="name") as world:
        world.act("阿岚", "interact",
                  {"target": "menpai.sect:青云门", "verb": "入门"}, surface="body")
        block = perceive(agent_id="阿岚", here="cafe",
                         stock_store=world.scheduler.stock_store,
                         visibility=world.scheduler.visibility_store,
                         ontology=world.scheduler.ontology,
                         edges=world._edges_for("阿岚")).render()
    edge_line = next(l for l in block.split("\n") if l.startswith("- 你和"))
    assert edge_line.startswith("- 你和青云门:"), edge_line
    # ⚠️ **只查这一行**:上面那行「这里的青云门[menpai.sect:青云门]」里的 id 是
    # **有意的** —— 她要拿它去 `interact`。两件事别混成一条断言。
    assert "menpai.sect:" not in edge_line, f"裸 id 漏进这一行了:{edge_line}"


def test_plugin_list_报得出种类边和动词(tmp_path):
    """🔴 **C 逮到的那条**:`plugin list` 对 kinds / edges / verbs **一字不报** ——
    「挂在 」后面是一片空白(这个插件的事实一个都不挂在节点上,全在边上)。

    连带给 `Verb.schema()` 接上第一个**生产路径的消费者**:它从前只有测试在读,
    而一份没人读的 schema 和一份没有的 schema,坏起来长得一模一样。
    """
    with _menpai_world(tmp_path, name="pl"):
        pass
    out = run_cli("plugin", "list", "--world-id", "w", "--json")
    assert out.returncode == 0, out.stderr
    row = _authored_rows(json.loads(out.stdout)["plugins"])[0]
    assert row["kinds"] == ["menpai.sect"], row
    assert row["edges"] == ["menpai.member_of"], row
    assert sorted(v["name"] for v in row["verbs"]) == ["menpai.入门", "menpai.拆了它",
                                                       "menpai.退出"]
    entry = next(v for v in row["verbs"] if v["name"] == "menpai.入门")
    assert entry["parameters"]["required"] == ["target"]
    assert entry["description"] == "递上名帖,拜入这个门派"
    # 人眼那一份也不许再印一个光秃秃的「挂在 」。
    human = run_cli("plugin", "list", "--world-id", "w")
    assert human.returncode == 0, human.stderr
    assert "挂在 \n" not in human.stdout and "种类 1" in human.stdout, human.stdout
    # 数得出「边 1」而印不出它叫什么,等于让人再去问一次(复核评审)。
    assert "边:menpai.member_of" in human.stdout, human.stdout


def test_只连边的动词_不许印成只是看看(tmp_path):
    """🔴 **C 逮到的那条**:一个只 `link` 的动词 `changes_world: false`,
    而 `ontology --json` 那条路把它印成「只是看看」——**它明明改了世界**。

    病根是 `changes_world` 住在本体那一层,而边效果**有意**不住在那儿
    (本体层不该认识插件的边)。所以补在**知道这件事的那一层**:`World.kinds()`。
    """
    with _menpai_world(tmp_path, name="cw") as world:
        row = next(k for k in world.kinds() if k["id"] == "menpai.sect")
        join = next(a for a in row["affordances"] if a["verb"] == "入门")
        assert join["changes_world"] is True, join
        assert join["edges"] == [{"op": "link", "type": "menpai.member_of"}], join
        assert join["description"] == "递上名帖,拜入这个门派", join


# ── ④ 投影式事实的 `sources`:把既有的内核事件认成自己的 delta ────────────
#
# 🔴 **这一格是 2d 真正的拦路石**(裁决 ④):折叠端只认 `.delta` 后缀,而设计 §9.3
# 说的是「`payment` 事件照旧是 `economy.coins` 的 delta」—— **这两句今天对不上**。
# 三条路里两条是坏的:改发 `economy.coins.delta`(`payment` 在白名单上,改名 =
# 破坏消费方)· 两条都发(同一笔钱记两遍账)。**只有 `sources` 不破坏消费方。**

WALLET = {
    "id": "wallet", "version": "1.0.0", "label": "钱包",
    "facts": {"钱": {
        "bearer": "actor", "shape": "number", "mode": "projected",
        "default": 0.0, "visibility": "self", "label": "钱",
        # `payment` 是**内核**事件,而且在那张白名单上 —— 一笔钱从 `from` 挪到 `to`。
        "sources": [{"event": "payment", "amount": "amount",
                     "debit": "from", "credit": "to"}]}},
}


def test_既有的内核事件认得成自己的delta(tmp_path):
    """一笔 `payment` 折进 `wallet.钱`:**付钱那头减、收钱那头加**,一条事件两端。"""
    with _purse_world(tmp_path, name="src", plugin=WALLET) as world:
        world.player_move("p1", "cafe", display_name="阿檀")
        world.player_topup("p1", 40.0)
        world.scheduler.catch_up_projection()
        assert world.scheduler._memory_projection.plugin_facts.get(
            "agent:player:p1", {}).get("wallet.钱") == 40.0
        # 物化视图也跟着走 —— 她的表达式与感知读的是量表,不是投影。
        assert world.stocks("agent:player:p1")["wallet.钱"] == 40.0
    # 重开:**从日志折回来**,而这一趟折的是 `payment`,不是 `.delta`。
    with _purse_world(tmp_path, name="src", plugin=WALLET, fresh=False) as world:
        assert world.stocks("agent:player:p1")["wallet.钱"] == 40.0


def test_sources只认内核白名单上的事件_认不了别家插件的(tmp_path):
    """🔴 **和「插件伪造不了别家的投影」是同一道边界的两面。**

    `sources` 是一张作者写的表,如果它认得了 `<别家>.<事实>.delta`,那么上一轮
    刚关上的那扇门就从这儿又开了 —— 只是这次是**声明式**地开。
    所以这一格只收 `contract.plugins.subscribable_events` 上那几种**内核**事件。
    """
    bad = {"id": "thief", "version": "1.0.0", "facts": {"赃": {
        "bearer": "actor", "shape": "number", "mode": "projected",
        "sources": [{"event": "bank.存款.delta", "amount": "delta",
                     "credit": "owner"}]}}}
    with pytest.raises(PluginError) as raised:
        parse_plugins([bad])
    joined = "\n".join(raised.value.errors)
    assert "内核" in joined and "subscribable_events" in joined, joined


def test_sources只给projected的事实():
    """一个直接写的事实认一条事件当自己的 delta,是**两个写者**写同一个数 ——
    而两份真相里有一份不更新,是这个仓库最怕的坏法。"""
    bad = {"id": "w2", "version": "1.0.0", "facts": {"钱": {
        "bearer": "actor", "shape": "number",
        "sources": [{"event": "payment", "credit": "to"}]}}}
    with pytest.raises(PluginError) as raised:
        parse_plugins([bad])
    assert any("projected" in e for e in raised.value.errors), raised.value.errors


def test_契约报得出sources这一格():
    out = run_cli("contract", "--json")
    assert out.returncode == 0, out.stderr
    plugins = json.loads(out.stdout)["plugins"]
    assert plugins["projected_source_keys"] == [
        "event", "amount", "credit", "debit", "owner_form", "round"]
    assert plugins["projected_source_events"] == "subscribable_events"


# ── 2d-①:钱包搬成出厂插件的那一格 ────────────────────────────────────────


#: 三笔**真的会分家**的钱。挑它们花了一次穷举 —— 而这正是上一版那条单测缺的东西:
#: `0.1 + 0.2` 在 round 2 和 round 6 下答案相同,于是那条闸把 `round` 改成 6 也照绿。
#: **一个不会分家的数,验不出"两处会不会分家"。**
#:
#:   逐笔先折再累加:round(1.005,2)=1.00,+round(0.055,2)=0.06 ⇒ **1.06**
#:   只折累加结果  :round(0+1.005,2)=1.00,round(1.00+0.055,2) ⇒ **1.05**
#: ⚠️ **一串长一点的钱反而可能又对上**(四笔那一串实测两边都是 1.19)——
#: 所以这儿写死的是穷举出来**确实分家**的那一对,别顺手往里加数。
_DIVERGING_AMOUNTS = (1.005, 0.055)


def test_钱包和账本折出同一个数_逐笔比(tmp_path):
    """🔴 **两处进位不一样就是两个钱包**(2026-08-27 复核评审实测:`balance()=63.13`
    而量表 `63.12`)。

    病根是**折的位置**,不是位数:`_apply_payment` 只折**累加结果**,而
    `fact_source_updates` 第一版把**每一笔 delta 先折了一次**。三笔
    `1.005 / 0.125 / 2.0005` 就分家 —— 而它们分家的那一位,正是"一笔正好够的交易
    会不会被门禁拒掉"那一位。

    ⚠️ **上一轮我给这一格写的那条单测是假绿的**(评审逮的):它取 `0.1 + 0.2`,
    而那个数在 round 2 和 round 6 下答案相同 —— **把 `round` 改成 6,全仓照样全绿**。
    所以这一条改成**拿两份真的折叠端逐笔对**:账本折一遍、插件折一遍,必须同一个数。
    **一个不会分家的数,验不出"两处会不会分家"。**
    """
    from anima_world.projection import _apply_fact_delta, _apply_payment
    from anima_world.projection import fact_source_updates
    from anima_world.types import Event, Projection

    specs = [{"event": "payment", "amount": "amount", "credit": "to",
              "fact": "w.钱", "owner_form": "actor", "round": 2}]
    ledger, wallet = Projection(), {}
    for seq, amount in enumerate(_DIVERGING_AMOUNTS, start=1):
        payload = {"from": "__town__", "to": "夏", "amount": amount}
        _apply_payment(ledger, Event(seq=seq, ts=0, type="payment", who="",
                                     loc="", payload=payload))
        for owner, fact, delta in fact_source_updates(specs, payload):
            wallet[owner] = round(wallet.get(owner, 0.0) + delta, 2)
    assert wallet["agent:夏"] == ledger.balances["夏"], (
        f"两个钱包:插件 {wallet['agent:夏']} ≠ 账本 {ledger.balances['夏']}"
    )
    # 顺带钉死"位数是声明出来的":同一串钱折到 6 位,和账本**必然**分家 ——
    # 这一条就是上一版那条单测缺的那颗牙。
    six = [{**specs[0], "round": 6}]
    loose = {}
    for amount in _DIVERGING_AMOUNTS:
        for owner, fact, delta in fact_source_updates(
                six, {"from": "__town__", "to": "夏", "amount": amount}):
            loose[owner] = round(loose.get(owner, 0.0) + delta, 6)
    assert loose["agent:夏"] != ledger.balances["夏"], (
        "折到 6 位竟然和账本一样 —— 那这条用例又挑了一串不会分家的数"
    )


def test_出厂钱包插件装上了_而且只多一个键(tmp_path):
    """**这一格是加法**:量表 hash 里多一个 `economy.coins`,和第 1 期 needs
    那三个键逐字同构。`plugin list` 报得出它,契约那张出厂表也报得出。"""
    with _purse_world(tmp_path, name="wal", plugin=QI) as world:
        world.config_set("economy.enabled", True)
        ids = [p.id for p in world.scheduler.plugins]
        assert "economy" in ids, ids
        fact = next(p for p in world.scheduler.plugins if p.id == "economy").facts["coins"]
        assert fact.projected and fact.bearer == "actor"
        assert fact.sources and fact.sources[0]["event"] == "payment"


def test_出厂表报得出钱包这一格():
    out = run_cli("contract", "--json")
    assert out.returncode == 0, out.stderr
    plugins = json.loads(out.stdout)["plugins"]
    assert plugins["factory"]["economy"] == "economy.enabled", plugins["factory"]
    assert plugins["factory"]["needs"] == "needs.enabled"
    # 🔴 **这一格说的是"搬了哪几格",不是"搬完几个系统"**(补裁 ⑤b:
    # 按系统计数正是把人推向换皮的那把尺子)。
    assert "货架" in plugins["factory_scope"]["economy"], plugins["factory_scope"]


def test_没入门就退出_同样拦在收费之前(tmp_path):
    """🔴 **复核评审逮的那半截**:`_verb_edge_gate` 只预检了 `op == "link"`。

    实测「没入门就退出」:体力 100→99、`ok: true`、边一条没断,只有回执里一条
    `ok: false` 和一行 warning。**而「拦在收费之前」这句话对 `unlink` 一样查得动**
    (`store.of_src` 是空的)—— 留半截的下场是同一件事在两个动词上两种下场,
    而作者读不出为什么。
    """
    with _menpai_world(tmp_path, name="halfgate") as world:
        before = world.stocks("agent:阿岚")["体力"]
        out = world.act("阿岚", "interact",
                        {"target": "menpai.sect:青云门", "verb": "退出"},
                        surface="body")
        assert out["ok"] is False, f"什么都没断,却答了成功:{out}"
        assert "门籍" in out["error"], out
        assert world.stocks("agent:阿岚")["体力"] == before, \
            "被拒的那一趟收了代价"
        assert world.scheduler.perform_affordance(
            "阿岚", "menpai.sect:青云门", "退出")["reason"] == "edge_blocked"


# ── 2e:边规律的 `emit`(现在有使用者了 —— 邀请的过期规律)────────────────


MENPAI_EMIT = {
    **MENPAI,
    "rules": [{
        "id": "熬出头", "every": {"ticks": 1}, "for_each": {"edge": "member_of"},
        "set": {"menpai.资历": "edge.menpai.资历 + 1"},
        "emit": [{"type": "menpai.出师", "when": "edge.menpai.资历 >= 2",
                  "payload": {"因为": "熬够了"}}]}],
}


def test_边规律发得出事件_而且是边沿触发(tmp_path):
    """🔴 **P1.7 那道加载期拒绝这一轮改成放行**,因为它有使用者了(邀请的过期规律)。

    **边沿触发照抄节点那一层**:门槛被跨过去那一下才发 —— 没有这一条,一条熬够了
    的边会每 tick 发一次"我出师了",直到世界末日。
    """
    with _menpai_world(tmp_path, name="emit", plugin=MENPAI_EMIT) as world:
        world.act("阿岚", "interact",
                  {"target": "menpai.sect:青云门", "verb": "入门"}, surface="body")
        world.tick(6)
        fired = [e for e in world.scheduler.event_log.replay()
                 if e.type == "menpai.出师"]
        assert len(fired) == 1, f"边沿触发没生效,发了 {len(fired)} 次"
        assert fired[0].payload["因为"] == "熬够了"
        assert fired[0].payload["rule"] == "menpai.熬出头"
        assert fired[0].payload["edge"] == "rise"
        # 两端也要说得出 —— 一条边上的事件,不写清是哪两个人之间的,读的人查不下去。
        assert fired[0].payload["src"] == "agent:阿岚"
        assert fired[0].payload["dst"] == "menpai.sect:青云门"


def test_没人订的边规律事件_进日志而不是被丢掉(tmp_path):
    """**"没订户"和"被丢掉"是两件事,而这一格必须说清。**

    它走 `_emit_rule_event` → 落库,和节点规律那一层逐字同一条路 ——
    **发生了就是进了日志**,有没有触发器订它是另一回事。
    丢掉的话,「这条规律到底跑没跑」就再也答不出来。
    """
    with _menpai_world(tmp_path, name="noear", plugin=MENPAI_EMIT) as world:
        assert world.scheduler._triggers_by_event == {}, "这个世界不该有触发器"
        world.act("阿岚", "interact",
                  {"target": "menpai.sect:青云门", "verb": "入门"}, surface="body")
        world.tick(6)
        assert [e for e in world.scheduler.event_log.replay()
                if e.type == "menpai.出师"], "没人订就被丢掉了"


def test_契约说边规律现在收set和emit():
    out = run_cli("contract", "--json")
    assert out.returncode == 0, out.stderr
    plugins = json.loads(out.stdout)["plugins"]
    assert plugins["edge_rule_effects"] == ["set", "emit"], plugins["edge_rule_effects"]


MENPAI_TRANSFER = {
    **MENPAI,
    "verbs": {**MENPAI["verbs"], "过继": {
        "target": "group:sect", "label": "把门籍过到自己名下",
        "costs": {"体力": "me_体力 - 2"},
        "effects": [{"transfer": {"type": "member_of", "from": "self",
                                  "to": "target", "by_dst": True}}]}},
}


def test_transfer动词真的转得动_而不是被自己的预检拦死(tmp_path):
    """🔴 **预检把判据写反了,于是 `transfer` 从此一次也成功不了**(复核评审实测)。

    `apply_edge_effect` 判「有没有可转的边」用的是 `of_dst(dst) if by_dst else
    of_src(src)`;而预检问的是 `get(type, src, dst)` —— **那是转移之后才会有的
    那条边**。两个相反的条件,于是闸说"没有"、执行说"有",而闸先说话。

    ⚠️ 它比 P1.1 那次更难查:那次是"该拒没拒",这次是**"该成的永远成不了"**,
    而回执里那句话("你身上没有这条门籍")听起来完全合理。
    """
    with _menpai_world(tmp_path, name="tr", plugin=MENPAI_TRANSFER) as world:
        sch = world.scheduler
        sch.apply_edge_effect({"op": "link", "type": "menpai.member_of",
                               "from": "agent:小竹", "to": "menpai.sect:青云门"}, {})
        before = world.stocks("agent:阿岚")["体力"]
        out = world.act("阿岚", "interact",
                        {"target": "menpai.sect:青云门", "verb": "过继"},
                        surface="body")
        assert out["ok"] is True, f"转得动的一次被预检拦死了:{out}"
        rows = [(s, d) for s, d, _f in sch.edge_store.all("menpai.member_of")]
        assert rows == [("agent:阿岚", "menpai.sect:青云门")], rows
        assert world.stocks("agent:阿岚")["体力"] == before - 2
        assert out["detail"]["edges"] == [
            {"op": "transfer", "type": "menpai.member_of", "ok": True}]


def test_没边可转时_transfer也拦在收费之前(tmp_path):
    """反面那一半:真的没得转,就得拒 —— 而且**照旧拦在收费之前**。"""
    with _menpai_world(tmp_path, name="tr2", plugin=MENPAI_TRANSFER) as world:
        before = world.stocks("agent:阿岚")["体力"]
        out = world.act("阿岚", "interact",
                        {"target": "menpai.sect:青云门", "verb": "过继"},
                        surface="body")
        assert out["ok"] is False, out
        assert world.stocks("agent:阿岚")["体力"] == before, "被拒的那一趟收了代价"


def test_运行中打开经济_钱包当场就对上_不必等重开(tmp_path):
    """🔴 **「重开才对上」正是 2b 里我自己防过的那种形状**(复核评审逮的)。

    `config_set` 那道热更新钩子走 `refresh_plugins`,它把插件重装了、把
    `fact_sources` 注册表也换了 —— **但没有把物化视图重建一遍**。
    于是运行中打开经济之后,量表里那一格停在 0,而账本早就有数;
    重开一次它才长出来。**跑着的世界一个数、重开之后另一个数**,零报错。
    """
    with _purse_world(tmp_path, name="hot", plugin=QI) as world:
        world.config_set("economy.enabled", False)
        world.player_move("p1", "cafe", display_name="阿檀")
        world.player_topup("p1", 40.0)
        owner = "agent:player:p1"
        assert world.stocks(owner).get("economy.coins") is None, "还没装就有了?"
        world.config_set("economy.enabled", True)
        assert world.stocks(owner).get("economy.coins") == 40.0, (
            "运行中打开经济,量表停在 0 —— 要重开一次才对上"
        )
        assert world.balance("player:p1") == 40.0


def test_出厂表那两张的键集必须一样():
    """🔴 **`FACTORY_SCOPE` 是 `FACTORY_PLUGINS` 的第二份键集,而第二份键集会烂**
    (契约链评审逮的)。

    烂了的样子是安静的:加一个出厂插件却忘了写它搬了哪几格,
    `contract --json` 那一格就少一行 —— 而**消费方读的正是那一格**
    (「别按『搬完几个系统』计数,按『哪几格搬了』计」)。
    少一行不会有任何一处报错,只是 tool 那边少知道一件事。

    **能点名就别数数**:这儿点的是两张表的键集,不是"有几个"。
    """
    from anima_world.__main__ import FACTORY_PLUGINS, FACTORY_SCOPE

    assert set(FACTORY_PLUGINS) == set(FACTORY_SCOPE), (
        f"两张出厂表对不上:只在 FACTORY_PLUGINS 里的 "
        f"{sorted(set(FACTORY_PLUGINS) - set(FACTORY_SCOPE))}、"
        f"只在 FACTORY_SCOPE 里的 {sorted(set(FACTORY_SCOPE) - set(FACTORY_PLUGINS))}"
    )
    # 每一格都得真说了点什么 —— 一句空话和缺一行一样没用。
    for plugin_id, scope in FACTORY_SCOPE.items():
        assert len(scope) > 10, f"{plugin_id} 那一格没说清它搬了哪几格:{scope!r}"
    # 契约那两格报的就是这两张表(消费方读的是契约,不是这两个常量)。
    out = run_cli("contract", "--json")
    assert out.returncode == 0, out.stderr
    plugins = json.loads(out.stdout)["plugins"]
    assert plugins["factory"] == dict(FACTORY_PLUGINS)
    assert plugins["factory_scope"] == dict(FACTORY_SCOPE)


def test_出厂钱包声明的进位就是账本的进位(tmp_path):
    """🔴 **上一条闸验的是「折叠机制认不认声明」,不是「出厂那一格声明的是几」。**

    它在自己的 specs 里写死了 `round: 2` —— 于是把 `economy.py` 里那份**真声明**
    改成 6,全仓照样绿(复核评审实测:86 passed,而真世界 `stocks=63.1305`
    对 `balance()=63.13`)。**一个自己造判据的闸,验的是它自己。**

    所以这一条**不造 specs**:直接拿 `economy.factory_plugin()` 那份真声明去折,
    和账本逐笔对。改那一格的位数,这里当场红。
    """
    from anima_world.economy import factory_plugin
    from anima_world.projection import _apply_payment, fact_source_updates
    from anima_world.types import Event, Projection

    coins = parse_plugins([factory_plugin()],
                          subscribable=("payment",))[0].facts["coins"]
    # 注册表那一行**照 `_install_plugins` 拼**:各拼一遍的话,这条闸验的又是
    # 它自己拼出来的那份,而不是真的跑起来的那份。
    specs = [{**spec, "fact": coins.qualified} for spec in coins.sources]

    ledger, wallet = Projection(), {}
    for seq, amount in enumerate(_DIVERGING_AMOUNTS, start=1):
        payload = {"from": "__town__", "to": "夏", "amount": amount}
        _apply_payment(ledger, Event(seq=seq, ts=0, type="payment", who="",
                                     loc="", payload=payload))
        for owner, fact, delta in fact_source_updates(specs, payload):
            digits = int(specs[0]["round"])
            wallet[owner] = round(wallet.get(owner, 0.0) + delta, digits)
    assert wallet["agent:夏"] == ledger.balances["夏"], (
        f"**出厂那一格声明的位数和账本对不上**:插件 {wallet['agent:夏']} ≠ "
        f"账本 {ledger.balances['夏']}(声明的是 round={specs[0]['round']})"
    )
    # 并把那个数本身钉死,连同**为什么是 2**:账本 `_apply_payment` 每一步折两位
    # (二进制浮点存不下 0.1),而**门禁读的是这个数** —— 一笔"正好够"的交易
    # 迟早会被它拒掉,而那一次不报错也不留痕。
    assert specs[0]["round"] == 2, (
        "出厂钱包的进位不是两位了 —— 钱有最小单位,而账本折的就是两位;"
        "两处不一样就是两个钱包,它们只在小数第三位往后分家"
    )


# ── 契约说的和引擎做的对不上,三处收口(2026-08-27,创作台接第 2 期时测出来的)──
#
# 三条都是同一个形状:**契约里写着不收,而 `parse_plugins` 照收**。
# 下场比"不支持"更坏 —— 作者写下的那一格**根本不在**,而退出码 0、日志干净。
# 创作台因此两头都没法判:照契约判是假红,照行为判是帮着引擎丢东西。
# **把两句话变成一句:引擎照契约拒。**


def test_挂在插件种类上的事实_做不了projected_顶层写法也拒():
    """🔴 **这一格 2b 就写明做不了,而闸只拦住了写在 `kinds` 里那一种。**

    顶层 `facts` 里写 `bearer: "entity:<种类>"` + `projected` **照收** ——
    而理由一个字没变:那样东西会被 `destroy` 抹掉,一串折向不存在的主人的 delta,
    重放出来是**一个没有主人的数**。契约那一格(`projected_bearers`)里从来没有
    `entity:` / `group:`,所以这是"契约说的和引擎做的对不上",不是一条新规矩。
    """
    with pytest.raises(PluginError) as raised:
        parse_plugins([{"id": "forge", "version": "1.0.0",
                        "kinds": {"entity:sword": {"facts": {}}},
                        "facts": {"锋利": {
                            "bearer": "entity:forge.sword", "shape": "number",
                            "mode": "projected",
                            "sources": [{"event": "payment", "credit": "to"}]}}}],
                      subscribable=("payment",))
    joined = "\n".join(raised.value.errors)
    assert "projected" in joined and "destroy" in joined, joined


def test_动词上多写一个键_当场拒而不是静默丢():
    """🔴 **和「target 写错字静默长出空种类」同族**(§2a 那条)。

    `verb_keys` 报十五个键,而多写的键从前**照收然后丢掉** —— 作者写的那一格
    根本不在,退出码 0、日志干净。**一个被静默丢掉的声明,比一条报错难查得多。**
    对照组就在同一份文件里:`sources` 那一层早就逐键查了不认识的键。
    """
    with pytest.raises(PluginError) as raised:
        parse_plugins([{"id": "forge", "version": "1.0.0",
                        "kinds": {"entity:sword": {"facts": {}}},
                        "verbs": {"磨": {"target": "entity:sword", "label": "磨",
                                         "cooldown": {"ticks": 5}}}}])
    joined = "\n".join(raised.value.errors)
    assert "cooldown" in joined and "verb_keys" in joined, joined


def test_插件规律发的事件_也得是自己的命名空间():
    """**触发器那一层早就查了、会拒;规律这一层从前不查** —— 同一个插件里
    两种写法两种下场,而作者读不出为什么。

    ⚠️ **这是一次收紧,而它成立的理由是量出来的**:第 1 期到现在,
    四个仓库里**一条 `plugin` 记录都没有**(`grep -rl '"type": "plugin"'` 答空),
    `3.8.0` 没打 tag、PyPI 停在 `3.7.0`、线上跑的镜像是 `anima-world:3.7.0`,
    出厂那三个插件里唯一发事件的写的就是全名。**消费方为零,所以现在收最便宜。**
    """
    with pytest.raises(PluginError) as raised:
        parse_plugins([{"id": "qi", "version": "1.0.0",
                        "facts": {"灵力": {"bearer": "agent", "shape": "number"}},
                        "rules": [{"id": "回气", "for_each": {"kind": "agent"},
                                   "set": {"qi.灵力": "qi.灵力 + 1"},
                                   "emit": [{"type": "耗尽", "when": "qi.灵力 > 5"}]}]}])
    joined = "\n".join(raised.value.errors)
    assert "耗尽" in joined and "qi." in joined, joined
    # 写全名的照收 —— 出厂那个 `invitation.expired` 走的就是这条。
    ok = parse_plugins([{"id": "qi", "version": "1.0.0",
                         "facts": {"灵力": {"bearer": "agent", "shape": "number"}},
                         "rules": [{"id": "回气", "for_each": {"kind": "agent"},
                                    "set": {"qi.灵力": "qi.灵力 + 1"},
                                    "emit": [{"type": "qi.耗尽",
                                              "when": "qi.灵力 > 5"}]}]}])
    assert ok[0].rules[0].emits[0].type == "qi.耗尽"


def test_插件规律里多写一个键_当场拒而不是静默丢():
    """🔴 **最后一个静默住户**(2026-08-27,创作台换钉 `a3b5fca` 重量出来的)。

    它报的是「边规律里写 `link`」,而实测**比那更宽**:`link` / `effects` /
    `cooldown` —— **随便什么不认识的键,插件的规律都照收然后丢掉**。
    和刚收掉的那三格同种,也和同一份文件里 `sources` / `verbs` 两层对不上脾气。

    ⚠️ **只收紧插件那一支**(`namespace` 不是 None)。作者层的 `rules` 是一个
    早就发出去的面 —— 线上那两个世界我够不着,而"没量过就别收"是这一单一路的口径。
    """
    from anima_world.rules import RuleError, parse_rules

    body = {"id": "qi", "version": "1.0.0",
            "facts": {"灵力": {"bearer": "agent", "shape": "number"}},
            "edges": {"bond": {"from": "agent", "to": "agent",
                               "facts": {"热": {"shape": "number"}}}}}
    for extra in ({"link": {"type": "bond"}}, {"effects": [{"link": {}}]},
                  {"cooldown": 5}):
        with pytest.raises(PluginError) as raised:
            parse_plugins([{**body, "rules": [
                {"id": "r", "for_each": {"kind": "agent"},
                 "set": {"qi.灵力": "qi.灵力 + 1"}, **extra}]}])
        joined = "\n".join(raised.value.errors)
        assert next(iter(extra)) in joined and "rule_keys" in joined, joined
    # **作者层那一支一个字没动** —— 同样多写一个键,照旧收下(没量过就别收)。
    assert parse_rules([{"id": "r", "for_each": {"kind": "agent"},
                         "set": {"高": "高 + 1"}, "cooldown": 5}])


def test_契约把六个盲区报出来_让创作台判得了():
    """创作台诉求第六条:**引擎开机拒、而契约不报**的那几格 —— 它判不了,
    只能眼看作者出包之后被引擎打回。

    六格实测**引擎全都已经拒了**,所以这不是"让引擎更严",是**把已经在做的事
    说出来**:纯增量,一格取值都不改。形状照 `id_pattern` 那条先例(给名字一格正则)。
    """
    from anima_world.plugins import (
        EMIT_KEYS, KIND_LOCAL_PATTERN, RULE_EVERY_KEYS, RULE_KEYS, TRIGGER_KEYS,
    )

    out = run_cli("contract", "--json")
    assert out.returncode == 0, out.stderr
    p = json.loads(out.stdout)["plugins"]
    assert p["version_required"] is True
    assert p["rule_keys"] == list(RULE_KEYS)
    assert p["rule_every_keys"] == list(RULE_EVERY_KEYS)
    assert p["emit_keys"] == list(EMIT_KEYS)
    assert p["emit_required_keys"] == ["type", "when"]
    assert p["trigger_keys"] == list(TRIGGER_KEYS)
    assert p["trigger_required_keys"] == ["id", "on", "effects"]
    assert p["kind_local_pattern"] == KIND_LOCAL_PATTERN
    # 报出来的正则得**真的**是引擎在用的那一条 —— 拿它跑一遍引擎拒过的那两个名字。
    import re

    assert re.match(p["kind_local_pattern"], "sword")
    assert not re.match(p["kind_local_pattern"], "my sword")
    assert not re.match(p["kind_local_pattern"], "")


# ── 收尾全扫:`plugin` 记录**每一个层级**都不许静默丢键(2026-08-27)────────
#
# 前几轮是一层一层收的(事实的 `sources` → 动词 → 规律 / `emit` / 触发器),
# 而创作台每换一次钉就量出新的一层。**一层一层来本身就是那个 bug 的形状** ——
# 所以这一轮把每一层一次过完,并把每层的键名单都进契约。


_SWEEP = {
    "顶层": {"id": "qi", "version": "1.0.0", "怪键": 1},
    "事实": {"id": "qi", "version": "1.0.0",
             "facts": {"灵": {"bearer": "agent", "shape": "number", "怪键": 1}}},
    "边": {"id": "qi", "version": "1.0.0",
           "edges": {"b": {"from": "agent", "to": "agent", "怪键": 1}}},
    "边上的事实": {"id": "qi", "version": "1.0.0",
                   "edges": {"b": {"from": "agent", "to": "agent",
                                   "facts": {"热": {"shape": "number", "怪键": 1}}}}},
    "种类": {"id": "qi", "version": "1.0.0",
             "kinds": {"entity:sword": {"gloss": "剑", "怪键": 1}}},
    "种类上的事实": {"id": "qi", "version": "1.0.0",
                     "kinds": {"entity:sword": {
                         "facts": {"锋": {"shape": "number", "怪键": 1}}}}},
    "触发器的 emit": {
        "id": "qi", "version": "1.0.0",
        "facts": {"灵": {"bearer": "agent", "shape": "number"}},
        "triggers": [{"id": "t", "on": {"event": "conversation"},
                      "effects": [{"emit": {"type": "qi.x", "怪键": 1}}]}]},
    "边效果体": {
        "id": "qi", "version": "1.0.0",
        "facts": {"灵": {"bearer": "agent", "shape": "number"}},
        "edges": {"b": {"from": "agent", "to": "agent"}},
        "triggers": [{"id": "t", "on": {"event": "conversation"},
                      "effects": [{"link": {"type": "b", "from": "self",
                                            "to": "agent:x", "怪键": 1}}]}]},
}


@pytest.mark.parametrize("level", sorted(_SWEEP))
def test_每一层多写一个键都当场拒(level):
    """🔴 **一层一层收本身就是这个 bug 的形状** —— 所以这一条把每层都扫一遍。

    ⚠️ **加一层就往 `_SWEEP` 里加一格**:漏一层的下场是安静的
    (作者写下的那一格根本不在,而退出码 0、日志干净)。
    """
    with pytest.raises(PluginError) as raised:
        parse_plugins([_SWEEP[level]], subscribable=("conversation",))
    joined = "\n".join(raised.value.errors)
    assert "怪键" in joined, f"{level} 这一层把不认识的键静默吞了:{joined}"


def test_触发器的emit和规律的emit_是两份键名单():
    """🔴 **别把这两层合成一格**(创作台量到的那条,它已用 `strict_keys` 分开):
    规律的 `emit` 有 `when` / `on` / `importance`(门槛与边沿是规律那一层的概念),
    触发器的 `emit` 只有 `type` / `payload` / `text`(它已经"因一件事而发"了)。
    **合成一格,创作台那边就是假红。**
    """
    from anima_world.plugins import EMIT_KEYS, TRIGGER_EMIT_KEYS

    assert set(TRIGGER_EMIT_KEYS) < set(EMIT_KEYS), "两份名单的关系变了"
    assert "when" in EMIT_KEYS and "when" not in TRIGGER_EMIT_KEYS
    # 触发器里写 `when` 会被当成不认识的键拒 —— 那正是两份名单分开的意思。
    with pytest.raises(PluginError) as raised:
        parse_plugins([{"id": "qi", "version": "1.0.0",
                        "facts": {"灵": {"bearer": "agent", "shape": "number"}},
                        "triggers": [{"id": "t", "on": {"event": "conversation"},
                                      "effects": [{"emit": {"type": "qi.x",
                                                            "when": "1 > 0"}}]}]}],
                      subscribable=("conversation",))
    assert "when" in "\n".join(raised.value.errors)


def test_契约把每一层的键名单都报出来_而且不许漂():
    """**每层一格,和引擎读同一份常量** —— 抄第二遍就是「契约说六个、引擎认七个」
    那种漂移的来路(`rule_keys` 那一轮的先例)。"""
    from anima_world import plugins as P

    out = run_cli("contract", "--json")
    assert out.returncode == 0, out.stderr
    c = json.loads(out.stdout)["plugins"]
    for grid, const in (
        ("plugin_keys", P.PLUGIN_KEYS), ("fact_keys", P.FACT_KEYS),
        ("edge_keys", P.EDGE_KEYS), ("kind_keys", P.PLUGIN_KIND_KEYS),
        ("trigger_emit_keys", P.TRIGGER_EMIT_KEYS),
        ("edge_effect_keys", P.EDGE_EFFECT_KEYS),
        ("rule_required_keys", P.RULE_REQUIRED_KEYS),
        ("authored_edge_keys", P.AUTHORED_EDGE_KEYS),
        ("edge_node_id_forms", P.EDGE_NODE_ID_FORMS),
    ):
        assert c[grid] == (dict(const) if isinstance(const, dict) else list(const)), (
            f"{grid} 和引擎那份常量漂了")
    # 🔴 **反向闸:每一层都得有一格。** 加一层却忘了报,创作台的盲区就多一个,
    # 而它那条"盲区不许变多"的闸只能靠人去发现。
    # ⚠️ 第十二层 `authored_edge_keys` 住在**作者层的 `edge` 记录**上,不在
    # `plugin` 记录里(2026-08-31,D44)—— 按记录类型给这张表设边界,正是
    # "一层一层收"那个 bug 的形状:新开的那一层恰好在边界外,于是又一次不在名单上。
    assert set(P.STRICT_LEVELS) == {
        "plugin_keys", "fact_keys", "edge_keys", "kind_keys", "verb_keys",
        "rule_keys", "emit_keys", "trigger_keys", "trigger_emit_keys",
        "edge_effect_keys", "projected_source_keys", "authored_edge_keys",
    }, sorted(P.STRICT_LEVELS)
    for grid in P.STRICT_LEVELS:
        assert c.get(grid), f"契约少报了一层:{grid}"


# ── 作者层里种得下什么:实例种得下,边种不下(2026-08-27,创作台问的那两半)────
#
# 创作台要做「组织」模板(门派 + `member_of` + 初始成员),于是问了两件事:
# **(a) 一条边**、**(b) 插件种类的一个实例**,作者层里写不写得下?
# 答案不一样,而两个答案都得有闸看着 —— 一句写在文档里的答案会烂,
# 而烂了的样子是创作台按它画了一个画不出东西来的界面。

MENPAI_SEED = {
    "id": "menpai", "version": "1.0.0", "label": "门派",
    "kinds": {"group:sect": {"gloss": "一个门派", "facts": {
        "声望": {"shape": "number", "default": 0.0, "visibility": "public",
                 "label": "声望"}}}},
    "edges": {"member_of": {
        "label": "门下", "from": "agent", "to": "group:sect", "exclusive": True,
        "facts": {"辈分": {"shape": "state", "default": "外门",
                           "visibility": "connected", "label": "辈分",
                           "values": [{"name": "外门"}, {"name": "内门"}]}}}},
    "verbs": {"入门": {"target": "group:sect", "description": "拜入这个门派",
                       "effects": [{"link": {"type": "menpai.member_of",
                                             "from": "self", "to": "target"}}]}},
}


def _seeded_menpai_world(tmp_path, *, entities=(), name="menpai"):
    path = write_seed_file(tmp_path / f"{name}.cyberworld",
                           {**BARE, "plugins": [dict(MENPAI_SEED)],
                            "entities": [dict(e) for e in entities]})
    return open_world_at(str(tmp_path / f"{name}.db"), world_file=path,
                         force_mock_llm=True)


def test_插件种类的实例_作者层里种得下_走的就是entities段(tmp_path):
    """**(b) 能。** 而且没有新段、没有新语法 —— 插件的种类编译成普普通通的本体
    种类之后,它的实例就是普普通通的一条 `entity` 记录,只有一条要记:
    id 里那个种类名要写**全名**(`<插件>.<局部名>`)。

    ⚠️ **量名不带命名空间**(`声望`,不是 `menpai.声望`):种类上的事实住在那个
    实例自己的量表里,不和别人共用一张表。顶层 `facts` 那一族才带 —— 它们住在
    角色/世界/地点的量表上,跨插件共用一张,所以必须分得开。

    🔴 **上一版这里写着「照 `menpai.声望` 写规律,那个名字恒等于 0」,而那句是错的**
    (2026-08-27 验收 C 实测、逐条敲过):**两种情形没有一种是它说的那样** ——
    规律**读**它当场三扇门全红(「没声明过这个事实」);规律**只写**它,从前是
    量表里并排住下 `声望` 与 `menpai.声望` 两个量(前者停在默认值、后者每 tick 在涨),
    **而 `validate` 说绿、零 warning、日志零字**。写不到的从来不是「那个名字」,
    是**那条规律更新了一个没人读的量**。第二种情形这一轮收了(见下一条用例)。
    """
    inst = {"id": "menpai.sect:青云门", "name": "青云门", "location": "cafe"}
    with _seeded_menpai_world(tmp_path, entities=[inst]) as world:
        rows = {e["id"]: e for e in world.entities()}
        assert "menpai.sect:青云门" in rows, f"实例没种进去:{sorted(rows)}"
        assert rows["menpai.sect:青云门"]["kind"] == "menpai.sect"
        assert rows["menpai.sect:青云门"]["location"] == "cafe"
        # 种类上声明的事实,**按默认值落地了**,而且量名是裸的。
        values = world.scheduler.stock_store.snapshot("menpai.sect:青云门")
        assert "声望" in values, f"种类上那个事实一格都没落地:{values}"
        assert "menpai.声望" not in values, (
            "种类上的事实**不带命名空间** —— 而带了命名空间的那个名字不是"
            "「恒等于 0」,是**另一个量**(下一条用例钉它)"
        )
        # 那条边真的连得上这个实例(**契约里那格节点 id 形状,拿它真跑一遍**)。
        assert world.scheduler.apply_edge_effect(
            {"op": "link", "type": "menpai.member_of",
             "from": "agent:阿岚", "to": "menpai.sect:青云门"}, {})
        assert world.scheduler.edge_store.all("menpai.member_of") == [
            ("agent:阿岚", "menpai.sect:青云门", {"menpai.辈分": 0.0})
        ]



def test_插件的规律写一个没声明过的事实_当场拒_而不是并排多一个量(tmp_path):
    """🔴 **验收 C 逮的那一条,而它是这一族最贵的形状:两个量并排住着。**

    种类上声明的事实量名是**裸的**(`声望`),而作者很自然会照顶层那一族的样子
    写成 `menpai.声望`。从前:`bad_output_name` 只查前缀,`menpai.<任何字>` 一律放行
    —— 于是那张量表里并排住下两个量,`声望` 停在默认值没人更新、`menpai.声望`
    每 tick 在涨而**没有一处读它**;`validate` 说绿、零 warning、日志零字,
    而 `rule_stats()` 报的是 written。作者看到的只有「我的声望不动」。

    ⚠️ **触发器那一层早就这么查了** —— 所以这不是加一道新闸,是把**同一个插件里
    两种写法的两种下场**抹平。

    **真跑若干 tick**,不是只看创世那一刻:上一版那条用例只断了创世,而这个 bug
    要等规律跑起来才现形。
    """
    seed = {**BARE, "entities": [{"id": "menpai.sect:青云门", "name": "青云门",
                                  "location": "cafe"}]}
    ghost = {
        "id": "menpai", "version": "1.0.0",
        "kinds": {"group:sect": {"gloss": "一个门派", "facts": {
            "声望": {"shape": "number", "default": 0.0, "visibility": "public"}}}},
        "rules": [{"id": "涨", "for_each": {"kind": "menpai.sect"},
                   "set": {"menpai.声望": "now"}}],   # 只写不读 —— 从前一路绿到底
    }
    path = write_seed_file(tmp_path / "ghost.cyberworld", {**seed, "plugins": [ghost]})
    payload = json.loads(run_cli("validate", "world", str(path), "--json").stdout)
    assert payload["valid"] is False, (
        "这条规律更新的是一个没人读的量,而两扇门说绿 —— 那正是这个 bug 的样子"
    )
    joined = "\n".join(payload["errors"])
    assert "声望" in joined and "bearer" in joined, joined
    with pytest.raises(Exception):      # noqa: B017 - 开机那一侧同一句话
        open_world_at(str(tmp_path / "ghost.db"), world_file=path,
                      force_mock_llm=True).close()

    # **第二种写法是对的,而且真跑得动**:顶层 `facts` + `bearer: entity:<种类>`,
    # 名字就带上命名空间了。⚠️ 这一条是给作者的替代写法,所以必须真敲。
    ok = {
        "id": "menpai", "version": "1.0.0",
        "kinds": {"group:sect": {"gloss": "一个门派", "facts": {
            "声望": {"shape": "number", "default": 0.0, "visibility": "public"}}}},
        "facts": {"香火": {"bearer": "entity:menpai.sect", "shape": "number",
                           "default": 0.0, "visibility": "public"}},
        "rules": [{"id": "涨", "for_each": {"kind": "menpai.sect"},
                   "set": {"menpai.香火": "menpai.香火 + 1"}}],
    }
    good = write_seed_file(tmp_path / "ok.cyberworld", {**seed, "plugins": [ok]})
    assert json.loads(
        run_cli("validate", "world", str(good), "--json").stdout)["valid"] is True
    with open_world_at(str(tmp_path / "ok.db"), world_file=good,
                       force_mock_llm=True) as world:
        world.tick(3)
        values = world.scheduler.stock_store.snapshot("menpai.sect:青云门")
        assert values["menpai.香火"][0] == 3.0, values
        assert values["声望"][0] == 0.0, "种类上那个事实照旧是裸名,谁也没动它"


def test_插件没装上时_那句引用不到要说清是连带的(tmp_path):
    """🔴 **一句字面上为真、而把人指向错误方向的报错**(2026-08-27 验收 C)。

    插件坏了 → 它声明的种类一行都没编译出来 → 实例那条 `entity` 记录收到一句
    「引用不到 —— 没有名叫 'menpai.sect' 的 kind」。那句话是真的,可作者会去改
    实例、改种类名,而错在别处。**只加一句说清因果,不去猜哪几条是连带的** ——
    猜错了比不猜更贵:把一条真错说成连带的,作者就再也不会去看它了。
    """
    seed = {
        **BARE,
        "kinds": [{"id": "tree",
                   "quantities": {"树高": {"default": 1.0, "visibility": "here"}}}],
        "entities": [{"id": "menpai.sect:青云门", "name": "青云门",
                      "location": "cafe"}],
        "plugins": [{
            "id": "menpai", "version": "1.0.0",
            "kinds": {"group:sect": {"gloss": "一个门派"}},
            "rules": [{"id": "涨", "for_each": {"kind": "menpai.sect"},
                       "set": {"别人.声望": "1"}}],       # 越界写:整条插件装不上
        }],
    }
    path = write_seed_file(tmp_path / "cascade.cyberworld", seed)
    payload = json.loads(run_cli("validate", "world", str(path), "--json").stdout)
    assert payload["valid"] is False
    joined = "\n".join(payload["errors"])
    assert "没有名叫 'menpai.sect'" in joined, joined      # 那句误导话还在(它是真的)
    assert "连带" in joined and "先修插件那几条" in joined, (
        f"没告诉作者这几条是连带的,他会去改一个没错的地方:{payload['errors']}"
    )
    # **对照组:插件是好的时候,这句话不许响** —— 一句总在响的提示等于没有提示。
    good = dict(seed)
    good["plugins"] = [{
        "id": "menpai", "version": "1.0.0",
        "kinds": {"group:sect": {"gloss": "一个门派"}},
    }]
    ok_path = write_seed_file(tmp_path / "cascade-ok.cyberworld", good)
    ok_payload = json.loads(
        run_cli("validate", "world", str(ok_path), "--json").stdout)
    assert ok_payload["valid"] is True, ok_payload["errors"]
    assert not any("连带" in e for e in ok_payload["errors"])

def test_一条边_作者层里种得下了_而且真的落进那张表(tmp_path):
    """**(a) 能了**(3.8.0,2026-08-31,收件箱 D44)。作者层第十四个段 `edge`。

    ⚠️ **这条用例上一版的方向是相反的** —— 它钉着"种不下",并写明哪天种得下了
    它会红、而那正是提醒。今天就是那一天,所以连它一起翻过来:契约里
    `deferred_author_sections` 那一格空了、`edge_author_type` 那三格新加、
    `_authored_uncreatable_edges` 改了口,三处都跟着动了。

    🔴 **判据是"那张表里真有这一行",不是"开机没报错"** —— 一条种下去而没落库的
    边和一条根本没写的边,在退出码和日志上长得一模一样。
    """
    path = write_seed_file(tmp_path / "seeded-edge.cyberworld", {
        **BARE,
        "plugins": [dict(MENPAI_SEED)],
        "entities": [{"id": "menpai.sect:青云门", "name": "青云门",
                      "location": "cafe"}],
        "edges": [{"type": "menpai.member_of", "from": "agent:夏",
                   "to": "menpai.sect:青云门"}],
    })
    payload = json.loads(
        run_cli("validate", "world", str(path), "--json").stdout)
    assert payload["valid"] is True, payload["errors"]
    with open_world_at(str(tmp_path / "seeded-edge.db"), world_file=path,
                       force_mock_llm=True) as world:
        rows = world.scheduler.edge_store.all("menpai.member_of")
        assert rows == [("agent:夏", "menpai.sect:青云门", {"menpai.辈分": 0.0})], rows
        # **声明过的事实照默认值落地** —— 走的是 `apply_edge_effect` 那条路,
        # 所以带命名空间(`menpai.辈分`),和运行期 `link` 出来的一模一样。


def test_不认识的作者层type_照旧当场开不了机_而且报错列出认得的那几个(tmp_path):
    """`edge` 收进来了,**而"不认识的 type 当场报错"这条一个字没松**。

    这两件事必须一起钉:只钉前一半的话,一个把 `edge` 写成 `edges` 的作者会得到
    一份**开得起来而少了一整层**的世界 —— 那正是"安静地少装一半世界"。
    """
    import gzip

    path = tmp_path / "typo.cyberworld"
    rows = [
        {"kind": "manifest", "version": 3, "world_id": "t", "engine_min": "3.8.0"},
        {"kind": "author", "type": "plugin", "body": dict(MENPAI_SEED)},
        {"kind": "author", "type": "edges",        # 多一个 s
         "body": {"type": "menpai.member_of", "from": "agent:阿岚",
                  "to": "menpai.sect:青云门"}},
    ]
    with gzip.GzipFile(path, "wb", mtime=0) as fh:
        fh.write(("\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
                  + "\n").encode())

    check = json.loads(run_cli("world", "check", str(path), "--edit", "--json").stdout)
    assert check["loadable"] is False, "不认识的 type 被收下了?"
    joined = "\n".join(check["errors"])
    assert "'edges'" in joined and "只认" in joined, joined
    # **报错里那张表就是答案本身** —— 作者不必去翻文档才知道能写哪几个段,
    # 而 `edge` 从今天起就在这张表上。
    for section in ("agent", "kind", "entity", "plugin", "edge"):
        assert f"'{section}'" in joined, joined


def test_契约说得出这两个答案_而且那几格是真的(tmp_path):
    """🔴 **一格「怎么写」比一段「请注意」值钱得多**(`edge_end_prefixes` 那条先例)。

    这里钉三件:实例走哪个段、节点 id 长什么样、以及**这一版收不下的作者层段
    连理由一起报**(和 `deferred_fact_shapes` 逐字同构)。
    ⚠️ 节点 id 那一格**不是抄的**:下面拿它和引擎真正在用的那个函数对了一遍。
    """
    from anima_world.world_file import AUTHOR_SECTIONS

    seg = json.loads(run_cli("contract", "--json").stdout)["plugins"]

    assert seg["kind_instance_section"] == "entities"
    assert seg["kind_instance_section"] in AUTHOR_SECTIONS.values()
    assert seg["kind_instance_id_syntax"] == "<plugin>.<local>:<实例名>"

    # 🔴 **`edge` 从 2026-08-31 起在作者层的段表上,而契约必须跟着改口** ——
    # 两句话对不上时,以 `AUTHOR_SECTIONS` 为准(它是开机真读的那一份)。
    assert AUTHOR_SECTIONS.get("edge") == seg["edge_author_section"], (
        "契约报的段名和开机真读的那份对不上 —— 抄第二遍就是这么烂的"
    )
    assert seg["edge_author_type"] == "edge"
    deferred = seg["deferred_author_sections"]
    assert "edge" not in deferred, (
        "作者层已经种得下边了,而契约还在说它收不下 —— 消费方读的就是这一格"
    )
    # ⚠️ **空对象 ≠ 整格缺席**:缺席 = 3.8.0 之前的老引擎(连这个问题都答不出),
    # 空 = 这一版作者层一个段都不欠。删掉这一格,消费方就再也分不出这两件事。
    assert deferred == {} and isinstance(deferred, dict), deferred

    # 节点 id 那一格:**拿引擎自己的函数对一遍**,不是读文档抄的。
    forms = seg["edge_node_id_forms"]
    with _seeded_menpai_world(tmp_path, name="forms") as world:
        assert forms["agent"].replace("<agent_id>", "阿岚") == \
            world.scheduler.stock_owner_of("阿岚")
        assert forms["player"].replace("<player_id>", "p1") == \
            world.scheduler.stock_owner_of("player:p1")
    assert forms["world"] == "world"
    assert forms["location"].startswith("location:")
    for template in ("entity:<kind>", "group:<kind>"):
        assert template in forms, sorted(forms)


def test_声明了一种边而没人造得出它_引擎会说话(tmp_path):
    """**警告,不是错误** —— 开机是权威,而开机收得下这种声明。可**没有一处会
    说话**才是病:那张表永远 0 条,而作者分不出「造不出来」和「还没有人连上」。

    ⚠️ 对照组在后半截:给它一个带 `link` 的动词,这句话就不许再响 ——
    **一句总在响的警告等于没有警告。**
    """
    from anima_world.__main__ import _authored_uncreatable_edges

    idle = dict(MENPAI_SEED)
    idle["verbs"] = {}                      # 把唯一那条 `link` 拿掉
    said = _authored_uncreatable_edges({"plugins": [idle]})
    assert said and "menpai.member_of" in said[0], said

    assert _authored_uncreatable_edges({"plugins": [dict(MENPAI_SEED)]}) == [], (
        "有动词造得出它,这句话就不该响"
    )
    # 🔴 **`transfer` 不算造得出**(2026-08-27 验收 A):`apply_edge_effect` 的
    # `transfer` 那一支是 `of_src` **把已有的行搬个家** —— 空表上一行都取不到、
    # 当场返回 False。一个只有 `transfer` 动词的插件,边表**永远**是空的,
    # 而上一版把它算成造法,于是这句警告对它**恰好不响**:
    # 一条在最该响的时候闭嘴的警告,比没有这条警告更坏。
    only_transfer = dict(MENPAI_SEED)
    only_transfer["verbs"] = {"改投": {
        "target": "group:sect", "description": "改投别的门派",
        "effects": [{"transfer": {"type": "menpai.member_of",
                                  "from": "self", "to": "target"}}]}}
    said = _authored_uncreatable_edges({"plugins": [only_transfer]})
    assert said and "menpai.member_of" in said[0], (
        f"只有 transfer 的插件边表永远是空的,而这句话没响:{said}"
    )
    # 而它**真的**搬不动一张空表 —— 判据不是读代码推的。
    with _seeded_menpai_world(tmp_path, name="mv") as world:
        assert world.scheduler.edge_store.all("menpai.member_of") == []
        assert world.scheduler.apply_edge_effect(
            {"op": "transfer", "type": "menpai.member_of",
             "from": "agent:阿岚", "to": "menpai.sect:青云门"}, {}) is False
        assert world.scheduler.edge_store.all("menpai.member_of") == [], (
            "空表上的 transfer 居然造出了一条边 —— 那这条规矩整个要重写"
        )

    # 触发器那条路也算数(动词不是唯一的造法)。
    by_trigger = dict(MENPAI_SEED)
    by_trigger["verbs"] = {}
    by_trigger["facts"] = {"资历": {"bearer": "agent", "shape": "number"}}
    by_trigger["triggers"] = [{
        "id": "入门礼", "on": {"event": "conversation"},
        "effects": [{"link": {"type": "menpai.member_of", "from": "self",
                              "to": "menpai.sect:青云门"}}]}]
    assert _authored_uncreatable_edges({"plugins": [by_trigger]}) == []

    # 🆕 **第三条造法:作者层里直接种下**(3.8.0,2026-08-31,D44)。
    # 这一句不跟着改口的话,它会对一份**写着初始成员**的世界报假警报,
    # 而作者会去加一个他并不需要的动词 —— 上一版这条用例的 docstring
    # 就写着"哪天种得下了这句话要改口"。
    assert _authored_uncreatable_edges({
        "plugins": [idle],
        "edges": [{"type": "menpai.member_of", "from": "agent:夏",
                   "to": "menpai.sect:青云门"}],
    }) == [], "作者层里种了边,这句「没人造得出」就不该再响"
    # **对照组:种的是别的一种边,它照旧要响** —— 一句"只要种过任何一条就闭嘴"
    # 的实现同样通得过上面那条断言,而它会对真正空着的那一种保持沉默。
    said = _authored_uncreatable_edges({
        "plugins": [idle],
        "edges": [{"type": "menpai.别的", "from": "agent:夏", "to": "x:y"}],
    })
    assert said and "menpai.member_of" in said[0], said


# ── `travel` 那一条:`parties` 说的不是「落在谁头上」(2026-08-27 复核)───────
#
# 契约里 `travel.parties == ["player_id"]`,而它的 gloss 写着「角色与玩家共用这一
# 条」—— 两句话并排读会推出一个结论:**角色出发时触发器对不上人**。
# 那个结论是错的,而错的不是代码,是**两处 gloss**(白名单那张表的说明,
# 和 `_fire_trigger` 的 docstring):它们都说 `parties` 决定 `for_each` 取谁,
# 而取人那条路一个字都不读它。下面这条把真相钉住。


def test_travel那一条_角色与玩家两半都对得上人(tmp_path):
    """🔴 **两半各真跑一次**,不是读代码推的。

    角色那条走 `Scheduler._start_journey`(事件顶层 `who` = 她的 id,载荷里
    **没有** `player_id`);玩家那条走 `World.player_walk`(顶层 `who` =
    `player:<id>`,载荷里才有 `player_id`)。触发器取人取的是**顶层 `who`**,
    所以两半都落得到人 —— 而照 `parties` 那一格推的话,角色那一半会被判成
    「对不上人,只能当一次全局脉冲」。

    ⚠️ **这条用例的价值在对照**:只跑玩家那一半的话,一个真的漏掉角色的实现
    照样绿。
    """
    from anima_world.actions import ActionDescriptor

    seed = {
        "agents": [{"id": "阿岚", "name": "阿岚", "location": "cafe",
                    "personality": "安静"}],
        "locations": [
            {"id": "cafe", "name": "咖啡馆", "description": "临海的小店"},
            {"id": "pier", "name": "码头", "description": "风很大"},
        ],
        "plugins": [{
            "id": "lu", "version": "1.0.0",
            "facts": {"里程": {"bearer": "agent", "shape": "number",
                               "default": 0.0, "visibility": "self"}},
            "triggers": [{"id": "上路", "on": {"event": "travel"},
                          "for_each": {"node": "agent"},
                          "effects": [{"set": {"lu.里程": "lu.里程 + 1"}}]}],
        }],
    }
    path = write_seed_file(tmp_path / "travel.cyberworld", seed)
    with open_world_at(str(tmp_path / "travel.db"), world_file=path,
                       force_mock_llm=True) as world:
        # ① 角色出发 —— 载荷里没有 `player_id` 那一格。
        started = world.scheduler._start_journey(
            world.scheduler.agents["阿岚"].agent,
            ActionDescriptor("walk", {"location": "pier"}))
        assert started, "夹具前提没成立:这个世界量不出两点之间的路"
        world.tick(1)
        assert world.stocks("agent:阿岚").get("lu.里程") == 1.0, (
            "角色出发,而订 `travel` 的触发器一次都没响 —— 那才是 `parties` 那一格"
            "会让人推出来的结论,而它不成立"
        )

        # ② 玩家出发 —— 这一半载荷里有 `player_id`。
        world.player_move("p1", "cafe", display_name="阿宇")
        world.tick(1)
        assert world.player_walk("p1", "pier")["in_transit"]
        world.tick(1)
        assert world.stocks("agent:player:p1").get("lu.里程") == 1.0, (
            "玩家那一半也得对得上人 —— 玩家和角色同一个量表命名空间"
        )


def test_契约说得出触发器从哪一格取人_而且那张表是全的():
    """**一格「从哪儿取」比一段「它决定……」值钱得多。**

    反向闸:`for_each.node` 收的那几种形式,`trigger_bearer_keys` 必须一种都不少 ——
    加一种 bearer 而忘了报,消费方就只能去猜,而猜错不报错。
    """
    from anima_world.plugins import TRIGGER_BEARER_FORMS

    seg = json.loads(run_cli("contract", "--json").stdout)["plugins"]
    table = seg["trigger_bearer_keys"]
    # 🔴 **和引擎自己那份受理名单逐格比,别写死一个字面量**(2026-08-27 验收 A:
    # 上一版断的是四元集合字面量,而 A 把 `actor`/`player` 加进受理集合、契约不动,
    # **它照样绿** —— 一条自称反向闸而其实两头都不看的断言)。
    assert set(table) == set(TRIGGER_BEARER_FORMS), (
        f"契约那一格和引擎真受理的对不上 —— 契约 {sorted(table)};"
        f"引擎 {list(TRIGGER_BEARER_FORMS)}"
    )
    assert "who" in table["agent"], table["agent"]
    assert "loc" in table["location"], table["location"]
    assert seg["trigger_bearer_gloss"].strip(), "光有表没有那句话,读的人会以为 parties 也算"
    assert "parties" in seg["trigger_bearer_gloss"], (
        "这一格存在的理由就是把 `parties` 那句假话按住,而它没提这件事"
    )
    # `travel` 那一条的 gloss 自己也要说清楚,别让两句话并排读推出一个错结论。
    note = seg["subscribable_events"]["travel"]["note"]
    assert "who" in note and "两半都对得上" in note, note


# ── 动词写得到自己的事实了(3.8.0,2026-08-27 第二波 ①)────────────────────────
#
# 调度台拿真世界试出来的第一条:**一个插件的动词改不动它自己的事实**。
# `costs: {"tape.精神": "me_tape.精神 - 10"}` 被本体那一层按"怪名字"拒掉,
# 而拒绝语是作者层规律那句「跨实体的相互作用 v1 还表达不了」—— 一句和插件毫无
# 关系的话。下场:「施法耗灵力」这种最基本的写法写不出来。

_TAPE = {
    "id": "tape", "version": "1.0.0", "label": "磁带",
    "facts": {"精神": {"bearer": "actor", "shape": "number", "default": 50.0,
                       "range": [0, 100], "visibility": "self", "label": "劲头"}},
    "kinds": {"entity:tape": {"gloss": "一盒磁带", "facts": {
        "录满": {"shape": "number", "default": 0.0, "range": [0, 1],
                 "visibility": "here", "label": "录了多少"}}}},
    "verbs": {"录一面": {
        "target": "entity:tape", "label": "录一面", "description": "把话录进去",
        "costs": {"嗓子": "me_嗓子 - 0.1", "tape.精神": "me_tape.精神 - 10"},
        "set": {"录满": "clamp(录满 + 0.5, 0, 1)"},
    }},
    "rules": [{"id": "缓过来", "every": {"ticks": 1}, "for_each": {"kind": "agent"},
               "set": {"tape.精神": "clamp(tape.精神 + 0.5 * dt, 0, 100)"}}],
}
_TAPE_SEED = {
    "agents": [{"id": "yu", "name": "露", "location": "cafe", "personality": "安静"}],
    "locations": [{"id": "cafe", "name": "咖啡馆", "description": "临海的小店"}],
    "kinds": [{"id": "agent",
               "quantities": {"嗓子": {"default": 1.0, "visibility": "self"}}}],
    "entities": [{"id": "tape.tape:h1978", "name": "一号带", "location": "cafe"}],
}


def _tape_world(tmp_path, plugin=None, name="tape"):
    path = write_seed_file(tmp_path / f"{name}.cyberworld",
                           {**_TAPE_SEED, "plugins": [plugin or _TAPE]})
    return path, open_world_at(str(tmp_path / f"{name}.db"), world_file=path,
                               force_mock_llm=True)


def test_动词的costs写得到自己的插件事实_而且真扣(tmp_path):
    """🔴 **第二波 ① 本身**,判据是调度台那份 `tape.json`:两扇门从红变绿,
    `World.act` 真扣,**而且重开一次照旧收**(存的那一行要再解析一遍)。"""
    path, world = _tape_world(tmp_path)
    payload = json.loads(run_cli("validate", "world", str(path), "--json").stdout)
    assert payload["valid"] is True, payload["errors"]
    with world:
        before = dict(world.stocks("agent:yu"))
        assert before["tape.精神"] == 50.0 and before["嗓子"] == 1.0
        out = world.act("yu", "interact",
                        {"target": "tape.tape:h1978", "verb": "录一面"})
        assert out["ok"] is True, out
        after = dict(world.stocks("agent:yu"))
        assert after["tape.精神"] == 40.0, f"自己的事实没被扣:{after}"
        assert after["嗓子"] == 0.9, f"内核那一格也得照旧扣:{after}"
        assert world.scheduler.stock_store.snapshot(
            "tape.tape:h1978")["录满"][0] == 0.5

    # 🔴 **重开一次** —— 本体是从 `:kinds` 那一行**重新解析**的
    # (`RedisOntologyStore.load`),genesis 放行而重开拒的话,世界会在第二天打不开。
    with open_world_at(str(tmp_path / "tape.db"), force_mock_llm=True) as again:
        assert again.stocks("agent:yu")["tape.精神"] == 40.0


def test_写不到别的插件的事实上_而且理由是投影那条(tmp_path):
    """🔴 **裁决(第二波 ①.②):`costs` 也只写得到自己的命名空间。**

    设计稿 §4.2 那个 `economy.coins - 500` 的例子写在这条边界定下来之前,以边界为准。
    最硬的一条理由不是对称性,是**别人的事实可能是 `projected`** —— `economy.coins`
    今天正是,量表里那个数只是物化视图,扣下去**重开一次就回来了,零报错**。
    """
    bad = json.loads(json.dumps(_TAPE, ensure_ascii=False))
    bad["verbs"]["录一面"]["costs"]["economy.coins"] = "economy.coins - 500"
    path = write_seed_file(tmp_path / "cross.cyberworld",
                           {**_TAPE_SEED, "plugins": [bad]})
    payload = json.loads(run_cli("validate", "world", str(path), "--json").stdout)
    assert payload["valid"] is False, "跨插件写被收下了 —— 那这条裁决没有闸"
    joined = "\n".join(payload["errors"])
    assert "写不到别的插件的事实上" in joined and "projected" in joined, joined
    assert "reads" in joined and "requires" in joined, (
        f"拒绝了却没告诉他该怎么写(读得到、写不了):{joined}"
    )


def test_写自己命名空间下一个没声明的事实_当场拒并说清裸名那一族(tmp_path):
    """拒绝语要**指着插件自己的病灶**,而不是作者层规律那句「跨实体的相互作用」。"""
    bad = json.loads(json.dumps(_TAPE, ensure_ascii=False))
    bad["verbs"]["录一面"]["costs"]["tape.没这个"] = "me_tape.没这个 - 1"
    path = write_seed_file(tmp_path / "typo.cyberworld",
                           {**_TAPE_SEED, "plugins": [bad]})
    payload = json.loads(run_cli("validate", "world", str(path), "--json").stdout)
    assert payload["valid"] is False
    joined = "\n".join(payload["errors"])
    assert "顶层 `facts` 里没有" in joined and "量名是裸的" in joined, joined
    assert "跨实体的相互作用" not in joined, (
        "还在拿作者层规律那句话当拒绝理由 —— 那正是第二波 ⑥ 说的「指错病灶」"
    )


def test_projected的事实写不得_而且costs要扣在人身上(tmp_path):
    """两条**挂错身子**的写法各一条:`projected` 的事实写不得;
    `costs` 扣的是施动者,挂在东西身上的事实扣不到人头上去。"""
    mine = json.loads(json.dumps(_TAPE, ensure_ascii=False))
    mine["facts"]["账"] = {"bearer": "actor", "shape": "number", "mode": "projected",
                           "sources": [{"event": "payment", "credit": "to"}]}
    mine["verbs"]["录一面"]["costs"]["tape.账"] = "me_tape.账 - 1"
    path = write_seed_file(tmp_path / "proj.cyberworld",
                           {**_TAPE_SEED, "plugins": [mine]})
    joined = "\n".join(
        json.loads(run_cli("validate", "world", str(path), "--json").stdout)["errors"])
    assert "`projected` 的事实**写不得**" in joined, joined
    assert "重开一次就回到折出来的那个数" in joined, joined

    wrong = json.loads(json.dumps(_TAPE, ensure_ascii=False))
    wrong["facts"]["温度"] = {"bearer": "world", "shape": "number", "default": 0.0}
    wrong["verbs"]["录一面"]["costs"]["tape.温度"] = "tape.温度 - 1"
    path = write_seed_file(tmp_path / "bearer.cyberworld",
                           {**_TAPE_SEED, "plugins": [wrong]})
    joined = "\n".join(
        json.loads(run_cli("validate", "world", str(path), "--json").stdout)["errors"])
    assert "扣的是**施动者**身上的量" in joined, joined


def test_本体那一层的形状判据_和插件id那条正则对得上():
    """两层各写一份形状的话,一个合法的插件 id 会在本体那一层被判成"怪名字"
    (而那正是这一条要防的假红)。"""
    import re

    from anima_world.plugins import PLUGIN_ID_PATTERN
    from anima_world.rules import namespaced_output

    for good in ("qi", "a1", "a" * 32, "with_underscore"):
        assert re.match(PLUGIN_ID_PATTERN, good), good
        assert namespaced_output(f"{good}.灵力"), good
    for bad in ("Qi", "1qi", "q", "q-i"):
        assert not re.match(PLUGIN_ID_PATTERN, bad), bad
        assert not namespaced_output(f"{bad}.灵力"), bad


# ── 触发器读得到 `world_*` 了,而且每条触发器有六个数(第二波 ②)──────────────
#
# 调度台在真世界上撞到的:`on:{event:"travel"}` + `when:["world_雨势 > 0.3"]`,
# 雨势 0.8,一天十六条 `travel` **一次没响**;把 `when` 去掉、升 1.0.1,当场就响。
# `world_雨势` 在**作者层规律**里是合法写法(晚潮自己在用),而触发器这条路上
# 它既不在命名空间里、装载期也不拒 —— **两条路对同一个写法给两个答案,
# 而错的那一边不说话。**

_WET = {
    "id": "wet", "version": "1.0.0", "label": "淋雨",
    "facts": {"身上": {"bearer": "actor", "shape": "state", "default": "干爽",
                       "visibility": "here", "label": "身上",
                       "values": [{"name": "干爽"}, {"name": "潮乎乎"},
                                  {"name": "湿透"}]}},
    "triggers": [{"id": "出门淋雨", "on": {"event": "travel"},
                  "when": ["world_雨势 > 0.3"],
                  "effects": [{"set": {"wet.身上": "2"}}]}],
}
_WET_SEED = {
    "agents": [{"id": "yu", "name": "露", "location": "cafe", "personality": "安静"}],
    "locations": [
        {"id": "cafe", "name": "咖啡馆", "description": "临海的小店"},
        {"id": "pier", "name": "码头", "description": "风很大"},
    ],
    "stocks": [{"owner": "world", "values": {"雨势": 0.8}}],
}


def _walk(world, who="yu", to="pier"):
    from anima_world.actions import ActionDescriptor

    return world.scheduler._start_journey(
        world.scheduler.agents[who].agent, ActionDescriptor("walk", {"location": to}))


def test_触发器的when读得到world_全局量(tmp_path):
    """🔴 **第二波 ② 本身**,判据用的就是调度台那份 `wet.json` 1.0.0(带 `when`)。

    ⚠️ **对照组在同一条用例里**:把世界的雨势调到 0.1,同一条触发器就不许再落笔 ——
    否则这条用例对一个"`when` 整个不算"的实现同样成立。
    """
    path = write_seed_file(tmp_path / "wet.cyberworld",
                           {**_WET_SEED, "plugins": [_WET]})
    with open_world_at(str(tmp_path / "wet.db"), world_file=path,
                       force_mock_llm=True) as world:
        assert json.loads(
            run_cli("validate", "world", str(path), "--json").stdout)["valid"]
        assert world.stocks("agent:yu")["wet.身上"] == 0.0
        assert _walk(world), "夹具前提没成立:这个世界量不出两点之间的路"
        world.tick(1)
        assert world.stocks("agent:yu")["wet.身上"] == 2.0, (
            "`world_雨势` 在触发器的 `when` 里恒为假 —— 一天十六条 travel 一次没响,"
            "而作者层规律里同一个写法是合法的"
        )
        stats = world.trigger_stats()["wet.出门淋雨"]
        assert stats["matched"] == 1 and stats["written"] == 1, stats
        assert stats["when_false"] == 0, stats

    # 对照组:雨停了,同一条触发器不许落笔,而且**说得出它是因为 `when`** 不响的。
    dry = {**_WET_SEED, "stocks": [{"owner": "world", "values": {"雨势": 0.1}}],
           "plugins": [_WET]}
    path = write_seed_file(tmp_path / "dry.cyberworld", dry)
    with open_world_at(str(tmp_path / "dry.db"), world_file=path,
                       force_mock_llm=True) as world:
        assert _walk(world)
        world.tick(1)
        assert world.stocks("agent:yu")["wet.身上"] == 0.0
        stats = world.trigger_stats()["wet.出门淋雨"]
        assert stats["matched"] == 1 and stats["when_false"] == 1, stats
        assert stats["written"] == 0, stats


def test_没人读world的时候_一次多余的查询都不发(tmp_path):
    """`world_*` 那一趟是**有人读才读**(和 `stocks.evaluate_due` 同一条判断)——
    否则每一条事件都要多一次 `HGETALL`,而绝大多数插件根本不读世界的量。"""
    plain = json.loads(json.dumps(_WET, ensure_ascii=False))
    plain["triggers"][0].pop("when")
    path = write_seed_file(tmp_path / "plain.cyberworld",
                           {**_WET_SEED, "plugins": [plain]})
    with open_world_at(str(tmp_path / "plain.db"), world_file=path,
                       force_mock_llm=True) as world:
        assert world.scheduler._trigger_world_values(
            [{"type": "travel", "who": "yu"}]) == {}, (
            "没有一条触发器读 `world_*`,这一趟不该去查世界的量"
        )
        assert world.scheduler._trigger_world_values([]) == {}


def test_触发器六个数各指一种修法(tmp_path):
    """**一条 `when` 恒为假的触发器,和一条根本没被叫到的触发器,屏幕上长得一样。**

    所以六个数要分得开 —— 这条钉 `no_bearer`(取不着当事人)那一格:
    订 `location` 的触发器碰上一条没有 `loc` 的事件。
    """
    probe = {"id": "probe", "version": "1.0.0",
             "facts": {"n": {"bearer": "location", "shape": "number",
                             "default": 0.0}},
             "triggers": [{"id": "记一笔", "on": {"event": "agent_join"},
                           "for_each": {"node": "location"},
                           "effects": [{"set": {"probe.n": "probe.n + 1"}}]}]}
    path = write_seed_file(tmp_path / "probe.cyberworld",
                           {**_WET_SEED, "plugins": [probe]})
    with open_world_at(str(tmp_path / "probe.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.scheduler._record_event({"type": "agent_join", "who": "yu"})  # 没有 loc
        world.tick(1)
        stats = world.trigger_stats()["probe.记一笔"]
        assert stats["matched"] == 1 and stats["no_bearer"] == 1, stats
        assert stats["written"] == 0 and stats["when_false"] == 0, (
            f"取不着人被记成了「条件不成立」—— 两种病两种修法:{stats}"
        )


# ── ③ `emit` 的「写了 A 就必须写 B」进契约了(第二波 ③)────────────────────────


def test_emit写了text没写importance_引擎拒_而契约现在说得出这条耦合(tmp_path):
    """🟡 **契约说收、引擎不收** —— 比缺一格更贵:创作台照契约判绿,而作者拿着
    那份包去开机,当场红。现在那条耦合是契约里的一格,判据和引擎读的是同一份常量。"""
    from anima_world.rules import EMIT_KEY_REQUIRES

    seg = json.loads(run_cli("contract", "--json").stdout)["plugins"]
    assert seg["emit_key_requires"] == {k: list(v)
                                        for k, v in EMIT_KEY_REQUIRES.items()}
    assert seg["emit_key_requires"] == {"text": ["importance"]}
    # ⚠️ **触发器那一层没有 `importance`**,别把这条耦合套过去(套了就是假红)。
    assert "importance" not in seg["trigger_emit_keys"]

    bad = {"id": "qi", "version": "1.0.0",
           "facts": {"灵力": {"bearer": "agent", "shape": "number", "default": 1.0}},
           "rules": [{"id": "喊一声", "for_each": {"kind": "agent"},
                      "set": {"qi.灵力": "qi.灵力"},
                      "emit": [{"type": "qi.累了", "when": "qi.灵力 < 1",
                                "text": "他喘了口气"}]}]}
    path = write_seed_file(tmp_path / "emit.cyberworld", {**BARE, "plugins": [bad]})
    payload = json.loads(run_cli("validate", "world", str(path), "--json").stdout)
    assert payload["valid"] is False, "引擎收下了?那这一格报的就是假话"
    assert any("importance" in e for e in payload["errors"]), payload["errors"]


# ── ④ 常数步长那条 lint 覆盖插件的规律了(第二波 ④)──────────────────────────


def test_插件的常数步长规律_也被那句提醒覆盖(tmp_path):
    """🟡 调度台量出来的:`onair.淡忘`(`every days:1`,`人气 - 1`)和晚潮作者层那条
    `梅雨` 是**同一种写法**,而引擎对后者说了三遍、对前者一个字都没有。

    **一条只覆盖一半写法的 lint,比没有这条 lint 更难查**:作者会把"引擎没说"
    读成"我这条没问题"。
    """
    from anima_world.__main__ import _authored_drift_warnings

    onair = {"id": "onair", "version": "1.0.0",
             "facts": {"人气": {"bearer": "actor", "shape": "number",
                                "default": 20.0, "range": [0, 100]}},
             "rules": [{"id": "淡忘", "every": {"days": 1},
                        "for_each": {"kind": "agent"},
                        "set": {"onair.人气": "clamp(onair.人气 - 1, 0, 100)"}}]}
    said = _authored_drift_warnings({"plugins": [onair]})
    assert said and any("淡忘" in w for w in said), (
        f"插件的常数步长规律照旧没人喊:{said}"
    )
    # 两扇门上真的印得出来(作者手上只有这条命令)。
    path = write_seed_file(tmp_path / "onair.cyberworld", {**BARE, "plugins": [onair]})
    payload = json.loads(run_cli("validate", "world", str(path), "--json").stdout)
    assert payload["valid"] is True, payload["errors"]
    assert any("淡忘" in w for w in payload["warnings"]), payload["warnings"]

    # **对照组**:同一条规律用了 `dt`,这句话就不许再响 ——
    # 一句总在响的提醒等于没有提醒。
    dt_ok = json.loads(json.dumps(onair, ensure_ascii=False))
    dt_ok["rules"][0]["set"] = {"onair.人气": "clamp(onair.人气 - 1 * dt, 0, 100)"}
    assert _authored_drift_warnings({"plugins": [dt_ok]}) == []


# ── ⑤ `doctor` 里终于有插件这一层了(第二波 ⑤)────────────────────────────────


def test_doctor_报得出插件跑没跑_而不是只报声明了几条(tmp_path):
    """🟡 二十行体检里从前**一行都没有**插件。

    🔴 而这一节答的是「**有没有发生过**」,不是「声明了几条」—— `plugin list`
    早就报得出声明面的计数,而调度台撞的那个病(一条 `when` 恒为假的触发器)
    **在声明面上完全正常**:声明得好好的、装载顺序也对,就是一次没响。

    从外面看得见的证据只有两样(所以只报这两样):事实的 `updated_tick`
    (规律与触发器写一个数**不发事件**,日志里查不到它们)、以及它发出的
    `<插件>.<type>` 事件条数。
    """
    db = tmp_path / "doc.db"
    redis_for(db)
    live = {"id": "qi", "version": "1.0.0",
            "facts": {"灵力": {"bearer": "agent", "shape": "number",
                               "default": 10.0, "range": [0, 100]}},
            "rules": [{"id": "回气", "every": {"ticks": 1},
                       "for_each": {"kind": "agent"},
                       "set": {"qi.灵力": "clamp(qi.灵力 + 1 * dt, 0, 100)"}}]}
    path = write_seed_file(tmp_path / "doc.cyberworld", {**BARE, "plugins": [live]})
    with open_world_at(str(db), world_file=path, force_mock_llm=True) as world:
        world.tick(5)

    out = run_cli("doctor", "--world-id", "w", "--skip-probe")
    assert "插件 qi" in out.stdout, out.stdout
    assert "规律 1 条" in out.stdout, out.stdout
    # **只看 qi 那一行** —— 出厂那几个也在这张表上,它们的数不是这条用例的事。
    line = next(ln for ln in out.stdout.splitlines() if "插件 qi" in ln)
    assert "写过的事实 1 个" in line, (
        f"规律真的跑过,而体检说它一个字都没动过世界:{line}"
    )
    assert "一次都没动过世界" not in line
    # **说出它看不见的那一半** —— 而不是假装查过了。
    assert "trigger_stats" in out.stdout, out.stdout


def test_doctor_对一条一次没动过世界的插件_会喊一声(tmp_path):
    """对照组,而它正是调度台那个病的形状:声明面全对、一次没响。

    ⚠️ **这一节不进退出码** —— 一个刚建好的世界什么都还没发生过,
    而「一条永远红的检查等于没有这条检查」。
    """
    db = tmp_path / "quiet.db"
    redis_for(db)
    dead = {"id": "wet", "version": "1.0.0",
            "facts": {"身上": {"bearer": "actor", "shape": "number",
                               "default": 0.0}},
            "triggers": [{"id": "淋雨", "on": {"event": "payment"},
                          "when": ["world_没这个量 > 0.3"],
                          "effects": [{"set": {"wet.身上": "2"}}]}]}
    path = write_seed_file(tmp_path / "quiet.cyberworld", {**BARE, "plugins": [dead]})
    with open_world_at(str(db), world_file=path, force_mock_llm=True) as world:
        world.tick(5)

    out = run_cli("doctor", "--world-id", "w", "--skip-probe")
    assert "插件 wet" in out.stdout, out.stdout
    assert "一次都没动过世界" in out.stdout, out.stdout
    assert "写过的事实 0 个" in out.stdout and "发出的事件 0 条" in out.stdout, out.stdout


# ── ⑥ 插件族的错,要说插件自己的理由(第二波 ⑥)────────────────────────────────
#
# 调度台点的两条:`costs` 那句拿的是作者层规律的理由(「跨实体的相互作用 v1 还
# 表达不了」),级联那句「没有名叫 `menpai.sect` 的 kind」把人指向没错的地方。
# 两条都修了(§3.52 / 上一轮 C4),而这一条把它们**钉住**:一句拒绝语说的是
# 哪一层的病,不是措辞问题 —— **它决定作者去改哪一行**。

#: 作者层那几句话,插件族的错里一句都不许出现。
_AUTHOR_LAYER_PHRASES = ("跨实体的相互作用", "在 kinds 里写一条")

_PLUGIN_MISTAKES = {
    "costs 写自己没声明的事实": {
        "id": "qi", "version": "1.0.0",
        "facts": {"灵力": {"bearer": "actor", "shape": "number", "default": 1.0}},
        "kinds": {"entity:符": {"gloss": "一张符"}},
        "verbs": {"贴": {"target": "entity:符", "description": "贴上去",
                         "costs": {"qi.没这个": "me_qi.没这个 - 1"}}},
    },
    "costs 写别的插件的事实": {
        "id": "qi", "version": "1.0.0",
        "facts": {"灵力": {"bearer": "actor", "shape": "number", "default": 1.0}},
        "kinds": {"entity:符": {"gloss": "一张符"}},
        "verbs": {"贴": {"target": "entity:符", "description": "贴上去",
                         "costs": {"economy.coins": "economy.coins - 1"}}},
    },
    "规律写自己没声明的事实": {
        "id": "qi", "version": "1.0.0",
        "facts": {"灵力": {"bearer": "actor", "shape": "number", "default": 1.0}},
        "rules": [{"id": "涨", "for_each": {"kind": "agent"},
                   "set": {"qi.没这个": "now"}}],
    },
    "规律写到别人的命名空间": {
        "id": "qi", "version": "1.0.0",
        "facts": {"灵力": {"bearer": "actor", "shape": "number", "default": 1.0}},
        "rules": [{"id": "涨", "for_each": {"kind": "agent"},
                   "set": {"别人.数": "1"}}],
    },
    "触发器的 for_each 写错": {
        "id": "qi", "version": "1.0.0",
        "facts": {"灵力": {"bearer": "actor", "shape": "number", "default": 1.0}},
        "triggers": [{"id": "t", "on": {"event": "conversation"},
                      "for_each": {"node": "actor"},
                      "effects": [{"set": {"qi.灵力": "1"}}]}],
    },
}


@pytest.mark.parametrize("case", sorted(_PLUGIN_MISTAKES))
def test_插件族的拒绝语_说的是插件自己的理由(tmp_path, case):
    """🔴 **一句拒绝语说的是哪一层的病,不是措辞问题** —— 它决定作者去改哪一行。

    调度台那一趟拿到的原话是作者层规律的理由(「跨实体的相互作用(挖矿让矿脉减少)
    v1 还表达不了」),而他写的是一个插件的动词。**照着那句话去改,他会去改一条
    根本没错的规律。**
    """
    path = write_seed_file(tmp_path / f"{hash(case) & 0xffff}.cyberworld",
                           {**BARE, "plugins": [_PLUGIN_MISTAKES[case]]})
    payload = json.loads(run_cli("validate", "world", str(path), "--json").stdout)
    assert payload["valid"] is False, f"「{case}」被收下了"
    joined = "\n".join(payload["errors"])
    for phrase in _AUTHOR_LAYER_PHRASES:
        assert phrase not in joined, (
            f"「{case}」拿作者层那句「{phrase}」当理由 —— 照它去改的是一条没错的行:"
            f"\n{joined}"
        )
    assert "qi" in joined or "插件" in joined, joined


# ── ⑦ 外部进程产得出 `conversation` 事件吗:产得出,只是名字不叫 close ──────────


def test_脚本产得出conversation事件_而订它的触发器真的响(tmp_path):
    """⚪ **第二波 ⑦ 的答案:不是缺出口,是叫错了名字。**

    调度台按"先 `chat` 再 `close_conversation`"的直觉写脚本,卡在这儿:
    `chat()` 之后 `conversations()` 答 `[]`、`close_conversation` 要一个**还不存在**
    的 id,于是订 `conversation` 的触发器那一趟没法验。

    真相是 `chat()` **有意不建会话行** —— 它是流式吐字,完整转录归宿主
    (README 第一条)。**建行 + 关行 + 发那条 `conversation` 事件的是
    `record_chat_turn()`**,它就是"这一轮结束了"的提交口。
    `close_conversation(id)` 是给**自己管着会话**的宿主用的(网站那种)。

    这条用例把整条链敲一遍:`chat` → `record_chat_turn` → `conversation` 事件 →
    订它的触发器真的落笔。
    """
    fame = {"id": "onair", "version": "1.0.0",
            "facts": {"人气": {"bearer": "actor", "shape": "number",
                               "default": 20.0, "range": [0, 100],
                               "visibility": "self"}},
            "triggers": [{"id": "上过节目", "on": {"event": "conversation"},
                          "effects": [{"set": {
                              "onair.人气": "clamp(onair.人气 + 5, 0, 100)"}}]}]}
    path = write_seed_file(tmp_path / "onair.cyberworld",
                           {**BARE, "plugins": [fame]})
    with open_world_at(str(tmp_path / "onair.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.player_move("p1", "cafe", display_name="阿宇")
        list(world.chat("阿岚", [{"role": "user", "content": "你好"}],
                        player_id="p1", display_name="阿宇"))
        # ⚠️ **这一格是有意的,不是 bug**:流式那一趟不建会话行。
        assert world.conversations("阿岚") == [], (
            "`chat()` 建了会话行 —— 那 README 那句「完整转录归你的应用管」就变了"
        )
        cid = world.record_chat_turn("阿岚", "p1", [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ])
        assert isinstance(cid, int)
        rows = world.conversations("阿岚")
        assert [r["status"] for r in rows] == ["closed"], rows
        types = [getattr(e, "type", "") for e in world.scheduler.event_log.replay()]
        assert types.count("conversation") == 1, (
            "「整场只发这一条,在关闭时」—— 而这一趟一条都没发"
        )
        world.tick(1)
        assert world.stocks("agent:阿岚")["onair.人气"] == 25.0, (
            "订 `conversation` 的触发器没响 —— 而事件确实发了"
        )
        stats = world.trigger_stats()["onair.上过节目"]
        assert stats["matched"] == 1 and stats["written"] == 1, stats


# ── ⑧ 一个「一个数都没有」的东西,她照样看得见(第二波 ⑧)──────────────────────
#
# 创作台验收 C 在真引擎上撞的:`group:` 种类的实例与角色同处一地,而她的工具块里
# **整条不出现**,于是组织类插件的动词谁也调不动 —— 而 `ontology` / `validate` /
# 工作台**三处都说那个动词存在**,`World.act` 直调也真的通。
#
# 🔴 **而病灶不是 `group:` 那个前缀**(这一条实测更正了诊断):`group` 那个记号
# 在感知那一路上根本没有被过滤。真正的那一行是 `perception.perceive` 里的
# `if not seen: continue` —— 而那一块带的不只是量,还有**名字、gloss 和动词**,
# 那三样一个都不依赖量。**一个身上没有数的东西(门派、组织、一块牌子)于是整条
# 不存在**,连带它身上的动词她永远看不见。

_GROUP_PLUGIN = {
    "id": "mp", "version": "1.0.0", "label": "门派",
    "kinds": {"group:sect": {"gloss": "一个门派", "budget": 5},
              "entity:tape": {"gloss": "一盒磁带", "budget": 5, "facts": {
                  "录满": {"shape": "number", "default": 0.0,
                           "visibility": "here", "label": "录了多少"}}}},
    "edges": {"member_of": {"from": "agent", "to": "group:sect"}},
    "verbs": {"拜入": {"target": "group:sect", "label": "拜入",
                       "description": "拜入这个门派",
                       "effects": [{"link": {"type": "mp.member_of",
                                             "from": "self", "to": "target"}}]},
              "听一面": {"target": "entity:tape", "label": "听一面",
                         "description": "听这盒带子",
                         "set": {"录满": "clamp(录满 - 0.1, 0, 1)"}}},
}
_GROUP_SEED = {
    "agents": [{"id": "yu", "name": "露", "location": "obsdeck",
                "personality": "安静"}],
    "locations": [{"id": "obsdeck", "name": "观景台", "description": "风大"}],
    "entities": [{"id": "mp.sect:青云门", "name": "青云门", "location": "obsdeck"},
                 {"id": "mp.tape:一号带", "name": "一号带", "location": "obsdeck"}],
}


@pytest.mark.parametrize("with_facts", [True, False],
                         ids=["门派身上有一个量", "门派身上一个量都没有"])
def test_同地点的group实例_动词进得了她的提示词(tmp_path, with_facts):
    """🔴 **两种都要过**,而**第二种正是那个 bug**:一个组织身上本来就可能一个数
    都没有,而从前那种东西**整条不进提示词** —— 三处说那个动词存在,
    却没有一个角色到得了它。

    ⚠️ 第一种是对照组:没有它,一个"把 group 整个放行"的实现同样成立,而真正的
    分界是**有没有她够得着的东西**,不是前缀。
    """
    plugin = json.loads(json.dumps(_GROUP_PLUGIN, ensure_ascii=False))
    if with_facts:
        plugin["kinds"]["group:sect"]["facts"] = {
            "声望": {"shape": "number", "default": 0.0, "visibility": "here",
                     "label": "声望"}}
    path = write_seed_file(tmp_path / f"g{int(with_facts)}.cyberworld",
                           {**_GROUP_SEED, "plugins": [plugin]})
    with open_world_at(str(tmp_path / f"g{int(with_facts)}.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.tick(2)
        text = str(world.debug_prompt("yu"))
        assert "青云门" in text, "同处一地的门派整条不在她的提示词里"
        assert "拜入" in text, (
            "门派在提示词里,而它身上的动词不在 —— 那她照样调不动它"
        )
        assert "一号带" in text and "听一面" in text, "`entity:` 那一半也不许掉"
        # **她够得着**:直调那条路本来就通,这里钉的是两条路说同一句话。
        out = world.act("yu", "interact",
                        {"target": "mp.sect:青云门", "verb": "拜入"})
        assert out["ok"] is True, out
        assert world.scheduler.edge_store.all("mp.member_of") == [
            ("agent:yu", "mp.sect:青云门", {})
        ]


def test_既没有量也没有动词的东西_照旧不进提示词(tmp_path):
    """**判据是「有没有她够得着的东西」,不是「有没有数」** —— 两样都没有的照旧
    跳过。放开成"只要在场就进"的话,一堆纯摆设会白占她的提示词预算,
    而「没声明 = 感知不到」那条默认值就从背面被拆掉了。"""
    plugin = json.loads(json.dumps(_GROUP_PLUGIN, ensure_ascii=False))
    plugin["kinds"]["group:sect"] = {"gloss": "一个门派", "budget": 5}
    plugin["verbs"].pop("拜入")
    plugin["edges"] = {}
    path = write_seed_file(tmp_path / "bare.cyberworld",
                           {**_GROUP_SEED, "plugins": [plugin]})
    with open_world_at(str(tmp_path / "bare.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.tick(2)
        assert "青云门" not in str(world.debug_prompt("yu")), (
            "一个既没有量也没有动词的东西白占了她的提示词"
        )


# ── 第三波:一次放行开出来的三个洞,和三句念不通的话 ──────────────────────────

_W3_SEED = {
    "agents": [{"id": "yu", "name": "露", "location": "cafe", "personality": "安静"}],
    "locations": [{"id": "cafe", "name": "咖啡馆", "description": "临海的小店"},
                  {"id": "pier", "name": "码头", "description": "风很大"}],
}


def _w3(tmp_path, name, seed):
    path = write_seed_file(tmp_path / f"{name}.cyberworld", seed)
    payload = json.loads(run_cli("validate", "world", str(path), "--json").stdout)
    return path, payload


def _wet(spell):
    return {"id": "wet", "version": "1.0.0",
            "facts": {"潮位": {"bearer": "world", "shape": "number", "default": 0.8},
                      "身上": {"bearer": "actor", "shape": "number", "default": 0.0}},
            "triggers": [{"id": "淋雨", "on": {"event": "travel"},
                          "when": [f"{spell} > 0.3"],
                          "effects": [{"set": {"wet.身上": "2"}}]}]}


def test_触发器读得到自己挂在world上的事实(tmp_path):
    """🟠 **第三波 A1**:② 只治了一半 —— 插件**自己**挂在 `world` 上的事实,
    在它自己的触发器 `when` 里三种写法全不通:`world_wet.潮位` **开不了机**,
    而拒绝语说「读了别的插件的 …,要读就写进 reads」;照着改,下一句变成
    「这个世界里没有装 `world_wet` 这个插件」——**一条把作者引进死胡同的拒绝语**。

    正确的写法就是 `world_<插件>.<事实>`(和规律那一层读全局量逐字同一个写法),
    而这条用例把它敲通:触发器真响、身上真被写。
    """
    from anima_world.actions import ActionDescriptor

    path, payload = _w3(tmp_path, "w3a1", {**_W3_SEED, "plugins": [_wet("world_wet.潮位")]})
    assert payload["valid"] is True, payload["errors"]
    with open_world_at(str(tmp_path / "w3a1.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.scheduler._start_journey(world.scheduler.agents["yu"].agent,
                                       ActionDescriptor("walk", {"location": "pier"}))
        world.tick(1)
        assert world.stocks("agent:yu")["wet.身上"] == 2.0, (
            "读自己挂在 world 上的事实,触发器一次没响"
        )
        assert world.trigger_stats()["wet.淋雨"]["errors"] == 0


def test_读自己挂在world上的事实却漏了world前缀_装载期就拦(tmp_path):
    """**从前这一支装载期全绿、运行期每来一条事件炸一次**,而声明面完全正常。
    现在当场拦,并且**把该写的那串给他**。"""
    _path, payload = _w3(tmp_path, "w3a1b", {**_W3_SEED, "plugins": [_wet("wet.潮位")]})
    assert payload["valid"] is False, "它在运行期每次都炸,而两扇门说绿"
    joined = "\n".join(payload["errors"])
    assert "挂在 `world` 上" in joined and "world_wet.潮位" in joined, joined


def test_动词读一个不存在的插件_装载期就拦_而且说插件的理由(tmp_path):
    """🟡 **第三波 A2**:① 那次放行只补了「键」那一半,**表达式里的名字**没补 ——
    `requires: ["me_qi.灵力 > 0"]` 而 `qi` 根本不存在,从前**开机绿**、运行期每一次
    `ok: False`。作者拿到的是「开机绿、动词永远调不动」,那比开不了机坏得多。
    """
    forge = {"id": "forge", "version": "1.0.0",
             "facts": {"精神": {"bearer": "actor", "shape": "number", "default": 50.0}},
             "kinds": {"entity:炉": {"gloss": "一座炉", "facts": {
                 "火": {"shape": "number", "default": 1.0, "visibility": "here"}}}},
             "verbs": {"锻": {"target": "entity:炉", "description": "打一炉",
                              "costs": {"forge.精神": "me_forge.精神 - 10"},
                              "requires": ["me_qi.灵力 > 0"]}}}
    _path, payload = _w3(tmp_path, "w3a2", {
        **_W3_SEED,
        "entities": [{"id": "forge.炉:一号", "name": "老炉", "location": "cafe"}],
        "plugins": [forge]})
    assert payload["valid"] is False
    joined = "\n".join(payload["errors"])
    # ⚠️ **这一句必须一口气说完两件事**,否则作者会走进和 A1 一样的死胡同:
    # 照「写进 reads」改完,下一句变成「这个世界里没有装 `qi` 这个插件」。
    assert "`reads` 里没有它" in joined, joined
    assert "那个插件也得真的在这个世界里" in joined, joined
    for phrase in _AUTHOR_LAYER_PHRASES:
        assert phrase not in joined, joined

    # 自己的命名空间、而事实没声明过 —— 同样当场拦(这一半是插件层判的)。
    typo = json.loads(json.dumps(forge, ensure_ascii=False))
    typo["verbs"]["锻"]["requires"] = ["me_forge.没这个 > 0"]
    _path, payload = _w3(tmp_path, "w3a2b", {
        **_W3_SEED,
        "entities": [{"id": "forge.炉:一号", "name": "老炉", "location": "cafe"}],
        "plugins": [typo]})
    assert payload["valid"] is False
    assert "顶层 `facts` 里没有" in "\n".join(payload["errors"]), payload["errors"]


def test_一个插件都没有的世界_作者层写带命名空间的名字照旧拒(tmp_path):
    """🟡 **第三波 A3**:那次放行是**只认形状**的,于是它把作者层也放开了 ——
    一个**没有插件**的世界里 `set: {"qi.灵力": …}` 一路绿到底,而那个量没有一处
    声明过。判据现在是**这个命名空间在这个世界里有人声明过**。"""
    _path, payload = _w3(tmp_path, "w3a3", {
        **_W3_SEED,
        "kinds": [{"id": "tree",
                   "quantities": {"树高": {"default": 1.0, "visibility": "here"}},
                   "affordances": {"照料": {"set": {"qi.灵力": "1"}}}}],
        "entities": [{"id": "tree:oak", "name": "橡树", "location": "cafe"}]})
    assert payload["valid"] is False, "一个插件都没有的世界收下了 `qi.灵力`"
    assert "没有哪个插件叫 `qi`" in "\n".join(payload["errors"]), payload["errors"]


def test_拒绝语里的me_插件_事实_念得通(tmp_path):
    """🟡 **第三波 B4**,而这条正是 §3.52 叫作者改去走的那条路。

    `requires: ["me_mana.魔力 >= 5"]` 的拒绝语从前被绞成「你的mana 5」——
    `.魔力` 和 `>=` 一起被吃掉。病根是 `ast.walk` 把 `Attribute` 和它里面那个
    `Name` **各换了一次**,两处 span 重叠,而替换是从后往前盲改的。
    **一句念不通的拒绝语和一句错的一样贵**:他会去找一样屏幕上不存在的东西。
    """
    mana = {"id": "mana", "version": "1.0.0",
            "facts": {"魔力": {"bearer": "actor", "shape": "number", "default": 1.0,
                               "visibility": "self", "label": "魔力"}},
            "kinds": {"entity:杖": {"gloss": "一根杖", "facts": {
                "光": {"shape": "number", "default": 0.0, "visibility": "here"}}}},
            "verbs": {"施法": {"target": "entity:杖", "description": "举杖",
                               "requires": ["me_mana.魔力 >= 5"],
                               "set": {"光": "光 + 1"}}}}
    path, payload = _w3(tmp_path, "w3b4", {
        **_W3_SEED,
        "entities": [{"id": "mana.杖:一号", "name": "旧杖", "location": "cafe"}],
        "plugins": [mana]})
    assert payload["valid"] is True, payload["errors"]
    with open_world_at(str(tmp_path / "w3b4.db"), world_file=path,
                       force_mock_llm=True) as world:
        out = world.act("yu", "interact",
                        {"target": "mana.杖:一号", "verb": "施法"})
        said = out["detail"]["refusal"]
        assert "魔力" in said and ">=" in said, f"念不通:{said}"
        assert "mana" not in said, f"命名空间漏给玩家了:{said}"


def test_读别人的事实要写reads_动词这条路也不例外(tmp_path):
    """⚪ **第三波 B5 的裁决**:`reads` 在动词表达式这条路上**承重**。

    REFERENCE §10.2 把「读别人的要 `reads`」写成**这一层的边界**,而动词这条路
    从前整个绕过它 —— **一条写在文档里而某条路不守的边界,比没有这条边界更坏**:
    读的人会以为它守着。

    ⚠️ 配方也在这条用例里敲了一遍(`requires` **只准读 `me_*`**,前缀不能省)。
    """
    mana = {"id": "mana", "version": "1.0.0",
            "facts": {"魔力": {"bearer": "actor", "shape": "number", "default": 9.0,
                               "visibility": "self", "label": "魔力"}}}

    def shop(reads):
        body = {"id": "shop", "version": "1.0.0",
                "kinds": {"entity:柜": {"gloss": "一个柜台", "facts": {
                    "存货": {"shape": "number", "default": 3.0,
                             "visibility": "here"}}}},
                "verbs": {"买一件": {"target": "entity:柜", "description": "买一件",
                                     "requires": ["me_mana.魔力 >= 5"],
                                     "set": {"存货": "存货 - 1"}}}}
        if reads:
            body["reads"] = ["mana.魔力"]
        return body

    seed = {**_W3_SEED,
            "entities": [{"id": "shop.柜:一号", "name": "柜台", "location": "cafe"}]}
    _path, payload = _w3(tmp_path, "w3b5a", {**seed, "plugins": [mana, shop(False)]})
    assert payload["valid"] is False, "没写 `reads` 也读得动别人的事实"
    joined = "\n".join(payload["errors"])
    assert "`reads` 里没有它" in joined and "mana.魔力" in joined, joined

    path, payload = _w3(tmp_path, "w3b5b", {**seed, "plugins": [mana, shop(True)]})
    assert payload["valid"] is True, payload["errors"]
    with open_world_at(str(tmp_path / "w3b5b.db"), world_file=path,
                       force_mock_llm=True) as world:
        out = world.act("yu", "interact",
                        {"target": "shop.柜:一号", "verb": "买一件"})
        assert out["ok"] is True, out          # 9 >= 5,门开着
        assert world.scheduler.stock_store.snapshot(
            "shop.柜:一号")["存货"][0] == 2.0


def test_规律的emit_when写成列表_说的是形状不是空(tmp_path):
    """🟡 **第三波 C1**:从前报「表达式是空的」—— 那句话在说一个**他没写错的
    地方**(他明明写了内容),于是他会去改表达式本身。`emit.when` 是**一句**,
    规律自己的 `when` 是**一列**。"""
    qi = {"id": "qi", "version": "1.0.0",
          "facts": {"灵力": {"bearer": "actor", "shape": "number", "default": 1.0}},
          "rules": [{"id": "喊", "for_each": {"kind": "agent"},
                     "set": {"qi.灵力": "qi.灵力"},
                     "emit": [{"type": "qi.累", "when": ["qi.灵力 < 1"],
                               "importance": 0.5, "text": "他喘了口气"}]}]}
    _path, payload = _w3(tmp_path, "w3c1", {**_W3_SEED, "plugins": [qi]})
    assert payload["valid"] is False
    joined = "\n".join(payload["errors"])
    assert "要写成**一句**表达式" in joined and "规律自己的" in joined, joined
    assert "表达式是空的" not in joined, joined


# ── 第三波那次修法自己开出来的两个洞(2026-08-28,A 视角 FAIL 排回来的)──────────
#
# 两条都是**回归**:上一波真的能跑的写法,被这一波的新闸拦下了。
# 记在一起是因为它们是同一种错的两面:**一份判断有两个副本 / 一份名单和它要判的
# 那份数据来自两次不同的合并。**

_W3_WET = {
    "id": "wet", "version": "1.0.0",
    "facts": {"潮位": {"bearer": "world", "shape": "number", "default": 0.8},
              # ⚠️ **这一格是给下面那条增量编辑用例当牙的**:作者层的 `kinds`
              # 里有一条能力扣它(`costs: {"wet.体感": …}`),而那条能力**跟着
              # 库里的 `:kinds` 走**。开机重解析时,名单要是只取"这次编辑那份
              # 文件"就找不到 `wet` 这个命名空间 —— 问题 2 的触发条件正是它。
              "体感": {"bearer": "actor", "shape": "number", "default": 5.0,
                       "visibility": "self", "label": "体感"}},
    "kinds": {"entity:桶": {"gloss": "一个桶", "facts": {
        "水": {"shape": "number", "default": 0.0, "visibility": "here"}}}},
    "verbs": {"接水": {"target": "entity:桶", "description": "接一桶雨水",
                       "when": ["world_wet.潮位 > 0.3"],
                       "set": {"水": "水 + world_wet.潮位"}}},
}


#: 🔴 **作者层写着插件的事实的那一行** —— 它住在库里的 `:kinds` 上,
#: 而增量编辑那份文件里一个字都没有它。**问题 2 的牙就在这儿**:
#: 把开机那份名单退回"只取文件",这一行当场解析不了,而作者没碰过它。
_W3_TREE = {
    "id": "tree",
    "quantities": {"树高": {"default": 1.0, "visibility": "here"}},
    "affordances": {"照料": {"set": {"树高": "树高 + 0.1"},
                             "requires": ["me_wet.体感 >= 1"],
                             "costs": {"wet.体感": "me_wet.体感 - 1"}}},
}


def _wet_world(tmp_path, name="wetv"):
    seed = {**BARE, "plugins": [_W3_WET], "kinds": [dict(_W3_TREE)],
            "entities": [{"id": "wet.桶:一号", "name": "旧桶", "location": "cafe"},
                         {"id": "tree:oak", "name": "橡树", "location": "cafe"}]}
    path = write_seed_file(tmp_path / f"{name}.cyberworld", seed)
    return path, open_world_at(str(tmp_path / f"{name}.db"), world_file=path,
                               force_mock_llm=True)


def test_动词读得到自己挂在world上的事实(tmp_path):
    """🔴 **回归 + 死胡同复活**(A 视角 FAIL 第 1 条)。

    A1 只在**读的那道闸**里剥了 `world_`,而同一轮**我自己新加的**「动词表达式
    名字闸」没剥 —— 于是同一句 `world_wet.潮位` 写在触发器里过得去、写在动词里
    被判成「读了别的插件的 …,写进 reads」,照着改下一句变成
    「没有装 `world_wet` 这个插件」。**同一条死胡同我在两处各修过一次,
    而第二处是我自己开的:剥前缀这件事只该有一份判断。**
    """
    path, world = _wet_world(tmp_path)
    payload = json.loads(run_cli("validate", "world", str(path), "--json").stdout)
    assert payload["valid"] is True, payload["errors"]
    with world:
        out = world.act("阿岚", "interact", {"target": "wet.桶:一号", "verb": "接水"})
        assert out["ok"] is True, out
        assert world.scheduler.stock_store.snapshot("wet.桶:一号")["水"][0] == 0.8, (
            "`when` 与 `set` 里那个 `world_wet.潮位` 没读到世界身上那个量"
        )


def test_插件世界之后的增量编辑_只带kinds也开得了机(tmp_path):
    """🔴 **回归**(A 视角 FAIL 第 2 条),而它直接打在创作台的增量编辑流程上。

    一个建好的插件世界,之后做**任何**带 `kinds` 的增量编辑(哪怕新种类和插件
    毫无关系)开不了机 —— 报的是作者**没碰过**的那一行,还断言「装着的是
    (一个都没有)」,而那个插件明明就在库里。

    病根:喂给本体那一层的 `kinds` 是**库合并之后的全集**,而那份「有哪些命名空间」
    的名单只取了**文件**那一份。**两份东西必须来自同一次合并** —— 这正是
    `_seed_ontology` 自己 docstring 反对的那件事(喂全集、判局部)。
    ⚠️ 「让作者在编辑文件里把 plugins 段抄一遍」**不算修**:那是让他维护一份
    迟早会不一致的抄件,而作者层「只填缺不覆盖」当初就是为了不逼他抄。
    """
    _path, world = _wet_world(tmp_path, name="wedit")
    world.close()
    edit = write_seed_file(tmp_path / "edit.cyberworld", {"kinds": [
        {"id": "bench", "quantities": {"油漆": {"default": 1.0,
                                                "visibility": "here"}}}]})
    with open_world_at(str(tmp_path / "wedit.db"), world_file=edit,
                       force_mock_llm=True) as again:
        kinds = sorted(again.scheduler.ontology.kinds)
        assert "bench" in kinds, f"这次编辑什么都没装进去:{kinds}"
        assert "wet.桶" in kinds, (
            f"插件的种类在这一趟里丢了 —— 名单和数据又不是同一次合并的:{kinds}"
        )
        assert "tree" in kinds, kinds
        # 编辑之后两条路都照旧点得动:插件自己的动词,以及**作者层那条扣插件事实的
        # 能力** —— 后者是这条用例的牙(名单退回"只取文件"时它当场解析不了,
        # 而作者这次连碰都没碰它)。
        out = again.act("阿岚", "interact", {"target": "wet.桶:一号", "verb": "接水"})
        assert out["ok"] is True, out
        tended = again.act("阿岚", "interact", {"target": "tree:oak", "verb": "照料"})
        assert tended["ok"] is True, tended
        assert again.stocks("agent:阿岚")["wet.体感"] == 4.0, again.stocks("agent:阿岚")


def test_开机按库里那份跑_连声明带规律_而不只是库里那一行(tmp_path):
    """🔴 **验收 A ① 逮的那条:3.11.2 那句话只做进了半层。**

    `install_plugins` 的 `on_boot` 跳过筛的是**它自己那张局部名单** —— 它挡住的
    只有「别拿旧声明覆盖库里那一行」。而**运行时装的是哪一份**由
    `_plugin_bodies` 决定,那一处照旧「文件里那份赢」。下场是一个只有真部署
    才有的裂口:

        库里记着 2.0.0 · `scheduler.plugins` 跑的是 1.0.0 ·
        2.0.0 那几条规律一条都不在跑 · 而日志说「库里那份说了算」

    **两处各说各的,而屏幕上什么都不少。** 上一条用例只断言了库里那一行,
    所以它一直是绿的 —— 这条断言**运行时**。
    """
    NEW = {**QI, "version": "2.0.0",
           # ⚠️ 2.0.0 才有的那条 `projected` 事实 —— A 报的现场里
           # `projected_facts` 是空的,而那正是"跑的是旧那份"的指纹之一。
           "facts": {**QI["facts"],
                     "香火": {"bearer": "actor", "shape": "number",
                              "mode": "projected",
                              "sources": [{"event": "payment", "credit": "amount"}]}},
           "rules": [{"id": "回气2", "every": {"ticks": 1},
                      "for_each": {"kind": "agent"},
                      "set": {"qi.灵力": "clamp(qi.灵力 + 2.0 * dt, 0, 100)"}}]}
    with _world_with(tmp_path, NEW, name="rt"):
        pass
    old_file = write_seed_file(tmp_path / "rt2.cyberworld",
                               {**BARE, "plugins": [{**QI, "version": "1.0.0"}]})
    world = open_world_at(str(tmp_path / "rt.db"), world_file=old_file,
                          force_mock_llm=True)
    try:
        running = {p.id: p.version for p in world.scheduler.plugins}
        assert running.get("qi") == "2.0.0", (
            f"库里是 2.0.0,而跑起来的是 {running.get('qi')} —— "
            "「库里那份说了算」只做进了记录那一层")
        # 2.0.0 那条规律真的在跑(1.0.0 那条叫「回气」,2.0.0 那条叫「回气2」)
        ids = set(world.scheduler.plugin_rule_ids)
        assert "qi.回气2" in ids, sorted(ids)      # 规律 id 带命名空间前缀
        assert "qi.回气" not in ids, sorted(ids)
        # 事实也是按库里那份投影的
        # 2.0.0 声明的那条 `projected` 事实真的在册(跑旧那份时它是空的)
        assert any("香火" in str(k) for k in world.scheduler.projected_facts), (
            f"projected_facts 里没有 2.0.0 那条:{world.scheduler.projected_facts}")
    finally:
        world.close()
