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

**ASCII 方括号照收**(`[tool:mute {"minutes": 5}]`)。这不是宽容癖:真模型隔一会儿
就会把 `〔〕` 打成 `[]`,而下场是标记一个字不落地漏进玩家看到的正文 —— 她"走开"
没走成,玩家却看见一行 `[tool:walk_away {}]`。全角那套留着当**权威线格式**(提示词
里教的是它,`〕` 不会在 JSON 里出现),ASCII 是收进来的降级写法。

⚠️ `[` 在散文里太常见,所以它**不是无条件的开括号**:只有后面跟得上
`tool:` / `stance:` / `wait` / `yield` 才当候选,跟不上就当场退回正文。于是
`[waiting]`、`[停顿]` 原样交给玩家。ASCII 的闭括号还要**避开 JSON 里的 `]`**
(`{"a":[1,2]}`),所以只认花括号深度为 0 的那个 `]`。

**光写工具名也算**(`〔delay_reply {"minutes": 5}〕`,漏了 `tool:`)。同一条理由的
另一半:线上抓到的样子是她整轮只输出了这一条标记、一句台词都没有,于是标记被摘掉、
工具没跑、玩家收到**零个字节**。她的选择没兑现,而屏幕上和"应用崩了"长得一模一样。

两道闸没松:**只认这个世界真有的能力名**(由 `chat_service` 注入 —— 这个模块不认识
工具注册表,也不该认识;猜一份名字出来是这一层最坏的错法),而且**只在全角括号里认**。
`[` 那一侧不认,因为 `eat` / `work` / `sleep` 这种名字在散文里是真会出现的,而全角
`〔〕` 是提示词里教的权威写法 —— 写下它的人已经在说"这是一条指令"了。

解析器是**流式**的:`feed()` 逐 token 喂,散文原样吐出,指令攒够一整条才交出去。
一个只写了半个 `〔` 的 token 不会把标记漏给玩家。

**没解析成的标记不许漏给玩家。** 一个迟迟不闭合的 `〔` 有两种来路,得分开:

- 模型在散文里手写了这个符号(`〔她愣了一下`)—— 按原文吐出,宁可玩家看到一个
  怪符号,也不能把她的话整段吞掉。这是原来的行为,一个字没动。
- 它是个**打残了的控制标记**(`〔tool:wait_for_user {}` 少了闭括号)—— 这个照原文
  吐出去就是让玩家看见引擎的内脏。判据是"开头像不像指令"(`_looks_like_directive`):
  像,就交成一条 `unknown` 指令。下游本来就把 `unknown` 咽掉并记进
  `meta["unknown_directives"]`,所以这条路上**既不漏给玩家、也不静默** ——
  运维照旧看得见她那一轮想调什么而没调成。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Collection

logger = logging.getLogger(__name__)

OPEN = "〔"
CLOSE = "〕"
ASCII_OPEN = "["
ASCII_CLOSE = "]"

# 一条指令最长能有多长。超过就判定"这不是指令,是散文里的符号"。
_MAX_BODY = 240

# 指令体的开头。`_HEADS` 同时干两件事:判一段完整的体是不是指令(前缀命中),
# 以及判一段**还没写完**的体有没有可能长成指令(反过来,拿它当前缀)。
# 后者是 ASCII 那条路能安全存在的全部理由 —— 没有它,`[` 就得一路缓冲到
# `]` 或 240 字,而那期间玩家什么也看不到。
_PREFIX_HEADS = ("tool:", "tool：", "stance:", "stance：")
_EXACT_HEADS = ("wait", "wait_for_user", "yield")


def _name_set(tool_names: Collection[str]) -> frozenset[str]:
    return frozenset(str(name).strip().lower() for name in tool_names if str(name).strip())


def _bare_tool_name(body: str, tool_names: frozenset[str]) -> str:
    """`〔delay_reply {…}〕` —— 漏了 `tool:` 的那种写法。命中返回那个能力名。

    只认注入进来的名字。「读不懂就是 unknown,绝不猜」那条一个字没松 —— 松的是
    "她少写了两个字"这一种,而不是"这个词看起来像个动词"。
    """
    if not tool_names:
        return ""
    name = body.strip().split(" ", 1)[0].strip().lower()
    return name if name in tool_names else ""


def _looks_like_directive(body: str, tool_names: frozenset[str] = frozenset()) -> bool:
    """这段完整的体像不像一条指令 —— **不管它解析得成解析不成**。

    "像"和"成"必须分开:`tool:mute {坏 JSON}` 解析不成,但它显然是一条打残了的
    指令,照原文吐给玩家就是让人看见引擎的内脏。判"像"只看开头。
    """
    text = body.strip().lower()
    if text in _EXACT_HEADS or text.startswith(_PREFIX_HEADS):
        return True
    return bool(_bare_tool_name(body, tool_names))


def _could_become_directive(partial: str) -> bool:
    """这段**还没写完**的体,还有没有可能长成一条指令。

    用来给 ASCII 的 `[` 早早地下车:`[停顿` 第一个字就否掉了,于是它原样流给玩家,
    而不是被扣在缓冲区里等一个也许永远不来的 `]`。
    """
    text = partial.lower()
    if _looks_like_directive(partial):
        return True
    return any(head.startswith(text) for head in (*_PREFIX_HEADS, *_EXACT_HEADS))


@dataclass
class Directive:
    """一条解析出来的控制指令。`raw` 留着,便于日志与回放对照。"""

    kind: str  # "stance" | "tool" | "wait" | "unknown"
    name: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    raw: str = ""


def _tool_directive(rest: str, body: str) -> Directive:
    """`<名字> <JSON>` 那一段。`tool:` 写了没写共用这一份 —— 两份写法解析出不同的
    参数,是这一层最难查的错。"""
    name, _, arg_text = rest.partition(" ")
    params: dict[str, Any] = {}
    arg_text = arg_text.strip()
    if arg_text:
        try:
            loaded = json.loads(arg_text)
        except ValueError:
            logger.warning("tool 调用 %r 的参数不是 JSON:%r", name, arg_text)
            return Directive(kind="unknown", name=name.strip(), raw=body)
        if not isinstance(loaded, dict):
            logger.warning("tool 调用 %r 的参数不是对象:%r", name, arg_text)
            return Directive(kind="unknown", name=name.strip(), raw=body)
        params = loaded
    return Directive(kind="tool", name=name.strip(), params=params, raw=body)


def parse_body(body: str, tool_names: Collection[str] = ()) -> Directive:
    """把 `〔…〕` 里面那段解析成一条指令。读不懂就是 unknown,绝不猜。

    `tool_names` 是这个世界真有的能力名(调用方注入)。给了它,漏写 `tool:`
    的那种写法也认;不给,行为和从前逐字相同。
    """
    text = body.strip()
    lowered = text.lower()
    if lowered in ("wait", "wait_for_user", "yield"):
        return Directive(kind="wait", name="wait_for_user", raw=body)
    if lowered.startswith("stance:") or lowered.startswith("stance："):
        return Directive(kind="stance", name=text.split(":", 1)[-1].split("：", 1)[-1].strip(), raw=body)
    if lowered.startswith("tool:") or lowered.startswith("tool："):
        return _tool_directive(text.split(":", 1)[-1].split("：", 1)[-1].strip(), body)
    if _bare_tool_name(text, _name_set(tool_names)):
        return _tool_directive(text, body)
    return Directive(kind="unknown", raw=body)


class DirectiveParser:
    """流式地把控制指令从散文里摘出来。

    `feed()` / `flush()` 返回 `(kind, value)` 序列:`("text", str)` 是给玩家看的,
    `("directive", Directive)` 是给引擎看的。顺序保持原样 —— 一句话中间调的工具
    仍然落在那句话中间。
    """

    def __init__(self, tool_names: Collection[str] = ()) -> None:
        self._buffer: str | None = None
        self._ascii = False          # 这一条候选是 `[` 开的还是 `〔` 开的
        self._tools = _name_set(tool_names)

    @property
    def _bare_ok(self) -> frozenset[str]:
        """这一条候选认不认光写工具名的写法 —— ASCII 那一侧一律不认(见模块说明)。"""
        return frozenset() if self._ascii else self._tools

    # 没闭合/没解析成的候选怎么收场 —— 三处(超长、ASCII 早退、流结束)共用一份,
    # 因为它们是同一个判断:像指令就咽掉并记账,不像就原样还给玩家。
    def _abandon(self, opener: str, body: str) -> tuple[str, Any]:
        if _looks_like_directive(body, self._bare_ok):
            return ("directive", Directive(kind="unknown", raw=body))
        return ("text", opener + body)

    def feed(self, text: str) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        while text:
            if self._buffer is None:
                # 两种开括号取**先出现的那个**。分开找再比,是因为
                # `str.find` 一次只认一个针,而谁先谁后决定了另一个算不算正文。
                wide = text.find(OPEN)
                ascii_at = text.find(ASCII_OPEN)
                if wide < 0 and ascii_at < 0:
                    out.append(("text", text))
                    break
                if wide >= 0 and (ascii_at < 0 or wide < ascii_at):
                    start, opener, is_ascii = wide, OPEN, False
                else:
                    start, opener, is_ascii = ascii_at, ASCII_OPEN, True
                if start:
                    out.append(("text", text[:start]))
                self._buffer = ""
                self._ascii = is_ascii
                text = text[start + len(opener) :]
                continue

            opener = ASCII_OPEN if self._ascii else OPEN
            consumed, done = self._consume(text, out, opener)
            text = text[consumed:]
            if not done:
                break
        return out

    def _consume(
        self, text: str, out: list[tuple[str, Any]], opener: str
    ) -> tuple[int, bool]:
        """吃掉候选体的下一段。返回 `(吃了几个字, 这一条了结了没有)`。"""
        end = self._find_close(self._buffer or "", text)
        if end < 0:
            self._buffer = (self._buffer or "") + text
            # ASCII 那条路多一道**早退**:开头就不可能是指令的话,别扣着玩家的话。
            if self._ascii and not _could_become_directive(self._buffer):
                out.append(("text", opener + self._buffer))
                self._buffer = None
                return len(text), False
            if len(self._buffer) > _MAX_BODY:
                out.append(self._abandon(opener, self._buffer))
                self._buffer = None
            return len(text), False

        body = (self._buffer or "") + text[:end]
        self._buffer = None
        if self._ascii and not _looks_like_directive(body):
            # `[waiting]`、`[停顿]` —— 方括号在散文里本来就有它自己的用法。
            out.append(("text", ASCII_OPEN + body + ASCII_CLOSE))
        else:
            out.append(("directive", parse_body(body, self._bare_ok)))
        return end + 1, True

    def _find_close(self, buffered: str, text: str) -> int:
        """闭括号在 `text` 里的下标,没有就 -1。

        全角那个直接找 —— `〕` 不会出现在 JSON 里。ASCII 的 `]` 会
        (`{"a": [1, 2]}`),所以只认**花括号深度为 0** 的那个;不数这一笔,
        带数组参数的调用会被从中间截断,然后当成散文漏出去。
        """
        if not self._ascii:
            return text.find(CLOSE)
        depth = buffered.count("{") - buffered.count("}")
        for index, char in enumerate(text):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            elif char == ASCII_CLOSE and depth <= 0:
                return index
        return -1

    def flush(self) -> list[tuple[str, Any]]:
        """流结束了。没闭合的候选按 `_abandon` 那条规矩收场。"""
        if self._buffer is None:
            return []
        buffered, self._buffer = self._buffer, None
        return [self._abandon(ASCII_OPEN if self._ascii else OPEN, buffered)]


def strip_directives(
    text: str, tool_names: Collection[str] = ()
) -> tuple[str, list[Directive]]:
    """一次性解析(非流式路径:摘要、分类器回包、测试)。"""
    parser = DirectiveParser(tool_names)
    parts: list[str] = []
    found: list[Directive] = []
    for kind, value in list(parser.feed(text)) + list(parser.flush()):
        if kind == "text":
            parts.append(value)
        else:
            found.append(value)
    return "".join(parts), found
