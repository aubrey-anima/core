"""Async LLM client backed by the OpenAI SDK (M3.5 chat subsystem).

`LLMClient` wraps `openai.AsyncOpenAI` — streaming, retries/backoff, and
timeouts come from the SDK, and typed errors (`APITimeoutError`,
`RateLimitError`, …) propagate to callers rather than being swallowed.
`MockLLMClient` implements the same interface deterministically with no
network, for offline / no-key runs and hermetic tests.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, AsyncIterator, Protocol, Sequence, runtime_checkable

Message = dict[str, str]

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_RETRIES = 2
_MOCK_CHUNK_SIZE = 4


@runtime_checkable
class LLMClientProtocol(Protocol):
    """Common interface for real and mock LLM clients."""

    async def stream(self, messages: Sequence[Message]) -> AsyncIterator[str]:
        ...

    async def complete(self, messages: Sequence[Message]) -> str:
        ...


def _disable_thinking_extra_body(base_url: str) -> dict[str, Any]:
    """LongCat's reasoning models default to slow "deep thinking"; turn it off.

    Only applied when the base URL is LongCat's, since other OpenAI-compatible
    vendors (including real OpenAI) may reject an unrecognized top-level field.
    """
    if "longcat" in base_url.lower():
        return {"thinking": {"type": "disabled"}}
    return {}


def _normalize_base_url(base_url: str) -> str:
    """Return the API root the SDK expects.

    Env config (and start-web.sh) may carry a full chat-completions URL like
    ``.../v1/chat/completions``; the SDK wants the ``.../v1`` root and appends
    the path itself. Strip a trailing ``/chat/completions`` and any trailing
    slash.
    """
    url = base_url.rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    return url.rstrip("/")


class MockLLMClient:
    """Deterministic, network-free client for offline/no-key/test use."""

    def __init__(self, reply: str | None = None) -> None:
        self._reply = reply

    def _resolve(self, messages: Sequence[Message]) -> str:
        if self._reply is not None:
            return self._reply
        last_user = ""
        for m in reversed(list(messages)):
            if m.get("role") == "user":
                last_user = m.get("content", "")
                break
        return f"收到：{last_user}"

    async def stream(self, messages: Sequence[Message]) -> AsyncIterator[str]:
        text = self._resolve(messages)
        for i in range(0, len(text), _MOCK_CHUNK_SIZE):
            yield text[i : i + _MOCK_CHUNK_SIZE]

    async def complete(self, messages: Sequence[Message]) -> str:
        return self._resolve(messages)


class LLMClient:
    """Async OpenAI-compatible client wrapping `openai.AsyncOpenAI`."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        temperature: float = 0.7,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self._extra_body = _disable_thinking_extra_body(base_url)
        if client is not None:
            self._client = client
        else:
            import httpx
            from openai import AsyncOpenAI

            # trust_env=False: ignore ambient proxy env (e.g. a socks:// ALL_PROXY
            # the SDK's httpx would otherwise choke on). LongCat is reached
            # directly, matching the prior urllib behavior.
            http_client = httpx.AsyncClient(trust_env=False, timeout=timeout)
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=_normalize_base_url(base_url),
                timeout=timeout,
                max_retries=max_retries,
                http_client=http_client,
            )

    async def stream(self, messages: Sequence[Message]) -> AsyncIterator[str]:
        resp = await self._client.chat.completions.create(
            model=self.model,
            messages=list(messages),
            temperature=self.temperature,
            stream=True,
            extra_body=self._extra_body,
        )
        async for chunk in resp:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    async def complete(self, messages: Sequence[Message]) -> str:
        resp = await self._client.chat.completions.create(
            model=self.model,
            messages=list(messages),
            temperature=self.temperature,
            extra_body=self._extra_body,
        )
        if not resp.choices:
            return ""
        return resp.choices[0].message.content or ""


class ConfigBackedLLMClient:
    """`LLMClientProtocol` implementation that reads `llm.*` from a
    `ConfigStore` live on every call (M5), rebuilding the underlying client
    only when the fingerprint of api_key/base_url/model/timeout/max_retries
    changes since the last call — an admin editing config via `World.config_set` takes
    effect on the next call with no process restart, but an unchanged
    config reuses the existing client instead of reconnecting every time.

    chat-agent(1.3.0):`model_key` 让**背景槽**成为同一个类的一个参数 —— 意图
    分类器与 autonomous loop 的每一步该走便宜快模型,而不是占着聊天那一条。
    留空(默认)时逐字等于 1.2 的行为:背景槽读不到自己的模型就用主模型。
    """

    def __init__(
        self,
        config_store: Any,
        client_factory: Any = LLMClient,
        *,
        model_key: str | None = None,
    ) -> None:
        self._config_store = config_store
        self._client_factory = client_factory
        self._model_key = model_key
        # (配置指纹, 事件循环) -> (循环, 客户端)。**循环也是键的一部分**:底层是一个
        # httpx.AsyncClient,连接池绑在创建它的那个循环上。而同步门面每调一次就新建
        # 一个循环(`asyncio.run`),于是一个被缓存住的客户端会被后面每一个循环复用
        # —— 轻则每轮泄一条连接并刷 "Task was destroyed / aclose was never awaited",
        # 重则某天开始报 "Event loop is closed"。2026-07-29 用真模型跑一局时,
        # 每一轮都在刷这两条。
        self._clients: dict[tuple[Any, ...], tuple[Any, LLMClientProtocol]] = {}

    @staticmethod
    def _running_loop() -> Any:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def _prune(self) -> None:
        """丢掉属于已经关掉的循环的条目 —— 否则每调一次就攒一个。"""
        for key, (loop, _) in list(self._clients.items()):
            if loop is not None and loop.is_closed():
                self._clients.pop(key, None)

    def _resolve(self) -> LLMClientProtocol:
        cfg = self._config_store
        api_key = cfg.get("llm.api_key", default="") or ""
        base_url = cfg.get("llm.base_url", default="") or "https://api.openai.com/v1"
        model = cfg.get("llm.model", default="gpt-4o-mini")
        if self._model_key:
            model = cfg.get(self._model_key, default="") or model
        timeout = cfg.get("llm.timeout", default=_DEFAULT_TIMEOUT)
        max_retries = cfg.get("llm.max_retries", default=_DEFAULT_MAX_RETRIES)
        loop = self._running_loop()
        key = (api_key, base_url, model, timeout, max_retries, id(loop) if loop else None)

        self._prune()
        entry = self._clients.get(key)
        if entry is not None:
            return entry[1]
        if not api_key:
            client: LLMClientProtocol = MockLLMClient()
        else:
            client = self._client_factory(
                api_key=api_key,
                base_url=base_url,
                model=model,
                timeout=timeout,
                max_retries=max_retries,
            )
        self._clients[key] = (loop, client)
        return client

    async def stream(self, messages: Sequence[Message]) -> AsyncIterator[str]:
        async for token in self._resolve().stream(messages):
            yield token

    async def complete(self, messages: Sequence[Message]) -> str:
        return await self._resolve().complete(messages)


def create_llm_client_from_config(config_store: Any) -> LLMClientProtocol:
    """Build a `ConfigBackedLLMClient` that hot-reloads `llm.*` config."""
    return ConfigBackedLLMClient(config_store)


def create_background_llm_client_from_config(config_store: Any) -> LLMClientProtocol:
    """背景槽的客户端:同一把 key / 同一个端点,模型读 `llm.background.model`。

    分类器(#16)与 loop 的每一步(#17)一轮要打好几次,用主模型既慢又贵;而没有配
    背景模型时它退回主模型 —— 便宜快模型是优化,不是前置条件。
    """
    return ConfigBackedLLMClient(config_store, model_key="llm.background.model")


def create_llm_client_from_env() -> LLMClientProtocol:
    """Build the configured client, or a `MockLLMClient` when no key is set.

    Env vars: `ANIMA_LLM_API_KEY`/`OPENAI_API_KEY`/`LONGCAT_API_KEY`,
    `ANIMA_LLM_BASE_URL`/`OPENAI_BASE_URL`, `ANIMA_LLM_MODEL`/`OPENAI_MODEL`.
    """
    longcat_key = os.getenv("LONGCAT_API_KEY")
    api_key = os.getenv("ANIMA_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or longcat_key
    if not api_key:
        return MockLLMClient()
    base_url = (
        os.getenv("ANIMA_LLM_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or ("https://api.longcat.chat/openai/v1" if longcat_key else "https://api.openai.com/v1")
    )
    model = (
        os.getenv("ANIMA_LLM_MODEL")
        or os.getenv("OPENAI_MODEL")
        or ("LongCat-2.0" if longcat_key else "gpt-4o-mini")
    )
    return LLMClient(api_key=api_key, base_url=base_url, model=model)
