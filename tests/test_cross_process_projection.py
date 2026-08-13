"""**别的进程写进日志的事件,这个世界永远读不到** —— 水位跳过空档的那个洞。

线上 `night-tide` 跑在一个长驻进程里。从**另一个**进程(一次性维护容器)给四个
角色写了角色卡:事件确实追加进了 `anima:night-tide:events`,回执写着
`changed=True` —— 而那个跑着的世界**永远**读不到它们,玩家看到的还是
`card: null` / `billing: supporting`。全程零报错。

洞在 `Scheduler._apply_memory_trigger` 的尾部。水位 `_projection_seq` 的含义是
"**≤ 它的事件我都折过了**",而这个进程自己追加一条时,它直接把水位挪到了自己
那条的 seq —— 中间那些**别的进程写的、这个进程一条都没折过**的事件,就这么被
宣布成"折过了"。`catch_up_projection` 只往前看(`replay(since_seq=水位)`),
于是它们再也不会被折进来。

**这不是竞态,是必然**:一个跑着的世界每 tick 都在追加事件,所以只要外面有第二
个进程写过一条,它就一定被跳过。而且没有任何一处会报错 —— 投影是派生数据,它
"少折了一条"和"那件事没发生"长得一模一样。

爆炸半径比角色卡大得多:**关系就是投影**(`projection._apply_player_departed`)。
对着一个跑着的世界 `player forget` 一个离开的玩家,共享 Redis 里的联系态、姿态
被清掉了,而那个进程内存里的 `proj.relations` **永远留着那个幽灵** —— 江晚继续
惦记一个不存在的人,占着她的社交位。运维台维护白名单存在的理由就是这条命令。

四条不变量,每条一个测试:

1. 别的进程写的**角色卡**,读得到。
2. 别的进程的**告别**,那个幽灵真的没了(这条是这个 bug 真正的代价)。
3. **水位语义**:水位前进的条件是"折过了",不是"我写了一条" —— 追加一条跳了号
   的事件之后,水位之前的每一条都真的折过了。
4. **没有空档时不许多花一次 replay** —— 防止修法退化成"每条事件都全量对账",
   那是每条事件一次 Redis 往返,而世界每 tick 都在发事件。

外加只读门那一半:修好水位之后一个**跑着**的世界会在下一次追加时自愈,而一个
**暂停的 / 空闲的**世界不会 —— `state()` / `roster()` 是只读门,不该指望世界
正好在动。
"""
from __future__ import annotations

import pytest

from anima_world.projection import project_events


def _heartbeat(world) -> dict:
    """这个世界自己往日志里追加一条 —— 一个跑着的世界每 tick 都在做的事。

    形状不重要(投影根本不认识 `subsystem_health`),重要的是**它占掉一个 seq**:
    洞正是在"这个进程自己写一条"的那一刻把水位推过别人写的那一段的。
    """
    return world.scheduler._record_event({
        "type": "subsystem_health",
        "who": None,
        "payload": {"subsystem": "probe", "status": "ok",
                    "reason": "", "previous": "degraded"},
    })


def _befriend(world, agent_id: str, player_id: str, name: str = "阿檀") -> None:
    """让世界里真的长出一段"她和这个玩家"的关系 —— 走既有的那条路,不手写投影。"""
    world.scheduler.contact_store.note_contact(
        agent_id, player_id, tick=world.scheduler.clock, name=name,
    )
    world.scheduler._record_and_deliver({
        "type": "state_change", "who": agent_id,
        "payload": {"kind": "sentiment_delta", "as": agent_id, "target": player_id,
                    "delta": 0.5, "as_name": world.scheduler.agent_display_name(agent_id),
                    "target_name": name},
    })


@pytest.fixture
def two_processes(open_world, bare_seed):
    """同一个世界(同一个 Redis 前缀)上的两个进程。

    `keeper` 是那个长驻的、跑着世界的进程;`admin` 是一次性维护容器 —— 线上就是
    这个形状。
    """
    keeper_seeded = open_world("shared", world_file=bare_seed)
    admin = open_world("shared")
    return keeper_seeded, admin


def _an_agent(world) -> str:
    return next(iter(world.scheduler.agents))


def _a_supporting_agent(world) -> str:
    """一个种子里**不是**主角的人 —— 「配角被提成主角」才看得出这次编辑到没到。"""
    for aid, state in world.scheduler._memory_projection.agents.items():
        if (state.spec.get("card") or {}).get("billing") != "lead":
            return aid
    raise AssertionError("这个世界里每个人都是主角,夹具没法验")


def test_另一个进程写的角色卡_跑着的世界读得到(two_processes):
    """线上那一幕的最小复现:维护容器写卡,长驻世界的 `roster()` 里还是旧的。"""
    keeper, admin = two_processes
    agent_id = _a_supporting_agent(keeper)

    receipt = admin.set_card(agent_id, {"billing": "lead", "tagline": "写了几周的主角"})
    assert receipt["changed"] is True, "夹具前提没成立:这次编辑什么也没改"

    _heartbeat(keeper)   # 长驻世界的下一个 tick

    row = next(r for r in keeper.roster()["agents"] if r["agent_id"] == agent_id)
    assert row["billing"] == "lead", "另一个进程写的卡,这个世界读不到 —— 玩家看到的还是配角"
    assert row["tagline"] == "写了几周的主角"
    assert row["card"] == receipt["after"], "读出来的卡和写回执对不上"


def test_另一个进程的告别_那个幽灵真的走了(two_processes):
    """**这条才是这个 bug 的代价。** 关系不是一张表,是投影。

    `player forget` 往日志里追加一条 `player_departed`,共享 Redis 里的联系态
    照着清了 —— 而跑着的那个进程内存里的 `proj.relations` 里,那个幽灵还在。
    她会继续想起一个永远不会再出现的人,而每天能想起的人数是有上限的。
    """
    keeper, admin = two_processes
    agent_id = _an_agent(keeper)

    _befriend(admin, agent_id, "claude-playtest-001")
    keeper.scheduler.catch_up_projection()      # 夹具前提:keeper 确实看见过这段关系
    assert (agent_id, "claude-playtest-001") in keeper.scheduler._memory_projection.relations

    admin.forget_player("claude-playtest-001", reason="试玩账号")
    _heartbeat(keeper)

    live = keeper.scheduler._memory_projection.relations
    assert not any("claude-playtest-001" in key for key in live), (
        "告别之后她还惦记着这个人 —— 水位跳过了那条 player_departed"
    )


def test_水位只在折过之后才前进(two_processes):
    """**水位前进的条件是"折过了",不是"我写了一条"。**

    和 `reset_projection` 那条注释("两个字段各写各的就是那个洞本身")是同一族的
    第二个洞:那一次是折了却不挪水位(事件折两遍),这一次是挪了水位却没折
    (事件一遍也不折)。

    钉法是"对账即重放":自己追加一条之后,这个进程的投影必须和**从头全量重放**
    逐位相同 —— 而水位停在日志末尾。水位在那儿却对不上,就是它在撒谎。
    """
    keeper, admin = two_processes
    agent_id = _an_agent(keeper)

    # admin 写一批(跳了好几个号):关系、卡、告别各一条路径。
    _befriend(admin, agent_id, "ghost-a")
    _befriend(admin, agent_id, "ghost-b")
    admin.set_card(agent_id, {"billing": "lead"})
    admin.forget_player("ghost-a")

    _heartbeat(keeper)

    live = keeper.scheduler._memory_projection
    replayed = project_events(keeper.scheduler.event_log.replay())
    assert set(live.relations) == set(replayed.relations), "投影和全量重放对不上"
    assert live.balances == replayed.balances
    assert {k: v for k, v in live.inventories.items() if v} == \
        {k: v for k, v in replayed.inventories.items() if v}
    assert live.agents[agent_id].spec.get("card") == \
        replayed.agents[agent_id].spec.get("card")
    assert keeper.scheduler._projection_seq == keeper.scheduler.event_log.max_seq(), (
        "水位没停在日志末尾"
    )


def test_没有空档时不许多花一次replay(open_world, bare_seed):
    """补空档只在**真有空档**时做(`seq > 水位 + 1`,这个判断是免费的)。

    无条件 replay 一次的话,一个跑着的世界每发一条事件就多一次 Redis 往返 ——
    修法退化成"每条事件都全量对账",比原来的 bug 更难查(它只是慢)。
    """
    world = open_world("solo", world_file=bare_seed)
    real = world.scheduler.event_log
    calls: list[object] = []

    class _CountingLog:
        """`RedisEventLog` 有 `__slots__`,补不了方法 —— 所以套一层数 `replay`。"""

        def replay(self, *args, **kwargs):
            calls.append(kwargs.get("since_seq", args[0] if args else None))
            return real.replay(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(real, name)

    world.scheduler.event_log = _CountingLog()
    try:
        _heartbeat(world)
        _heartbeat(world)
        _heartbeat(world)
    finally:
        world.scheduler.event_log = real
    assert calls == [], f"连号追加也去 replay 了一遍:{calls}"


def test_只读门自己会补课_因为世界不一定正在动(two_processes):
    """修好水位之后,一个**跑着**的世界会在下一次追加时自愈。

    但一个**暂停的 / 空闲的**世界一条事件都不发,而 `state()` / `roster()` 是
    玩家那一侧真正在读的两扇门(运维台的壳 `/internal/v1/roster` 与
    `/internal/v1/state` 就调它们)。只读门不该指望世界正好在动。
    """
    keeper, admin = two_processes
    agent_id = _a_supporting_agent(keeper)

    admin.set_card(agent_id, {"billing": "lead", "tagline": "她是主角"})
    _befriend(admin, agent_id, "ghost-c")

    # keeper 一个字都没写,也没 tick 过。
    row = next(r for r in keeper.roster()["agents"] if r["agent_id"] == agent_id)
    assert row["billing"] == "lead", "roster() 没补课"
    assert f"{agent_id}|ghost-c" in keeper.state()["relations"], "state() 没补课"
