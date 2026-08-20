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
        # 第三个地方,只为了"两个人各走各的"那一条:两处地点时"他也走开"
        # 只可能走到她那儿去,而那恰好是**面对面**,验不到要验的东西。
        {"id": "shop", "name": "杂货铺", "description": "隔着一条街"},
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
    assert set(page) == {
        "events", "next_seq", "cursor", "scanned", "total", "now_tick"}
    # **拷一份,不改那条事件**:`pending` 不许就地写进日志里那一条。
    logged = _events(world, "agent_invites")[0]
    assert "pending" not in logged and "expires_in" not in logged


def test_还剩多久_一次调用就答得出(world):
    """P2-9。`expires_tick` 是绝对刻度,"还剩多久"要减去**现在** —— 而"现在"
    从前得再问一次 `state()`。两次调用之间世界还在走,于是前端画出来的倒计时是
    拿另一刻的现在减这一刻的到期,偏偏在最后那几秒显示成"还有时间"。

    **过期按世界时钟判、前端不许自己算**,那就得由这一格把"现在"一起给出去。
    """
    world.player_move("p1", "cafe")
    _invite(world)
    page = world.invitations_page("p1")
    assert page["now_tick"] == int(world.scheduler.clock)
    row = page["events"][0]
    assert row["expires_in"] == together.DEFAULT_INVITE_TTL_TICKS
    assert row["expires_in"] == (
        row["payload"]["expires_tick"] - page["now_tick"]
    )
    world.tick(3)
    assert world.invitations_page("p1")["events"][0]["expires_in"] == (
        together.DEFAULT_INVITE_TTL_TICKS - 3
    )
    # 答复过 / 过期了的那些没有"还剩多久"可言 —— 给一个负数或者一个陈旧的
    # 正数,都会让宿主画出一个点不动的倒计时。
    seq = world.invitations("p1")[0]["seq"]
    world.answer_invitation("p1", seq, accept=False)
    done = world.invitations_page("p1")["events"][0]
    assert done["pending"] is False and done["expires_in"] == 0


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
    # **是他走开的,报文就得这么说。**(3.6.1 之前这里只验一个「跟前」——
    # 而她走开时那句话逐字相同,一个断言同时盖住了两件相反的事。)
    assert out["absent"] == "player"
    assert "你已经离开" in str(out.get("refusal") or "")
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


# ── 四之二、谁开的口,决定了要不要问 ──────────────────────────────────────
#
# 邀请门补的是"她点他的名"那一条。而玩家在对话里说「陪我听完这一面」走的是同一段
# 代码 —— 于是 3.6.0 上线的那一刻,世界开始对着刚开口的那个人落一条邀请,问他
# 要不要做他刚说的事。裁决里那句「玩家自己按按钮那条路一个字不变」,只有在
# **聊天这条路也不变**时才是真的:按钮和聊天是同一个人的同一个意思。


def _director(world):
    from anima_world.intent import Director

    return Director(world._tool_runtime)


def _asks(world, verb="同坐", **params):
    """玩家在对话里约她 —— 分类器判出来的就是这份 `together`。"""
    return _director(world).direct(
        agent_id="夏",
        params={"target": "夏", "action": "together",
                "object": "bench:oak", "verb": verb, **params},
        player_id="p1",
    )


def test_玩家在对话里约人_当场就该发生(world):
    """P0-1。**他自己开的口就是他的同意。**

    落一条 `agent_invites` 问他要不要做他刚说的那件事,不只是多此一举 ——
    那封信今天没有任何一处看得见(引擎有意不给 CLI 出口,壳那扇门还没开),
    于是他说的那句话在世界里**什么也没发生**,而回执写着"她在等你回话"。
    """
    world.player_move("p1", "cafe")
    out = _asks(world)
    assert out.ok is True, out.detail
    assert world.scheduler.stock_store.of("bench:oak").get("坐过几回") == 1
    assert _events(world, "agent_invites") == [], "给他自己发了一封信"
    consents = (out.detail or {}).get("consents") or []
    assert [c["who"] for c in consents] == ["player:p1"]
    assert consents[0]["accepted"] is True


def test_她点他的名那条路一个字不变(world):
    """同一个函数、同一个动词,**只有"谁开的口"不同**:她开的口就得等他答。

    两条路必须在同一份测试里对着看 —— 分开写的话,修好一条的那天很容易把另一条
    带回去(3.6.0 那次正是这么把玩家自己的口封掉的)。
    """
    world.player_move("p1", "cafe")
    named = _invite(world)                       # 她点他的名(聊天里的 with=["我"])
    assert named["ok"] is False
    assert (named.get("detail") or named)["reason"] == together.INVITE_PENDING
    assert len(_events(world, "agent_invites")) == 1
    assert world.scheduler.stock_store.of("bench:oak").get("坐过几回") == 0


def test_他不在她跟前时_他自己开的口也不算数(world):
    """`accepted_ids` 跳过的只有"问"这一步,**不跳过闸** —— 隔着半张地图
    说一句"我们一起坐会儿",世界里那条长椅不该动。"""
    world.player_move("p1", "yard")
    out = _asks(world)
    assert out.ok is False
    assert world.scheduler.stock_store.of("bench:oak").get("坐过几回") == 0


def test_一次只算得进一个还没点头的人(world):
    """P0-2。两个玩家被点名时,**先点头的那个必然被判成没做成**。

    路是这样的:两份邀请都发出去 → p1 按「好」→ `answer_invitation` 拿着
    `player_id=p1` 重跑一遍同意 → p2 在 `_invitee` 那道 `player_not_you` 上被判
    过不了闸 → 整件事记成 `expired`。**他按了「好」,而世界一声不吭** —— 这是
    这个仓库最忌的那类坏法,而且没有任何一处报错。

    修法是拦在**开口之前**:拦在后面的话,两份挂到过期的邀请已经发出去了,
    她今天的额度也已经扣掉了。
    """
    world.player_move("p1", "cafe")
    world.player_move("p2", "cafe")
    out = world.act(
        "夏", "interact",
        {"target": "bench:oak", "verb": "同坐",
         "with": ["player:p1", "player:p2"]},
        surface="body",
    )
    assert out["ok"] is False
    assert "一次只算得进一个" in str(out.get("error") or "")
    assert _events(world, "agent_invites") == [], "两份必然挂到过期的邀请发出去了"
    assert world.scheduler._invited_today == {}, "白扣掉了她今天的额度"
    assert world.scheduler.stock_store.of("bench:oak").get("坐过几回") == 0


def test_他自己开的口_再带上一个角色_照旧要问那个角色(world):
    """他那一票记上了,不等于别人的也记上了。"""
    world.player_move("p1", "cafe")
    out = _asks(world, **{"with": ["遥"]})
    assert out.ok is True, out.detail
    who = {c["who"]: c for c in (out.detail or {}).get("consents") or []}
    assert set(who) == {"player:p1", "遥"}
    assert who["player:p1"]["source"] == "gate"
    assert who["遥"]["source"] != "gate", "沈遥没被问过就被算成答应了"


# ── 四之三、重查的是闸,不是人心 ──────────────────────────────────────────


class _Judge:
    """只答"愿不愿意"的关系判定器。`answers` 用完之后一直用最后一个。"""

    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []

    def judge_invite(self, *, a, inviter, invitation, relation, memories,
                     location, recent_talk):
        from types import SimpleNamespace

        self.calls.append(str(a.get("name") or ""))
        accept = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        return SimpleNamespace(accept=accept, reason="嗯" if accept else "今天不想")


def test_他按一下好_不该去等一次模型(world):
    """P1-5。答复那一刻从前把整段同意重跑一遍 —— 包括**再问一次同行的角色**。

    她开口那一刻已经问过了,答案存在 `agent_invites` 的 `consented` 里。再问一遍
    不是省一次网络的问题:他按「好」得等一次 LLM 往返,而那一下本该是即时的。
    """
    judge = _Judge(True)
    world.scheduler.relationship_judge = judge
    world.player_move("p1", "cafe")
    out = world.act(
        "夏", "interact",
        {"target": "bench:oak", "verb": "同坐", "with": ["遥", "player:p1"]},
        surface="body",
    )
    assert (out.get("detail") or {}).get("reason") == together.INVITE_PENDING, out
    assert judge.calls == ["沈遥"], "她开口那一刻问了一次"
    invited = _events(world, "agent_invites")[0]
    assert invited["payload"]["consented"] == ["遥"], invited["payload"]

    seq = world.invitations("p1")[0]["seq"]
    answered = world.answer_invitation("p1", seq, accept=True)
    assert answered["ok"] is True and answered["outcome"] == "accepted", answered
    assert judge.calls == ["沈遥"], "他按一下「好」,又去问了一次模型"


def test_她当时点了头_就是点过头了(world):
    """同一个人同一件事,模型第二次的答案可以和第一次不同。

    再问一遍的话:他按了「好」,却因为**别人**这次改了主意被记成 `expired` ——
    而他这辈子也不会知道自己那一下点得对不对。**闸照查**(见上面"他点头那一刻
    人已经走开"那条),重查的只是"问"这一步。
    """
    judge = _Judge(True, False)      # 第二次她改口
    world.scheduler.relationship_judge = judge
    world.player_move("p1", "cafe")
    world.act("夏", "interact",
              {"target": "bench:oak", "verb": "同坐", "with": ["遥", "player:p1"]},
              surface="body")
    seq = world.invitations("p1")[0]["seq"]
    out = world.answer_invitation("p1", seq, accept=True)
    assert out["ok"] is True and out["outcome"] == "accepted", out
    assert world.scheduler.stock_store.of("bench:oak").get("坐过几回") == 1


# ── 四之四、第四种结局:她自己把话收回去 ──────────────────────────────────


def test_她走开时把还等着的邀请收回去(world):
    """P2-6。`cancelled` 从前是 `INVITE_OUTCOMES` 里一个**发不出来的枚举** ——
    投影的 docstring 说着"四种结局",而全仓库只有那一行声明提到它。

    留着一份她已经兑现不了的邀请,等于摆一个必然失败的按钮:他点下去只会得到
    一句"她不在你跟前"。而这**不是拒绝也不是错过**,所以一个字都不写在他头上。
    """
    world.player_move("p1", "cafe")
    _invite(world)
    before = _shifts(world, "夏")
    out = world.act("夏", "walk_away", {"to_location": "后院"},
                    player_id="p1", surface="chat")
    assert out["ok"] is True, out
    assert out["detail"]["withdrew_invites"], out["detail"]

    settled = _events(world, "invitation_settled")
    assert len(settled) == 1 and settled[0]["payload"]["outcome"] == "cancelled"
    assert settled[0]["payload"]["note"] == "她起身走开了"
    assert world.invitations("p1")[0]["pending"] is False
    # 撤回不写在他头上 —— 他什么也没做错。
    assert _shifts(world, "夏") == before
    assert [e for e in _events(world, "state_change")
            if (e["payload"] or {}).get("cause") == "invitation_declined"] == []


def test_不认识的结局当场报错(world):
    """一个拼错的结局会安安静静地落进日志、落进那扇门,而读的人对着一个它不认识
    的词只能当成"别的" —— 那份邀请在清单上消失、在账上不存在,两边都不报错。"""
    world.player_move("p1", "cafe")
    _invite(world)
    seq = world.invitations("p1")[0]["seq"]
    with pytest.raises(ValueError):
        world.scheduler.settle_invitation(seq, "canceled")     # 少一个 l
    assert world.invitations("p1")[0]["pending"] is True
    assert _events(world, "invitation_settled") == []


# ── 四之五、到底是谁不在场 ────────────────────────────────────────────────
#
# 🔴 **`cancelled` 从前只堵住了最窄的一条路。** `walk_away` 那个工具只挂在
# `chat` 面上,于是"她在对话里说了句我先走"会撤回邀请,而她**按作息表**溜达开
# (`move_agent`,和她自己决定走一模一样的那条路)不会 —— 那份邀请照旧挂着、
# 照旧倒计时,他按下「好」拿到的是一句「『访客』不在她跟前」。
#
# **她走的,话却说成他不在。** 下面这几条把两条路各钉一遍;
# `walk_away` 的 `surfaces` 一个字没拓宽(那是产品向的裁决题,不是这里能定的)。


def _walk_off(world, agent_id="夏", to="yard"):
    """她按作息表溜达开 —— 走 BT 那条路,和她自己决定走一模一样。"""
    world._tool_runtime.move_agent(agent_id, to)
    for _ in range(30):
        if world._tool_runtime.agent_location(agent_id) == to:
            return
        world.tick(1)
    raise AssertionError(f"{agent_id} 没走到 {to}")


def test_她按作息表走开_报文不许怪到他头上(world):
    """同一份报文从前对两件相反的事逐字相同 —— 实测过:她走开和他走开,
    `refusal` 一个字都不差(「『访客』不在她跟前 —— 一起做事得当面」)。"""
    world.player_move("p1", "cafe")
    _invite(world)
    seq = world.invitations("p1")[0]["seq"]
    _walk_off(world)                             # 她走了,他一步没动
    out = world.answer_invitation("p1", seq, accept=True)
    assert out["ok"] is False and out["outcome"] == "expired"
    assert out["absent"] == "agent"              # 机器读的那一半
    refusal = str(out.get("refusal") or "")
    assert "苏晚夏已经离开咖啡店" in refusal      # 人读的那一半:两头都点名
    assert "后院" in refusal and "咖啡店" in refusal
    # **一个字都不许写成是他没到场。**
    assert "你不在" not in refusal


def test_他走开和她走开_是两句不同的话(world):
    """这一条是上面那条的对照组:**只验一边等于没验。**"""
    world.player_move("p1", "cafe")
    _invite(world)
    seq = world.invitations("p1")[0]["seq"]
    world.player_move("p1", "yard")              # 这回是他走的
    out = world.answer_invitation("p1", seq, accept=True)
    assert out["absent"] == "player"
    assert str(out["refusal"]).startswith("你已经离开")


def test_闸的名字也交出去_而不是只有一句人话(world):
    """枚举给机器读、句子给人读 —— 而 `reason` 那一格上"闸拦下的"和"他自己说
    不去"都写成 `declined`。`gate` 是唯一分得开的那一格。"""
    world.player_move("p1", "cafe")
    _invite(world)
    seq = world.invitations("p1")[0]["seq"]
    _walk_off(world)
    out = world.answer_invitation("p1", seq, accept=True)
    assert out["gate"] == "player_not_here"
    # `reason` 一个字没动 —— 它是 `act()` 那扇门上的既有枚举(只加不改)。
    assert out["reason"] == "declined"


def test_她收回之后他再按好_说得出是她收回的(world):
    """**`gone` 从前只说得出"要么答过了,要么已经过期"** —— 一句恰好把
    `cancelled` 排除在外的话,而那是四种里唯一他什么也没做错的一种。"""
    world.player_move("p1", "cafe")
    _invite(world)
    seq = world.invitations("p1")[0]["seq"]
    assert world.act("夏", "walk_away", {"to_location": "后院"},
                     player_id="p1", surface="chat")["ok"] is True
    out = world.answer_invitation("p1", seq, accept=True)
    assert out["outcome"] == "gone"
    assert out["settled"] == "cancelled"
    assert "她自己收回去的" in str(out["refusal"])
    assert "过期" not in str(out["refusal"])


def test_他自己回过的那一份_再按也说得出是他回的(world):
    """四种结局四句话 —— 合成一句就等于把她做的事和他做的事记在一起。"""
    world.player_move("p1", "cafe")
    _invite(world)
    seq = world.invitations("p1")[0]["seq"]
    assert world.answer_invitation("p1", seq, accept=False)["ok"] is True
    again = world.answer_invitation("p1", seq, accept=True)
    assert again["settled"] == "declined" and "你当时说的是不去" in again["refusal"]


def test_那扇门上每一行都带着结局(world):
    """🔴 **消费方那条路上没有游标。** 站点只能去 `recent_events` 里捡那条
    `invitation_settled`(壳截最后 100 条),离线久一点它就滑出去 —— 那一行
    于是永远显示成「错过了」,把**她**做的事记在**他**头上。这扇门本来就有
    游标,结局挂在这里等于顺手把那扇门补上。"""
    world.player_move("p1", "cafe")
    _invite(world)
    rows = world.invitations("p1")
    assert rows[0]["pending"] is True and rows[0]["outcome"] == ""
    assert world.act("夏", "walk_away", {"to_location": "后院"},
                     player_id="p1", surface="chat")["ok"] is True
    rows = world.invitations("p1")
    assert rows[0]["pending"] is False and rows[0]["outcome"] == "cancelled"


def test_她在哪儿开的口_跟着那份邀请一起挂着(world):
    """归因靠的就是这一格。它从事件顶层抄下来,**payload 一个字没加** ——
    线格式没动,所以镜像端不必跟。"""
    world.player_move("p1", "cafe")
    _invite(world)
    seq = world.invitations("p1")[0]["seq"]
    assert world.scheduler.pending_invitation(seq)["loc"] == "cafe"
    assert "loc" not in _events(world, "agent_invites")[0]["payload"]


def test_两个人都走开了_一句话说全两头(world):
    world.player_move("p1", "cafe")
    _invite(world)
    seq = world.invitations("p1")[0]["seq"]
    _walk_off(world, to="yard")
    world.player_move("p1", "shop")              # 他去了第三个地方
    out = world.answer_invitation("p1", seq, accept=True)
    assert out["absent"] == "both"
    assert "你们俩都不在咖啡店了" in out["refusal"]


def test_世界不知道他在哪时_不许说成是他站错了地方(world):
    """`_colocation_refusal` 那张三分表的第二行:这一种**世界自己说不上来**,
    而合成一句「你不在她跟前」会让它看起来像是玩家站错了地方,而他做什么都改不了。

    ⚠️ **走 `player_leave()` 而不是 `world.players.pop()`** —— 要用引擎自己产得出
    的那条路。直接掏投影的话,这条测试钉住的是一个只有测试到得了的状态,而线上
    真正会走到这儿的是"他下线了 / 在场记录过了 15 分钟"。
    """
    world.player_move("p1", "cafe")
    _invite(world)
    seq = world.invitations("p1")[0]["seq"]
    world.player_leave("p1")                     # 他下线了
    out = world.answer_invitation("p1", seq, accept=True)
    assert out["absent"] == "unknown"
    assert out["gate"] == "player_where_unknown"
    # **不许再说"宿主没调过 `player_move`"** —— 那句话在一个接得好好的宿主上是假的
    # (站点 2026-08-13 前后已接上;这里的宿主也确实调过,只是他离开了)。
    assert "player_move" not in out["refusal"]
    assert "世界这会儿不知道你在哪" in out["refusal"]


def test_世界不知道他在哪时_句子里不许漏出裸pid(world):
    """`player_name()` 找不到行时回落成 id —— 那是给调用方的兜底,而这句话是
    **念给玩家看的**。「「p1」不在她跟前」里那个 `p1` 是一个主键,不是谁的名字。

    这一支恰好只在"世界没有这个玩家的行"时才走到,也就是最该说实话的那一次。"""
    out = _invite(world, player_id="ghost")      # 世界从没见过这个人
    detail = out.get("detail") or out
    refusal = str(detail.get("refusal") or "")
    assert detail["gate"] == "player_where_unknown", detail
    assert "ghost" not in refusal, refusal
    assert "这位玩家" in refusal, refusal
    # **框的只有数据里来的那一截**(`Scheduler._named` 里玩家那个「你」同一条)。
    # 「这位玩家」是引擎写的一个称呼,套上「」读起来像在念一个人的名字 ——
    # 而这一支恰好是最不该假装知道他叫什么的那次。
    assert "「这位玩家」" not in refusal, refusal


def test_她在赶路时_那句话不许写成他站错了地方(world):
    """`face_to_face()` 折掉的第三种原因。照 `agent_location` 那份直说的话,回执
    会写成"你在咖啡店,她也在咖啡店 —— 一起做事得当面",一句技术上没错、而玩家
    读起来是谎的话(`_colocation_refusal` 里逐字同一条)。"""
    world.player_move("p1", "cafe")
    world._tool_runtime.move_agent("夏", "yard")   # 起程,还没落脚
    assert "夏" in world.scheduler._transit
    detail = _invite(world).get("detail") or {}
    assert detail["gate"] == "inviter_in_transit", detail
    assert "在赶路" in str(detail["refusal"])


def test_四个闸都属于当面那一族(world):
    """**别写 `== "player_not_here"`。** 拆闸时把整族的名字收进一个常量,是为了
    让下游那一支不会因为拆闸而悄悄关掉 —— 少掉的三种恰好是最需要点名的。"""
    from anima_world import together

    assert together.COLOCATION_GATES <= set(together.GATE_LABELS)
    assert "player_not_here" in together.COLOCATION_GATES
    assert len(together.COLOCATION_GATES) == 4


def test_闸和面对面必须逐位同构(world):
    """`_colocation_gate()` 的 docstring 说这一条"钉着"—— 在 3.6.0 第五轮之前
    它**一条测试都没有**(`git grep _colocation_gate -- tests/` 是 0 行)。

    两份判断分了岔的样子很安静:门(`face_to_face()`)说"当得成"而闸说"当不成",
    于是这一支既拦不住也说得出理由;反过来就是拦下了却说不出为什么。次序尤其
    脆:`agent_location()` 对在途的人仍报着**出发前那个地名**,先看地名的话
    "两处相同"会得出一个和 `face_to_face()` 相反的结论。所以这里按**状态 ×
    (她, 他)** 铺开来对,不是挑一个样本点。
    """
    runtime = world._tool_runtime

    def blackboard(agent_id):
        return world.scheduler.agents[agent_id].agent.blackboard

    def states():
        # 每一项:(这一步怎么把世界摆成那样, 期待的闸)。**摆完不复位** ——
        # 后一项接着前一项走,和真世界里状态会叠加一样。
        yield ("两个人都在咖啡店", lambda: world.player_move("p1", "cafe"), "")
        yield ("他去了后院", lambda: world.player_move("p1", "yard"), "player_not_here")
        yield ("他下线了", lambda: world.player_leave("p1"), "player_where_unknown")
        yield ("他回到她那儿", lambda: world.player_move("p1", "cafe"), "")

    for name, arrange, expected in states():
        arrange()
        for agent_id in ("夏", "遥"):
            gate = world._tool_runtime._colocation_gate(agent_id, "p1")
            assert gate == expected, f"{name}:{agent_id} 的闸是 {gate!r}"
            assert runtime.face_to_face(agent_id, "p1") is (gate == ""), name
            assert gate == "" or gate in together.COLOCATION_GATES, gate

    # 她在赶路 —— `agent_location()` 这时报的还是「cafe」,和他一模一样。
    runtime.move_agent("夏", "yard")
    assert "夏" in world.scheduler._transit
    assert runtime.agent_location("夏") == "cafe", "前提:在途的人还挂着出发前那个地名"
    assert runtime._colocation_gate("夏", "p1") == "inviter_in_transit"
    assert runtime.face_to_face("夏", "p1") is False
    assert runtime._colocation_gate("遥", "p1") == "", "对照组:没赶路的那个照旧当得成面"

    # 世界说不上来**她**在哪(黑板还没写过、名册上也没有地名)。
    blackboard("遥").write("loc", "")
    world.scheduler.agents["遥"].agent.location = ""
    assert runtime._colocation_gate("遥", "p1") == "inviter_where_unknown"
    assert runtime.face_to_face("遥", "p1") is False

    # 世界从没见过这个人:两头都说不上来,而**先答的是她那一头**。
    assert runtime._colocation_gate("柔", "ghost") == "player_where_unknown"
    assert runtime.face_to_face("柔", "ghost") is False
    assert runtime._colocation_gate("遥", "ghost") == "inviter_where_unknown"


def test_他按好时她在赶路_答话那扇门也得点她的名(world):
    """**同一族闸有两扇门**:开口那扇(`_invite`,她问得出问不出)和答话那扇
    (`answer_invitation`,他按下去那一刻重查)。3.6.0 第五轮之前,答话那扇门上
    整族四个闸**只有 `player_where_unknown` 一条测试** —— 而这一族存在的全部理由
    就是"说得出是谁不在场",漏掉的三种里两种恰好都不怪他。

    走到这儿的路在线上很常见:她按作息表溜达开,他手机上那份邀请还亮着。
    """
    world.player_move("p1", "cafe")
    _invite(world)
    seq = world.invitations("p1")[0]["seq"]
    world._tool_runtime.move_agent("夏", "yard")   # 起程,**不 tick 到落脚**
    assert "夏" in world.scheduler._transit
    out = world.answer_invitation("p1", seq, accept=True)
    assert out["gate"] == "inviter_in_transit", out
    assert out["absent"] == "agent", out
    assert "在路上" in out["refusal"] and "不是你不在" in out["refusal"]
    # 他按了「好」而这件事没做成 —— 那是**错过**,不是他拒绝:关系与记忆一个字不写。
    assert out["outcome"] == "expired"
    row = world.invitations("p1")[0]
    assert row["pending"] is False and row["outcome"] == "expired"


def test_他按好时世界不知道她在哪_不许说成是他没到场(world):
    """整族的第四条。`_invite_absence` 里这一支写的是 `unknown` 而不是 `player`,
    理由和玩家那一头逐字同一条:**说不上来不等于都怪你**。落到下面几支的话,
    句子会变成"她已经离开咖啡店了 —— 她这会儿在别处",一句把"查不到"说成
    "她走了"的话。"""
    world.player_move("p1", "cafe")
    _invite(world)
    seq = world.invitations("p1")[0]["seq"]
    world.scheduler.agents["夏"].agent.blackboard.write("loc", "")
    world.scheduler.agents["夏"].agent.location = ""
    out = world.answer_invitation("p1", seq, accept=True)
    assert out["gate"] == "inviter_where_unknown", out
    assert out["absent"] == "unknown", out
    name = world._tool_runtime.agent_names()["夏"]
    assert f"不知道{name}在哪" in out["refusal"] and "不是你没到场" in out["refusal"]
    assert "离开" not in out["refusal"], out["refusal"]


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
