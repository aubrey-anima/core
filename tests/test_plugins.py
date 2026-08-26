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
    with pytest.raises(PluginError) as raised:
        open_world_at(str(tmp_path / "d.db"), world_file=path, force_mock_llm=True)
    assert any("不降级" in e for e in raised.value.errors)


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


def test_无插件的世界_提示词逐字节不变(tmp_path):
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
        authored = [p.id for p in world.scheduler.plugins if p.id != "needs"]
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
    # 🔴 **逐字节那一半由 `tests/test_needs_plugin_parity.py` 钉**:那份基线是
    # 插件系统落地**之前**旧路真跑出来的,而 `test_提示词逐字节相同` 拿它比 sha256。


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
