"""实时编剧接在世界上那一半(3.11.0,批 3a)。

`test_director.py` 测的是纯函数;这个文件测的是**它真的接上了** ——
老板那三句判据在这儿变成可跑的断言:
「用户每操作一次应该就有新的剧情触发」·「剧情要有张力,NPC 要配合」·
「两个玩家走出两条不一样的线」。
"""
from __future__ import annotations

import json

import pytest

from _worldfile import open_world_at


def _act_once(world, pid):
    """他真做一件事 —— 走 `player_tool` 那条真路,不手塞事件。"""
    menu = world.player_options(pid)
    target = next((t for t in menu["targets"] for v in t["verbs"] if v["available"]), None)
    assert target is not None, "橱窗里该有一样点得动的东西"
    verb = next(v for v in target["verbs"] if v["available"])
    world.player_tool(pid, "interact", {"target": target["id"], "verb": verb["verb"]})


def _logs(world):
    return [e["payload"] for e in world.history(kind="director_log")["events"]]


# ── 老板判据一:每操作一次就有新剧情 ──────────────────────────────────────

def test_每操作一次都有一拍_而且一条沉默都没有(tmp_path):
    """🔴 **这条是老板那句话的可验形式**(裁决 §2.8)。

    判据写死成:连点二十次 → `director_log` 二十条 · **零沉默** ·
    `scene.source == "cached"` 零次。
    """
    with open_world_at(tmp_path / "d1.db") as world:
        world.player_move("p1", "cafe")
        world.tick(3)
        world.host_turn("p1")                       # 第一屏
        cached = 0
        # 🔴 **四种操作各来几次,不是二十次都点动词**(3.11.1,验收 A ①)。
        # 我 3a 那一版**二十次全是动词、一次没走** —— 而"走一步"那一半恰恰是坏的:
        # `arrive` 抢在 `acted` 前面拿走时刻名,`travel` 又不在回顾白名单上,
        # 于是那道守卫为假、编剧一个字不写。**它测的是我写对了的那一半。**
        # **试牙也要试对地方**,这个仓库记第五次了。
        # ⚠️ **三种操作轮着来**,而 `走一步` 是 A ① 逮住的那一种。
        # `conversation` / `invitation_settled` 那两种由纯函数那条用例钉
        # (`test_那两条who是她的事件_按payload筛才筛得出来`)—— 它们的 `who` 是**她**,
        # 而这里要量的是"每一次操作都有一拍",不是"哪几种事件算操作"。
        for i in range(20):
            kind = i % 3
            if kind == 0:                            # 走一步(A ① 那一半)
                # ⚠️ **走到他已经站着的地方不是一次操作**(引擎照实:没有 `travel`)
                # —— 所以每次都走去**另一个**地方,否则这条用例会拿自己的 no-op
                # 当"引擎漏了一拍"。
                here = world.player_location("p1")
                world.player_walk("p1", "workshop" if here != "workshop" else "cafe")
                world.player_location("p1")          # 惰性结算:让他到站
            elif kind == 1:                          # 宿主自报的一次操作
                world.player_action("p1", f"看了看第 {i} 眼")
            else:                                    # 点一个动词(要站在 cafe)
                world.player_walk("p1", "cafe")
                world.player_location("p1")
                _act_once(world, "p1")
            turn = world.host_turn("p1")
            if turn["scene"]["source"] == "cached":
                cached += 1
        logs = _logs(world)
        assert len(logs) == 20, (
            f"二十次操作只写了 {len(logs)} 拍 —— 走路那几次多半一个字没写")
        assert cached == 0, f"有 {cached} 次屏没换 —— 「每操作一次就有」不成立"
        assert all(str(row.get("line") or "").strip() for row in logs), "有一拍是沉默的"


def test_第一屏不开口_因为他还没做过任何事(tmp_path):
    """**编剧的输入永远是「他刚做了什么」,所以没有"刚"就不写。**

    ⚠️ 这**不是**沉默:那条纪律管的是「他做了一件事而世界没回应」,
    而第一屏他一件事都还没做 —— 那时开口写出来的是一句
    **关于什么都没发生的旁白**,而且它顶在开场白前面。
    """
    with open_world_at(tmp_path / "d2.db") as world:
        world.player_move("p1", "cafe")
        world.tick(3)
        first = world.host_turn("p1")
        assert first["trigger"] == "arrive"
        assert _logs(world) == [], "第一屏就写了一拍"
        assert first["scene"]["text"].startswith("第 "), first["scene"]["text"]


def test_没配key也每次都有一句_而且不抄他刚做的那句(tmp_path):
    """**没配 key 是这个引擎的默认状态** —— 那条承诺在默认状态下也要成立。
    ⚠️ 而这一句排在回顾**后面**,把回顾那句抄进来就是屏上连说两遍
    (端到端实跑逮的)。"""
    with open_world_at(tmp_path / "d3.db") as world:
        world.player_move("p1", "cafe")
        world.tick(3)
        world.host_turn("p1")
        _act_once(world, "p1")
        turn = world.host_turn("p1")
        logs = _logs(world)
        assert len(logs) == 1 and logs[0]["source"] == "mock"
        text = turn["scene"]["text"]
        # 「你端详了…」在这一屏上只许出现一次
        head = text.split("。")[0] + "。"
        assert text.count(head) == 1, f"同一句连说了两遍:{text}"


# ── 老板判据二:NPC 要配合 ────────────────────────────────────────────────

def test_编剧意图进了她的提示词_而且过期就消失(tmp_path):
    """口径 5:「编剧写了谁来,谁就真来、真说那句、在对话里追着这拍的意图走」。

    🔴 **走 `prompt_blocks` 那一个拼装点** —— 「调试视图另写一遍拼装就会撒谎」,
    而改提示词的人正是靠 `anima-world prompt` 逐块看它。
    """
    with open_world_at(tmp_path / "d4.db") as world:
        agent = next(iter(world.scheduler.agents))
        world.player_move("p1", "cafe")
        world.tick(3)
        # 手写一条意图进故事状态(编剧那条路已由上面几条覆盖)
        world.scheduler._memory_projection.stories["p1"] = {
            "intent": {"agent_id": agent, "line": "跟我来一趟",
                       "goal": "把那件事说清楚",
                       "until_tick": int(world.scheduler.clock) + 10},
        }
        blocks = world.debug_prompt(agent)["blocks"]
        names = [b["label"] for b in blocks]
        assert "director.intent" in names, names
        said = next(b["text"] for b in blocks if b["label"] == "director.intent")
        assert "把那件事说清楚" in said and "跟我来一趟" in said
        # 🔴 **锚点仍然最后落锤**:意图排在 `persona.anchor` 之前
        assert names.index("director.intent") < names.index("persona.anchor")

        # 过期就消失 —— 和 pin 同一个来源、同一个有效期
        world.tick(12)
        after = [b["label"] for b in world.debug_prompt(agent)["blocks"]]
        assert "director.intent" not in after, after


def test_被编剧钉住的人_排班带不走她(tmp_path):
    """口径 5 的另一半:**pin 要真拴**。

    引擎里早就写着「`emit_action` 的 walk 那一支从不查 `_occupying`」——
    所以这一版**自己拴**:只拴 walk 那一支、只对编剧的 pin 生效。
    """
    from anima_world.actions import ActionDescriptor

    with open_world_at(tmp_path / "d5.db") as world:
        sch = world.scheduler
        agent_id = next(iter(sch.agents))
        agent = sch.agents[agent_id].agent
        agent.location = "cafe"
        sch._memory_projection.stories["p1"] = {
            "intent": {"agent_id": agent_id, "line": "", "goal": "",
                       "until_tick": int(sch.clock) + 10},
        }
        assert sch._director_pinned(agent_id) is True
        moved = sch.emit_action(agent, ActionDescriptor(
            kind="walk", params={"location": "workshop"}))
        assert moved is False, "她被钉着,这一步不该走"
        assert agent_id not in sch._transit, "她起程了"

        world.tick(12)                       # 到点自己松开
        assert sch._director_pinned(agent_id) is False


# ── 老板判据三:两个玩家走出两条不一样的线 ────────────────────────────────

def test_两个玩家各有各的故事状态_互不相干(tmp_path):
    """「两个不同的玩家走出两条不一样的线」——**故事状态是按人分的**,
    而这一条是那句话在引擎里的地基:一个人的张力、相位、开着的线,
    不该被另一个人的操作推动。"""
    with open_world_at(tmp_path / "d6.db") as world:
        for pid in ("p1", "p2"):
            world.player_move(pid, "cafe")
        world.tick(3)
        world.host_turn("p1"); world.host_turn("p2")
        for _ in range(3):
            _act_once(world, "p1")
            world.host_turn("p1")
        _act_once(world, "p2")
        world.host_turn("p2")
        assert world.player_story("p1")["moves"] == 3
        assert world.player_story("p2")["moves"] == 1
        mine = {row["player_id"] for row in _logs(world)}
        assert mine == {"p1", "p2"}


# ── 真赌注:到期引擎自己动手扣 ────────────────────────────────────────────

def test_一条线到期没人管_那笔赌注真的掉了(tmp_path):
    """🔴 **「真」的判据是:到期没处理,引擎自己动手扣,不问模型。**

    一个只写在 `threads[]` 里、到期没有人执行的赌注,**和没有赌注是同一件事**,
    而玩家永远不知道自己其实什么都没输过。
    """
    with open_world_at(tmp_path / "d7.db") as world:
        sch = world.scheduler
        agent_id = next(iter(sch.agents))
        world.player_move("p1", "cafe")
        world.tick(2)
        before = world.relationship_summary(agent_id, "player:p1")["axes"]["sentiment"]
        sch._memory_projection.stories["p1"] = {
            "threads": [{"id": "t1", "promise": "她欠你一个解释", "with": agent_id,
                         "phase": "escalation", "opened_tick": 0,
                         "stake": {"kind": "relation", "amount": 0.2, "what": "她的信任"},
                         "due_tick": int(sch.clock) + 1, "closed": False}],
        }
        world.tick(3)
        after = world.relationship_summary(agent_id, "player:p1")["axes"]["sentiment"]
        assert after < before, f"赌注没兑现:{before} → {after}"
        collected = [r for r in _logs(world) if r["move"] == "collect"]
        assert collected and collected[0]["thread_id"] == "t1", _logs(world)


def test_contract里那一段_和闭集逐项相等(tmp_path):
    """消费方**按段做能力探测,不比版本号**。"""
    from anima_world.__main__ import contract_payload
    from anima_world import director as D

    seg = contract_payload()["director"]
    assert seg["moves"] == list(D.MOVES)
    assert seg["phases"] == list(D.PHASES)
    assert seg["stake_kinds"] == list(D.STAKE_KINDS)
    assert seg["subscribable"] is False, "载荷形状 3b 还要动,现在进白名单就是一句拿不掉的契约"
    from anima_world.events import SUBSCRIBABLE_EVENTS
    assert "director_log" not in SUBSCRIBABLE_EVENTS


def test_同一天派第二次人_屏上不许零字(tmp_path):
    """🔴 **验收 A ②(真站上量的:四次 approach 三次屏上零字)。**

    `claim_hail` 那道「一天一次」的闸防的是**她自己**一天叫你十次 —— 那是她的
    主动性,该有节制;而编剧是**世界的节奏**,它自己那把尺是每世界小时的上限。
    两本账混在一起的下场:同一天第二次派人被挡,`_director_apply` 拿不到
    `landed` 就 `return ""`,**屏上一个字都没有**。

    🔴 **而我 3a 那两条「零沉默」「≤6/世界小时」的判据,正是被这道闸挡出来的假绿**
    —— 它们看着绿,是因为根本没派出去几次。
    """
    with open_world_at(tmp_path / "d8.db") as world:
        agent = next(iter(world.scheduler.agents))
        holder = "player:p1"
        world.player_move("p1", "cafe")
        world.tick(2)
        first = world.scheduler._beat_hail(
            {"op": "hail", "agent_id": agent, "target": holder, "source": "director"})
        assert first, "第一次就没派出去"
        # 同一天再派一次 —— **编剧那条路不受一天一次的闸管**
        again = world.scheduler._beat_hail(
            {"op": "hail", "agent_id": agent, "target": holder, "source": "director"})
        assert again, "同一天第二次派人被挡了 —— 那道闸管的是她自己,不是世界的节奏"
        assert again[0]["payload"]["source"] == "director", again[0]["payload"]
        # 🔴 **两本账各记各的,两个方向都要成立**:
        # ① 编剧派了两次,**没有花掉她自己那一次**;
        assert world.scheduler._beat_hail(
            {"op": "hail", "agent_id": agent, "target": holder}), "编剧把她的额度花掉了"
        # ② 她自己用掉之后,**编剧照旧派得动**。
        assert world.scheduler._beat_hail(
            {"op": "hail", "agent_id": agent, "target": holder}) == [], "她的一天一次没了"
        assert world.scheduler._beat_hail(
            {"op": "hail", "agent_id": agent, "target": holder,
             "source": "director"}), "她用完额度之后编剧就派不动了 —— 那是一本账不是两本"


def test_她没真的来时_退模板句而不是零字(tmp_path):
    """**「不许沉默」是这一层的头条**,而 3a 那一版在「她没来成」时 `return ""`。

    她没来 → 别说她来了(一句和世界对不上的话比不说更坏);但**也不许什么都不说**
    —— 退成一句指着他刚做的事的 `breathe`。
    """
    with open_world_at(tmp_path / "d9.db") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        said = world._director_apply(
            "p1", {"move": "approach", "who": "根本不在这个世界里的人",
                   "line": "喂", "why": "", "promise": "", "stake": None,
                   "source": "mock"},
            tension_before=0.1, phase="setup", tick=int(world.scheduler.clock),
            place="cafe", thread=None, pin_ticks=12, due_ticks=100, capped=False,
            forbidden_ops=set(), recap=["你端详了老橡树。"], place_name="咖啡店")
        assert said, "她没来成,而屏上一个字都没有 —— 那正是 A ② 那条"


def test_禁区里的地点_站在那儿的人不进候选(tmp_path):
    """🔴 **验收 A ④**:§2.1⑤ 那四道闸里写着 `forbidden.locations`,而运行时
    **一处都没查** —— 创作者写下的「这一周别碰那间地下室」在引擎里是一句摆设。
    **一道声明了却没人执行的闸,和没有那道闸一样,只是更贵**:作者以为自己挡住了。
    """
    with open_world_at(tmp_path / "da.db") as world:
        here = {}
        for aid, brain in world.scheduler.agents.items():
            here.setdefault(str(brain.agent.location or ""), []).append(aid)
        banned = next(p for p, who in here.items() if p and who)
        blocked = set(here[banned])
        got = world._director_candidates("p1", "cafe", banned_places={banned})
        assert not (blocked & {c["id"] for c in got}), (
            f"{banned} 在禁区里,而站在那儿的 {blocked} 还在候选里")
        # 不给禁区时他们照旧在
        loose = {c["id"] for c in world._director_candidates("p1", "cafe")}
        assert blocked & loose


# ── 🔴 真走客户端接口那条(3.11.1,验收 C ①)────────────────────────────────
#
# **教训写在这儿,因为这一条就是它的判据**:3.11.0 的编剧在真部署上**从来没跑过**
# —— `_director_reply` 调的是 `client.complete_sync(...)`,而
# `ConfigBackedLLMClient` 只有 `complete` / `stream`。`AttributeError` 被那句
# 「编剧挂了绝不掀翻这一屏」的 `except` 吞成一条 WARNING,于是**配了 key 的世界
# 每一拍都是模板句、永远不派人**,而 2355 条测试全绿 —— 因为它们**全走 mock 那条路**。
#
# **一个吞掉一切异常的降级路径,会把「方法名打错」和「模型这次没答上来」
# 变成同一个现象。** 所以闸必须有一条**真走客户端接口**的:假客户端只实现
# 真接口(`complete`),方法名对不上就当场炸。


class _FakeDirectorLLM:
    """只实现**真接口**的假客户端 —— `complete` 是协程,和 `ConfigBackedLLMClient`
    逐字同形。⚠️ **有意不实现 `complete_sync`**:那正是要咬住的那个名字。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0          # 编剧那条路被调了几次
        self.scenes = 0         # 主持人那条路被调了几次

    async def complete(self, messages):
        # ⚠️ **同一个背景槽,两条路都用它**(主持人写场景 / 编剧写这一拍)——
        # 假客户端要分得开,否则编剧那段 JSON 会被当成场景印到屏上。
        blob = "".join(m.get("content") or "" for m in messages)
        if "你是一个文字冒险游戏的**编剧**" in blob:
            self.calls += 1
            return self.reply
        self.scenes += 1
        # 主持人那条:**把「刚发生的事」原样念回去**。真模型会把它编织进散文里,
        # 而这里要断的是「编剧那一句到底有没有走到这一屏的输入里」——
        # 念回去让那件事**看得见**。
        happened = [ln[2:] for ln in blob.splitlines() if ln.startswith("- ")]
        return ("".join(happened) or "这儿很安静。") + "\n看看那棵树\n跟人说说话"


def _with_key(world, reply: str) -> _FakeDirectorLLM:
    fake = _FakeDirectorLLM(reply)
    world.config_set("llm.api_key", "sk-test")
    world.chat_service._background_llm = fake
    return fake


def test_配了key时_编剧真的调得动客户端并派得出人(tmp_path):
    """🔴 **断的是 `host_turn` 那一屏,不是 `director_log`** —— C 特意点名的:
    日志里写着 `approach` 而屏上是模板句,两者都"绿"。"""
    with open_world_at(tmp_path / "dk.db") as world:
        agent = next(iter(world.scheduler.agents))
        name = world.scheduler.agent_display_name(agent)
        world.player_move("p1", "cafe")
        world.tick(2)
        world.host_turn("p1")
        fake = _with_key(world, '{"move":"approach","who":"%s",'
                                '"line":"跟我来一趟","why":"推一把"}' % agent)
        _act_once(world, "p1")
        turn = world.host_turn("p1")

        assert fake.calls >= 1, "配了 key 而客户端一次都没被调到"
        logs = _logs(world)
        assert logs[-1]["move"] == "approach", logs[-1]
        assert logs[-1]["source"] == "llm", logs[-1]
        # 🔴 **屏上真的有那个人和那句话**
        text = turn["scene"]["text"]
        assert name in text and "跟我来一趟" in text, text


def test_配了key而模型读不懂_退模板句但屏上不许空(tmp_path):
    """真客户端答了一句读不懂的话 —— 退 `breathe`,而**屏上照旧有字**。"""
    with open_world_at(tmp_path / "dk2.db") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        world.host_turn("p1")
        fake = _with_key(world, "我觉得应该让恺撒出场")
        _act_once(world, "p1")
        turn = world.host_turn("p1")
        assert fake.calls >= 1
        assert _logs(world)[-1]["move"] == "breathe"
        assert turn["scene"]["text"].strip(), "读不懂就把屏清空了"


def test_故事那扇门给的是人话_而不是让宿主自己译(tmp_path):
    """🔴 **player 带回的那条**:`tension` 是浮点、`phase` 是枚举,而站点按纪律
    **两样都不上屏**(不自己译、不给玩家看数字)—— 于是这两格在引擎里有、
    在屏上没有,**等于没有这个读数**(`ask_ready_text` 那条先例逐字同一句)。

    ⚠️ **分档表只有一份**:各译一遍的话,同一个世界会对同一个人说两种话。
    """
    from anima_world import director as D

    with open_world_at(tmp_path / "dt.db") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        sch = world.scheduler
        sch._memory_projection.stories["p1"] = {
            "tension": 0.62, "tension_tick": int(sch.clock), "phase": "climax",
            "threads": [{"id": "t1", "promise": "她欠你一个解释", "with": "夏",
                         "phase": "escalation", "opened_tick": 0, "stake": None,
                         "due_tick": int(sch.clock) + 12 * 30, "closed": False}],
            "recent": [], "intent": {}, "moves": 3,
        }
        got = world.player_story("p1")
        assert got["tension_text"] == D.tension_text(got["tension"])
        assert got["tension_text"] and not got["tension_text"].isascii()
        assert got["phase_text"] == D.PHASE_LABELS["climax"]
        thread = got["threads"][0]
        assert thread["phase_text"] == D.PHASE_LABELS["escalation"]
        assert thread["due_text"] and not thread["due_text"].isascii()
        # 契约要报这几格键名 —— 宿主按段对表,不按这份代码猜
        from anima_world.__main__ import contract_payload
        seg = contract_payload()["director"]
        assert set(seg["text_keys"]) <= set(got)
        assert set(seg["thread_text_keys"]) <= set(thread)


def test_查无此人和还没动过手_这扇门自己分得开(tmp_path):
    """🔴 **platform 带回的那条**:上一版两种都答一份空故事,于是壳只能自己去翻
    `player_join` —— **让消费方补一个只有引擎答得出的判断,就是让它持一份对名册
    的猜测**,而猜错了不报错:一个打错的 pid 会得到一份看起来完全正常的空故事。
    """
    with open_world_at(tmp_path / "dn.db") as world:
        # 从没露过面的人
        ghost = world.player_story("从来没有过这个人")
        assert ghost["known"] is False, ghost
        assert ghost["moves"] == 0 and ghost["threads"] == []

        # 认识、但还没动过手
        world.player_move("p1", "cafe")
        world.tick(2)
        world.host_turn("p1")
        mine = world.player_story("p1")
        assert mine["known"] is True, mine
        assert mine["moves"] == 0, "他还没动过手,却有拍了"

        # 契约要报这张键表
        from anima_world.__main__ import contract_payload
        assert set(contract_payload()["director"]["story_keys"]) == set(mine)


def test_时钟的内存读法_零IO而且不冒充真答案(tmp_path):
    """🔴 **它不是 `clock` 的替代**(platform 带回)。`RedisClock` 有意不缓存 ——
    「不缓存意味着任何一个进程随时读到的都是真的现在」,而两个进程各持一份
    "现在",世界就分叉了。所以这一条只给**只读、能容忍陈一点**的路用。
    """
    with open_world_at(tmp_path / "dc.db") as world:
        sch = world.scheduler
        world.tick(3)
        # 🔴 **真量一次 I/O,别只断相等**(3.11.2,验收 A ⑤)。
        # 「零 I/O」是这条读法存在的**全部理由**,而"两个数相等"对一个
        # 每次都打 Redis 的实现**也成立** —— 那样这条用例就什么都没验。
        calls: list[str] = []
        raw = sch.redis.execute_command

        def _spy(*a, **k):
            calls.append(str(a[0]).upper())
            return raw(*a, **k)

        sch.redis.execute_command = _spy
        try:
            cached = sch.clock_cached
            assert not calls, f"内存读法打了 Redis:{calls}"
            _ = sch.clock                       # 对照:真读法**必然**打一次
            assert calls, "真读法一次 Redis 都没打 —— 那这条对照就没意义了"
        finally:
            sch.redis.execute_command = raw
        assert cached == sch.clock
        assert world.world_time_cached().day == world.world_time().day

        # 🔴 **别人推的 tick,这个进程看不见** —— 而那正是它诚实的地方:
        # 它答的是「我上一次看到的」,不是「世界的现在」。
        sch._clock_box().set(sch.clock + 500)
        assert sch.clock_cached != sch.clock, (
            "内存读法跟着别的进程动了 —— 那它就不是内存读法")
        assert sch.clock_cached < sch.clock

        # 而一次真的推进会把它带上
        world.tick(1)
        assert sch.clock_cached == sch.clock


def test_两条只读出口的屏上_没有裸英文枚举也没有python字面量(tmp_path):
    """🔴 **验收 B ③**:`guidance show` 印的是
    `{'setup': 1, 'escalation': 3, 'climax': 5}` —— 花括号、引号、冒号全是给机器
    看的,而 `setup` 是枚举名。

    ⚠️ **别数源码,去问屏幕**(3.7.0 那条判据):这里真敲两条出口,
    对**印出来的字**断言。
    """
    import io as _io
    import contextlib

    from anima_world.__main__ import main as _main

    with open_world_at(tmp_path / "sc.db") as world:
        world.player_move("p1", "cafe")
        world.tick(2)

    for argv in (["guidance", "show", "--world-id", "w"],
                 ["player", "story", "--player", "p1", "--world-id", "w"]):
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            _main(argv)
        screen = buf.getvalue()
        assert "**" not in screen, f"{argv} 印了裸星号:{screen[:200]}"
        assert "{'" not in screen and "': " not in screen, (
            f"{argv} 印了 Python 字面量:{screen[:200]}")
        for enum in ("setup", "escalation", "climax", "release",
                     "breathe", "approach", "complicate"):
            assert enum not in screen, f"{argv} 印了裸英文枚举 {enum!r}:{screen[:200]}"


def test_契约报三张人话表_而且和纯模块逐项相等():
    """🔴 **分档表只有一份**(验收 B ⑤,tool 三处在等)—— 各译一遍的话,
    同一个世界会在引擎屏 / 创作台 / 站点上说三种话,而没有一处会报错。"""
    from anima_world import director as D
    from anima_world.__main__ import contract_payload

    seg = contract_payload()["director"]
    assert seg["move_labels"] == dict(D.MOVE_LABELS)
    assert seg["phase_labels"] == dict(D.PHASE_LABELS)
    assert seg["stake_labels"] == dict(D.STAKE_LABELS)


def test_那一屏的最近几拍_只给他自己的(tmp_path):
    """🔴 **这一格 3.11.1 修了,而它零覆盖**(3.11.2,验收 A ①:把它改回
    3.11.0 的写法,2371 条全绿)。

    `recent_log` 不按人过滤的下场有两层:**跨玩家剧透**(别人的线、别人的赌注),
    以及**和同一屏上面那三格自相矛盾**(张力 / 相位 / 开着的线都是按人取的)。
    ⚠️ **取 200 条再筛,不是取 10 条再筛** —— 多人世界里最近 10 条可能一条都不是
    他的,那会让这一格永远空着。
    """
    with open_world_at(tmp_path / "rl.db") as world:
        agent = next(iter(world.scheduler.agents))
        for pid in ("p1", "p2"):
            world.player_move(pid, "cafe")
        world.tick(2)
        now = int(world.scheduler.clock)
        # p1 一拍,然后 p2 连写十几拍 —— 把 p1 那条挤出"最近 10 条"
        world._director_apply(
            "p1", {"move": "reveal", "who": agent, "line": "只给 p1 的那一句",
                   "why": "", "promise": "", "stake": None, "source": "mock"},
            tension_before=0.2, phase="setup", tick=now, place="cafe", thread=None,
            pin_ticks=12, due_ticks=0, capped=False, forbidden_ops=set(),
            recap=[], place_name="咖啡店")
        for i in range(14):
            world._director_apply(
                "p2", {"move": "breathe", "who": "", "line": f"p2 的第 {i} 句",
                       "why": "", "promise": "", "stake": None, "source": "mock"},
                tension_before=0.2, phase="setup", tick=now, place="cafe",
                thread=None, pin_ticks=12, due_ticks=0, capped=False,
                forbidden_ops=set(), recap=[], place_name="咖啡店")

        mine = world.player_story("p1")["recent_log"]
        assert mine, "他自己那一拍被别人的挤没了 —— 取 10 条再筛就是这个下场"
        for row in mine:
            assert (row["payload"] or {}).get("player_id") == "p1", (
                f"别人的剧情漏到他这一屏了:{row['payload']}")
        assert any("只给 p1" in str((r["payload"] or {}).get("line")) for r in mine)


# ── 真站第三轮那三条(3.11.2)────────────────────────────────────────────────


def test_编剧默认开着():
    """🔴 **默认关的下场是实测出来的**:三个世界换到 3.11.1 之后编剧根本没开 ——
    **老板的主命题在线上不存在**,而屏幕上一切正常。

    这个仓库的规矩是「引擎默认值全关」,而这一条是**有意的例外**:
    编剧不是一个特性,是产品命题本身。
    ⚠️ 它**没配 key 也照跑**(整条 mock 路是活的),所以"默认开"不会让任何
    世界因为缺 key 而变坏。
    """
    from anima_world.config_store import _DEFAULTS

    assert _DEFAULTS["director.enabled"][0] is True
    said = _DEFAULTS["director.enabled"][4]
    assert "DEFAULT ON" in said, said


def test_编剧关着时_动词之后屏照样要换(tmp_path):
    """**「每操作一次就有新剧情」在 `director.enabled=false` 下退化成什么,要写清**:
    至少 `acted` 那个时刻与「上一屏之后发生了什么」那半句要在(批 1.1 承诺的)——
    **编剧只是这一屏的上半场**,不是它的全部。
    """
    with open_world_at(tmp_path / "off.db") as world:
        world.config_set("director.enabled", False)
        world.player_move("p1", "cafe")
        world.tick(2)
        first = world.host_turn("p1")
        _act_once(world, "p1")
        turn = world.host_turn("p1")
        assert turn["trigger"] == "acted", turn["trigger"]
        assert turn["scene"]["source"] != "cached"
        assert turn["scene"]["text"] != first["scene"]["text"], "屏一个字没变"
        # 编剧关着 —— 一拍都不该写
        assert _logs(world) == []


def test_同一个tick里连聊十轮_每一轮都要换屏(tmp_path):
    """🔴 **验收 A ② 那条,逐字**:龙族一个 tick 是 4.2 真实分钟,而 3.11.2 那把
    钥匙上「他说过话没有」读的是 `last_contact_tick` —— **世界时钟**。
    于是玩家在一个 tick 里聊十轮,那个数一格没动:**十轮里只有一轮换屏**,
    而那正是"真站第四轮"报上来的症状本身。

    ⚠️ 3.11.2 那两条用例手喂 `clock + 1` 才绿 —— **把测试改绿不是把东西修好**。
    这条用例**一个 tick 都不推**:时钟从头到尾不动,而十轮要有十屏。
    """
    with open_world_at(tmp_path / "ten.db") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        world.host_turn("p1")
        frozen = int(world.scheduler.clock)
        seen: list[str] = []
        for i in range(10):
            _drive_chat(world, "p1")
            turn = world.host_turn("p1")
            seen.append(turn["scene"]["source"])
        assert int(world.scheduler.clock) == frozen, "这条用例不许推时钟"
        cached = [i for i, src in enumerate(seen) if src == "cached"]
        assert not cached, f"同一个 tick 里第 {cached} 轮没换屏:{seen}"


def test_聊天那格水位_是轮数不是时钟(tmp_path):
    """**闸的那一格自己也要量**:上面那条屏级用例在 `record_chat_turn` 这条门上
    会被 `move_seq` 兜住(它顺带发一条 `conversation`)—— 而钥匙上这一格
    要挡的是**另一条门**(会话一直开着、一条事件都不发)。所以直接量水位。
    """
    with open_world_at(tmp_path / "seq.db") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        frozen = int(world.scheduler.clock)
        got = []
        for _ in range(3):
            _drive_chat(world, "p1")
            got.append(world._last_chat("p1")[0])
        assert int(world.scheduler.clock) == frozen
        assert got == sorted(set(got)) and len(set(got)) == 3, (
            f"同一个 tick 里聊三轮,水位是 {got} —— 它读的还是时钟")


def test_回来那一屏之后聊一轮_编剧必须写那一拍(tmp_path):
    """🔴 **线上复现(龙族,`director.enabled` 刚设 true)**:玩家真聊了一轮
    (`/chat` 200,诺诺回了话),之后两发 `/internal/v1/host` 都是
    `trigger=return` / `source=cached`,`director_log` 仍然 **0**。

    两条,都真:

    ① **那个 `trigger=return` 是上一屏的抬头,不是这一屏的**:`trigger is None`
      时返回里报的是 `last.trigger`(为了 `ask_ready` 那一格说真话)。
      **「这一屏没开口」只有 `scene.source == "cached"` 说得出来**,`trigger` 说不出。

    ② **而编剧那道守卫又漏了聊天那一半**:3.11.2 把 `chat_tick` 加进了**时刻钥匙**,
      于是聊一轮之后屏会重写、抬头是 `acted` —— 可 `acted_since` 还只比 `move_seq`。
      🔴 **这和 3.11.1 验收 A ① 逮的是同一个形状**:开屏用的是一个事实,
      而判"他动过手没有"用的是另一个。我修那次时**只把守卫从时刻名换成 `move_seq`,
      没有回头问一句「他动过手」这件事一共有几个来源」** —— 一个月内第二次。
    """
    with open_world_at(tmp_path / "back.db") as world:
        agent = next(iter(world.scheduler.agents))
        world.player_move("p1", "cafe")
        world.tick(2)
        world.host_turn("p1")                       # 第一屏 arrive
        world.tick(288 * 2)
        world.player_leave("p1")                    # 他真的走了
        back = world.host_turn("p1")
        assert back["trigger"] == "return", back["trigger"]
        before = len(_logs(world))

        # 他回来之后**聊了一轮** —— 站点那条真门,不喂 tick(验收 A ②)
        _drive_chat(world, "p1")
        turn = world.host_turn("p1")

        assert turn["scene"]["source"] != "cached", (
            f"聊了一轮而这一屏没开口(抬头 {turn['trigger']} 是上一屏的)")
        assert turn["trigger"] == "acted", turn["trigger"]
        assert len(_logs(world)) == before + 1, (
            f"屏开了而编剧一个字没写:{before} → {len(_logs(world))}")


# ── 「他动过手没有」那张钥匙表:闸和守卫必须逐格都认(3.11.2,真站第四轮)──────

def _drive_chat(world, pid):
    """聊一轮 —— **走站点那条真门**(`record_chat_turn`),不发事件(硬不变量)。

    🔴 **不许手喂 `clock + 1`**(3.11.3,验收 A ②)。3.11.2 那两条用例正是那么
    写的,于是一个**按世界时钟**记水位的实现在它们眼里是好的 —— 而真门传的是
    `scheduler.clock`,龙族一个 tick 是 4.2 真实分钟:玩家一个 tick 里聊十轮,
    水位一格没动。**把测试改绿不是把东西修好**,而这条用例当时是绿的。
    """
    world.record_chat_turn(
        next(iter(world.scheduler.agents)), pid,
        [{"role": "user", "content": "在吗"},
         {"role": "assistant", "content": "在。"}])


#: 每一格钥匙**怎么让它动**。⚠️ 这张表要和 `host.ACTED_GRAINS` 逐格对上 ——
#: 加一格而忘了写驱动,下面第一条用例当场红。
_ACTED_DRIVERS = {
    "move_seq": _act_once,
    "chat_seq": _drive_chat,
}


def test_钥匙表上每一格都有驱动():
    """加一格 `ACTED_GRAINS` 而不写驱动 = 那一格没有人测过。"""
    from anima_world import host

    assert set(_ACTED_DRIVERS) == set(host.ACTED_GRAINS), (
        f"驱动表 {sorted(_ACTED_DRIVERS)} vs 钥匙表 {sorted(host.ACTED_GRAINS)}")


@pytest.mark.parametrize("grain", list(_ACTED_DRIVERS))
def test_每一格钥匙动了_屏要开口而且编剧要写(tmp_path, grain):
    """🔴 **一格钥匙有两个读者,而它们分岔过两次**(见 `host.ACTED_GRAINS`):
    开屏那道闸认得,编剧那道守卫不认得 —— 下场是**屏重写了而剧情一个字没有**,
    零报错。这条用例把两个读者钉在同一格上逐格问一遍。
    """
    with open_world_at(tmp_path / f"g-{grain}.db") as world:
        world.player_move("p1", "cafe")
        world.tick(3)
        world.host_turn("p1")                       # 第一屏(他还没动过手)
        before = len(_logs(world))
        _ACTED_DRIVERS[grain](world, "p1")
        turn = world.host_turn("p1")
        assert turn["trigger"] == "acted", f"{grain}:闸没认 → {turn['trigger']}"
        assert turn["scene"]["source"] != "cached", f"{grain}:屏没开口"
        assert len(_logs(world)) == before + 1, (
            f"{grain}:屏开了而编剧一个字没写({before} → {len(_logs(world))})")


# ── 真站第四轮 ①②(3.11.2)─────────────────────────────────────────────────

def _buy_shears(world, pid):
    world.player_topup(pid, 100)
    world.player_buy(pid, "cafe", "garden_shears")


def _start_long_verb(world, pid):
    """点一个**有 `duration` 的**动词 —— 真站上那个「拉票」就是这一种。"""
    res = world.player_tool(pid, "interact",
                            {"target": "tree:harbor_oak", "verb": "嫁接"})
    assert res.get("ok"), res
    return res


def test_走动词对话走_四连屏_一次cached都不许有(tmp_path):
    """🔴 **真站第四轮 ① 的判据,逐字**:六件事里三件 `scene.source=cached`,
    **同一句 LLM 台词连出三屏**。

    根有两条,这条用例一次咬住两条:

    ① **长动词点下去只发 `entity_engage`** —— 那条 `entity_interaction` 要等
      一小时后收尾才发。于是「拉票」点完 `move_seq` 一格没动。
      ⚠️ 我 3a 那条二十连击的用例点的全是**短**动词,所以它绿着。
      **同一个"试牙也要试对地方"的教训,这是第三次。**
    ② 聊完一轮不推 `move_seq`(会话不关就没有事件)—— `chat_tick` 那一格。
    """
    with open_world_at(tmp_path / "four.db") as world:
        agent = next(iter(world.scheduler.agents))
        world.player_move("p1", "cafe")
        world.tick(3)
        _buy_shears(world, "p1")
        world.host_turn("p1")                        # 第一屏

        seen: list[str] = []
        world.player_walk("p1", "workshop"); world.player_location("p1")
        seen.append("走")
        world.player_walk("p1", "cafe"); world.player_location("p1")
        t = world.host_turn("p1")
        screens = [t]

        _start_long_verb(world, "p1")                # 长动词(真站那个「拉票」)
        screens.append(world.host_turn("p1"))

        world.record_chat_turn(agent, "p1", [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "嗯。"}])
        screens.append(world.host_turn("p1"))

        world.player_walk("p1", "workshop"); world.player_location("p1")
        screens.append(world.host_turn("p1"))

        cached = [i for i, s in enumerate(screens) if s["scene"]["source"] == "cached"]
        assert not cached, f"第 {cached} 屏是 cached:{[s['trigger'] for s in screens]}"
        texts = [s["scene"]["text"] for s in screens]
        assert len(set(texts)) == len(texts), "四屏里有两屏一模一样"
        # 四步操作 → 四拍,一条沉默都没有
        assert len(_logs(world)) == 4, [l["move"] for l in _logs(world)]


class _PromptFake:
    """按**提示词里的名单**挑人的假客户端 —— 挑第一个,并把历史那一段抄进
    `promise`。真模型没有理由换人,所以"挑第一个"正是真站上那个坏法。"""

    def __init__(self):
        self.prompts: list[str] = []
        self.picked: list[str] = []

    async def complete(self, messages):
        import re

        blob = "".join(m.get("content") or "" for m in messages)
        if "你是一个文字冒险游戏的**编剧**" not in blob:
            happened = [ln[2:] for ln in blob.splitlines() if ln.startswith("- ")]
            return ("".join(happened) or "这儿很安静。") + "\n看看那棵树\n跟人说说话"
        self.prompts.append(blob)
        ids = re.findall(r"\(id=([^),]+)\)", blob)
        who = ids[0] if ids else ""
        self.picked.append(who)
        # `promise` 抄他这一路最后那一句 —— **断的是"他自己的路走没走进模型手里"**
        # ⚠️ 只收**紧跟在那句话后面**那一段 `- ` —— 提示词更下面还有一份名单
        # 也是 `- ` 开头,连着收会把「沈亦柔(id=柔)」当成他走过的路。
        been: list[str] = []
        if "他这一路走过来做过的事" in blob:
            for ln in blob.split("他这一路走过来做过的事")[-1].splitlines()[1:]:
                if not ln.startswith("- "):
                    break
                been.append(ln[2:])
        promise = (been[-1] if been else "空")[:18]
        return ('{"move":"approach","who":"%s","line":"来一趟","why":"推一把",'
                '"promise":"%s"}' % (who, promise))


def test_两个玩家六步不同_编剧的输入和产出都得不同(tmp_path):
    """🔴 **真站第四轮 ②,老板那句「两个玩家走出两条不一样的线」的可验形式**。

    量出来的:两人做完全不同的六件事,而故事页**线数 1=1、相位同、张力同、
    对手同**;4 次 approach 全指同一个人(池子里有 13 个);B 那条 `why` 写着
    「玩家刚入场还没做什么」,而他已经走过报刊亭、聊过路明非。

    根**不在模型**:编剧手上只有「上一屏之后」那一条,两个人喂进去的输入
    一模一样。**故事的分岔在输入里。**
    """
    with open_world_at(tmp_path / "two.db") as world:
        agents = list(world.scheduler.agents)
        world.player_move("p1", "cafe")
        world.player_move("p2", "cafe")
        world.tick(3)
        _buy_shears(world, "p1")
        world.host_turn("p1")
        world.host_turn("p2")
        fake = _PromptFake()
        world.config_set("llm.api_key", "sk-test")
        world.chat_service._background_llm = fake

        # p1:买剪子 → 长动词 → 走 —— p2:只走路 + 聊天
        _start_long_verb(world, "p1")
        world.host_turn("p1")
        world.player_walk("p1", "workshop"); world.player_location("p1")
        world.host_turn("p1")

        world.player_walk("p2", "workshop"); world.player_location("p2")
        world.host_turn("p2")
        # ⚠️ 这儿**不聊天**:配了 key 之后那扇门会连带叫醒会话总结那条真路
        # (`chat_session._summarize`),而这条用例要断的是编剧。
        # 换一个动作照样把两个人的历史拉开 —— 这条用例问的是"输入不同,
        # 产出就该不同",不是"聊天这条路通不通"(那条由上面那张钥匙表钉)。
        world.player_action("p2", "在报刊亭前站了一会儿")
        world.host_turn("p2")

        s1, s2 = world.player_story("p1"), world.player_story("p2")
        p1 = [t["promise"] for t in s1["threads"]]
        p2 = [t["promise"] for t in s2["threads"]]
        assert p1 and p2, (p1, p2)
        assert set(p1) != set(p2), f"两个人走出同一条线:{p1} vs {p2}"
        # 连着两拍不许是同一张脸
        assert fake.picked[0] != fake.picked[1], fake.picked


def test_连着两拍不许派同一个人_除非线开在他身上():
    """软闸,不是硬闸:**筛空了就整份还回去** —— 一个空 cast 会让编剧沉默,
    而「不许沉默」是硬纪律。而线开在谁身上,收线就得是谁。"""
    from anima_world import director as D

    pool = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert [c["id"] for c in D.select_cast(pool, recent=["a"])] == ["b", "c"]
    # 全被让位 → 整份还回去(宁可重复,不可沉默)
    assert [c["id"] for c in D.select_cast(pool[:1], recent=["a"])] == ["a"]
    # 线开在 a 身上 → a 留着
    assert [c["id"] for c in D.select_cast(pool, recent=["a"], keep="a")] == ["a", "b", "c"]


def test_相位那句和张力那句_不许在同一屏上打架(tmp_path):
    """🔴 真站第四轮 ④:`phase=climax`(「到节骨眼了」)和 `tension=0.1`(「松弛」)
    **并排印在同一屏上**。两句话都是引擎说的 —— 这不是模型胡说,是引擎自己
    对同一时刻说了两句相反的话,而**没有一处会报错**。

    这道闸量的是**写下那一拍的那一刻**(衰减是后来的事,一条搁了三天的线
    松下来是真的)。
    """
    from anima_world import director as D

    with open_world_at(tmp_path / "phase.db") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        world.host_turn("p1")
        for i in range(12):
            here = world.player_location("p1")
            world.player_walk("p1", "workshop" if here != "workshop" else "cafe")
            world.player_location("p1")
            world.host_turn("p1")
        rows = _logs(world)
        assert rows, "一拍都没写"
        for row in rows:
            phase, after = str(row.get("phase") or ""), float(row.get("tension_after") or 0)
            if phase == "climax":
                assert after >= D.PHASE_TARGET["escalation"], (
                    f"屏上会同时说「{D.PHASE_LABELS['climax']}」和"
                    f"「{D.tension_text(after)}」:{row}")


def test_source那几格_和客户端真被调过几次对得上账(tmp_path):
    """🔴 **验收 A ④ / B+C ② 的判据,逐字**:真站 12 拍里 10 条 `refused`,
    而假客户端只被调过 6 次 —— **读数自己就对不上,而没有一处报错**。

    `refused` 的意思是「问过模型,而它答的不在闭集里」。算术选了 `breathe`、
    没人可派、额度用完、让给锚点 —— 四种**根本没问过模型**,却都被记成它。
    🔴 **拆三合一时最容易犯的错,是拆出一个新的三合一。**

    判据能证伪:`refused` 的条数 **≤** 客户端真被调过的次数。
    """
    from anima_world import director as D

    with open_world_at(tmp_path / "src.db") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        world.host_turn("p1")
        fake = _with_key(world, "我觉得应该让恺撒出场")   # 永远读不懂 → 真 refused
        for i in range(12):
            here = world.player_location("p1")
            world.player_walk("p1", "workshop" if here != "workshop" else "cafe")
            world.player_location("p1")
            world.host_turn("p1")
        rows = _logs(world)
        by = {}
        for row in rows:
            by[row["source"]] = by.get(row["source"], 0) + 1
        refused = by.get("refused", 0)
        assert refused <= fake.calls, (
            f"{refused} 拍记成「模型答的不在闭集里」,而客户端只被调过 "
            f"{fake.calls} 次:{by}")
        # 而没走模型的那几种,都得落在闭集里(不许再出现一个新桶)
        assert set(by) <= set(D.SOURCES), f"冒出闭集之外的 source:{sorted(by)}"


def test_故事那一屏_不印浮点也不印两句打架的话(tmp_path):
    """🔴 **验收 A ⑤ / B+C ④**:上一版那一行是

        张力 0.00(松弛)· 这条线到节骨眼了 · 编剧一共写过 3 拍
        还没有开着的线。

    三宗罪挤在两行里:`0.00` 是给机器看的(而这一屏正是「两样都别自己译」
    那条规矩的作者)· 「到节骨眼了」和「松弛」**互相打脸**(相位不倒退,
    张力会衰减)· 紧接着又说「还没有开着的线」—— 一条不存在的线到了节骨眼。

    ⚠️ **别数源码,去问屏幕**。
    """
    import contextlib
    import io as _io

    from anima_world.__main__ import main as _main

    with open_world_at(tmp_path / "sl.db") as world:
        agent = next(iter(world.scheduler.agents))
        world.player_move("p1", "cafe")
        world.tick(2)
        now = int(world.scheduler.clock)
        # 走到 climax,然后让时间过去 —— 张力衰减,而相位不倒退
        world._director_apply(
            "p1", {"move": "complicate", "who": agent, "line": "出事了",
                   "why": "", "promise": "", "stake": None, "source": "mock"},
            tension_before=0.65, phase="escalation", tick=now, place="cafe", thread=None,
            pin_ticks=12, due_ticks=0, capped=False, forbidden_ops=set(),
            recap=[], place_name="咖啡店")
        world.tick(288 * 3)
        story = world.player_story("p1")

    assert story["phase"] == "climax" and story["tension"] < 0.25, story
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        _main(["player", "story", "--player", "p1", "--world-id", "w"])
    screen = buf.getvalue()
    assert "0.00" not in screen and "张力 " not in screen, f"印了浮点:{screen}"
    assert not ("到节骨眼了·" in screen or "这条线到节骨眼了" in screen), screen
    # 那两句不许并排出现在同一屏上
    assert not ("到节骨眼了,松弛" in screen), screen
    assert "到过" in screen, f"松下来的高潮该说成一句:{screen}"


def test_同一个tick里_走动词聊走四连_一屏cached都不许有(tmp_path):
    """🔴 **真站第五轮的验收判据,逐字**(11 屏 6 个 `cached`)。

    ⚠️ **时钟一格都不推** —— 真站上那 11 屏多半落在同几个 tick 里
    (龙族 4.2 真实分钟一个 tick),而按 tick 记水位的实现在这条用例下才会现形。
    """
    with open_world_at(tmp_path / "5th.db") as world:
        agent = next(iter(world.scheduler.agents))
        world.player_move("p1", "cafe")
        world.tick(3)
        world.player_topup("p1", 100)
        world.player_buy("p1", "cafe", "garden_shears")
        world.host_turn("p1")
        frozen = int(world.scheduler.clock)

        seen = []
        world.player_walk("p1", "workshop"); world.player_location("p1")
        seen.append(world.host_turn("p1"))
        world.player_walk("p1", "cafe"); world.player_location("p1")
        world.player_tool("p1", "interact",
                          {"target": "tree:harbor_oak", "verb": "嫁接"})
        seen.append(world.host_turn("p1"))
        _drive_chat(world, "p1")
        seen.append(world.host_turn("p1"))
        world.player_walk("p1", "workshop"); world.player_location("p1")
        seen.append(world.host_turn("p1"))

        assert int(world.scheduler.clock) == frozen, "这条用例不许推时钟"
        cached = [i for i, t in enumerate(seen) if t["scene"]["source"] == "cached"]
        assert not cached, (
            f"第 {cached} 屏 cached:{[(t['trigger'], t['scene']['source']) for t in seen]}")
        texts = [t["scene"]["text"] for t in seen]
        # ⚠️ 判据是**连着两屏**不许一样(真站报的是「连着三屏同一句」)。
        # 不判"四屏两两不同":第 1 屏和第 4 屏都是"正在去建筑工作室的路上",
        # 那是**同一个处境**,同一句话是照实说 —— 拿"全都不同"当判据,
        # 会把一句诚实的话判成 bug。
        repeats = [i for i in range(1, len(texts)) if texts[i] == texts[i - 1]]
        assert not repeats, f"第 {repeats} 屏和上一屏一字不差:{texts}"


def test_在忙的时候连点同一个长动词_屏原样不动_而抬头是上一屏的(tmp_path):
    """🔴 **真站第五轮那三屏同一句,根在这儿 —— 而引擎这一处是对的。**

    实测复现(和线上「你正为狮心会拉票,芬格尔凑过来…」连出三屏逐字同形):
    玩家被一个长动词占着,**再点同一个动词一律被拒**(「你已经在做这件事了」)
    → 一条事件都不发 → 时刻钥匙一格没动 → `scene.source == "cached"`,
    而抬头报的是**上一屏那个** `acted`(见 `_host_trigger` 的回声)。

    ⚠️ **这条用例钉的是"当前行为是什么",不是"这样就对"**:
    「一次被拒的操作也是玩家做过的一次操作」要不要开口,动的是
    `contract.host.moments` 那张表(「只在六个时刻开口」是结构性纪律,
    不是提示词里的一句话)—— **那是一次契约变更,不该由这一层顺手做掉**。
    在有裁决之前,这条用例保证这个行为**不会悄悄变**。
    """
    with open_world_at(tmp_path / "busy.db") as world:
        world.player_move("p1", "cafe")
        world.tick(3)
        world.player_topup("p1", 100)
        world.player_buy("p1", "cafe", "garden_shears")
        world.host_turn("p1")
        world.player_tool("p1", "interact",
                          {"target": "tree:harbor_oak", "verb": "嫁接"})
        first = world.host_turn("p1")
        assert first["scene"]["source"] != "cached"

        for _ in range(3):
            got = world.player_tool("p1", "interact",
                                    {"target": "tree:harbor_oak", "verb": "嫁接"})
            assert got["ok"] is False, "在忙时再点该被拒"
            assert "已经在做" in str(got.get("error") or ""), got
            turn = world.host_turn("p1")
            assert turn["scene"]["source"] == "cached"
            assert turn["scene"]["text"] == first["scene"]["text"]
            # 抬头是**上一屏**那个 —— 「这一趟开没开口」只有 `source` 答得了
            assert turn["trigger"] == first["trigger"]


def test_钥匙那几格和source闭集_都要在契约里探得到():
    """🔴 **验收 B+C ⑦**:`chat_seq` / `entity_engage` 这两格是 3.11.2 加的,
    而**契约里一个字都没有** —— 下游探不到。探不到的下场是具体的:
    站点为「聊完一轮屏不动」自己加一条每轮事件,而那正是
    「整场会话只在关闭时发一个事件」那条硬不变量挡的东西。

    ⚠️ `chat_seq` **不是事件**,所以它不能藏在 `move_event_types` 里 ——
    拿那张表去数会永远少一格,而少的正是最常走的那条路。
    """
    from anima_world import director as D
    from anima_world import host as H
    from anima_world.__main__ import contract_payload

    seg = contract_payload()["director"]
    assert seg["acted_grains"] == list(H.ACTED_GRAINS)
    assert "entity_engage" in seg["move_event_types"], seg["move_event_types"]
    assert seg["sources"] == list(D.SOURCES)
    assert seg["source_labels"] == dict(D.SOURCE_LABELS)
    assert "story_text" in seg["text_keys"] and "story_text" in seg["story_keys"]


def test_编剧派一个不在场的人_hail载荷自带名字(tmp_path):
    """🔴 **真站第五轮 + player 带回**:编剧 `approach` 派来的诺诺,
    `agent_hail.payload.agent_name` 是空的,而那个人**又不在 `/state.agents`** ——
    站点两条路都取不到名,屏上是「一位还没报上名字的人」。
    **一个人被派来找你,而屏幕说不出他是谁。**

    判据:**派一个不在他跟前的人**(那正是 `approach` 存在的理由 ——
    「她不在这儿」不是闸),payload 里那一格非空。
    """
    with open_world_at(tmp_path / "hn.db") as world:
        world.player_move("p1", "cafe")
        world.tick(2)
        world.host_turn("p1")
        here = {a: world.scheduler._where_is(a) for a in world.scheduler.agents}
        away = [a for a, loc in here.items() if loc != "cafe"]
        assert away, "这条用例要一个不在场的角色"
        who = away[0]
        world._director_apply(
            "p1", {"move": "approach", "who": who, "line": "我找你有事",
                   "why": "", "promise": "", "stake": None, "source": "mock"},
            tension_before=0.2, phase="setup", tick=int(world.scheduler.clock),
            place="cafe", thread=None, pin_ticks=12, due_ticks=0, capped=False,
            forbidden_ops=set(), recap=[], place_name="咖啡店")
        hails = [e for e in world.events() if e["type"] == "agent_hail"]
        assert hails, "编剧派了人而一条 hail 都没有"
        got = (hails[-1].get("payload") or {}).get("agent_name")
        assert str(got or "").strip(), f"派来的人没名字:{hails[-1]['payload']}"


def test_名字空着的角色_hail那一格也不许空():
    """**兜底到 id,不留空**(纯函数那一半):读的一方手上没有名册,
    而"事后回查"对一个已经不在名册里的人根本查不到 ——
    照实说一个 id,好过让屏幕说「一位还没报上名字的人」。"""
    from anima_world.scheduler import Scheduler

    class _Blank:
        """`agent_display_name` 答空的那种世界。"""
        agent_display_name = staticmethod(lambda aid: "")
        _relation_name = staticmethod(lambda aid: "")

    got = Scheduler.hail_agent_name(_Blank(), "诺诺")
    assert got == "诺诺", got
