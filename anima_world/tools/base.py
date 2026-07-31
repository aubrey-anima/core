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

    def agent_names(self) -> dict[str, str]: ...

    def present_player_ids(self, agent_id: str | None = None) -> list[str]: ...

    def player_name(self, player_id: str) -> str: ...

    def agent_location(self, agent_id: str) -> str: ...

    def face_to_face(self, agent_id: str, player_id: str) -> bool: ...

    def point_ids(self) -> list[str]: ...

    def move_agent(self, agent_id: str, location: str) -> dict[str, Any]: ...

    def do_action(self, agent_id: str, kind: str, params: dict[str, Any]) -> bool: ...

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


# 能力露在哪个面上。**不是装饰**:自主轮次里没有"对方"这个人,`walk_away` /
# `end_conversation` / `delay_reply` / `wait_for_user` 在那儿一律没有意义 —— 一个
# 在无人对话时也能被选中的 `end_conversation` 只会写出一堆关掉空会话的动作。
CHAT = "chat"            # 玩家跟她说话的那一轮
AUTONOMY = "autonomy"    # 定时轮次:没人跟她说话,她自己决定要不要做点什么
BODY = "body"            # 过日子的动作:走、吃、干活、睡 —— 谁都能做,不挑场合
SURFACES = (CHAT, AUTONOMY, BODY)


@dataclass(frozen=True)
class ToolSpec:
    """一个能力的声明 + 实现。形状对齐 OpenAI function calling 的 tool 定义。"""

    id: str
    kind: str
    description: str
    params_schema: dict[str, Any]
    handler: ToolHandler
    surfaces: tuple[str, ...] = (CHAT,)
    writes: tuple[str, ...] = ()
    """**它把世界改在哪儿。** 每一项是一张表名,或 `events:<类型>`。

    这个字段存在的理由是账面上的:这一版靠人肉找出了八处"声明了却没兑现" ——
    `broadcast` 告诉她"世界里的人都能看到"而只写了一行日志、`walk_away` 隔着手机是
    空动作、规律写 `world_x` 落到别人名下而仪表报成功。**每一个都是"能跑、不报错、
    给错东西"**,而每一个都是玩到了才发现的。

    声明之后这条就成了机器能验的:`tests/test_verb_writes.py` 逐个动词调一遍,
    比对声明的地方到底变没变。CLAUDE.md 里"**她的选择必须在世界里兑现**"那条不变量,
    从一句人得自己记住的话,变成一条会红的测试。

    留空是**故意的空**,不是忘了填 —— 有测试盯着不许留空。
    """

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
    surfaces: tuple[str, ...] = (CHAT,),
    writes: tuple[str, ...] = (),
) -> Callable[[ToolHandler], ToolHandler]:
    """把一个函数登记成能力。重复登记同一个 id 是错,不是覆盖。"""

    def decorate(handler: ToolHandler) -> ToolHandler:
        if id in _REGISTRY:
            raise ToolCallError(f"tool {id!r} 已经登记过了")
        unknown = [surface for surface in surfaces if surface not in SURFACES]
        if unknown:
            raise ToolCallError(f"tool {id!r} 声明了不存在的面:{unknown}")
        _REGISTRY[id] = ToolSpec(
            id=id, kind=kind, description=description, writes=tuple(writes),
            params_schema=dict(params or {}), handler=handler,
            surfaces=tuple(surfaces),
        )
        return handler

    return decorate


def get(tool_id: str) -> ToolSpec:
    spec = _REGISTRY.get(tool_id)
    if spec is None:
        raise ToolCallError(f"没有 {tool_id!r} 这个能力")
    return spec


def tools_for(agent_id: str, surface: str | None = None) -> list[ToolSpec]:
    """这个角色在某个面上能用的能力。

    v1 所有角色同一套(按性格分工是 v2),但**面是分的**:`surface=None` 给全部
    (目录、`contract`、`World.tools()` 用),给了面就只给那个面上的。
    """
    specs = [_REGISTRY[key] for key in sorted(_REGISTRY)]
    if surface is None:
        return specs
    return [spec for spec in specs if surface in spec.surfaces]


def capability_payloads() -> list[dict[str, Any]]:
    """创世时写进能力目录的那份(`capability_registered` 事件的 payload)。"""
    return [
        {
            "id": spec.id,
            "kind": spec.kind,
            "description": spec.description,
            "params_schema": spec.params_schema,
            "surface": ",".join(spec.surfaces),
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
