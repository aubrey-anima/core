"""contact:**她自己想起你** —— 玩家不开口时,世界里唯一还是死的那一半。

这些测试盯的是那条链真的通,以及**它挡得住的四种坏法**:

- 关系不够近就不该想起你(不然这一层退化成一个定时骚扰器)
- 在睡觉/在赶路/手上有事/你就在跟前,一律不该想起你
- 冷却期内不许重复,而且今天越找过越难再找(上限 + 衰减,两条都要)
- **没有由头就绝不触发** —— 这是"判定从世界里长出来"的落点。一个会因为闲着
  就想起你的机制,和引擎自己编一个理由在产物上完全一样

以及那条最容易漏的:判定与线索是两回事,**LLM 挂了事件照发**。
"""
from __future__ import annotations

import time

import pytest
from _worldfile import open_world_at

from anima_world import contact
from anima_world.api import World
from anima_world.types import Relation

A_DAY = 288


class ScriptedLLM:
    """只负责写那句线索 —— 判定不经过它,所以这些测试里它基本是个道具。"""

    def __init__(self, *replies: str, boom: bool = False) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.boom = boom

    async def complete(self, messages) -> str:
        self.prompts.append("\n".join(m["content"] for m in messages))
        if self.boom:
            raise RuntimeError("模型这会儿不在")
        return self.replies.pop(0) if self.replies else "想问问他那件事办完没有"

    async def stream(self, messages):
        yield await self.complete(messages)


def _world(tmp_path, *replies: str, boom: bool = False, interval: int = 1) -> tuple[World, ScriptedLLM]:
    world = open_world_at(str(tmp_path / "w.db"), agents=1, force_mock_llm=True)
    llm = ScriptedLLM(*replies, boom=boom)
    world.chat_service._background_llm = llm
    world.config_set("contact.enabled", True)
    world.config_set("contact.interval_ticks", interval)
    world._install_contact()
    return world, llm


def _befriend(world: World, agent_id: str = "夏", player_id: str = "p1",
              *, sentiment: float = 0.7, affection: float = 0.7, trust: float = 0.6) -> None:
    """把关系直接写进投影。

    走真事件的话得先跑一遍关系判定(要 LLM、要一整场对话),而这些测试问的是
    **判定这一步**,不是关系怎么爬上来的 —— 那有它自己的测试。
    """
    world.scheduler._memory_projection.relations[(agent_id, player_id)] = Relation(
        sentiment=sentiment, affection=affection, trust=trust,
    )


def _give_reason(world: World, agent_id: str = "夏", player_id: str = "p1",
                 *, name: str = "阿檀", kind: str = "directive",
                 summary: str = "阿檀要我去把窗边那束花换掉", importance: float = 0.7) -> int:
    """给她一条由头 —— 一条真的记忆行,不是一个开关。"""
    store = world.scheduler.contact_store
    store.note_contact(agent_id, player_id, world.scheduler.clock, name)
    return world.scheduler.memory_store.add(
        agent_id, world.scheduler.clock, kind, summary, importance=importance,
    )


def _settle(world: World, predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    return predicate()


# ── 整条链 ──────────────────────────────────────────────────────────────────


def test_she_thinks_of_you_while_you_are_nowhere_near(tmp_path):
    """整条链:时钟到点 → 有由头、关系够近 → 一条查得到的世界事件。

    **玩家一个字都没说,也不在场** —— 这正是这一层存在的全部理由。
    """
    world, llm = _world(tmp_path, "想问问他那束花的事")
    with world:
        _befriend(world)
        _give_reason(world)

        world.tick(1)
        events = _settle(world, lambda: world.contact_requests("p1"))
        assert events, f"这条链没走通:{world.contact_stats()}"

        payload = events[0]["payload"]
        assert payload["agent_id"] == "夏"
        assert payload["player_id"] == "p1"
        assert payload["player_name"] == "阿檀"
        assert payload["reason"] == "errand"
        assert payload["topic"] == "想问问他那束花的事"
        assert payload["topic_source"] == "llm"
        # 由头带着**出处**:顺着它翻得回世界里那条记忆。
        assert payload["reasons"][0]["ref"]["memory_id"] is not None
        assert payload["reasons"][0]["note"] == "阿檀要我去把窗边那束花换掉"
        # 什么时候产生的
        assert payload["day"] == world.world_time().day
        assert ":" in payload["at"]
        # 分数与成分都在,所以"她为什么想起我"答得出来
        assert payload["components"]["score"] >= payload["components"]["threshold"]
        assert world.contact_stats()["fired"] == 1


def test_it_is_off_by_default(tmp_path, bare_seed):
    """默认关。点亮之前,时钟上什么都不该发生。

    **必须用素配种子。** 内置那份是橱窗,它替这个世界点亮了 `contact.enabled` ——
    拿橱窗去验引擎默认值,验的是橱窗的布置(conftest 里写着这一条)。
    """
    world = open_world_at(
        str(tmp_path / "w.db"), agents=1, force_mock_llm=True, world_file=bare_seed,
    )
    with world:
        assert world.config_get("contact.enabled") is False
        _befriend(world)
        _give_reason(world)
        world.tick(5)
        time.sleep(0.2)
        assert world.contact_requests() == []
        assert world.contact_stats()["checked"] == 0


# ── 四道闸 ──────────────────────────────────────────────────────────────────


def test_not_close_enough_never_fires(tmp_path):
    """关系不够近就不该想起你 —— 由头再硬也不行。"""
    world, _ = _world(tmp_path)
    with world:
        _befriend(world, sentiment=0.1, affection=0.05, trust=0.0)
        _give_reason(world)

        # 一 tick 就够,而且**只跑一 tick**:再跑下去她会睡着,于是"没触发"的
        # 原因换成了"她在睡觉",这条测试就不再验它声称要验的东西了。
        world.tick(1)
        time.sleep(0.3)
        assert world.contact_requests() == []
        assert world.contact_stats()["checked"] > 0, "闸没被走到,这条测试等于没验"
        assert "不够近" in world.contact_stats()["last"]


def test_no_reason_no_contact(tmp_path):
    """**没有由头就绝不触发** —— 哪怕关系好得不能再好。

    这一条是"判定从世界里长出来"的落点。挡不住它的话,这一层就是一个按亲密度
    定时响的闹钟,而那和从提示词里编一个理由没有区别。
    """
    world, _ = _world(tmp_path)
    with world:
        _befriend(world, sentiment=1.0, affection=1.0, trust=1.0)
        # 只登记"他来过",不给任何记忆;而且刚来过,所以久别那条也不成立。
        world.scheduler.contact_store.note_contact("夏", "p1", world.scheduler.clock, "阿檀")

        world.tick(1)
        time.sleep(0.3)
        assert world.contact_requests() == []
        assert "没有由头" in world.contact_stats()["last"]


def test_she_does_not_think_of_you_in_her_sleep(tmp_path):
    """在睡觉不该想起你。**归零不是打折** —— 而且要说得出是哪一条挡的。"""
    world, _ = _world(tmp_path)
    with world:
        _befriend(world)
        _give_reason(world)
        # 让 `_agent_activity` 读到"她在睡觉":当前动作就是引擎自己那一格。
        from anima_world.actions import ActionDescriptor

        world.scheduler._current_action["夏"] = ActionDescriptor(kind="sleep", params={})
        assert "sleep" in world._contact_blockers("夏", "p1")

        world.tick(3)
        time.sleep(0.3)
        assert world.contact_requests() == []
        assert world.contact_stats()["blocked"] > 0
        assert "睡觉" in world.contact_stats()["last"]


def test_face_to_face_belongs_to_the_other_door(tmp_path):
    """他就在她跟前时,该发生的是她开口(`reach_out`),不是一条"她想联系你"。

    两条路重叠的那一块必须有一边让出来,否则玩家会在跟她面对面聊天的同时收到
    一条"她想找你"的推送。
    """
    world, _ = _world(tmp_path)
    with world:
        _befriend(world)
        _give_reason(world)
        brain = world.scheduler.agents["夏"]
        world.player_move("p1", brain.agent.blackboard.read("loc") or brain.agent.location)
        # 直接问那道闸:整条链上还有别的闸(半夜她本来就睡着),而这条测试要钉的
        # 是**这一条**成立 —— 靠"没触发"来证明,证到的可能是另一条。
        assert "face_to_face" in world._contact_blockers("夏", "p1")

        world.tick(1)
        time.sleep(0.3)
        assert world.contact_requests() == []
        assert "跟前" in world.contact_stats()["last"]


def test_cooldown_and_the_daily_cap_both_hold(tmp_path):
    """冷却期内不重复,而且**今天越找过越难再找**(上限 + 衰减)。

    只有上限的话,今天那两次会挤在同一个小时里 —— 过了闸之后再触发一次的代价
    是零。所以这条同时钉住两样。
    """
    world, _ = _world(tmp_path)
    with world:
        world.config_set("contact.cooldown_ticks", 100)
        _befriend(world)
        _give_reason(world)

        world.tick(1)
        first = _settle(world, lambda: world.contact_requests("p1"))
        assert len(first) == 1

        # 冷却期内一直有由头、一直够近,照样不该再来一条。
        world.tick(20)
        time.sleep(0.3)
        assert len(world.contact_requests("p1")) == 1

        row = world.scheduler.contact_store.get("夏", "p1")
        assert row["fired_today"] == 1
        # 衰减本身:今天找过一次之后,门槛真的抬高了。
        base = float(world.config_get("contact.threshold"))
        fatigue = float(world.config_get("contact.fatigue"))
        assert contact.threshold_now(base, 1, fatigue) > base


def test_the_cooldown_survives_a_restart(tmp_path):
    """冷却落库,不是进程内的字典。

    换引擎镜像重启是这个部署形态下的家常便饭。内存态的冷却让玩家看到的是
    "一发版就四个人同时来找我",而日志里一条错都没有。
    """
    path = str(tmp_path / "w.db")
    world = open_world_at(path, agents=1, force_mock_llm=True)
    world.chat_service._background_llm = ScriptedLLM()
    world.config_set("contact.enabled", True)
    world.config_set("contact.interval_ticks", 1)
    world._install_contact()
    with world:
        _befriend(world)
        _give_reason(world)
        world.tick(1)
        assert _settle(world, lambda: world.contact_requests("p1"))
        clock = world.scheduler.clock

    again = open_world_at(path, agents=1, force_mock_llm=True)
    with again:
        row = again.scheduler.contact_store.get("夏", "p1")
        assert row.get("last_fired_tick") is not None
        assert int(row["fired_today"]) == 1
        assert int(row["last_contact_tick"]) <= clock


# ── 判定本身(纯函数,不用开世界) ────────────────────────────────────────────


def test_absence_ramps_and_then_stops_ramping():
    """久别要封顶。不封的话,一个下线三个月的玩家一登录就被所有人同时想起。"""
    span = 288
    assert contact.absence_weight(100, span) == 0.0            # 还没到一天
    day_one = contact.absence_weight(span, span)
    day_three = contact.absence_weight(span * 3, span)
    day_thirty = contact.absence_weight(span * 30, span)
    assert 0 < day_one < day_three
    assert day_thirty == pytest.approx(day_three), "久别没有上界"


def test_reasons_add_up_but_never_run_away():
    """多条由头叠加,但合成值进不了 1.0 —— 求和的话门槛形同虚设。"""
    one = contact.combine_urge([contact.Reason("gossip", 0.4)])
    four = contact.combine_urge([contact.Reason(f"r{i}", 0.4) for i in range(4)])
    assert one < four < 1.0


def test_a_declared_quantity_is_the_personality_dial():
    """性格进这一层走**她声明过的量**,不靠从人设文本里猜关键词。

    没声明 = 1.0,所以不写这个量的世界行为逐位不变(和本体层同构)。
    """
    args = dict(
        relation=Relation(sentiment=0.7, affection=0.7, trust=0.6),
        reasons=[contact.Reason("errand", 0.65, note="…")],
    )
    plain = contact.decide(**args)
    reluctant = contact.decide(**args, initiative=0.3)
    assert plain.fire
    assert not reluctant.fire
    assert reluctant.score < plain.score


def test_avoiding_him_makes_her_think_of_him_less():
    """她上一轮选的 stance 是她自己的选择 —— 「回避」的人不该隔两小时就想起你。

    中性是 1.0,所以 stance 关着的世界行为逐位不变。
    """
    args = dict(
        relation=Relation(sentiment=0.7, affection=0.7, trust=0.6),
        reasons=[contact.Reason("errand", 0.65, note="…")],
    )
    assert contact.decide(**args, stance="neutral").score == contact.decide(**args).score
    assert contact.decide(**args, stance="avoid").score < contact.decide(**args).score
    assert contact.decide(**args, stance="seduce").score > contact.decide(**args).score


def test_every_refusal_can_name_itself():
    """说不出原因的静默,和这层机制没接上长得一模一样。"""
    near = Relation(sentiment=0.7, affection=0.7, trust=0.6)
    reasons = [contact.Reason("errand", 0.65, note="…")]
    assert contact.decide(relation=near, reasons=reasons, blockers=["sleep"]).blocked_by == "sleep"
    assert contact.decide(relation=None, reasons=reasons).blocked_by == "not_close_enough"
    assert not contact.decide(relation=near, reasons=[]).fire


# ── 判定与线索是两回事 ──────────────────────────────────────────────────────


def test_a_dead_model_does_not_swallow_the_thought(tmp_path):
    """LLM 挂了,事件照发,线索退回由头原文 —— 而且**退了要吭声**。

    "模型没回话所以她就不想你了"和没有这个机制是一回事。
    """
    world, _ = _world(tmp_path, boom=True)
    with world:
        _befriend(world)
        _give_reason(world)

        world.tick(1)
        events = _settle(world, lambda: world.contact_requests("p1"))
        assert events, f"模型挂了就把念头吞了:{world.contact_stats()}"
        payload = events[0]["payload"]
        assert payload["topic"] == "阿檀要我去把窗边那束花换掉"
        assert payload["topic_source"] == "reason"
        assert world.contact_stats()["compose_failed"] == 1


def test_the_clock_never_waits_for_the_network(tmp_path):
    """引擎最老的一条不变量。新挂一个 hook 最容易破的就是它。"""
    import threading

    class SlowLLM(ScriptedLLM):
        async def complete(self, messages):
            self.prompts.append("x")
            time.sleep(0.5)
            return "慢死了"

    world, _ = _world(tmp_path)
    world.chat_service._background_llm = SlowLLM()
    with world:
        _befriend(world)
        _give_reason(world)

        ticking = threading.current_thread().name
        started = time.monotonic()
        world.tick(1)
        elapsed = time.monotonic() - started
        assert elapsed < 0.2, f"tick 被 LLM 拖住了({elapsed:.2f}s)"
        assert _settle(world, lambda: world.contact_requests("p1"), timeout=8.0)
        assert threading.current_thread().name == ticking


def test_gossip_about_the_player_is_a_reason(tmp_path):
    """八卦也算由头 —— 而且它比"他交代过我一件事"轻。

    别人嘴里的他,不是他和我之间发生过的事。
    """
    world, _ = _world(tmp_path)
    with world:
        _befriend(world)
        _give_reason(world, kind="hearsay1", summary="听遥说:阿檀昨天在冰灯那边待了很久",
                     importance=0.6)

        world.tick(1)
        events = _settle(world, lambda: world.contact_requests("p1"))
        assert events
        assert events[0]["payload"]["reason"] == "gossip"


def test_talking_to_her_resets_the_absence_clock(tmp_path):
    """"很久没出现"的那个"上次"是**她**这边的水位,而且两条聊天门都要记。"""
    world, _ = _world(tmp_path)
    with world:
        world.record_chat_turn("夏", "p1", [
            {"role": "user", "content": "在吗"},
            {"role": "assistant", "content": "在的。"},
        ])
        row = world.scheduler.contact_store.get("夏", "p1")
        assert int(row["last_contact_tick"]) == world.scheduler.clock


def test_a_colleague_who_is_not_registered_here_is_not_a_player(tmp_path):
    """**没注册在这个进程里的角色不是玩家。**

    差点放行的那个错:候选集本来是"关系投影里不是角色的那些 id"。用 `agents=1`
    打开的世界(或任何名册没注册全的世界)里,`遥` 和 `柔` 于是成了"玩家" ——
    她会对着两个同事算亲密度、写一句想说的话,发一条谁也收不到的事件。
    世界照跑,日志干净,而收件箱里多出两个不存在的人。

    判据改成了"这个 id 走过玩家那扇门"(`contact` 表里有行,或此刻登记在场)。
    """
    world, _ = _world(tmp_path)
    with world:
        # 这个世界只注册了一个角色,而她和「遥」之间是有关系的 —— 名册没注册全
        # 正是 `agents=N` 和节拍导演的 `agent_leave` 都会造出来的常态。
        _befriend(world, player_id="遥")
        assert "遥" not in world.scheduler.agents
        assert world._contact_targets("夏") == [], "同事被当成玩家了"

        # 而真的玩家一走过那扇门就进候选集。
        world.scheduler.contact_store.note_contact("夏", "p1", world.scheduler.clock, "阿檀")
        assert world._contact_targets("夏") == ["p1"]


def test_talking_at_tick_zero_still_counts_as_having_shown_up(tmp_path):
    """**"从来没有过"的哨兵是 `None`,不是 0。**

    真模型实测撞出来的:玩家在世界刚开机时就跟她说了话(CLI 试聊、真世界的第一个
    访客,都是这个形状),记下的 `last_contact_tick` 正是 **0** —— 而 0 被读成
    "他从没跟她说过话",于是"很久没出现"这条由头对他**永远**不成立。两个角色跑满
    两个世界日,一条都没触发,日志里一个字的异常都没有。
    """
    world, _ = _world(tmp_path)
    with world:
        assert world.scheduler.clock == 0, "这条测试要的就是创世那一 tick"
        world.scheduler.contact_store.note_contact("夏", "p1", 0, "阿檀")
        _befriend(world)

        reasons = world._contact_reasons(
            "夏", "p1", "阿檀",
            now_tick=A_DAY * 3, last_contact_tick=0,
        )
        assert [r.kind for r in reasons] == ["absence"], reasons
        assert reasons[0].ref["last_contact_tick"] == 0

        # 而真的"从没说过话"仍然不该长出一条久别 —— 引擎不替一个没发生过的过去
        # 编一个时长。
        assert world._contact_reasons(
            "夏", "p2", "别人", now_tick=A_DAY * 3, last_contact_tick=None,
        ) == []


def test_who_a_conversation_was_with_comes_from_the_event_not_the_wording(tmp_path):
    """跟玩家的对话**按事实认人,不按摘要措辞**。

    真模型实测(gemma4:26b)同一场对话的两条摘要:

        白霜:「面对阿檀对离别的感伤,白霜表现出怀疑与试探……」   ← 提到了名字
        零  :「面对即将到来的离别,对话充满了依依不舍的感伤与温情。」← 一个名字都没有

    照名字匹配的话,「零」拿不到这条由头而「白霜」拿得到 —— 而这个差别和两人的性格
    毫无关系,纯粹是那一次模型怎么措辞。**照跑、不报错,而且看上去像是性格起了作用。**
    """
    world, _ = _world(tmp_path)
    with world:
        _befriend(world)
        world.record_chat_turn("夏", "p1", [
            {"role": "user", "content": "我明天就走了"},
            {"role": "assistant", "content": "……哦。"},
        ])
        memories = [m for m in world.memories("夏") if m["kind"] == "user_conversation"]
        assert memories, "这一轮没落下对话记忆,后面的断言就没有意义"
        assert "p1" not in memories[0]["summary"], "Mock 摘要里正好带了 id,这条测试失效"

        reasons = world._contact_reasons(
            "夏", "p1", "阿檀", now_tick=world.scheduler.clock, last_contact_tick=None,
        )
        assert [r.kind for r in reasons] == ["strong_memory"], reasons
        assert reasons[0].ref["event_seq"] is not None

        # 而别的玩家不该认领这场对话。
        assert world._contact_reasons(
            "夏", "p2", "另一个人", now_tick=world.scheduler.clock, last_contact_tick=None,
        ) == []


def test_a_world_where_only_sentiment_moves_can_still_reach_the_gate():
    """**照线上那个世界的真数据钉的一条。**

    2026-08-07 对着 897e282865f5(1975 条事件)算了一遍账:二十条关系里,
    `trust` / `affection` / `respect` **无一例外全是 0.0**。判定这三个数的
    `relationship_judge` 确实在问(`axes_a_to_b`),但真模型那一路上它们从来没
    落过地;真正在动的只有 `sentiment`,最高的一条是

        bai-shuang → <玩家>  sentiment 0.668

    第一版的权重是三轴均摊(0.4/0.4/0.2),于是那个世界能达到的最大 closeness 是
    **0.27**,而闸设在 0.35 —— 这一层在真世界里一次都不会触发,而表现和"今天没人
    想你"逐字相同。整个特性会以"看着都对"的样子死在生产上。

    所以这条钉的不是某组数字,是**那个世界的形状过得了闸**。
    """
    live_shaped = Relation(sentiment=0.668, trust=0.0, affection=0.0, respect=0.0)
    assert contact.closeness(live_shaped) >= contact.DEFAULT_MIN_CLOSENESS, (
        "只有 sentiment 在动的世界过不了亲密度闸 —— 这一层在生产上等于不存在"
    )
    # 而闸仍然挡得住一个真的不熟的人。
    assert contact.closeness(Relation(sentiment=0.2)) < contact.DEFAULT_MIN_CLOSENESS
    # 敌意不是"负的亲密",夹到 0 —— 不是让它变成一个负数去和由头相乘。
    assert contact.closeness(Relation(sentiment=-0.9)) == 0.0


def test_the_forecast_reports_closeness_even_when_a_hard_gate_fired_first():
    """**没参与判断的成分也要如实报出来。**

    `contact --why` 是调这一层的唯一办法。硬闸提前返回时如果不填 `closeness`,
    它会对着一个正在睡觉的挚友打出「近 0.00」—— 而调阈值的人照那个数字去调,
    调的是一个不存在的问题。观察窗不许撒谎,这条在这一层就是这个样子。
    """
    near = Relation(sentiment=0.7, affection=0.7, trust=0.6)
    asleep = contact.decide(
        relation=near, reasons=[contact.Reason("errand", 0.65)], blockers=["sleep"],
    )
    assert asleep.blocked_by == "sleep"
    assert asleep.readiness == 0.0, "睡着的人 readiness 真的是 0,这个不是假的"
    assert asleep.closeness == pytest.approx(contact.closeness(near)), "亲密度被报成了 0"
