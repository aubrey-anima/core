"""运行时状态住进 Redis:进程不再持有变量。

这一层解的是"很多进程操作同一个世界"里最硬的那个结。世界的真相此前**只有一半在
db 里**:事件、记忆、关系、量都落库,而**黑板**(每个角色 20 个键:她此刻在哪、
在干嘛、饿不饿、打算做什么、行为树这一 tick 选了哪个动作)、时钟、在途集合,全都是
Python 对象。两个进程各自开同一个世界文件,会读到同一份历史,然后**在各自内存里跑出
两个不同的世界**。

把黑板搬进 Redis,那一半就不再是进程私有的了。

## 代价是实打实的,别假装没有

实测(3 个角色):**每 tick 80 次黑板访问,一个世界日 22949 次**。

| | 一个世界日 |
|---|---|
| 进程内 dict | 0.048 秒 |
| Redis,逐个访问 | 1.1 秒(unix socket)~ 11.5 秒(跨机) |
| Redis,每 tick 批量读写一次 | 0.13 ~ 0.39 秒 |

`RedisBlackboard` 是**逐个访问**的老实实现 —— "进程不存变量"就是这个意思。
需要那 24 倍回来的话,`CachedRedisBlackboard` 提供"一 tick 一读一写"的批量版,
代价是**一个 tick 之内它确实在进程内存里**(见那个类自己的说明)。

## 值必须能被别的进程读懂

所以一律 JSON。黑板上放的是 dict / list / float / str(`plan.params`、`mailbox`、
`goals`),不是随便什么对象 —— 存不进 JSON 的东西本来就不该在黑板上,那是进程私有
状态,而这一层的全部目的就是消灭进程私有状态。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from anima_world.world_store import BTStore as _BTStore
from anima_world.world_store import LocationStore as _LocationStore

logger = logging.getLogger(__name__)

# 一个世界在 Redis 里的键前缀。**世界 id 必须进键名** —— 一个 Redis 实例上跑十个
# 世界是常态,而键撞车的后果是两个世界的角色共用一个脑子。
KEY_PREFIX = "anima"


def agent_key(world_id: str, agent_id: str) -> str:
    return f"{KEY_PREFIX}:{world_id}:agent:{agent_id}"


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        # 别人手改过这个键,或者写进去的时候不是 JSON。返回原样比抛异常好:
        # 黑板读不出一个键,行为树会退回兜底;而一次异常会掀翻整个 tick。
        logger.warning("黑板上有一个读不回来的值,原样返回:%r", raw)
        return raw


class RedisBlackboard:
    """一个角色的黑板,住在 Redis 的一个 hash 里。

    接口和进程内那个 `Blackboard` **逐字相同**(`read` / `write`),所以它是直接替换,
    行为树、需求、调度器一行都不用改 —— 它们从来只通过这两个方法碰黑板。
    """

    __slots__ = ("_redis", "_key")

    def __init__(self, redis: Any, key: str) -> None:
        self._redis = redis
        self._key = key

    def read(self, key: str) -> Any:
        return _loads(self._redis.hget(self._key, key))

    def write(self, key: str, value: Any) -> None:
        self._redis.hset(self._key, key, _dumps(value))

    # 便于调试与整体搬运:一次拿全 / 一次写全。
    def snapshot(self) -> dict[str, Any]:
        return {k: _loads(v) for k, v in (self._redis.hgetall(self._key) or {}).items()}

    def restore(self, data: dict[str, Any]) -> None:
        if data:
            self._redis.hset(self._key, mapping={k: _dumps(v) for k, v in data.items()})

    def __repr__(self) -> str:
        return f"RedisBlackboard({self._key!r})"


class CachedRedisBlackboard(RedisBlackboard):
    """一 tick 一读一写的批量版。**代价要说清楚。**

    它在 `begin()` 时把整个 hash 读进来,期间的 `read` / `write` 走内存,`flush()`
    时一次写回。往返从每 tick 80 次降到 2 次(实测 0.13~0.39 秒/世界日,而不是
    1.1~11.5 秒)。

    ⚠️ **代价:一个 tick 之内,状态确实在这个进程的内存里。** 于是两个进程同时跑
    同一个角色的同一个 tick 会互相覆盖,后写的赢,而且无声。用它的前提是**同一时刻
    只有一个进程在推这个世界的时钟**(别的进程提交动词,由那个进程执行)——
    也就是 `docs/AGENT-RUNTIME.md` 里"世界进程是权威"那条。

    如果你要的是"任何进程都能随时改任何状态",用 `RedisBlackboard`,并接受那 24 倍。
    """

    __slots__ = ("_cache", "_dirty", "_open")

    def __init__(self, redis: Any, key: str) -> None:
        super().__init__(redis, key)
        self._cache: dict[str, Any] = {}
        self._dirty: set[str] = set()
        self._open = False

    def begin(self) -> None:
        self._cache = super().snapshot()
        self._dirty.clear()
        self._open = True

    def flush(self) -> None:
        if self._dirty:
            super().restore({k: self._cache.get(k) for k in self._dirty})
        self._dirty.clear()
        self._open = False

    def read(self, key: str) -> Any:
        if self._open:
            return self._cache.get(key)
        return super().read(key)

    def write(self, key: str, value: Any) -> None:
        if self._open:
            self._cache[key] = value
            self._dirty.add(key)
            return
        super().write(key, value)


class ClockStore:
    """时钟住哪儿。默认在进程里 —— 行为和以前逐字相同。"""

    __slots__ = ("_value",)

    def __init__(self, value: int = 0) -> None:
        self._value = int(value)

    def get(self) -> int:
        return self._value

    def set(self, value: int) -> None:
        self._value = int(value)


class RedisClock(ClockStore):
    """时钟住进 Redis:**"现在是第几 tick"只有一个答案**。

    这是多进程里最不能含糊的一个数。两个进程各推各的时钟,世界就分叉了 —— 而分叉
    之后两边都还在正常跑,只是它们不再是同一个世界。这正是这个仓库最怕的那种坏。

    实测每 tick 读 7.3 次、写 1 次(黑板是 80 次),所以逐次访问 Redis 完全可接受,
    不需要缓存 —— 而不缓存意味着**任何一个进程随时读到的都是真的现在**。
    """

    __slots__ = ("_redis", "_key")

    def __init__(self, redis: Any, key: str, initial: int = 0) -> None:
        self._redis = redis
        self._key = key
        # 只在没有值时写入初值:重开一个世界不该把时钟拨回去。
        self._redis.setnx(self._key, int(initial))

    def get(self) -> int:
        raw = self._redis.get(self._key)
        return 0 if raw is None else int(raw)

    def set(self, value: int) -> None:
        self._redis.set(self._key, int(value))


def clock_key(world_id: str) -> str:
    return f"{KEY_PREFIX}:{world_id}:clock"


def lock_key(world_id: str) -> str:
    return f"{KEY_PREFIX}:{world_id}:lock"


# 释放锁必须是"我持有的才删" —— 直接 DEL 会删掉别人刚拿到的那把(我超时了、别人拿走
# 了、我醒来把它删了)。比对 token 再删,这一步必须原子,所以走 Lua。
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisLock:
    """跨进程的世界锁。**它在进程内的 RLock 之外,不是替代它。**

    为什么是两层:调度器那把 `threading.RLock` 还被 `threading.Condition` 用着
    (等规划落地),而 Condition 要的是真线程锁。所以进程内的线程照旧走 RLock,
    跨进程走这一把。

    **可重入**:一个动作里工具会再拿一次锁(`move_agent` 自己就拿),深度计数在
    线程本地,只有最外层才真的去 Redis 拿和放。

    **有超时**(`ttl_ms`):一个进程拿着锁崩了,世界不能就此永远停摆。代价是超时
    之后别人会拿到锁,而原主人若还活着就会写坏 —— 所以 ttl 要显著大于一个动作的
    真实耗时(实测持锁 62 微秒,默认 30 秒有五个数量级的余量)。
    """

    def __init__(self, redis: Any, key: str, *, ttl_ms: int = 30_000,
                 retry_seconds: float = 0.005, wait_seconds: float = 30.0) -> None:
        import threading

        self._redis = redis
        self._key = key
        self._ttl_ms = int(ttl_ms)
        self._retry = float(retry_seconds)
        self._wait = float(wait_seconds)
        self._local = threading.local()

    def _depth(self) -> int:
        return getattr(self._local, "depth", 0)

    def acquire(self, blocking: bool = True) -> bool:
        import time
        import uuid

        if self._depth():                      # 可重入:已经是我的了
            self._local.depth += 1
            return True
        token = uuid.uuid4().hex
        deadline = time.monotonic() + self._wait
        while True:
            if self._redis.set(self._key, token, nx=True, px=self._ttl_ms):
                self._local.token = token
                self._local.depth = 1
                return True
            if not blocking:
                return False
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"等世界锁 {self._key} 超过 {self._wait:.0f} 秒 —— "
                    "多半是有个进程拿着锁挂了,而它的 ttl 还没到"
                )
            time.sleep(self._retry)

    def release(self) -> None:
        depth = self._depth()
        if depth <= 1:
            self._local.depth = 0
            token = getattr(self._local, "token", None)
            if token is not None:
                try:
                    self._redis.eval(_RELEASE, 1, self._key, token)
                except Exception:  # noqa: BLE001 - 释放失败不该掀翻调用方
                    logger.warning("释放世界锁失败,等它自己超时", exc_info=True)
                self._local.token = None
        else:
            self._local.depth = depth - 1

    def __enter__(self) -> "RedisLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


class RedisDict:
    """一个住在 Redis hash 里的 dict —— 只实现真正被用到的那几个操作。

    `_transit`(谁在路上)与 `_current_action`(谁在干嘛)此前是纯内存的 dict。
    它们的后果很具体:另一个进程不知道她**正在赶路**,于是会让她"走开"、让她跟一个
    还没走到的人搭话 —— 而这些判断恰恰是引擎用来把约束变成等待、把等待变成相遇的。

    **不做成通用 MutableMapping**:只实现 `get / pop / items / [] / in / len / bool`,
    因为只有这几个被用到。多实现一个方法就多一处"它看起来像 dict,但在某个边角上
    不是"的机会,而那种错最难查。

    `encode` / `decode` 让值不必是 JSON 原生的(`ActionDescriptor` 就不是)。
    """

    __slots__ = ("_redis", "_key", "_encode", "_decode")

    def __init__(self, redis: Any, key: str, *, encode: Any = None, decode: Any = None) -> None:
        self._redis = redis
        self._key = key
        self._encode = encode or (lambda v: v)
        self._decode = decode or (lambda v: v)

    def __getitem__(self, key: str) -> Any:
        raw = self._redis.hget(self._key, key)
        if raw is None:
            raise KeyError(key)
        return self._decode(_loads(raw))

    def __setitem__(self, key: str, value: Any) -> None:
        self._redis.hset(self._key, key, _dumps(self._encode(value)))

    def __contains__(self, key: str) -> bool:
        return bool(self._redis.hexists(self._key, key))

    def __len__(self) -> int:
        return int(self._redis.hlen(self._key) or 0)

    def __bool__(self) -> bool:
        return len(self) > 0

    def __iter__(self) -> Any:
        return iter(self._redis.hkeys(self._key) or [])

    def get(self, key: str, default: Any = None) -> Any:
        raw = self._redis.hget(self._key, key)
        return default if raw is None else self._decode(_loads(raw))

    def pop(self, key: str, default: Any = None) -> Any:
        raw = self._redis.hget(self._key, key)
        self._redis.hdel(self._key, key)
        return default if raw is None else self._decode(_loads(raw))

    def items(self) -> list[tuple[str, Any]]:
        # 快照一份再返回:调用方会在遍历里改它(`_transit` 就是边走边删),
        # 而对着一个活的 Redis hash 边遍历边删,行为取决于服务端实现。
        return [
            (k, self._decode(_loads(v)))
            for k, v in (self._redis.hgetall(self._key) or {}).items()
        ]

    def __repr__(self) -> str:
        return f"RedisDict({self._key!r}, {len(self)} 项)"


def transit_key(world_id: str) -> str:
    return f"{KEY_PREFIX}:{world_id}:transit"


def current_action_key(world_id: str) -> str:
    return f"{KEY_PREFIX}:{world_id}:doing"


def encode_action(action: Any) -> dict[str, Any]:
    return {"kind": action.kind, "params": dict(action.params)}


def decode_action(raw: Any) -> Any:
    from anima_world.actions import ActionDescriptor

    if raw is None:
        return None
    return ActionDescriptor(str(raw.get("kind")), dict(raw.get("params") or {}))


def plans_key(world_id: str) -> str:
    return f"{KEY_PREFIX}:{world_id}:plans"


def events_key(world_id: str) -> str:
    return f"{KEY_PREFIX}:{world_id}:events"


class RedisEventLog:
    """事件日志住进 Redis。**这是"唯一真相"那张表。**

    接口和 `EventLog` 逐字相同(`append` / `replay` / `count` / `max_seq` / `page`),
    所以它是直接替换 —— 前提是所有读日志的路都走这扇门,而那是先做的一步:
    此前有 10 处直接写 SQL 读 `events` 表,搬完之后它们会读到一张空表**而且不报错**。

    ## 用列表,而不是 Stream

    `seq` 在这个引擎里是**1 起的连续整数**,而且投影、分页、`since_seq` 全都建立在
    "它连续"上。Redis 列表的 `RPUSH` 返回新长度,那正好就是 seq —— 而且 RPUSH 是原子的,
    Redis 又是单线程的,所以**两个进程同时追加,各自拿到唯一且递增的 seq**。
    这一条正是多进程下最不能含糊的东西(`docs/AGENT-RUNTIME.md` §4 的三个问题之二)。

    Stream 的 ID 是 `时间-序号`,换过去就要把 `seq` 的语义一起改,而 `seq` 是跨仓库
    看得见的东西(`events export` 的每一行、`history` 的分页游标)。不值得。

    ## 代价

    `who` / `kind` 过滤在客户端做:列表没有二级索引。一个世界日约 100 条事件,一年
    3.6 万条 —— `LRANGE` 全量再过滤,在这个量级上没问题,但**它不是能一直撑下去的
    形状**。真需要的时候再加按类型的索引集合,别现在就猜。
    """

    __slots__ = ("_redis", "_key")

    def __init__(self, redis: Any, key: str) -> None:
        self._redis = redis
        self._key = key

    def append(self, event: dict) -> Any:
        from anima_world.types import Event

        ts = event["ts"]
        if ts < 0:
            raise ValueError("event ts MUST be non-negative")
        e = Event(
            seq=0, ts=ts, type=event["type"],
            who=event.get("who"), loc=event.get("loc"),
            payload=event.get("payload", {}),
        )
        # RPUSH 返回新长度 = 这一条的 seq。原子,而且 Redis 单线程 ——
        # 两个进程同时追加,各自拿到唯一且递增的号。
        e.seq = int(self._redis.rpush(self._key, _dumps({
            "ts": e.ts, "type": e.type, "who": e.who, "loc": e.loc, "payload": e.payload,
        })))
        return e

    def _rows(self, since_seq: int = 0) -> list[Any]:
        from anima_world.types import Event

        raw = self._redis.lrange(self._key, int(since_seq), -1) or []
        out = []
        for offset, item in enumerate(raw):
            d = _loads(item) or {}
            out.append(Event(
                seq=int(since_seq) + offset + 1,
                ts=int(d.get("ts") or 0), type=str(d.get("type") or ""),
                who=d.get("who"), loc=d.get("loc"), payload=d.get("payload") or {},
            ))
        return out

    def replay(self, since_seq: int = 0) -> list[Any]:
        return self._rows(since_seq)

    def max_seq(self) -> int:
        return int(self._redis.llen(self._key) or 0)

    def count(self, *, who: str | None = None, kind: str | None = None) -> int:
        if who is None and kind is None:
            return self.max_seq()
        return len(self._match(self._rows(0), who, kind))

    def page(
        self, *, since_seq: int = 0, limit: int = 100,
        who: str | None = None, kind: str | None = None,
    ) -> list[Any]:
        return self._match(self._rows(since_seq), who, kind)[: int(limit)]

    @staticmethod
    def _match(rows: list[Any], who: str | None, kind: str | None) -> list[Any]:
        return [
            e for e in rows
            if (who is None or e.who == who) and (kind is None or e.type == kind)
        ]


def stock_key(world_id: str, owner: str) -> str:
    return f"{KEY_PREFIX}:{world_id}:stock:{owner}"


def stock_owners_key(world_id: str) -> str:
    return f"{KEY_PREFIX}:{world_id}:stock_owners"


class RedisStockStore:
    """世界的量住进 Redis。**每个 owner 一个 hash + 一个 owner 索引集合。**

    索引那个集合不是可有可无:`owners(kind)` 与 `snapshot_kind(kind)` 要按前缀选
    (`owner == kind` 或 `owner LIKE kind:%`),而在 Redis 里对应的就是"扫一遍键"。
    `SCAN` 是 O(整个 keyspace) —— 一个 Redis 上跑十个世界的时候,别人的键也得扫。
    所以自己维护一份 owner 名单。

    **性能承诺跟着一起搬。** 这一层文档里写着"一万棵树跑一个世界日 1.4 秒",而那个
    数字是改出来的不是天生的:第一版逐个 owner 查、逐个 commit,2000 棵树就到
    72ms/tick。所以这里同样必须**按类批量取**(pipeline 一次问完)和**整轮一次写回**,
    而不是逐个往返 —— 换了后端把那条承诺丢掉,和没搬一样糟。
    """

    __slots__ = ("_redis", "_world")

    def __init__(self, redis: Any, world_id: str) -> None:
        self._redis = redis
        self._world = world_id

    # ── 读 ──────────────────────────────────────────────────────────────────

    def get(self, owner: str, key: str, default: float = 0.0) -> float:
        raw = self._redis.hget(stock_key(self._world, owner), key)
        return default if raw is None else float((_loads(raw) or [default, 0])[0])

    def of(self, owner: str) -> dict[str, float]:
        return {k: v for k, (v, _t) in self.snapshot(owner).items()}

    def snapshot(self, owner: str) -> dict[str, tuple[float, int]]:
        raw = self._redis.hgetall(stock_key(self._world, owner)) or {}
        out: dict[str, tuple[float, int]] = {}
        for key, item in raw.items():
            pair = _loads(item) or [0.0, 0]
            out[key] = (float(pair[0]), int(pair[1]))
        return out

    def owners(self, kind: str | None = None) -> list[str]:
        names = self._redis.smembers(stock_owners_key(self._world)) or set()
        names = {n.decode() if isinstance(n, bytes) else n for n in names}
        if kind is not None:
            names = {n for n in names if n == kind or n.startswith(f"{kind}:")}
        return sorted(names)

    def snapshot_kind(self, kind: str) -> dict[str, dict[str, tuple[float, int]]]:
        return self.snapshot_many(self.owners(kind))

    def snapshot_many(self, owners: Any) -> dict[str, dict[str, tuple[float, int]]]:
        owners = list(owners)
        if not owners:
            return {}
        # **一次问完**:逐个 owner 一次往返,正是当年 72ms/tick 那个形状。
        pipe = self._redis.pipeline()
        for owner in owners:
            pipe.hgetall(stock_key(self._world, owner))
        out: dict[str, dict[str, tuple[float, int]]] = {}
        for owner, raw in zip(owners, pipe.execute()):
            values: dict[str, tuple[float, int]] = {}
            for key, item in (raw or {}).items():
                key = key.decode() if isinstance(key, bytes) else key
                pair = _loads(item) or [0.0, 0]
                values[key] = (float(pair[0]), int(pair[1]))
            if values:
                out[owner] = values
        return out

    # ── 写 ──────────────────────────────────────────────────────────────────

    def set(self, owner: str, key: str, value: float, tick: int = 0) -> None:
        self.set_many(owner, {key: value}, tick)

    def set_many(self, owner: str, values: dict[str, float], tick: int = 0) -> None:
        if not values:
            return
        pipe = self._redis.pipeline()
        pipe.hset(
            stock_key(self._world, owner),
            mapping={k: _dumps([float(v), int(tick)]) for k, v in values.items()},
        )
        pipe.sadd(stock_owners_key(self._world), owner)
        pipe.execute()

    def write_round(self, pending: dict[str, dict[str, float]], tick: int) -> int:
        """整轮一次写回 —— 逐个 owner 提交是当年 72ms/tick 的另一半原因。"""
        if not pending:
            return 0
        pipe = self._redis.pipeline()
        written = 0
        touched: list[str] = []
        for owner, values in pending.items():
            if not values:
                continue
            pipe.hset(
                stock_key(self._world, owner),
                mapping={k: _dumps([float(v), int(tick)]) for k, v in values.items()},
            )
            touched.append(owner)
            written += len(values)
        # owner 名单**一次加完**:两千棵树两千次 SADD,是白花的两千条命令。
        if touched:
            pipe.sadd(stock_owners_key(self._world), *touched)
        pipe.execute()
        return written

    def delete(self, owner: str, key: str | None = None) -> None:
        if key is None:
            pipe = self._redis.pipeline()
            pipe.delete(stock_key(self._world, owner))
            pipe.srem(stock_owners_key(self._world), owner)
            pipe.execute()
            return
        self._redis.hdel(stock_key(self._world, owner), key)
        # 最后一个键被删掉,这个 owner 就不该再出现在名单里 —— 否则
        # `owners()` 会报出一个空壳,而调用方会以为那儿还有东西。
        if not self._redis.hlen(stock_key(self._world, owner)):
            self._redis.srem(stock_owners_key(self._world), owner)


class RedisRows:
    """一张表的行,住在一个 Redis hash 里。**给纯 CRUD 的 store 当底座。**

    `field` 是主键(拼出来的),值是整行的 JSON。这解决的是样板:剩下的 store 大多
    是"按主键存一行、按主键取一行、列全部",各写一遍 hget/hset/hgetall 只会让
    每个实现各自带一份 JSON 解码的坑。

    **不做成通用 ORM**:带条件的查询(记忆的三因子检索、事件的过滤分页)照旧各写
    各的 —— 那些地方的语义差别正是它们存在的理由,套进一个通用查询层只会把语义磨平。
    """

    __slots__ = ("_redis", "_key")

    def __init__(self, redis: Any, key: str) -> None:
        self._redis = redis
        self._key = key

    def get(self, field: str) -> Any:
        return _loads(self._redis.hget(self._key, field))

    def put(self, field: str, row: Any) -> None:
        self._redis.hset(self._key, field, _dumps(row))

    def drop(self, field: str) -> int:
        return int(self._redis.hdel(self._key, field) or 0)

    def all(self) -> dict[str, Any]:
        raw = self._redis.hgetall(self._key) or {}
        return {
            (k.decode() if isinstance(k, bytes) else k): _loads(v)
            for k, v in raw.items()
        }

    def clear(self) -> None:
        self._redis.delete(self._key)

    def __len__(self) -> int:
        return int(self._redis.hlen(self._key) or 0)


class RedisPromptStore:
    """提示词模板。**改完即生效**这条性质在多进程下反而更重要:

    一个进程改了模板,别的进程下一次拼提示词就该用新的 —— 而不是各自记着自己那份,
    于是同一个世界里两个角色按两套模板说话。
    """

    __slots__ = ("_rows",)

    def __init__(self, redis: Any, world_id: str) -> None:
        self._rows = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:prompts")

    def has(self, name: str) -> bool:
        return self._rows.get(name) is not None

    def get(self, name: str, default: str = "") -> str:
        row = self._rows.get(name)
        return default if row is None else str(row.get("template", default))

    def set(self, name: str, template: str, description: str | None = None) -> None:
        old = self._rows.get(name) or {}
        self._rows.put(name, {
            "name": name, "template": template,
            # 没给过说明就是 None(SQLite 那列可空);给过之后再改模板要**保留**它 ——
            # 这两条我都猜错过一次,互验当场抓到:改模板不该顺手抹掉说明。
            "description": description if description is not None else old.get("description"),
        })

    def list(self) -> list[dict[str, Any]]:
        return sorted(self._rows.all().values(), key=lambda r: str(r.get("name") or ""))


class RedisVisibilityStore:
    """可见性声明 + 量在哪儿。**没声明 = 感知不到**,那条默认值必须原样保住。"""

    __slots__ = ("_rules", "_places")

    def __init__(self, redis: Any, world_id: str) -> None:
        self._rules = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:visibility")
        self._places = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:stock_places")

    def declare(self, owner_kind: str, key: str, visibility: str,
                label: str | None = None) -> None:
        from anima_world.perception import VISIBILITIES

        if visibility not in VISIBILITIES:
            raise ValueError(
                f"不认识的可见档 {visibility!r};只有 {sorted(VISIBILITIES)}"
            )
        self._rules.put(f"{owner_kind}\x00{key}", {
            # 字段名照 SQLite 版:`visibility`。种子里那个字段才叫 `visible` ——
            # 两边差一个字,而互验之前谁都发现不了。
            "kind": owner_kind, "key": key, "visibility": visibility, "label": label,
        })

    def declarations(self) -> list[dict[str, Any]]:
        return sorted(
            self._rules.all().values(),
            key=lambda r: (str(r.get("kind") or ""), str(r.get("key") or "")),
        )

    def rules_map(self) -> dict[tuple[str, str], str]:
        return {
            (str(r["kind"]), str(r["key"])): str(r["visibility"])
            for r in self._rules.all().values()
        }

    def place(self, owner: str, location: str, label: str | None = None) -> None:
        self._places.put(owner, {"owner": owner, "location": location, "label": label})

    def at(self, location: str) -> dict[str, str | None]:
        return {
            str(r["owner"]): r.get("label")
            for r in self._places.all().values()
            if r.get("location") == location
        }

    def place_of(self, owner: str) -> str | None:
        row = self._places.get(owner)
        return None if row is None else row.get("location")

    def labels(self) -> dict[str, str | None]:
        return {str(r["owner"]): r.get("label") for r in self._places.all().values()}


class RedisMemoryStore:
    """她记得什么。**行在一个 hash 里,按角色建索引集合。**

    检索的打分(三因子:时近×重要×相关)本来就在 Python 里做 —— SQL 只负责把某个
    角色的行取出来。所以这一层不用重写检索,只要把"取出来"换掉,`score()` 原样复用。

    **次序必须确定。** SQLite 版特意写了 `ORDER BY tick DESC, id DESC`,理由写在那儿:
    创世注入的记忆全是 `tick=0`,光靠 tick 分不出先后;而不确定的次序意味着**同一个
    世界在两台机器上召回不同的记忆,而且不报错**。这里照抄那个排序键。
    """

    __slots__ = ("_redis", "_world", "_rows", "_config")

    def __init__(self, redis: Any, world_id: str, config_store: Any = None) -> None:
        self._redis = redis
        self._world = world_id
        self._rows = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:memories")
        self._config = config_store

    def _next_id(self) -> int:
        return int(self._redis.incr(f"{KEY_PREFIX}:{self._world}:memories:id"))

    def add(self, agent_id: str, tick: int, kind: str, summary: str,
            importance: float = 0.5, anchor: bool = False,
            event_seq: int | None = None, created_at: int | None = None,
            source_ids: list[int] | None = None) -> int:
        memory_id = self._next_id()
        self._rows.put(str(memory_id), {
            "id": memory_id, "agent_id": agent_id, "tick": int(tick), "kind": kind,
            "summary": summary, "importance": float(importance), "anchor": int(bool(anchor)),
            "event_seq": event_seq, "created_at": int(tick if created_at is None else created_at),
            "source_ids": list(source_ids or []), "strength": 1.0,
            "last_access": None, "access_count": 0,
        })
        return memory_id

    def query(self, agent_id: str, kind: str | None = None,
              min_importance: float | None = None) -> list[dict[str, Any]]:
        rows = [
            r for r in self._rows.all().values()
            if r.get("agent_id") == agent_id
            and (kind is None or r.get("kind") == kind)
            and (min_importance is None or float(r.get("importance") or 0) >= min_importance)
        ]
        # 和 SQLite 版同一个排序键 —— 见类的说明:不确定的次序是一次静默的分叉。
        rows.sort(key=lambda r: (int(r.get("tick") or 0), int(r.get("id") or 0)), reverse=True)
        return rows

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
            # 检索就是复习 —— 加固是设计的一部分,不是副作用。
            for m in top:
                row = self._rows.get(str(m["id"])) or {}
                row["strength"] = min(float(row.get("strength") or 1.0) + 0.3, 3.0)
                row["last_access"] = int(now_tick)
                row["access_count"] = int(row.get("access_count") or 0) + 1
                self._rows.put(str(m["id"]), row)
        return top

    def decay_pass(self, agent_id: str, now_tick: int, ticks_per_day: int = 288) -> None:
        from anima_world.memory_retrieval import decayed_strength

        for row in self.query(agent_id=agent_id):
            last = row.get("last_access")
            last = int(row.get("tick") or 0) if last is None else int(last)
            fresh = decayed_strength(
                float(row.get("strength") or 1.0), last, int(now_tick), int(ticks_per_day),
            )
            if fresh != row.get("strength"):
                row["strength"] = fresh
                self._rows.put(str(row["id"]), row)

    def set_anchor(self, memory_id: int, anchor: bool) -> None:
        row = self._rows.get(str(memory_id))
        if row is not None:
            row["anchor"] = int(bool(anchor))
            self._rows.put(str(memory_id), row)

    def anchors(self, agent_id: str) -> list[dict[str, Any]]:
        return [r for r in self.query(agent_id) if int(r.get("anchor") or 0)]


class RedisLocationStore(_LocationStore):
    """地图。**只换存储,算出来的东西继承复用。**

    `tree()` / `absolute_xy()` / `distance()` 已经只依赖 `all()` —— 它们是从行算出来
    的,不是存出来的。所以这一层继承 `LocationStore` 并只覆盖三个真正碰库的方法:
    `all` / `get` / `upsert`。

    这不是省事,是**不许有第二份几何**:地图的父子链、相对坐标折算、距离公式再写一遍,
    迟早两个后端算出不同的路程,而两边都跑得动。
    """

    def __init__(self, redis: Any, world_id: str) -> None:
        # **有意不调父类 `__init__`**:那一支要一个 sqlite 连接,而这一层根本没有。
        # 继承在这里只为了拿到那三个"从行算出来"的方法,不是为了拿它的状态。
        # 但父类那些 seed_* 会用 `self._lock`,所以这个得有 —— 它只保护本进程内的
        # 线程;跨进程那把在 `World` 上(`RedisLock`)。
        import threading

        self._lock = threading.RLock()
        self._rows_store = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:locations")

    def all(self) -> list[dict[str, Any]]:
        return sorted(self._rows_store.all().values(), key=lambda r: str(r.get("id") or ""))

    def get(self, loc_id: str) -> dict[str, Any] | None:
        return self._rows_store.get(loc_id)

    def upsert(self, loc_id: str, **fields: Any) -> None:
        row = self._rows_store.get(loc_id) or {
            "id": loc_id, "name": loc_id, "description": "", "kind": "point",
            "parent": None, "x": None, "y": None, "w": None, "h": None,
        }
        row.update({k: v for k, v in fields.items()})
        row["id"] = loc_id
        # SQLite 版的行带 `updated_at`,少一列就是"行的形状不一样" —— 而调用方
        # 拿到的是整行 dict,少一个键会在某条路上变成 KeyError 或静默的 None。
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._rows_store.put(loc_id, row)

    def seed_defaults(self, entries: list[dict[str, Any]]) -> None:
        """创世播地图。**已有地图就不动** —— 地图是运行数据,不许被今天的种子覆盖。

        条目是**扁平列表 + `parent` 字段**,不是嵌套的 `children`(我第一版照想象
        写了递归,互验当场抓到:只播出了顶层那一个)。父先于子的排序用 SQLite 版
        那份 `_parents_first` —— 拓扑排序也不该有第二份。
        """
        from anima_world.world_store import _LOCATION_FIELDS, _parents_first

        if self.all():
            return
        for entry in _parents_first(entries):
            self.upsert(
                str(entry["id"]),
                **{f: entry[f] for f in _LOCATION_FIELDS if f in entry},
            )


class RedisBTStore(_BTStore):
    """行为树与动作绑定表。**只覆盖四个碰库的原语,别的继承。**

    `action_table()` / `build_tree()` / `seed_*` / `duty_windows()` 都建在
    `actions` / `set_action` / `add_node` / `_tree_rows` 之上 —— 把它们重写一遍,
    就等于让"这棵树怎么组装"有两份实现,而组装错的树不会崩,只会让她一整天站着不动。

    为什么冷数据也要搬:**只要还有一张表留在 SQLite,你就仍然需要那个文件** ——
    而这一整件事的目的正是让世界不再是一个文件。完整性比冷热重要。
    """

    def __init__(self, redis: Any, world_id: str) -> None:
        # 有意不调父类 __init__(那一支要 sqlite 连接)。父类的 seed_* 会用
        # `self._lock`,所以补一把 —— 它只保护本进程内的线程,跨进程那把在 `World` 上。
        import threading

        self._lock = threading.RLock()
        self._actions = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:bt_actions")
        self._nodes = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:bt_nodes")

    def actions(self) -> list[dict[str, Any]]:
        return sorted(
            self._actions.all().values(), key=lambda r: str(r.get("node_id") or "")
        )

    def set_action(self, node_id: str, kind: str, params: dict[str, Any] | None = None) -> None:
        self._actions.put(node_id, {
            "node_id": node_id, "kind": kind, "params": dict(params or {}),
        })

    def add_node(self, tree: str, node_id: str, node_type: str, parent: str | None,
                 sort: int = 0, params: dict[str, Any] | None = None) -> None:
        self._nodes.put(f"{tree}\x00{node_id}", {
            "tree": tree, "node_id": node_id, "type": node_type,
            "parent": parent, "sort": int(sort), "params": dict(params or {}),
        })

    def _tree_rows(self, tree: str) -> list[dict[str, Any]]:
        rows = [r for r in self._nodes.all().values() if r.get("tree") == tree]
        # 和 SQLite 版一样按 sort 排。次序决定 Selector 的优先级 —— 排错了她会
        # 先做该后做的事,而且不报错。
        rows.sort(key=lambda r: int(r.get("sort") or 0))
        return [
            {"node_id": r["node_id"], "type": r["type"], "parent": r.get("parent"),
             "sort": int(r.get("sort") or 0), "params": dict(r.get("params") or {})}
            for r in rows
        ]
