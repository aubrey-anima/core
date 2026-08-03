"""运行时状态住进 Redis:进程不再持有变量。

这一层解的是"很多进程操作同一个世界"里最硬的那个结。世界的真相此前**只有一半在
db 里**:事件、记忆、关系、量都落库,而**黑板**(每个角色 20 个键:她在哪、在干嘛、
饿不饿、打算做什么、行为树这一 tick 选了哪个动作)、时钟、在途集合全是 Python 对象。
两个进程各开同一个世界文件,会读到同一份历史,然后**在各自内存里跑出两个不同的世界**。

盯四件事:

1. **另一个只有 Redis 连接的进程,读得到、改得动** —— 这是全部意义
2. 搬家不是清空 —— 创世写进黑板的性格、位置必须跟过去
3. 接口和进程内那个黑板**逐字相同**,所以行为树/需求/调度器一行都不用改
4. 代价照实报,不假装没有
"""
from __future__ import annotations

import pytest

fakeredis = pytest.importorskip("fakeredis")

from anima_world.api import World  # noqa: E402
from anima_world.bt_nodes import Blackboard  # noqa: E402
from anima_world.redis_state import (  # noqa: E402
    CachedRedisBlackboard,
    RedisBlackboard,
    agent_key,
)


@pytest.fixture()
def redis():
    return fakeredis.FakeStrictRedis(decode_responses=True)


@pytest.fixture()
def world(tmp_path, redis):
    w = World.open(
        str(tmp_path / "world.db"), force_mock_llm=True, redis=redis, world_id="t"
    )
    yield w
    w.close()


def _an_agent(world: World) -> str:
    return sorted(world.scheduler.agents)[0]


def test_another_process_sees_and_can_change_the_same_agent(world, redis):
    """**全部意义在这一条。**

    第二个"进程"只有一个 Redis 连接 —— 没有 World、没有调度器、没有任何本地状态。
    它必须读得到她此刻在哪、在干嘛,而且改了之后第一个进程立刻看得见。
    """
    agent = _an_agent(world)
    world.tick(20)
    mine = world.scheduler.agents[agent].agent.blackboard

    outsider = RedisBlackboard(redis, agent_key("t", agent))
    assert outsider.read("loc") == mine.read("loc"), "另一个进程读到的位置对不上"
    assert outsider.read("need.energy") is not None

    outsider.write("state.status", "被别的进程改了")
    assert mine.read("state.status") == "被别的进程改了", (
        "别的进程改了,这个进程没看见 —— 状态还是进程私有的"
    )


def test_moving_in_carries_what_was_already_on_the_board(world):
    """搬家不是清空:创世写进去的性格、位置得跟过去,否则第一个 tick 她没有性格。"""
    agent = _an_agent(world)
    board = world.scheduler.agents[agent].agent.blackboard
    assert isinstance(board, RedisBlackboard)
    assert board.read("loc"), "位置没跟过来"
    assert board.read("personality"), "性格没跟过来"


def test_the_interface_matches_the_in_process_blackboard(redis):
    """接口逐字相同,所以它是直接替换 —— 行为树/需求/调度器一行都不用改。

    `snapshot()` 也在这条里:此前 scheduler 直接摸 `blackboard._data`,而那个属性
    在 Redis 版上根本不存在。接口漏一个洞,替换就会在运行期炸。
    """
    plain, backed = Blackboard(), RedisBlackboard(redis, "t:x")
    for board in (plain, backed):
        board.write("loc", "cafe")
        board.write("plan.params", {"location": "home"})
        board.write("goals", ["把店开起来", "多认识人"])
        assert board.read("loc") == "cafe"
        assert board.read("plan.params") == {"location": "home"}
        assert board.read("goals") == ["把店开起来", "多认识人"]
        assert board.read("从没写过的键") is None
        assert board.snapshot()["loc"] == "cafe"


def test_a_world_on_redis_still_runs(world):
    """换了黑板,世界照样活 —— 这一层不该改变任何行为。"""
    agent = _an_agent(world)
    before = world._tool_runtime.agent_location(agent)
    world.tick(288)
    assert world.scheduler.clock >= 288
    assert world.scheduler.agents[agent].agent.blackboard.read("loc")
    assert world.history(limit=10)["events"], "跑了一天一个事件都没有"
    assert before is not None


def test_two_worlds_on_one_redis_do_not_share_brains(tmp_path, redis):
    """一个 Redis 上跑十个世界是常态。键撞车的后果是两个世界的角色共用一个脑子。"""
    a = World.open(str(tmp_path / "a.db"), force_mock_llm=True, redis=redis, world_id="a")
    b = World.open(str(tmp_path / "b.db"), force_mock_llm=True, redis=redis, world_id="b")
    try:
        agent = _an_agent(a)
        a.scheduler.agents[agent].agent.blackboard.write("state.status", "A 世界的")
        b.scheduler.agents[agent].agent.blackboard.write("state.status", "B 世界的")
        assert a.scheduler.agents[agent].agent.blackboard.read("state.status") == "A 世界的", (
            "两个世界的同名角色共用了一个黑板"
        )
    finally:
        a.close()
        b.close()


def test_the_cached_variant_batches_but_says_so(redis):
    """批量版把往返从每 tick 80 次降到 2 次 —— 代价是那一 tick 状态确实在进程里。

    所以它只在"同一时刻只有一个进程推这个世界的时钟"的前提下能用。这条测试钉住
    那个代价是**真实存在**的,免得有人把它当成免费的加速。
    """
    board = CachedRedisBlackboard(redis, "t:cached")
    board.write("loc", "cafe")

    board.begin()
    board.write("loc", "workshop")
    # 还没 flush:别的进程看到的仍是旧值 —— 这就是那个代价
    assert RedisBlackboard(redis, "t:cached").read("loc") == "cafe"
    assert board.read("loc") == "workshop", "自己读自己该看见新值"

    board.flush()
    assert RedisBlackboard(redis, "t:cached").read("loc") == "workshop"


# ---- 时钟与跨进程的锁 -------------------------------------------------------


def test_the_clock_lives_in_redis_and_has_one_answer(world, redis):
    """**"现在是第几 tick"只能有一个答案。**

    两个进程各推各的时钟,世界就分叉了 —— 而分叉之后两边都还在正常跑,只是不再是
    同一个世界。这正是这个仓库最怕的那种坏:照跑,但给的不是同一个东西。
    """
    from anima_world.redis_state import RedisClock, clock_key

    world.tick(30)
    outsider = RedisClock(redis, clock_key("t"))
    assert outsider.get() == world.scheduler.clock == 30
    world.tick(5)
    assert outsider.get() == 35, "别的进程读到的时钟停在了旧值"


def test_reopening_does_not_wind_the_clock_back(tmp_path, redis):
    """重开一个世界不该把时钟拨回去 —— Redis 里已有的值说了算。"""
    from anima_world.redis_state import RedisClock, clock_key

    first = World.open(
        str(tmp_path / "w.db"), force_mock_llm=True, redis=redis, world_id="t"
    )
    first.tick(40)
    first.close()
    assert RedisClock(redis, clock_key("t")).get() == 40

    again = World.open(
        str(tmp_path / "w.db"), force_mock_llm=True, redis=redis, world_id="t"
    )
    try:
        assert again.scheduler.clock == 40, "重开把时钟拨回去了"
    finally:
        again.close()


def test_an_action_locks_the_world_against_other_processes(world, redis):
    """一个动作执行期间,别的进程拿不到世界锁。

    **一个动作原子**这条要求跨进程也成立才有意义:两个 agent 进程同时提交动作,
    必须一个做完另一个才开始,否则 world-rules 的双缓冲、三源仲裁、`events.seq`
    的折叠顺序全都失去依据。
    """
    from anima_world import tools as tools_mod
    from anima_world.redis_state import RedisLock, lock_key

    agent = _an_agent(world)
    got: list[bool] = []
    real = tools_mod.call

    def spy(ctx, tool_id, params):
        outsider = RedisLock(redis, lock_key("t"), wait_seconds=0.2)
        got.append(outsider.acquire(blocking=False))
        return real(ctx, tool_id, params)

    tools_mod.call = spy
    try:
        world.act(agent, "broadcast", {"text": "占着锁呢"})
    finally:
        tools_mod.call = real
    assert got == [False], "动作执行期间别的进程拿到了世界锁 —— 跨进程不是原子的"


def test_the_lock_is_reentrant_and_releases(redis):
    """一个动作里工具会再拿一次锁(`move_agent` 自己就拿)—— 不可重入就是死锁。"""
    from anima_world.redis_state import RedisLock

    mine = RedisLock(redis, "t:lock", wait_seconds=0.2)
    other = RedisLock(redis, "t:lock", wait_seconds=0.2)
    with mine:
        with mine:  # 重入
            assert other.acquire(blocking=False) is False
        assert other.acquire(blocking=False) is False, "内层退出就把锁放了 —— 深度没算对"
    assert other.acquire(blocking=False) is True
    other.release()


def test_a_lock_left_behind_by_a_dead_process_expires(redis):
    """拿着锁的进程崩了,世界不能永远停摆 —— ttl 到了别人能接手。"""
    import time

    from anima_world.redis_state import RedisLock

    dead = RedisLock(redis, "t:dead", ttl_ms=60)
    dead.acquire()
    alive = RedisLock(redis, "t:dead", wait_seconds=2.0)
    assert alive.acquire(blocking=False) is False
    time.sleep(0.12)
    assert alive.acquire(blocking=False) is True, "锁过期之后没人接得了手"
    alive.release()


# ---- 谁在路上、谁在干嘛 -----------------------------------------------------


def test_another_process_knows_she_is_on_the_road(world, redis):
    """`_transit` 此前是纯内存,后果很具体。

    另一个进程不知道她**正在赶路**,就会让她"走开"、让她跟一个还没走到的人搭话 ——
    而"在途"这道闸恰恰是引擎用来把约束变成等待、把等待变成相遇的
    (提示词里那段自相矛盾的身份声明,修的也是同一种病)。
    """
    from anima_world.redis_state import RedisDict, transit_key

    agent = _an_agent(world)
    world.tick(50)
    target = next(
        p for p in world._tool_runtime.point_ids()
        if p != world._tool_runtime.agent_location(agent)
    )
    assert world.act(agent, "walk", {"location": target}, surface="body")["ok"]

    outsider = RedisDict(redis, transit_key("t"))
    trip = outsider.get(agent)
    assert trip, "她在路上,而另一个进程不知道"
    assert trip["to"] == target
    assert "arrive_at" in trip, "别的进程看不出她什么时候到"

    for _ in range(60):
        world.tick(1)
        if not outsider:
            break
    assert not outsider, "她到了,而在途集合没清"


def test_another_process_knows_what_she_is_doing(world, redis):
    """`_current_action` 存的是 `ActionDescriptor` —— 不是 JSON 原生的,要带编解码。"""
    from anima_world.actions import ActionDescriptor
    from anima_world.redis_state import RedisDict, current_action_key, decode_action

    world.tick(60)
    outsider = RedisDict(redis, current_action_key("t"), decode=decode_action)
    doing = dict(outsider.items())
    assert doing, "跑了 60 tick,没有任何人在干任何事"
    for agent_id, action in doing.items():
        assert isinstance(action, ActionDescriptor), (
            f"{agent_id} 那条取回来不是 ActionDescriptor 而是 {type(action).__name__} —— "
            "编解码丢了类型"
        )
        assert action.kind


def test_the_redis_dict_only_pretends_to_be_a_dict_where_it_really_is(redis):
    """只实现真正被用到的操作 —— 多实现一个就多一处"看起来像 dict 但边角上不是"。"""
    from anima_world.redis_state import RedisDict

    d = RedisDict(redis, "t:d")
    assert not d and len(d) == 0
    d["x"] = {"to": "cafe"}
    assert bool(d) and len(d) == 1 and "x" in d
    assert d["x"] == {"to": "cafe"}
    assert d.get("x")["to"] == "cafe"
    assert d.get("没有的", "兜底") == "兜底"
    assert d.items() == [("x", {"to": "cafe"})]
    assert d.pop("x") == {"to": "cafe"}
    assert not d
    assert d.pop("x", "兜底") == "兜底"
    with pytest.raises(KeyError):
        d["x"]


# ---- 规划,以及"投影不进 Redis"这个决定 -------------------------------------


def test_plans_move_too(world, redis):
    from anima_world.redis_state import RedisDict, plans_key

    assert isinstance(world.scheduler._plans, RedisDict)
    world.scheduler._plans["夏"] = [{"kind": "walk", "params": {"location": "cafe"}}]
    outsider = RedisDict(redis, plans_key("t"))
    assert outsider.get("夏")[0]["kind"] == "walk"


def test_the_projection_is_not_moved_but_caught_up(tmp_path, redis):
    """**投影是派生的,不进 Redis。**

    它从事件日志折出来,而日志本来就是共享的。存一份派生数据的唯一后果,是多出一种
    "它和日志不一致"的坏法 —— 而这个仓库最怕的就是那类。

    但**不搬不等于不管**:进程 A 记了一条事件,进程 B 的投影里还没有它,而 B 正是
    靠投影判断"她买得起吗""他们认识吗"。所以 B 要能追上。
    """
    a = World.open(str(tmp_path / "w.db"), force_mock_llm=True, redis=redis, world_id="t")
    try:
        a.tick(40)
        # 同一个世界文件的第二个"进程"
        b = World.open(str(tmp_path / "w.db"), force_mock_llm=True, redis=redis, world_id="t")
        try:
            seen_before = b.scheduler._projection_seq
            # 让 A 真的写进去一条:光 tick 不一定产生事件 —— 事件只在动作**改变**
            # 时才发(`_emit_on_transition`),大家在睡觉的那段时间一条都没有。
            assert a.act(_an_agent(a), "broadcast", {"text": "A 写的"})["ok"]
            caught = b.scheduler.catch_up_projection()
            assert caught > 0, "A 写了一批事件,B 一条都没追上"
            assert b.scheduler._projection_seq > seen_before
            # 再追一次不该重复折
            assert b.scheduler.catch_up_projection() == 0
        finally:
            b.close()
    finally:
        a.close()


def test_acting_catches_up_first(world, redis):
    """提交动作前先追赶 —— 否则会拿着过时的投影去判断世界。"""
    agent = _an_agent(world)
    world.tick(20)
    calls: list[int] = []
    real = world.scheduler.catch_up_projection

    def counting():
        calls.append(1)
        return real()

    world.scheduler.catch_up_projection = counting
    try:
        world.act(agent, "broadcast", {"text": "喂"})
    finally:
        world.scheduler.catch_up_projection = real
    assert calls, "act() 没有先追赶投影"


# ---- 事件日志:唯一真相那张表 -----------------------------------------------


def _drive(log, script):
    return [log.append(e) for e in script]


_SCRIPT = [
    {"ts": 0, "type": "travel", "who": "夏", "payload": {"to": "cafe"}},
    {"ts": 1, "type": "narrative", "who": "夏", "payload": {"text": "她推开门"}},
    {"ts": 2, "type": "travel", "who": "柔", "payload": {"to": "home"}},
    {"ts": 3, "type": "payment", "who": "夏", "loc": "cafe", "payload": {"amount": 12}},
]


def test_the_two_event_logs_answer_identically(tmp_path, redis):
    """**两个实现互验。**

    换后端最坏的坏法不是崩,是"两边都能跑,但答案不一样" —— 而事件日志是唯一真相,
    它答错一次,所有投影跟着错。所以不各测各的:同一串事件喂给两个实现,逐个问题
    比对答案。
    """
    import sqlite3

    from anima_world.db import open_db
    from anima_world.events import EventLog
    from anima_world.redis_state import RedisEventLog, events_key

    conn = open_db(str(tmp_path / "w.db"))
    try:
        sqlite_log = EventLog(conn)
        redis_log = RedisEventLog(redis, events_key("cmp"))
        for log in (sqlite_log, redis_log):
            _drive(log, _SCRIPT)

        def shape(events):
            return [(e.seq, e.ts, e.type, e.who, e.loc, e.payload) for e in events]

        assert shape(sqlite_log.replay()) == shape(redis_log.replay())
        assert sqlite_log.max_seq() == redis_log.max_seq() == 4
        for kwargs in (
            {}, {"who": "夏"}, {"kind": "travel"}, {"who": "夏", "kind": "travel"},
        ):
            assert sqlite_log.count(**kwargs) == redis_log.count(**kwargs), kwargs
        for since in (0, 1, 3, 9):
            assert shape(sqlite_log.replay(since)) == shape(redis_log.replay(since)), since
        for kwargs in ({"limit": 2}, {"since_seq": 1, "limit": 2, "kind": "travel"}):
            assert shape(sqlite_log.page(**kwargs)) == shape(redis_log.page(**kwargs)), kwargs
    finally:
        conn.close()


def test_two_appenders_get_unique_increasing_seqs(redis):
    """`seq` 的保序是多进程下最不能含糊的东西之一。

    `RPUSH` 返回新长度,而 Redis 是单线程的 —— 所以两个进程同时追加,各自拿到唯一且
    递增的号。没有这一条,"日志是唯一真相、重放能重建状态"整个失去依据。
    """
    import threading

    from anima_world.redis_state import RedisEventLog, events_key

    seqs: list[int] = []
    lock = threading.Lock()

    def hammer(n):
        log = RedisEventLog(redis, events_key("race"))
        for i in range(25):
            e = log.append({"ts": i, "type": "t", "who": f"w{n}", "payload": {}})
            with lock:
                seqs.append(e.seq)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(seqs) == 100
    assert len(set(seqs)) == 100, "有 seq 撞车了 —— 重放重建不出这个世界"
    assert sorted(seqs) == list(range(1, 101)), "seq 不连续 —— since_seq 分页会漏事件"


# ---- 世界的量 ---------------------------------------------------------------


def test_the_two_stock_stores_answer_identically(tmp_path, redis):
    """两个实现互验 —— 量答错一次,规律就一路错下去,而且照跑。"""
    from anima_world.db import open_db
    from anima_world.redis_state import RedisStockStore
    from anima_world.stocks import StockStore

    conn = open_db(str(tmp_path / "w.db"))
    try:
        a, b = StockStore(conn), RedisStockStore(redis, "cmp")
        for store in (a, b):
            store.set("tree:oak", "树高", 3.2, 10)
            store.set_many("tree:oak", {"生长速度": 0.004, "最大树高": 12.0}, 10)
            store.set("tree:elm", "树高", 5.0, 10)
            store.set("agent:夏", "功力", 120.0, 3)
            store.set("world", "季节", 2.0, 0)

        assert a.get("tree:oak", "树高") == b.get("tree:oak", "树高")
        assert a.get("tree:oak", "没有的", 7.0) == b.get("tree:oak", "没有的", 7.0) == 7.0
        assert a.of("tree:oak") == b.of("tree:oak")
        assert a.owners() == b.owners()
        for kind in ("tree", "agent", "world", "mine"):
            assert a.owners(kind) == b.owners(kind), kind
            assert a.snapshot_kind(kind) == b.snapshot_kind(kind), kind
        assert a.snapshot("tree:oak") == b.snapshot("tree:oak")
        assert a.snapshot_many(["tree:oak", "world"]) == b.snapshot_many(["tree:oak", "world"])

        pending = {"tree:oak": {"树高": 3.3}, "tree:elm": {"树高": 5.1}}
        assert a.write_round(pending, 22) == b.write_round(pending, 22)
        assert a.snapshot_kind("tree") == b.snapshot_kind("tree")

        for store in (a, b):
            store.delete("tree:elm", "树高")
        assert a.owners() == b.owners(), "删光最后一个键之后,owner 名单对不上"
        for store in (a, b):
            store.delete("agent:夏")
        assert a.owners() == b.owners()
    finally:
        conn.close()


def test_the_rules_get_the_same_answer_on_either_store(tmp_path, redis):
    """不只比接口,连**规律真跑一轮**的结果也比。

    `dt` 从量自己的 `updated_tick` 算,所以 tick 有没有跟着值一起存对,决定了树长
    多高。这条是接口比对看不出来的。
    """
    from anima_world.db import open_db
    from anima_world.redis_state import RedisStockStore
    from anima_world.rules import parse_rules
    from anima_world.stocks import StockStore, evaluate_due

    spec = [{
        "id": "grow", "every": {"ticks": 12}, "for_each": {"kind": "tree"},
        "set": {"树高": "min(树高 + 生长速度 * dt, 最大树高)"},
    }]
    rules = parse_rules(spec)
    conn = open_db(str(tmp_path / "w.db"))
    try:
        results = []
        for store in (StockStore(conn), RedisStockStore(redis, "rules")):
            store.set_many("tree:oak", {"树高": 3.2, "生长速度": 0.01, "最大树高": 12.0}, 0)
            last_run: dict[str, int] = {}
            for now in range(12, 289, 12):
                evaluate_due(store, rules, now, last_run=last_run)
            results.append(round(store.get("tree:oak", "树高"), 6))
        assert results[0] == results[1], f"同一条规律两个后端长出不同的树:{results}"
        assert results[0] > 3.2, "树根本没长,这条测试没在验它想验的"
    finally:
        conn.close()


def test_the_stock_store_keeps_its_batching_promise(redis):
    """**性能承诺跟着一起搬。**

    这一层文档里写着"一万棵树跑一个世界日 1.4 秒",而那个数字是改出来的不是天生的:
    第一版逐个 owner 查、逐个 commit,2000 棵树就到 72ms/tick。换了后端把那条承诺
    丢掉,和没搬一样糟 —— 而且这次真丢过一回:`write_round` 里每个 owner 一次
    `SADD`,两千棵树就是白花的两千条命令。

    所以这条不测绝对时间(机器和后端都会变),测**命令条数**:批量取和整轮写都必须
    是"一次问完",条数不该随 owner 数线性膨胀到不可接受。
    """
    from anima_world.redis_state import RedisStockStore

    store = RedisStockStore(redis, "perf")
    for i in range(200):
        store.set_many(f"tree:t{i}", {"树高": 1.0, "生长速度": 0.01}, 0)

    sent: list[str] = []
    real_pipeline = redis.pipeline

    def counting_pipeline(*a, **k):
        pipe = real_pipeline(*a, **k)
        real_execute = pipe.execute

        def execute(*ea, **ek):
            sent.append(len(getattr(pipe, "command_stack", []) or []))
            return real_execute(*ea, **ek)

        pipe.execute = execute
        return pipe

    redis.pipeline = counting_pipeline
    try:
        snap = store.snapshot_kind("tree")
        assert len(snap) == 200
        store.write_round({owner: {"树高": 2.0} for owner in snap}, 5)
    finally:
        redis.pipeline = real_pipeline

    assert len(sent) == 2, f"批量取 + 整轮写应该各一次 pipeline,实际发了 {len(sent)} 次"
    read_cmds, write_cmds = sent
    assert read_cmds == 200, "批量取该是每个 owner 一条 HGETALL,一次问完"
    # 200 条 HSET + **一条** SADD。每个 owner 一次 SADD 就是 400,那正是丢掉的那次。
    assert write_cmds == 201, (
        f"整轮写发了 {write_cmds} 条命令 —— owner 名单该一次加完,不是一个一条"
    )


# ---- 记忆 / 提示词模板 / 可见性 ---------------------------------------------


def test_the_two_memory_stores_answer_identically(tmp_path, redis):
    """记忆答错一次,她说出来的话就不是她该记得的事 —— 而且照跑。"""
    from anima_world.db import open_db
    from anima_world.memory_store import MemoryStore
    from anima_world.redis_state import RedisMemoryStore

    conn = open_db(str(tmp_path / "w.db"))
    try:
        script = [
            (0, "observation", "下雨了", 0.3),
            (0, "observation", "他说了难听的话", 0.9),   # 同 tick:排序键必须再看 id
            (10, "seed", "这家店是我盘下来的", 0.8),
            (20, "observation", "卖出第一杯咖啡", 0.7),
        ]
        a, b = MemoryStore(conn), RedisMemoryStore(redis, "mem")
        for store in (a, b):
            for tick, kind, summary, importance in script:
                store.add("夏", tick=tick, kind=kind, summary=summary, importance=importance)

        def shape(rows):
            return [(r["summary"], r["kind"], round(float(r["importance"]), 6)) for r in rows]

        assert shape(a.query("夏")) == shape(b.query("夏")), "同 tick 时次序对不上"
        assert shape(a.query("夏", kind="seed")) == shape(b.query("夏", kind="seed"))
        assert shape(a.query("夏", min_importance=0.7)) == shape(b.query("夏", min_importance=0.7))
        assert a.query("查无此人") == b.query("查无此人") == []

        assert shape(a.retrieve("夏", now_tick=30, query="咖啡", k=2)) == \
               shape(b.retrieve("夏", now_tick=30, query="咖啡", k=2))
        # 检索是复习:两边加固的强度也要一样
        assert [round(float(r["strength"]), 6) for r in a.query("夏")] == \
               [round(float(r["strength"]), 6) for r in b.query("夏")]

        for store in (a, b):
            store.decay_pass("夏", now_tick=2000)
        assert [round(float(r["strength"]), 6) for r in a.query("夏")] == \
               [round(float(r["strength"]), 6) for r in b.query("夏")], "两个后端遗忘速度不同"

        for store in (a, b):
            store.set_anchor(2, True)
        assert shape(a.anchors("夏")) == shape(b.anchors("夏"))
    finally:
        conn.close()


def test_the_two_prompt_stores_answer_identically(tmp_path, redis):
    from anima_world.db import open_db
    from anima_world.prompt_store import PromptStore
    from anima_world.redis_state import RedisPromptStore

    conn = open_db(str(tmp_path / "w.db"))
    try:
        a, b = PromptStore(conn), RedisPromptStore(redis, "pr")
        for store in (a, b):
            store.set("chat.x", "模板A", "说明")
            store.set("chat.y", "模板B")
            store.set("chat.x", "模板A2")          # 改内容不该把说明冲掉
        for store in (a, b):
            assert store.has("chat.x") and not store.has("没有的")
            assert store.get("chat.x") == "模板A2"
            assert store.get("没有的", "兜底") == "兜底"
        assert [(r["name"], r["template"], r.get("description")) for r in a.list()] == \
               [(r["name"], r["template"], r.get("description")) for r in b.list()]
    finally:
        conn.close()


def test_the_two_visibility_stores_answer_identically(tmp_path, redis):
    """**没声明 = 感知不到**那条默认值,换后端也必须原样保住。"""
    from anima_world.db import open_db
    from anima_world.perception import VisibilityStore
    from anima_world.redis_state import RedisVisibilityStore

    conn = open_db(str(tmp_path / "w.db"))
    try:
        a, b = VisibilityStore(conn), RedisVisibilityStore(redis, "vis")
        for store in (a, b):
            store.declare("tree", "树高", "here", "门口那棵老橡树")
            store.declare("world", "季节", "public")
            store.declare("agent", "功力", "self")
            store.place("tree:oak", "cafe", "老橡树")
            store.place("mine:north", "hill")
        assert a.declarations() == b.declarations()
        assert a.rules_map() == b.rules_map()
        assert a.at("cafe") == b.at("cafe")
        assert a.at("没这个地方") == b.at("没这个地方") == {}
        assert a.place_of("tree:oak") == b.place_of("tree:oak") == "cafe"
        assert a.place_of("没这个东西") == b.place_of("没这个东西") is None
        for store in (a, b):
            with pytest.raises(ValueError):
                store.declare("tree", "x", "乱写的档")
    finally:
        conn.close()
