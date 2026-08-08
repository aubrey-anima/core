"""ConfigStore: runtime-tunable scalar configuration (M5).

Every value that used to be a Python constant or an env-var-only setting
(scheduler timing, chat/memory thresholds) lives in the world's `:config`
rows and is read live at the point of use, so an admin editing it via
`World.config_set` takes effect on the next call with no process restart.

**世界里没有 secret。** `llm.api_key` 是唯一声明为密文的键,它住机器配置
(`machine_config`,~/.anima-world/config.json)。world.db 时代的 Fernet
加密与随库搬迁的 keyfile 随 SQLite 一起退役 —— 往世界里写一个非空 secret
现在是**当场报错**,不是加密,因为世界是分发物,而引擎已没有钥匙可用。

存取分离:ConfigStore 拿一个 `backend`(`all() -> {key: row}` /
`put(key, row)`,见 `redis_state.RedisConfigBackend`),自己只负责类型、
声明回落与解析顺序。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from anima_world import together

logger = logging.getLogger(__name__)

_VALUE_TYPES = {"str", "int", "float", "bool"}


def _coerce(raw_value: str, value_type: str) -> Any:
    if value_type == "int":
        return int(raw_value)
    if value_type == "float":
        return float(raw_value)
    if value_type == "bool":
        return raw_value == "1"
    return raw_value


def _stringify(value: Any, value_type: str) -> str:
    if value_type == "bool":
        return "1" if value else "0"
    return str(value)


def _infer_value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "str"


def coerce_to_declared_type(value: Any, value_type: str) -> Any:
    """按**声明类型**把一个值强转过去,转不了就抛 ValueError/TypeError。

    `ConfigStore.set` 本身是原始写入(不强转),强转此前只长在 `World.config_set`
    里 —— 于是任何**绕过门面**的写入(种子的 `config` 块就是一条)会把
    `"needs.enabled": "yes"` 原样塞进去:db 里存 `"1"`、内存缓存里却是字符串
    `"yes"`,两边不一致,而且没人报错。抽到这里让两条路共用同一份规则。
    """
    if value_type == "int":
        return int(value)
    if value_type == "float":
        return float(value)
    if value_type == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            if value.lower() not in ("true", "false", "1", "0"):
                raise ValueError(f"invalid bool value: {value}")
            return value.lower() in ("true", "1")
        return bool(value)
    return str(value)


def mask_secret(value: str) -> str:
    """Prefix + last-4-characters mask for API responses (design.md D6)."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}***{value[-4:]}"


class ConfigStore:
    """Backend-agnostic, in-memory-cached store for typed scalar config values.

    `backend` 只有两个动作:`all()` 取全部行、`put(key, row)` 写一行
    (`redis_state.RedisConfigBackend` 是唯一的生产实现;测试给个 dict 包装就行)。
    All access is serialized through `lock`(缺省自带 RLock;宿主可传调度器
    共享锁,和 `MemoryStore` 同一个模式)。
    """

    def __init__(
        self,
        backend: Any,
        lock: Any | None = None,
    ) -> None:
        self._backend = backend
        self._lock = lock if lock is not None else threading.RLock()
        self._cache: dict[str, Any] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._hydrate()

    def _hydrate(self) -> None:
        with self._lock:
            rows = self._backend.all()
        for key, row in rows.items():
            is_secret = bool(row.get("is_secret"))
            value_type = row.get("value_type") or "str"
            self._meta[key] = {
                "value_type": value_type,
                "category": row.get("category"),
                "is_secret": is_secret,
                "description": row.get("description"),
            }
            raw_value = row.get("value")
            # 世界里不该有 secret(见模块 docstring);真读到一个就当没配,
            # 并点名 —— 静默用一个不该在这儿的密钥比报警坏。
            if is_secret and raw_value:
                logger.warning(
                    "config %s 是 secret 却存在世界里 —— 忽略;它属于机器配置"
                    "(anima-world config set %s …)", key, key,
                )
                self._cache[key] = None
                continue
            self._cache[key] = _coerce(raw_value or "", value_type)

    def get(self, key: str, default: Any = None) -> Any:
        """解析顺序:**环境变量 → 机器配置 → 世界配置 → 引擎默认值。**

        `llm.*` 那几个属于**这台机器**,不属于任何世界(见 `machine_config`)——
        你用哪家模型、哪把钥匙,和"这个世界是什么样"无关。世界配置排在倒数第二
        只为向后兼容:1.3.0 之前建的世界里真的有那一行。

        最后一层是 `_DEFAULTS`,**不是调用方传的 `default`** —— 引擎声明过的键
        以引擎的声明为准。调用方那个 `default=` 只在键根本没被声明时才轮得到,
        今天它们全是死参数,而且已经有两个和 `_DEFAULTS` 对不上
        (`llm.timeout` 20 vs 30、`scheduler.tick_rate` 1.0 vs 1/300)——
        让它们生效等于给同一个键留两份真相。
        """
        from anima_world import machine_config

        if machine_config.is_machine_key(key):
            found = machine_config.resolve(key)
            if found is not None:
                return self._coerce_like(key, found[0])
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        declared = _DEFAULTS.get(key)
        return declared[0] if declared is not None else default

    def _coerce_like(self, key: str, raw: Any) -> Any:
        """机器配置里的值按这个键声明的类型强转 —— 环境变量一律是字符串。"""
        meta = self._meta.get(key) or {}
        declared = meta.get("value_type") or (_DEFAULTS.get(key, (None, "str"))[1])
        try:
            return coerce_to_declared_type(raw, str(declared))
        except (TypeError, ValueError):
            logger.warning("机器配置里的 %s 转不成 %s,按原样用:%r", key, declared, raw)
            return raw

    def world_value(self, key: str, default: Any = None) -> Any:
        """**只看世界文件这一层**,不走解析顺序。

        `get()` 会先问环境变量和机器配置,所以它回答不了"这个世界文件里有没有它"
        —— 而那正是"钥匙泄漏没有"这个问题。诊断和测试要的是这个。
        """
        with self._lock:
            return self._cache.get(key, default)

    def provenance(self, key: str) -> str:
        """这个值是从哪儿来的。**"为什么我改了配置没生效"几乎总是这个问题。**"""
        from anima_world import machine_config

        if machine_config.is_machine_key(key):
            found = machine_config.resolve(key)
            if found is not None:
                return found[1]
        # 空值不算"世界文件里有" —— 创世会给每个键播一行,包括加密过的空串。
        # 拿"这一行存在"当"这儿有值",就会把每个没配过的键都报成来自世界文件。
        if self.world_value(key) not in (None, ""):
            return "世界文件"
        return "默认值"

    def set(
        self,
        key: str,
        value: Any,
        value_type: str | None = None,
        category: str | None = None,
        is_secret: bool | None = None,
        description: str | None = None,
    ) -> None:
        # **元数据回落到引擎的声明,不只看已有的行。**
        #
        # 创世不再播默认值之后,一个新世界的 `config` 表是空的 —— 于是
        # `set("llm.api_key", "sk-…")` 拿不到任何元数据,`is_secret` 缺省成 False,
        # 密钥**明文写进世界文件,而且一声不吭**。这正是 `.cyberworld` 是分发物那条
        # 纪律要防的事,而移动 1 把它的地基抽掉了。
        #
        # 判据是"引擎声明过什么",不是"表里有没有行" —— 和 `has` / `meta` / `list`
        # 同一条。`meta()` 已经会回落,这里用它。
        existing = self.meta(key)
        with self._lock:
            if existing is not None:
                value_type = existing["value_type"] if value_type is None else value_type
                category = existing["category"] if category is None else category
                is_secret = existing["is_secret"] if is_secret is None else is_secret
                description = existing["description"] if description is None else description
            if value_type is None:
                value_type = _infer_value_type(value)
            if category is None:
                category = "general"
            if is_secret is None:
                is_secret = False

            raw = _stringify(value, value_type)
            if is_secret and raw:
                # 世界是分发物,而引擎已没有加密钥匙 —— 明文落进世界文件的对应物
                # 正是当年那个"密钥无声写进 config 表"的事故。当场拒绝,并指路。
                raise RuntimeError(
                    f"cannot store secret '{key}' in a world: secrets live in "
                    f"machine config (anima-world config set {key} …)"
                )

            updated_at = datetime.now(timezone.utc).isoformat()
            self._backend.put(key, {
                "value": raw, "value_type": value_type, "category": category,
                "is_secret": bool(is_secret), "description": description,
                "updated_at": updated_at,
            })
            self._meta[key] = {
                "value_type": value_type,
                "category": category,
                "is_secret": is_secret,
                "description": description,
            }
            self._cache[key] = value

    def undecryptable_secrets(self) -> list[str]:
        """世界里读不回来的 secret。SQLite 与 keyfile 退役后这个集合恒空 ——
        方法留着是因为 `World.state()` / doctor 的报告面还在问。"""
        return []

    def has(self, key: str) -> bool:
        """这个键**存在** —— 世界文件里有一行,或者引擎声明过它。

        创世不再播默认值之后,"表里没有"不等于"没这个键";拿行的存在当键的存在,
        一个新世界里 `config get chat.recall_k` 会说"没有这个配置项"。
        问"作者动过没有"用 `provenance()`。
        """
        with self._lock:
            if key in self._meta:
                return True
        return key in _DEFAULTS

    def meta(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            meta = self._meta.get(key)
            if meta is not None:
                return dict(meta)
        declared = _DEFAULTS.get(key)
        if declared is None:
            return None
        _, value_type, category, is_secret, description = declared
        return {
            "value_type": value_type,
            "category": category,
            "is_secret": is_secret,
            "description": description,
        }

    def list(self, category: str | None = None) -> list[dict[str, Any]]:
        """全部的键 —— 引擎声明的加上世界文件里多出来的,每一行带 `source`。

        `source` 是移动 1 兑现出来的那半:世界文件里只剩作者动过的之后,
        "这个值是谁定的"才第一次答得上来。
        """
        with self._lock:
            extra = [key for key in self._meta if key not in _DEFAULTS]
        items = []
        for key in list(_DEFAULTS) + extra:
            meta = self.meta(key) or {}
            if category is not None and meta["category"] != category:
                continue
            items.append(
                {
                    "key": key,
                    "value": self.get(key),
                    "value_type": meta["value_type"],
                    "category": meta["category"],
                    "is_secret": meta["is_secret"],
                    "description": meta["description"],
                    "source": self.provenance(key),
                }
            )
        return items


# key -> (default value, value_type, category, is_secret, description)
_DEFAULTS: dict[str, tuple[Any, str, str, bool, str]] = {
    "llm.api_key": ("", "str", "llm", True, "LLM API key (OpenAI-compatible)"),
    "llm.base_url": ("", "str", "llm", False, "LLM API base URL"),
    "llm.model": ("gpt-4o-mini", "str", "llm", False, "LLM model name"),
    "llm.timeout": (30.0, "float", "llm", False, "LLM request timeout (seconds)"),
    "llm.max_retries": (2, "int", "llm", False, "LLM SDK max retries"),
    "scheduler.tick_rate": (1 / 300, "float", "scheduler", False, "Real-time clock: one 5-minute world tick every 5 real minutes"),
    "scheduler.max_agents": (100, "int", "scheduler", False, "Roster cap, a performance guardrail; 0 = unlimited. The operator's number, not the authoring tool's"),
    "agent.idle_timeout": (30.0, "float", "scheduler", False, "BT idle watchdog threshold (seconds)"),
    "world.minutes_per_tick": (5, "int", "scheduler", False, "World minutes one tick represents (5 → a world day is 288 ticks)"),
    "world.travel_minutes_per_unit": (60, "int", "scheduler", False, "World minutes to cross one canvas unit on foot (the old harbour district is walkable)"),
    "planner.enabled": (True, "bool", "planner", False, "LLM plans each agent's free time between duties"),
    "planner.timeout": (30.0, "float", "planner", False, "Planner LLM call timeout (seconds)"),
    "judge.timeout": (30.0, "float", "judge", False, "Relationship judge LLM call timeout (seconds)"),
    "chat.idle_timeout": (600, "int", "chat", False, "Chat session auto-close threshold (seconds)"),
    "chat.recall_k": (3, "int", "chat", False, "Closed-session summaries recalled into the prompt"),
    "chat.recall_n": (10, "int", "chat", False, "Recent turns of the open conversation kept in the prompt"),
    # chat-agent(1.3.0):四条,全部默认关闭。开一条就多一层"她自己的选择",
    # 也多一次 LLM 往返 —— 所以点亮与否是世界作者的决定,不是引擎替他做的。
    "llm.background.model": ("", "str", "llm", False, "Cheap/fast model for the background slot (intent classifier, loop steps); empty = use llm.model"),
    "chat.stance.enabled": (False, "bool", "chat", False, "NPC picks an explicit relational stance before each reply (#18)"),
    "chat.tools.enabled": (False, "bool", "chat", False, "NPC can call capabilities mid-chat: mute / walk away / delay / refuse a topic / broadcast (#15)"),
    "chat.tools.max_mute_minutes": (1440.0, "float", "chat", False, "Upper bound on a single mute / topic refusal (minutes)"),
    "chat.tools.max_delay_minutes": (720.0, "float", "chat", False, "Upper bound on a single delay_reply (minutes)"),
    "chat.intent.enabled": (False, "bool", "chat", False, "Classify each player message: dialogue / narrative_direction / style_adjust (#16)"),
    "chat.intent.min_confidence": (0.6, "float", "chat", False, "Below this the classification falls back to dialogue"),
    "chat.loop.enabled": (False, "bool", "chat", False, "chat_burst keeps generating until the NPC yields (#17)"),
    "chat.loop.max_messages": (8, "int", "chat", False, "Hard cap on messages in one autonomous loop"),
    "chat.loop.max_tool_calls": (15, "int", "chat", False, "Hard cap on tool calls in one autonomous loop"),
    # autonomy:没人跟她说话时的定时轮次。要 chat.tools.enabled 一起点亮 ——
    # 没有能力可挑的轮次是一次白花的 LLM 调用。
    "autonomy.enabled": (False, "bool", "autonomy", False, "Ask each character every N ticks whether she wants to do something on her own"),
    "autonomy.interval_ticks": (72, "int", "autonomy", False, "World ticks between autonomy rounds (72 = 6 world hours at 5 min/tick)"),
    "autonomy.max_per_day": (2, "int", "autonomy", False, "How many times a day one character may act on her own"),
    # contact:她会不会想起一个**不在跟前**的玩家。和 autonomy 是两条路(候选集
    # 互补、额度两本账)—— 理由写在 `contact.py` 的模块说明里。
    # ⚠️ 它**不要求** `chat.tools.enabled`:这一层不挑动词,只发一条事件,
    # 没有 LLM 也成立(线索退回由头原文)。
    "contact.enabled": (False, "bool", "contact", False, "Characters may decide on their own that they want to reach a player who is not present"),
    "contact.interval_ticks": (24, "int", "contact", False, "World ticks between contact checks (24 = 2 world hours at 5 min/tick)"),
    "contact.min_closeness": (0.35, "float", "contact", False, "Weighted three-axis closeness below which she never thinks of him"),
    "contact.threshold": (0.25, "float", "contact", False, "Score (closeness x urge x readiness) needed to fire"),
    "contact.cooldown_ticks": (144, "int", "contact", False, "Minimum world ticks between two wants-contact events for one (agent, player)"),
    "contact.max_per_day": (2, "int", "contact", False, "How many times a day one character may want to reach one player"),
    "contact.fatigue": (1.7, "float", "contact", False, "Threshold multiplier per already-fired contact today (the decay)"),
    "contact.absence_ticks": (288, "int", "contact", False, "World ticks of silence before 'he has not been around' becomes a reason"),
    "contact.recent_ticks": (288, "int", "contact", False, "How fresh a memory must be to count as a reason"),
    "contact.initiative_stock": ("主动", "str", "contact", False, "Agent quantity that scales how readily she reaches out; undeclared = 1.0"),
    "contact.compose.enabled": (True, "bool", "contact", False, "Use the background LLM to write the one-line topic hint (falls back to the reason text)"),
    "memory.capacity": (50, "int", "memory", False, "Per-agent memory row cap before strength-based eviction"),
    "memory.sentiment_threshold": (0.3, "float", "memory", False, "Relationship-shift memory trigger threshold"),
    "memory.half_life_days": (3.0, "float", "memory", False, "Recency half-life for memory retrieval (world days)"),
    "memory.reflection_threshold": (3.0, "float", "memory", False, "Accumulated importance that triggers a reflection"),
    "needs.enabled": (False, "bool", "needs", False, "Need curves (energy/hunger/social) drive behavior"),
    "economy.enabled": (False, "bool", "economy", False, "Items, money, shops and price drift"),
    "economy.daily_wage": (20.0, "float", "economy", False, "Per-agent daily wage from the town treasury"),
    # 一个**世界自己声明的**量,把她的心气儿往下拖(熬夜攒的睡眠债是标准用法)。
    # 空 = 关,和 perception / ontology 同一条:声明本身就是开关。引擎不认识
    # 「睡眠债」,只认识"作者指着她身上的哪个量" —— 量怎么攒怎么还由规律写。
    "needs.mood_penalty_stock": (
        "", "str", "needs", False,
        "An agent quantity (0~1) subtracted from mood; empty = off. "
        "The world's rules decide how it accrues and how it is paid off",
    ),
    "social.enabled": (False, "bool", "social", False, "Gossip propagation and clique detection"),
    # 听到一句八卦之后她有没有反应(吃醋是其中一种)。**自成一个开关**,理由是
    # 代价:它是每落地一条八卦就多一次模型往返,而 `social.enabled` 那一层本身
    # 一次模型都不调。默认关,和引擎其余的开关一样;橱窗种子里点亮它。
    "social.hearsay_reaction.enabled": (
        False, "bool", "social", False,
        "After hearing gossip, ask the judge how it lands (jealousy and its seven siblings); "
        "one LLM round trip per rumor that lands",
    ),
    # 一起做事。**没有 `social.joint.enabled`** —— 声明本身就是开关(和 perception /
    # ontology 逐字同构):没有哪个 kind 写过 `participants` 的世界,这一层整个缺席,
    # 行为与从前逐位相同。这几个键是"点亮之后"的刻度,不是开关。
    "social.joint.consent_stock": (
        together.DEFAULT_CONSENT_STOCK, "str", "social", False,
        "Agent quantity that scales how readily he says yes to an invitation; undeclared = 1.0",
    ),
    "social.joint.min_willingness": (
        together.DEFAULT_MIN_WILLINGNESS, "float", "social", False,
        "Willingness (closeness x agreeableness x stance) needed to accept, "
        "when there is no invite judge to read his persona",
    ),
    "social.joint.relation_step": (
        together.DEFAULT_RELATION_STEP, "float", "social", False,
        "Base relationship step one shared experience is worth. Deliberately larger than "
        "DeterministicRelationshipJudge.STEP (0.04): things done together should move a "
        "relationship more than lines exchanged",
    ),
    "social.joint.full_duration_ticks": (
        together.DEFAULT_FULL_DURATION_TICKS, "int", "social", False,
        "How long together counts as 'the whole way' for the relationship effect "
        "(12 = one world hour at 5 min/tick)",
    ),
    # 异地就只能打电话。**默认关**,而这不是犹豫,是账:引擎侧收紧会当场打断线上
    # 世界 —— `player_move` 是宿主可选调用,今天线上根本没人调,于是"异地"是每一次
    # 调用的默认值,一开就是 give / 一起做事全线开始拒绝。迁移次序只能是
    # "先让宿主调 player_move,再开这个开关"(`anima-world presence` 就是查这个的)。
    "presence.enforce_colocation": (
        False, "bool", "presence", False,
        "Capabilities declared requires_colocation refuse when the player is not "
        "face to face with the character. Requires the host to call player_move",
    ),
}

# **创世不播默认值**(DB-SPLIT.md 移动 1)。播下去的那 36 行看着无害,坏处要过一个
# 版本才显形:引擎把 `chat.recall_k` 从 3 改成 99,已有的世界一个都吃不到,而
# `config list` 看上去一模一样 —— 照跑,但给的不是你以为的东西。
#
# 于是表里剩下的就是**作者的意见**,别的现场从 `_DEFAULTS` 取。这是加法兼容:
# 老引擎打开一个 `config` 空表的世界,会照它自己那套播回默认值再照常运行。
#
# 代价是真实的:今天"表里有一行"意味着值锁死了,对**可复现性**有好处 —— 同一个
# 世界文件在两个引擎版本上行为一致。需要可复现的场合,把值显式写进种子的
# `config` 块即可,那本来就是作者的意见。
