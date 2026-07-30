"""chat 时角色能调的能力(issue #15)。

`import anima_world.tools` 就等于把 `social.py` 里所有 `@tool` 登记进注册表 ——
声明在代码里,一处加、处处可见(提示词菜单、`World.tools()`、创世时的能力目录)。
"""

from __future__ import annotations

from anima_world.tools import social as _social  # noqa: F401 - import 即登记
from anima_world.tools.base import (
    AUTONOMY,
    CHAT,
    SURFACES,
    ToolCallError,
    ToolContext,
    ToolResult,
    ToolRuntime,
    ToolSpec,
    call,
    capability_payloads,
    get,
    tool,
    tools_for,
)

__all__ = [
    "AUTONOMY",
    "CHAT",
    "SURFACES",
    "ToolCallError",
    "ToolContext",
    "ToolResult",
    "ToolRuntime",
    "ToolSpec",
    "call",
    "capability_payloads",
    "get",
    "tool",
    "tools_for",
    "prompt_menu",
]


def prompt_menu(agent_id: str = "*", surface: str = CHAT) -> str:
    """提示词里那份能力清单。默认给 chat 面 —— 调用点没传的旧代码不该突然看到
    自主轮次专属的能力(`reach_out` 在聊天里没有意义,对方就在眼前)。"""
    return "\n".join(spec.prompt_line() for spec in tools_for(agent_id, surface))
