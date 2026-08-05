"""后端无关的转录帮助函数:一轮观测量的取舍与一场会话的聚合。

会话/消息的持久化实现在 `anima_world.redis_state.RedisChatStore` 与
`anima_world.mysql_state.MySQLChatStore`;SQLite 版 `ChatStore` 已随
world.db 层退役,这里只留下一个接口占位(`chat_service` / `chat_session`
的类型注解在用)和各后端**必须共用**的纯函数 —— 聚合写两遍,SQLite 世界
和 MySQL 世界就会给同一场对话算出不同的 meta。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def summarize_annotations(rows: list[tuple]) -> dict[str, Any]:
    """`(role, stance, intent, tool_calls)` 的行 → 一场会话的 stance/intent 分布。

    **后端无关,所以只写一遍。** 每个后端各写一份聚合的话,SQLite 世界和 MySQL
    世界会给同一场对话算出不同的 meta —— 而两边都跑得动,只是答案不一样。
    """
    stances: dict[str, int] = {}
    intents: dict[str, int] = {}
    tools: list[str] = []
    for _role, stance_value, intent_value, tool_json in rows:
        if stance_value:
            stances[stance_value] = stances.get(stance_value, 0) + 1
        if intent_value:
            intents[intent_value] = intents.get(intent_value, 0) + 1
        if tool_json:
            try:
                for call in json.loads(tool_json) if isinstance(tool_json, str) else (tool_json or []):
                    name = (call or {}).get("tool")
                    if name:
                        tools.append(str(name))
            except ValueError:
                logger.warning("消息 tool_calls 不是合法 JSON,跳过")
    meta: dict[str, Any] = {}
    if stances:
        meta["stances"] = stances
    if intents:
        meta["intents"] = intents
    if tools:
        meta["tools_used"] = tools
    return meta


ANNOTATION_COLUMNS = ("stance", "intent", "intent_confidence", "tool_calls")


def annotation_values(
    stance: str | None, intent: str | None,
    intent_confidence: float | None, tool_calls: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """给出什么写什么,None 一律不动 —— 各后端共用这一份取舍。"""
    out: dict[str, Any] = {}
    if stance is not None:
        out["stance"] = stance
    if intent is not None:
        out["intent"] = intent
    if intent_confidence is not None:
        out["intent_confidence"] = float(intent_confidence)
    if tool_calls is not None:
        out["tool_calls"] = json.dumps(tool_calls, ensure_ascii=False)
    return out


class ChatStore:
    """会话转录存储的接口占位(仅供类型注解)。

    SQLite 实现已退役;真实现是 `RedisChatStore` / `MySQLChatStore`,接口逐字
    相同:`active_conversation` / `start_conversation` / `active_or_start` /
    `get` / `list_conversations` / `close` / `touch` / `idle_open_conversations` /
    `add_message` / `messages_for` / `recent_messages` / `past_summaries` /
    `annotate_message` / `annotation_rows` / `conversation_meta`。
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "SQLite 版 ChatStore 已退役:请用 anima_world.redis_state.RedisChatStore "
            "或 anima_world.mysql_state.MySQLChatStore"
        )
