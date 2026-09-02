"""流式的两把尺,以及「还没吐正文就断了」自动重来一次(3.9.0,收件箱 D51)。

病历(2026-09-02 线上量的):两次聊天都在吐出开头的 `〔stance:neutral〕` 三个分片
之后**停住 30 秒**,`ReadTimeout`,整轮作废;拿同一份提示词原样重放 4 次 **4/4 成功**。
坏在两处:① 首片延迟与分片空档共用一把 30 秒的尺;② SDK 的 `max_retries` 只在流
**开始之前**管用。

这一族要钉死的三件:

- **两把尺真的是两把** —— 首片给得宽、空档给得紧,而且各自说得出自己是哪一把。
- **重来的边界是"玩家看没看到字"**,不是"我 yield 过没有":那三片是控制标记,
  玩家一个字都看不到,而按"yield 过就不重来"判,最该重来的那一类永远不重来。
- **吐过正文就绝不重来** —— 重复的正文比一句"她没能接上话"更难解释。
"""
from __future__ import annotations

import asyncio

import pytest

from anima_world.llm_client import (
    LLMClient,
    _only_leading_markers,
    _is_retryable_stream_error,
)


class _Chunk:
    def __init__(self, text: str | None) -> None:
        class _D:
            content = text

        class _C:
            delta = _D()

        self.choices = [_C()] if text is not None else []


class _Stream:
    """一趟假流:按脚本吐分片,`None` 表示"在这儿停住"(睡到超时)。"""

    def __init__(self, script, stall_seconds: float = 5.0) -> None:
        self._script = list(script)
        self._stall = stall_seconds
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._script:
            raise StopAsyncIteration
        item = self._script.pop(0)
        if item is None:
            await asyncio.sleep(self._stall)
            raise AssertionError("不该走到这里:上面那一觉应该被超时打断")
        if isinstance(item, BaseException):
            raise item
        return _Chunk(item)

    async def close(self):
        self.closed = True


class _Completions:
    def __init__(self, streams) -> None:
        self._streams = list(streams)
        self.calls = 0
        self.timeouts: list[float] = []

    async def create(self, **kwargs):
        self.calls += 1
        self.timeouts.append(kwargs.get("timeout"))
        return self._streams.pop(0)


class _FakeSDK:
    def __init__(self, streams) -> None:
        self.chat = type("chat", (), {})()
        self.chat.completions = _Completions(streams)


def _client(streams, *, first=0.30, gap=0.05) -> LLMClient:
    return LLMClient(api_key="k", base_url="https://example.com/v1", model="m",
                     client=_FakeSDK(streams), first_timeout=first, gap_timeout=gap)


async def _drain(client) -> str:
    return "".join([piece async for piece in client.stream([{"role": "user", "content": "hi"}])])


# ── 那条线上的病,逐字复现 ────────────────────────────────────────────────────

def test_三片标记之后空档_重来一次_整轮成功():
    """线上 09-02 那两次的形状:`〔stance:neutral〕` 吐完就停住。"""
    stalled = _Stream(["〔stance", ":neutral", "〕", None])
    good = _Stream(["〔stance:neutral〕", "你好呀", ",今天有点冷。"])
    client = _client([stalled, good])
    out = asyncio.run(_drain(client))
    assert out == "〔stance:neutral〕你好呀,今天有点冷。"
    assert client._client.chat.completions.calls == 2, "该重来一次"
    assert stalled.closed, "断掉的那条流要放手"


def test_已经吐出正文之后空档_不重来_按今天的样子抛():
    """重复的正文比一句「她没能接上话」更难解释 —— 所以这一格有意不救。"""
    stalled = _Stream(["〔stance:neutral〕", "你好呀", None])
    spare = _Stream(["不该用到我"])
    client = _client([stalled, spare])

    async def go():
        seen = []
        with pytest.raises(TimeoutError):
            async for piece in client.stream([{"role": "user", "content": "hi"}]):
                seen.append(piece)
        return seen

    seen = asyncio.run(go())
    assert "".join(seen) == "〔stance:neutral〕你好呀", "已经吐出去的一个字都不许丢"
    assert client._client.chat.completions.calls == 1, "吐过正文就不许重来"


def test_两把尺是两把_而且说得出自己是哪一把():
    # 首片就停住 → 这一趟的预算是宽的那把;两趟都停 → 只重来一次,然后抛。
    client = _client([_Stream([None]), _Stream([None])], first=0.20, gap=0.02)
    with pytest.raises(TimeoutError) as first_err:
        asyncio.run(_drain(client))
    assert "首片" in str(first_err.value)
    assert client._client.chat.completions.calls == 2, "重来一次,不是两次"

    client2 = _client([_Stream(["你好", None])], first=0.20, gap=0.02)
    with pytest.raises(TimeoutError) as gap_err:
        asyncio.run(_drain(client2))
    assert "分片空档" in str(gap_err.value)
    assert client2._client.chat.completions.calls == 1


def test_空档那把尺真的比首片紧():
    """睡 0.10 秒:首片(0.30)等得起,空档(0.05)等不起 —— 一趟里两种结局。"""
    client = _client([_Stream(["先", None], stall_seconds=0.10),
                      _Stream(["先", None], stall_seconds=0.10)],
                     first=0.30, gap=0.05)
    with pytest.raises(TimeoutError) as err:
        asyncio.run(_drain(client))
    assert "分片空档" in str(err.value), "第一片是按宽的那把过的,断的是紧的那把"


def test_请求自己的超时放宽到两把尺之外():
    """不放宽的话 30 秒的 read timeout 和 30 秒的首片预算是竞态,拆分就白做了。"""
    client = _client([_Stream(["嗯"])], first=30.0, gap=15.0)
    asyncio.run(_drain(client))
    assert client._client.chat.completions.timeouts == [35.0]


def test_只有标记没有正文的一轮_照样把标记交出去():
    client = _client([_Stream(["〔wait〕"])])
    assert asyncio.run(_drain(client)) == "〔wait〕"


def test_攒过了头就当成正文_不再等一个闭括号():
    """散文里手写的一个 `〔` 不该让整轮被扣住 —— 上限兜的就是"我认错了"。"""
    long_prose = "〔" + "她愣了一下," * 60
    client = _client([_Stream([long_prose, None]), _Stream(["不该用到我"])])
    async def go():
        seen = []
        with pytest.raises(TimeoutError):
            async for piece in client.stream([{"role": "user", "content": "hi"}]):
                seen.append(piece)
        return "".join(seen)
    assert asyncio.run(go()) == long_prose
    assert client._client.chat.completions.calls == 1, "攒过了头 = 已经算正文,不重来"


# ── 两个判断各自的真值表 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("", True),
    ("  \n ", True),
    ("〔stance:neutral〕", True),
    ("〔stance:neutral〕\n〔wait〕", True),
    ("〔stance", True),                       # 还没闭合 —— 可能是半条标记
    ("[stance:neutral]", True),               # 输入法把 〔〕 打成 []
    ("〔stance:neutral〕你好", False),
    ("你好〔stance:neutral〕", False),
    ("你好", False),
])
def test_还没有正文这个判断的真值表(text, expected):
    assert _only_leading_markers(text) is expected


def test_限流不算可重来的():
    """重来一次只会更快撞第二次 —— 这一格是有意留在外面的。"""
    class RateLimitError(Exception):
        pass

    class APITimeoutError(Exception):
        pass

    assert _is_retryable_stream_error(APITimeoutError("x")) is True
    assert _is_retryable_stream_error(TimeoutError("x")) is True
    assert _is_retryable_stream_error(RateLimitError("x")) is False
    assert _is_retryable_stream_error(ValueError("x")) is False


def test_两个新键在机器配置那张表上():
    """它们属于这台机器不属于任何世界 —— 和 `llm.timeout` 逐字同一条判据。"""
    from anima_world.config_store import _DEFAULTS
    from anima_world.machine_config import ENV_ALIASES, MACHINE_KEYS

    for key in ("llm.stream.first_timeout", "llm.stream.gap_timeout"):
        assert key in MACHINE_KEYS
        assert key in ENV_ALIASES
        assert key in _DEFAULTS
    assert _DEFAULTS["llm.stream.first_timeout"][0] == 30.0, "有意不收紧"
    assert _DEFAULTS["llm.stream.gap_timeout"][0] == 15.0


def test_改了那两个键_缓存里的客户端要换掉():
    """屏幕上说改了、行为没改,是这个仓库最怕的那种。"""
    from anima_world.llm_client import ConfigBackedLLMClient

    class _Store:
        def __init__(self) -> None:
            self.rows = {"llm.api_key": "k", "llm.stream.gap_timeout": 15.0}

        def get(self, key, default=None):
            return self.rows.get(key, default)

    store = _Store()
    made: list[float] = []

    def factory(**kwargs):
        made.append(kwargs["gap_timeout"])
        return object()

    backed = ConfigBackedLLMClient(store, client_factory=factory)
    backed._resolve()
    backed._resolve()
    assert made == [15.0], "配置没变就别重连"
    store.rows["llm.stream.gap_timeout"] = 3.0
    backed._resolve()
    assert made == [15.0, 3.0]
