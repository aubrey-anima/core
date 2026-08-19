"""随时间无限增长的那些东西住 MySQL,不住内存。

## 分界线是"增长性",不是"是不是世界"

前面十一步把世界搬进了 Redis,而 Redis 主要活在内存里。实测一个三人世界:

```
 世界日     Redis 内存    增量    占大头的键
   1天        997 KB     +30 KB   events=18KB  prompts=16KB
  20天       1228 KB    +129 KB   events=158KB memories=77KB
```

**只有两样在随时间线性增长:`events` 与 `memories`** —— 20 天里它俩占了内存增量的
九成。平均每世界日 13 KB,一年 4.7 MB;**一千个世界跑一年就是 4.6 GB 常驻内存**,
而且只会涨,永远不会回落。

别的东西不是这样:黑板是每人 20 个键,`stocks` 随实体数走,地图和行为树创世之后基本
不动 —— 它们**随世界的规模有界**,不随时间涨。

所以分界线是:

    随时间无限增长   →  MySQL(事件、记忆、关系边、转录)
    随世界规模有界   →  Redis(黑板、时钟、在途、当前动作、量、需求、意图……)

这条线和"热不热"高度重合但不完全一样,而**增长性才是那个真正的判据** ——
内存装得下一个热但有界的东西,装不下一个冷但无限的东西。

## 为什么不是"全都 MySQL"

`events.seq` 的连续保序在 Redis 上是白捡的(`RPUSH` 返回新长度,而 Redis 单线程)。
换到 MySQL 要靠自增主键 + 事务,能做,但那是另一件要想清楚的事(见
`docs/AGENT-RUNTIME.md` §4 的三个问题)。而黑板那种每 tick 碰 80 次的东西放进
一个要解析 SQL 的后端,是把最热的一层放进最慢的地方。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from anima_world.memory_store import provenance_of

logger = logging.getLogger(__name__)

# 只有这几样进 MySQL —— 判据是"随时间无限增长"。
#
# **`edges` 曾经在这个名单里,是错的。** 关系边有 `UNIQUE(subject, predicate, object)`,
# 而谓词是闭集(`friendship` / `rivalry`,scheduler.py 里写死的两个),主宾都是
# `agent:<id>` —— 上界是 2×N²,**按世界的规模封顶,不按时间涨**。它属于 Redis。
# 判据是判据:分错一张表的代价是把一个有界的热读放进最慢的后端。
#
# ⚠️ 那个闭集是**承重的**:哪天让 LLM 自己造谓词,边就不再有上界,这条要重新算
# (`tests/test_bounded.py` 盯着)。
GROWS_FOREVER = ("events", "memories", "conversations", "messages")

SCHEMA = {
    "events": """
        CREATE TABLE IF NOT EXISTS `{prefix}events` (
          seq     BIGINT AUTO_INCREMENT PRIMARY KEY,
          ts      BIGINT NOT NULL,
          type    VARCHAR(64) NOT NULL,
          who     VARCHAR(128),
          loc     VARCHAR(128),
          payload JSON NOT NULL,
          KEY idx_type (type),
          KEY idx_who (who)
        ) CHARACTER SET utf8mb4
    """,
    "memories": """
        CREATE TABLE IF NOT EXISTS `{prefix}memories` (
          id           BIGINT AUTO_INCREMENT PRIMARY KEY,
          agent_id     VARCHAR(128) NOT NULL,
          tick         BIGINT NOT NULL,
          kind         VARCHAR(64) NOT NULL,
          summary      TEXT NOT NULL,
          importance   DOUBLE NOT NULL DEFAULT 0.5,
          anchor       TINYINT NOT NULL DEFAULT 0,
          event_seq    BIGINT,
          created_at   BIGINT NOT NULL DEFAULT 0,
          strength     DOUBLE NOT NULL DEFAULT 1.0,
          last_access  BIGINT,
          access_count INT NOT NULL DEFAULT 0,
          source_ids   JSON,
          provenance   VARCHAR(16) NOT NULL DEFAULT 'experienced',
          KEY idx_agent (agent_id)
        ) CHARACTER SET utf8mb4
    """,
    "conversations": """
        CREATE TABLE IF NOT EXISTS `{prefix}conversations` (
          id               BIGINT AUTO_INCREMENT PRIMARY KEY,
          agent_id         VARCHAR(128) NOT NULL,
          status           VARCHAR(16) NOT NULL DEFAULT 'open',
          started_at       BIGINT NOT NULL,
          last_activity_at BIGINT NOT NULL,
          closed_at        BIGINT,
          summary          TEXT,
          message_count    INT NOT NULL DEFAULT 0,
          participants     JSON,
          location         VARCHAR(128),
          player_id        VARCHAR(128),
          KEY idx_agent (agent_id),
          KEY idx_status (status)
        ) CHARACTER SET utf8mb4
    """,
    "messages": """
        CREATE TABLE IF NOT EXISTS `{prefix}messages` (
          id                BIGINT AUTO_INCREMENT PRIMARY KEY,
          conversation_id   BIGINT NOT NULL,
          role              VARCHAR(32) NOT NULL,
          content           MEDIUMTEXT NOT NULL,
          created_at        BIGINT NOT NULL,
          stance            VARCHAR(64),
          intent            VARCHAR(64),
          intent_confidence DOUBLE,
          tool_calls        JSON,
          KEY idx_conversation (conversation_id)
        ) CHARACTER SET utf8mb4
    """,
}


class ThreadLocalConnection:
    """**每个线程一条自己的连接。**

    这不是优化,是正确性:`pymysql` 的 `threadsafety` 是 1 —— 模块可以多线程用,
    **一条连接不行**。而这个引擎有线程池(叙事、规划),它们都会记事件;共享一条
    连接的后果是 `InterfaceError(0, '')` —— 两个线程的协议帧交叉,连接就废了。
    实测撞到过:世界跑到第 12 天崩在一条 INSERT 上。

    Redis 那边没有这个问题(`redis-py` 的连接池自己管),所以这一层是 MySQL 特有的。

    `close()` 只关**这个线程**那条;别的线程的由它们自己或进程退出时收。
    """

    __slots__ = ("_connect", "_local")

    def __init__(self, connect: Any) -> None:
        import threading

        self._connect = connect
        self._local = threading.local()

    @property
    def _conn(self) -> Any:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self._connect()
        return conn

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        return self._conn.cursor(*args, **kwargs)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


def as_connection(mysql: Any) -> Any:
    """把调用方给的东西变成"每线程一条连接"。

    收三种:

    - **可调用对象**(推荐):`lambda: pymysql.connect(...)` —— 自动包成
      `ThreadLocalConnection`,每个线程按需要自己建一条。
    - 已经是 `ThreadLocalConnection`:原样用。
    - **一条裸连接**:能用,但**当场大声警告**。`pymysql` 的 threadsafety 是 1,
      而这个引擎有线程池(叙事、规划),它们都会记事件 —— 两个线程的协议帧交叉,
      连接就废了。

    裸连接为什么必须点名:它**不是必现**。大多数 tick 相安无事,某天在负载下炸成
    `InterfaceError: (0, '')` 或 `ValueError: read of closed file` —— 一个离原因
    很远、看不出是并发的报错。实测同一份代码跑两次:一次好的,一次崩在第 12 个
    世界日的一条 INSERT 上。**这种"大多数时候没事"正是最该在开机时说破的。**
    """
    if mysql is None or isinstance(mysql, ThreadLocalConnection):
        return mysql
    if callable(mysql) and not hasattr(mysql, "cursor"):
        return ThreadLocalConnection(mysql)
    logger.warning(
        "mysql= 收到一条裸连接。引擎有线程池,而 pymysql 的 threadsafety 是 1 —— "
        "多个线程共用一条连接会让协议帧交叉,连接当场作废(症状是 "
        "InterfaceError (0, '') 或 read of closed file,而且**不是必现**)。"
        "改传一个工厂:World.open(..., mysql=lambda: pymysql.connect(...))"
    )
    return mysql


#: 加法式迁移:`(表, 列, 列定义)`。**`CREATE TABLE IF NOT EXISTS` 对已经存在的表
#: 一个字都不会改** —— 新加的列在老库上永远不出现,而代码照读它、读到 None、
#: 然后静默走默认分支。这是"照跑但给错东西"的标准形状,所以补列要显式做。
#:
#: 只收**加法**(可空列 / 带默认值的列):改列、删列会让老引擎打不开这个库,
#: 那是主版本级的事,不该藏在一次 `ensure_schema` 里。
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # R3 记忆分型:她是亲历的、听说的、还是自己想出来的。
    ("memories", "provenance", "VARCHAR(16) NOT NULL DEFAULT 'experienced'"),
)


def ensure_schema(conn: Any, prefix: str = "") -> None:
    """建表,并把加法式迁移补上。

    `prefix` 让一个库上跑多个世界 —— 撞表的后果是两个世界共用一段历史。
    """
    with conn.cursor() as cur:
        for ddl in SCHEMA.values():
            cur.execute(ddl.format(prefix=prefix))
        for table, column, ddl in _ADDITIVE_COLUMNS:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.COLUMNS"
                " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s"
                " AND COLUMN_NAME = %s",
                (f"{prefix}{table}", column),
            )
            if int(cur.fetchone()[0]) == 0:
                cur.execute(f"ALTER TABLE `{prefix}{table}` ADD COLUMN `{column}` {ddl}")
    conn.commit()


class MySQLEventLog:
    """事件日志。**唯一真相那张表,而且是增长最快的那张。**

    接口和 `EventLog` / `RedisEventLog` 逐字相同(`append` / `replay` / `count` /
    `max_seq` / `page`),所以是直接替换。

    `seq` 用自增主键 —— 和 SQLite 版同源。**但要知道它和 Redis 版的一个区别**:
    Redis 那边 `RPUSH` 返回的长度保证连续,而 MySQL 的自增在事务回滚后**会留下
    空洞**。`since_seq` 分页仍然正确(它问的是"比这个大的"),但"seq 连续"这条
    不再成立 —— 任何依赖连续性的代码都会悄悄错。目前没有这种代码,写新代码时别引入。
    """

    __slots__ = ("_conn", "_prefix")

    def __init__(self, conn: Any, prefix: str = "") -> None:
        self._conn = conn
        self._prefix = prefix

    @property
    def _table(self) -> str:
        return f"`{self._prefix}events`"

    def append(self, event: dict) -> Any:
        from anima_world.types import Event

        ts = event["ts"]
        if ts < 0:
            raise ValueError("event ts MUST be non-negative")
        e = Event(
            seq=0, ts=ts, type=event["type"], who=event.get("who"),
            loc=event.get("loc"), payload=event.get("payload", {}),
        )
        with self._conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {self._table} (ts, type, who, loc, payload)"
                " VALUES (%s, %s, %s, %s, %s)",
                (e.ts, e.type, e.who, e.loc,
                 json.dumps(e.payload, ensure_ascii=False, default=str)),
            )
            e.seq = int(cur.lastrowid)
        self._conn.commit()
        return e

    def _rows(self, where: str, params: tuple, limit: int | None = None) -> list[Any]:
        from anima_world.types import Event

        sql = (
            f"SELECT seq, ts, type, who, loc, payload FROM {self._table}"
            f"{where} ORDER BY seq ASC"
        )
        if limit is not None:
            sql += " LIMIT %s"
            params = (*params, int(limit))
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            Event(seq=int(r[0]), ts=int(r[1]), type=str(r[2]),
                  who=r[3], loc=r[4],
                  payload=json.loads(r[5]) if isinstance(r[5], str) else (r[5] or {}))
            for r in rows
        ]

    @staticmethod
    def _filter(who: str | None, kind: str | None,
                since_seq: int | None = None) -> tuple[str, tuple]:
        parts, params = [], []
        if since_seq is not None:
            parts.append("seq > %s")
            params.append(int(since_seq))
        if who:
            parts.append("who = %s")
            params.append(who)
        if kind:
            parts.append("type = %s")
            params.append(kind)
        return ((" WHERE " + " AND ".join(parts)) if parts else "", tuple(params))

    def replay(self, since_seq: int = 0) -> list[Any]:
        where, params = self._filter(None, None, since_seq)
        return self._rows(where, params)

    def max_seq(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT MAX(seq) FROM {self._table}")
            row = cur.fetchone()
        return int(row[0] or 0) if row else 0

    def count(self, *, who: str | None = None, kind: str | None = None) -> int:
        where, params = self._filter(who, kind)
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {self._table}{where}", params)
            return int(cur.fetchone()[0])

    def page(self, *, since_seq: int = 0, limit: int = 100,
             who: str | None = None, kind: str | None = None) -> list[Any]:
        where, params = self._filter(who, kind, since_seq)
        return self._rows(where, params, limit)

    def rewrite(self, seq: int, event: dict) -> None:
        """原地改写一条已有事件(**seq 不变**)。只给法务抹除用 ——
        和 `RedisEventLog.rewrite` 同一份契约:不增删行,只换内容。"""
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE {self._table} SET ts=%s, type=%s, who=%s, loc=%s, payload=%s"
                " WHERE seq=%s",
                (int(event.get("ts") or 0), str(event.get("type") or ""),
                 event.get("who"), event.get("loc"),
                 json.dumps(event.get("payload", {}), ensure_ascii=False, default=str),
                 int(seq)),
            )
        self._conn.commit()


class MySQLMemoryStore:
    """她记得什么。**第二快的增长源** —— 一个角色的记忆只增不减(遗忘只是把强度调低)。

    检索的三因子打分照旧在 Python 里做(`score()`),SQL 只负责取行 —— 和 Redis 版
    同一条:**打分是世界的规则,不是存储**。排序键也照抄 `tick DESC, id DESC`。
    """

    __slots__ = ("_conn", "_prefix", "_config")

    def __init__(self, conn: Any, prefix: str = "", config_store: Any = None) -> None:
        self._conn = conn
        self._prefix = prefix
        self._config = config_store

    @property
    def _table(self) -> str:
        return f"`{self._prefix}memories`"

    _COLS = ("id", "agent_id", "tick", "kind", "summary", "importance", "anchor",
             "event_seq", "created_at", "strength", "last_access", "access_count",
             "source_ids", "provenance")

    def count(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {self._table}")
            (total,) = cur.fetchone()
        return int(total)

    def rebuild(self, events, trigger) -> int:
        """Replay events through `trigger` only if the store is empty (idempotent).

        与 SQLite 时代同一条契约:有行就一动不动 —— 记忆是持久状态,重放一遍
        等于把她的一生按今天的触发器重新裁一遍。
        """
        total = self.count()
        if total > 0:
            return total
        for event in events:
            descriptor = trigger(event)
            if descriptor is not None:
                self.add(
                    agent_id=descriptor.agent_id,
                    tick=descriptor.tick,
                    kind=descriptor.kind,
                    summary=descriptor.summary,
                    importance=descriptor.importance,
                    anchor=descriptor.anchor,
                    event_seq=descriptor.event_seq,
                )
        return self.count()

    def add(self, agent_id: str, tick: int, kind: str, summary: str,
            importance: float = 0.5, anchor: bool = False,
            event_seq: int | None = None, created_at: int | None = None,
            source_ids: list[int] | None = None,
            provenance: str | None = None) -> int:
        # **没说就按 kind 判**(`provenance_of`)—— 和 Redis 版写新行、和两个
        # 后端的读侧给老行补出处,是同一个函数。硬写一个 `"experienced"` 就是
        # 给"同一个 kind、新行读亲历、老行读听说"留位置。
        with self._conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {self._table}"
                " (agent_id, tick, kind, summary, importance, anchor, event_seq,"
                "  created_at, source_ids, provenance)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (agent_id, int(tick), kind, summary, float(importance),
                 1 if anchor else 0, event_seq,
                 int(tick if created_at is None else created_at),
                 json.dumps(list(source_ids or []), ensure_ascii=False),
                 str(provenance or provenance_of(kind))),
            )
            new_id = int(cur.lastrowid)
        self._conn.commit()
        return new_id

    def _dicts(self, rows: Any) -> list[dict[str, Any]]:
        out = []
        for row in rows:
            item = dict(zip(self._COLS, row))
            raw = item.get("source_ids")
            item["source_ids"] = json.loads(raw) if isinstance(raw, str) else (raw or [])
            item["anchor"] = int(item.get("anchor") or 0)
            # 老库补列之前写下的行读出来是 NULL —— 默认值写在读的这一侧,
            # 和 Redis 版逐字同一条(`redis_state._fill_provenance`)。
            # **按 kind 补,不是一律「亲历」**:老行里躺着 `kind='reaction'`
            # (八卦传过来的),把它报成亲历正是分型要治的那个病本身。
            if not item.get("provenance"):
                item["provenance"] = provenance_of(item.get("kind") or "")
            out.append(item)
        return out

    def query(self, agent_id: str, kind: str | None = None,
              min_importance: float | None = None) -> list[dict[str, Any]]:
        sql = f"SELECT {', '.join(self._COLS)} FROM {self._table} WHERE agent_id = %s"
        params: list[Any] = [agent_id]
        if kind is not None:
            sql += " AND kind = %s"
            params.append(kind)
        if min_importance is not None:
            sql += " AND importance >= %s"
            params.append(min_importance)
        # 和别的两个后端同一个排序键:创世注入的记忆全是 tick=0,光靠 tick 分不出
        # 先后,而不确定的次序意味着同一个世界在两台机器上召回不同的记忆。
        sql += " ORDER BY tick DESC, id DESC"
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return self._dicts(cur.fetchall())

    def retrieve(self, agent_id: str, *, now_tick: int, query: str | None = None,
                 k: int = 5, ticks_per_day: int = 288,
                 reinforce: bool = True) -> list[dict[str, Any]]:
        from anima_world.memory_retrieval import HALF_LIFE_DAYS_DEFAULT, score

        half_life = HALF_LIFE_DAYS_DEFAULT
        if self._config is not None:
            half_life = self._config.get("memory.half_life_days", default=half_life)
        rows = self.query(agent_id=agent_id)
        rows.sort(key=lambda m: -score(
            m, now_tick=now_tick, query=query,
            ticks_per_day=ticks_per_day, half_life_days=float(half_life),
        ))
        top = rows[: max(0, int(k))]
        if top and reinforce:
            with self._conn.cursor() as cur:
                cur.executemany(
                    f"UPDATE {self._table} SET strength = LEAST(strength + 0.3, 3.0),"
                    " last_access = %s, access_count = access_count + 1 WHERE id = %s",
                    [(int(now_tick), int(m["id"])) for m in top],
                )
            self._conn.commit()
        return top

    def decay_pass(self, agent_id: str, now_tick: int, ticks_per_day: int = 288) -> None:
        from anima_world.memory_retrieval import decayed_strength

        updates = []
        for row in self.query(agent_id=agent_id):
            if row.get("anchor"):
                continue   # 锚定的不衰减 —— 和 Redis/SQLite 时代同一条
            last = row.get("last_access")
            last = int(row.get("tick") or 0) if last is None else int(last)
            updates.append((
                decayed_strength(float(row.get("strength") or 1.0), last,
                                 int(now_tick), int(ticks_per_day)),
                int(row["id"]),
            ))
        if updates:
            with self._conn.cursor() as cur:
                cur.executemany(
                    f"UPDATE {self._table} SET strength = %s WHERE id = %s", updates
                )
            self._conn.commit()

    def forget_memory(self, memory_id: int) -> bool:
        """真的删掉一条记忆行(夜间固化清扫用)。语义与 Redis 版逐字相同。"""
        with self._conn.cursor() as cur:
            cur.execute(f"DELETE FROM {self._table} WHERE id = %s", (int(memory_id),))
            dropped = int(cur.rowcount or 0)
        self._conn.commit()
        return dropped > 0

    def retick(self, memory_id: int, tick: int, created_at: int | None = None) -> bool:
        """见 `RedisMemoryStore.retick`。**两个后端都要有** —— 只修得动 Redis 世界
        的迁移,等于让接了 MySQL 的世界永远带着那批脏 tick。"""
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE {self._table} SET tick = %s, created_at = %s WHERE id = %s",
                (int(tick), int(tick if created_at is None else created_at), int(memory_id)),
            )
            changed = int(cur.rowcount or 0)
        self._conn.commit()
        return changed > 0

    def set_anchor(self, memory_id: int, anchor: bool) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE {self._table} SET anchor = %s WHERE id = %s",
                (1 if anchor else 0, int(memory_id)),
            )
        self._conn.commit()

    def anchors(self, agent_id: str) -> list[dict[str, Any]]:
        return [r for r in self.query(agent_id) if int(r.get("anchor") or 0)]

    # ── 法务抹除(`World.erase_player`)——语义与 Redis 版逐字相同 ─────────

    def erase_for_event_seqs(self, seqs: set[int], *, dry_run: bool = False) -> int:
        """删掉由这些事件而起的记忆行,返回删了几行。判据与理由见 Redis 版。"""
        if not seqs:
            return 0
        wanted = sorted({int(s) for s in seqs})
        total = 0
        for i in range(0, len(wanted), 500):
            chunk = wanted[i:i + 500]
            marks = ",".join(["%s"] * len(chunk))
            with self._conn.cursor() as cur:
                if dry_run:
                    cur.execute(
                        f"SELECT COUNT(*) FROM {self._table}"
                        f" WHERE event_seq IN ({marks})", chunk)
                    total += int(cur.fetchone()[0])
                else:
                    cur.execute(
                        f"DELETE FROM {self._table}"
                        f" WHERE event_seq IN ({marks})", chunk)
                    total += int(cur.rowcount or 0)
        if not dry_run:
            self._conn.commit()
        return total

    def redact_summaries(self, replacements: dict[str, str], *,
                         dry_run: bool = False) -> int:
        """把摘要里出现的这些名字换掉,返回改了几行。替换在 Python 里做 ——
        和检索打分同一条理由:文本规则是世界的,不是存储的。"""
        if not replacements:
            return 0
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT id, summary FROM {self._table}")
            rows = cur.fetchall()
        changed = 0
        for row_id, summary in rows:
            text = str(summary or "")
            fresh = text
            for old, new in replacements.items():
                fresh = fresh.replace(old, new)
            if fresh != text:
                changed += 1
                if not dry_run:
                    with self._conn.cursor() as cur:
                        cur.execute(
                            f"UPDATE {self._table} SET summary=%s WHERE id=%s",
                            (fresh, int(row_id)))
        if changed and not dry_run:
            self._conn.commit()
        return changed


class MySQLChatStore:
    """转录:`conversations` + `messages`。**用户点名要在这儿的那一样。**

    它是所有表里最该离开内存的:一条消息动辄几百字,而聊天记录只增不减,还完全
    不参与世界的推演 —— 世界只在会话关闭时收到一个摘要事件(那条老不变量:
    "整场会话只在关闭时发一个事件")。把它放进 Redis 等于拿最贵的存储装最冷的数据。

    接口和 `ChatStore` 逐字相同,所以是直接替换。`participants` / `tool_calls` 在
    SQLite 那边是 TEXT 里的 JSON 串,这边是 JSON 列 —— **读出来必须是同一个形状**,
    否则宿主拿到的一会儿是 list 一会儿是 str。`_dicts` 统一在这儿抹平。
    """

    # `content_filter`:落库前的最后一道(`chat_store.filter_message_content`),
    # World 装的。⚠️ 这一格曾经不在 __slots__ 里 —— 于是 `World.open(mysql=…)`
    # 在装闸那一行当场 AttributeError,**任何带 MySQL 的世界都开不起来**;而
    # MySQL 测试没有服务就整体 skip,这条在本机永远是绿的。真 MySQL 一接就现形。
    __slots__ = ("_conn", "_prefix", "_lock", "content_filter")

    def __init__(self, conn: Any, prefix: str = "", lock: Any | None = None) -> None:
        import threading

        self._conn = conn
        self._prefix = prefix
        self._lock = lock if lock is not None else threading.RLock()
        self.content_filter: Any | None = None

    def _t(self, name: str) -> str:
        return f"`{self._prefix}{name}`"

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        for row in rows:
            for field in ("participants", "tool_calls"):
                raw = row.get(field)
                if isinstance(raw, (str, bytes)) and raw:
                    try:
                        row[field] = json.loads(raw)
                    except ValueError:
                        pass
        return rows

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(sql, params)
            rowid = int(cur.lastrowid or 0)
        self._conn.commit()
        return rowid

    # ── conversations ───────────────────────────────────────────────────────

    def active_conversation(
        self, agent_id: str, player_id: str | None = None
    ) -> dict[str, Any] | None:
        if player_id is None:
            return self._one(
                f"SELECT * FROM {self._t('conversations')} WHERE agent_id = %s"
                " AND status = 'open' ORDER BY id DESC LIMIT 1", (agent_id,))
        return self._one(
            f"SELECT * FROM {self._t('conversations')} WHERE agent_id = %s"
            " AND status = 'open' AND COALESCE(player_id, 'user') = %s"
            " ORDER BY id DESC LIMIT 1", (agent_id, player_id))

    def start_conversation(
        self, agent_id: str, ts: int,
        participants: list[dict[str, Any]] | None = None,
        location: str | None = None, player_id: str | None = None,
        player_name: str | None = None,
    ) -> int:
        pid = player_id or "user"
        if participants is None:
            user_entry: dict[str, Any] = {"id": pid, "kind": "user"}
            if player_name:
                user_entry["name"] = player_name
            participants = [user_entry, {"id": agent_id, "kind": "agent"}]
        return self._write(
            f"INSERT INTO {self._t('conversations')}"
            " (agent_id, status, started_at, last_activity_at, message_count,"
            "  participants, location, player_id)"
            " VALUES (%s, 'open', %s, %s, 0, %s, %s, %s)",
            (agent_id, ts, ts, json.dumps(participants, ensure_ascii=False),
             location, pid))

    def active_or_start(
        self, agent_id: str, ts: int,
        participants: list[dict[str, Any]] | None = None,
        location: str | None = None, player_id: str | None = None,
        player_name: str | None = None,
    ) -> int:
        with self._lock:
            active = self.active_conversation(agent_id, player_id=player_id)
            if active is not None:
                return int(active["id"])
            return self.start_conversation(
                agent_id, ts, participants=participants, location=location,
                player_id=player_id, player_name=player_name)

    def get(self, conversation_id: int) -> dict[str, Any] | None:
        return self._one(
            f"SELECT * FROM {self._t('conversations')} WHERE id = %s",
            (int(conversation_id),))

    def list_conversations(self, agent_id: str) -> list[dict[str, Any]]:
        return self._query(
            f"SELECT * FROM {self._t('conversations')} WHERE agent_id = %s"
            " ORDER BY id DESC", (agent_id,))

    def close(self, conversation_id: int, summary: str, ts: int) -> None:
        self._write(
            f"UPDATE {self._t('conversations')} SET status = 'closed',"
            " closed_at = %s, summary = %s WHERE id = %s",
            (ts, summary, int(conversation_id)))

    def touch(self, conversation_id: int, ts: int) -> None:
        self._write(
            f"UPDATE {self._t('conversations')} SET last_activity_at = %s WHERE id = %s",
            (ts, int(conversation_id)))

    def idle_open_conversations(self, now: int, timeout: int) -> list[dict[str, Any]]:
        return self._query(
            f"SELECT * FROM {self._t('conversations')} WHERE status = 'open'"
            " AND last_activity_at <= %s", (now - timeout,))

    # ── messages ────────────────────────────────────────────────────────────

    def add_message(self, conversation_id: int, role: str, content: str, ts: int) -> int:
        from anima_world.chat_store import filter_message_content

        content = filter_message_content(self, role, content)
        with self._lock:
            message_id = self._write(
                f"INSERT INTO {self._t('messages')}"
                " (conversation_id, role, content, created_at)"
                " VALUES (%s, %s, %s, %s)",
                (int(conversation_id), role, content, ts))
            self._write(
                f"UPDATE {self._t('conversations')} SET"
                " message_count = message_count + 1, last_activity_at = %s WHERE id = %s",
                (ts, int(conversation_id)))
        return message_id

    def messages_for(self, conversation_id: int) -> list[dict[str, Any]]:
        return self._query(
            f"SELECT role, content, created_at FROM {self._t('messages')}"
            " WHERE conversation_id = %s ORDER BY id", (int(conversation_id),))

    def recent_messages(self, conversation_id: int, n: int) -> list[dict[str, Any]]:
        rows = self._query(
            f"SELECT role, content, created_at FROM {self._t('messages')}"
            " WHERE conversation_id = %s ORDER BY id DESC LIMIT %s",
            (int(conversation_id), int(n)))
        return list(reversed(rows))

    def past_summaries(self, agent_id: str, k: int,
                       player_id: str | None = None) -> list[str]:
        if player_id is None:
            rows = self._query(
                f"SELECT summary FROM {self._t('conversations')} WHERE agent_id = %s"
                " AND status = 'closed' AND summary IS NOT NULL"
                " ORDER BY closed_at DESC, id DESC LIMIT %s", (agent_id, int(k)))
        else:
            rows = self._query(
                f"SELECT summary FROM {self._t('conversations')} WHERE agent_id = %s"
                " AND status = 'closed' AND summary IS NOT NULL"
                " AND COALESCE(player_id, 'user') = %s"
                " ORDER BY closed_at DESC, id DESC LIMIT %s",
                (agent_id, player_id, int(k)))
        return [row["summary"] for row in rows]

    # ── 一轮的观测量 ────────────────────────────────────────────────────────

    def annotate_message(
        self, message_id: int, *, stance: str | None = None, intent: str | None = None,
        intent_confidence: float | None = None, tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        from anima_world.chat_store import annotation_values

        values = annotation_values(stance, intent, intent_confidence, tool_calls)
        if not values:
            return
        sets = ", ".join(f"{col} = %s" for col in values)
        self._write(
            f"UPDATE {self._t('messages')} SET {sets} WHERE id = %s",
            (*values.values(), int(message_id)))

    def annotation_rows(self, conversation_id: int) -> list[tuple]:
        rows = self._query(
            f"SELECT role, stance, intent, tool_calls FROM {self._t('messages')}"
            " WHERE conversation_id = %s ORDER BY id", (int(conversation_id),))
        return [(r["role"], r["stance"], r["intent"], r["tool_calls"]) for r in rows]

    def conversation_meta(self, conversation_id: int) -> dict[str, Any]:
        from anima_world.chat_store import summarize_annotations

        return summarize_annotations(self.annotation_rows(conversation_id))

    def erase_player(self, player_id: str, *, dry_run: bool = False) -> dict[str, int]:
        """删掉这个玩家的全部转录。语义与 `RedisChatStore.erase_player` 逐字相同
        (整场删、观测量随消息走、空 player_id 读作 'user')。"""
        pid = str(player_id or "").strip()
        out = {"conversations": 0, "messages": 0}
        if not pid:
            return out
        with self._lock:
            rows = self._query(
                f"SELECT id FROM {self._t('conversations')}"
                " WHERE COALESCE(player_id, 'user') = %s", (pid,))
            conv_ids = [int(r["id"]) for r in rows]
            out["conversations"] = len(conv_ids)
            if not conv_ids:
                return out
            marks = ",".join(["%s"] * len(conv_ids))
            with self._conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) FROM {self._t('messages')}"
                    f" WHERE conversation_id IN ({marks})", conv_ids)
                out["messages"] = int(cur.fetchone()[0])
                if not dry_run:
                    cur.execute(
                        f"DELETE FROM {self._t('messages')}"
                        f" WHERE conversation_id IN ({marks})", conv_ids)
                    cur.execute(
                        f"DELETE FROM {self._t('conversations')}"
                        f" WHERE id IN ({marks})", conv_ids)
            if not dry_run:
                self._conn.commit()
        return out
