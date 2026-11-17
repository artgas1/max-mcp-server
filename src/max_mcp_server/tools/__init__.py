"""Регистрация инструментов: чтение — всегда; запись — только при MAX_READ_ONLY=false (тред 3)."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from max_mcp_server.tools.common import ToolDeps
from max_mcp_server.tools.read import READ_TOOL_NAMES, register_read_tools


def register_tools(server: MCPServer[Any], deps: ToolDeps) -> list[str]:
    register_read_tools(server, deps)
    names = list(READ_TOOL_NAMES)
    if not deps.policy.read_only:
        # Инструменты записи появятся в треде 3 (tools/write.py). До тех пор сервер
        # честно работает только на чтение даже при MAX_READ_ONLY=false.
        try:
            from max_mcp_server.tools import write  # type: ignore[attr-defined]
        except ImportError:
            pass
        else:
            names.extend(write.register_write_tools(server, deps))
    return names
