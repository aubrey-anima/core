"""Async LLM client backed by the OpenAI SDK (M3.5 chat subsystem).

`LLMClient` wraps `openai.AsyncOpenAI` — streaming, retries/backoff, and
timeouts come from the SDK, and typed errors (`APITimeoutError`,
`RateLimitError`, …) propagate to callers rather than being swallowed.
`MockLLMClient` implements the same interface deterministically with no
network, for offline / no-key runs and hermetic tests.
"""

from __future__ import annotations

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
    """

    def __init__(self, config_store: Any, client_factory: Any = LLMClient) -> None:
        self._config_store = config_store
        self._client_factory = client_factory
        self._fingerprint: tuple[Any, ...] | None = None
        self._client: LLMClientProtocol | None = None

    def _resolve(self) -> LLMClientProtocol:
        cfg = self._config_store
        api_key = cfg.get("llm.api_key", default="") or ""
        base_url = cfg.get("llm.base_url", default="") or "https://api.openai.com/v1"
        model = cfg.get("llm.model", default="gpt-4o-mini")
        timeout = cfg.get("llm.timeout", default=_DEFAULT_TIMEOUT)
        max_retries = cfg.get("llm.max_retries", default=_DEFAULT_MAX_RETRIES)
        fingerprint = (api_key, base_url, model, timeout, max_retries)

        if self._client is None or fingerprint != self._fingerprint:
            if not api_key:
                self._client = MockLLMClient()
            else:
                self._client = self._client_factory(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    timeout=timeout,
                    max_retries=max_retries,
                )
            self._fingerprint = fingerprint
        return self._client

    async def stream(self, messages: Sequence[Message]) -> AsyncIterator[str]:
        async for token in self._resolve().stream(messages):
            yield token

    async def complete(self, messages: Sequence[Message]) -> str:
        return await self._resolve().complete(messages)


def create_llm_client_from_config(config_store: Any) -> LLMClientProtocol:
    """Build a `ConfigBackedLLMClient` that hot-reloads `llm.*` config."""
    return ConfigBackedLLMClient(config_store)


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
