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

from anima_world.chat_state import ChatStateStore as _ChatStateStore
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
        """整份写回。**覆盖** —— 这是 flush 的语义,不是搬家的语义。"""
        if data:
            self._redis.hset(self._key, mapping={k: _dumps(v) for k, v in data.items()})

    def seed_missing(self, data: dict[str, Any]) -> int:
        """只填 Redis 里还没有的键。返回填了几个。

        **搬家用这个,不是 `restore`。** 第一个进程把创世值搬进来,后来的进程
        接上一个已经在跑的世界 —— 它手里那份是从 SQLite 读出来的**创世快照**,
        写回去等于把她按回原点。

        这个坏法是真的:第二个 `World.open` 会把她的位置悄悄倒带回 cafe,而
        第一个进程眼里她在 home。**两边都不报错,两边都还在跑。** 时钟那边用
        `setnx` 早就做对了(注释写着"重开一个世界不该把时钟拨回去"),黑板是同一个
        道理,只是当时没连起来。

        逐键 `HSETNX` 而不是"整份看空不空":一个新版本引擎加了新的黑板键时,老
        世界里没有它,该补上;而已经有的一个都不许动。
        """
        filled = 0
        for key, value in (data or {}).items():
            if self._redis.hsetnx(self._key, key, _dumps(value)):
                filled += 1
        return filled

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


def engagements_key(world_id: str) -> str:
    """在做的长过程。**真状态,必须住 Redis** —— 内存态的话每次重启都流产一次,
    而一件事要花多久由作者决定,重启多少次由运维决定,两件事不该有关系。"""
    return f"{KEY_PREFIX}:{world_id}:engaged"


def plans_key(world_id: str) -> str:
    return f"{KEY_PREFIX}:{world_id}:plans"


def encode_plan(plan: Any) -> dict[str, Any]:
    """一天的自由时间规划 → JSON。

    ⚠️ **`RedisDict` 不给 `encode` 就是原样交给 redis-py**,而 redis-py 对一个
    它不认得的对象只会 `str()` —— 于是 `plans` 里存的是 `repr(Plan(...))`,
    读回来是一个字符串。下场是 `state()` 在 `plan.steps` 上
    `AttributeError: 'str' object has no attribute 'steps'`,**而且只在她真的
    有计划的那一刻才炸**:一个刚创世的世界永远碰不到,一个跑了两周的世界一开机
    就 500。`_current_action` 早就有这一对,`_plans` 漏了。
    """
    return {
        "agent_id": plan.agent_id,
        "day": int(plan.day),
        "steps": [step.to_dict() for step in plan.steps],
    }


def decode_plan(raw: Any) -> Any:
    from anima_world.planner import Plan, PlanStep

    if raw is None:
        return None
    # 老世界里可能躺着 repr 字符串(这个 bug 存在期间写下的)。解不出来就当没有 ——
    # 一个读不懂的计划不该让整个 `state()` 塌掉,而她下一轮会重新规划。
    if not isinstance(raw, dict):
        return None
    steps = tuple(
        PlanStep(
            start_min=int(s.get("start_min") or 0),
            kind=str(s.get("kind") or ""),
            params=dict(s.get("params") or {}),
            note=str(s.get("note") or ""),
        )
        for s in (raw.get("steps") or [])
    )
    return Plan(agent_id=str(raw.get("agent_id") or ""), day=int(raw.get("day") or 0), steps=steps)


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

    def rewrite(self, seq: int, event: dict) -> None:
        """原地改写一条已有事件(LSET,**seq 不变**)。只给法务抹除用。

        这个后端的 `seq` 是列表下标 + 1(见类的说明),所以抹除**不许删行** ——
        删一行,它后面每一条的 seq 都错一位,投影、分页、`since_seq` 全部跟着错,
        而且不报错。改写保形状:同一个位置、同一个 seq、同一套字段。

        越界(seq 不存在)让 Redis 自己报错 —— 调用方改写的本来就是它刚读出来的行。
        """
        idx = int(seq) - 1
        if idx < 0:
            raise ValueError("seq 从 1 起")
        self._redis.lset(self._key, idx, _dumps({
            "ts": event.get("ts"), "type": event.get("type"),
            "who": event.get("who"), "loc": event.get("loc"),
            "payload": event.get("payload", {}),
        }))

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
        from anima_world.prompt_store import _DEFAULTS

        return self._rows.get(name) is not None or name in _DEFAULTS

    def get(self, name: str, default: str = "") -> str:
        from anima_world.prompt_store import resolve

        row = self._rows.get(name)
        return resolve(name, None if row is None else str(row.get("template", "")), default)

    def set(self, name: str, template: str, description: str | None = None) -> None:
        from anima_world.prompt_store import check_renders

        # 坏模板当场拒绝(PromptRenderError)—— SQLite 版就在这儿挡;不挡的话
        # 它落进世界,渲染时才炸,而渲染在聊天路径上。
        check_renders(name, template)
        old = self._rows.get(name) or {}
        self._rows.put(name, {
            "name": name, "template": template,
            # 没给过说明就是 None(SQLite 那列可空);给过之后再改模板要**保留**它 ——
            # 这两条我都猜错过一次,互验当场抓到:改模板不该顺手抹掉说明。
            "description": description if description is not None else old.get("description"),
        })

    def list(self) -> list[dict[str, Any]]:
        from anima_world.prompt_store import merged_listing

        return merged_listing(self._rows.all())


class RedisVisibilityStore:
    """可见性声明 + 量在哪儿。**没声明 = 感知不到**,那条默认值必须原样保住。"""

    __slots__ = ("_rules", "_places")

    def __init__(self, redis: Any, world_id: str) -> None:
        self._rules = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:visibility")
        self._places = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:stock_places")

    def declare(self, owner_kind: str, key: str, visibility: str,
                label: str | None = None, bands: Any = None) -> None:
        from anima_world.perception import VISIBILITIES, band_errors

        if visibility not in VISIBILITIES:
            raise ValueError(
                f"不认识的可见档 {visibility!r};只有 {sorted(VISIBILITIES)}"
            )
        # 分档写错**一个字都不写**:留一份坏声明在库里,下场是她一直报数字而
        # 日志干净。加载期那道闸(`visibility_band_errors`)在文件那一侧,这里
        # 守的是同一条判断的另一个入口(`World.declare_visibility`)——
        # 判断只有一份,不会两边给出不同答案。
        # 空(`None` / `[]` / `()`)在这一层就是"没分档" —— 调用方交过来的是
        # 规范化之后的值,而"作者写了一个空数组"那种错在作者层那道闸上判
        # (`visibility_band_errors`),这里判等于把一个正常的 no-op 变成异常。
        problems = band_errors(bands, label=f"{owner_kind}.{key}") if bands else []
        if problems:
            raise ValueError("bands 声明有问题:\n" + "\n".join(f"- {p}" for p in problems))
        row: dict[str, Any] = {
            # 字段名照 SQLite 版:`visibility`。种子里那个字段才叫 `visible` ——
            # 两边差一个字,而互验之前谁都发现不了。
            "kind": owner_kind, "key": key, "visibility": visibility, "label": label,
        }
        # 没分档就**没有这个字段**,和"没声明可见性 = 没有行"同一条:留一个
        # `"bands": null` 只会让"这个量分没分档"多出一种写法。
        if bands:
            row["bands"] = [[float(t), str(w)] for t, w in bands]
        self._rules.put(f"{owner_kind}\x00{key}", row)

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

    def labels_map(self) -> dict[tuple[str, str], str]:
        """量的**人话名字**。`(种类, 量名) → label`,没写 label 的量不在表里。

        ⚠️ 和 `labels()` 是两张表:那个是**东西**的名字(老橡树),这个是**量**的
        名字(树高)。同一行提示词里各占一个位置,合成一张表就会互相盖。
        """
        return {
            (str(r["kind"]), str(r["key"])): str(r["label"])
            for r in self._rules.all().values()
            if r.get("label")
        }

    def bands_map(self) -> dict[tuple[str, str], list[list[Any]]]:
        """分档过的量的档表。**只收声明过 `bands` 的行** —— 一个量分没分档,
        问的就是它在不在这张表里。"""
        return {
            (str(r["kind"]), str(r["key"])): r["bands"]
            for r in self._rules.all().values()
            if r.get("bands")
        }

    def place(self, owner: str, location: str, label: str | None = None) -> None:
        # 挪位置不丢名字:label 没给就沿用旧的(SQLite 版 COALESCE 的对应物)。
        if label is None:
            row = self._places.get(owner)
            if row is not None:
                label = row.get("label")
        self._places.put(owner, {"owner": owner, "location": location, "label": label})

    def unplace(self, owner: str) -> None:
        """这个东西不在任何地方了(被抹掉时)。**留着行比删掉坏** —— 一个已经
        不存在的东西还挂在某个地点上,`at()` 会一直把它报进那儿的在场名单,
        于是她的提示词里有一样她走过去也摸不到的东西。"""
        self._places.drop(owner)

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

    __slots__ = ("_redis", "_world", "_rows", "_config", "_explicit_capacity")

    def __init__(self, redis: Any, world_id: str, config_store: Any = None,
                 capacity: int | None = None) -> None:
        self._redis = redis
        self._world = world_id
        self._rows = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:memories")
        self._config = config_store
        # 显式 capacity 归测试;生产从 config(`memory.capacity`)现读。
        self._explicit_capacity = capacity

    def _next_id(self) -> int:
        return int(self._redis.incr(f"{KEY_PREFIX}:{self._world}:memories:id"))

    def count(self) -> int:
        return len(self._rows)

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
            provenance: str = "experienced") -> int:
        memory_id = self._next_id()
        self._rows.put(str(memory_id), {
            "id": memory_id, "agent_id": agent_id, "tick": int(tick), "kind": kind,
            "summary": summary, "importance": float(importance), "anchor": int(bool(anchor)),
            "event_seq": event_seq, "created_at": int(tick if created_at is None else created_at),
            "source_ids": list(source_ids or []), "strength": 1.0,
            "last_access": None, "access_count": 0,
            # R3 记忆分型。**老行读出来没有这一格**,所以读的地方一律
            # `row.get("provenance") or "experienced"` —— 默认值写在读的那一侧,
            # 才不用为了一个新字段去改写一整张历史表。
            "provenance": str(provenance or "experienced"),
        })
        self._evict_if_over_capacity(agent_id)
        return memory_id

    def _capacity(self) -> int:
        if self._explicit_capacity is not None:
            return int(self._explicit_capacity)
        if self._config is not None:
            return int(self._config.get("memory.capacity", default=50))
        return 50

    def _evict_if_over_capacity(self, agent_id: str) -> None:
        """memory-2.0:淘汰**最弱**的,不是最旧的 —— 常被想起的旧记忆活过被无视的
        新记忆。锚定的永远不走。"""
        rows = [r for r in self._rows.all().values() if r.get("agent_id") == agent_id]
        overflow = len(rows) - self._capacity()
        if overflow <= 0:
            return
        evictable = sorted(
            (r for r in rows if not r.get("anchor")),
            key=lambda r: (float(r.get("strength") or 1.0), int(r["id"])),
        )
        for row in evictable[:overflow]:
            self._rows.drop(str(row["id"]))

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
            if row.get("anchor"):
                continue   # 锚定的不衰减 —— 创世记忆是她是谁的一部分
            last = row.get("last_access")
            last = int(row.get("tick") or 0) if last is None else int(last)
            fresh = decayed_strength(
                float(row.get("strength") or 1.0), last, int(now_tick), int(ticks_per_day),
            )
            if fresh != row.get("strength"):
                row["strength"] = fresh
                self._rows.put(str(row["id"]), row)

    def forget_memory(self, memory_id: int) -> bool:
        """真的删掉一条记忆行。返回它本来在不在。

        **和"遗忘"不是一回事**:遗忘(`decay_pass`)只是把强度调低,那条记忆还在,
        被想起一次就会重新变清晰 —— 那是这个引擎对遗忘的定义,不该改。这一条是
        夜间固化(R5)清扫用的:强度已经低到永远召不回来的行,留着只是占容量,
        而容量是**别的记忆的位置**。锚定的永不清,判断在调用方。
        """
        return bool(self._rows.drop(str(int(memory_id))))

    def retick(self, memory_id: int, tick: int, created_at: int | None = None) -> bool:
        """把一条记忆的 `tick` 改掉。**只给迁移用**(`World.repair_memory_ticks`)。

        记忆是派生数据,而 `tick` 是它唯一的排序键;2.0 之前 `conversation` 事件
        盖的是墙钟,那批行的 tick 是 1.78e9,于是 `query` 的前 k 条永远是它们。
        改的是一份能从事件日志重折出来的投影,不是历史本身 —— 事件日志一个字
        不动(见 `World.repair_memory_ticks` 的说明)。
        """
        row = self._rows.get(str(int(memory_id)))
        if row is None:
            return False
        row["tick"] = int(tick)
        row["created_at"] = int(tick if created_at is None else created_at)
        self._rows.put(str(int(memory_id)), row)
        return True

    def set_anchor(self, memory_id: int, anchor: bool) -> None:
        row = self._rows.get(str(memory_id))
        if row is not None:
            row["anchor"] = int(bool(anchor))
            self._rows.put(str(memory_id), row)

    def anchors(self, agent_id: str) -> list[dict[str, Any]]:
        return [r for r in self.query(agent_id) if int(r.get("anchor") or 0)]

    # ── 法务抹除(`World.erase_player`)────────────────────────────────────

    def erase_for_event_seqs(self, seqs: set[int], *, dry_run: bool = False) -> int:
        """删掉**由这些事件而起**的记忆行(`event_seq` ∈ seqs),返回删了几行。

        判据是出处不是文本:一条从他的对话里长出来的记忆整条都是他的交互数据,
        哪怕摘要里一个名字都没提。文本层面的旁及(别人的反思里提了他一句)走
        `redact_summaries` —— 那种只改名字不删行,删了等于把别人的记忆也抹掉一角。
        """
        if not seqs:
            return 0
        wanted = {int(s) for s in seqs}
        hit = [
            r for r in self._rows.all().values()
            if r.get("event_seq") is not None and int(r["event_seq"]) in wanted
        ]
        if not dry_run:
            for row in hit:
                self._rows.drop(str(row["id"]))
        return len(hit)

    def redact_summaries(self, replacements: dict[str, str], *,
                         dry_run: bool = False) -> int:
        """把摘要里出现的这些名字换掉,返回改了几行。只改文本,行还是那一行。"""
        if not replacements:
            return 0
        changed = 0
        for field, row in self._rows.all().items():
            summary = str(row.get("summary") or "")
            fresh = summary
            for old, new in replacements.items():
                fresh = fresh.replace(old, new)
            if fresh != summary:
                changed += 1
                if not dry_run:
                    row["summary"] = fresh
                    self._rows.put(field, row)
        return changed


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

    def seed_defaults(self, entries: list[dict[str, Any]], *,
                      merge: bool = False) -> list[str]:
        """创世播地图。**已有地图就不动** —— 地图是运行数据,不许被今天的种子覆盖。

        条目是**扁平列表 + `parent` 字段**,不是嵌套的 `children`(我第一版照想象
        写了递归,互验当场抓到:只播出了顶层那一个)。父先于子的排序用 SQLite 版
        那份 `_parents_first` —— 拓扑排序也不该有第二份。

        `merge=True` 见基类那份 docstring:粒度从整张表降到每个地点,**默认必须是
        False**。返回真的写下去的那些 id(调用方要拿它发 `location_join`,并且
        汇总时只报真的新增了多少)。
        """
        from anima_world.world_store import _LOCATION_FIELDS, _parents_first

        have = {str(row.get("id")) for row in self.all()}
        if have and not merge:
            return []
        written: list[str] = []
        for entry in _parents_first(entries):
            loc_id = str(entry["id"])
            if merge and loc_id in have:
                continue          # 只增不改:这个地点已经有了,它的描述归这个世界自己
            self.upsert(loc_id, **{f: entry[f] for f in _LOCATION_FIELDS if f in entry})
            written.append(loc_id)
        return written


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

    def set_action(
        self,
        node_id: str,
        kind: str,
        params: dict[str, Any] | None = None,
        *,
        tree: str | None = None,
    ) -> None:
        # 共享绑定的字段名保持**裸 node_id** —— 老引擎写下的行一个字都不用动,
        # 读出来 `tree` 缺席 = 共享 = 和从前逐位相同的行为。
        self._actions.put(node_id if tree is None else f"{tree}\x00{node_id}", {
            "node_id": node_id, "kind": kind, "params": dict(params or {}),
            "tree": tree,
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


class RedisChatStateStore(_ChatStateStore):
    """她和某个人之间的状态:意图 / 静音 / 拒谈 / 回头找你 / 玩家教的规则。

    五张表五个 hash。**转录不在这五张里** —— `annotate_message` 与
    `conversation_meta` 碰的是 `messages` 表,SQL 住在 `ChatStore` 那一族,这一层
    只经 `transcript` 转发。

    那个 `transcript` 参数不是可选的装饰:不传的话基类会去找 `self._conn`,而这里
    根本没有 SQLite 连接 —— **开了 `chat.stance.enabled` 的 Redis 世界会当场
    AttributeError**。这个洞真的存在过一版(我上一轮说"等转录搬走时再一起处理",
    而"以后再说"在这里等于留了一条死路)。

    时间语义原样保住,两条都不能想当然:

    - **静音用墙钟,不用 tick。** 玩家那一侧的"五分钟"是真的五分钟,而世界时钟可能
      是演示速度(1 tick/秒)。用 tick 会让"五分钟别理我"在演示速度下变成不到一分钟。
    - **回头找你用 tick。** 那是世界内部的约定(到第几 tick 回来敲门),和墙钟无关。

    过期的顺手清掉:`quiet_until` / `refused_topics` 读的时候会删掉过期行,和 SQLite
    版一样 —— 这不是优化,是"读到的就是此刻还成立的"。
    """

    def __init__(self, redis: Any, world_id: str, transcript: Any | None = None) -> None:
        import threading

        self._lock = threading.RLock()
        self._transcript = transcript
        self._stances = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:stance")
        self._quiet = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:mutes")
        self._topics = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:refused_topics")
        self._followups = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:followups")
        self._overrides = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:overrides")
        self._redis = redis
        self._world = world_id

    # ── 关系性意图 ──────────────────────────────────────────────────────────

    def stance(self, agent_id: str, target_id: str) -> dict[str, Any] | None:
        row = self._stances.get(f"{agent_id}\x00{target_id}")
        if row is None:
            return None
        return {
            "stance": row["stance"], "declared": bool(row["declared"]),
            "updated_tick": int(row.get("updated_tick") or 0),
            "updated_at": int(row.get("updated_at") or 0),
        }

    def set_stance(self, agent_id: str, target_id: str, stance: str, *,
                   declared: bool = True, tick: int = 0) -> None:
        from anima_world import stance as stance_mod

        if not stance_mod.is_stance(stance):
            raise ValueError(f"unknown stance {stance!r}")
        self._stances.put(f"{agent_id}\x00{target_id}", {
            "agent_id": agent_id, "target": target_id, "stance": stance,
            "declared": 1 if declared else 0, "updated_tick": int(tick),
            "updated_at": self._now(),
        })

    def stances(self, agent_id: str) -> list[dict[str, Any]]:
        rows = [r for r in self._stances.all().values() if r.get("agent_id") == agent_id]
        rows.sort(key=lambda r: str(r.get("target") or ""))
        return [
            {"target": r["target"], "stance": r["stance"], "declared": bool(r["declared"]),
             "updated_tick": int(r.get("updated_tick") or 0),
             "updated_at": int(r.get("updated_at") or 0)}
            for r in rows
        ]

    # ── 静音 / 等会儿再说 ───────────────────────────────────────────────────

    def set_quiet(self, agent_id: str, player_id: str, *, kind: str = "mute",
                  minutes: float, reason: str | None = None,
                  now: int | None = None) -> int:
        if kind not in ("mute", "delay"):
            raise ValueError(f"unknown quiet kind {kind!r}")
        base = self._now() if now is None else int(now)
        expires = base + int(max(0.0, float(minutes)) * 60)
        self._quiet.put(f"{agent_id}\x00{player_id}\x00{kind}", {
            "agent_id": agent_id, "player_id": player_id, "kind": kind,
            "expires_at": expires, "reason": reason, "created_at": base,
        })
        return expires

    def _sweep_quiet(self, moment: int) -> list[dict[str, Any]]:
        alive = []
        for field, row in self._quiet.all().items():
            if int(row.get("expires_at") or 0) <= moment:
                self._quiet.drop(field)      # 过期的顺手清掉 —— 和 SQLite 版一样
            else:
                alive.append(row)
        return alive

    def quiet_until(self, agent_id: str, player_id: str, *,
                    now: int | None = None) -> dict[str, Any] | None:
        moment = self._now() if now is None else int(now)
        mine = [
            r for r in self._sweep_quiet(moment)
            if r.get("agent_id") == agent_id and r.get("player_id") == player_id
        ]
        if not mine:
            return None
        row = max(mine, key=lambda r: int(r["expires_at"]))   # 最晚的那条
        return {
            "kind": row["kind"], "expires_at": int(row["expires_at"]),
            "reason": row.get("reason"),
            "seconds_left": max(0, int(row["expires_at"]) - moment),
        }

    def clear_quiet(self, agent_id: str, player_id: str, *, kind: str | None = None) -> None:
        for field, row in self._quiet.all().items():
            if row.get("agent_id") != agent_id or row.get("player_id") != player_id:
                continue
            if kind is None or row.get("kind") == kind:
                self._quiet.drop(field)

    def mutes(self, agent_id: str | None = None, *,
              now: int | None = None) -> list[dict[str, Any]]:
        moment = self._now() if now is None else int(now)
        rows = [
            r for r in self._quiet.all().values()
            if int(r.get("expires_at") or 0) > moment
            and (agent_id is None or r.get("agent_id") == agent_id)
        ]
        rows.sort(key=lambda r: int(r["expires_at"]))
        return [
            {"agent_id": r["agent_id"], "player_id": r["player_id"], "kind": r["kind"],
             "expires_at": int(r["expires_at"]), "reason": r.get("reason"),
             "seconds_left": max(0, int(r["expires_at"]) - moment)}
            for r in rows
        ]

    # ── 拒谈的话题 ──────────────────────────────────────────────────────────

    def refuse_topic(self, agent_id: str, keyword: str, *,
                     minutes: float | None = None, now: int | None = None) -> None:
        keyword = (keyword or "").strip()
        if not keyword:
            raise ValueError("keyword is required")
        base = self._now() if now is None else int(now)
        expires = None if minutes is None else base + int(max(0.0, float(minutes)) * 60)
        self._topics.put(f"{agent_id}\x00{keyword}", {
            "agent_id": agent_id, "keyword": keyword,
            "expires_at": expires, "created_at": base,
        })

    def refused_topics(self, agent_id: str, *,
                       now: int | None = None) -> list[dict[str, Any]]:
        moment = self._now() if now is None else int(now)
        rows = []
        for field, row in self._topics.all().items():
            expires = row.get("expires_at")
            if expires is not None and int(expires) <= moment:
                self._topics.drop(field)
                continue
            if row.get("agent_id") == agent_id:
                rows.append(row)
        rows.sort(key=lambda r: str(r.get("keyword") or ""))
        return [
            {"keyword": r["keyword"],
             "expires_at": None if r.get("expires_at") is None else int(r["expires_at"])}
            for r in rows
        ]

    # ── 回头找你 ────────────────────────────────────────────────────────────

    def add_followup(self, agent_id: str, player_id: str, *, due_tick: int,
                     kind: str = "delayed_reply", reason: str | None = None,
                     created_tick: int = 0) -> int:
        followup_id = int(self._redis.incr(f"{KEY_PREFIX}:{self._world}:followups:id"))
        self._followups.put(str(followup_id), {
            "id": followup_id, "agent_id": agent_id, "player_id": player_id,
            "due_tick": int(due_tick), "kind": kind, "reason": reason,
            "created_tick": int(created_tick), "fired_tick": None,
        })
        return followup_id

    @staticmethod
    def _followup_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(row["id"]), "agent_id": row["agent_id"],
            "player_id": row["player_id"], "due_tick": int(row["due_tick"]),
            "kind": row["kind"], "reason": row.get("reason"),
        }

    def due_followups(self, tick: int) -> list[dict[str, Any]]:
        rows = [
            r for r in self._followups.all().values()
            if r.get("fired_tick") is None and int(r.get("due_tick") or 0) <= int(tick)
        ]
        rows.sort(key=lambda r: (int(r["due_tick"]), int(r["id"])))
        return [self._followup_view(r) for r in rows]

    def mark_followup_fired(self, followup_id: int, tick: int) -> None:
        row = self._followups.get(str(followup_id))
        if row is not None:
            row["fired_tick"] = int(tick)
            self._followups.put(str(followup_id), row)

    def pending_followups(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        rows = [
            r for r in self._followups.all().values()
            if r.get("fired_tick") is None
            and (agent_id is None or r.get("agent_id") == agent_id)
        ]
        rows.sort(key=lambda r: (int(r["due_tick"]), int(r["id"])))
        return [self._followup_view(r) for r in rows]

    # ── 玩家教过的规则 ──────────────────────────────────────────────────────

    def set_override(self, agent_id: str, player_id: str, kind: str, value: str) -> None:
        from anima_world.chat_state import OVERRIDE_KINDS

        if kind not in OVERRIDE_KINDS:
            raise ValueError(f"unknown override kind {kind!r}")
        value = (value or "").strip()
        if not value:
            raise ValueError("override value is required")
        self._overrides.put(f"{agent_id}\x00{player_id}\x00{kind}", {
            "agent_id": agent_id, "player_id": player_id, "kind": kind,
            "value": value, "updated_at": self._now(),
        })

    def overrides(self, agent_id: str, player_id: str) -> list[dict[str, Any]]:
        rows = [
            r for r in self._overrides.all().values()
            if r.get("agent_id") == agent_id and r.get("player_id") == player_id
        ]
        rows.sort(key=lambda r: str(r.get("kind") or ""))
        return [
            {"kind": r["kind"], "value": r["value"],
             "updated_at": int(r.get("updated_at") or 0)}
            for r in rows
        ]

    def clear_override(self, agent_id: str, player_id: str, kind: str) -> bool:
        return bool(self._overrides.drop(f"{agent_id}\x00{player_id}\x00{kind}"))

    # ── 一个人离开了这个世界 ────────────────────────────────────────────────

    def forget_player(self, player_id: str) -> int:
        """把这几张表里关于这个玩家的**朝前看**的行清掉,返回清了几行。

        stance / mutes / followups / overrides 四张按 (角色, 玩家) 存的都要走 ——
        `refused_topics` 是按 (角色, 关键词) 存的,和玩家无关,不动。

        **按行里的字段判,不按主键前缀判**:主键是 `\\x00` 拼出来的,而玩家 id
        里出现过裸 UUID —— 按字符串前缀切会在某个 id 恰好是另一个的前缀时静默
        多删一行,而多删的那一行属于另一个还活着的人。
        """
        player_id = str(player_id or "").strip()
        if not player_id:
            return 0
        dropped = 0
        with self._lock:
            for rows, field_name in (
                (self._stances, "target"),
                (self._quiet, "player_id"),
                (self._followups, "player_id"),
                (self._overrides, "player_id"),
            ):
                for field, row in rows.all().items():
                    if isinstance(row, dict) and row.get(field_name) == player_id:
                        dropped += rows.drop(field)
        return dropped


class RedisKnowledgeGraph:
    """关系图的边。`(subject, predicate, object)` 唯一 —— **重复写不覆盖**
    (SQLite 那边是 `INSERT OR IGNORE`),所以第一次记下的 `source_event_seq`
    与 `created_at` 说了算:一条边的出处是它第一次被证实的那一刻。

    这条**是设计**,不是缺陷:边记的是"这两人是朋友"这个**事实**,不是
    "他们又亲近了一次"这个**事件**。关系在「亲近」「挚交」之间来回跨档时事实
    没变,重写 `source_event_seq` 只会让"这条边是哪件事立起来的"每次都换个答案。
    值本身的变化在 relations 那一层(它是可变的),边不是它的第二份拷贝。

    可撤销**才是**缺陷,而且是被上面那条挡住看不见的那个:见 `drop`。"""

    __slots__ = ("_rows", "_redis", "_world")

    def __init__(self, redis: Any, world_id: str) -> None:
        self._rows = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:edges")
        self._redis = redis
        self._world = world_id

    def add(self, subject: str, predicate: str, object: str,  # noqa: A002 - 对齐既有签名
            source_event_seq: int | None = None, created_at: int = 0) -> None:
        """立一条边。已经**有效**就一动不动(INSERT OR IGNORE:先到的那条说了算);
        曾经作废过的**复活**(R3:时间有效期)。

        复活这一支是有意的:两个人绝交之后又和好,那不该是"一条永远死着的边加一条
        新边",而是同一条事实**又成立了**。复活时清掉 `invalid_at` 并把
        `valid_from` 挪到今天 —— 于是"从什么时候起又是朋友"这个问题答得出来。
        """
        field = f"{subject}\x00{predicate}\x00{object}"
        row = self._rows.get(field)
        if row is not None and row.get("invalid_at") is None:
            return                      # 还有效:先到的那条说了算
        if row is not None:             # 作废过的复活,id 保留(它是同一件事)
            row["invalid_at"] = None
            row["valid_from"] = int(created_at)
            row["source_event_seq"] = source_event_seq
            self._rows.put(field, row)
            return
        self._rows.put(field, {
            "id": int(self._redis.incr(f"{KEY_PREFIX}:{self._world}:edges:id")),
            "subject": subject, "predicate": predicate, "object": object,
            "source_event_seq": source_event_seq, "created_at": int(created_at),
            # R3 时间有效期(Graphiti 那一路):一条事实**什么时候起成立、什么时候
            # 不再成立**。`invalid_at is None` = 此刻有效。
            "valid_from": int(created_at), "invalid_at": None,
        })

    def drop(self, subject: str, predicate: str, object: str,  # noqa: A002 - 对齐 add
             *, at: int | None = None, hard: bool = False) -> bool:
        """撤销一条边。返回它本来在不在(**已经作废的返回 False**,所以幂等)。

        存在的理由:边只增不减的话,一对从「亲近」跌进「交恶」的人身上会**同时**
        挂着 friendship 和 rivalry,而且永远。`cliques.compute_cliques` 只看
        friendship 的连通分量,于是那个小团体里坐着两个此刻互相看不顺眼的人 ——
        算得出来、画得出来、一条日志都不会报错。

        **R3 起这是"作废"而不是"删行"**:写下 `invalid_at`,`query()` 从此看不见它,
        而 `query(as_of=…)` 仍然答得出"那时候他俩是朋友吗"。理由和事件不删行是同一条 ——
        一段关系结束了是**又一件发生过的事**,不是"从来没有过"。

        `hard=True` 才真删行,只给法务抹除用(`forget_player` / `erase_player`:
        那不是"关系变了",是这个人不该在世界里留下痕迹)。
        """
        field = f"{subject}\x00{predicate}\x00{object}"
        row = self._rows.get(field)
        if row is None:
            return False
        if hard:
            return bool(self._rows.drop(field))
        if row.get("invalid_at") is not None:
            return False                # 已经作废过:幂等
        row["invalid_at"] = int(at if at is not None else 0)
        self._rows.put(field, row)
        return True

    def query(self, subject: str | None = None, predicate: str | None = None,
              object: str | None = None, *,  # noqa: A002
              as_of: int | None = None,
              include_invalid: bool = False) -> list[dict[str, Any]]:
        """**默认只给此刻有效的边。**

        这是承重的:`compute_cliques` / `World.graph()` / 关系那一路读的都是它,
        默认带上作废的边等于把"他们曾经是朋友"当成"他们是朋友"。

        `as_of=tick` 问的是**那时候**(`valid_from <= tick` 且还没作废);
        `include_invalid=True` 要的是整段历史(含已经作废的),给审计用。
        """
        rows = []
        for r in self._rows.all().values():
            if subject is not None and r["subject"] != subject:
                continue
            if predicate is not None and r["predicate"] != predicate:
                continue
            if object is not None and r["object"] != object:
                continue
            if include_invalid:
                rows.append(r)
                continue
            invalid_at = r.get("invalid_at")
            if as_of is None:
                if invalid_at is None:
                    rows.append(r)
                continue
            # 那一刻:已经立起来了,而且还没作废(或作废在那之后)。
            if int(r.get("valid_from") or r.get("created_at") or 0) > int(as_of):
                continue
            if invalid_at is not None and int(invalid_at) <= int(as_of):
                continue
            rows.append(r)
        rows.sort(key=lambda r: int(r.get("id") or 0))
        return rows

    def query_by_event(self, event_seq: int) -> list[dict[str, Any]]:
        rows = [
            r for r in self._rows.all().values()
            if r.get("source_event_seq") == event_seq
        ]
        rows.sort(key=lambda r: int(r.get("id") or 0))
        return rows


class RedisNeedsStore:
    """她的身体。**结算曲线不重写** —— 那是世界的规则,只换存储。"""

    __slots__ = ("_rows",)

    def __init__(self, redis: Any, world_id: str) -> None:
        self._rows = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:needs")

    def load(self, agent_id: str) -> dict[str, float]:
        from anima_world.needs import NEEDS

        row = self._rows.get(agent_id) or {}
        values = {k: float(v) for k, v in row.items() if k in NEEDS}
        for need in NEEDS:
            values.setdefault(need, 1.0)      # 没记过就是满的,和 SQLite 版一样
        return values

    def persist(self, agent_id: str, values: dict[str, Any], tick: int) -> None:
        from anima_world.needs import NEEDS

        row = self._rows.get(agent_id) or {}
        row.update({need: float(values.get(need, 1.0)) for need in NEEDS})
        row["updated_tick"] = int(tick)
        self._rows.put(agent_id, row)


class RedisCliqueStore:
    """小团体。派生缓存,写是**全量重写** —— 重算即真相。"""

    __slots__ = ("_redis", "_key")

    def __init__(self, redis: Any, world_id: str) -> None:
        self._redis = redis
        self._key = f"{KEY_PREFIX}:{world_id}:cliques"

    def store(self, groups: list[list[str]], tick: int) -> None:
        pipe = self._redis.pipeline()
        pipe.delete(self._key)
        for index, members in enumerate(groups, start=1):
            pipe.rpush(self._key, _dumps({
                "id": index, "member_ids": list(members), "computed_tick": int(tick),
            }))
        pipe.execute()

    def load(self) -> list[dict[str, Any]]:
        return [_loads(x) for x in (self._redis.lrange(self._key, 0, -1) or [])]


class RedisReflectionStore:
    """反思水位。当前值,不是历史 —— 反思本身是 `memory_seed` 事件,重放能重建。"""

    __slots__ = ("_rows",)

    def __init__(self, redis: Any, world_id: str) -> None:
        self._rows = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:reflection")

    def get(self, agent_id: str) -> float:
        row = self._rows.get(agent_id) or {}
        return float(row.get("accumulated") or 0.0)

    def set(self, agent_id: str, value: float) -> None:
        row = self._rows.get(agent_id) or {}
        row["accumulated"] = float(value)
        self._rows.put(agent_id, row)

    def add(self, agent_id: str, amount: float) -> float:
        fresh = self.get(agent_id) + float(amount)
        self.set(agent_id, fresh)
        return fresh

    def reset(self, agent_id: str, tick: int) -> None:
        self._rows.put(agent_id, {"accumulated": 0.0, "last_reflection_tick": int(tick)})


class RedisContactStore:
    """她想起某个玩家这件事的当前值:上次是什么时候、今天几次了、他上次出现在
    她面前是哪一 tick(contact)。

    **一行是一对 (她, 他)**,主键 `agent\\x00player`,和 `stance` / `mutes` 同一个
    形状。

    ⚠️ **它必须落库,不能是进程内的字典。** 冷却是内存态的话,每次重启就把所有
    人的冷却一起清零 —— 而重启在这个部署形态下是家常便饭(换引擎镜像)。玩家看到
    的样子是"发一次版,四个角色同时来找我",而日志里一条错都没有。这正是 `:engaged`
    那一条("真状态,不是缓存")的同一个坑。

    `last_contact_tick` 是**她**这边看到的"他上次出现" —— 走 `World.chat` /
    `record_chat_turn` 那扇门写。用世界时钟而不是墙钟:"很久没出现"里的"久"
    是世界里的久,一个跑在演示速度上的世界一小时就过完一天。
    """

    __slots__ = ("_rows",)

    def __init__(self, redis: Any, world_id: str) -> None:
        self._rows = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:contact")

    @staticmethod
    def _field(agent_id: str, player_id: str) -> str:
        return f"{agent_id}\x00{player_id}"

    def get(self, agent_id: str, player_id: str) -> dict[str, Any]:
        row = self._rows.get(self._field(agent_id, player_id))
        if not isinstance(row, dict):
            return {"agent_id": agent_id, "player_id": player_id}
        return dict(row)

    def all(self) -> list[dict[str, Any]]:
        return [row for row in self._rows.all().values() if isinstance(row, dict)]

    def note_contact(
        self, agent_id: str, player_id: str, tick: int, name: str = ""
    ) -> None:
        """他刚跟她说过话。**改的是这一两个字段,不是整行** —— 整行写回会把冷却
        状态抹掉,而那正是"只填缺,不覆盖"这条纪律在这一层的样子。

        顺手记下他叫什么:`World.players` 是刻意的内存态(重启即新访),而这一层
        要在**他不在线的时候**发一条带着他名字的事件 —— 不记下来的话,重启之后
        她想起的是一串 uuid。
        """
        row = self.get(agent_id, player_id)
        row["last_contact_tick"] = int(tick)
        if name:
            row["player_name"] = str(name)
        self._rows.put(self._field(agent_id, player_id), row)

    def note_fired(self, agent_id: str, player_id: str, *, tick: int, day: int) -> int:
        """记一次"她想起他了"。返回今天第几次(含这次)。

        跨日自动归零:存的是 `fired_day` 而不是"今天的计数" —— 一个只存计数的
        实现要靠别人在日切时来清它,而漏掉日切清理的样子是她从第二天起再也想不
        起你,永远。
        """
        row = self.get(agent_id, player_id)
        count = int(row.get("fired_today") or 0) if int(row.get("fired_day") or -1) == int(day) else 0
        count += 1
        row.update({"last_fired_tick": int(tick), "fired_day": int(day), "fired_today": count})
        self._rows.put(self._field(agent_id, player_id), row)
        return count

    def fired_today(self, agent_id: str, player_id: str, day: int) -> int:
        row = self.get(agent_id, player_id)
        if int(row.get("fired_day") or -1) != int(day):
            return 0
        return int(row.get("fired_today") or 0)

    def forget_player(self, player_id: str) -> int:
        """他走了 —— 把每个角色对他的联系态清掉,返回清了几行。

        留着的下场是这一层永远替一个不存在的人占着名额:线上那个世界里,6 个早就
        注销的试玩账号在这张表里活着,而她每天能想起的人数是有上限的。
        **按行里的 `player_id` 判**,理由同 `RedisChatStateStore.forget_player`。
        """
        player_id = str(player_id or "").strip()
        if not player_id:
            return 0
        dropped = 0
        for field, row in self._rows.all().items():
            if isinstance(row, dict) and row.get("player_id") == player_id:
                dropped += self._rows.drop(field)
        return dropped


class RedisEconomyStore:
    """货架与物品。**价格漂移曲线不重写** —— 那是世界的规则,不是存储。

    `daily_price_pass` 的算法留在 `economy.drift_price` 里,这里只负责把货架读出来、
    算完写回去。两份漂移曲线的后果是两个后端的物价慢慢分家,而两边都跑得动。
    """

    __slots__ = ("_items", "_shelves")

    def __init__(self, redis: Any, world_id: str) -> None:
        self._items = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:item_defs")
        self._shelves = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:shop_stock")

    @staticmethod
    def _shelf_field(location_id: str, item_id: str) -> str:
        return f"{location_id}\x00{item_id}"

    def put_item(self, item_id: str, name: str, kind: str,
                 base_price: float, restores: Any = None) -> None:
        self._items.put(item_id, {
            "id": item_id, "name": name, "kind": kind,
            "base_price": float(base_price), "restores": restores,
        })

    def put_shelf(self, location_id: str, item_id: str,
                  quantity: int, price: float) -> None:
        self._shelves.put(self._shelf_field(location_id, item_id), {
            "location_id": location_id, "item_id": item_id,
            "quantity": int(quantity), "price": float(price),
        })

    def restores_of(self, item_id: str) -> dict[str, float]:
        row = self._items.get(item_id) or {}
        restores = row.get("restores")
        if isinstance(restores, str):
            import json

            try:
                restores = json.loads(restores)
            except (TypeError, ValueError):
                restores = {}
        return dict(restores or {})

    def name_of(self, item_id: str) -> str:
        """这样东西的人话名字;没有定义就回空字符串,让调用方自己决定退回什么。

        回 `item_id` 而不是空的话,调用方分不出"它叫这个名字"和"查不到" ——
        而"引用即存在"那条路上,一样东西的名字**本来就是**它的 id。
        """
        return str((self._items.get(item_id) or {}).get("name") or "")

    def items(self) -> list[dict[str, Any]]:
        return sorted(self._items.all().values(), key=lambda r: str(r.get("id") or ""))

    def shelves(self) -> list[dict[str, Any]]:
        return sorted(
            self._shelves.all().values(),
            key=lambda r: (str(r.get("location_id") or ""), str(r.get("item_id") or "")),
        )

    def cheapest_meal(self, location_id: str) -> dict[str, Any] | None:
        by_id = {str(r["id"]): r for r in self._items.all().values()}
        candidates = [
            {"item_id": r["item_id"], "price": float(r["price"]),
             "quantity": int(r["quantity"]), "name": by_id.get(str(r["item_id"]), {}).get("name")}
            for r in self._shelves.all().values()
            if r.get("location_id") == location_id and int(r.get("quantity") or 0) > 0
            and by_id.get(str(r["item_id"]), {}).get("kind") == "consumable"
            # 一样什么也补不回来的东西不是饭。`kind == "consumable"` 单独一个判据太宽:
            # 一包肥料、一管颜料也是用一次就没的东西,而 `eat` 会挑最便宜的那个 ——
            # 于是她会把肥料当午饭吃掉,而且吃得很饱(需求照样归零)。
            and self.restores_of(str(r["item_id"]))
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda c: (c["price"], str(c["item_id"])))

    def take_stock(self, location_id: str, item_id: str, qty: int = 1) -> bool:
        field = self._shelf_field(location_id, item_id)
        row = self._shelves.get(field)
        if row is None or int(row.get("quantity") or 0) < int(qty):
            return False
        row["quantity"] = int(row["quantity"]) - int(qty)
        self._shelves.put(field, row)
        return True

    def daily_price_pass(self, sales: dict[tuple[str, str], int]) -> None:
        """日切:**补货 + 漂价**,不是只漂价。

        我第一版只写了漂价,而且 `restocked` 传了 0、价格没四舍五入 —— 三处都错。
        互验当场抓到:两个后端的货架数量和价格全都对不上。这正是"照直觉写"的代价,
        而 `daily_price_pass` 这个名字本身也在误导(它其实是"日切")。
        """
        from anima_world.economy import MAX_STOCK, RESTOCK_PER_DAY, drift_price

        by_id = {str(r["id"]): r for r in self._items.all().values()}
        for field, row in self._shelves.all().items():
            base = float(by_id.get(str(row["item_id"]), {}).get("base_price") or row["price"])
            sold = int(sales.get((row["location_id"], row["item_id"]), 0))
            row["quantity"] = min(MAX_STOCK, int(row["quantity"]) + RESTOCK_PER_DAY)
            row["price"] = round(
                drift_price(base, float(row["price"]), sold, RESTOCK_PER_DAY), 2
            )
            self._shelves.put(field, row)

    def seed_authored(
        self,
        items: list[tuple[str, str, str, float, dict[str, Any]]],
        stock: list[tuple[str, str, int, float | None]],
        *,
        merge: bool = False,
    ) -> list[str]:
        """种子里写下的物品定义与店铺货架,种进空 store(#12)。

        与 `seed_defaults` 同一条规矩:**空的才种**。作者数据先落地,内置演示
        物品随后看到非空 store 就整体让位。`price` 为 None 时取 base_price。

        `merge=True`(作者指名的世界文件 + 已有世界)把粒度降到**每件物品 / 每格
        货架**:已有的定义与货架一个字都不动 —— 货架上的数量是这个世界跑出来的
        现在,按今天的文件写回去就是把卖掉的东西变回来。返回新增的物品 id。
        """
        rows = self._items.all()
        base_prices = {
            str(r["id"]): float(r.get("base_price") or 0.0) for r in rows.values()
        }
        added: list[str] = []
        if not rows or merge:
            for item_id, name, kind, base_price, restores in items:
                if merge and item_id in base_prices:
                    continue
                self.put_item(item_id, name, kind, base_price, restores)
                base_prices[item_id] = float(base_price)
                added.append(item_id)
        shelves = set(self._shelves.all())
        if not shelves or merge:
            for location_id, item_id, quantity, price in stock:
                if merge and self._shelf_field(location_id, item_id) in shelves:
                    continue
                resolved = price if price is not None else base_prices.get(item_id, 0.0)
                self.put_shelf(location_id, item_id, quantity, float(resolved))
        return added

    def seed_defaults(self) -> None:
        """创世播默认货架。**已有货架就不动** —— 和别的 seed_* 同一条规矩。"""
        if self._items.all():
            return
        from anima_world.economy import DEFAULT_ITEMS, DEFAULT_STOCK

        prices: dict[str, float] = {}
        for item_id, name, kind, base_price, restores in DEFAULT_ITEMS:
            self.put_item(item_id, name, kind, base_price, restores)
            prices[item_id] = float(base_price)
        for location_id, item_id, quantity in DEFAULT_STOCK:
            if item_id in prices:      # 货架上的东西必须先是个物品
                self.put_shelf(location_id, item_id, quantity, prices[item_id])


def durability_warning(redis: Any) -> str | None:
    """这个 Redis 会不会在重启后把世界忘掉。**不会忘就返回 None。**

    ## 为什么必须有这一条

    Redis 主要活在内存里,持久化(RDB 快照 / AOF)是**配置选项**,而默认的
    `redis.conf` 里 AOF 是关的。世界搬进 Redis 之后,"世界还在不在"这件事就取决于
    一个引擎管不着、也从来没问过的配置。

    后果实测过,而且比"数据丢了"更坏:

        持久化关着:跑完一天 104 条事件 → Redis 重启后 50 条
        AOF 打开:  跑完一天 103 条事件 → 重启后 110 条

    50 是创世那批 —— 重开时又从 SQLite 复制了一遍。所以世界**没有报错**,它只是
    悄悄年轻了一天,然后接着跑。这正是这个仓库最怕的那一类:照跑,但给错东西。

    ## 探不到就沉默

    `CONFIG GET` 可能被禁用(托管 Redis 常见),也可能这个客户端根本不支持
    (fakeredis 就不支持)。**探不到不等于没配好** —— 因为探测失败就报警,等于
    训练人忽略这条警告。
    """
    try:
        save = (redis.config_get("save") or {}).get("save", "")
        appendonly = (redis.config_get("appendonly") or {}).get("appendonly", "")
    except Exception:  # noqa: BLE001 - 探不到就沉默,见 docstring
        return None
    if str(appendonly).lower() == "yes":
        return None
    if str(save).strip():
        # 有 RDB 存盘点:会丢最后一个快照之后的部分,但不会整个忘掉。
        return (
            f"这个 Redis 只有 RDB 快照(save = {save!r}),没开 AOF —— "
            "崩溃时会丢掉最后一次快照之后的世界。要一条不丢就开 "
            "`appendonly yes`(配 `appendfsync always` 更严)。"
        )
    return (
        "**这个 Redis 不会把世界存下来**(save 是空的、appendonly 是 no)—— "
        "它一重启,这个世界就退回创世那一刻,而且不会报错,只会接着跑。\n"
        "      开 `appendonly yes`,或者至少给它一组 `save` 存盘点。"
    )


# ── 1.5.0(去 SQLite):世界文件退役后,这几样也要有 Redis 的家 ──────────────


def meta_rows(redis: Any, world_id: str) -> "RedisRows":
    """世界的元数据行(`:meta`):创世出生证明(world_seed)、占用标记(owner_pid/host)。

    world.db 时代它们住 `db_meta`;格式联锁(format_version)不再需要 —— 键的
    形状就是格式,而键前缀里带着 world_id。
    """
    return RedisRows(redis, f"{KEY_PREFIX}:{world_id}:meta")


class RedisRulesStore:
    """世界的规律(`world_rules`)的家:`:world_rules` 一个 hash,field=规律 id。

    此前规律只在开机时从 SQLite 读进进程 —— SQLite 退役后没有这个家的话,
    "只有 Redis 连接的进程"看到的世界就没有物理法则。定义存原文(JSON),
    编译(parse_rules)仍在读取侧:坏定义要在**读的时候**当场报错,不流到运行期。
    """

    __slots__ = ("_rows",)

    def __init__(self, redis: Any, world_id: str) -> None:
        self._rows = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:world_rules")

    def __len__(self) -> int:
        return len(self._rows)

    def seed(self, entries: list, stamp: str, *, merge: bool = False) -> list[str]:
        """空的时候播一次;之后这里的行说了算 —— 和地图/行为树同一条契约。

        `merge=True` 把粒度降到**每一条规律**(作者指名了世界文件那条路),而且
        **同名的照文件里那条重写**:规律是这个世界的物理法则,是法不是状态,它身上
        没有任何东西会随时间漂。从前同名的一个字都不动,于是一条漂了的规律(常数步
        长不看 `dt`)在一个跑着的世界里永远改不掉 —— 只能连玩家的进度一起抹掉重建。

        **默认必须是 False** —— 内置橱窗每次开机都在手上,让它逐条合并,一个写了
        `kinds` 却没写 `rules` 的世界重开一次就会被塞进橱窗那条引用 `tree` 的生长
        规律,而这个世界里没有 tree:下场不是算错,是这个世界从此打不开。

        返回真的写下去的那些 id。**没变的不写**:每次开机都重写会把 `updated_at`
        刷成开机时间,于是"这条规律上次改是什么时候"永远读不出来。
        """
        have = self._rows.all()
        if have and not merge:
            return []
        written: list[str] = []
        for entry in entries:
            rule_id = str(entry.get("id"))
            if merge and (have.get(rule_id) or {}).get("definition") == entry:
                continue
            self._rows.put(rule_id, {"id": rule_id, "definition": entry, "updated_at": stamp})
            written.append(rule_id)
        return written

    def definitions(self) -> list[dict]:
        rows = self._rows.all()
        return [rows[k]["definition"] for k in sorted(rows)]


class RedisOntologyStore:
    """世界的本体:`:kinds`(种类)与 `:entities`(实例)**两个 hash,物理分开**。

    分成两张表是这一层唯一的结构性决定,理由在 `ontology.py` 的开头:一张表里
    "种类"和"实例"只能靠一个字段区分,而靠字段区分就是靠作者自觉 —— Wikidata
    那条路走完的账单是 8400 万条 split-order pair。分成两张表让它**不可表达**。

    **种类只在创世时播,之后冻结;实例可以运行期长出来。** 这一刀不是任意的:
    规律是按种类校验的(`resolve`),运行期新增一个种类等于让"这条规律合不合法"
    随时间变化,重放就不再确定。而种一棵树只是多一个 owner —— 规律早就写好了。

    定义存原文,编译在读取侧(`load`)—— 和 `RedisRulesStore` 同一条:坏声明要在
    **读的时候**当场报错,不流到运行期。
    """

    __slots__ = ("_kinds", "_entities", "_redis", "_rev_key", "_seq_key")

    def __init__(self, redis: Any, world_id: str) -> None:
        self._kinds = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:kinds")
        self._entities = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:entities")
        self._redis = redis
        # 实例表改过几次。**别用行数当版本** —— 一生一灭净变化是 0,而那正是
        # 世界跑起来之后最常见的一对。别的进程于是永远看不到这次更替。
        self._rev_key = f"{KEY_PREFIX}:{world_id}:entities_rev"
        self._seq_key = f"{KEY_PREFIX}:{world_id}:entities_seq"

    def revision(self) -> int:
        """实例表的版本号。别的进程拿它判断"我手里这份还新不新"。"""
        raw = self._redis.get(self._rev_key)
        return int(raw) if raw else 0

    def mint_id(self, kind: str) -> str:
        """给一个新生的东西起个 id:`kind:序号`。

        **计数器住 Redis,不住进程**:两个进程各数各的,迟早撞出同一个 id,而撞上的
        后果是后来的那个**覆盖**前一个 —— 世界里少一样东西,没有任何地方报错。
        序号只增不减(抹掉一个不会把号让出来),因为 id 会进事件、进提示词、进
        `.cyberworld`,复用一个死者的号等于让历史指向另一样东西。
        """
        return f"{kind}:{int(self._redis.incr(self._seq_key))}"

    def __len__(self) -> int:
        return len(self._kinds)

    def seed(self, kinds: list, entities: list, stamp: str, *,
             merge: bool = False, replace_kinds: bool = False
             ) -> tuple[list[str], list[str]]:
        """空的时候播一次 —— 和规律/地图/行为树同一条契约:只填缺,不覆盖。

        `merge=True` 把粒度降到**每个种类 / 每个实例**(作者指名了世界文件那条路):
        **"种类是冻的"那一条没有被放开** —— 它说的是 *世界跑起来之后*不许长出新种类
        (那会让"这条规律合不合法"随时间变化,重放就不再确定)。而这是**开机前**作者
        的一次明示编辑:整份本体在这次开机里重新解析一遍(`_precheck_ontology` 按合并
        后的**全集**验、`load` 再验一次),所以这一次开机之内它仍然是一个确定的闭集。

        `replace_kinds=True` 让**同名的种类也照文件里那份重写**。理由在
        `_seed_ontology` 的 docstring 里:种类是法不是状态,它身上没有会随时间漂的
        东西,而"同名的一个字都不动"意味着一个跑着的世界永远改不了自己的声明 ——
        改一个写错的 `label` 得连玩家的进度一起抹掉重建。实例照旧只增(它有位置,
        而位置另有一份镜像在可见性表里)。

        返回 `(真的写下去的种类 id, 新增的实例 id)` —— 种类那一半含"改了的",
        调用方要拿它去把可见性镜像跟着改。**没变的不写**:每次开机都重写一遍会
        把 `updated_at` 刷成开机时间,于是"这条声明上次改是什么时候"永远读不出来。
        实例变了要挪版本号 —— 别的进程靠 `revision()` 判断"我手里这份还新不新"。
        """
        if not merge and (len(self._kinds) or len(self._entities)):
            return [], []
        have_kinds = self._kinds.all()
        have_entities = set(self._entities.all())
        new_kinds: list[str] = []
        new_entities: list[str] = []
        for entry in kinds or []:
            kind_id = str(entry.get("id"))
            if merge and kind_id in have_kinds:
                if not replace_kinds:
                    continue
                if (have_kinds[kind_id] or {}).get("definition") == entry:
                    continue
            self._kinds.put(kind_id, {"definition": entry, "updated_at": stamp})
            new_kinds.append(kind_id)
        for entry in entities or []:
            entity_id = str(entry.get("id"))
            if merge and entity_id in have_entities:
                continue
            self._entities.put(entity_id, {"definition": entry, "updated_at": stamp})
            new_entities.append(entity_id)
        if merge and new_entities:
            self._redis.incr(self._rev_key)
        return new_kinds, new_entities

    def kind_definitions(self) -> list[dict]:
        rows = self._kinds.all()
        return [rows[k]["definition"] for k in sorted(rows)]

    def entity_definitions(self) -> list[dict]:
        rows = self._entities.all()
        return [rows[k]["definition"] for k in sorted(rows)]

    def load(self, *, rules: Any = (), locations: Any = (), items: Any = ()) -> Any:
        """读 + 编译 + 解析全部引用。坏了当场 `OntologyError`。"""
        from anima_world.ontology import parse_entities, parse_kinds, resolve

        kinds = parse_kinds(self.kind_definitions())
        entities = parse_entities(self.entity_definitions(), kinds)
        return resolve(kinds, entities, rules=rules, locations=locations, items=items)

    def add_entity(self, entry: dict, stamp: str) -> Any:
        """运行期种一棵树。**种类照样要解析得到** —— 新实例不是绕过本体的后门。"""
        from anima_world.ontology import parse_entities, parse_kinds

        kinds = parse_kinds(self.kind_definitions())
        entity = parse_entities([entry], kinds)[str(entry.get("id")).strip()]
        self._entities.put(entity.id, {"definition": entry, "updated_at": stamp})
        self._redis.incr(self._rev_key)
        return entity

    def drop_entity(self, entity_id: str) -> int:
        dropped = self._entities.drop(entity_id)
        if dropped:
            self._redis.incr(self._rev_key)
        return dropped

    def parse_entities_now(self, kinds: Any) -> dict:
        """重读实例表并编译。**只重读实例,不重读种类** —— 种类是冻的,重跑一遍
        `parse_kinds` 只是把同一份声明再编译一次,而世界每生一样东西就付一次。"""
        from anima_world.ontology import parse_entities

        return parse_entities(self.entity_definitions(), kinds)


class RedisConfigBackend:
    """`ConfigStore` 的 Redis 底座:`:config` 一个 hash,field=键名,值=整行。

    行的形状和 world.db 时代的 `config` 表逐列相同(value/value_type/category/
    is_secret/description/updated_at),而且**只存作者动过的** —— 判据照旧是
    "引擎声明过什么",不是"这儿有没有行"(那套逻辑全在 ConfigStore,这里纯存取)。
    """

    __slots__ = ("_rows",)

    def __init__(self, redis: Any, world_id: str) -> None:
        self._rows = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:config")

    def all(self) -> dict[str, dict]:
        return self._rows.all()

    def put(self, key: str, row: dict) -> None:
        self._rows.put(key, row)


class RedisChatStore:
    """转录(conversations / messages)的 Redis 家 —— 没给 `mysql=` 的世界用它。

    转录随时间无限增长,判据上它属于 MySQL;但 events / memories 在"只有 Redis"
    的世界里也接受了同一笔账 —— 转录没理由是那个例外,否则没有 MySQL 的世界
    聊天记录无处可放。给了 `mysql=` 的世界照旧走 `MySQLChatStore`,这里让位。

    形状:
        :conversations       hash  field=会话 id,值=整行(participants 是列表)
        :conversations:id    INCR  会话 id
        :messages            hash  field=消息 id,值=整行(含逐轮观测量)
        :messages:id         INCR  消息 id
        :conv_msgs:{conv_id} list  这场会话的消息 id,按序

    语义逐条对照老 ChatStore:player_id 缺省读作 'user'(COALESCE 的对应物)、
    recent_messages 返回时间正序、past_summaries 最近的在前。
    """

    # `content_filter`:落库前的最后一道(`chat_store.filter_message_content`)。
    # World 装的,用来把引擎自己的回执从她的台词里摘掉。
    __slots__ = ("_redis", "_world", "_convs", "_msgs", "_lock", "content_filter")

    def __init__(self, redis: Any, world_id: str, lock: Any | None = None) -> None:
        import threading

        self._redis = redis
        self._world = world_id
        self._convs = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:conversations")
        self._msgs = RedisRows(redis, f"{KEY_PREFIX}:{world_id}:messages")
        self._lock = lock if lock is not None else threading.RLock()
        self.content_filter: Any | None = None

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _conv_msgs_key(self, conversation_id: int) -> str:
        return f"{KEY_PREFIX}:{self._world}:conv_msgs:{int(conversation_id)}"

    @staticmethod
    def _pid(row: dict) -> str:
        return row.get("player_id") or "user"

    def _conversations(self) -> list[dict]:
        rows = list(self._convs.all().values())
        rows.sort(key=lambda r: int(r["id"]))
        return rows

    # ── conversations ───────────────────────────────────────────────────────

    def active_conversation(self, agent_id: str, player_id: str | None = None):
        with self._lock:
            best = None
            for row in self._conversations():
                if row.get("agent_id") != agent_id or row.get("status") != "open":
                    continue
                if player_id is not None and self._pid(row) != player_id:
                    continue
                best = row
            return dict(best) if best is not None else None

    def start_conversation(self, agent_id: str, ts: int, participants=None,
                           location: str | None = None, player_id: str | None = None,
                           player_name: str | None = None) -> int:
        pid = player_id or "user"
        if participants is None:
            user_entry: dict = {"id": pid, "kind": "user"}
            if player_name:
                user_entry["name"] = player_name
            participants = [user_entry, {"id": agent_id, "kind": "agent"}]
        with self._lock:
            conv_id = int(self._redis.incr(f"{KEY_PREFIX}:{self._world}:conversations:id"))
            self._convs.put(str(conv_id), {
                "id": conv_id, "agent_id": agent_id, "status": "open",
                "started_at": int(ts), "last_activity_at": int(ts), "closed_at": None,
                "summary": None, "message_count": 0, "participants": participants,
                "location": location, "player_id": pid,
            })
            return conv_id

    def active_or_start(self, agent_id: str, ts: int, participants=None,
                        location: str | None = None, player_id: str | None = None,
                        player_name: str | None = None) -> int:
        with self._lock:
            active = self.active_conversation(agent_id, player_id=player_id)
            if active is not None:
                return int(active["id"])
            return self.start_conversation(
                agent_id, ts, participants=participants, location=location,
                player_id=player_id, player_name=player_name,
            )

    def get(self, conversation_id: int):
        row = self._convs.get(str(int(conversation_id)))
        return dict(row) if row is not None else None

    def list_conversations(self, agent_id: str) -> list[dict]:
        with self._lock:
            rows = [r for r in self._conversations() if r.get("agent_id") == agent_id]
            rows.reverse()
            return [dict(r) for r in rows]

    def close(self, conversation_id: int, summary: str, ts: int) -> None:
        with self._lock:
            row = self._convs.get(str(int(conversation_id)))
            if row is None:
                return
            row.update(status="closed", closed_at=int(ts), summary=summary)
            self._convs.put(str(int(conversation_id)), row)

    def touch(self, conversation_id: int, ts: int) -> None:
        with self._lock:
            row = self._convs.get(str(int(conversation_id)))
            if row is None:
                return
            row["last_activity_at"] = int(ts)
            self._convs.put(str(int(conversation_id)), row)

    def idle_open_conversations(self, now: int, timeout: int) -> list[dict]:
        with self._lock:
            return [
                dict(r) for r in self._conversations()
                if r.get("status") == "open" and int(r.get("last_activity_at") or 0) <= now - timeout
            ]

    # ── messages ────────────────────────────────────────────────────────────

    def add_message(self, conversation_id: int, role: str, content: str, ts: int) -> int:
        from anima_world.chat_store import filter_message_content

        content = filter_message_content(self, role, content)
        with self._lock:
            msg_id = int(self._redis.incr(f"{KEY_PREFIX}:{self._world}:messages:id"))
            self._msgs.put(str(msg_id), {
                "id": msg_id, "conversation_id": int(conversation_id), "role": role,
                "content": content, "created_at": int(ts),
                "stance": None, "intent": None, "intent_confidence": None, "tool_calls": None,
            })
            self._redis.rpush(self._conv_msgs_key(conversation_id), msg_id)
            row = self._convs.get(str(int(conversation_id)))
            if row is not None:
                row["message_count"] = int(row.get("message_count") or 0) + 1
                row["last_activity_at"] = int(ts)
                self._convs.put(str(int(conversation_id)), row)
            return msg_id

    def _message_rows(self, conversation_id: int) -> list[dict]:
        ids = self._redis.lrange(self._conv_msgs_key(conversation_id), 0, -1) or []
        out = []
        for raw in ids:
            mid = raw.decode() if isinstance(raw, bytes) else str(raw)
            row = self._msgs.get(mid)
            if row is not None:
                out.append(row)
        return out

    def messages_for(self, conversation_id: int) -> list[dict]:
        with self._lock:
            return [
                {"role": r["role"], "content": r["content"], "created_at": r["created_at"]}
                for r in self._message_rows(conversation_id)
            ]

    def recent_messages(self, conversation_id: int, n: int) -> list[dict]:
        with self._lock:
            rows = self._message_rows(conversation_id)[-int(n):] if n else []
            return [
                {"role": r["role"], "content": r["content"], "created_at": r["created_at"]}
                for r in rows
            ]

    def past_summaries(self, agent_id: str, k: int, player_id: str | None = None) -> list[str]:
        with self._lock:
            closed = [
                r for r in self._conversations()
                if r.get("agent_id") == agent_id and r.get("status") == "closed"
                and r.get("summary")
                and (player_id is None or self._pid(r) == player_id)
            ]
            closed.sort(key=lambda r: (int(r.get("closed_at") or 0), int(r["id"])), reverse=True)
            return [r["summary"] for r in closed[: int(k)]]

    # ── 一轮的观测量(表在谁那儿,写它的入口就在谁那儿)────────────────────────

    def annotate_message(self, message_id: int, *, stance=None, intent=None,
                         intent_confidence=None, tool_calls=None) -> None:
        from anima_world.chat_store import annotation_values

        values = annotation_values(stance, intent, intent_confidence, tool_calls)
        if not values:
            return
        with self._lock:
            row = self._msgs.get(str(int(message_id)))
            if row is None:
                return
            row.update(values)
            self._msgs.put(str(int(message_id)), row)

    def annotation_rows(self, conversation_id: int) -> list[tuple]:
        with self._lock:
            return [
                (r.get("role"), r.get("stance"), r.get("intent"), r.get("tool_calls"))
                for r in self._message_rows(conversation_id)
            ]

    def conversation_meta(self, conversation_id: int) -> dict:
        from anima_world.chat_store import summarize_annotations

        return summarize_annotations(self.annotation_rows(conversation_id))

    def erase_player(self, player_id: str, *, dry_run: bool = False) -> dict[str, int]:
        """删掉这个玩家的**全部转录**(会话行 + 消息行 + 索引列表)。法务抹除的一环。

        整场删,不是逐条挑:一场会话里她说的那半也是**对他说的**,单删他那半留下的
        是一段自言自语 —— 看上去完整,读起来是假的。逐轮观测量(stance/intent/
        tool_calls)住在消息行上,和消息同生共死(那条老纪律),跟着一起走。

        `_pid` 的缺省语义照旧:`player_id` 为空的老会话读作 'user' ——
        `erase_player("user")` 因此会把没记玩家 id 的那批全删掉,这是明说的行为。
        """
        pid = str(player_id or "").strip()
        out = {"conversations": 0, "messages": 0}
        if not pid:
            return out
        with self._lock:
            for row in self._conversations():
                if self._pid(row) != pid:
                    continue
                conv_id = int(row["id"])
                key = self._conv_msgs_key(conv_id)
                ids = self._redis.lrange(key, 0, -1) or []
                out["conversations"] += 1
                out["messages"] += len(ids)
                if dry_run:
                    continue
                for raw in ids:
                    mid = raw.decode() if isinstance(raw, bytes) else str(raw)
                    self._msgs.drop(mid)
                self._redis.delete(key)
                self._convs.drop(str(conv_id))
        return out


# ── 在场玩家(3.2.0:从进程内存搬进 Redis)──────────────────────────────────


def players_index_key(world_id: str) -> str:
    """在场玩家的名册索引(SET)。行本身在 `player_key` 的每人一个 hash 上。"""
    return f"{KEY_PREFIX}:{world_id}:players"


def player_key(world_id: str, player_id: str) -> str:
    """一个在场玩家的行(HASH,字段值是 JSON)。**过期时间挂在这个键上。**"""
    return f"{KEY_PREFIX}:{world_id}:player:{player_id}"


#: 行程住在同一个 hash 的这个字段里。**不另开一个键**:两个键就有两条过期规则,
#: 而"人走了但他的行程还在"这种半截状态会让 `player_in_transit` 说谎。
_TRANSIT_FIELD = "__transit__"


class RedisPlayerPresence:
    """谁这会儿在场、他站在哪、他在往哪走。

    **为什么它必须落 Redis(3.2.0 之前是"刻意的内存态")。** 那个"刻意"在单进程的
    宿主里成立,在容器里不成立:一次重启把名册清空之后,`player_location()` 返回空串,
    于是每一场对话都**静默**退回"手机私聊"的措辞 —— 世界照跑、日志干净,而人明明
    就坐在她对面。按这个仓库的分家判据它本来就该在 Redis:并发的玩家是有限的
    (有界),而 `_present_roster` 直接进她的决定上下文(进提示词)。

    **过期只有一条规则:键还在不在。** 从前 `_present_roster` 自己按 `last_seen`
    过滤,现在改用 Redis 自己的 TTL —— 两套过期规则迟早给出不同答案(一个进程说
    他在场、另一个说他走了,而两边都不报错)。`last_seen` 照旧记着给人看,但**没有
    任何判断读它**。

    **只填缺不覆盖**:`create` 用 `HSETNX` 逐字段落,所以一个刚起来的进程不会把另一个
    进程刚写下的"他在咖啡店"盖回 `None`。
    """

    __slots__ = ("_redis", "_world_id", "_ttl")

    def __init__(self, redis: Any, world_id: str, ttl_seconds: float) -> None:
        self._redis = redis
        self._world_id = str(world_id)
        self._ttl = float(ttl_seconds)

    # ── TTL ───────────────────────────────────────────────────────────────
    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    @ttl_seconds.setter
    def ttl_seconds(self, value: float) -> None:
        self._ttl = float(value)
        # 改了就当场作用到在场的人身上。不重挂的话,改短 TTL 对已经在场的人无效,
        # 而调用方看到的是"我明明调小了却还有幽灵"。
        for pid in self.ids():
            self._touch_ttl(player_key(self._world_id, pid))

    def _touch_ttl(self, key: str) -> None:
        if self._ttl <= 0:
            # 0 / 负数 = 不过期(测试里的"永远在场")。持久化成没有 TTL 的键。
            self._redis.persist(key)
            return
        self._redis.pexpire(key, max(1, int(self._ttl * 1000)))

    # ── 读 ────────────────────────────────────────────────────────────────
    def ids(self) -> list[str]:
        """此刻在场的玩家 id。顺手把过期掉的人从索引里摘掉(惰性剪枝)。"""
        raw = self._redis.smembers(players_index_key(self._world_id)) or set()
        members = sorted(
            (m.decode() if isinstance(m, bytes) else str(m)) for m in raw
        )
        if not members:
            return []
        pipe = self._redis.pipeline()
        for pid in members:
            pipe.exists(player_key(self._world_id, pid))
        alive = pipe.execute()
        live = [pid for pid, ok in zip(members, alive) if ok]
        stale = [pid for pid, ok in zip(members, alive) if not ok]
        if stale:
            self._redis.srem(players_index_key(self._world_id), *stale)
        return live

    def get(self, player_id: str) -> dict[str, Any] | None:
        raw = self._redis.hgetall(player_key(self._world_id, player_id)) or {}
        if not raw:
            return None
        return {
            (k.decode() if isinstance(k, bytes) else k): _loads(v)
            for k, v in raw.items()
            if (k.decode() if isinstance(k, bytes) else k) != _TRANSIT_FIELD
        }

    def all(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for pid in self.ids():
            row = self.get(pid)
            if row is not None:
                out[pid] = row
        return out

    def __contains__(self, player_id: str) -> bool:
        return bool(self._redis.exists(player_key(self._world_id, player_id)))

    # ── 写 ────────────────────────────────────────────────────────────────
    def create(self, player_id: str, defaults: dict[str, Any]) -> bool:
        """第一次见到这个人。返回"这次真的是新建的吗"。

        逐字段 `HSETNX`:多个进程同时操作一个世界,只填缺不覆盖。
        """
        key = player_key(self._world_id, player_id)
        fresh = not self._redis.exists(key)
        pipe = self._redis.pipeline()
        for field, value in defaults.items():
            pipe.hsetnx(key, field, _dumps(value))
        pipe.sadd(players_index_key(self._world_id), player_id)
        pipe.execute()
        self._touch_ttl(key)
        return fresh

    def update(self, player_id: str, fields: dict[str, Any]) -> None:
        """写几个字段,顺便续命(任何一次交互都算"我还在")。"""
        key = player_key(self._world_id, player_id)
        if fields:
            self._redis.hset(
                key, mapping={f: _dumps(v) for f, v in fields.items()}
            )
        self._redis.sadd(players_index_key(self._world_id), player_id)
        self._touch_ttl(key)

    def drop_field(self, player_id: str, field: str) -> None:
        self._redis.hdel(player_key(self._world_id, player_id), field)

    def forget(self, player_id: str) -> None:
        """他走了。行与索引一起清 —— 只清一半就是留一个查不到行的名字。"""
        pipe = self._redis.pipeline()
        pipe.delete(player_key(self._world_id, player_id))
        pipe.srem(players_index_key(self._world_id), player_id)
        pipe.execute()

    def expire_now(self, player_id: str) -> None:
        """把 TTL 走到头这件事当场演一遍(测试用)。

        故意**只删行、留索引**:真的过期就长这样,于是 `ids()` 的剪枝那条路也
        跟着被验到。
        """
        self._redis.delete(player_key(self._world_id, player_id))

    # ── 行程 ──────────────────────────────────────────────────────────────
    def get_transit(self, player_id: str) -> dict[str, Any] | None:
        raw = self._redis.hget(player_key(self._world_id, player_id), _TRANSIT_FIELD)
        if raw is None:
            return None
        trip = _loads(raw)
        return trip if isinstance(trip, dict) else None

    def set_transit(self, player_id: str, trip: dict[str, Any]) -> None:
        self.update(player_id, {_TRANSIT_FIELD: trip})

    def clear_transit(self, player_id: str) -> None:
        self.drop_field(player_id, _TRANSIT_FIELD)

    def transit_ids(self) -> list[str]:
        return [pid for pid in self.ids() if self.get_transit(pid) is not None]


def drop_stale_copies_for_mysql(redis: Any, world_id: str) -> list[str]:
    """删掉"只有 Redis"时期留下的 events / memories 拷贝 —— 这些表归 MySQL 了。

    一个世界可能先只用 Redis 跑过,后来才接上 MySQL。不删的话那份旧拷贝会一直
    躺着冒充这个世界的历史:引擎自己不读它,但"只有 Redis 连接的进程"读得到 ——
    两份真相里一份不会更新,是这个仓库最怕的坏法。
    """
    doomed = [
        events_key(world_id),
        f"{KEY_PREFIX}:{world_id}:memories",
        f"{KEY_PREFIX}:{world_id}:memories:id",
    ]
    dropped = [key for key in doomed if redis.delete(key)]
    if dropped:
        logger.info("删掉 Redis 里冻住的旧拷贝(这些表归 MySQL 了):%s", dropped)
    return dropped
