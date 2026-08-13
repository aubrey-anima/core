"""降级要在世界里留下痕迹。

引擎的降级纪律是"加载严格 / 运行降级":LLM 挂了世界照跑,叙事退回模板,规划退回
作息表。这是对的。问题在于**它只在 stderr 刷一行 warning** —— 日志会滚掉,而"这个
世界当时跑在什么档位上"恰恰是解释它为什么长成这样的关键:一个整整三天没有 planner
的世界,和一个角色确实无所事事的世界,产物看起来一模一样。

所以两件事:计数进 `state()`(现在怎么样),档位切换落一条事件(一路上怎么样)。
**只在切换时发事件**,不是每次都发 —— 一个持续降级的子系统会每 tick 触发一次,那样
事件日志会被自己的健康报告淹掉(needs 抖动那个教训)。
"""
from __future__ import annotations

from _worldfile import open_world_at

import pytest

from anima_world.api import World


@pytest.fixture
def world(tmp_path):
    w = open_world_at(str(tmp_path / "w.db"), force_mock_llm=True)
    yield w
    w.close()


def _health_events(world):
    return [
        e for e in world.history(kind="subsystem_health", limit=1000)["events"]
    ]


def test_a_working_subsystem_does_not_announce_itself(world):
    """开机就正常的东西不该发事件 —— 健康报告不是噪音源。"""
    world.tick(30)
    assert _health_events(world) == []


def test_a_degrading_subsystem_leaves_an_event_and_a_count(world):
    world.tick(1)
    world.scheduler.note_subsystem("probe", False, "provider exploded")

    events = _health_events(world)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["subsystem"] == "probe"
    assert payload["status"] == "degraded"
    assert "exploded" in payload["reason"]

    health = world.state()["runtime"]["subsystems"]["probe"]
    assert health["status"] == "degraded" and health["degraded"] == 1


def test_a_persistent_degradation_does_not_flood_the_log(world):
    """持续降级只发一条 —— 计数照常涨。"""
    world.tick(1)
    for _ in range(20):
        world.scheduler.note_subsystem("probe2", False, "timeout")

    assert len(_health_events(world)) == 1, "每次降级都发事件的话,日志会被自己淹掉"
    assert world.state()["runtime"]["subsystems"]["probe2"]["degraded"] == 20


def test_recovery_is_announced_too(world):
    """只报坏消息的话,读日志的人没法知道它什么时候好的。"""
    world.tick(1)
    world.scheduler.note_subsystem("probe2", False, "timeout")
    world.scheduler.note_subsystem("probe2", True)

    statuses = [e["payload"]["status"] for e in _health_events(world)]
    assert statuses == ["degraded", "ok"]
    assert world.state()["runtime"]["subsystems"]["probe2"]["status"] == "ok"


def test_the_health_record_survives_a_restart(tmp_path):
    """"当时跑在什么档位上"必须是历史,不是内存里的一个数。"""
    db = str(tmp_path / "w.db")
    with open_world_at(db, force_mock_llm=True) as world:
        world.tick(1)
        world.scheduler.note_subsystem("probe", False, "no api key")

    with open_world_at(db, force_mock_llm=True) as reopened:
        events = _health_events(reopened)
        assert [e["payload"]["subsystem"] for e in events] == ["probe"]
        assert "no api key" in events[0]["payload"]["reason"]


def test_a_real_narrative_run_counts_successes(world):
    """接线检查:真跑一段,叙事的成功计数必须真的在涨(这条盯的是接线本身)。"""
    world.tick(120)
    world.close(wait=True)  # 叙事在线程池上,排干才数得准
    health = world.state()["runtime"]["subsystems"]
    assert health.get("narrative", {}).get("ok", 0) > 0, health


class _DeadLLM:
    """配好了、但打不通 —— 线上那个世界的模型就是这个样子。"""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages):
        self.calls += 1
        raise ConnectionError("All connection attempts failed")
        yield ""  # pragma: no cover - 让它是个 async generator

    async def complete(self, messages):
        self.calls += 1
        raise ConnectionError("All connection attempts failed")


def test_模型打不通的世界不许在健康表上报满分(world):
    """一次真试玩逮到的:线上灯塔湾的主模型(本机 ollama)没起来。

    每一次开口都在 `httpx.ConnectError` 上断掉,而那个世界的 `World.state()` 报的是
    `llm: {"mock": false, "degraded_reason": null}`、`subsystems: {}` —— 「一切正常」。
    `_llm_degraded_reason` 答的其实是**配没配**这个问题(它只在 Mock 那一支算),
    而字段名叫 `degraded_reason`、三支里都有,运维台和宿主界面都当健康读。
    于是一个一句话都答不出来的世界报满分:照跑、报成功、日志一行不错。

    健康的家在 `subsystems`,planner / narrative / relationship_judge 早就在里面,
    **聊天一直缺席**。这条盯两头:记了一笔,以及**照旧往外抛** —— 吞掉就得替她编
    一句道歉,而那句会原样落进转录、进摘要、进她的长期记忆。
    """
    dead = _DeadLLM()
    world.chat_service._llm = dead

    with pytest.raises(Exception) as caught:
        world.chat_reply("夏", [{"role": "user", "content": "在吗"}], player_id="p1")
    assert "connection" in str(caught.value).lower(), caught.value
    assert dead.calls, "夹具前提没成立:根本没走到那次 LLM 往返"

    health = world.state()["runtime"]["subsystems"].get("chat")
    assert health, "模型全程打不通,健康表上却没有 chat 这一格"
    assert health["status"] == "degraded" and health["degraded"] >= 1
    assert "ConnectionError" in health["reason"], health

    events = [e for e in _health_events(world) if e["payload"]["subsystem"] == "chat"]
    assert events and events[0]["payload"]["status"] == "degraded"


def test_聊得通的世界要在健康表上留下成功(world):
    """只记坏消息的话,`subsystems` 里没有 chat 有两种意思:没人聊过,和接线断了。"""
    world.chat_reply("夏", [{"role": "user", "content": "在吗"}], player_id="p1")

    health = world.state()["runtime"]["subsystems"].get("chat")
    assert health and health["ok"] > 0, health


def test_日程排满不是一次规划失败(tmp_path):
    """线上晚潮 21 个角色里 17 个日程排满 —— 于是 planner 常驻 `ok 0 / degraded 1071`。

    这条测试盯的正是这个模块开头那句话的**另一半**:它建这个计数器,是为了让
    「planner 挂了三天的世界」和「角色确实无所事事的世界」在健康表上长得不一样;
    而它自己把另一对混成了一个 —— 「她今天没有自由时间」连 LLM 都不会调,却被
    记成一次子系统降级。真挂了那天,读表的人看到的数字和今天一模一样。
    """
    from anima_world.planner import Planner

    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        scheduler = world.scheduler
        # 整天都是班,一分钟空窗都没有。
        # 世界照旧,只把日程排满 —— 一分钟空窗都没有。
        scheduler.planner = Planner(
            llm=None,
            bt_store=scheduler.bt_store,
            location_store=scheduler.location_store,
            persona_provider=lambda aid: {"name": aid, "personality": ""},
            agent_ids=lambda: list(scheduler.agents),
            duty_windows=lambda aid: [(0, 1440)],
        )
        world.tick(1)
        for agent_id in scheduler.agents:
            scheduler._make_and_install_plan(agent_id, 1)

        assert "planner" not in world.state()["runtime"]["subsystems"], \
            "没有空窗要排,那不是规划失败"


def test_排不出计划的人一天只被试一次(tmp_path):
    """失败什么都不留下,于是每 tick 重排一次 —— 线上 61 tick 攒了 1071 次。

    今天它只是白转,因为那些人在调 LLM **之前**就返回了;LLM 真挂掉的那天,
    这就是一条对着付费接口每 tick 一次、没有任何退避的重试风暴。
    """
    class _InlinePool:
        """就地跑完再回来。**不是图快**:线程池会让"这一 tick 到底重排了几次"
        取决于 worker 抢不抢得过 tick 循环,于是漏掉闸门也可能只多出一两次,
        这条测试就成了掷骰子。就地跑 = 每一 tick 都干干净净地问一次。
        (`_lock` 是 RLock,同一个线程里重入没问题。)"""

        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

        def shutdown(self, *args, **kwargs):
            pass

    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        scheduler = world.scheduler
        attempts: list[tuple[str, int]] = []

        class _NeverPlans:
            """空窗有、动作空间有,就是排不出来 —— LLM 挂了长这个样子。"""

            def plan_inputs(self, agent_id):
                attempts.append((agent_id, scheduler.world_time().day))
                return [(0, 60)], {"idle_wander": {}}

            def make_plan(self, agent_id, day, windows=None, space=None):
                return None

        # Mock 档位的世界不带规划器,所以池子也没建 —— 这里补上,好让下面的
        # `world.tick` 走的是**真的那条 tick 循环**,而不是直接戳 worker。
        scheduler.planner = _NeverPlans()
        scheduler._planner_pool = _InlinePool()
        world.tick(60)
        world.close(wait=True)

    per_agent_day = {}
    for agent_id, day in attempts:
        per_agent_day[(agent_id, day)] = per_agent_day.get((agent_id, day), 0) + 1
    assert attempts, "接线断了:一次都没试过"
    assert max(per_agent_day.values()) == 1, \
        f"同一个人同一天被重排了多次:{per_agent_day}"
