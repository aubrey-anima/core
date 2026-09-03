"""1.x 的 `world.db` → 2.0 的世界文件。

这道桥只走一次,而**一次性的代码最容易赌**:迁错了不报错,只是那个世界安静地
少一层、或者时间不对。所以这份文件盯的不是"函数被调用了",是**迁过去之后
那个世界还是不是同一个世界**。

三条来自真世界的教训,每条一个测试:

1. **时钟的权威在 `db_meta.clock`,不是 `MAX(events.ts)`。** 先写错过:1.x 的事件
   里混着一行 unix 时间戳的脏数据,拿 MAX 当时钟,世界照样开得起来 —— 只是她
   以为现在是第 620 万天。
2. **1.x 的 seq 有洞而 2.0 不允许。** 洞是 AUTOINCREMENT 在事务回滚时消耗掉的号。
   重新编号会静默改掉 `memories.event_seq` 的指向(真世界里 356 条引用它)。
3. **表清单是闭集。** 少迁一张的下场是这个世界少一层,而日志干净。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from anima_world.migrate_v1 import LEGACY_TABLES, MigrationError, migrate_world_db


def _legacy_db(path, *, clock="3865", events=None, extra_tables=(), stocks=True):
    """一个最小但形状真实的 1.x world.db。"""
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE db_meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
                             type TEXT NOT NULL, who TEXT, loc TEXT, payload TEXT NOT NULL);
        CREATE TABLE locations (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                                description TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'point',
                                parent TEXT, x REAL, y REAL, w REAL, h REAL, updated_at TEXT NOT NULL);
        CREATE TABLE stocks (owner TEXT NOT NULL, key TEXT NOT NULL, value REAL NOT NULL DEFAULT 0,
                             updated_tick INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (owner, key));
        CREATE TABLE agent_needs (agent_id TEXT NOT NULL, need TEXT NOT NULL, value REAL NOT NULL DEFAULT 1.0,
                                  updated_tick INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (agent_id, need));
        CREATE TABLE world_rules (id TEXT PRIMARY KEY, definition TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,
                               tick INTEGER NOT NULL, kind TEXT NOT NULL, summary TEXT NOT NULL,
                               importance REAL NOT NULL, anchor INTEGER NOT NULL DEFAULT 0,
                               event_seq INTEGER, created_at INTEGER NOT NULL,
                               strength REAL NOT NULL DEFAULT 1.0, last_access INTEGER,
                               access_count INTEGER NOT NULL DEFAULT 0, source_ids TEXT);
        """
    )
    if clock is not None:
        db.execute("INSERT INTO db_meta VALUES ('clock', ?)", (clock,))
    db.execute(
        "INSERT INTO locations VALUES ('cafe','咖啡店','临海',"
        "'point',NULL,0.1,0.1,NULL,NULL,'2026-01-01')"
    )
    if stocks:
        db.execute("INSERT INTO stocks VALUES ('tree:oak','树高',3.2,7)")
        db.execute("INSERT INTO stocks VALUES ('tree:oak','生长速度',0.004,0)")
    db.execute("INSERT INTO agent_needs VALUES ('夏','energy',0.8,10)")
    db.execute("INSERT INTO agent_needs VALUES ('夏','hunger',0.6,10)")
    db.execute(
        "INSERT INTO world_rules VALUES ('grow', ?, '2026-01-01')",
        # **要写一条真规律** —— 坏规律引擎是整体拒绝的,拿一条假的当夹具会让
        # "迁过来的世界开得了机"这类测试红在一个和迁移无关的地方。
        (json.dumps({
            "id": "grow",
            "every": {"days": 1},
            "for_each": {"kind": "tree"},
            "set": {"树高": "树高 + 生长速度 * dt"},
        }),),
    )
    for seq, ts, kind in events or [(1, 0, "location_join"), (2, 5, "narrative")]:
        db.execute(
            "INSERT INTO events (seq, ts, type, who, loc, payload) VALUES (?,?,?,?,?,?)",
            (seq, ts, kind, "夏", "cafe", "{}"),
        )
    db.execute(
        "INSERT INTO memories (agent_id,tick,kind,summary,importance,event_seq,created_at,source_ids)"
        " VALUES ('夏',5,'observation','下雨了',0.5,2,5,'[1,2]')"
    )
    for name in extra_tables:
        db.execute(f'CREATE TABLE "{name}" (x TEXT)')
    db.commit()
    db.close()
    return path


def _records(path, **kw):
    return list(migrate_world_db(path, world_id="w", **kw))


def _one(records, kind, key=None):
    for record in records:
        if record["kind"] == kind and (key is None or record.get("key") == key):
            return record
    return None


# ── 时钟 ────────────────────────────────────────────────────────────────────


def test_时钟取自_db_meta_而不是最大的事件_ts(tmp_path):
    """1.x 的事件里混着一行 unix 时间戳,拿 MAX(ts) 当时钟会让世界跑到第 620 万天。

    而世界**照样开得起来** —— 这正是要拿真库演一遍才看得见的那种错。
    """
    db = _legacy_db(tmp_path / "w.db", clock="3865",
                    events=[(1, 0, "location_join"), (2, 1785820326, "narrative")])
    clock = _one(_records(db), "redis", "clock")
    assert clock["value"] == "3865", "权威是 db_meta.clock,不是那行脏数据"


def test_没有时钟的库当场拒绝(tmp_path):
    # 默默从 0 开始的话,她的记忆里全是"还没发生"的事。
    db = _legacy_db(tmp_path / "w.db", clock=None)
    with pytest.raises(MigrationError, match="clock"):
        _records(db)


# ── seq 的洞 ────────────────────────────────────────────────────────────────


def test_seq_的洞用占位事件补上_原有编号一个不改(tmp_path):
    """2.0 的 seq 是连续整数(Redis 列表下标就是它),而 1.x 会有 AUTOINCREMENT 空号。

    重新编号会静默改掉 `memories.event_seq` 的指向 —— 那条记忆从此指向另一件事。
    """
    db = _legacy_db(tmp_path / "w.db", events=[(1, 0, "a"), (5, 3, "b")])
    gaps: list[int] = []
    records = list(migrate_world_db(db, world_id="w", gaps=gaps))
    events = [r for r in records if r["kind"] == "event"]

    assert [e["seq"] for e in events] == [1, 2, 3, 4, 5], "必须连续"
    assert gaps == [2, 3, 4], "补了哪几个要报出去"
    assert events[4]["type"] == "b", "原来那条的编号一个不改"
    assert all(e["type"] == "legacy_seq_gap" for e in events[1:4])

    # 记忆引用的 event_seq 仍然指向原来那条。
    memory = _one(records, "mysql")
    assert memory["row"]["event_seq"] == 2, memory["row"]


def test_占位事件在重放时是惰性的(tmp_path):
    from anima_world.projection import project_events
    from anima_world.types import Event

    db = _legacy_db(tmp_path / "w.db", events=[(1, 0, "a"), (3, 1, "b")])
    events = [r for r in _records(db) if r["kind"] == "event"]
    projected = project_events(
        [Event(seq=e["seq"], ts=e["ts"], type=e["type"], who=e["who"],
               loc=e["loc"], payload=e["payload"]) for e in events]
    )
    assert projected is not None, "补的空号不该让重放炸掉"


# ── 表清单是闭集 ────────────────────────────────────────────────────────────


def test_不认识的表当场报错而不是跳过(tmp_path):
    # 跳过等于让这个世界安静地少一层:比如 agent_stance 丢了,她对每个人的
    # 关系性意图归零,而世界照跑、日志干净。
    db = _legacy_db(tmp_path / "w.db", extra_tables=("something_new",))
    with pytest.raises(MigrationError, match="something_new"):
        _records(db)


def test_不是世界库的_sqlite_不会被当成世界(tmp_path):
    path = tmp_path / "other.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE whatever (x TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(MigrationError):
        _records(path)


# ── 形状 ────────────────────────────────────────────────────────────────────


def test_量按_owner_拆开_并且攒出_stock_owners(tmp_path):
    # 少了 stock_owners,"这个世界里有量的东西有哪些"问不出来,而每个量还在:
    # 规律照跑,只是没有任何一处枚举得到它们。
    db = _legacy_db(tmp_path / "w.db")
    records = _records(db)
    stock = _one(records, "redis", "stock:tree:oak")
    assert json.loads(stock["value"]["树高"]) == [3.2, 7], "值是 [数值, tick]"

    owners = _one(records, "redis", "stock_owners")
    assert owners["type"] == "set" and "tree:oak" in owners["value"]


def test_需求从一需求一行合并成一角色一行(tmp_path):
    db = _legacy_db(tmp_path / "w.db")
    needs = _one(_records(db), "redis", "needs")
    body = json.loads(needs["value"]["夏"])
    assert body["energy"] == 0.8 and body["hunger"] == 0.6


def test_规律的_definition_还原成对象而不是字符串(tmp_path):
    # 不还原的话规律层拿到一个字符串,而坏规律是**整体拒绝** —— 整个世界开不了机。
    db = _legacy_db(tmp_path / "w.db")
    rules = _one(_records(db), "redis", "world_rules")
    assert isinstance(json.loads(rules["value"]["grow"])["definition"], dict)


def test_会增长的那几样发成_mysql_记录而不是_redis(tmp_path):
    # 和 2.0 自己 dump 出来的形状一致,所以迁移和导出走的是同一条装载路径。
    db = _legacy_db(tmp_path / "w.db")
    memory = _one(_records(db), "mysql")
    assert memory["table"] == "memories"
    assert memory["row"]["summary"] == "下雨了"
    assert memory["row"]["source_ids"] == [1, 2], "TEXT 里的 JSON 要还原"


def test_每张_1_x_的表都在清单里登记过():
    """清单是闭集,而闭集要写全 —— 漏登记一张,遇到它时会抛错(那是对的),
    但更早的问题是没人知道它该去哪儿。这条把清单本身钉住。"""
    assert len(LEGACY_TABLES) == 26, "1.x 有 26 张表(含 sqlite_sequence)"


def test_迁过来的世界开机时不会被橱窗塞进一棵别人的树(tmp_path):
    """播种按"这张表恰好还空着"判,只在第一次开机时和创世重合。

    一个 1.x 迁过来的世界 `stocks` 表本来就是空的 —— 开机一次就被内置演示世界
    塞进 `tree:harbor_oak` 和两个世界量。**世界照跑、日志干净**,只是它凭空多了
    一棵别人世界里的橡树。这是 rules / stock_visibility / stock_places 那三个
    已经补过的闸漏掉的第四个。
    """
    import fakeredis

    from anima_world.api import World
    from anima_world.migrate_v1 import write_migrated_world
    from anima_world.world_package import import_world_file

    # **量必须一个都没有** —— 触发条件是"这张表恰好空着"。夹具里留着两行的话,
    # "空表才播"那条自己就挡住了,于是这条测试会在闸撤掉之后照样绿(我先这么写过,
    # 而一条不会红的回归测试比没有更坏:它让人以为这个洞被守着)。
    # 线上那个 qingshi 正是一个量都没有的世界。
    db = _legacy_db(tmp_path / "w.db", stocks=False)
    package = tmp_path / "w.cyberworld"
    write_migrated_world(db, package, world_id="q", name="q")

    redis = fakeredis.FakeRedis(decode_responses=True)
    import_world_file(package, redis=redis, world_id="q")
    world = World.open("q", redis=redis)
    try:
        owners = {k.split(":", 2)[-1] for k in redis.keys("anima:q:stock:*")}
        assert "stock:tree:harbor_oak" not in owners, "橱窗的橡树跑进来了"
        assert not any("harbor_oak" in o for o in owners), f"多了别人的东西:{owners}"
    finally:
        world.close(wait=False)


def test_一天的规划存的是_JSON_不是_repr(tmp_path):
    """`RedisDict` 不给 `encode` 就是原样交给 redis-py,而它对一个不认得的对象
    只会 `str()` —— 于是 `plans` 里存的是 `repr(Plan(...))`,读回来是字符串。

    下场是 `state()` 在 `plan.steps` 上 AttributeError,**而且只在她真的有计划的
    那一刻才炸**:一个刚创世的世界永远碰不到,一个跑了两周的世界一开机就 500。
    上线时正是这样:两个世界好好的,第三个(唯一装着规划器的那个)整个读不了。
    """
    import fakeredis

    from anima_world.planner import Plan, PlanStep
    from anima_world.redis_state import RedisDict, decode_plan, encode_plan

    redis = fakeredis.FakeRedis(decode_responses=True)
    plans = RedisDict(redis, "t:plans", encode=encode_plan, decode=decode_plan)
    plans["夏"] = Plan(agent_id="夏", day=3, steps=(PlanStep(420, "walk", {"location": "cafe"}, "散步"),))

    got = plans.get("夏")
    assert got is not None and not isinstance(got, str), "读回来必须是 Plan,不是 repr 字符串"
    assert got.steps[0].start_min == 420
    assert got.steps[0].params["location"] == "cafe"
    assert got.steps[0].note == "散步"


def test_读不懂的旧规划当作没有_而不是让整个世界塌掉():
    """这个 bug 存在期间写下的 repr 字符串还躺在老世界里。

    读不懂就当没有 —— 她下一轮会重新规划;而让 `state()` 塌掉的话,
    整个世界对外就是一个 500。
    """
    import fakeredis

    from anima_world.redis_state import RedisDict, decode_plan, encode_plan

    redis = fakeredis.FakeRedis(decode_responses=True)
    redis.hset("t2:plans", "夏", "Plan(agent_id='夏', day=14, steps=())")
    plans = RedisDict(redis, "t2:plans", encode=encode_plan, decode=decode_plan)
    assert plans.get("夏") is None


def test_加密的配置行不迁进世界(tmp_path):
    """引擎自己的导出路径有 `_strip_secrets`,而迁移**直读 SQLite、绕过了它**。

    于是 1.x 那行 Fernet 密文 `llm.api_key` 原样进了 `.cyberworld` —— 而那是
    **分发物**。功能上看不出来(2.0 手里没有 Fernet 钥匙,读的时候会点名跳过),
    所以它能一直躺着。线上迁完之后是在 Redis 里翻到的。
    """
    db = _legacy_db(tmp_path / "w.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL,"
        " value_type TEXT NOT NULL, category TEXT NOT NULL, is_secret INTEGER NOT NULL DEFAULT 0,"
        " description TEXT, updated_at TEXT NOT NULL);"
    )
    conn.execute("INSERT INTO config VALUES ('llm.api_key','gAAAAABq密文','str','llm',1,'','x')")
    conn.execute("INSERT INTO config VALUES ('needs.enabled','1','bool','needs',0,'','x')")
    conn.commit()
    conn.close()

    dropped: dict[str, int] = {}
    records = list(migrate_world_db(db, world_id="w", dropped=dropped))
    config = _one(records, "redis", "config")
    assert "needs.enabled" in config["value"], "普通配置要迁过去"
    assert "llm.api_key" not in config["value"], "密文进了世界文件 —— 那是分发物"
    assert dropped.get("config.secret") == 1, "丢掉了要报数,不能静默"


def test_名字像密钥的也不迁_哪怕_is_secret_是_0(tmp_path):
    """**不能只信 `is_secret` 这一栏,因为 1.x 自己把它标错过。**

    `llm.background.api_key` 在 1.x 里是 `is_secret=0` 的**明文行** —— 于是
    "剥 is_secret"那道闸对它完全无效,一把明文 API key 直接进了分发物。
    实测三个线上世界的 `.cyberworld` 里都有。

    宁可多剥一个 `*_key` 配置项(作者重新填一次),也不能漏一把真钥匙 ——
    发出去就收不回来。
    """
    db = _legacy_db(tmp_path / "w.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL,"
        " value_type TEXT NOT NULL, category TEXT NOT NULL, is_secret INTEGER NOT NULL DEFAULT 0,"
        " description TEXT, updated_at TEXT NOT NULL);"
    )
    # is_secret=0,但它是一把真钥匙 —— 1.x 的原样
    conn.execute("INSERT INTO config VALUES ('llm.background.api_key','sk-真钥匙','str','llm',0,'','x')")
    conn.execute("INSERT INTO config VALUES ('llm.background.model','gpt-4','str','llm',0,'','x')")
    conn.execute("INSERT INTO config VALUES ('needs.enabled','1','bool','needs',0,'','x')")
    conn.commit()
    conn.close()

    config = _one(list(migrate_world_db(db, world_id="w")), "redis", "config")
    assert "llm.background.api_key" not in config["value"], "明文钥匙进了分发物"
    assert "llm.background.model" in config["value"], "模型名不是密钥,该迁"
    assert "needs.enabled" in config["value"]
