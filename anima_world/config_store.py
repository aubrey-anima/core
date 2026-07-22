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


def load_or_create_key(db_path: str | Path) -> bytes:
    """Resolve the Fernet key for secret config values.

    Precedence: `ANIMA_SETTINGS_KEY` env var > existing sibling keyfile >
    freshly generated keyfile. The keyfile is `<db_path>.key`, mode 0600,
    and MUST NOT be committed to version control (see `.gitignore`).
    """
    env_key = os.getenv("ANIMA_SETTINGS_KEY")
    if env_key:
        return env_key.encode()
    keyfile = Path(str(db_path) + ".key")
    if keyfile.exists():
        return keyfile.read_bytes()
    key = Fernet.generate_key()
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
    ) -> None:
        self._conn = conn
        self._lock = lock if lock is not None else threading.RLock()
        self._fernet = Fernet(fernet_key) if fernet_key else None
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
                logger.warning("config %s is a secret but no encryption key is configured", key)
                self._undecryptable.add(key)
                return None
            try:
                raw_value = self._fernet.decrypt(raw_value.encode()).decode()
            except InvalidToken:
                logger.warning("config %s could not be decrypted (missing/corrupted keyfile)", key)
                self._undecryptable.add(key)
                return None
        return _coerce(raw_value, value_type)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            return default

    def set(
        self,
        key: str,
        value: Any,
        value_type: str | None = None,
        category: str | None = None,
        is_secret: bool | None = None,
        description: str | None = None,
    ) -> None:
        with self._lock:
            existing = self._meta.get(key)
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
        with self._lock:
            return key in self._meta

    def meta(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            meta = self._meta.get(key)
            return dict(meta) if meta is not None else None

    def list(self, category: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = []
            for key, meta in self._meta.items():
                if category is not None and meta["category"] != category:
                    continue
                items.append(
                    {
                        "key": key,
                        "value": self._cache.get(key),
                        "value_type": meta["value_type"],
                        "category": meta["category"],
                        "is_secret": meta["is_secret"],
                        "description": meta["description"],
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
    "memory.capacity": (50, "int", "memory", False, "Per-agent memory row cap before LRU eviction"),
    "memory.sentiment_threshold": (0.3, "float", "memory", False, "Relationship-shift memory trigger threshold"),
}

# env vars consulted only during first-boot seeding of llm.* keys (design.md D5)
_ENV_SEED = {
    "llm.api_key": ("ANIMA_LLM_API_KEY", "OPENAI_API_KEY", "LONGCAT_API_KEY"),
    "llm.base_url": ("ANIMA_LLM_BASE_URL", "OPENAI_BASE_URL"),
    "llm.model": ("ANIMA_LLM_MODEL", "OPENAI_MODEL"),
}


def _seed_defaults(store: ConfigStore, *, category_filter: str | None = None) -> None:
    longcat_only = bool(os.getenv("LONGCAT_API_KEY")) and not (
        os.getenv("ANIMA_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    )
    for key, (default, value_type, category, is_secret, description) in _DEFAULTS.items():
        if category_filter is not None and category != category_filter:
            continue
        if store.has(key):
            continue
        value = default
        for env_name in _ENV_SEED.get(key, ()):
            env_value = os.getenv(env_name)
            if env_value:
                value = env_value
                break
        if longcat_only and key == "llm.base_url" and not value:
            value = "https://api.longcat.chat/openai/v1"
        elif longcat_only and key == "llm.model" and value == default:
            value = "LongCat-2.0"
        store.set(
            key,
            value,
            value_type=value_type,
            category=category,
            is_secret=is_secret,
            description=description,
        )


def seed_defaults(store: ConfigStore) -> None:
    """Idempotently seed any config key that doesn't already have a row.

    `llm.*` keys prefer an existing env var over the hardcoded default; every
    other key seeds straight from its hardcoded default. Once a row exists,
    this is a no-op for that key on every subsequent call — the DB value
    always wins from then on (design.md D5).
    """
    _seed_defaults(store)


def seed_llm_defaults(store: ConfigStore) -> None:
    """Seed only platform-owned LLM settings into an independent database."""
    _seed_defaults(store, category_filter="llm")
