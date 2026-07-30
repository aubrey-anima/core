"""chat 时角色能调的能力(issue #15)。

`import anima_world.tools` 就等于把 `social.py` 里所有 `@tool` 登记进注册表 ——
声明在代码里,一处加、处处可见(提示词菜单、`World.tools()`、创世时的能力目录)。
"""

from __future__ import annotations

from anima_world.tools import social as _social  # noqa: F401 - import 即登记
from anima_world.tools.base import (
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


def prompt_menu(agent_id: str = "*") -> str:
    """提示词里那份能力清单。"""
    return "\n".join(spec.prompt_line() for spec in tools_for(agent_id))
