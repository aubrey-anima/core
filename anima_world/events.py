"""事件模块:`Event` 类型的老家(re-export)+ 事件日志的接口占位。

SQLite 版 `EventLog` 已随 world.db 层整体退役。真实现在别处:

- `anima_world.redis_state.RedisEventLog`(默认)
- `anima_world.mysql_state.MySQLEventLog`(给了 `mysql=` 的世界)

两者接口逐字相同:`append` / `replay` / `count` / `max_seq` / `page`。
这里留下的 `EventLog` 只是那份接口的占位(scheduler / __main__ 用它做类型注解),
不可实例化 —— 构造即报错,免得有人拿着一个空壳日志静默地丢事件。
"""

from __future__ import annotations

from anima_world.types import Event

__all__ = ["Event", "EventLog"]


class EventLog:
    """事件日志的接口占位(仅供类型注解)。

    SQLite 实现已退役;要一个能用的日志,用 `RedisEventLog` 或 `MySQLEventLog`。
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "SQLite 版 EventLog 已退役:请用 anima_world.redis_state.RedisEventLog "
            "或 anima_world.mysql_state.MySQLEventLog"
        )
