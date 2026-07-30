"""@tool 注册表:声明在代码里,调用在聊天里,执行在引擎进程内(issue #15)。

`bt_actions` 那套能力早就是数据化的(声明在 db、实现在包里,形状和 OpenAI function
calling / MCP tool definition 对齐),但**聊天时完全没人读它** —— LLM 看不到"我可以
选择走开",只能用词把话接下去。这个包补的就是那一半:她真有可以选择的行动。

三条边界:

- **执行在引擎进程内。** 跨进程 / 字面 MCP server 留给未来(比如接一个 TTS 让她哼
  一段),v1 全内建。
- **v1 所有角色共用一套 tool。** 按性格分工是 v2 —— 先让"她能走开"这件事成立。
- **工具改的是世界,不是提示词。** `walk_away` 真的发起一次行程,`mute` 真的让下一条
  消息被拒。声明了却没人兑现的能力,比没有更坏。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)


class ToolCallError(RuntimeError):
    """工具调用本身是坏的(未知 id、参数不合法、运行时不支持)。"""


@dataclass
class ToolResult:
    """一次工具执行的结果。

    `text` 是给玩家看的一句(可空 —— 静音通常什么也不说);`end_conversation`
    与 `stop_loop` 是这次调用对本轮对话的处置。
    """

    ok: bool = True
    text: str = ""
    end_conversation: bool = False
    stop_loop: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self, tool_id: str, params: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool": tool_id,
            "params": dict(params),
            "ok": self.ok,
        }
        if self.text:
            payload["text"] = self.text
        if self.detail:
            payload["detail"] = dict(self.detail)
        if self.error:
            payload["error"] = self.error
        if self.end_conversation:
            payload["end_conversation"] = True
        return payload


class ToolRuntime(Protocol):
    """工具能碰到的世界。由 `World` 实现并注入 —— 聊天子系统本身不认识调度器。"""

    def tick(self) -> int: ...

    def now(self) -> int: ...

    def ticks_for_minutes(self, minutes: float) -> int: ...

    def config(self, key: str, default: Any = None) -> Any: ...

    @property
    def state(self) -> Any:
        """`ChatStateStore`。"""
        ...

    def emit(self, event: dict[str, Any]) -> None: ...

    def agent_ids(self) -> list[str]: ...

    def agent_location(self, agent_id: str) -> str: ...

    def face_to_face(self, agent_id: str, player_id: str) -> bool: ...

    def point_ids(self) -> list[str]: ...

    def move_agent(self, agent_id: str, location: str) -> dict[str, Any]: ...

    def close_conversation(self, agent_id: str, player_id: str) -> bool: ...


@dataclass
class ToolContext:
    """一次调用的现场:谁在调、对谁调、以及那个世界。"""

    agent_id: str
    player_id: str
    runtime: ToolRuntime
    agent_name: str = ""

    @property
    def state(self) -> Any:
        return self.runtime.state


ToolHandler = Callable[[ToolContext, dict[str, Any]], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    """一个能力的声明 + 实现。形状对齐 OpenAI function calling 的 tool 定义。"""

    id: str
    kind: str
    description: str
    params_schema: dict[str, Any]
    handler: ToolHandler

    def prompt_line(self) -> str:
        params = ", ".join(
            f"{name}"
            + (":必填" if isinstance(meta, dict) and meta.get("required") else "")
            for name, meta in self.params_schema.items()
        )
        suffix = f" 参数:{params}" if params else " 无参数"
        return f"- {self.id}:{self.description}{suffix}"


_REGISTRY: dict[str, ToolSpec] = {}


def tool(
    *, id: str, kind: str, description: str,  # noqa: A002 - id 是这份契约里的字段名
    params: dict[str, Any] | None = None,
) -> Callable[[ToolHandler], ToolHandler]:
    """把一个函数登记成能力。重复登记同一个 id 是错,不是覆盖。"""

    def decorate(handler: ToolHandler) -> ToolHandler:
        if id in _REGISTRY:
            raise ToolCallError(f"tool {id!r} 已经登记过了")
        _REGISTRY[id] = ToolSpec(
            id=id, kind=kind, description=description,
            params_schema=dict(params or {}), handler=handler,
        )
        return handler

    return decorate


def get(tool_id: str) -> ToolSpec:
    spec = _REGISTRY.get(tool_id)
    if spec is None:
        raise ToolCallError(f"没有 {tool_id!r} 这个能力")
    return spec


def tools_for(agent_id: str) -> list[ToolSpec]:
    """这个角色此刻能用的能力。v1:所有人同一套(按性格分工是 v2)。"""
    return [_REGISTRY[key] for key in sorted(_REGISTRY)]


def capability_payloads() -> list[dict[str, Any]]:
    """创世时写进能力目录的那份(`capability_registered` 事件的 payload)。"""
    return [
        {
            "id": spec.id,
            "kind": spec.kind,
            "description": spec.description,
            "params_schema": spec.params_schema,
            "surface": "chat",
        }
        for spec in tools_for("*")
    ]


def call(ctx: ToolContext, tool_id: str, params: dict[str, Any]) -> ToolResult:
    """执行一次调用。工具自己的失败降级成一个 `ok=False` 的结果 —— 一次坏调用
    不该掀翻整轮聊天,但**必须留下痕迹**(结果会随观测量落到消息行上)。"""
    spec = _REGISTRY.get(tool_id)
    if spec is None:
        logger.warning("角色 %s 调了一个不存在的能力 %r", ctx.agent_id, tool_id)
        return ToolResult(ok=False, error=f"unknown tool {tool_id}")
    try:
        return spec.handler(ctx, dict(params or {}))
    except ToolCallError as exc:
        logger.warning("能力 %s 拒绝了这次调用:%s", tool_id, exc)
        return ToolResult(ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - 工具坏了不该让她哑掉
        logger.warning("能力 %s 执行失败", tool_id, exc_info=True)
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
