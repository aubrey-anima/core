"""PromptStore: named prompt templates as data (M5).

Chat/narrative prompt wording used to be Python string literals in
`chat_service.py`, `chat_session.py`, and `narrative.py`. It now lives in the
`prompt_templates` table and is read live at the point each prompt is built,
so an admin editing it via `World.prompt_set` takes effect on the next call with
no process restart (design.md D3). Saves are validated against a
representative set of the variables the corresponding call site actually
passes (design.md D7) so a typo can't silently break live chat/narrative —
storing only the current value, no history (design.md Non-Goals).
"""

from __future__ import annotations

import sqlite3
import string
import threading
from datetime import datetime, timezone
from typing import Any

from anima_world.planner import _DEFAULT_PROMPT as _PLANNER_FREETIME
from anima_world.chat_service import (
    _DEFAULT_PRESENCE_TEMPLATE as _CHAT_PRESENCE,
)
from anima_world.chat_service import (
    _DEFAULT_RELATION_TEMPLATE as _CHAT_RELATION,
)
from anima_world.chat_service import (
    _DEFAULT_WORLD_MEMORY_TEMPLATE as _CHAT_WORLD_MEMORY,
)
from anima_world.relationship_judge import _DEFAULT_PROMPT as _JUDGE_RELATIONSHIP
from anima_world.relationship_judge import _DEFAULT_RELABEL_PROMPT as _JUDGE_RELABEL
from anima_world.relationship_judge import _DEFAULT_USER_JUDGE_PROMPT as _JUDGE_USER


class PromptRenderError(ValueError):
    """A template references a placeholder its call site doesn't provide."""


# name -> representative variables the corresponding call site passes at
# runtime, used to render-check a template before it's persisted.
_SAMPLE_VARS: dict[str, dict[str, Any]] = {
    "chat.system_persona": {"name": "夏", "personality": "开朗热情"},
    "chat.memory_block": {"summaries": "- 上次聊了咖啡馆的事"},
    "chat.session_summary": {},
    "narrative.describe": {},
    "world.setting": {},
    "judge.relationship": {
        "a_name": "夏", "a_personality": "开朗", "a_goals": "把店开好", "a_memories": "（无）",
        "b_name": "遥", "b_personality": "冷静", "b_goals": "（无特别目标）", "b_memories": "（无）",
        "a_to_b": 0.1, "b_to_a": 0.0, "r_type": "朋友", "r_type_back": "朋友",
        "location": "cafe",
    },
    "judge.relabel": {
        "a_name": "夏", "a_personality": "开朗", "b_name": "遥", "b_personality": "冷静",
        "old_r_type": "点头之交", "old_band": "熟识", "new_band": "亲近", "memories": "（无）",
    },
    "chat.world_memory_block": {"memories": "- 债务解除了"},
    "chat.presence_block": {
        "day": 5, "hh": "20", "mm": "15", "location": "酒吧", "activity": "在值班",
        "others": "罗本",
    },
    "chat.relation_block": {"r_type": "有点好奇的新面孔", "band": "熟识"},
    "judge.user_relationship": {
        "a_name": "夏", "a_personality": "开朗", "player_name": "阿檀",
        "a_to_b": 0.1, "b_to_a": 0.0, "r_type": "初次见面的访客",
        "transcript": "user: 你好\nassistant: 你好呀", "location": "cafe",
    },
    "planner.freetime": {
        "name": "苏晚夏", "personality": "开朗热情", "day": 3,
        "free_windows": "- 18:00 到 23:00", "action_space": "- walk（location 可选：cafe）",
        "memories": "",
        # prompt-grounding: the call site renders goals as one block; listed
        # here so an admin-saved template referencing {goals} passes
        # check_renders instead of being rejected.
        "goals": "你当前的目标：\n- 示例目标\n\n",
    },
}

_WORLD_SETTING = """【世界】
这里是临海市旧港区，二零四几年的一个平常年份。城市早就数字化了，旧港区却还慢着：手冲咖啡、纸质草图、颜料、和总也下不完的雨。
街区只有三个地方——咖啡店（cafe）、建筑工作室（workshop）、以及一间用来画画的家（home）。走出这三处，世界就模糊了，没人会去想那之外有什么。

【居民】
住在这里的都是有血有肉的普通人：会累，会走神，会对彼此生出好感或疏远，也会遗忘。
每个人只记得自己亲身经历过的事，读不到别人心里在想什么。
偶尔会有一位「访客」来搭话。没人知道他从哪来，就当是个新面孔，自然地招呼、闲聊。

【怎么说话】
说中文，口语，短句。始终以角色本人的身份开口。
不解释设定，不提"AI""模型""提示词"，也不承认自己是虚构的。"""

_DEFAULTS: dict[str, tuple[str, str]] = {
    "planner.freetime": (
        _PLANNER_FREETIME,
        "LLM 规划 agent 自由时间动作序列的 prompt",
    ),
    "world.setting": (
        _WORLD_SETTING,
        "世界基础设定（世界观）——注入所有 agent 的 chat 与 narrative 提示词",
    ),
    "chat.system_persona": (
        "你是{name}。{personality}",
        "Chat 系统人设模板",
    ),
    "chat.memory_block": (
        "你和对方过去的对话回顾：\n{summaries}",
        "Chat 跨会话记忆拼接模板",
    ),
    "chat.session_summary": (
        "用一句中文概括这次对话的主要内容和情绪基调。只输出摘要，不要解释。",
        "会话关闭时生成总结用的 prompt",
    ),
    "chat.world_memory_block": (
        _CHAT_WORLD_MEMORY,
        "Chat 世界记忆块——MemoryStore top-K 进 prompt（chat-grounding）",
    ),
    "chat.presence_block": (
        _CHAT_PRESENCE,
        "Chat 在场块——时间/地点/当前活动/同地者（chat-grounding）",
    ),
    "chat.relation_block": (
        _CHAT_RELATION,
        "Chat 关系块——对来访者的 r_type 与档位（chat-grounding）",
    ),
    "judge.relationship": (
        _JUDGE_RELATIONSHIP,
        "chat 后 LLM 判定对话摘要与双向好感变化的 prompt（llm-relationship-judge）",
    ),
    "judge.relabel": (
        _JUDGE_RELABEL,
        "关系跨档后 LLM 改写 r_type 描述的 prompt（relationship-stage-machine）",
    ),
    "judge.user_relationship": (
        _JUDGE_USER,
        "玩家会话关闭后按真实转录判双向好感变化的 prompt（player-visitor）",
    ),
    "narrative.describe": (
        "请用中文生成一句适合作为这个 agent 的世界叙事或聊天回复。只输出正文，不要解释。",
        "LLM narrative provider 的描述指令",
    ),
}


# Templates whose consumers use the text RAW (never .format()ed) — brace
# characters in them are prose, not placeholders. Running placeholder
# validation against these rejected perfectly good worldview text containing
# a literal '{' (prompt-grounding code review #1).
_RAW_TEMPLATES = {"world.setting"}


def check_renders(name: str, template: str) -> None:
    """Raise `PromptRenderError` if `template` references an unknown placeholder."""
    if name in _RAW_TEMPLATES:
        return
    sample = _SAMPLE_VARS.get(name, {})
    try:
        string.Formatter().vformat(template, (), sample)
    except (KeyError, IndexError, ValueError) as exc:
        raise PromptRenderError(
            f"template '{name}' references an unknown placeholder: {exc}"
        ) from exc


class PromptStore:
    """SQLite-backed, in-memory-cached store for named prompt templates.

    Mirrors `ConfigStore`'s cache/lock pattern; `lock` defaults to its own
    RLock but the web server passes the scheduler's shared lock since the
    connection is shared across threads.
    """

    def __init__(self, conn: sqlite3.Connection, lock: Any | None = None) -> None:
        self._conn = conn
        self._lock = lock if lock is not None else threading.RLock()
        self._cache: dict[str, str] = {}
        self._descriptions: dict[str, str | None] = {}
        self._hydrate()

    def _hydrate(self) -> None:
        with self._lock:
            cur = self._conn.execute("SELECT name, template, description FROM prompt_templates")
            rows = cur.fetchall()
        for name, template, description in rows:
            self._cache[name] = template
            self._descriptions[name] = description

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._cache

    def get(self, name: str, default: str = "") -> str:
        with self._lock:
            return self._cache.get(name, default)

    def set(self, name: str, template: str, description: str | None = None) -> None:
        check_renders(name, template)
        with self._lock:
            if description is None:
                description = self._descriptions.get(name)
            updated_at = datetime.now(timezone.utc).isoformat()
            self._conn.execute(
                "INSERT INTO prompt_templates (name, template, description, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "template=excluded.template, description=excluded.description, updated_at=excluded.updated_at",
                (name, template, description, updated_at),
            )
            self._conn.commit()
            self._cache[name] = template
            self._descriptions[name] = description

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"name": name, "template": template, "description": self._descriptions.get(name)}
                for name, template in self._cache.items()
            ]


def seed_defaults(store: PromptStore) -> None:
    """Idempotently seed the four default templates from their previously
    hardcoded wording. A no-op for any name that already has a row."""
    for name, (template, description) in _DEFAULTS.items():
        if store.has(name):
            continue
        store.set(name, template, description=description)
