"""线索:一个「知道 / 不知道」的东西(玩法层批 3c §2.4,3.13.0)。

**零新原语** —— 一条线索是一个实体,「他知不知道」是一条边。
这个文件钉三条纪律,每条都是可验的(见 `anima_world/clues.py` 的模块 docstring)。
"""
from __future__ import annotations

import pytest

from _worldfile import open_world_at, write_seed_file

#: 🔴 **哨兵**:未解锁那条线索的正文。它**不许出现在任何一扇只读门的输出里**。
SECRET = "路明非其实是龙王"

_WORLD = {
    "agents": [{"id": "阿岚", "name": "阿岚", "location": "cafe",
                "personality": "安静"}],
    "locations": [{"id": "cafe", "name": "咖啡馆", "description": "小店"}],
    "kinds": [{"id": "clue", "gloss": "一条线索",
               "affordances": {"look": {}}}],
    "entities": [
        {"id": "clue:老橡树的来历", "name": "老橡树的来历", "location": "cafe",
         "gloss": "夏说这棵树是她外婆种的。"},
        {"id": "clue:昂热知道的那件事", "name": "昂热知道的那件事",
         "location": "cafe", "gloss": SECRET},
        {"id": "clue:第三条", "name": "第三条", "location": "cafe"},
    ],
}


def _world(tmp_path, name="c"):
    path = write_seed_file(tmp_path / f"{name}.cyberworld", _WORLD)
    world = open_world_at(str(tmp_path / f"{name}.db"), world_file=path,
                          force_mock_llm=True)
    world.config_set("clues.enabled", True)
    return world


def test_玩家在图上只有一个形状_写的人和读的人不许各写各的(tmp_path):
    """🔴 **验收 A ② 实测出来的**:边连上了,而板子读不到。

    这个引擎里玩家有两个形状,**各管一头**(`beats.bind_player` 逐字):
    量表按 `agent:player:<id>` 存,而关系 / 账本 / 库存 / 事件顶层的 `who` /
    在场位置一律是 `player:<id>`。**边属于前者**
    (`plugins.EDGE_NODE_ID_FORMS["player"]`,那一行的注释就写着「最容易写错」)。

    上一版 `_apply_verb_edges` 写 `agent:player:p1`、`player_clues` 读 `player:p1`
    —— **写的人和读的人错成了两个样子**,而两边都不报错:
    `link` 返回 True、日志一条不少,板子上是 0。
    ⚠️ 而夹具当时直接调 `apply_edge_effect` 传了个 `"player:p1"` 字面量,
    于是**它和读的那一半错成了同一个样子**,一条不红。

    现在规范化只有一处(`Scheduler._edge_node`),这条闸钉两件:
    ① 两种写法进去都落在**同一个节点**上;② 板子读的就是那个节点。
    """
    from anima_world.plugins import EDGE_NODE_ID_FORMS
    from anima_world.scheduler import Scheduler

    assert EDGE_NODE_ID_FORMS["player"] == "agent:player:<player_id>", (
        "契约里玩家节点的形状换了 —— 这条闸和 `_edge_node` 都照它写")
    assert Scheduler._edge_node("player:p1") == "agent:player:p1"
    assert Scheduler._edge_node("agent:player:p1") == "agent:player:p1"
    assert Scheduler._edge_node("夏") == "夏", "别把角色 id 也改写了"

    with _world(tmp_path, "node") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        edge = f"{clues_edge()}"
        # 两种写法各连一次 —— 落在同一个节点上,所以第二次是同一条边
        world.scheduler.apply_edge_effect(
            {"type": edge, "from": "player:p1", "to": "clue:老橡树的来历"}, {})
        world.scheduler.apply_edge_effect(
            {"type": edge, "from": "agent:player:p1", "to": "clue:第三条"}, {})
        rows = world.scheduler.edge_store.of_src(edge, "agent:player:p1")
        board = world.player_clues("p1")

    assert len(rows) == 2, f"两种写法落在了两个节点上:{rows}"
    assert board["known"] == 2, (
        f"边连上了而板子读不到 —— 写的人和读的人又各写各的:{board}")


def clues_edge() -> str:
    from anima_world import clues as C

    return f"{C.PLUGIN_ID}.{C.KNOWS_EDGE}"


def test_线索板只报存在与状态_不报内容(tmp_path):
    """🔴 **纪律 1**(设计 §5 那张表的原话)。

    ⚠️ 判据是**哨兵扫每一扇只读门** —— 不是"看看 board 里有没有 text 字段":
    内容可能从任何一格漏出去,而**漏了的那一格不会报错**。
    """
    with _world(tmp_path, "board") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        board = world.player_clues("p1")
        doors = [
            board, world.player_options("p1"), world.host_turn("p1"),
            world.player_story("p1"), world.player_perception("p1"),
        ]

    assert board["total"] == 3, board
    assert board["known"] == 0 and board["unknown"] == 3, board
    assert board["known_ids"] == [], (
        "一条都没解锁,却报得出 id —— 未解锁线索的 id 往往就是它的谜面")
    assert board["text"], "一句人话都没有"
    for door in doors:
        assert SECRET not in str(door), f"未解锁那条的正文漏出去了:{door}"
        assert "昂热知道的那件事" not in str(door), (
            f"未解锁那条的**名字**也漏了(名字就是谜面):{door}")


#: 一个**作者写的**插件:线索上挂一个动词,做完就知道了这条线索。
#: 🔴 **`clues.knows` 是出厂插件声明的边**,而这份插件叫 `mystery` ——
#: 上一版这种写法**当场被拒**(「只连得动自己声明的边」),于是
#: `contract.clues.unlock_paths` 里那条 `verb_effect` **一条真路都没有**。
_MYSTERY = {
    "id": "mystery", "version": "1.0.0", "label": "谜",
    "verbs": {"打听": {"target": "clue", "label": "打听",
                      "effects": [{"link": {"type": "clues.knows",
                                            "from": "self", "to": "target"}}]}},
}


def test_没人声明过的边_一条都不连(tmp_path):
    """🔴 **验收 A 整体 ③**:上一版 `apply_edge_effect` 对**谁都没声明过**的
    边类型照连 —— 一个拼错的 `type`、一个开关关着的出厂插件、一条早就删掉的边,
    全都安静地建成一条边,`link` 返回 True,**而没有任何一处读得到它**
    (读的人按声明去读)。

    ⚠️ 它还骗过了这个仓库自己的夹具:线索那两条用例当时直接调这个函数,
    于是 `clues.enabled=False`(**根本没有这种边**)那一趟也是 True ——
    **连插件装没装上都没验到**。
    """
    with _world(tmp_path, "undeclared") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        sch = world.scheduler
        assert "clues.knows" in sch.edge_types, "夹具没把线索装上,这条就白验了"
        # 拼错一个字 —— 谁都没声明过它
        got = sch.apply_edge_effect(
            {"op": "link", "type": "clues.know", "from": "player:p1",
             "to": "clue:第三条"}, {})
        assert got is False, "拼错的边类型也照连 —— 而没有任何一处读得到它"
        assert sch.edge_store.all("clues.know") == []

    # 开关关着的世界:那条边根本不在,照样一条都不连
    path = write_seed_file(tmp_path / "off2.cyberworld", _WORLD)
    with open_world_at(str(tmp_path / "off2.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        got = world.scheduler.apply_edge_effect(
            {"op": "link", "type": "clues.knows", "from": "player:p1",
             "to": "clue:第三条"}, {})
        assert got is False, (
            "`clues.enabled` 关着而 `link` 说它连上了 —— "
            "夹具照这个 True 写下去,插件装没装上就永远验不到")
        assert world.player_clues("p1")["known"] == 0


def test_解锁路一_动词effects_走真门连得上(tmp_path):
    """🔴 **验收 A 整体 ①:三条路一条都连不上**(3.13.0)。

    上一版这条用例直接调 `apply_edge_effect` 传字面量 —— 那**不是一条路**,
    那是绕过所有闸去手写一条边:插件装没装上、作者写不写得出这条效果、
    动词跑不跑得到这儿,**一件都没验到**。
    ⚠️ 而它当时是绿的,因为**它和读的那一半错成了同一个形状**(见上面那条)。

    现在走的是真门:作者的插件 → 玩家做那个动词 → 板子看得见。
    """
    path = write_seed_file(tmp_path / "verb.cyberworld",
                           {**_WORLD, "plugins": [dict(_MYSTERY)]})
    with open_world_at(str(tmp_path / "verb.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.config_set("clues.enabled", True)
        world.player_move("p1", "cafe")
        world.tick(2)
        got = world.scheduler.perform_affordance(
            "player:p1", "clue:老橡树的来历", "打听")
        board = world.player_clues("p1")

    assert got["ok"] is True, got
    assert got["edges"] == [{"op": "link", "type": "clues.knows", "ok": True}], (
        f"动词跑完了,而那条边一格没动:{got}")
    assert board["known"] == 1 and board["unknown"] == 2, board
    assert board["known_ids"] == ["clue:老橡树的来历"], board
    # 已知那条**可以**点名 —— 他本来就知道
    assert "老橡树的来历" in str(board)


def test_别的作者插件的边_照旧连不动(tmp_path):
    """放开的只是**出厂那几条公共边**,不是「谁的边都能连」。

    ⚠️ 两条要一起看:只写上面那条的话,把这道闸整个删掉也是绿的。
    """
    from anima_world.__main__ import world_plugin_errors

    bad = {**_MYSTERY, "verbs": {"打听": {
        "target": "clue", "label": "打听",
        "effects": [{"link": {"type": "sect.apprentice_of",
                              "from": "self", "to": "target"}}]}}}
    said = world_plugin_errors({"plugins": [bad]})
    assert any("apprentice_of" in line for line in said), (
        f"连别的作者插件的边也放行了 —— 那条规矩一个字都没松过:{said}")


def test_解锁路二_剧情拍的link_走真门连得上(tmp_path):
    """🔴 **第二条路上一版根本不存在**:`link` 不在 `beats.VALID_OPS` 上,
    一份这么写的世界**开机当场拒** —— 而契约里写着它是三条路之一。

    这一条走真门:作者写一条拍 → 世界跑到那个时刻 → 板子看得见。
    ⚠️ 顺带钉住 `ops_applied` —— 没连成的 `link` **不许**出现在那儿:
    一个"什么都没做"的 link 和一个"建成了"的 link 在日志上长得一样,
    而 `ops_applied` 是作者唯一读得到的回执。
    """
    beat = {"id": "开场", "for_each": {"node": "player"},
            "trigger": {"at": {"day": 0, "minute_of_day": 5}},
            "payload": [{"op": "link", "type": "clues.knows",
                         "from": "player", "to": "clue:老橡树的来历"}]}
    path = write_seed_file(tmp_path / "beat.cyberworld", {**_WORLD, "beats": [beat]})
    with open_world_at(str(tmp_path / "beat.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.config_set("clues.enabled", True)
        world.player_move("p1", "cafe")
        world.tick(8)
        fired = [e["payload"] for e in world.history(kind="beat_fired")["events"]]
        board = world.player_clues("p1")

    assert fired and fired[0]["ops_applied"] == ["link"], (
        f"这一拍响了,而那条边一格没动:{fired}")
    assert board["known"] == 1 and board["known_ids"] == ["clue:老橡树的来历"], board


def test_没装线索的世界_那条拍不假装自己连上了(tmp_path):
    """`clues.enabled` 关着时那条边**根本不存在** —— 这一格没连成,
    而 `ops_applied` 里**不许**有它。

    ⚠️ 这条闸拦的是「静默成功」:边没连、拍照旧 `mark_fired`,
    作者读到的回执说 `link` 干过了 —— 他会去别处找 bug。
    """
    beat = {"id": "开场", "for_each": {"node": "player"},
            "trigger": {"at": {"day": 0, "minute_of_day": 5}},
            "payload": [{"op": "link", "type": "clues.knows",
                         "from": "player", "to": "clue:老橡树的来历"}]}
    path = write_seed_file(tmp_path / "off.cyberworld", {**_WORLD, "beats": [beat]})
    with open_world_at(str(tmp_path / "off.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.player_move("p1", "cafe")
        world.tick(8)
        fired = [e["payload"] for e in world.history(kind="beat_fired")["events"]]

    assert fired, "拍没响,这条用例就什么都没验到"
    assert fired[0]["ops_applied"] == [], (
        f"边没连上,回执里却说 `link` 干过了:{fired[0]}")


def test_端点形状不合声明_一条都不连_而且回执不撒谎(tmp_path):
    """🔴 **A 末轮 ③**:上一版那道新闸只查「有没有人声明过这种边」,
    **不查这两个 id 配不配得上它声明的那两端**。

    于是一条拍里写 `{"from": "阿岚"}`(`clues.knows` 的起点声明的是 `player`)
    的 `link` **建得出来**:`ops_applied` 里有它、库里真有那一行 ——
    而板子读的是 `agent:player:<id>`,**那条边谁也读不到、谁也看不见**,
    零报错。**回执说成了,而世界里什么都没发生。**
    """
    beat = {"id": "开场", "for_each": {"node": "player"},
            "trigger": {"at": {"day": 0, "minute_of_day": 5}},
            # 🔴 起点写成一个角色 —— 而这条边的起点声明的是 `player`
            "payload": [{"op": "link", "type": "clues.knows",
                         "from": "阿岚", "to": "clue:老橡树的来历"}]}
    path = write_seed_file(tmp_path / "shape.cyberworld", {**_WORLD, "beats": [beat]})
    with open_world_at(str(tmp_path / "shape.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.config_set("clues.enabled", True)
        world.player_move("p1", "cafe")
        world.tick(8)
        fired = [e["payload"] for e in world.history(kind="beat_fired")["events"]]
        rows = world.scheduler.edge_store.all("clues.knows")
        board = world.player_clues("p1")

    assert fired, "拍没响,这条用例就什么都没验到"
    assert fired[0]["ops_applied"] == [], (
        f"端点形状不合声明,而回执说 `link` 干过了:{fired[0]}")
    assert rows == [], f"库里落了一条谁都读不到的边:{rows}"
    assert board["known"] == 0, board


def test_借来的种类那一端_照旧连得上(tmp_path):
    """上一条的另一半 —— **拦过头和漏掉一样坏,只是方向相反**。

    `clues.knows` 的终点声明的是 `entity:clue`,而 `clue` 那个种类是**作者写的**
    (实例 id 就是 `clue:<名>`,不是 `clues.clue:<名>`)。
    只按"插件自己声明的种类"去判的话,这条出厂边**一条都连不上**。
    """
    path = write_seed_file(tmp_path / "borrow.cyberworld",
                           {**_WORLD, "plugins": [dict(_MYSTERY)]})
    with open_world_at(str(tmp_path / "borrow.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.config_set("clues.enabled", True)
        world.player_move("p1", "cafe")
        world.tick(2)
        got = world.scheduler.perform_affordance(
            "player:p1", "clue:老橡树的来历", "打听")
        board = world.player_clues("p1")

    assert got["edges"] == [{"op": "link", "type": "clues.knows", "ok": True}], got
    assert board["known"] == 1, board


def test_契约点名了解锁那两条路_而且说得出不做哪条_为什么():
    """🔴 **纪律 2 / 3 写进契约**:一个等不来的东西会让人一直等着,
    而不是换个写法(和「作者层写不了 `confront`」同一课);
    而**报一个算不出来的档,就是让屏幕替引擎撒谎**。

    🔴 3.13.0 从三条收到两条,**收的那一版才是真的**:上一版那三条一条都连不上。
    **一个报得出、却走不通的取值,比不报它更坏** —— tool 正照着它写第 3 周的底稿。
    """
    from anima_world import clues as C
    from anima_world.__main__ import contract_payload

    seg = contract_payload()["clues"]
    assert seg["unlock_paths"] == list(C.UNLOCK_PATHS)
    assert set(seg["unlock_paths"]) == {"verb_effect", "beat_op"}, seg["unlock_paths"]
    assert "director_reveal" not in seg["unlock_paths"], (
        "编剧那条又回到闭集里了 —— 它要模型知道线索名单,而那个名字就是谜面")
    assert seg["refused"]["director_reveal"], (
        "拿掉一条路而不说为什么,下一个人会把它当成漏掉的、再加一遍")
    assert "judge" not in str(seg["unlock_paths"])
    # 两档,不是三档
    assert seg["states"] == ["known", "unknown"], seg["states"]
    assert "suspect" not in str(seg) and "怀疑" not in seg["gloss"].replace(
        "**不报「怀疑」那一档**", "")
    assert set(seg["options_keys"]) == set(C.BOARD_KEYS)


def test_契约里的options_field_只有一个意思(tmp_path):
    """🟡 **验收 A 整体 ⑤**:同一个键名在两处是两个意思。

    `person_verbs.options_field` = `player_options()` 返回里的**那一格**;
    而 `clues`/`factions` 那一版写的 `"clues"`/`"faction"` **不是任何一格** ——
    照它去取会拿到 `None`。**一个含义两解的键,比没有那个键更坏**:
    消费方读得懂两种写法里的哪一种,全靠猜。

    这条闸把那一个意思钉死:**声明了它,真门返回里就必须有那一格。**
    """
    from anima_world.__main__ import contract_payload

    payload = contract_payload()
    with _world(tmp_path, "field") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        for name, seg in sorted(payload.items()):
            if not isinstance(seg, dict) or "options_field" not in seg:
                continue
            method = seg.get("options_method") or ""
            assert method and hasattr(world, method), (
                f"`{name}.options_field` 指着一扇不存在的门 `{method}`")
            got = getattr(world, method)("p1")
            assert isinstance(got, dict) and seg["options_field"] in got, (
                f"`{name}.options_field` = {seg['options_field']!r},"
                f"而 `{method}()` 返回里没有这一格:{sorted(got)} —— "
                "照它去取会拿到 None")
    # 而线索/阵营两段**有意没有这一格**:那两扇门返回的就是板子本身
    for name in ("clues", "factions"):
        assert "options_field" not in payload[name], (
            f"`{name}` 又多了一格 `options_field` —— 那扇门返回的就是板子本身,"
            "写一格上去就是给同一个键名第二个意思")


def test_作者插件的id撞上出厂插件_当场拒(tmp_path):
    """🟡 **验收 A 整体 ⑤**:上一版这一格没查,而下场是安静的。

    插件 id **就是命名空间**(边是 `<id>.<边名>`)。作者写一个也叫 `clues` 的插件,
    开 `clues.enabled` 那一刻他那份被换掉(`clues.heard` 从 `edge_types` 里消失,
    而库里那些行还躺着),关掉它又把两份一起摘走 ——
    **他写的东西不见了,而没有一处报错。**
    """
    from anima_world.__main__ import world_plugin_errors

    mine = {"id": "clues", "version": "1.0.0", "label": "我的线索",
            "edges": {"heard": {"label": "听说过", "from": "player",
                                "to": "entity:clue"}}}
    said = world_plugin_errors({"plugins": [mine]})
    assert any("出厂插件的 id" in line for line in said), (
        f"作者写了一个和出厂同 id 的插件而没有一处拦下来:{said}")
    # 换个 id 就放行 —— 拦的是撞名,不是"作者不许写线索相关的插件"
    assert world_plugin_errors({"plugins": [{**mine, "id": "my_clues"}]}) == []


def test_没有本体层的世界_线索板也不塌(tmp_path):
    """`blocked` 该挡的只有「这儿有什么」—— 和 `own` / `person_verbs` 同一课。"""
    seed = {k: v for k, v in _WORLD.items() if k not in ("kinds", "entities")}
    path = write_seed_file(tmp_path / "bare.cyberworld", seed)
    with open_world_at(str(tmp_path / "bare.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.config_set("clues.enabled", True)
        world.player_move("p1", "cafe")
        world.tick(2)
        board = world.player_clues("p1")

    assert board["total"] == 0 and board["known"] == 0, board
    assert board["text"], "一个没有线索的世界也该有一句人话"
