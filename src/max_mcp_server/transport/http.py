"""Streamable HTTP только на loopback + bearer-токен (базово; усиление — тред 3)."""

from __future__ import annotations

import hmac
import ipaddress
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from max_mcp_server.adapter.base import MaxAdapter
from max_mcp_server.config import LOOPBACK_HOSTS, Settings
from max_mcp_server.errors import ConfigError
from max_mcp_server.session.auth import read_secret_file
from max_mcp_server.transport.stdio import start_adapter_soft

log = logging.getLogger("max_mcp_server.transport")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_token(settings: Settings) -> str | None:
    if settings.http_token_file is not None:
        return read_secret_file(settings.http_token_file)
    return settings.http_token or None


class BearerAuth:
    """Чистый ASGI-middleware: ``Authorization: Bearer <token>`` (compare_digest) или 401."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = b""
        for key, value in scope.get("headers", []):
            if key.lower() == b"authorization":
                header = value
                break
        ok = header[:7].lower() == b"bearer " and hmac.compare_digest(
            header[7:].strip(), self._token
        )
        if not ok:
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        await self.app(scope, receive, send)


def build_http_app(
    server: MCPServer[Any], settings: Settings, *, insecure_no_auth: bool = False
) -> ASGIApp:
    host, port = settings.http_host, settings.http_port
    if not is_loopback(host) and not settings.http_allow_nonlocal:
        raise ConfigError(
            f"MAX_HTTP_HOST={host}: разрешён только loopback (127.0.0.1). "
            "Иное — только с MAX_HTTP_ALLOW_NONLOCAL=1 (не рекомендуется)."
        )
    token = resolve_token(settings)
    if token is None and not insecure_no_auth:
        raise ConfigError(
            "Для HTTP нужен токен: MAX_HTTP_TOKEN_FILE (файл 600) или MAX_HTTP_TOKEN. "
            "Без авторизации — только --insecure-no-auth (для отладки)."
        )
    origins = [o.strip() for o in settings.http_allowed_origins.split(",") if o.strip()]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            f"[::1]:{port}",
            "127.0.0.1",
            "localhost",
        ],
        allowed_origins=[f"http://127.0.0.1:{port}", f"http://localhost:{port}", *origins],
    )
    app: ASGIApp = server.streamable_http_app(
        streamable_http_path=settings.http_path,
        transport_security=security,
        host=host,
        max_request_body_size=1024 * 1024,
    )
    if token is not None:
        app = BearerAuth(app, token)
    else:
        log.warning(
            "HTTP без авторизации (--insecure-no-auth): любой локальный процесс получит доступ к MAX"
        )
    return app


async def run_http(
    server: MCPServer[Any],
    adapter: MaxAdapter,
    settings: Settings,
    *,
    insecure_no_auth: bool = False,
) -> None:
    import uvicorn

    app = build_http_app(server, settings, insecure_no_auth=insecure_no_auth)
    await start_adapter_soft(adapter)
    config = uvicorn.Config(
        app,
        host=settings.http_host,
        port=settings.http_port,
        log_level="warning",
        access_log=False,
        server_header=False,
        proxy_headers=False,
    )
    try:
        await uvicorn.Server(config).serve()
    finally:
        await adapter.stop()
