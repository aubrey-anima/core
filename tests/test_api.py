"""anima_world.api.World —— 纯库门面的回归。

引擎没有 HTTP:任何用世界的模块 import 本包,用函数操作 world.db。
这里守住门面的核心动线:开世界 → 走时钟 → 读状态 → 聊天 → 记回合 →
玩家动作 → 改配置 → 关闭 → 重开(从事件日志重放恢复)。全程 Mock LLM,离线。
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


def test_open_tick_state_roundtrip(world):
    assert len(world.scheduler.agents) == 3  # 内置种子:夏、遥、柔
    before = world.state()["world_time"]["tick"]
    world.tick(5)
    state = world.state()
    assert state["world_time"]["tick"] == before + 5
    assert set(state["agents"]) == set(world.scheduler.agents)
    assert state["locations"], "地图必须随 state 输出"
    assert state["runtime"]["llm"]["mock"] is True


def test_chat_streams_and_records_nothing(world):
    reply = world.chat_reply(
        "夏",
        [{"role": "user", "content": "你好"}],
        player_id="p1",
        display_name="阿宇",
    )
    assert reply  # Mock LLM 也要有回复
    # respond 路径不落平台历史:世界库里不该出现会话
    assert world.conversations("夏") == []


def test_chat_rejects_unknown_agent_and_bad_tail(world):
    with pytest.raises(KeyError):
        world.chat_reply("不存在", [{"role": "user", "content": "hi"}], player_id="p1")
    with pytest.raises(ValueError):
        world.chat_reply("夏", [{"role": "assistant", "content": "hi"}], player_id="p1")


def test_record_chat_turn_closes_and_emits_one_event(world):
    conversation_id = world.record_chat_turn(
        "夏",
        "p1",
        [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ],
    )
    convs = world.conversations("夏")
    assert [c["id"] for c in convs] == [conversation_id]
    assert convs[0]["status"] == "closed"
    assert len(world.conversation_messages(conversation_id)) == 2
    conversation_events = [
        ev for ev in world.events() if ev.get("type") == "conversation"
    ]
    assert len(conversation_events) == 1, "整场会话只在关闭时发一个事件"

    with pytest.raises(ValueError):
        world.record_chat_turn("夏", "p1", [{"role": "user", "content": "只有一半"}])


def test_player_move_and_action(world):
    world.player_move("p1", "cafe")
    assert world.state()["players"]["p1"]["location"] == "cafe"
    with pytest.raises(KeyError):
        world.player_move("p1", "不存在的地方")
    with pytest.raises(KeyError):
        world.player_move("p1", "oldport")  # region 不是可站立的 point

    world.player_action("p1", "挥手", {"target": "夏"})
    actions = [ev for ev in world.events() if ev.get("type") == "player_action"]
    assert actions and actions[-1]["player_id"] == "p1"
    assert actions[-1]["loc"] == "cafe"


def test_config_set_coerces_and_validates(world):
    world.config_set("scheduler.tick_rate", "2.5")
    assert world.config_get("scheduler.tick_rate") == 2.5
    with pytest.raises(KeyError):
        world.config_set("no.such.key", "1")
    with pytest.raises(ValueError):
        world.config_set("scheduler.tick_rate", "0")
    masked = {row["key"]: row["value"] for row in world.config_list()}
    assert "llm.api_key" in masked


def test_world_reopens_from_the_event_log(tmp_path):
    """时钟恢复语义:回到事件日志里最后一个世界时间戳。没有事件的静默 tick
    不是历史(事件溯源的本义),所以用一个确定性事件钉住最后时刻 ——
    依赖异步叙事事件恰好落盘的写法会 flaky。

    断的是**推进了三格**,不是钟面上的绝对数字:这个世界几点开门是它作者的意见
    (`world.start_time`),而"重开接不接得上最后那个事件"和几点开门毫无关系。
    把创世那一刻的绝对值抄进断言,等于让这条测试改成靠一个巧合站着。
    """
    db = str(tmp_path / "w.db")
    with open_world_at(db, force_mock_llm=True) as world:
        genesis = world.state()["world_time"]["tick"]
        world.tick(3)
        world.player_action("p1", "留下到此一游")  # ts = 当前时钟,同步落盘
        tick = world.state()["world_time"]["tick"]
        assert tick == genesis + 3

    with open_world_at(db, force_mock_llm=True) as reopened:
        state = reopened.state()
        assert state["world_time"]["tick"] == tick, "重开必须接着最后一个事件的时钟走"


def test_subscribe_receives_events(world):
    q = world.subscribe()
    try:
        world.player_action("p1", "跺脚")
        batch = q.get(timeout=2)
        assert any(ev.get("type") == "player_action" for ev in batch["events"])
    finally:
        world.unsubscribe(q)


def test_agent_context_is_bounded_grounding(world):
    ctx = world.agent_context("夏", "p1")
    assert "presence" in ctx
    assert ctx["presence"]["location_id"]


def test_world_has_exactly_one_projection(tmp_path):
    """回归:曾经有两份投影,第二份在运行中会停在开机状态。

    `_WorldView` 自己维护一份,开机全量重放建起来,此后只同步叙事日志和角色
    位置 —— 经济与关系变化一概不折叠。而 `scheduler._memory_projection` 每条
    事件都折叠。于是从 view 那份读余额会读到开机时的旧值(已删除的 snapshots
    表正是把它写回库里,才留下会累积的错账)。
    """
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.config_set("economy.enabled", "true")
        assert world._view.projection is world.scheduler._memory_projection, (
            "系统里只允许有一份投影"
        )
        world.tick(300)  # 跨过日切,工资会入账
        agent_id = next(iter(world.scheduler.agents))
        assert world._view.projection.balances[agent_id] == world.balance(agent_id), (
            "view 读到的余额必须与对外 API 一致 —— 不许停在开机状态"
        )
        assert world.balance("__town__") < 0, "金库发了工资就该是负的"


def test_state_reports_live_locations(tmp_path):
    """位置改成快照时读活黑板后,state() 必须仍与角色实际所在一致。"""
    with open_world_at(str(tmp_path / "w.db"), force_mock_llm=True) as world:
        world.tick(50)
        live = {
            aid: (brain.agent.blackboard.read("loc") or brain.agent.location)
            for aid, brain in world.scheduler.agents.items()
        }
        reported = {aid: d["location"] for aid, d in world.state()["agents"].items()}
        assert {k: v for k, v in reported.items() if k in live} == live


def _tick_into_quiet_tail(world, budget: int = 600) -> int:
    """走到"最后一个 tick 没发事件"为止,返回当时的时钟。

    这正是旧写法丢时间的状态:时钟从 max(事件 ts) 倒推,而末尾这段安静 tick
    在日志里没有任何痕迹。世界的事件疏密逐次运行都不同(角色行为带骰子),
    所以固定 tick 数的写法会时红时绿 —— 必须按"有没有事件"构造。
    """
    def count() -> int:
        return world.scheduler.event_log.count()

    world.tick(30)  # 先离开开局的密集播种段
    for _ in range(budget):
        before = count()
        world.tick(1)
        if count() == before:
            return world.state()["world_time"]["tick"]
    raise AssertionError(f"{budget} tick 内没出现无事件的 tick,构造不出安静尾巴")


def test_clock_survives_close_and_reopen(tmp_path):
    """回归:世界时钟必须落盘,不能靠"最后一条事件的 ts"倒推。

    时钟曾经只在 `load_persisted_events` 里恢复成 max(事件 ts) —— 世界末尾
    那段没有事件的安静 tick(角色都在睡)于是被无声丢掉:CLI 报 clock=350、
    重开读到 320,而且欠账永久追不回来。ARCHITECTURE.md 第 2 节的判据是
    "'发生了一件事'进事件日志,'现在是多少'进 data-plane 表",时钟正是后者。
    """
    db = str(tmp_path / "w.db")
    with open_world_at(db, force_mock_llm=True) as world:
        reported = _tick_into_quiet_tail(world)

    with open_world_at(db, force_mock_llm=True) as world:
        assert world.state()["world_time"]["tick"] == reported, (
            "重开后的时钟必须等于关闭时报告的时钟"
        )


def test_clock_deficit_never_accumulates_across_restarts(tmp_path):
    """同一条 bug 的累积面:欠账是永久的,反复重启会让世界日历越落越后。"""
    db = str(tmp_path / "w.db")
    expected = 0
    for _ in range(3):
        with open_world_at(db, force_mock_llm=True) as world:
            expected = _tick_into_quiet_tail(world)
        with open_world_at(db, force_mock_llm=True) as world:
            assert world.state()["world_time"]["tick"] == expected, (
                "上一轮关闭时走到的时钟必须原样还在,欠账不许攒下来"
            )


def test_explicit_seed_that_cannot_be_read_fails_loudly(tmp_path):
    """回归:显式指定的世界文件读不了,必须当场报错,不能静默换成内置演示世界。

    作者层只在空库首启读一次 —— 静默降级因此不可挽回:路径打错一个字母,你
    拿到的是内置三人世界(夏/遥/柔),而且改对路径重开也救不回来(库已非空,
    作者层被忽略)。CLI 那边更糟:`simulate --world-file typo.cyberworld`
    退出码 0,部署脚本会以为成功了。坏节拍脚本一直是当场硬失败,世界文件
    没有理由更宽松。
    """
    from anima_world.world_file import WorldFileError

    with pytest.raises(WorldFileError) as excinfo:
        open_world_at(
            str(tmp_path / "w.db"),
            seed_path=str(tmp_path / "typo.json"),
            force_mock_llm=True,
        )
    assert "typo.json" in str(excinfo.value), "报错必须点名是哪个文件"


def test_explicit_seed_failing_schema_says_what_is_wrong(tmp_path):
    """同上,但种子读得到、schema 不过:必须说清缺什么,而不是只说"无效"。"""
    import json

    from anima_world.world_seed import WorldSeedError

    seed = tmp_path / "myworld.json"
    seed.write_text(
        json.dumps({"agents": [{"id": "阿茶", "name": "阿茶"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(WorldSeedError) as excinfo:
        open_world_at(str(tmp_path / "w.db"), seed_path=str(seed), force_mock_llm=True)
    message = str(excinfo.value)
    assert "locations" in message, "缺了整个 locations 列表,报错得说出来"
    assert "location" in message and "personality" in message, (
        "角色缺的字段也要逐个点名,像坏节拍脚本那样"
    )


def _authored_seed() -> dict:
    """A world that is deliberately NOT the bundled demo roster."""
    return {
        "agents": [
            {"id": "顾昀", "name": "顾昀", "location": "teahouse", "personality": "沉默"},
            {"id": "白露", "name": "白露", "location": "teahouse", "personality": "话痨"},
            {"id": "老陈", "name": "老陈", "location": "market", "personality": "精明"},
        ],
        "locations": [
            {"id": "teahouse", "name": "茶馆", "description": "老城的茶馆"},
            {"id": "market", "name": "集市", "description": "热闹的集市"},
        ],
    }


def test_reopening_a_world_keeps_its_own_cast(tmp_path):
    """回归:重开一个已有世界,角色必须来自这个世界自己的事件日志。

    曾经 roster 完全由种子文件驱动 —— 不传 --seed 重开时,注册的是内置演示
    角色(夏/遥/柔),世界自己的角色一个 tick 都不跑,而这三个陌生人的
    narrative / state_change 事件被永久写进用户的库,全程没有任何提示。
    命中的正是文档推荐的工作流(先 simulate --seed 建库,再 run)。

    唯一能从事件重建的路径(节拍导演的重启扫描)明确跳过 ts <= 0,而创世的
    agent_join 恰恰是 ts=0 写的,所以创世角色结构性地被排除在外。
    """
    import json

    seed = tmp_path / "authored.json"
    seed.write_text(json.dumps(_authored_seed(), ensure_ascii=False), encoding="utf-8")
    db = str(tmp_path / "w.db")

    with open_world_at(db, seed_path=str(seed), force_mock_llm=True) as world:
        assert set(world.state()["agents"]) == {"顾昀", "白露", "老陈"}

    # 不传 seed 重开 —— 世界必须还是那三个人,一个外人都不许进来
    with open_world_at(db, force_mock_llm=True) as world:
        assert set(world.state()["agents"]) == {"顾昀", "白露", "老陈"}
        world.tick(30)

    who = {
        e.get("who")
        for e in open_world_at(db, force_mock_llm=True).events()
        if e.get("who")
    }
    assert who <= {"顾昀", "白露", "老陈"}, f"外来角色污染了世界:{who}"
