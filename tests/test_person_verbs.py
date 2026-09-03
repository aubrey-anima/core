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
