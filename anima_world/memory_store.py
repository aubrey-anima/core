"""记忆层的公共类型:`MemoryDescriptor` —— 触发器决定"这件事值得记"时产出的描述。

记忆是派生真相,不是源真相(design.md D1):`memories` 是"足够重要的事件"的
投影,重要与否由调用方注入的触发器决定(与 `memory_triggers.TriggerEngine`
解耦,两个模块可独立构建/测试;design.md D7:存储层不做跨角色 ACL,
访问范围是调用点的约定)。

持久化实现在 `anima_world.redis_state.RedisMemoryStore` 与
`anima_world.mysql_state.MySQLMemoryStore`(三因子检索/加固/遗忘的打分逻辑
在 `anima_world.memory_retrieval`,两个后端共用);SQLite 版 `MemoryStore`
已随 world.db 层退役。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryDescriptor:
    """What a trigger decides should become a memory row."""

    agent_id: str
    tick: int
    kind: str
    summary: str
    importance: float = 0.5
    anchor: bool = False
    event_seq: int | None = None
    #: **她是怎么知道这件事的**(R3:记忆分型)。三种存在方式,默认「亲历」。
    #:
    #: - `experienced` 她在场,这件事发生在她身上
    #: - `heard`       别人告诉她的(八卦传过来的那条)
    #: - `believed`    她自己想出来的(反思、推断)
    #:
    #: 为什么要分:**这三种记忆的可靠性不一样,而她说出口时的语气也该不一样。**
    #: 不分型的下场是她把一条听来的传闻当亲眼所见讲出去 —— 而八卦系统每传一手
    #: 就多一层失真,传到第三个人那里已经和事实无关了,她却仍然说得斩钉截铁。
    #: 前沿的记忆系统(Hindsight 的 beliefs 网络、typed-memory 那一路)把这条
    #: 单独列出来,理由是同一个:出处丢了之后,再多的检索精度也救不回来。
    provenance: str = "experienced"
