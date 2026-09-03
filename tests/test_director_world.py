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
        for _ in range(20):
            _act_once(world, "p1")
            turn = world.host_turn("p1")
            if turn["scene"]["source"] == "cached":
                cached += 1
        logs = _logs(world)
        assert len(logs) == 20, f"二十次操作只写了 {len(logs)} 拍"
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
