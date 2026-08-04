"""ConfigStore: runtime-tunable scalar configuration (M5).

Every value that used to be a Python constant or an env-var-only setting
(LLM credentials, scheduler timing, chat/memory thresholds) now lives in the
`config` table and is read live at the point of use, so an admin editing it
via `World.config_set` takes effect on the next call with no process restart (see
design.md D2/D3). Secret values (`is_secret=1`) are encrypted at rest with
Fernet; the encryption key lives in a sibling keyfile next to the database,
never in the database itself (design.md D4) — losing that keyfile makes
existing secrets unrecoverable, so a decrypt failure degrades to "unset"
rather than crashing the process.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_VALUE_TYPES = {"str", "int", "float", "bool"}


def has_keyfile(db_path: str | Path) -> bool:
    """这个世界原本有没有自己的密钥文件。

    **判据不是密文长什么样,是这个世界有没有过 keyfile。** 没有 keyfile 就说明
    它从来没存过真 secret(1.3.0 之后的世界都是这样),那么解不开只可能是解一个
    创世播下的空串 —— 那不是丢钥匙,报警是假警报。
    """
    return Path(str(db_path) + ".key").exists() or bool(os.getenv("ANIMA_SETTINGS_KEY"))


def load_or_create_key(db_path: str | Path, *, create: bool = True) -> bytes:
    """Resolve the Fernet key for secret config values.

    Precedence: `ANIMA_SETTINGS_KEY` env var > existing sibling keyfile >
    freshly generated keyfile. The keyfile is `<db_path>.key`, mode 0600,
    and MUST NOT be committed to version control (see `.gitignore`).

    `create=False` 时:没有 keyfile 就**不造一个**,返回一把只活在内存里的临时密钥。

    为什么要有这个开关 —— **世界里已经没有 secret 了。** `llm.api_key` 是唯一
    声明为密文的键,而它归了机器配置(`machine_config`)。于是一个新世界不再需要
    keyfile,而那条"**Fernet 密钥必须随 db 搬迁**,丢了就全线降级 Mock"的不变量
    整个不需要成立 —— 那条链的根就是把一把 API key 存进了世界文件。

    **旧世界照旧**:keyfile 还在就照读,里面那把旧钥匙解得开旧密文。
    """
    env_key = os.getenv("ANIMA_SETTINGS_KEY")
    if env_key:
        return env_key.encode()
    keyfile = Path(str(db_path) + ".key")
    if keyfile.exists():
        return keyfile.read_bytes()
    key = Fernet.generate_key()
    if not create:
        # 只活在这一个进程里:没有密文要存,也就没有东西需要下次解得开。
        return key
    keyfile.write_bytes(key)
    keyfile.chmod(0o600)
    return key


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
    """SQLite-backed, in-memory-cached store for typed scalar config values.

    All access is serialized through `lock` (defaults to its own RLock; the
    web server passes the scheduler's shared lock since the connection is
    shared across threads, same pattern as `MemoryStore`/`ChatStore`).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        fernet_key: bytes | None = None,
        lock: Any | None = None,
        had_keyfile: bool = True,
    ) -> None:
        self._conn = conn
        self._lock = lock if lock is not None else threading.RLock()
        self._fernet = Fernet(fernet_key) if fernet_key else None
        # 这个世界原本有没有自己的密钥文件。没有就说明它从来没存过真 secret
        # (1.3.0 之后的世界都是这样),那么"解不开"只可能是解一个创世播下的空串
        # —— 报"你的钥匙丢了"是假警报,而假警报会让人学会忽略真警报。
        # 缺省 True 是**保守的**:不知道来历就照旧报警,漏报比误报坏。
        self._had_keyfile = had_keyfile
        self._cache: dict[str, Any] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        # Secrets that are STORED but unreadable (almost always a lost keyfile).
        # They decode to None, which every caller reads as "unset" — without
        # this set there is no way to tell an unconfigured key from a broken
        # one, and the world silently degrades to Mock either way.
        self._undecryptable: set[str] = set()
        self._hydrate()

    def _hydrate(self) -> None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT key, value, value_type, category, is_secret, description FROM config"
            )
            rows = cur.fetchall()
        for key, raw_value, value_type, category, is_secret, description in rows:
            is_secret = bool(is_secret)
            self._meta[key] = {
                "value_type": value_type,
                "category": category,
                "is_secret": is_secret,
                "description": description,
            }
            self._cache[key] = self._decode(key, raw_value, value_type, is_secret)

    def _decode(self, key: str, raw_value: str, value_type: str, is_secret: bool) -> Any:
        if is_secret:
            if self._fernet is None:
                # 没有密钥又没有过 keyfile = 这个世界从来没存过真 secret(1.3.0 之后
                # 都是这样)。那不是"读不出来",是"本来就没有" —— 报警是噪音,
                # 而每次开机都来一遍的噪音会让人学会忽略真警告。
                if not self._had_keyfile:
                    return None
                logger.warning("config %s is a secret but no encryption key is configured", key)
                self._undecryptable.add(key)
                return None
            try:
                raw_value = self._fernet.decrypt(raw_value.encode()).decode()
            except InvalidToken:
                # **解不开一个空值不算丢了钥匙。** 创世会给每个 secret 键播一行
                # (加密过的空串),而新世界不再生成 keyfile —— 于是每次开机都会用
                # 一把临时钥匙去解那个空串,解不开。报"你的钥匙丢了"是假警报,
                # 而假警报会让人学会忽略真警报。
                if not self._had_keyfile:
                    return None
                logger.warning("config %s could not be decrypted (missing/corrupted keyfile)", key)
                self._undecryptable.add(key)
                return None
        return _coerce(raw_value, value_type)

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
            if is_secret:
                if self._fernet is None:
                    raise RuntimeError(f"cannot store secret '{key}': no encryption key configured")
                raw = self._fernet.encrypt(raw.encode()).decode()

            updated_at = datetime.now(timezone.utc).isoformat()
            self._conn.execute(
                "INSERT INTO config (key, value, value_type, category, is_secret, description, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, value_type=excluded.value_type, category=excluded.category, "
                "is_secret=excluded.is_secret, description=excluded.description, updated_at=excluded.updated_at",
                (key, raw, value_type, category, int(is_secret), description, updated_at),
            )
            self._conn.commit()
            self._meta[key] = {
                "value_type": value_type,
                "category": category,
                "is_secret": is_secret,
                "description": description,
            }
            self._cache[key] = value
            self._undecryptable.discard(key)  # rewritten under the current key

    def undecryptable_secrets(self) -> list[str]:
        """Secret keys that are stored but could not be read back.

        `get()` returns None for these, exactly as it does for a key that was
        never set — so callers see "unset" and degrade quietly. Boot checks and
        `World.state()` use this to tell the two apart and name the real cause
        (a `<db>.key` that did not travel with the database).
        """
        with self._lock:
            return sorted(self._undecryptable)

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
    "memory.capacity": (50, "int", "memory", False, "Per-agent memory row cap before strength-based eviction"),
    "memory.sentiment_threshold": (0.3, "float", "memory", False, "Relationship-shift memory trigger threshold"),
    "memory.half_life_days": (3.0, "float", "memory", False, "Recency half-life for memory retrieval (world days)"),
    "memory.reflection_threshold": (3.0, "float", "memory", False, "Accumulated importance that triggers a reflection"),
    "needs.enabled": (False, "bool", "needs", False, "Need curves (energy/hunger/social) drive behavior"),
    "economy.enabled": (False, "bool", "economy", False, "Items, money, shops and price drift"),
    "economy.daily_wage": (20.0, "float", "economy", False, "Per-agent daily wage from the town treasury"),
    "social.enabled": (False, "bool", "social", False, "Gossip propagation and clique detection"),
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
