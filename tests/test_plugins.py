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
        assert [p.id for p in world.scheduler.plugins] == ["qi"]
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


def test_不降级(tmp_path):
    with _world_with(tmp_path, {**QI, "version": "2.0.0"}, name="d"):
        pass
    path = write_seed_file(tmp_path / "d2.cyberworld",
                           {**BARE, "plugins": [{**QI, "version": "1.0.0"}]})
    # ⚠️ 开机路上它被包成 `WorldSeedError` —— **和 `OntologyError` 同一类**:
    # 作者写错了东西,而作者该看到的是那几行中文,不是一段 Python 堆栈
    # (2026-08-26 验收 C:一次被拒的降级,屏幕先甩 `Traceback …` 才轮到中文)。
    from anima_world.world_seed import WorldSeedError

    with pytest.raises(WorldSeedError) as raised:
        open_world_at(str(tmp_path / "d.db"), world_file=path, force_mock_llm=True)
    assert "不降级" in str(raised.value)


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
        assert world.scheduler.plugins == [], "卸完再开机它又活了"
        assert ("agent", "qi.灵力") not in world.scheduler.visibility_store.rules_map()


def test_cli_plugin_list(tmp_path):
    with _world_with(tmp_path, QI, name="l"):
        pass
    out = run_cli("plugin", "list", "--world-id", "w", "--json")
    assert out.returncode == 0, out.stderr
    rows = json.loads(out.stdout)["plugins"]
    assert [r["id"] for r in rows] == ["qi"]
    assert rows[0]["version"] == "1.0.0" and rows[0]["facts"] == ["灵力"]
    assert rows[0]["rules"] == 1 and rows[0]["triggers"] == 1
    assert rows[0]["order"] == 0, "装载顺序也是答案的一部分"

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
        authored = [p.id for p in world.scheduler.plugins
                    if p.id not in ("needs", "economy")]
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


def test_边规律写emit_当场拒而不是静默无效():
    """🔴 **B 逮到的那条**:契约里 `effects` 含 `emit`、`rule_selectors` 含 `edge`,
    **没有一格说这个组合不成立** —— 而 `_evaluate_edge_rules` 一条 emit 都不发,
    开机不拦、零 warning。

    对照组是我自己在 `projected` 上写的那两条限制(加载期拒 + 说得出为什么)。
    **将来要支持是加法,今天静默不支持是撒谎。**
    """
    bad = {**MENPAI, "rules": [{
        "id": "熬资历", "every": {"ticks": 1}, "for_each": {"edge": "member_of"},
        "set": {"menpai.资历": "edge.menpai.资历 + 1"},
        "emit": [{"type": "menpai.熬出头了", "when": "edge.menpai.资历 > 10"}]}]}
    with pytest.raises(PluginError) as raised:
        parse_plugins([bad])
    joined = "\n".join(raised.value.errors)
    assert "emit" in joined and "set" in joined, joined


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
    row = json.loads(out.stdout)["plugins"][0]
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


def test_钱的进位跟着账本走_不是折到第六位(tmp_path):
    """🔴 **`projected` 事实要能声明自己的进位,而钱是它的第一个使用者。**

    `_apply_payment` 的 docstring 逐字写着为什么账本折到**两位**:二进制浮点存不下
    0.1,`60 − 5.23 − 1.16 − …` 折下来是 `0.3799999999999921`;门禁读的是这个数,
    一笔"正好够"的交易迟早会被它拒掉,**而那一次不报错也不留痕**。

    折叠端默认折到六位。**两套进位就是两个钱包**,而它们只在小数第三位往后分家 ——
    那正是"看着一样、判起来不一样"的形状。
    """
    from anima_world.projection import fact_source_updates

    specs = [{"event": "payment", "amount": "amount", "credit": "to",
              "fact": "w.钱", "owner_form": "actor", "round": 2}]
    got = fact_source_updates(specs, {"to": "夏", "amount": 0.1 + 0.2})
    assert got == [("agent:夏", "w.钱", 0.3)], got


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
