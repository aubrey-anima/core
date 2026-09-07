"""对人动词那一层(3.12.0,批 3b · 裁决 §2.6)。

这一族最要紧的一条不是"它能不能跑",是**那道同意门的方向** ——
它和编剧那道**正相反**,而两处都不会报错。
"""
from __future__ import annotations

from anima_world import person_verbs as P


def test_默认要她点头_而且没有关掉同意门的写法():
    """🔴 和 `together` 那条逐字同一句:**没有 `consent: false`**。

    给作者一个关掉同意门的开关,等于把「拉着谁就一起吃饭」交回去让他自己选 ——
    而挡住那件事正是这道门存在的全部理由。**少一个能问的问题,比多一分表达力值钱。**
    """
    assert P.consent_needed({}) is True
    assert P.consent_needed({"consent": "required"}) is True
    assert P.consent_needed({"consent": "none"}) is False
    assert P.CONSENT_MODES == ("required", "none")
    said = P.verb_errors({"id": "拜师", "consent": False}, "x")
    assert said and "不认识" in said[0]


def test_她不肯和世界说不行_是两句话():
    """**拒绝语是她的话,不是系统话**(纪律 1)。
    「她不肯」进气泡,「世界说不行」是一句规则说明 —— 合成一句的话,
    玩家分不出该改主意还是该等一会儿。"""
    entry = {"refusal": "你还不够格。"}
    hers = P.refusal_line(entry, agent_name="昂热")
    worlds = P.refusal_line(entry, agent_name="昂热", gate="asleep", gate_label="睡着了")
    assert hers == "你还不够格。"
    assert "睡着了" in worlds and "你还不够格" not in worlds


def test_没写拒绝语时_兜底也是一句克制的人话():
    got = P.refusal_line({}, agent_name="昂热")
    assert got and not got.isascii() and "昂热" in got


def test_三种结局三种事件_而且分得出是谁不肯():
    """合成一条按字段分辨的话,运维台数「她被拒了几次」要先解析载荷。"""
    ok = P.proposal_event(verb="拜师", entry={"label": "拜师"}, actor="player:p1",
                          target="ang-re", agent_name="昂热", answer="accepted")
    no = P.proposal_event(verb="拜师", entry={"label": "拜师"}, actor="player:p1",
                          target="ang-re", agent_name="昂热", answer="declined")
    gated = P.proposal_event(verb="拜师", entry={"label": "拜师"}, actor="player:p1",
                             target="ang-re", agent_name="昂热",
                             answer="declined", gate="asleep")
    assert ok["type"] == "person_verb.accepted"
    assert no["type"] == "person_verb.refused"
    # 🔴 **`gate` 分得出「世界说不行」和「她不肯」** —— 后者才该记在她头上
    assert no["payload"]["gate"] == "" and gated["payload"]["gate"] == "asleep"


def test_纯ascii的动词必须给label():
    """和本体层那条逐字同构:她读到的是那几个字,「拜师、duel」里的 duel 是噪音。"""
    assert P.verb_errors({"id": "duel"}, "x")
    assert not P.verb_errors({"id": "duel", "label": "决斗"}, "x")
    assert not P.verb_errors({"id": "拜师"}, "x")


def test_不认识的键当场说():
    said = P.verb_errors({"id": "拜师", "target_form": "agent"}, "x")
    assert said and "target_form" in said[0]


# ── 🔴 反向闸:对人动词不许掉回 affordance 那条路 ────────────────────────────

def test_verb_target_forms里永远不许出现agent():
    """🔴 **裁决 §2.6 点名的那道反向闸。**

    `agent` 一旦混进 `verb_target_forms`,对人动词就从工具路**掉回 affordance**
    —— 而 affordance 整条路上没有一处问过对方肯不肯,那正是这一层明令禁止的。
    放行的样子是安静的:世界照跑、动词照点得动,只是**她再也没有否决权**。
    """
    from anima_world.__main__ import contract_payload

    forms = contract_payload()["plugins"].get("verb_target_forms") or []
    assert "agent" not in forms, (
        f"`agent` 混进 verb_target_forms 了:{forms} —— 对人动词会掉回 affordance,"
        "而那条路上她没有否决权")


def test_对人动词那一段和纯模块的闭集逐项相等():
    """消费方**按段做能力探测,不比版本号**。"""
    from anima_world.__main__ import contract_payload

    seg = contract_payload()["person_verbs"]
    assert seg["keys"] == list(P.PERSON_VERB_KEYS)
    assert seg["answers"] == list(P.ANSWERS)
    assert seg["consent_modes"] == list(P.CONSENT_MODES)
    assert seg["events"] == list(P.EVENTS)
    # 它住在 `plugin` 段里,**不是**新开一个作者层段 ——
    # 所以已发布世界的 `engine_min` 一格不用抬。
    assert seg["in_section"] == "plugin"


def test_插件里写坏一条对人动词_加载期当场拒(tmp_path):
    """判断住在 `person_verbs.py`,而插件那一层只是把它接上 —— **不抄第二份**。"""
    from anima_world.__main__ import world_plugin_errors

    body = {"id": "menpai", "version": "1.0.0",
            "person_verbs": [{"id": "duel"}]}       # 纯 ASCII 没给 label
    said = world_plugin_errors({"plugins": [body]})
    assert any("label" in line for line in said), said
    # 写对了就放行
    good = dict(body, person_verbs=[{"id": "duel", "label": "决斗"}])
    assert not any("label" in line for line in world_plugin_errors({"plugins": [good]}))


# ── 接上世界那一半:同意门用真门,而且方向和编剧那道相反(3.12.0,批 3b)──────

_MENPAI = {
    "id": "menpai", "version": "1.0.0", "label": "门派",
    "person_verbs": [
        {"id": "拜师", "label": "拜师",
         "refusal": "「你我缘分未到。」她把茶碗放下。"},
        # `consent: "none"` 只给**不需要对方配合**的动词
        {"id": "远远看她一眼", "consent": "none"},
    ],
}
_BARE = {
    "agents": [{"id": "阿岚", "name": "阿岚", "location": "cafe",
                "personality": "安静"}],
    "locations": [{"id": "cafe", "name": "咖啡馆", "description": "临海的小店"}],
}


def _world(tmp_path, name="pv"):
    from _worldfile import open_world_at, write_seed_file

    path = write_seed_file(tmp_path / f"{name}.cyberworld",
                           {**_BARE, "plugins": [dict(_MENPAI)]})
    return open_world_at(str(tmp_path / f"{name}.db"), world_file=path,
                         force_mock_llm=True)


def test_声明折自插件库_没有第二张表(tmp_path):
    with _world(tmp_path) as world:
        got = world.person_verbs()
        assert set(got) == {"拜师", "远远看她一眼"}, sorted(got)
        assert got["拜师"]["refusal"].startswith("「你我缘分未到。」")


def test_这个世界没声明过的动词_照实说而不是退成她不肯(tmp_path):
    """⚠️ 退成「她不肯」的话,一个把动词名拼错的宿主会以为是角色的问题,
    而去调她的性格 —— **一句错的诊断比没有诊断贵**。"""
    with _world(tmp_path, name="pv2") as world:
        world.player_move("p1", "cafe")
        got = world.player_person_verb("p1", "阿岚", "御剑")
        assert got["ok"] is False
        assert "没有声明过" in got["error"], got
        assert got["said"] == "", "没声明过的动词不该替她编一句拒绝"


def test_世界说不行和她不肯_是两句话而且都不留痕(tmp_path):
    """🔴 **纪律 1 + 纪律 2 同时量**:拒绝语分得开(前者照 `GATE_LABELS`
    的措辞、后者是作者写的那句),而**被回了一个字都不写**。"""
    from anima_world import together

    with _world(tmp_path, name="pv3") as world:
        # 他根本不在她跟前 —— 世界说不行
        got = world.player_person_verb("p1", "阿岚", "拜师")
        assert got["ok"] is False and got["gate"], got
        assert together.GATE_LABELS[got["gate"]] in got["said"], got["said"]
        assert "缘分未到" not in got["said"], "世界说不行时不许拿她的台词顶包"
        kinds = [e["type"] for e in world.events()]
        assert "person_verb.refused" in kinds
        assert "person_verb.accepted" not in kinds, "没成的事留了痕"


def test_不用点头的那种_直接成(tmp_path):
    """`consent: "none"` 只给不需要对方配合的动词 —— 它**不是**「关掉同意门」。"""
    with _world(tmp_path, name="pv4") as world:
        world.player_move("p1", "cafe")
        got = world.player_person_verb("p1", "阿岚", "远远看她一眼")
        assert got["ok"] is True and got["answer"] == "accepted", got
        assert got["gate"] == ""
        assert "person_verb.accepted" in [e["type"] for e in world.events()]


def test_玩家菜单上真的递得出对人动词(tmp_path):
    """🔴 **player 逐个核过之后带回的那条**:`World.person_verbs()` 有、
    `player_person_verb` 也有,而它**到不了任何玩家面报文** ——
    `targets[].verbs[]` 是 affordance 那一族(契约明令对人动词不走那条)、
    工具目录那两个参数是自由字符串不枚举、主持人那屏也不递。
    **玩家点不到。而一个玩家点不到的能力,和没有那个能力是同一件事。**
    """
    from anima_world.__main__ import contract_payload

    with _world(tmp_path, name="opt") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        menu = world.player_options("p1")
        rows = menu.get("person_verbs")
        assert rows, f"菜单上一个对人动词都没有:{sorted(menu)}"
        assert rows[0]["agent_id"] == "阿岚"
        verbs = {v["verb"]: v for v in rows[0]["verbs"]}
        assert set(verbs) == {"拜师", "远远看她一眼"}, sorted(verbs)
        assert verbs["拜师"]["consent_mode"] == "required"
        assert verbs["远远看她一眼"]["consent_mode"] == "none"
        # 键表以契约为准 —— 宿主照它写解析
        want = set(contract_payload()["person_verbs"]["options_keys"])
        assert want <= set(verbs["拜师"]), sorted(verbs["拜师"])

        # 🔴 **菜单上列的那个 verb,真走得通同意门**(不是一份好看的假清单)
        got = world.player_person_verb("p1", rows[0]["agent_id"], "远远看她一眼")
        assert got["ok"] is True, got


def test_菜单上的available是问得出口_不是她会答应(tmp_path):
    """⚠️ **这一格最容易被读错,所以用例把它钉住。**

    `available` 过的是**世界那几条硬闸**;她肯不肯是**真门那一下**才知道的。
    这一层要是先替她答一遍,就是把她的否决权挪到了菜单上。
    """
    with _world(tmp_path, name="opt2") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        rows = world.player_options("p1")["person_verbs"]
        assert all(v["available"] for v in rows[0]["verbs"]), rows

        # 菜单说"问得出口",而她照样可以不肯 —— 两件事
        got = world.player_person_verb("p1", "阿岚", "拜师")
        assert got["ok"] is False and got["answer"] == "declined", got
        # 🔴 她不肯时说的是**她的话**,不是世界那句规则说明,也不是裸枚举
        assert got["gate"] == "", got
        assert "缘分未到" in got["said"], got["said"]
        assert "declined" not in got["said"], got["said"]


def test_没有本体层的世界_对人动词照样递得出(tmp_path):
    """🔴 **`blocked` 该挡的只有 `targets`**(和 `own` 那一格逐字同一条课):
    对人动词**永不走 affordance**,所以「这个世界没声明过 kinds」不该把它一起
    挡掉 —— 挡掉的样子是安静的:菜单上什么都不少,只是永远没有对人动词。
    """
    from _worldfile import open_world_at, write_seed_file

    seed = {**_BARE, "plugins": [dict(_MENPAI)]}
    seed.pop("kinds", None)
    path = write_seed_file(tmp_path / "noont.cyberworld", seed)
    with open_world_at(str(tmp_path / "noont.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        menu = world.player_options("p1")
        assert menu["blocked"] == "no_ontology", menu["blocked"]
        assert menu["person_verbs"], "本体层挡掉了一个不依赖本体层的能力"


def test_人话那张脸上真的印得出对人动词(tmp_path):
    """🔴 **验收 B+C ⑦**:引擎里有、`--json` 里有,而**人话那张脸一个字不印**。
    这个仓库对这种形状有一句现成的话:**库里有而对方看不见,等于没有。**

    ⚠️ **别数源码,去问屏幕。**
    """
    import contextlib
    import io as _io

    from anima_world.__main__ import contract_payload, main as _main

    with _world(tmp_path, name="scr") as world:
        world.player_move("p1", "cafe")
        world.tick(2)

    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        _main(["player", "options", "--player", "p1", "--world-id", "w"])
    screen = buf.getvalue()
    assert "拜师" in screen, f"屏上没有对人动词:{screen}"
    assert "阿岚" in screen, screen
    # 「可以」要说得出「问得出口 ≠ 她会答应」这条分界
    assert "她可以不答应" in screen, screen
    # 屏幕纪律:不许裸英文枚举、不许 Python 字面量、不许裸星号
    assert "**" not in screen and "{'" not in screen, screen
    for enum in ("required", "none", "accepted", "declined"):
        assert enum not in screen, f"屏上印了裸枚举 {enum!r}:{screen}"
    # 组外面那两格进契约(宿主要知道这一组是对谁的)
    want = set(contract_payload()["person_verbs"]["options_group_keys"])
    assert want == {"agent_id", "agent_name", "verbs"}, sorted(want)


def test_没有本体层时_那张脸也不许早退(tmp_path):
    """`blocked` 挡的只有「这儿有什么」—— 上一版在 `blocked` 那一行就
    `return 0`,于是一个 `no_ontology` 的世界屏上连对人动词都看不到。"""
    import contextlib
    import io as _io

    from _worldfile import open_world_at, write_seed_file
    from anima_world.__main__ import main as _main

    seed = {**_BARE, "plugins": [dict(_MENPAI)]}
    path = write_seed_file(tmp_path / "no2.cyberworld", seed)
    with open_world_at(str(tmp_path / "no2.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        assert world.player_options("p1")["blocked"] == "no_ontology"

    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        _main(["player", "options", "--player", "p1", "--world-id", "w"])
    screen = buf.getvalue()
    assert "拜师" in screen, f"blocked 那一行把不相干的东西也挡掉了:{screen}"


# ── tool 诉求 E:占位符 + 装得进去却什么都不做(3.13.0)─────────────────────

_DUIREN = {
    "id": "duiren", "version": "1.0.0", "label": "对人",
    "person_verbs": [
        {"id": "请教一句", "label": "请教一句", "consent": "none",
         "effects": [{"op": "sentiment_delta", "as": "$target",
                      "target": "player:p1", "delta": 0.1}]},
        {"id": "借一次笔记", "label": "借一次笔记"},
        {"id": "远远看一眼", "consent": "none"},
    ],
}


def test_占位符指得到刚才问的那个人_同意之后世界真的变了(tmp_path):
    """🔴 **tool 诉求 E ①**(真发包到龙族时量出来的)。

    一条对人动词的全部意义就是**对谁做在运行时才知道**,而在这之前作者只能写死
    一个角色 id。tool 写了 `{"as": "$target"}`,引擎**照收**(`world check` rc 0),
    运行时去找一个叫 `$target` 的角色、展开不出、`except` 吞掉,
    而玩家侧 `ok:true / accepted` **一切正常** —— 龙族装着的三个对人动词
    **同意了也什么都不发生**。

    ⚠️ **占位符只是这条 bug 的一半**:另一半是那几行**从来没发过事件** ——
    展开完只把 op 名字攒进 `landed` 就完了。即便把角色 id 写死也一样什么都不动。
    """
    from _worldfile import open_world_at, write_seed_file

    path = write_seed_file(tmp_path / "e1.cyberworld",
                           {**_BARE, "plugins": [dict(_DUIREN)]})
    with open_world_at(str(tmp_path / "e1.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        before = world.relationship_summary("阿岚", "player:p1")["axes"]["sentiment"]
        got = world.player_person_verb("p1", "阿岚", "请教一句")
        after = world.relationship_summary("阿岚", "player:p1")["axes"]["sentiment"]

    assert got["ok"] is True and got["answer"] == "accepted", got
    assert after != before, (
        f"同意了,而世界一格没动({before} → {after})—— "
        "`ok:true / accepted` 一切正常,正是这条 bug 最难查的地方")
    assert after > before


def test_占位符拼错_加载期当场拒(tmp_path):
    """🔴 **另一半:`world check` 从前一声不吭**(rc 0)。
    **一个装得进去、却什么都不做的动词,比一个装不进去的动词坏得多**:
    前者要人去线上一个一个试,后者当场就告诉你。
    """
    from anima_world.__main__ import world_plugin_errors
    from anima_world.person_verbs import TARGET_PLACEHOLDER

    bad = {**_DUIREN, "person_verbs": [
        {"id": "请教一句", "label": "请教",
         "effects": [{"op": "sentiment_delta", "as": "$targt",
                      "target": "player:p1", "delta": 0.1}]}]}
    said = world_plugin_errors({"plugins": [bad]})
    assert any("$targt" in line for line in said), said
    assert any(TARGET_PLACEHOLDER in line for line in said), (
        f"报了错却没告诉他正确写法是什么:{said}")

    # 写对了就放行
    assert not world_plugin_errors({"plugins": [dict(_DUIREN)]})


def test_指人的那几格_每一格都真有op读它():
    """🔴 **三个死格那一批一条闸都没有**(验收 A 三轮 ④,3.13.0)。

    上一批把 `who` 从 `TARGET_FIELDS` 里删掉了 —— 理由写得很清楚
    (**没有任何一个 op 用这个字段名**,而它报在契约里会让作者写下
    `{"who": "$target"}` 然后发现什么都没发生)。而**那一改一条闸都没留**:
    把 `who` 加回去,全仓一条不红。

    **一次靠读代码读出来的删除,下一个人靠读代码是读不回来的。**
    判据在这儿写成机器能问的话:`TARGET_FIELDS` 里的每一格,
    都得是**真有 op 拿它当字段名**的那种。
    """
    from anima_world.beats import OP_REQUIRED_FIELDS
    from anima_world.person_verbs import TARGET_FIELDS

    used = {f for fields in OP_REQUIRED_FIELDS.values() for f in fields}
    for field in TARGET_FIELDS:
        assert field in used, (
            f"`{field}` 报在 `contract.person_verbs.target_fields` 里,"
            f"而没有任何一个 op 拿它当字段名 —— 作者写 "
            f'{{"{field}": "$target"}} 会一声不吭地什么都不发生。'
            f"真有 op 读的那几格是 {sorted(used)}")


def test_指到一个不存在的角色_也要拒():
    """⚠️ 这一格**插件那一层查不动**(它有意不认识世界),所以判断放在
    `person_verbs.effect_errors` 上,由认识世界的那一层传 `known_agents`。
    这条用例把那个函数本身钉住。"""
    from anima_world.person_verbs import effect_errors

    said = effect_errors(
        {"effects": [{"op": "sentiment_delta", "as": "没这个人"}]},
        "拜师", known_agents=["阿岚"])
    assert said and "指不到" in said[0], said
    # 认识的角色、以及占位符,都不该被咬
    assert not effect_errors(
        {"effects": [{"op": "sentiment_delta", "as": "阿岚"}]},
        "拜师", known_agents=["阿岚"])
    assert not effect_errors(
        {"effects": [{"op": "sentiment_delta", "as": "$target"}]},
        "拜师", known_agents=["阿岚"])


def test_plugin_list_报得出对人动词有几条(tmp_path):
    """🟡 **tool 诉求 E ②**:那行 `动词 N` 是 **affordance 那一族**,而对人动词
    **永不走 affordance** —— 于是一个装了三条对人动词的插件在这一屏上
    显示的是「动词 0」。**库里有而对方看不见,等于没有。**
    """
    import contextlib
    import io as _io

    from _worldfile import open_world_at, write_seed_file
    from anima_world.__main__ import main as _main

    path = write_seed_file(tmp_path / "e2.cyberworld",
                           {**_BARE, "plugins": [dict(_DUIREN)]})
    with open_world_at(str(tmp_path / "e2.db"), world_file=path,
                       force_mock_llm=True) as world:
        world.tick(1)

    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        _main(["plugin", "list", "--world-id", "w"])
    screen = buf.getvalue()
    assert "duiren" in screen, screen
    assert "对人动词 3" in screen, f"那一屏报不出对人动词有几条:{screen}"
