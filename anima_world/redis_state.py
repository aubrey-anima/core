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
from typing import Any

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
