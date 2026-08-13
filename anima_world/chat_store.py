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


def filter_message_content(store: Any, role: str, content: str) -> str:
    """落库前过一道 `store.content_filter`(没装就原样放行)。

    **装它的是 `World`,为的是把引擎自己的回执从她的台词里摘掉** —— 那句
    「(没有 哈尔滨 这个地方;有的是 …)」是塞在她的回复前面流给玩家的,而宿主
    原样把整段流当成"她这一轮说的话"交回来。

    ⚠️ **闸必须在这一层,不在 `World.record_chat_turn`。** 那个方法是"记一轮并当场
    关闭",而真宿主(运维台的世界壳)**有意绕开它**:每轮都关会话等于每句话生成一次
    摘要、跑一次关系判定,既贵、语义也不对(一句「你好」不是一场会面)。它直接调
    `world.chat_store.add_message`。第一版只修了 `record_chat_turn`,于是整条修法在
    真部署上一次都没生效 —— 而单测全绿,因为测试走的是引擎自带的那条路。
    **两条路都要过这道闸,所以闸放在两条路的交汇处:落库那一下。**

    过滤器的形状是 `(role, content) -> content`,永不抛(它坏掉不该让一句话丢掉)。
    """
    fn = getattr(store, "content_filter", None)
    if fn is None:
        return content
    try:
        return str(fn(role, content))
    except Exception:  # noqa: BLE001 - 过滤器坏掉不该吃掉一句话
        logger.warning("转录内容过滤器出错,原样落库", exc_info=True)
        return content


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
