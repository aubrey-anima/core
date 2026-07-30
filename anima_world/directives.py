"""角色在回复流里夹带的控制指令 —— stance、tool_call、让位,一个解析器全收。

为什么走行内标记而不是 OpenAI 的 `tools=` 字段(issue #15 开放问题 1):

- **默认状态必须成立。** 没有 key 时世界跑在 Mock 上,而本地 ollama、若干国产
  OpenAI 兼容端点的 function calling 支持参差。如果 tool_call 只在原生 tools
  上可用,那"角色能选择走开"这件事在默认状态下就是缺席的 —— README 承诺的那一屏
  又变成了兑现不了的那一屏。
- **流式不能退化。** 原生 tool_call 要等整条响应成形才知道调了什么;行内标记随
  token 流过来,散文照旧一个字一个字吐给玩家。
- 一个解析器、一份测试面。原生 tools 是 v2 的事,那时这里就是它的降级路径。

线格式(全角方头括号,和回复格式里的动作括号（）不冲突):

    〔stance:provoke〕                     关系性意图,应当出现在回复第一行
    〔tool:mute {"minutes": 5}〕           调一个能力,参数是 JSON 对象(可省略)
    〔wait〕                                显式让位:我说完了,轮到你

解析器是**流式**的:`feed()` 逐 token 喂,散文原样吐出,指令攒够一整条才交出去。
一个只写了半个 `〔` 的 token 不会把标记漏给玩家,而一个迟迟不闭合的 `〔`(模型在
散文里手写了这个符号)在超过 `_MAX_BODY` 之后按原文吐出 —— 宁可玩家看到一个怪
符号,也不能把她的话整段吞掉。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

OPEN = "〔"
CLOSE = "〕"

# 一条指令最长能有多长。超过就判定"这不是指令,是散文里的符号"。
_MAX_BODY = 240


@dataclass
class Directive:
    """一条解析出来的控制指令。`raw` 留着,便于日志与回放对照。"""

    kind: str  # "stance" | "tool" | "wait" | "unknown"
    name: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    raw: str = ""


def parse_body(body: str) -> Directive:
    """把 `〔…〕` 里面那段解析成一条指令。读不懂就是 unknown,绝不猜。"""
    text = body.strip()
    lowered = text.lower()
    if lowered in ("wait", "wait_for_user", "yield"):
        return Directive(kind="wait", name="wait_for_user", raw=body)
    if lowered.startswith("stance:") or lowered.startswith("stance："):
        return Directive(kind="stance", name=text.split(":", 1)[-1].split("：", 1)[-1].strip(), raw=body)
    if lowered.startswith("tool:") or lowered.startswith("tool："):
        rest = text.split(":", 1)[-1].split("：", 1)[-1].strip()
        name, _, arg_text = rest.partition(" ")
        params: dict[str, Any] = {}
        arg_text = arg_text.strip()
        if arg_text:
            try:
                loaded = json.loads(arg_text)
            except ValueError:
                logger.warning("tool 调用 %r 的参数不是 JSON:%r", name, arg_text)
                return Directive(kind="unknown", name=name.strip(), raw=body)
            if isinstance(loaded, dict):
                params = loaded
            else:
                logger.warning("tool 调用 %r 的参数不是对象:%r", name, arg_text)
                return Directive(kind="unknown", name=name.strip(), raw=body)
        return Directive(kind="tool", name=name.strip(), params=params, raw=body)
    return Directive(kind="unknown", raw=body)


class DirectiveParser:
    """流式地把控制指令从散文里摘出来。

    `feed()` / `flush()` 返回 `(kind, value)` 序列:`("text", str)` 是给玩家看的,
    `("directive", Directive)` 是给引擎看的。顺序保持原样 —— 一句话中间调的工具
    仍然落在那句话中间。
    """

    def __init__(self) -> None:
        self._buffer: str | None = None

    def feed(self, text: str) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        while text:
            if self._buffer is None:
                start = text.find(OPEN)
                if start < 0:
                    if text:
                        out.append(("text", text))
                    break
                if start:
                    out.append(("text", text[:start]))
                self._buffer = ""
                text = text[start + len(OPEN) :]
                continue

            end = text.find(CLOSE)
            if end < 0:
                self._buffer += text
                if len(self._buffer) > _MAX_BODY:
                    # 不是指令。原样还给玩家 —— 吞掉她的话是更坏的错。
                    out.append(("text", OPEN + self._buffer))
                    self._buffer = None
                break
            self._buffer += text[:end]
            out.append(("directive", parse_body(self._buffer)))
            self._buffer = None
            text = text[end + len(CLOSE) :]
        return out

    def flush(self) -> list[tuple[str, Any]]:
        """流结束了。没闭合的 `〔` 按原文吐出。"""
        if self._buffer is None:
            return []
        buffered = self._buffer
        self._buffer = None
        return [("text", OPEN + buffered)]


def strip_directives(text: str) -> tuple[str, list[Directive]]:
    """一次性解析(非流式路径:摘要、分类器回包、测试)。"""
    parser = DirectiveParser()
    parts: list[str] = []
    found: list[Directive] = []
    for kind, value in list(parser.feed(text)) + list(parser.flush()):
        if kind == "text":
            parts.append(value)
        else:
            found.append(value)
    return "".join(parts), found
