"""随时间无限增长的那些东西住 MySQL —— 三个后端逐个问题比答案。

## 为什么要分这一家

前面十一步把世界搬进了 Redis,而 Redis 主要活在内存里。实测一个三人世界:

```
 世界日     Redis 内存    增量    占大头的键
   1天        997 KB     +30 KB   events=18KB  prompts=16KB
  20天       1228 KB    +129 KB   events=158KB memories=77KB
```

**只有 `events` 与 `memories` 在随时间线性增长** —— 20 天里它俩占了内存增量的九成。
一年 4.7 MB/世界;**一千个世界跑一年 4.6 GB 常驻**,而且永远不回落。

别的东西随**世界的规模**有界(黑板是每人 20 个键,地图创世后不动),不随时间涨。
所以分界线是增长性,不是冷热 —— **内存装得下一个热但有界的东西,装不下一个冷但
无限的东西。**

## 为什么三方比,而不是各测各的

这套跨实现互验在 Redis 那一轮抓出了十来个只有对比才看得见的错:字段名差一个字、
`description` 缺省是 `None` 不是空串、日切不只漂价还补货、地图种子是扁平不是嵌套……
每一个单测都测不出来,因为**两边都能跑,只是答案不一样**。

本机没有 MySQL 就整体 skip(`ANIMA_TEST_MYSQL` 指向一个可连的实例时才跑)。
"""
from __future__ import annotations

import os

import pytest

pymysql = pytest.importorskip("pymysql")

_DSN = os.environ.get("ANIMA_TEST_MYSQL")
pytestmark = pytest.mark.skipif(
    not _DSN, reason="没有 ANIMA_TEST_MYSQL(形如 unix:///tmp/my.sock 或 host:port)"
)


def _connect():
    if _DSN.startswith("unix://"):
        return pymysql.connect(
            unix_socket=_DSN[len("unix://"):], user="root",
            database="anima_test", charset="utf8mb4",
        )
    host, _, port = _DSN.partition(":")
    return pymysql.connect(
        host=host, port=int(port or 3306), user="root",
        database="anima_test", charset="utf8mb4",
    )


@pytest.fixture()
def mysql_factory():
    """**开 World 的测试用这个,不要用裸连接。**

    `pymysql` 的 threadsafety 是 1,而这个引擎有线程池。裸连接大多数时候相安无事,
    某次在负载下炸成 `read of closed file` —— 一个离原因很远、看不出是并发的报错。
    这条 fixture 存在就是为了不让测试自己踩那个坑,然后把竞态当成"偶发"。
    """
    return _connect


@pytest.fixture()
def mysql(request):
    from anima_world.mysql_state import ensure_schema

    conn = _connect()
    prefix = f"t{abs(hash(request.node.name)) % 100000}_"
    with conn.cursor() as cur:
        for table in ("events", "memories", "conversations", "messages"):
            cur.execute(f"DROP TABLE IF EXISTS `{prefix}{table}`")
    conn.commit()
    ensure_schema(conn, prefix)
    yield conn, prefix
    with conn.cursor() as cur:
        for table in ("events", "memories", "conversations", "messages"):
            cur.execute(f"DROP TABLE IF EXISTS `{prefix}{table}`")
    conn.commit()
    conn.close()


@pytest.fixture()
def redis():
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.FakeStrictRedis(decode_responses=True)


_SCRIPT = [
    {"ts": 0, "type": "travel", "who": "夏", "payload": {"to": "cafe"}},
    {"ts": 1, "type": "narrative", "who": "夏", "payload": {"text": "她推开门"}},
    {"ts": 2, "type": "travel", "who": "柔", "payload": {"to": "home"}},
    {"ts": 3, "type": "payment", "who": "夏", "loc": "cafe", "payload": {"amount": 12}},
]


def test_both_event_logs_answer_identically(mysql, redis):
    """事件日志答错一次,所有投影跟着错 —— 而且两边都还能跑。

    (2.0 之前这条是三方互验;SQLite 版随 world.db 整体退役,基准换成 Redis。)
    """
    from anima_world.mysql_state import MySQLEventLog
    from anima_world.redis_state import RedisEventLog, events_key

    conn, prefix = mysql
    logs = {
        "redis": RedisEventLog(redis, events_key("cmp")),
        "mysql": MySQLEventLog(conn, prefix),
    }
    for log in logs.values():
        for event in _SCRIPT:
            log.append(event)

    def shape(events):
        return [(e.seq, e.ts, e.type, e.who, e.loc, e.payload) for e in events]

    base_log = logs["redis"]
    base = shape(base_log.replay())
    for name, log in logs.items():
        assert shape(log.replay()) == base, f"{name} 的 replay 和别人不一样"
        assert log.max_seq() == 4, name
        for kwargs in ({}, {"who": "夏"}, {"kind": "travel"},
                       {"who": "夏", "kind": "travel"}):
            assert log.count(**kwargs) == base_log.count(**kwargs), (name, kwargs)
        for since in (0, 1, 3, 9):
            assert shape(log.replay(since)) == shape(base_log.replay(since)), \
                (name, since)
        for kwargs in ({"limit": 2}, {"since_seq": 1, "limit": 2, "kind": "travel"}):
            assert shape(log.page(**kwargs)) == shape(base_log.page(**kwargs)), \
                (name, kwargs)


def test_both_memory_stores_answer_identically(mysql, redis):
    """记忆答错一次,她说出来的话就不是她该记得的事。

    (原是三方互验;SQLite 版已退役,基准换成 Redis。)
    """
    from anima_world.mysql_state import MySQLMemoryStore
    from anima_world.redis_state import RedisMemoryStore

    conn, prefix = mysql
    stores = {
        "redis": RedisMemoryStore(redis, "mem", capacity=50),
        "mysql": MySQLMemoryStore(conn, prefix),
    }
    script = [
        (0, "observation", "下雨了", 0.3),
        (0, "observation", "他说了难听的话", 0.9),   # 同 tick:排序键要再看 id
        (10, "seed", "这家店是我盘下来的", 0.8),
        (20, "observation", "卖出第一杯咖啡", 0.7),
    ]
    for store in stores.values():
        for tick, kind, summary, importance in script:
            store.add("夏", tick=tick, kind=kind, summary=summary,
                      importance=importance)

    def shape(rows):
        return [(r["summary"], r["kind"], round(float(r["importance"]), 6))
                for r in rows]

    def strengths(store):
        return [round(float(r["strength"]), 6) for r in store.query("夏")]

    base_store = stores["redis"]
    base = shape(base_store.query("夏"))
    for name, store in stores.items():
        assert shape(store.query("夏")) == base, f"{name} 的次序或内容不一样"
        assert shape(store.query("夏", kind="seed")) == \
               shape(base_store.query("夏", kind="seed")), name
        assert shape(store.query("夏", min_importance=0.7)) == \
               shape(base_store.query("夏", min_importance=0.7)), name
        assert store.query("查无此人") == [], name

    for store in stores.values():
        store.retrieve("夏", now_tick=30, query="咖啡", k=2)
    base_strength = strengths(base_store)
    for name, store in stores.items():
        assert strengths(store) == base_strength, f"{name} 的加固不一样"

    for store in stores.values():
        store.decay_pass("夏", now_tick=2000)
    base_decayed = strengths(base_store)
    for name, store in stores.items():
        assert strengths(store) == base_decayed, f"{name} 的遗忘速度不一样"

    for store in stores.values():
        store.set_anchor(2, True)
    base_anchors = shape(base_store.anchors("夏"))
    for name, store in stores.items():
        assert shape(store.anchors("夏")) == base_anchors, name


# 关系图的互验在 `tests/test_redis_state.py`(SQLite ↔ Redis)—— `edges` 不进 MySQL:
# 它有 `UNIQUE(subject, predicate, object)` 且谓词是闭集,上界 2×N²,按世界的规模
# 封顶而不按时间涨。理由与那条闸门见 `tests/test_bounded.py`。

def test_unicode_survives_the_round_trip(mysql):
    """世界是中文的。**建表字符集写错的后果是问号,而且不报错。**"""
    from anima_world.mysql_state import MySQLEventLog

    conn, prefix = mysql
    log = MySQLEventLog(conn, prefix)
    log.append({"ts": 0, "type": "narrative", "who": "苏晚夏",
                "payload": {"text": "她把抹布搭在吧台上,说了句「明天见」。"}})
    back = log.replay()[0]
    assert back.who == "苏晚夏"
    assert "「明天见」" in back.payload["text"]


def test_two_worlds_in_one_database_do_not_share_history(mysql):
    """一个库上跑多个世界是常态。撞表的后果是两个世界共用一段历史。"""
    from anima_world.mysql_state import MySQLEventLog, ensure_schema

    conn, prefix = mysql
    other = f"{prefix}b_"
    ensure_schema(conn, other)
    try:
        MySQLEventLog(conn, prefix).append({"ts": 0, "type": "a", "payload": {}})
        MySQLEventLog(conn, other).append({"ts": 0, "type": "b", "payload": {}})
        assert [e.type for e in MySQLEventLog(conn, prefix).replay()] == ["a"]
        assert [e.type for e in MySQLEventLog(conn, other).replay()] == ["b"]
    finally:
        with conn.cursor() as cur:
            for table in ("events", "memories", "conversations", "messages"):
                cur.execute(f"DROP TABLE IF EXISTS `{other}{table}`")
        conn.commit()


# ── 转录:用户点名要在 MySQL 的那一样 ───────────────────────────────────────


def test_both_transcript_stores_answer_identically(mysql, redis):
    """聊天记录是所有表里最该离开内存的:一条消息几百字,只增不减,而世界只在
    会话关闭时收一个摘要事件。换后端时**答案必须一个字不差** —— 转录错了不会
    报错,只会让她"记得"的上一次见面不是真的那一次。

    (原是 SQLite ↔ MySQL;SQLite 版已退役,如今比的是 Redis ↔ MySQL ——
    没给 `mysql=` 的世界转录住 Redis,这俩才是今天真会同时在线的两个后端。)
    """
    from anima_world.mysql_state import MySQLChatStore
    from anima_world.redis_state import RedisChatStore

    conn, prefix = mysql
    stores = {
        "redis": RedisChatStore(redis, "cmp-chat"),
        "mysql": MySQLChatStore(conn, prefix),
    }
    ids = {}
    for name, store in stores.items():
        cid = store.start_conversation("夏", 100, location="cafe", player_id="p1")
        other = store.start_conversation("夏", 100, player_id="p2")
        mid = store.add_message(cid, "user", "在吗", 101)
        aid = store.add_message(cid, "agent", "在的,刚擦完吧台。", 102)
        store.add_message(other, "user", "喂", 103)
        store.annotate_message(aid, stance="warm", intent="converse",
                               intent_confidence=0.8,
                               tool_calls=[{"tool": "delay_reply"}])
        store.close(cid, "他来问了句话,我说我在。", 200)
        ids[name] = (cid, other, mid, aid)

    def conv_shape(rows):
        return [(r["agent_id"], r["status"], r["started_at"], r["closed_at"],
                 r["summary"], r["message_count"], r["participants"],
                 r["location"], r["player_id"]) for r in rows]

    def msg_shape(rows):
        return [(r["role"], r["content"], r["created_at"]) for r in rows]

    base = stores["redis"]
    base_cid = ids["redis"][0]
    for name, store in stores.items():
        cid, other, _mid, _aid = ids[name]
        assert conv_shape(store.list_conversations("夏")) == \
               conv_shape(base.list_conversations("夏")), f"{name} 的会话列表不一样"
        assert msg_shape(store.messages_for(cid)) == \
               msg_shape(base.messages_for(base_cid)), f"{name} 的消息不一样"
        assert msg_shape(store.recent_messages(cid, 1)) == \
               msg_shape(base.recent_messages(base_cid, 1)), name
        assert store.past_summaries("夏", 5) == base.past_summaries("夏", 5), name
        # **按玩家分档**:跨会话的记忆是按关系记的,不是按角色记的
        assert store.past_summaries("夏", 5, player_id="p2") == [], name
        assert store.active_conversation("夏", player_id="p1") is None, \
            f"{name}:关掉的会话还算 open"
        assert int(store.active_conversation("夏", player_id="p2")["id"]) == other, name
        assert store.conversation_meta(cid) == base.conversation_meta(base_cid), \
            f"{name} 算出的 meta 不一样"
        assert store.conversation_meta(cid)["tools_used"] == ["delay_reply"], name
        assert store.idle_open_conversations(now=999, timeout=1) != [], name


def test_participants_come_back_as_a_list_not_a_string(mysql):
    """SQLite 存的是 TEXT 里的 JSON 串,MySQL 是 JSON 列。**读出来必须同形状** ——
    否则宿主拿到的一会儿是 list 一会儿是 str,而两边都"能跑"。"""
    from anima_world.mysql_state import MySQLChatStore, ensure_schema

    conn, prefix = mysql
    ensure_schema(conn, prefix)
    store = MySQLChatStore(conn, prefix)
    cid = store.start_conversation("夏", 0, player_name="阿玖", player_id="p1")
    row = store.get(cid)
    assert isinstance(row["participants"], list), \
        f"participants 是 {type(row['participants']).__name__},不是 list"
    assert {p["id"] for p in row["participants"]} == {"p1", "夏"}
    assert any(p.get("name") == "阿玖" for p in row["participants"])


def test_the_transcript_has_one_instance_and_every_holder_shares_it(
    mysql_factory, redis
):
    """**攥着另一份转录的人,一个都不能漏。**

    这条的前身是踩出来的:换 `chat_state` 后端时只改了 `world.chat_state`,而
    `ChatService` 在构造时把旧的存进了自己的 `_state` —— 聊天照跑、照写,写进了
    旧后端,测试全绿。2.0 之后没有"运行中换后端"这回事(`_rebind_chat_store`
    已删):转录后端在 `World.__init__` 一次定型,防同一类病靠的是**只构造一个
    实例,人人共享它** —— 这条枚举持有者,加了新的持有者也要加进名单,
    否则它会安静地写进另一份。
    """
    from anima_world.api import World
    from anima_world.mysql_state import MySQLChatStore

    world = World.open("rebind", redis=redis, mysql=mysql_factory,
                       force_mock_llm=True)
    try:
        store = world.chat_store
        assert isinstance(store, MySQLChatStore), "给了 mysql= 转录就该归 MySQL"
        holders = {
            "chat_service._store": world.chat_service._store,
            "session_manager._store": world.session_manager._store,
            "chat_state._transcript": world.chat_state._transcript,
        }
        stale = [name for name, held in holders.items() if held is not store]
        assert not stale, f"这些人攥着另一份转录:{stale}"
        # meta_provider 是绑定方法,比对它转发到哪儿
        assert world.session_manager._meta_provider.__self__ is world.chat_state
    finally:
        world.close()


def test_one_connection_per_thread(mysql):
    """**`pymysql` 的 `threadsafety` 是 1:模块可以多线程用,一条连接不行。**

    而这个引擎有线程池(叙事、规划),它们都会记事件。共享一条连接的后果不是
    "慢一点",是两个线程的协议帧交叉、连接当场作废 —— `InterfaceError(0, '')`。
    实测撞到过:一个世界跑到第 12 天崩在一条 INSERT 上,而前 11 天全对。

    这条并发写一遍,验两件事:线程各拿各的连接,而且写进去的一条不少。
    """
    import threading

    from anima_world.mysql_state import MySQLEventLog, ThreadLocalConnection, ensure_schema

    _conn, prefix = mysql
    pool = ThreadLocalConnection(_connect)
    ensure_schema(pool, prefix)
    log = MySQLEventLog(pool, prefix)
    seen: dict[int, int] = {}
    errors: list[BaseException] = []

    def writer(n: int) -> None:
        try:
            seen[threading.get_ident()] = id(pool._conn)
            for i in range(20):
                log.append({"ts": n * 100 + i, "type": "beat", "who": f"t{n}", "payload": {}})
        except BaseException as exc:  # noqa: BLE001 — 线程里的异常默认吞掉
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"并发写炸了:{errors[0]!r}"
    assert len(set(seen.values())) == len(seen) >= 4, "不同线程共用了同一条连接"
    assert log.count() == 80, f"并发写少了 {80 - log.count()} 条"
    for n in range(4):
        assert log.count(who=f"t{n}") == 20


# ── 跨两个后端的一致性 ─────────────────────────────────────────────────────


def test_a_crash_between_the_two_backends_does_not_strand_her(
    tmp_path, mysql, mysql_factory, redis
):
    """**一个动作横跨两个后端,而崩溃不挑时候。**

    分家之后一次 `walk` 要写两个地方:在途状态进 Redis,`travel` 事件进 MySQL。
    写序是 Redis 先、MySQL 后(`emit_action` 里 `_start_journey` 在 `_record_event`
    之前),所以中间死掉的样子是:**Redis 说她在路上,而历史里没有这趟**。

    她不会永远卡在路上 —— 在途状态带着到达 tick,时钟一到照样落地。真正的伤是
    历史缺了一条:任何从事件重折出来的东西(经济账本、`catch_up_projection` 的
    投影)从此少算这一次,而且**永远不会自己补回来**。

    这条把伤面钉住:先证明它确实发生,再证明它的边界是"历史少一条",不是
    "她卡住了"或"两个进程看到不同的世界"。判据变了要当场知道。
    """
    from anima_world.api import World

    world = World.open("crash", redis=redis, mysql=mysql_factory,
                       force_mock_llm=True)
    try:
        agent = next(iter(world.state()["agents"]))
        here = world.state()["agents"][agent].get("location")
        dest = next(loc["id"] for loc in world.state()["locations"]
                    if loc["id"] != here and loc["kind"] == "point")

        # **包住世界自己那条日志**,而不是另建一个指向别的表的。
        # 另建的那种"也能通过",但它验的不是这个世界真正走的那条路。
        real_log = world.scheduler.event_log

        class _Dying:
            dead = False

            def __getattr__(self, name):
                return getattr(real_log, name)

            def append(self, event):
                if _Dying.dead:
                    raise RuntimeError("进程在这里死了")
                return real_log.append(event)

        world.scheduler.event_log = _Dying()
        before = world.scheduler.event_log.count()

        _Dying.dead = True
        out = world.act(agent, "walk", {"location": dest}, surface="body")
        _Dying.dead = False

        assert out["ok"] is False, "记事件炸了,而 act 报了成功"
        assert "进程在这里死了" in str(out.get("error"))
        assert world.scheduler.event_log.count() == before, "半条事件落地了"

        # 伤面:Redis 那一半留下了,历史那一半没有。
        stranded = world.scheduler._transit.get(agent)
        assert stranded is not None, (
            "写序变了 —— 现在是 MySQL 先 Redis 后。那是更好的顺序,"
            "但这条测试的前提没了,重写它而不是删掉它"
        )
        assert stranded["to"] == dest

        # 边界一:她不会永远卡在路上。到达 tick 一到就落地。
        for _ in range(int(stranded["arrive_at"]) - world.scheduler.clock + 1):
            world.tick()
        assert world.state()["agents"][agent].get("location") == dest, (
            "她卡在路上了 —— 这比历史少一条严重得多"
        )

        # 边界二:两个进程看到的是同一个世界(在途在 Redis 上,不在进程内存里)。
        other = World.open("crash", redis=redis, mysql=mysql_factory,
                           force_mock_llm=True)
        try:
            assert other.state()["agents"][agent].get("location") == dest
        finally:
            other.close()
    finally:
        world.close()


def test_redis_keeps_no_stale_copy_of_what_mysql_owns(
    tmp_path, mysql, mysql_factory, redis
):
    """**两份真相里有一份不会更新,是这个仓库最怕的坏法。**

    搬家分两步:先整个搬进 Redis,再把无限增长的那几样接到 MySQL。第二步只换掉
    store 对象 —— 第一步写进 Redis 的那份**留在原地,而且冻在创世那一刻**。

    引擎自己不读它(store 已经是 MySQL 版了),所以全量测试一片绿。但 Redis 的全部
    意义是"另一个只有 Redis 连接的进程读得到这个世界" —— 那个进程读到的会是一个
    从创世起什么都没发生过的世界。实测过:MySQL 289 条事件,Redis 那份停在 50 条。

    两条一起验:MySQL 接手的表不往 Redis 搬,而且**上一次只用 Redis 时留下的那份
    要删掉**(一个世界可能先只用 Redis 跑过,后来才接上 MySQL)。
    """
    from anima_world.api import World
    from anima_world.mysql_state import GROWS_FOREVER, ensure_schema
    from anima_world.redis_state import KEY_PREFIX

    conn, prefix = mysql
    ensure_schema(conn, prefix)

    # 先只用 Redis 跑一段 —— 于是 Redis 里真的有一份 events / memories
    redis_only = World.open("stale", redis=redis, force_mock_llm=True)
    try:
        redis_only.tick(288)
        assert redis.llen(f"{KEY_PREFIX}:stale:events") > 0, "这条测试的前提没建立起来"
    finally:
        redis_only.close()

    # 再接上 MySQL
    world = World.open("stale", redis=redis, mysql=mysql_factory,
                       force_mock_llm=True)
    try:
        world.tick(288)
        assert world.scheduler.event_log.count() > 0

        leftovers = [
            key for key in redis.keys(f"{KEY_PREFIX}:stale:*")
            if key.rsplit(":", 1)[-1].split(":")[0] in GROWS_FOREVER
        ]
        assert not leftovers, (
            f"Redis 里躺着 MySQL 才有权威的那几样:{leftovers} —— "
            f"只有 Redis 连接的进程会读到一个冻住的世界"
        )
    finally:
        world.close()


def test_both_backends_erase_a_player_identically(mysql, redis):
    """法务抹除在两个后端要给出同一份答案 —— 删多少、留什么、逐字一致。

    抹除是拿真实身份数据当输入的写路径,两个后端各答各的话,一边的用户被删干净、
    另一边还留着他的话 —— 而两边都跑得动,谁也不报错。所以照这文件里其余测试的
    规矩:同一个剧本喂两边,答案必须一个字不差。
    """
    from anima_world.mysql_state import (
        MySQLChatStore, MySQLEventLog, MySQLMemoryStore,
    )
    from anima_world.redis_state import (
        RedisChatStore, RedisEventLog, RedisMemoryStore, events_key,
    )

    conn, prefix = mysql

    # ── 转录:整场删、别人的不动、幂等 ──────────────────────────────────────
    chat_stores = {
        "redis": RedisChatStore(redis, "cmp-erase"),
        "mysql": MySQLChatStore(conn, prefix),
    }
    for name, store in chat_stores.items():
        cid = store.start_conversation("夏", 100, player_id="p1")
        store.add_message(cid, "user", "我是阿檀,我的秘密是蓝色气球", 101)
        store.add_message(cid, "agent", "我听见了。", 102)
        keep = store.start_conversation("夏", 100, player_id="p2")
        store.add_message(keep, "user", "我是小竹", 103)

        preview = store.erase_player("p1", dry_run=True)
        assert preview == {"conversations": 1, "messages": 2}, name
        assert store.list_conversations("夏") and len(store.list_conversations("夏")) == 2, \
            f"{name}:dry_run 动了转录"
        assert store.erase_player("p1") == preview, name
        assert store.erase_player("p1") == {"conversations": 0, "messages": 0}, \
            f"{name}:抹除不幂等"
        rows = store.list_conversations("夏")
        assert [r["player_id"] for r in rows] == ["p2"], f"{name}:别人的会话被误伤"

    # ── 事件:rewrite 原地改写,seq 与其余字段一动不动 ──────────────────────
    logs = {
        "redis": RedisEventLog(redis, events_key("cmp-erase")),
        "mysql": MySQLEventLog(conn, prefix),
    }
    for log in logs.values():
        for event in _SCRIPT:
            log.append(event)
        second = log.replay()[1]
        log.rewrite(2, {"ts": second.ts, "type": second.type, "who": second.who,
                        "loc": second.loc, "payload": {"text": "(已抹除)"}})

    def shape(events):
        return [(e.seq, e.ts, e.type, e.who, e.loc, e.payload) for e in events]

    assert shape(logs["redis"].replay()) == shape(logs["mysql"].replay()), \
        "两个后端改写后的日志不一样"
    assert shape(logs["redis"].replay())[1][5] == {"text": "(已抹除)"}
    assert logs["redis"].max_seq() == logs["mysql"].max_seq() == len(_SCRIPT), \
        "rewrite 动了行数"

    # ── 记忆:按出处删行、按名字改文本 ─────────────────────────────────────
    mem_stores = {
        "redis": RedisMemoryStore(redis, "cmp-erase", capacity=50),
        "mysql": MySQLMemoryStore(conn, prefix),
    }
    for name, store in mem_stores.items():
        store.add("夏", tick=1, kind="conversation", summary="和阿檀聊了聊", event_seq=7)
        store.add("夏", tick=2, kind="reflection", summary="阿檀和小竹都来过")
        assert store.erase_for_event_seqs({7}, dry_run=True) == 1, name
        assert store.count() == 2, f"{name}:dry_run 删了行"
        assert store.erase_for_event_seqs({7}) == 1, name
        assert store.redact_summaries({"阿檀": "(已注销)"}, dry_run=True) == 1, name
        assert store.redact_summaries({"阿檀": "(已注销)"}) == 1, name
        assert [r["summary"] for r in store.query("夏")] == ["(已注销)和小竹都来过"], \
            f"{name}:旁及的记忆没换对名字"
        assert store.erase_for_event_seqs({7}) == 0, f"{name}:按出处删不幂等"


def test_erase_player_through_a_mysql_world(mysql_factory, redis):
    """法务抹除走 `World` 真门,而转录/记忆/事件全在 MySQL 上 —— store 级互验
    再齐,World 少接一根线它也各绿各的。"""
    import json as _json

    from anima_world.api import World

    world = World.open("erase-w", redis=redis, mysql=mysql_factory,
                       force_mock_llm=True)
    try:
        agent = next(iter(world.scheduler.agents))
        world.scheduler.contact_store.note_contact(
            agent, "p9", tick=world.scheduler.clock, name="阿檀")
        world.scheduler._record_and_deliver({
            "type": "state_change", "who": agent,
            "payload": {"kind": "sentiment_delta", "as": agent, "target": "p9",
                        "delta": 0.5,
                        "as_name": world.scheduler.agent_display_name(agent),
                        "target_name": "阿檀"},
        })
        world.record_chat_turn(agent, "p9", [
            {"role": "user", "content": "我是阿檀,我的秘密是蓝色气球"},
            {"role": "assistant", "content": "我听见了。"},
        ])

        receipt = world.erase_player("p9", reason="用户要求删除")
        assert receipt["conversations"] >= 1 and receipt["events"] >= 1

        text = _json.dumps(
            [{"who": e.who, "payload": e.payload}
             for e in world.scheduler.event_log.replay()],
            ensure_ascii=False)
        assert "阿檀" not in text and "蓝色气球" not in text
        assert all(c.get("player_id") != "p9" for c in world.conversations(agent))
        assert world.erase_player("p9")["events"] == 0, "MySQL 世界上的抹除不幂等"
    finally:
        world.close()
