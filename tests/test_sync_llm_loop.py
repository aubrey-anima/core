"""同一个 LLM 门,从宿主那侧推开时不该是空的。

## 这个 bug 长什么样

`SyncLLM.complete_sync` 从前一律 `asyncio.run()`。引擎自己那几条线程上没有事件
循环,所以它一直是对的;而**跟着一次玩家请求走的判定**不在那几条线程上 —— 宿主的
请求处理器是 `async def`,那条线程上已经有一个跑着的循环,`asyncio.run` 在那里是
`RuntimeError`。

抛出去的下场不是报错。上游一律 `except Exception`(理由正当:一个死掉的 LLM 不该
停住世界),于是:

- 每一次玩家发出的邀请都退回确定性那条启发式,
- 而降级理由写的是"多半是没配 key" —— 配了 key 的世界也永远走不到模型那一路,
- 一个刚进来的访客对谁都是 0.0 分,门槛 0.2:**他请不动任何人做任何事**。

线上那个世界的 33MB LLM 日志里,邀请提示词出现过 0 次。

## 这个文件守的

两头都验:没有循环的那条路照旧(**它是引擎自己天天走的那条**),有循环时借一条
线程跑完再回来。第三条从 `judge_invite` 那一头验同一件事 —— 中间隔着那句
`except Exception`,只验底下那层的话,以后有人把它改回去,这里照绿。
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from anima_world.planner import SyncLLM
from anima_world.relationship_judge import RelationshipJudge


class _Client:
    """记下自己是在哪条线程上被 await 的。"""

    def __init__(self, reply: str = "好") -> None:
        self.reply = reply
        self.calls: list[list[dict[str, str]]] = []
        self.thread: str | None = None

    async def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        self.thread = threading.current_thread().name
        return self.reply


class _Hangs:
    async def complete(self, messages: list[dict[str, str]]) -> str:
        await asyncio.sleep(3600)
        return ""


def _inside_a_running_loop(fn):
    """照宿主那侧的样子调一次:线程上有一个正在跑的循环。"""

    async def _main():
        return fn()

    return asyncio.run(_main())


def test_没有循环时照旧():
    """引擎自己的判定线程走的是这条,它一个字都不该变。"""
    client = _Client("行")
    assert SyncLLM(client).complete_sync([{"role": "user", "content": "在吗"}]) == "行"
    assert client.calls == [[{"role": "user", "content": "在吗"}]]


def test_线程上有循环时借一条线程跑_不抛():
    client = _Client("行")
    out = _inside_a_running_loop(
        lambda: SyncLLM(client).complete_sync([{"role": "user", "content": "在吗"}])
    )
    assert out == "行"
    assert client.thread != threading.main_thread().name, "得是另一条线程上 await 的"


def test_借来的线程也有超时_不是一个无限的等():
    """超时是这条路唯一的上限。没有它的话,一个不回话的端点会把宿主的请求线程
    永远占住 —— 比抛异常坏得多。"""
    llm = SyncLLM(_Hangs(), timeout=0.05)
    with pytest.raises(TimeoutError):
        _inside_a_running_loop(lambda: llm.complete_sync([{"role": "user", "content": "在吗"}]))


def test_玩家邀请得着人_而不是安静地退回启发式():
    """真正被这个 bug 打中的是这一头,而中间隔着一句 `except Exception`:
    只验底下那层的话,以后有人把它改回去,这里照绿。"""
    client = _Client('{"accept": true, "reason": "反正闲着"}')
    judge = RelationshipJudge(llm=SyncLLM(client))
    verdict = _inside_a_running_loop(
        lambda: judge.judge_invite(
            {"name": "苏晚夏", "personality": "开朗"},
            inviter="阿布", invitation="一起擦窗",
            relation={"a_to_b": 0.0}, memories=[], location="cafe",
        )
    )
    assert verdict is not None, "判定没产出 = 退回确定性启发式,而生人在那条路上永远是 0 分"
    assert verdict.accept is True
    assert verdict.reason == "反正闲着"
