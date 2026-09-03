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


def test_说过话也算动过手_而它不进事件日志(tmp_path):
    """🔴 **真站第三轮那条的根因**:`conversation` 只在**会话关闭**那一刻发一条
    (「整场会话只在关闭时发一个事件」是这个仓库的硬不变量),而站点把会话
    **一直开着** —— 于是玩家聊了十轮,`move_seq` 一格没动,整屏纹丝不动。

    ⚠️ **不许为它加一条每轮事件** —— 那正是那条不变量挡的东西。
    接的是转录那一侧本来就在写的水位(`contact_store.last_contact_tick`)。
    """
    with open_world_at(tmp_path / "ct.db") as world:
        agent = next(iter(world.scheduler.agents))
        world.player_move("p1", "cafe")
        world.tick(2)
        world.host_turn("p1")
        # 会话**不关**,只记一轮 —— 站点就是这么用的
        world.scheduler.contact_store.note_contact(
            agent, "p1", tick=int(world.scheduler.clock) + 1)
        turn = world.host_turn("p1")
        assert turn["trigger"] == "acted", (
            f"聊了一轮而屏纹丝不动:{turn['trigger']} / {turn['scene']['source']}")
        # 而且**没有**为此新发一条事件(那条不变量还在)
        assert not [e for e in world.events() if e["type"] == "conversation"]


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

        # 他回来之后**聊了一轮** —— 站点那条路(record_chat_turn)写的水位
        world.scheduler.contact_store.note_contact(
            agent, "p1", int(world.scheduler.clock) + 1)
        turn = world.host_turn("p1")

        assert turn["scene"]["source"] != "cached", (
            f"聊了一轮而这一屏没开口(抬头 {turn['trigger']} 是上一屏的)")
        assert turn["trigger"] == "acted", turn["trigger"]
        assert len(_logs(world)) == before + 1, (
            f"屏开了而编剧一个字没写:{before} → {len(_logs(world))}")


# ── 「他动过手没有」那张钥匙表:闸和守卫必须逐格都认(3.11.2,真站第四轮)──────

def _drive_chat(world, pid):
    """聊一轮 —— 走的是站点那条路写的水位,**不发事件**(硬不变量)。"""
    world.scheduler.contact_store.note_contact(
        next(iter(world.scheduler.agents)), pid, int(world.scheduler.clock) + 1)


#: 每一格钥匙**怎么让它动**。⚠️ 这张表要和 `host.ACTED_GRAINS` 逐格对上 ——
#: 加一格而忘了写驱动,下面第一条用例当场红。
_ACTED_DRIVERS = {
    "move_seq": _act_once,
    "chat_tick": _drive_chat,
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
