"""人做的事也算数:**在场的人记得住**、**旁白讲得到**、**她开口约得动**。

这三件是同一条裂缝的三段。线上晚潮第 238 天、161,648 条事件里取的证:

| | NPC / 世界 | 34 个玩家合计 |
|---|---|---|
| `narrative` 旁白 | 49,990 | **0 次提到玩家** |
| `memory_seed` | 8,480 | 13,**全部来自对话摘要** |
| 11 个联合动词 | —— | **238 天 0 次** |

你「说」的话进得了世界,你「做」的事进不去。而第三件更难看一点:她其实点得动
他的名,只是引擎**替他点了头** —— 一句写在代码注释里的「你自己点的头」,而他
从来没被问过。

`test_witness_and_rule_marks.py` 验的是规律 `emit` 那一半的见证记忆,这一份验
**能力**那一半,外加邀请门。共用的纪律逐字同一条:**声明本身就是开关**。
"""
from __future__ import annotations

import json

import pytest

from _worldfile import write_seed_file

from anima_world import together


# ── 夹具 ────────────────────────────────────────────────────────────────────


_SEED = {
    "locations": [
        {"id": "cafe", "name": "咖啡店", "description": "拐角那家"},
        {"id": "yard", "name": "后院", "description": "堆着几只空木箱"},
    ],
    "agents": [
        {"id": "夏", "name": "苏晚夏", "location": "cafe", "personality": "开朗"},
        {"id": "遥", "name": "沈遥", "location": "cafe", "personality": "话少"},
        {"id": "柔", "name": "林柔", "location": "yard", "personality": "温吞"},
    ],
    "kinds": [
        {"id": "agent", "quantities": {"体力": {"default": 100, "visibility": "self"}}},
        {"id": "bench", "gloss": "一条长椅", "quantities": {
            "坐过几回": {"default": 0.0, "visibility": "here"},
        }, "affordances": {
            # **作者声明了 importance** —— 于是同屋的人记得住。
            "同坐": {
                "label": "一起坐会儿",
                "participants": {"min": 1, "max": 2},
                "importance": 0.7,
                "set": {"坐过几回": "坐过几回 + 1"},
            },
            # 长过程:见证者是**收尾那一刻**在场的人。
            "长谈": {
                "label": "坐着长谈",
                "participants": {"min": 1, "max": 2},
                "duration": 3,
                "importance": 0.6,
                "set": {"坐过几回": "坐过几回 + 10"},
            },
            # 一个人做得成、而且作者说它值得记住 —— 玩家那几条走这一个。
            "上发条": {"label": "上一次发条", "importance": 0.8,
                       "set": {"坐过几回": "坐过几回 + 1"}},
            # **作者没写 importance** —— 对照组,这一层整个缺席。
            "擦一擦": {"label": "擦一擦", "set": {"坐过几回": "坐过几回 + 0"}},
        }},
    ],
    "entities": [{"id": "bench:oak", "name": "橡木长椅", "location": "cafe"}],
    "relations": [
        {"a": "夏", "b": "遥", "sentiment": 0.6},
        {"a": "遥", "b": "夏", "sentiment": 0.6},
    ],
}


@pytest.fixture
def world(open_world, tmp_path):
    seed = json.loads(json.dumps(_SEED))
    yield open_world(world_file=write_seed_file(tmp_path / "w.cyberworld", seed))


def _witness(world, agent_id):
    return [m for m in world.memories(agent_id) if m.get("kind") == "witness"]


def _events(world, kind):
    return world.history(kind=kind, limit=500)["events"]


def _shifts(world, agent_id):
    """她的 `relation_shift` 记忆(按摘要)。**创世那几条关系自己带一批**,
    所以这几条测试比的是**增量**,不是总数。"""
    return [m["summary"] for m in world.memories(agent_id)
            if m.get("kind") == "relation_shift"]


# ── 一、声明本身就是开关 ────────────────────────────────────────────────────


def test_没声明importance的能力做一百次_一条种子都不落(world):
    """**没有默认值。** 给它一个缺省等于替每个作者宣布"世界上任何一次交互都值得
    记一辈子",于是记忆里塞满了谁又擦了一次长椅,真正要紧的那几件淹在里面。"""
    for _ in range(100):
        assert world.act("夏", "interact",
                         {"target": "bench:oak", "verb": "擦一擦"},
                         surface="body")["ok"] is True
    assert _witness(world, "夏") == []
    assert _witness(world, "遥") == []
    assert [e for e in _events(world, "memory_seed")
            if (e["payload"] or {}).get("kind") == "witness"] == []


def test_声明了的_同屋每个人各落一条(world):
    """屋里的人各记一条,别处的人一条都没有 —— **谁在场按位置算**。"""
    assert world.act("夏", "interact", {"target": "bench:oak", "verb": "同坐",
                                       "with": ["遥"]}, surface="body")["ok"] is True
    assert len(_witness(world, "夏")) == 1
    assert len(_witness(world, "遥")) == 1
    assert _witness(world, "柔") == [], "林柔在后院 —— 她不该记得咖啡店里的事"
    assert _witness(world, "夏")[0]["summary"].startswith("我"), (
        "自己做的事用「我」:写成「苏晚夏一起坐会儿」等于让她从外面看着自己"
    )
    assert "苏晚夏" in _witness(world, "遥")[0]["summary"]
    assert _witness(world, "夏")[0]["importance"] == pytest.approx(0.7)


def test_来路留在事件上_不在记忆行里(world):
    """日后要问"这条记忆是哪个能力种下的",日志里答得出来。"""
    world.act("夏", "interact", {"target": "bench:oak", "verb": "同坐",
                                "with": ["遥"]}, surface="body")
    seeds = [e for e in _events(world, "memory_seed")
             if (e["payload"] or {}).get("kind") == "witness"]
    assert seeds and all(
        e["payload"]["affordance"] == "bench:oak.同坐"
        and e["payload"]["source_type"] == "entity_interaction"
        for e in seeds
    )


def test_长过程的见证者是收尾那一刻在场的人(world):
    """一件做了三个 tick 的事,记得它的该是**看见它做成**的人。"""
    assert world.act("夏", "interact", {"target": "bench:oak", "verb": "长谈",
                                       "with": ["遥"]}, surface="body")["ok"] is True
    assert _witness(world, "夏") == [], "还没做完 —— 这会儿谁都不该记得"
    world.tick(4)
    assert len(_witness(world, "夏")) == 1
    assert len(_witness(world, "遥")) == 1


def test_一起做的一场只种一轮_不是一人一轮(world):
    """一起做的事会为每个人各发一条 `entity_interaction`(各带自己那份代价)。

    照着每条都种一次的话,两个人坐一次,屋里每个人会记得坐了两回。
    """
    world.act("夏", "interact", {"target": "bench:oak", "verb": "同坐",
                                "with": ["遥"]}, surface="body")
    assert len(_witness(world, "夏")) == 1
    assert len(_witness(world, "遥")) == 1


# ── 二、施动者是玩家时,这条路上一处分支都没有 ────────────────────────────


def test_玩家做的事_屋里的人一样记得住_而且记的是他的名字(world):
    """病灶原文:`memory_seed` 8,480 条,关于 34 个玩家的是 13 条,**全部来自
    对话摘要**。他擦过的窗、上过的发条、对过的表,一个人都不记得。

    ⚠️ 见证者从 `_agent_locations()` 里来(它只装引擎模拟得动的那些人),玩家
    因此**自然**不在其中 —— 不是被一句 `if` 滤掉的。
    """
    world.player_move("p1", "cafe")
    world.players["p1"]["display_name"] = "阿布"
    assert world.player_tool(
        "p1", "interact", {"target": "bench:oak", "verb": "上发条"})["ok"] is True
    assert len(_witness(world, "夏")) == 1
    assert len(_witness(world, "遥")) == 1
    assert _witness(world, "柔") == []
    assert _witness(world, "夏")[0]["summary"].startswith("阿布"), (
        "印一个 player:9f2c… 的 uuid 进她的记忆,等于她记得一个不存在的人"
    )
    assert _witness(world, "夏")[0]["summary"] == "阿布上一次发条,在橡木长椅"


# ── 三、玩家动作进旁白:路修通,开关默认关 ────────────────────────────────
#
# **走真的那条路** —— 不换掉叙事器、不直接调 `_generate_narrative`:这一层的病
# 恰恰是"每一段单看都对,合起来玩家不在里面",而那种病只在真路径上现形。


def _drain(world):
    """旁白跑在线程池上(时钟永不等网络),所以断言之前得等它落完。"""
    pool = world.scheduler._narrative_pool
    if pool is not None:
        pool.shutdown(wait=True)


def _player_narratives(world):
    return [e for e in _events(world, "narrative")
            if str(e.get("who") or "").startswith("player:")]


def test_玩家动作的旁白默认不生成(world):
    """路铺好、开关留着,默认 **0** —— 旁白是一次 LLM 调用,而它按玩家的每一次
    动作触发。默认开等于替每个已有世界多开一笔账。"""
    world.player_move("p1", "cafe")
    assert world.player_tool(
        "p1", "interact", {"target": "bench:oak", "verb": "上发条"})["ok"] is True
    _drain(world)
    assert _player_narratives(world) == []


def test_开了开关_玩家做的事进旁白_而且印的是他的名字(world):
    """病灶原文:49,990 条旁白,**0 次提到玩家**。"""
    world.config_set("narrative.player.enabled", True)
    world.player_move("p1", "cafe")
    world.players["p1"]["display_name"] = "阿布"
    assert world.player_tool(
        "p1", "interact", {"target": "bench:oak", "verb": "上发条"})["ok"] is True
    _drain(world)
    lines = _player_narratives(world)
    assert len(lines) == 1
    assert lines[0]["payload"]["speaker_name"] == "阿布", (
        "一个 player:9f2c… 印在他自己的世界动态里,是这一层最不该有的样子"
    )
    assert "player:" not in lines[0]["payload"]["text"]


def test_没声明importance的动作_开了开关也不进旁白(world):
    """同一根轴。擦一次长椅不值得一句旁白,而"值不值得"只有作者说得出。"""
    world.config_set("narrative.player.enabled", True)
    world.player_move("p1", "cafe")
    assert world.player_tool(
        "p1", "interact", {"target": "bench:oak", "verb": "擦一擦"})["ok"] is True
    _drain(world)
    assert _player_narratives(world) == []


def test_角色那半边一个字不变_不会写两遍旁白(world):
    """她的旁白早就有了,走的是行为树那条路(`emit_action`)。在这里再发一次
    等于同一件事写两遍旁白。"""
    world.config_set("narrative.player.enabled", True)
    before = len(_events(world, "narrative"))
    world.act("夏", "interact", {"target": "bench:oak", "verb": "同坐",
                                "with": ["遥"]}, surface="body")
    _drain(world)
    assert len(_events(world, "narrative")) == before


# ── 四、邀请门:她开口问了,他还没答 ──────────────────────────────────────


def _invite(world, agent_id="夏", player_id="p1", verb="同坐"):
    """她点他的名(聊天里的 `interact(with=["我"])` 就长这样)。"""
    return world.act(
        agent_id, "interact",
        {"target": "bench:oak", "verb": verb, "with": [f"player:{player_id}"]},
        surface="body",
    )


def test_她点他的名时_他得自己答(world):
    """`api.py` 那句假话的修法。**这不是拒绝,也不是错** —— 她该等。"""
    world.player_move("p1", "cafe")
    out = _invite(world)
    assert out["ok"] is False
    detail = out.get("detail") or out
    assert detail["reason"] == together.INVITE_PENDING
    assert detail["invite_seq"] > 0
    # **那件事一个字都没做掉。**
    assert world.scheduler.stock_store.of("bench:oak").get("坐过几回") == 0
    events = _events(world, "agent_invites")
    assert len(events) == 1
    assert events[0]["payload"]["player_id"] == "p1", "过滤键是裸 pid"
    assert events[0]["payload"]["expires_tick"] == (
        events[0]["payload"]["created_tick"] + together.DEFAULT_INVITE_TTL_TICKS
    )


def test_邀请从那扇门里看得见_而且带着还等不等得到(world):
    world.player_move("p1", "cafe")
    _invite(world)
    rows = world.invitations("p1")
    assert len(rows) == 1 and rows[0]["pending"] is True
    assert rows[0]["payload"]["text"]
    assert world.invitations("p9") == [], "别人的邀请不该出现在他那扇门里"
    page = world.invitations_page("p1")
    assert set(page) == {"events", "next_seq", "cursor", "scanned", "total"}
    # **拷一份,不改那条事件**:`pending` 不许就地写进日志里那一条。
    logged = _events(world, "agent_invites")[0]
    assert "pending" not in logged


def test_没人答_到点就过期_而且一个字都不写在他头上(world):
    """老板拍的那一条:**只有明确拒绝才记,过期不记。**

    区分「拒绝」和「错过」,这对一个在手机上玩的人很要紧 —— 他放下手机去吃了顿
    饭,回来发现她问过他一句。把这记成"他拒绝了我",是引擎替他说反话。

    **按世界时钟判**:把 tick 推过去,不 sleep。
    """
    world.player_move("p1", "cafe")
    _invite(world)
    before = len(world.memories("夏"))
    world.tick(together.DEFAULT_INVITE_TTL_TICKS - 1)
    assert _events(world, "invitation_settled") == [], "还没到点"
    world.tick(2)
    settled = _events(world, "invitation_settled")
    assert len(settled) == 1 and settled[0]["payload"]["outcome"] == "expired"
    assert world.invitations("p1")[0]["pending"] is False
    # 这一支的全部内容:**不落种子、不动关系**。
    assert len(world.memories("夏")) == before
    assert [e for e in _events(world, "state_change")
            if (e["payload"] or {}).get("cause") == "invitation_declined"] == []
    world.scheduler.catch_up_projection()
    rel = world.scheduler._memory_projection.relations.get(("夏", "p1"))
    assert rel is None or rel.sentiment == 0.0


def test_他按了不_这一支才记(world):
    """一个人做出的决定 —— 进她的记忆、进关系。**纯算术,一次模型都不调。**"""
    world.player_move("p1", "cafe")
    world.players["p1"]["display_name"] = "阿布"
    _invite(world)
    seq = world.invitations("p1")[0]["seq"]
    before = _shifts(world, "夏")
    out = world.answer_invitation("p1", seq, accept=False)
    assert out["ok"] is True and out["outcome"] == "declined"
    settled = _events(world, "invitation_settled")
    assert len(settled) == 1 and settled[0]["payload"]["outcome"] == "declined"
    fresh = [s for s in _shifts(world, "夏") if s not in before]
    assert len(fresh) == 1 and "阿布" in fresh[0]
    world.scheduler.catch_up_projection()
    rel = world.scheduler._memory_projection.relations.get(("夏", "p1"))
    assert rel is not None and rel.sentiment < 0
    # **只写她 → 他这一条。** 他推掉一次邀请,不该顺手改写他自己对她的感觉。
    assert world.scheduler._memory_projection.relations.get(("p1", "夏")) is None


def test_他点头_那件事才真的做掉(world):
    world.player_move("p1", "cafe")
    _invite(world)
    seq = world.invitations("p1")[0]["seq"]
    out = world.answer_invitation("p1", seq, accept=True)
    assert out["ok"] is True and out["outcome"] == "accepted", out
    assert world.scheduler.stock_store.of("bench:oak").get("坐过几回") == 1
    assert _events(world, "invitation_settled")[0]["payload"]["outcome"] == "accepted"
    assert world.invitations("p1")[0]["pending"] is False


def test_他点头那一刻人已经走开_那件事不许做掉(world):
    """决定与执行之间世界还在跑。`accepted_ids` 跳过的只有"问"这一步,**不跳过闸**。"""
    world.player_move("p1", "cafe")
    _invite(world)
    seq = world.invitations("p1")[0]["seq"]
    world.player_move("p1", "yard")          # 他起身走了
    before = _shifts(world, "夏")
    out = world.answer_invitation("p1", seq, accept=True)
    assert out["ok"] is False and out["outcome"] == "expired"
    assert "跟前" in str(out.get("refusal") or "")
    assert world.scheduler.stock_store.of("bench:oak").get("坐过几回") == 0
    # 走开不是拒绝 —— 这一支同样不写在他头上。
    assert _shifts(world, "夏") == before


def test_替别人答是不行的(world):
    world.player_move("p1", "cafe")
    _invite(world)
    seq = world.invitations("p1")[0]["seq"]
    from anima_world import tools as tools_mod

    with pytest.raises(tools_mod.ToolCallError):
        world.answer_invitation("p2", seq, accept=True)


def test_答过的邀请再答一次_不报错(world):
    """两个设备同时点同一份邀请是常态,而第二下不该看到一次异常。"""
    world.player_move("p1", "cafe")
    _invite(world)
    seq = world.invitations("p1")[0]["seq"]
    assert world.answer_invitation("p1", seq, accept=False)["ok"] is True
    again = world.answer_invitation("p1", seq, accept=False)
    assert again["ok"] is False and again["outcome"] == "gone"
    assert len(_events(world, "invitation_settled")) == 1


def test_上限用完_她今天不再开口_而这不是错(world):
    """老板拍的那一条:**这是「像个人」和「像推送」的分界。**

    上限用完不是报错,是她今天不再开口。
    """
    world.player_move("p1", "cafe")
    cap = together.DEFAULT_INVITES_PER_PLAYER_PER_DAY
    for _ in range(cap):
        assert (_invite(world).get("detail") or {}).get("reason") == together.INVITE_PENDING
        seq = [r for r in world.invitations("p1") if r["pending"]][0]["seq"]
        world.answer_invitation("p1", seq, accept=False)
    out = _invite(world)
    assert out["ok"] is False
    consents = (out.get("detail") or {}).get("consents") or []
    assert consents and consents[0]["reason"] == "invite_capped"
    assert len(_events(world, "agent_invites")) == cap, "第 N+1 次连事件都不该有"


def test_日切之后她又开得了口(world):
    """键里带着世界日,所以它也自己过期。"""
    world.player_move("p1", "cafe")
    for _ in range(together.DEFAULT_INVITES_PER_PLAYER_PER_DAY):
        _invite(world)
        seq = [r for r in world.invitations("p1") if r["pending"]][0]["seq"]
        world.answer_invitation("p1", seq, accept=False)
    world.scheduler._invited_today.clear()      # = `_on_day_rollover` 做的那一下
    assert (_invite(world).get("detail") or {}).get("reason") == together.INVITE_PENDING


def test_世界关着这扇门时她压根不开口(world):
    """`social.joint.npc_may_invite_player` —— 默认开,但关得掉。"""
    world.config_set("social.joint.npc_may_invite_player", False)
    world.player_move("p1", "cafe")
    out = _invite(world)
    assert out["ok"] is False
    consents = (out.get("detail") or {}).get("consents") or []
    assert consents and consents[0]["reason"] == "player_invites_off"
    assert _events(world, "agent_invites") == []


def test_有人过不了闸时_她不先在他手机上响一下(world):
    """红线 2 在开口这一侧的样子:一件已经办不成的事,不该先发一份会挂到过期的
    邀请,还白占掉她今天的额度。"""
    world.player_move("p1", "cafe")
    out = world.act(
        "夏", "interact",
        {"target": "bench:oak", "verb": "同坐", "with": ["player:p1", "柔"]},
        surface="body",
    )
    assert out["ok"] is False
    assert _events(world, "agent_invites") == [], "林柔在后院 —— 这件事本来就办不成"


def test_邀请是事件不是易失态(world, fresh_redis):
    """**存储契约一格不动。** 它折进投影,不开新的 Redis 键 —— 于是它免费得到
    跨进程一致(`catch_up_projection`)和可重放,运维台那侧的 deepEqual 也照旧。"""
    from anima_world.__main__ import contract_payload

    world.player_move("p1", "cafe")
    _invite(world)
    assert [k for k in fresh_redis.keys("*") if "invit" in str(k).lower()] == []
    storage = json.dumps(contract_payload()["storage"], ensure_ascii=False)
    assert "invit" not in storage


def test_重放折得出同一份还等着的清单(world):
    """它是**从账本折出来的,不是第二张真相表**。一份"谁在等你"的清单如果自己
    存一份状态,那么每个进程手里都有一份可能不一样的答案,而分叉的那一天没有
    任何一处会报错。"""
    world.player_move("p1", "cafe")
    _invite(world)
    pending = dict(world.scheduler._memory_projection.invitations)
    assert len(pending) == 1
    world.scheduler.reset_projection(world.scheduler.event_log.replay())
    assert dict(world.scheduler._memory_projection.invitations) == pending


# ── 五、契约那一格答得出来 ────────────────────────────────────────────────


def test_contract_json_答得出importance那一格():
    """创作台按出口判,**不比版本号** —— 同一个版本号下有过好几份不同的引擎。"""
    from anima_world.__main__ import contract_payload

    cell = contract_payload()["seed"]["affordance_importance"]
    assert cell["range"] == [0.0, 1.0]
    assert cell["default"] is None, "不写 = 这一层整个缺席,不是 0"
    assert cell["read_command"] == "ontology"
    assert "importance" in contract_payload()["seed"]["affordance_keys"]


def test_契约那一格和校验器读的是同一份():
    """抄一份的话,加一格时总会有一次只改了校验器,而这一头照旧答着旧清单。"""
    from anima_world.__main__ import contract_payload
    from anima_world.ontology import AFFORDANCE_KEYS

    assert contract_payload()["seed"]["affordance_keys"] == sorted(AFFORDANCE_KEYS)


def test_ontology_那扇门读得出这一格(tmp_path, open_world):
    """`contract` 说读出口是 `ontology`,那就得真的读得出来 —— **两头都验**:
    `--json` 是契约,那张字符表是赠品。

    渲染那一行也要在:创作台的人是照着终端那张表读世界的,而一格只在 `--json`
    里有的东西,他们看不见 = 对他们不存在(FOR-STUDIO 的判据逐字如此)。
    ⚠️ **没声明的那条一行都不许印** —— 印一句「importance 无」会让读的人以为
    有个默认值在那儿,而真相是这一层压根没铺开。
    """
    from _worldfile import open_world_at, run_cli

    seed = json.loads(json.dumps(_SEED))
    path = write_seed_file(tmp_path / "w.cyberworld", seed)
    with open_world_at(tmp_path / "w.db", world_file=path):
        pass

    rows = {
        row["verb"]: row
        for kind in json.loads(run_cli("ontology", "--world-id", "w", "--json").stdout)["kinds"]
        if kind["id"] == "bench"
        for row in kind["affordances"]
    }
    assert rows["同坐"]["importance"] == 0.7
    assert rows["擦一擦"]["importance"] is None, "没声明的那格该是空的,不是 0"

    out = run_cli("ontology", "--world-id", "w").stdout
    assert "在场的人会记住这一下(importance 0.7)" in out
    assert out.count("在场的人会记住这一下") == 3, "四条能力里只有三条声明过"


def test_world_check_认这个字段(tmp_path):
    """`world check` 走的是开机第一秒那两个函数 —— 它认不认这个字段,等于引擎
    收不收这份世界文件。"""
    from anima_world.__main__ import main

    seed = json.loads(json.dumps(_SEED))
    path = write_seed_file(tmp_path / "ok.cyberworld", seed)
    assert main(["world", "check", str(path), "--json"]) == 0

    bad = json.loads(json.dumps(_SEED))
    bad["kinds"][1]["affordances"]["同坐"]["importance"] = 8
    bad_path = write_seed_file(tmp_path / "bad.cyberworld", bad)
    assert main(["validate", "world", str(bad_path)]) == 2, (
        "写了 8 的作者想的是「很重要」;按 1 截断的话他永远不会知道自己写错了刻度"
    )
