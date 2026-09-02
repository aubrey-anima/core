"""Async LLM client backed by the OpenAI SDK (M3.5 chat subsystem).

`LLMClient` wraps `openai.AsyncOpenAI` — streaming, retries/backoff, and
timeouts come from the SDK, and typed errors (`APITimeoutError`,
`RateLimitError`, …) propagate to callers rather than being swallowed.
`MockLLMClient` implements the same interface deterministically with no
network, for offline / no-key runs and hermetic tests.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator, Protocol, Sequence, runtime_checkable

from anima_world.directives import ASCII_CLOSE, ASCII_OPEN, CLOSE, OPEN

logger = logging.getLogger(__name__)

Message = dict[str, str]

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_RETRIES = 2
_MOCK_CHUNK_SIZE = 4

# ── 流式的两把尺(3.9.0,收件箱 D51)────────────────────────────────────────────
#
# 从前只有一把:`llm.timeout` 同时管**第一片什么时候到**和**片与片之间能空多久**,
# 因为 httpx 的 read timeout 是**每次读**都重新计时的。而这两件事根本不是一回事:
#
# - **首片**里包着整个 prefill(几千字的提示词要先读完),慢是正常的;
# - **首片之后**的空档,在任何一家 OpenAI 兼容端点上都不该超过几秒 —— 它不是"慢",
#   是**卡住了**。
#
# 合成一把的下场 2026-09-02 在线上量到了:两次聊天都在吐出开头的
# `〔stance:neutral〕` 三个分片之后**停住 30 秒**,`ReadTimeout`,整轮作废,
# 玩家看到的是站点那句「她没能接上话」。而拿同一份提示词、同样 30 秒超时重放 4 次,
# **4/4 成功**(3–4 秒答完)—— 供应商抖了一下,而我们没有兜底。
#
# ⚠️ **两个默认值的理由不一样,别一起调**:
# - `first`= **30.0**,和从前的 `llm.timeout` 逐字相同 —— **有意不收紧**。首片慢的
#   世界今天跑得好好的,而收紧一个超时是"看不见的破坏":它只在别人的机器上、
#   别人的提示词长度上发作。
# - `gap` = **15.0**,**比从前紧一半**。敢收紧是因为这一格的语义变了:超时落在
#   "还没吐正文"那一段时**会自动重来一次**,所以更早发现 = 玩家更早拿到答案,
#   而不是更早拿到失败。落在正文之后的话,今天也一样是半截回复,只是不必再干等 15 秒。
_DEFAULT_STREAM_FIRST_TIMEOUT = 30.0
_DEFAULT_STREAM_GAP_TIMEOUT = 15.0
# 还没吐正文时最多攒住多少字符。攒住是为了**认出"这一片不是正文"** —— 见
# `_only_leading_markers`。上限存在的理由是"万一我认错了,也只压后这么多字"。
_RETRY_HOLD_LIMIT = 256


def _only_leading_markers(text: str) -> bool:
    """这段文字到此为止**还没有正文** —— 只有空白和 `〔…〕` / `[…]` 标记。

    🔴 **这个判断是"能不能重来一次"的全部依据,所以它必须比"我 yield 过东西了吗"
    更细。** 线上那两次挂掉时,流已经吐出过三片 —— 而那三片是
    `〔stance:neutral〕`,一个**玩家根本看不到**的控制标记(`directives.py` 会把它
    吃掉)。按"吐过就不重来"判,恰恰是最该重来的那一类永远不重来。

    未闭合的 `〔` 算**还在标记里**(返回 True):它可能是半条标记,而
    `_RETRY_HOLD_LIMIT` 兜住"它其实是散文里的一个括号"那种情形。
    `[` 同样算 —— 输入法把 `〔〕` 打成 `[]` 是 `directives` 已经认了的一种写法,
    两边对同一个符号给不同答案的话,能重来的判断和能解析的判断就分叉了。
    """
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == OPEN or ch == ASCII_OPEN:
            closer = CLOSE if ch == OPEN else ASCII_CLOSE
            end = text.find(closer, i + 1)
            if end < 0:
                return True
            i = end + 1
            continue
        return False
    return True


def _is_retryable_stream_error(exc: BaseException) -> bool:
    """这条流是**断在半路**(可以重来),还是模型/我们自己不对(重来也一样)?

    **按类名认,不 import openai**:引擎在没有 key 的世界里跑着 Mock,而测试拿的是
    假客户端 —— 为了一个判断把 SDK 拽进导入链,等于让最常见的那条路依赖最少用的那个包。
    `RateLimitError` **有意不在里面**:重来一次只会更快撞第二次。
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(names & {"APITimeoutError", "APIConnectionError", "ReadTimeout",
                         "ConnectError", "RemoteProtocolError"})


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
        first_timeout: float | None = None,
        gap_timeout: float | None = None,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self._extra_body = _disable_thinking_extra_body(base_url)
        # 两把尺各自的回落**不一样**:首片没配就是从前那把(`llm.timeout`),
        # 空档没配就是它自己的默认值 —— 让空档跟着 `llm.timeout` 走,等于把这次
        # 拆分又合回去,而一个把 `llm.timeout` 调到 120 的世界正是最需要拆的那个。
        self.first_timeout = float(first_timeout if first_timeout is not None else timeout)
        self.gap_timeout = float(
            gap_timeout if gap_timeout is not None else _DEFAULT_STREAM_GAP_TIMEOUT
        )
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

    async def _open_stream(self, messages: Sequence[Message]) -> Any:
        # 请求自己的超时放宽到**两把尺里大的那把再加一点**,让下面那两个
        # `wait_for` 成为真正说话的那一个。不放宽的话,30 秒的 httpx read timeout
        # 会和 30 秒的首片预算打平手,而谁先响是竞态 —— 那样"首片超时"和"空档超时"
        # 在日志上又变成同一句话,这次拆分就白做了。
        return await self._client.chat.completions.create(
            model=self.model,
            messages=list(messages),
            temperature=self.temperature,
            stream=True,
            extra_body=self._extra_body,
            timeout=max(self.first_timeout, self.gap_timeout) + 5.0,
        )

    async def _stream_once(self, messages: Sequence[Message]) -> AsyncIterator[str]:
        """一趟流,**两把尺各自计时**。超时按哪一把响,抛的都是 `TimeoutError`。"""
        resp = await self._open_stream(messages)
        first = True
        try:
            it = resp.__aiter__()
            while True:
                budget = self.first_timeout if first else self.gap_timeout
                try:
                    chunk = await asyncio.wait_for(it.__anext__(), timeout=budget)
                except StopAsyncIteration:
                    return
                except (asyncio.TimeoutError, TimeoutError) as exc:
                    where = "首片" if first else "分片空档"
                    raise TimeoutError(
                        f"流式响应在「{where}」上等了 {budget} 秒还没有下一片"
                    ) from exc
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    first = False
                    yield delta
        finally:
            # 半路松手就把连接放掉。**吞掉这里的异常**:关一条已经断了的流失败,
            # 不该盖掉上面那个真正的原因(而调用方要读的正是那个原因)。
            closer = getattr(resp, "close", None) or getattr(resp, "aclose", None)
            if closer is not None:
                try:
                    result = closer()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:  # noqa: BLE001
                    pass

    async def stream(self, messages: Sequence[Message]) -> AsyncIterator[str]:
        """流式,**还没吐出正文时断掉的话自动重来一次**(3.9.0,收件箱 D51)。

        SDK 自己的 `max_retries` 在这条路上帮不上忙:它只在**流开始之前**管用,
        而线上那两次是流开始之后停住的。

        🔴 **重来的边界是"玩家有没有看到过字",不是"我有没有 yield 过"** ——
        见 `_only_leading_markers`。已经吐出正文之后**绝不重来**:那会把同一段话
        说两遍,而重复的正文比一句"她没能接上话"更难解释。
        """
        attempt = 0
        while True:
            committed = False
            held: list[str] = []
            try:
                async for piece in self._stream_once(messages):
                    if committed:
                        yield piece
                        continue
                    held.append(piece)
                    text = "".join(held)
                    # 攒到这里算数了:要么这一片带来了正文,要么攒过了头(我认错了)。
                    if len(text) > _RETRY_HOLD_LIMIT or not _only_leading_markers(text):
                        committed = True
                        held.clear()
                        yield text          # 一次交出去,**顺序与内容逐字不变**
                if held:
                    yield "".join(held)     # 整轮只有标记,没有正文
                return
            except Exception as exc:  # noqa: BLE001 - 下面按可重来与否分流
                if committed or attempt >= 1 or not _is_retryable_stream_error(exc):
                    raise
                attempt += 1
                logger.warning(
                    "流式响应还没吐出正文就断了(%s),重来一次:%s",
                    type(exc).__name__, exc,
                )

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
        first_timeout = cfg.get(
            "llm.stream.first_timeout", default=_DEFAULT_STREAM_FIRST_TIMEOUT)
        gap_timeout = cfg.get(
            "llm.stream.gap_timeout", default=_DEFAULT_STREAM_GAP_TIMEOUT)
        loop = self._running_loop()
        # **两把新尺也进指纹** —— 不进的话 `config set llm.stream.gap_timeout` 之后
        # 缓存里那个客户端原样被复用,而 `config list` 上那个数已经变了:
        # 屏幕说改了、行为没改,一处不报错。
        key = (api_key, base_url, model, timeout, max_retries,
               first_timeout, gap_timeout, id(loop) if loop else None)

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
                first_timeout=first_timeout,
                gap_timeout=gap_timeout,
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
