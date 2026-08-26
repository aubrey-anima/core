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

    # ⚠️ **跑到第五次。** 只跑第二次是不够的 —— 那正是这条 bug 溜过去的方式:
    # 第 2 次各格确实全 0,而**第 3 次起 `names_skipped` 永远是 1**。
    # 来路:`forget_player` 每次都追加一条 `player_departed`,联系态已经清空之后
    # 名字问不出来,`player_name` 兜底成 player_id;下一轮扫描把这个 id 当成他的
    # 一个显示名收进来,于是被判进 `skipped`。**数据是对的,坏的是回执** ——
    # 而文档承诺的是"第二次跑各格都是 0",宿主拿它写合规断言会红。
    counted = ("events", "conversations", "messages", "memories_dropped",
               "memories_redacted", "facts", "names", "names_skipped")
    for attempt in range(2, 6):
        again = world.erase_player("ghost-5")
        zeros = {key: again[key] for key in counted}
        assert set(zeros.values()) == {0}, f"第 {attempt} 次跑还有非零格:{zeros}"


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


def test_预览走的是另一条路_数出来的却是同一批(world):
    """`dry_run` 不再造那份拷贝(只判"会不会变"),于是它和真跑是**两条代码路径**。

    两条路给同一个数这件事必须有人盯:回执正是宿主拿去写合规记录的那份,
    而"预览说 3 条、真跑改了 5 条"不会有任何一处报错。
    差的那一条是**文档写死的**(`forget` 追加的 `player_departed`,dry_run 时压根
    不存在),所以这里连它一起钉 —— 只断"两个数差不多"等于什么都没断。
    抹除的判断是递归的,所以下面每条事件**只有一处**能改,而且各藏在不同的形状里
    (list 里的字符串 / list 套 dict / 第三层的原文字段)—— 两份递归里任何一份漏
    一层,这个数当场差一。混在一条事件里就查不出来了:一条事件只算一次,
    漏掉的那一层被同一条上别的命中掩护过去。
    """
    agent_id = next(iter(world.scheduler.agents))
    _befriend(world, agent_id, "ghost-probe", name="阿檀")
    _chat_once(world, agent_id, "ghost-probe", "我是阿檀,我怕黑")
    _quiesce(world)
    log = world.scheduler.event_log
    log.append({"ts": 0, "type": "state_change", "who": agent_id,
                "payload": {"kind": "gossip", "items": ["阿檀在场", 3]}})
    log.append({"ts": 0, "type": "state_change", "who": agent_id,
                "payload": {"kind": "gossip", "items": [{"who_said": "阿檀"}]}})
    log.append({"ts": 0, "type": "state_change", "who": "ghost-probe",
                "payload": {"kind": "gossip",
                            "deep": {"inner": {"text": "一句没有名字的原文"}}}})

    preview = world.erase_player("ghost-probe", reason="用户要求删除", dry_run=True)
    done = world.erase_player("ghost-probe", reason="用户要求删除")
    assert preview["events"] > 0
    assert preview["events"] + 1 == done["events"], (
        "预览和真跑数出的不是同一批(差的那一条只该是 player_departed)"
    )
    assert "阿檀" not in _all_payload_text(world)


def test_两遍之间落库的那几条照旧现判(world):
    """第二遍复用第一遍的判断,**只能复用到第一遍看见过的那一条为止**。

    比它新的必须现判:`forget_player` 自己就在两遍中间追加一条 `player_departed`,
    别的进程也可能刚好在这中间落一条。边界画错的话,那几条会被当成"和他无关"
    放过去 —— 名字照旧躺在日志里,而回执说抹干净了,没有一处报错。
    """
    agent_id = next(iter(world.scheduler.agents))
    _befriend(world, agent_id, "ghost-mid", name="阿檀")
    _quiesce(world)
    real_forget = world.forget_player

    def forget_then_another_process_writes(pid, **kw):
        out = real_forget(pid, **kw)
        if not kw.get("dry_run"):
            world.scheduler.event_log.append({
                "ts": 0, "type": "conversation", "who": agent_id,
                "payload": {"player_id": "ghost-mid", "player_name": "阿檀",
                            "summary": "阿檀最后又说了一句"},
            })
        return out

    world.forget_player = forget_then_another_process_writes
    world.erase_player("ghost-mid", reason="用户要求删除")
    text = _all_payload_text(world)
    assert "阿檀" not in text, "两遍之间落的那条漏了"
    assert "最后又说了一句" not in text, "那条的原文没抹"


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


# ── 可续与分片(3.5.0)──────────────────────────────────────────────────────
#
# 这一节钉的**第一件事是正确性,不是性能**。3.4.0 及更早有一个不可逆的死角:
# 改写从低 seq 往高 seq 走,而名字的来源之一就是日志自己(`*_id`/`*_name` 配对)。
# 一趟被杀在半路之后,低 seq 那半的配对已经是「(已注销)」,`forget_player` 又早把
# 在场与联系态清了 —— 重跑的第一遍**收不到他的名字**,于是尾巴上那些只在自由文本里
# 提过他的句子再也抹不掉,而且一处不报错。
#
# 分片本身不让任何一发请求变快(收名字那一遍永远 O(全量事件),`World.open` 那次
# 重放更是分不动),所以别拿它去换宿主那侧的墙 —— 它买到的是"续得上"和"一次调用的
# 写入量有上限"。


PID = "ghost-shard"

from anima_world.api import (  # noqa: E402 —— 挨着用到它们的那一节放
    _ERASE_PHASE_DONE as _DONE,
    _ERASE_PHASE_NOT_STARTED as _NOT_STARTED,
    _ERASE_PHASE_PARTIAL as _PARTIAL,
)


def _bare_event(world, payload: dict) -> int:
    """直接往日志里放一条,payload 由测试完全控制。"""
    event = world.scheduler._record_and_deliver(
        {"type": "state_change", "who": None, "payload": payload}
    )
    return int((event or {}).get("seq") or 0)


def _dead_corner(world) -> tuple[int, int, int]:
    """摆出那个死角:**配对的名字在低 seq,自由文本里的名字在高 seq。**

    刻意**不**建在场也不建联系态 —— 那正是真实场景(在场 15 分钟就过期,而合规
    抹除往往发生在这个人走了很久之后)。于是他的名字只有日志里那一处配对说得出,
    而 `forget_player` 追加的那条 `player_departed` 只兜底得出他的 id。
    """
    before = world.scheduler.event_log.max_seq()
    paired = _bare_event(world, {
        "kind": "sentiment_delta", "as": "npc",
        "target": PID, "target_name": "阿檀", "note": "他来过",
    })
    free_text = _bare_event(world, {"kind": "narration", "text": "阿檀昨天说她怕黑"})
    assert free_text > paired
    return before, paired, free_text


def test_杀在中途再续跑_名字不丢(world):
    """**这一条是这一轮的理由。** 半路停下、名字只剩自由文本那一份 —— 续跑要抹掉它。"""
    before, paired, free_text = _dead_corner(world)
    assert world.players.get(PID) is None, "夹具前提:他不在场"

    # 一片只做到配对那条为止(`before` 条创世事件 + 配对那条)。
    partial = world.erase_player(PID, reason="用户要求删除", limit=before + 1)
    assert partial["phase"] == "partial"
    assert partial["resume_seq"] == paired
    assert partial["seq"] is None, "没到日志尽头就写审计事件 = 把半途报成抹完了"

    rows = {int(e.seq): e for e in world.scheduler.event_log.replay()}
    assert "阿檀" not in json.dumps(rows[paired].payload, ensure_ascii=False)
    assert "阿檀" in json.dumps(rows[free_text].payload, ensure_ascii=False), \
        "夹具前提没成立:自由文本那条本来就该还没被碰过"

    # 死角本身:此刻**重新推断**名字什么也推不出来 —— 配对那格已经被自己抹掉了。
    # 进度键买的就是这一格,所以这一行必须留着:它一旦推得出来,下面那条测试
    # 就不再是在验进度键了。
    fresh_names, _, _, _ = world._erase_survey(PID)
    assert fresh_names == set(), "推得出名字的话,这个测试证明不了进度键有用"

    # 而进度键记着它。续跑不重新推断,直接接着抹。
    done = world.erase_player(PID, resume=True)
    assert done["phase"] == "done" and done["seq"] is not None
    assert done["resume_seq"] is None
    assert "阿檀" not in _all_payload_text(world), "自由文本那条永远抹不掉了"
    assert world.erasure_progress.load(PID) is None, "做完了还留着一份待抹名单"


def test_进度键在动日志之前就落盘_而且计数跨片累加(world):
    before, paired, _ = _dead_corner(world)
    first = world.erase_player(PID, limit=before + 1)
    saved = world.erasure_progress.load(PID)
    assert saved is not None and "阿檀" in saved["names"]
    assert int(saved["cursor"]) == paired
    # 第二片把剩下的做完,计数是**整趟活**的总数,不是这一片的。
    second = world.erase_player(PID, since_seq=paired)
    assert second["events"] >= first["events"] >= 1
    assert second["phase"] == "done"


def test_since_seq_不许越过水位(world):
    before, paired, _ = _dead_corner(world)
    world.erase_player(PID, limit=before + 1)
    with pytest.raises(ValueError, match="水位"):
        world.erase_player(PID, since_seq=paired + 5)
    # 预演造不出洞,所以它不受这条管。
    world.erase_player(PID, dry_run=True, since_seq=paired + 5)


def _world_fingerprint(world) -> tuple:
    """一个世界此刻的样子 —— 拒绝路径跑前跑后必须逐字节相同。"""
    log = world.scheduler.event_log
    return (
        log.max_seq(),
        _all_payload_text(world),
        sorted(world.presence_store.ids()),
        json.dumps(sorted(
            (r.get("player_id"), r.get("player_name"))
            for r in (world.scheduler.contact_store.all() or [])
        ), ensure_ascii=False),
    )


def test_被拒的那一趟一个字都不许写(world):
    """**拒绝必须零副作用。**

    这一条钉的是一个真发生过的坏法:水位校验排在 `forget_player` **后面**,于是
    一条被拒的命令 rc=2、stdout 零字节,而世界已经被改了(在场与联系态清掉、
    日志多一条 `player_departed`)—— 连敲三次 `max_seq` 54→57。一次拒绝在调用方
    那儿的意思就是"什么都没发生",而这条路上没有比这更容易被信以为真的一句话。
    """
    _dead_corner(world)
    before = _world_fingerprint(world)

    for _ in range(3):
        with pytest.raises(ValueError, match="水位"):
            world.erase_player(PID, since_seq=9999)
    assert _world_fingerprint(world) == before, "被拒的那一趟动了世界"

    # 另两条校验同一水位:它们本来就在任何写之前,一起钉住免得将来被挪下去。
    for kwargs in ({"limit": 0}, {"since_seq": -1}):
        with pytest.raises(ValueError):
            world.erase_player(PID, **kwargs)
    assert _world_fingerprint(world) == before
    assert world.erasure_progress.load(PID) is None, "被拒的那一趟建了进度键"


def test_phase与resume_seq不许自相矛盾(world):
    """`not_started` ⇒ 一个字都没写,而且不带 `resume_seq`;`partial` ⇔ 进度键在。

    从前:「没什么可抹 + `--limit` + 循环期间落了别人的新事件」会回
    `{"phase": "not_started", "resume_seq": 55}` —— 而那一趟已经跑过 `forget_player`。
    宿主照 `not_started` 判"还没开始"、照 `resume_seq` 去续,而 `--resume` 又回它
    "没有未完成的":三句话互相打架,没有一句是对的。而 platform 的抹除门正要按
    这一格分「被墙挡在门外 vs 抹到一半」。
    """
    # 复现那个窗口:一个没在这个世界里留下任何痕迹的 id(收名字收不到、涉他事件
    # 一条都没有 → `nothing_to_change`),而**别的进程在两遍之间落了新事件**。
    # 那个"之间"就是 `forget_player` 跑的那一刻,所以拿它当注入点最贴真相。
    ghost = "never-here"
    real_forget = world.forget_player

    def forget_then_someone_else_writes(*args, **kwargs):
        receipt = real_forget(*args, **kwargs)
        for _ in range(3):
            _bare_event(world, {"kind": "narration", "text": "别人的事"})
        return receipt

    world.forget_player = forget_then_someone_else_writes
    try:
        receipt = world.erase_player(ghost, limit=1)
    finally:
        del world.forget_player

    assert receipt["phase"] != _NOT_STARTED, "跑过 forget 了还报没开始"
    assert receipt["phase"] == _PARTIAL and receipt["resume_seq"] is not None
    assert world.erasure_progress.load(ghost) is not None, \
        "报了 partial 却没有进度键 —— --resume 会回「没有未完成的」"
    # 不变量①的另一半:报了 partial,`--resume` 就必须真的接得上。
    done = world.erase_player(ghost, resume=True)
    assert done["phase"] == _DONE and done["seq"] is not None
    assert done["resume_seq"] is None

    # `not_started` 只剩一条路能走到:`--resume` 空跑。它一个字都不写。
    idle = world.erase_player("nobody-at-all", resume=True)
    assert idle["phase"] == _NOT_STARTED
    assert idle["resume_seq"] is None
    assert idle["forget"] is None, "空跑跑了 forget"


def test_导入侧也跳过带TTL的键(tmp_path):
    """`lock` 与进度键在**装包**那一侧也要拦下 —— 手改过的包会带着它们。

    导出早就跳过 `lock`,而导入没有:一份手改过的包能装进一把**永不过期的死锁**,
    新世界第一次 `act()` 就撞上它;进度键则会让那边的下一趟抹除从一个跟它毫无
    关系的水位接着做。两条都不报错。
    """
    import redis as redis_mod

    from anima_world.world_package import install_world_records

    client = redis_for(tmp_path / "target.db")
    assert isinstance(client, redis_mod.Redis) or client is not None
    install_world_records(
        [
            {"kind": "redis", "key": "lock", "type": "string", "value": "someone-else"},
            {"kind": "redis", "key": "erasure:u1", "type": "hash",
             "value": {"names": '["阿檀"]', "cursor": "7"}},
            {"kind": "redis", "key": "clock", "type": "string", "value": "42"},
        ],
        redis=client, world_id="target",
    )
    keys = {k for k in client.scan_iter("anima:target:*")}
    assert "anima:target:clock" in keys, "正常的键被误伤了"
    assert "anima:target:lock" not in keys, "装进了一把永不过期的死锁"
    assert "anima:target:erasure:u1" not in keys, "装进了别人的一份待抹名单"


def test_resume_没有未完成的就什么都不做(world):
    _dead_corner(world)
    receipt = world.erase_player(PID, resume=True)
    assert receipt["phase"] == "not_started"
    assert receipt["events"] == 0 and receipt["seq"] is None
    assert receipt["forget"] is None, "--resume 顺手开了一趟新的"
    assert "阿檀" in _all_payload_text(world), "--resume 在没活可续时抹了东西"


def test_预演读进度键但不写它_而且答得出半途(world):
    before, _, _ = _dead_corner(world)
    world.erase_player(PID, limit=before + 1)
    saved = dict(world.erasure_progress.load(PID))

    preview = world.erase_player(PID, dry_run=True)
    assert preview["phase"] == "partial", "预演答不出半途,宿主就只能把它猜成没开始"
    assert preview["dry_run"] is True
    assert world.erasure_progress.load(PID) == saved, "预演写了进度键"


def test_进度键不进包(world):
    """它装着**正要被抹掉的那些名字**,而包是分发物。"""
    from anima_world.world_package import dump_world_records

    before, _, _ = _dead_corner(world)
    world.erase_player(PID, limit=before + 1)
    assert world.erasure_progress.load(PID) is not None, "夹具前提没成立"

    shorts = [
        r["key"] for r in dump_world_records(
            redis=world.scheduler.redis, world_id=world.scheduler.world_id
        ) if r.get("kind") == "redis"
    ]
    assert not any(k.startswith("erasure:") for k in shorts), \
        f"一份待抹名单被打进了包里:{shorts}"


def test_cli_分片两发再续跑(tmp_path):
    db = tmp_path / "w.db"
    redis_for(db)
    with open_world_at(str(db), force_mock_llm=True) as world:
        before, paired, _ = _dead_corner(world)

    first = run_cli("player", "erase", "--world-id", "w", "--player", PID,
                    "--yes", "--limit", str(before + 1), "--json")
    assert first.returncode == 0, first.stderr
    receipt = json.loads(first.stdout)
    assert receipt["phase"] == "partial" and receipt["resume_seq"] == paired

    # 越过水位当场拒,而且是退 2 —— 按退出码判断的脚本要看得出这次没做成。
    hole = run_cli("player", "erase", "--world-id", "w", "--player", PID,
                   "--yes", "--since-seq", str(paired + 5))
    assert hole.returncode == 2
    assert "水位" in hole.stderr

    done = run_cli("player", "erase", "--world-id", "w", "--player", PID,
                   "--yes", "--resume", "--json")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["phase"] == "done"

    with open_world_at(str(db), force_mock_llm=True) as world:
        assert "阿檀" not in _all_payload_text(world)


def test_cli_不带新参数时_输出与3_4_0逐行相同(tmp_path):
    """**3.4.0 形状的调用,stdout 一行不多一行不少。**

    分片那两行只在真的停在半路时才出现;一趟走到尽头的普通抹除照旧只印计数 +
    `player_erased`。按行数钉是有意的 —— 多印一行不会让任何断言红,而下游按行
    找东西的脚本会当场错位。
    """
    db = tmp_path / "w.db"
    redis_for(db)
    with open_world_at(str(db), force_mock_llm=True) as world:
        agent_id = next(iter(world.scheduler.agents))
        _befriend(world, agent_id, "u9", name="阿檀")

    preview = run_cli("player", "erase", "--world-id", "w", "--player", "u9")
    lines = [ln for ln in preview.stdout.splitlines() if ln.startswith("[player]")]
    assert len(lines) == 2, lines
    assert lines[0].startswith("[player] u9 —— 要抹:")
    assert lines[1] == "[player] (没带 --yes:世界一个字节都没动)"

    done = run_cli("player", "erase", "--world-id", "w", "--player", "u9", "--yes")
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("[player]")]
    assert len(lines) == 2, lines
    assert lines[0].startswith("[player] u9 —— 抹了:")
    assert lines[1].startswith("[player] 已记下 player_erased(seq=")


def test_cli_没做完要说出来(tmp_path):
    """一趟停在半路而只印一行计数,读的人会当成做完了 —— 这条路上那是不可逆的一边。"""
    db = tmp_path / "w.db"
    redis_for(db)
    with open_world_at(str(db), force_mock_llm=True) as world:
        before, _, _ = _dead_corner(world)

    out = run_cli("player", "erase", "--world-id", "w", "--player", PID,
                  "--yes", "--limit", str(before + 1)).stdout
    assert "还没到日志尽头" in out and "--resume" in out
    assert "player_erased" not in out, "半途印出审计事件 = 说它抹完了"


# ── 他身上那张量表(3.8.0,收件箱 D39)────────────────────────────────────────
#
# 引擎给**每个玩家**开量表,和角色同一个命名空间(`Scheduler.stock_owner_of`
# 写着这条为什么必须):owner 是 `agent:player:<id>`。而 `erase_player` 的
# docstring 从 3.5.0 起就承诺"把这个人的交互数据从世界里抹掉" —— 那句话在这一格上
# 一直是假的。2026-08-26 线上量到 night-tide 36 行、lighthouse-bay 23 行,
# 而回执上一格都没有:**它不是"抹得不够干净",是"回执上看不出这件事发生过"**。


def _player_stock_owner(world, pid: str) -> str:
    return world.scheduler.stock_owner_of(f"{world.scheduler.PLAYER_PREFIX}{pid}")


def test_他身上那张量表跟着抹掉_而回执有facts一格(world):
    """量表整个没了、owner 索引上也没了,而回执**数得出**删了几个。"""
    world.player_move("ghost-facts", "cafe", display_name="阿檀")
    owner = _player_stock_owner(world, "ghost-facts")
    seeded = world.stocks(owner)
    assert len(seeded) >= 1, "夹具前提没成立:橱窗里 agent 一个量都没声明?"

    receipt = world.erase_player("ghost-facts", reason="用户要求删除")

    assert receipt["facts"] == len(seeded), (
        f"回执数出的量表条数不对:{receipt['facts']} vs {len(seeded)}"
    )
    assert world.stocks(owner) == {}, "他身上的量还在"
    assert owner not in world.stock_owners(), (
        "hash 删了而 owner 索引没删 —— `owners()` 会报出一个空壳"
    )


def test_可见性表按种类声明_一行都不许动(world):
    """**这一格最容易删错。** `stock_visibility` 的主键是(种类, 量名),
    玩家和世界里每一个角色**共用同一行** —— 跟着他一起删掉,等于把所有人的
    「体力」从感知里一起抹掉,而世界照跑、日志干净。"""
    world.player_move("ghost-vis", "cafe", display_name="阿檀")
    before = world.scheduler.visibility_store.rules_map()
    assert ("agent", "体力") in before, "夹具前提没成立"

    world.erase_player("ghost-vis")

    assert world.scheduler.visibility_store.rules_map() == before, (
        "抹一个玩家动了按种类声明的可见性表"
    )


def test_他站在哪与叫什么也从可见性表上撤下来(world):
    """`stock_places` 那一行的 label **就是他的显示名** —— 抹除全篇在做的就是
    把那个名字从世界里拿掉。跑着的世界靠 `_sweep_ghost_players` 迟早会扫掉它,
    **而抹除多半跑在一次性容器里**:那种进程一个 tick 都不推。"""
    world.player_move("ghost-place", "cafe", display_name="阿檀")
    owner = _player_stock_owner(world, "ghost-place")
    world.scheduler.visibility_store.place(owner, "cafe", "阿檀")
    assert world.scheduler.visibility_store.place_of(owner) == "cafe", "夹具前提没成立"

    world.erase_player("ghost-place")

    assert world.scheduler.visibility_store.place_of(owner) is None, "他还站在咖啡店里"
    assert "阿檀" not in json.dumps(
        world.scheduler.visibility_store.labels(), ensure_ascii=False
    ), "他的显示名还挂在可见性表上"


def test_预演只数不删(world):
    world.player_move("ghost-facts-dry", "cafe", display_name="阿檀")
    owner = _player_stock_owner(world, "ghost-facts-dry")
    seeded = world.stocks(owner)

    preview = world.erase_player("ghost-facts-dry", dry_run=True)

    assert preview["facts"] == len(seeded)
    assert world.stocks(owner) == seeded, "dry_run 删了他的量"


def test_没有量表的世界也报0_不是缺席(world, monkeypatch):
    """**缺席和 0 是两件事。** 一支不查这一格的引擎是整格**没有**,
    而报 0 是在说"我查过了,他身上没有量" —— 下游按 `.get("facts", 0)` 读,
    两者分得开的前提是新引擎**永远**给这一格。"""
    monkeypatch.setattr(world.scheduler, "stock_store", None, raising=False)
    receipt = world.erase_player("ghost-nostock")
    assert "facts" in receipt and receipt["facts"] == 0


def test_量表这一格也是幂等的(world):
    world.player_move("ghost-facts-twice", "cafe", display_name="阿檀")
    first = world.erase_player("ghost-facts-twice")
    assert first["facts"] >= 1
    again = world.erase_player("ghost-facts-twice")
    assert again["facts"] == 0


def test_续跑不把量表数第二遍(world):
    """转录、记忆、量表**第一片就做完**;续跑的计数从进度键里带过来。
    重做一遍的话同一批会被数两次,而回执正是宿主写进合规记录的那份。"""
    world.player_move(PID, "cafe", display_name="阿檀")
    before, _, _ = _dead_corner(world)
    seeded = len(world.stocks(_player_stock_owner(world, PID)))
    assert seeded >= 1

    partial = world.erase_player(PID, reason="用户要求删除", limit=before + 1)
    assert partial["phase"] == _PARTIAL
    assert partial["facts"] == seeded

    done = world.erase_player(PID, reason="用户要求删除", resume=True)
    assert done["phase"] == _DONE
    assert done["facts"] == seeded, "续跑把量表又数了一遍"


def test_cli_量表那一格印在屏幕上_零也印(tmp_path):
    """**零也印**:「他身上没有量」和「这一版引擎不查这个」在屏幕上必须分得开,
    而后者的样子是这句话整个不出现。"""
    db = tmp_path / "w.db"
    redis_for(db)
    with open_world_at(str(db), force_mock_llm=True) as world:
        world.player_move("cli-facts", "cafe", display_name="阿檀")
        seeded = len(world.stocks(_player_stock_owner(world, "cli-facts")))
    assert seeded >= 1

    done = run_cli("player", "erase", "--world-id", "w", "--player", "cli-facts",
                   "--yes", "--json")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["facts"] == seeded

    # 第二趟:什么都没了,那一格照旧印 0。
    again = run_cli("player", "erase", "--world-id", "w", "--player", "cli-facts",
                    "--yes")
    assert "他身上的量 0 个" in again.stdout, again.stdout


def test_契约报得出这一格_老引擎是缺席不是null(tmp_path):
    """消费方按 `receipt_count_keys` 探测,不比版本号。"""
    from anima_world.api import _ERASE_COUNT_KEYS

    out = run_cli("contract", "--json")
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["erasure"]["receipt_count_keys"] == list(_ERASE_COUNT_KEYS)
    assert "facts" in payload["erasure"]["receipt_count_keys"]
