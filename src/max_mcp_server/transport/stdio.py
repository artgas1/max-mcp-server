"""stdio: stdout — только JSON-RPC; все логи — в stderr."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import MCPServer

from max_mcp_server.adapter.base import MaxAdapter
from max_mcp_server.errors import MaxMcpError

log = logging.getLogger("max_mcp_server.transport")


async def start_adapter_soft(adapter: MaxAdapter) -> None:
    """Старт адаптера, не роняющий сервер: без сессии инструменты вернут понятную ошибку."""
    try:
        await adapter.start()
    except MaxMcpError as exc:
        log.warning("MAX adapter not started: %s", exc.public_message)


async def run_stdio(server: MCPServer[Any], adapter: MaxAdapter) -> None:
    await start_adapter_soft(adapter)
    try:
        await server.run_stdio_async()
    finally:
        await adapter.stop()
