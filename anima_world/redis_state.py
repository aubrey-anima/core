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
