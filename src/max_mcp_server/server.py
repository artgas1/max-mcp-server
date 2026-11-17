"""Сборка MCP-сервера: инструменты + инструкции. Жизненный цикл адаптера — в transport/*."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from max_mcp_server import __version__
from max_mcp_server.adapter.base import MaxAdapter
from max_mcp_server.config import Settings
from max_mcp_server.security.policy import Policy
from max_mcp_server.security.untrusted import UNTRUSTED_NOTICE
from max_mcp_server.tools import register_tools
from max_mcp_server.tools.common import ToolDeps

INSTRUCTIONS = (
    "Сервер даёт доступ к личному аккаунту мессенджера MAX владельца. "
    + UNTRUSTED_NOTICE
    + " Запись (отправка, удаление, изменение) — только по прямой просьбе владельца в этом чате; "
    "перед такими действиями покажите владельцу, что именно будет сделано. "
    "Старую переписку читайте по датам: сначала max_chat_timeline (когда шла переписка), затем "
    "max_read_messages с date_from/date_to и next_cursor. Инструменты чтения не отмечают сообщения "
    "прочитанными."
)


def make_policy(settings: Settings) -> Policy:
    return Policy(
        read_only=settings.read_only,
        read_allow=settings.read_allow,
        write_allow=settings.write_allow,
        deny=settings.deny,
        allow_join=settings.allow_join,
    )


def build_server(
    settings: Settings, adapter: MaxAdapter, policy: Policy | None = None
) -> tuple[MCPServer[Any], list[str]]:
    policy = policy or make_policy(settings)
    adapter.events().set_filter(policy.can_read)
    server: MCPServer[Any] = MCPServer(
        name="max-mcp-server",
        title="MAX messenger",
        version=__version__,
        instructions=INSTRUCTIONS,
        log_level=settings.log_level,
    )
    names = register_tools(server, ToolDeps(adapter=adapter, policy=policy, settings=settings))
    return server, names
