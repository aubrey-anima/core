"""First-run experience: LLM status, guided setup, human-readable diagnostics.

`serve` is the production entrypoint a world image runs and nothing here
changes it. This module backs the commands a *person* types — `start`,
`config`, `doctor` — where the job is to answer three questions before
anything else can matter:

- is an LLM actually configured (and does it answer)?
- where is this world, and was it just created?
- how fast does its clock run — because the packaged default is real time,
  and a world that moves 5 minutes per 5 minutes looks broken to a newcomer.

Every message here is written for someone who has never seen this package.
"""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from anima_world.world_time import MINUTES_PER_DAY

# ── output helpers ───────────────────────────────────────────────────────────

OK = "✓"
BAD = "✗"
WARN = "!"


def _color_enabled(stream: Any) -> bool:
    if os.getenv("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def paint(text: str, code: str, *, stream: Any = None) -> str:
    """ANSI-wrap `text`, but only for a real terminal (never in a pipe/CI log)."""
    if not _color_enabled(stream if stream is not None else sys.stdout):
        return text
    return f"\033[{code}m{text}\033[0m"


def dim(text: str) -> str:
    return paint(text, "2")


def bold(text: str) -> str:
    return paint(text, "1")


def green(text: str) -> str:
    return paint(text, "32")


def red(text: str) -> str:
    return paint(text, "31")


def yellow(text: str) -> str:
    return paint(text, "33")


def rule(title: str = "") -> str:
    """A left-rail heading. Deliberately no right border: CJK text is
    double-width and a box would never line up across terminals."""
    return f"\n  {bold(title)}\n  {dim('─' * 44)}" if title else f"  {dim('─' * 44)}"


# ── LLM status ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LLMStatus:
    """What the LLM stack will actually do when this world runs."""

    state: str  # "ok" | "unset" | "unreadable" | "no-store"
    summary: str  # one line, for a human
    fix: str | None  # the command that fixes it, when there is one
    model: str | None = None
    base_url: str | None = None
    masked_key: str | None = None

    @property
    def degraded(self) -> bool:
        return self.state != "ok"


def llm_status(config_store: Any | None, db_path: str | Path | None = None) -> LLMStatus:
    """Classify the LLM configuration into the three states that matter.

    `unset` and `unreadable` both read as "no key" everywhere downstream and
    both degrade the world to Mock — telling them apart is the whole point,
    because their fixes are opposites (configure one vs. restore a keyfile).
    """
    if config_store is None:
        return LLMStatus(
            state="no-store",
            summary="这个世界没有配置库(纯内存运行),LLM 不可用",
            fix=None,
        )

    undecryptable = getattr(config_store, "undecryptable_secrets", None)
    if callable(undecryptable) and "llm.api_key" in undecryptable():
        keyfile = f"{db_path}.key" if db_path else "<db>.key"
        return LLMStatus(
            state="unreadable",
            summary=f"llm.api_key 解不开 —— {keyfile} 多半没跟着 db 一起搬过来",
            fix="补回 keyfile,或 anima-world config set llm.api_key sk-…",
        )

    from anima_world.config_store import mask_secret

    api_key = config_store.get("llm.api_key", default="") or ""
    model = config_store.get("llm.model", default="") or None
    base_url = config_store.get("llm.base_url", default="") or None
    if not api_key:
        return LLMStatus(
            state="unset",
            summary="LLM 未配置 —— 叙事、空闲计划、关系判定都会降级成模板文本",
            fix="anima-world config set llm.api_key sk-…",
            model=model,
            base_url=base_url,
        )
    return LLMStatus(
        state="ok",
        summary=f"LLM 已配置({model or '未指定模型'})",
        fix=None,
        model=model,
        base_url=base_url,
        masked_key=mask_secret(api_key),
    )


def probe_llm(config_store: Any | None) -> str | None:
    """Actually call the configured LLM once. Returns an error string, or None.

    A configured key is not a working key. This is the only check that can
    tell you the difference before a world has been running for an hour on
    silently-failing calls.
    """
    from anima_world.llm_client import create_llm_client_from_config
    from anima_world.planner import SyncLLM

    if config_store is None:
        return "没有配置库 —— 纯内存世界没有 LLM 配置"
    api_key = config_store.get("llm.api_key", default="") or ""
    if not api_key:
        return "llm.api_key 是空的"
    try:
        reply = SyncLLM(
            create_llm_client_from_config(config_store), config_store=config_store
        ).complete_sync([{"role": "user", "content": "ping——只回复 pong"}])
    except Exception as exc:  # noqa: BLE001 - any probe failure means "not usable"
        return f"调用失败:{exc}"
    if not (reply or "").strip():
        return "调用成功但返回了空内容"
    return None


# ── world clock, in words ────────────────────────────────────────────────────


def _trim(value: float) -> str:
    """One decimal place, but never a bare `.0` ("24 小时", not "24.0 小时")."""
    return f"{value:.1f}".rstrip("0").rstrip(".")


def human_tick_rate(tick_rate: float, minutes_per_tick: int) -> str:
    """Describe a tick rate the way a person experiences it.

    `scheduler.tick_rate` is ticks per real second and `world.minutes_per_tick`
    is world minutes per tick; neither number tells you the thing you want to
    know, which is "will I see anything happen while I watch".
    """
    if tick_rate <= 0 or minutes_per_tick <= 0:
        return "时钟已停"
    seconds_per_tick = 1.0 / tick_rate
    ticks_per_day = MINUTES_PER_DAY / minutes_per_tick
    seconds_per_day = ticks_per_day * seconds_per_tick
    speedup = minutes_per_tick * 60.0 * tick_rate

    if seconds_per_tick <= 1:
        pace = f"{tick_rate:.3g} tick/秒"
    elif seconds_per_tick < 90:
        pace = f"每 {seconds_per_tick:.0f} 秒 1 tick"
    else:
        pace = f"每 {seconds_per_tick / 60:.0f} 分钟 1 tick"

    if seconds_per_day < 90:
        day = f"{seconds_per_day:.0f} 秒"
    elif seconds_per_day < 5400:
        day = f"{seconds_per_day / 60:.0f} 分钟"
    else:
        day = f"{_trim(seconds_per_day / 3600)} 小时"

    if 0.95 <= speedup <= 1.05:
        ratio = "与现实 1:1"
    elif speedup >= 1:
        ratio = f"现实时间的 {speedup:.0f} 倍速"
    else:
        ratio = f"现实时间的 1/{1 / speedup:.0f} 速"
    return f"{pace} —— 约 {day}走完一个世界日({ratio})"


# ── guided setup ─────────────────────────────────────────────────────────────

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
_LONGCAT_BASE_URL = "https://api.longcat.chat/openai/v1"


def can_prompt() -> bool:
    """Interactive setup needs a real terminal on both ends."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def configure_llm_interactively(
    config_store: Any,
    db_path: str | Path,
    *,
    input_fn: Callable[[str], str] | None = None,
    secret_input_fn: Callable[[str], str] | None = None,
) -> bool:
    """Ask for a key, base URL and model; store them encrypted. False = skipped.

    An empty key is a deliberate, supported answer ("show me Mock first"), not
    an error — so it must cost exactly one keypress.
    """
    import getpass

    from anima_world.config_store import mask_secret

    ask = input_fn or input
    ask_secret = secret_input_fn or getpass.getpass

    # getpass echoes nothing at all, so without saying so a paste looks like a
    # dead terminal — and it also discards type-ahead (TCSAFLUSH), so the key
    # must be typed *after* this line lands.
    print("     贴上 API key 回车即可(直接回车 = 先用 Mock 试跑,之后随时能配)")
    print(f"     {dim('输入不会显示出来,粘贴后直接回车')}")
    try:
        api_key = ask_secret("     > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not api_key:
        return False

    # Env vars are the other place a key normally lives; prefer their endpoint
    # over a blind OpenAI guess when one is set.
    default_base = (
        os.getenv("ANIMA_LLM_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or (_LONGCAT_BASE_URL if os.getenv("LONGCAT_API_KEY") else DEFAULT_BASE_URL)
    )
    default_model = os.getenv("ANIMA_LLM_MODEL") or os.getenv("OPENAI_MODEL") or DEFAULT_MODEL
    try:
        base_url = ask(f"     接口地址 [{default_base}]: ").strip() or default_base
        model = ask(f"     模型 [{default_model}]: ").strip() or default_model
    except (EOFError, KeyboardInterrupt):
        print()
        base_url, model = default_base, default_model

    config_store.set("llm.api_key", api_key)
    config_store.set("llm.base_url", base_url)
    config_store.set("llm.model", model)
    print(f"     {green(OK)} 已加密写入 {db_path}(密钥文件 {db_path}.key —— 搬 db 必须带上)")
    print(f"       {dim(mask_secret(api_key))}  {dim(model)}  {dim(base_url)}")
    return True


# ── misc ─────────────────────────────────────────────────────────────────────


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host if host != "0.0.0.0" else "", port))
        except OSError:
            return False
    return True


def find_free_port(host: str, preferred: int, span: int = 10) -> int | None:
    """The first free port at or after `preferred`. None when the whole span is taken."""
    for candidate in range(preferred, preferred + span):
        if port_is_free(host, candidate):
            return candidate
    return None


def browsable_url(host: str, port: int) -> str:
    """A URL that actually resolves in a browser — 0.0.0.0 does not."""
    shown = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    return f"http://{shown}:{port}"
