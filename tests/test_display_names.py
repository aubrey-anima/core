"""面向玩家的载荷里,**每一个 id 旁边都得有名字**。

线上 `night-tide` 的私聊窗口里,玩家看到的第一行是:

    mai：她把伞收在门边，指了指靠窗那张桌子

`mai` 是麦子。世界动态那条路已经修过一次(`test_mock_narration.py` 里那条
「第一屏写的是名字和地名」),修的是**正文**;而这里漏的是**结构化字段** ——
状态记录长这样:

    {"agent": "bai", "speaker": "bai", "text": "…", "ts": 22065}

`speaker` 是 id,于是每个照着它渲染发言人的宿主都会把 id 写在玩家脸上。**正文
修好了不代表这条修好了**,两条各有各的路。

修法是**加一个字段**(`speaker_name`),不是改 `speaker` 的含义 —— 后者是
宿主已经在用的机器可读键(去重、按人过滤都靠它),改它等于跨仓库破坏。

三条不变量:

1. **名字永远非空**。名册里没有这个人(离场了、老日志里的角色早就不在)就退回
   id —— 一个空的发言人名比一个 id 更难看。
2. **老日志重放也补得出来**。night-tide 的库里已经躺着几千条没有 `speaker_name`
   的 `narrative`,而重放是这个引擎的真相模型:补在发射端而不补在折叠端,等于
   说"这个 bug 修好了,但你已经有的历史永远是坏的"。
3. **同一类的口子要一起堵**。`agent_hail`(她来敲门)一直带着 `player_name`
   却不带 `agent_name` —— 玩家收到的推送里是"bai 来找你了"。
"""
from __future__ import annotations

import pytest

from _worldfile import open_world_at, run_cli, redis_for

from anima_world.projection import project_events
from anima_world.types import Event


# ── 1. 叙事:状态记录里的发言人 ────────────────────────────────────────────


def test_叙事状态记录的发言人带着人话名(tmp_path):
    """`state()["narrative_log"]` 是宿主渲染世界动态那一屏的数据源。

    走真的 CLI 跑一段,不是直接调 `_generate_narrative` —— 会红的测试必须走
    玩家走过的那条路。
    """
    db = tmp_path / "w.db"
    redis_for(db)
    result = run_cli("simulate", "--world-id", "w", "--ticks", "300", "--llm", "mock")
    assert result.returncode == 0, result.stderr

    with open_world_at(str(db), force_mock_llm=True) as world:
        entries = world.state()["narrative_log"]
        expected = {aid: world.scheduler.agent_display_name(aid) for aid in world.scheduler.agents}

    assert entries, "跑 300 tick 总该有叙事"
    for entry in entries:
        assert entry.get("speaker_name"), f"发言人没有名字:{entry!r}"
        speaker = entry.get("speaker")
        if speaker in expected:
            assert entry["speaker_name"] == expected[speaker], (
                f"发言人名字和名册对不上:{entry!r}"
            )
            # 名字里可以**包含** id(`夏` 之于 `苏晚夏`),但不许**就是** id。
            assert entry["speaker_name"] != speaker, (
                f"`speaker_name` 只是把 id 抄了一遍:{entry!r}"
            )


def test_玩家自己那条也带名字():
    """`user_message` 折出来的条目 `speaker` 是字面量 `"user"` —— 它同样会被
    渲染成发言人。给不出玩家名字时至少给一句人话的兜底,别把 `user` 印出来。

    这类事件今天只从宿主那侧进来(引擎自己不发),所以只有折叠端这一条路。
    """
    events = [
        Event(seq=1, ts=0, type="user_message", who="p1", loc="cafe",
              payload={"text": "在吗", "player_name": "小柯"}),
        Event(seq=2, ts=1, type="user_message", who="p2", loc="cafe",
              payload={"text": "喂"}),
    ]
    log = project_events(events).narrative_log
    assert log[0]["speaker_name"] == "小柯", f"宿主给了名字却没用上:{log[0]!r}"
    assert log[1].get("speaker_name"), f"玩家那条没有名字:{log[1]!r}"
    assert log[1]["speaker_name"] != "user", f"把 `user` 当名字印出来了:{log[1]!r}"


def test_老日志重放补得出名字():
    """库里已经躺着的那几千条没有 `speaker_name` —— 折叠端要认得出来。

    只修发射端的话,一个已经跑了三个月的世界重启之后,历史那一段仍然是 id。
    """
    events = [
        Event(
            seq=1, ts=0, type="agent_join", who="bai", loc="studio",
            payload={"spec": {"id": "bai", "name": "舒白"}, "location": "studio"},
        ),
        # 老引擎写下的那种:payload 里只有 text 和 speaker。
        Event(
            seq=2, ts=22065, type="narrative", who="bai", loc="studio",
            payload={"text": "她把伞收在门边", "speaker": "bai"},
        ),
        # 名册里根本没有的人(离场了/被抹掉了)—— 退回 id,但绝不空。
        Event(
            seq=3, ts=22066, type="narrative", who="ghost", loc=None,
            payload={"text": "谁在那儿", "speaker": "ghost"},
        ),
    ]
    proj = project_events(events)
    log = proj.narrative_log
    assert log[0]["speaker_name"] == "舒白", f"重放没补出名字:{log[0]!r}"
    assert log[1]["speaker_name"] == "ghost", f"名册外的人也得有个非空的名字:{log[1]!r}"


# ── 2. 同一类的口子:她来敲门 ─────────────────────────────────────────────


def test_敲门事件带着她的名字(open_world):
    """`agent_hail` 一直带 `player_name` 不带 `agent_name` —— 玩家收到的是
    「bai 来找你了」。`inbox()` 是宿主直接渲染成推送的那一条。"""
    world = open_world()
    scheduler = world.scheduler
    agent_id = next(iter(scheduler.agents))
    # 把玩家挪到她身边,让在场那条路自己发 —— 不手写事件。
    brain = scheduler.agents[agent_id]
    here = brain.agent.blackboard.read("loc") or brain.agent.location
    world.player_move("p1", here)
    scheduler._maybe_hail_player(brain.agent)

    hails = world.inbox("p1")
    assert hails, "同处一地该敲一次门"
    for event in hails:
        payload = event["payload"]
        assert payload.get("agent_name"), f"敲门的人没有名字:{payload!r}"
        assert payload["agent_name"] == scheduler.agent_display_name(payload["agent_id"])
        assert payload.get("location_name"), f"敲门的地点是键名:{payload!r}"
