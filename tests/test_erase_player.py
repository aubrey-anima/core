"""**法务抹除** —— 删除权在事件溯源里的正确形状(《拟人化互动办法》第十六条)。

`forget_player` 是"告别":历史一个字不动,她记得这个人来过。`erase_player` 是
另一个动作 —— 用户行使删除权,他的交互数据必须真的消失。而这个引擎的地基是
"对账即重放",所以抹除的形状被两条硬约束钉死:

1. **事件不删行,原地改写。** `seq` 在 Redis 后端是列表下标 + 1 —— 删一行,
   后面每一条的 seq 错一位,投影、分页、`since_seq` 全部跟着错,而且不报错。
2. **不透明 id 保留,不换假名。** 换假名会和跨进程折叠竞态(落后的进程折了真名
   delta、再折到假名 departed,真名关系成了清不掉的幽灵),而假名映射一旦落库
   就等于没抹。名字和原文抹干净之后,id 只是一串指向宿主已删账号的字符。

于是可验的形状是:转录整场删、由他而起的记忆删行、旁及他的记忆只换名、
事件里名字换「(已注销)」原文换「(已抹除)」、seq 与重放照旧、账本不动。
"""
from __future__ import annotations

import json
import time

import pytest

from _worldfile import open_world_at, run_cli, redis_for

from anima_world.projection import project_events


def _befriend(world, agent_id: str, player_id: str, name: str = "阿檀") -> None:
    """让世界里真的长出一段"她和这个玩家"的关系 —— 和告别的测试同一条真路。"""
    world.scheduler.contact_store.note_contact(
        agent_id, player_id, tick=world.scheduler.clock, name=name,
    )
    world.scheduler._record_and_deliver({
        "type": "state_change", "who": agent_id,
        "payload": {"kind": "sentiment_delta", "as": agent_id, "target": player_id,
                    "delta": 0.5, "as_name": world.scheduler.agent_display_name(agent_id),
                    "target_name": name},
    })


def _chat_once(world, agent_id: str, player_id: str, said: str) -> int:
    """一整回合真转录(user→assistant),关闭时发 `conversation` 事件、长出记忆。"""
    return world.record_chat_turn(agent_id, player_id, [
        {"role": "user", "content": said},
        {"role": "assistant", "content": "我听见了。"},
    ])


def _quiesce(world, *, timeout: float = 5.0) -> None:
    """等判定线程把它那条 `sentiment_delta` 落完。

    关系判定跑在线程池上(时钟永不等网络),所以一场对话关闭之后**还会再落一条
    事件**,时机不定。不等它就抹,抹完它才落地 —— 于是名字又回到日志里,而这
    正是文档里明说的边界(抹除后落库的新事件不在扫描范围里),不是抹除的 bug。
    测试要验的是抹除本身,所以先让世界静下来:事件数连着几轮不动就算落完。
    """
    log = world.scheduler.event_log
    deadline = time.monotonic() + timeout
    stable, last = 0, -1
    while time.monotonic() < deadline:
        now = log.max_seq()
        stable = stable + 1 if now == last else 0
        if stable >= 3:
            return
        last = now
        time.sleep(0.05)


def _all_payload_text(world) -> str:
    return json.dumps(
        [{"who": e.who, "payload": e.payload} for e in world.scheduler.event_log.replay()],
        ensure_ascii=False,
    )


@pytest.fixture
def world(tmp_path):
    w = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    yield w
    w.close()


def test_转录整场删_别人的一条不动(world):
    agent_id = next(iter(world.scheduler.agents))
    _chat_once(world, agent_id, "ghost-1", "我是阿檀,我的秘密是蓝色气球")
    keep_id = _chat_once(world, agent_id, "friend-1", "我是小竹,常来")
    _quiesce(world)
    convs = world.conversations(agent_id)
    assert {c.get("player_id") for c in convs} >= {"ghost-1", "friend-1"}, "夹具前提没成立"

    receipt = world.erase_player("ghost-1", reason="用户要求删除")
    assert receipt["conversations"] >= 1 and receipt["messages"] >= 2

    left = world.conversations(agent_id)
    assert all(c.get("player_id") != "ghost-1" for c in left), "他的会话还在"
    kept = world.conversation_messages(keep_id)
    assert any("小竹" in (m.get("content") or "") for m in kept), "别人的转录被误伤"
    # 秘密不许还躺在任何一条消息行里
    for conv in left:
        for m in world.conversation_messages(int(conv["id"])):
            assert "蓝色气球" not in (m.get("content") or "")


def test_名字与原文从事件里消失_而seq与重放不变(world):
    agent_id = next(iter(world.scheduler.agents))
    _befriend(world, agent_id, "ghost-2", name="阿檀")
    _chat_once(world, agent_id, "ghost-2", "我是阿檀,别告诉别人我怕黑")
    _quiesce(world)
    assert "阿檀" in _all_payload_text(world), "夹具前提没成立:名字压根没进事件"
    before = world.scheduler.event_log.replay()

    receipt = world.erase_player("ghost-2", reason="用户要求删除")
    assert receipt["events"] >= 1 and receipt["names"] >= 1

    after = world.scheduler.event_log.replay()
    # 改写不删行:至少多出告别 + 审计两条(判定线程可能异步再落几条),
    # 而先前的每一条都在原位、还是原来那件事。
    assert len(after) >= len(before) + 2
    assert [(e.seq, e.type) for e in after[: len(before)]] == \
        [(e.seq, e.type) for e in before]
    text = _all_payload_text(world)
    assert "阿檀" not in text, "名字还在事件里"
    assert "(已注销)" in text, "名字该被换成占位,不是整段蒸发"
    # 涉他事件的原文字段抹空:那场对话的 summary 不许还带内容。
    for e in after:
        payload = e.payload or {}
        if e.type == "conversation" and any(
            (p or {}).get("id") == "ghost-2" for p in payload.get("participants") or []
        ):
            assert payload.get("summary") in ("", "(已抹除)")
    # 对账即重放照旧成立,而且折不出他的关系。
    replayed = project_events(after)
    assert not any("ghost-2" in key for key in replayed.relations)


def test_由他而起的记忆删行_旁及他的只换名(world):
    agent_id = next(iter(world.scheduler.agents))
    _befriend(world, agent_id, "ghost-3", name="阿檀")
    _chat_once(world, agent_id, "ghost-3", "我是阿檀,我怕黑")
    _quiesce(world)
    derived_before = [
        m for m in world.memories(agent_id)
        if m.get("event_seq") is not None and "ghost-3" in _seq_payload(world, m["event_seq"])
    ]
    assert derived_before, "夹具前提没成立:他的对话没长出记忆"
    # 旁及:别人的反思里提了他一句 —— 不是从他的事件长出来的。
    aside = world.scheduler.memory_store.add(
        agent_id, tick=world.scheduler.clock, kind="reflection",
        summary="今天店里热闹,阿檀和小竹都来过。",
    )

    receipt = world.erase_player("ghost-3")
    assert receipt["memories_dropped"] >= len(derived_before)
    assert receipt["memories_redacted"] >= 1

    rows = {m["id"]: m for m in world.memories(agent_id)}
    for m in derived_before:
        assert m["id"] not in rows, "由他而起的记忆还在"
    kept = rows[aside]["summary"]
    assert "阿檀" not in kept and "(已注销)" in kept and "小竹" in kept, (
        "旁及的记忆应该只换他的名字,别人的名字一个不许动"
    )


def _seq_payload(world, seq: int) -> str:
    for e in world.scheduler.event_log.replay():
        if int(e.seq or 0) == int(seq):
            return json.dumps({"who": e.who, "payload": e.payload}, ensure_ascii=False)
    return ""


def test_不带yes只数_一个字节不写(world):
    agent_id = next(iter(world.scheduler.agents))
    _befriend(world, agent_id, "ghost-4", name="阿檀")
    _chat_once(world, agent_id, "ghost-4", "我是阿檀")
    _quiesce(world)
    before_events = len(world.scheduler.event_log.replay())
    before_text = _all_payload_text(world)

    preview = world.erase_player("ghost-4", dry_run=True)
    assert preview["dry_run"] is True
    assert preview["events"] >= 1 and preview["conversations"] >= 1
    assert preview["seq"] is None

    assert len(world.scheduler.event_log.replay()) == before_events, "dry_run 加了事件"
    assert _all_payload_text(world) == before_text, "dry_run 改了事件"
    assert any(c.get("player_id") == "ghost-4" for c in world.conversations(agent_id)), (
        "dry_run 删了转录"
    )


def test_抹除是幂等的(world):
    """**这条不许和判定线程赛跑。** 关系判定跑在线程池上,一场对话关闭后它会
    异步再落一条 `sentiment_delta` —— 落在第一次抹除之后的话,第二次当然还有得改。
    那不是不幂等,那正是文档里明说的边界(抹除后落库的新事件不在扫描范围里),
    所以这里只用同步那条路建关系:幂等验的是抹除自己,不是线程的时序。
    """
    agent_id = next(iter(world.scheduler.agents))
    _befriend(world, agent_id, "ghost-5", name="阿檀")

    first = world.erase_player("ghost-5")
    assert first["events"] >= 1
    second = world.erase_player("ghost-5")
    assert second["events"] == 0, "第二次不该还有可改写的事件"
    assert second["conversations"] == 0 and second["memories_dropped"] == 0
    assert second["names"] == 0, "名字来源在第一次就该被抹干净了"


def test_审计事件里只有id和数目_没有名字(world):
    agent_id = next(iter(world.scheduler.agents))
    _befriend(world, agent_id, "ghost-6", name="阿檀")
    world.erase_player("ghost-6", reason="用户要求删除")
    erased = [e for e in world.scheduler.event_log.replay() if e.type == "player_erased"]
    assert len(erased) == 1
    payload = erased[0].payload
    assert payload["player_id"] == "ghost-6"
    assert "阿檀" not in json.dumps(payload, ensure_ascii=False)
    assert payload["events"] >= 1


def test_本进程的内存事件窗口也跟着改(world):
    """`World.events()` 是只读门 —— 抹完不该还端着抹掉之前的原文。"""
    agent_id = next(iter(world.scheduler.agents))
    _befriend(world, agent_id, "ghost-7", name="阿檀")
    assert "阿檀" in json.dumps(world.events(), ensure_ascii=False), "夹具前提没成立"
    world.erase_player("ghost-7")
    assert "阿檀" not in json.dumps(world.events(), ensure_ascii=False)


def test_与角色重名的显示名不替换(world):
    """替换会把她的名字和世界的文本一起绞碎 —— 跳过,并在回执里数着。"""
    agent_id = next(iter(world.scheduler.agents))
    her_name = world.scheduler.agent_display_name(agent_id)
    _befriend(world, agent_id, "ghost-8", name=her_name)
    receipt = world.erase_player("ghost-8")
    assert receipt["names_skipped"] >= 1
    assert her_name in _all_payload_text(world), "把角色自己的名字也抹掉了"


def test_账本不动(world):
    """钱是世界的账,不是他的话 —— 守恒不许破。"""
    agent_id = next(iter(world.scheduler.agents))
    world.scheduler._record_and_deliver({
        "type": "payment", "who": "ghost-9",
        "payload": {"from": "ghost-9", "to": agent_id, "amount": 5.0, "reason": "buy"},
    })
    world.scheduler.catch_up_projection()
    before = dict(world.scheduler._memory_projection.balances)
    world.erase_player("ghost-9")
    replayed = project_events(world.scheduler.event_log.replay())
    assert replayed.balances.get(agent_id) == before.get(agent_id), "抹除动了账本"


# ── CLI 出口 ────────────────────────────────────────────────────────────────


def test_cli_不带yes只数_带yes真抹(tmp_path):
    db = tmp_path / "w.db"
    redis_for(db)
    with open_world_at(str(db), force_mock_llm=True) as world:
        agent_id = next(iter(world.scheduler.agents))
        _befriend(world, agent_id, "claude-playtest-001", name="阿檀")
        _chat_once(world, agent_id, "claude-playtest-001", "我是阿檀")

    preview = run_cli("player", "erase", "--world-id", "w",
                      "--player", "claude-playtest-001", "--json")
    assert preview.returncode == 0, preview.stderr
    receipt = json.loads(preview.stdout)
    assert receipt["dry_run"] is True and receipt["events"] >= 1

    with open_world_at(str(db), force_mock_llm=True) as world:
        assert "阿檀" in _all_payload_text(world), "不带 --yes 动了世界"

    done = run_cli("player", "erase", "--world-id", "w",
                   "--player", "claude-playtest-001",
                   "--reason", "用户要求删除", "--yes", "--json")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["dry_run"] is False

    with open_world_at(str(db), force_mock_llm=True) as world:
        assert "阿檀" not in _all_payload_text(world)
        agent_id = next(iter(world.scheduler.agents))
        assert all(c.get("player_id") != "claude-playtest-001"
                   for c in world.conversations(agent_id))


def test_cli_对不存在的世界一律拒绝(tmp_path):
    redis_for(tmp_path / "w.db")
    result = run_cli("player", "erase", "--world-id", "nope", "--player", "x", "--yes")
    assert result.returncode == 2
