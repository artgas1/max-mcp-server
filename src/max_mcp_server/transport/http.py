"""Streamable HTTP с защитой (design.md, 8.2).

Слои (снаружи внутрь):
1. ``HostOriginGuard`` — ``Host`` только из списка (loopback:порт), ``Origin`` — отсутствует или
   loopback/явно разрешённый; иначе 403. Это защита от DNS rebinding и от страниц в браузере,
   стучащихся на 127.0.0.1. Идёт ДО авторизации.
2. ``BodyLimit`` — тело запроса ≤ ``MAX_HTTP_MAX_BODY`` (1 МБ), иначе 413.
3. ``BearerAuth`` — ``Authorization: Bearer <token>``, сравнение ``hmac.compare_digest`` по
   SHA-256 (без утечки длины), иначе 401.
4. MCP-приложение на пути ``MAX_HTTP_PATH`` или секретном ``/mcp/<32 байта>``
   (``MAX_HTTP_SECRET_PATH=true``); чужой путь → 404. Плюс встроенная проверка SDK.

Не-loopback адрес: только с ``MAX_HTTP_ALLOW_NONLOCAL=1`` **и** с токеном (``--insecure-no-auth``
на не-loopback запрещён всегда).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import secrets
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from max_mcp_server.adapter.base import MaxAdapter
from max_mcp_server.config import LOOPBACK_HOSTS, Settings
from max_mcp_server.errors import ConfigError
from max_mcp_server.session.auth import read_secret_file
from max_mcp_server.session.store import ensure_private_dir, write_private_bytes
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
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _split(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


# --------------------------------------------------------------------------- секреты


def generate_secret() -> str:
    return secrets.token_urlsafe(32)


def ensure_secret_file(path: Path, *, rotate: bool = False) -> str:
    """Создаёт (или читает) секрет в файле 0600 внутри каталога 0700."""
    path = path.expanduser()
    if path.exists() and not rotate:
        return read_secret_file(path)
    ensure_private_dir(path.parent)
    value = generate_secret()
    write_private_bytes(path, (value + "\n").encode())
    return value


def resolve_token(settings: Settings) -> str | None:
    """Токен: MAX_HTTP_TOKEN_FILE → MAX_HTTP_TOKEN → файл ``max-mcp http-token`` (если есть)."""
    if settings.http_token_file is not None:
        return read_secret_file(settings.http_token_file)
    if settings.http_token:
        return settings.http_token
    default = settings.http_token_default_file
    if default.exists():
        return read_secret_file(default)
    return None


def resolve_mcp_path(settings: Settings, *, create: bool = True) -> str:
    base = settings.http_path.rstrip("/") or "/mcp"
    if not settings.http_secret_path:
        return base
    f = settings.http_secret_file
    if not f.exists() and not create:
        raise ConfigError("Секретный путь ещё не создан: max-mcp http-token")
    return f"{base}/{ensure_secret_file(f)}"


def mask_path(path: str, settings: Settings) -> str:
    return settings.http_path.rstrip("/") + "/<secret>" if settings.http_secret_path else path


# --------------------------------------------------------------------------- middleware


async def _deny(send: Send, status: int, error: str, extra: Iterable[tuple[bytes, bytes]] = ()):
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), *extra],
        }
    )
    await send({"type": "http.response.body", "body": b'{"error":"' + error.encode() + b'"}'})


def _header(scope: Scope, name: bytes) -> bytes | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value
    return None


class HostOriginGuard:
    """403 на чужой ``Host`` или ``Origin`` (DNS rebinding, CSRF из браузера)."""

    def __init__(self, app: ASGIApp, *, hosts: Iterable[str], origins: Iterable[str]) -> None:
        self.app = app
        self.hosts = frozenset(h.lower() for h in hosts)
        self.origins = frozenset(o.lower().rstrip("/") for o in origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = (_header(scope, b"host") or b"").decode("latin-1").lower()
        if host not in self.hosts:
            log.warning("HTTP: отклонён запрос с чужим Host")
            await _deny(send, 403, "forbidden_host")
            return
        origin = _header(scope, b"origin")
        if origin is not None:
            o = origin.decode("latin-1").lower().rstrip("/")
            if o not in self.origins:
                log.warning("HTTP: отклонён запрос с чужим Origin")
                await _deny(send, 403, "forbidden_origin")
                return
        await self.app(scope, receive, send)


class BodyLimit:
    """Тело запроса не больше ``limit`` байт: читается целиком до передачи дальше, иначе 413."""

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS", "DELETE"):
            await self.app(scope, receive, send)
            return
        declared = _header(scope, b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.limit:
                    await _deny(send, 413, "payload_too_large")
                    return
            except ValueError:
                await _deny(send, 400, "bad_content_length")
                return
        body = bytearray()
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body.extend(msg.get("body", b""))
            if len(body) > self.limit:
                await _deny(send, 413, "payload_too_large")
                return
            if not msg.get("more_body", False):
                break
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class BearerAuth:
    """``Authorization: Bearer <token>``; сравнение SHA-256 через ``compare_digest`` или 401."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._digest = hashlib.sha256(token.encode()).digest()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = _header(scope, b"authorization") or b""
        given = header[7:].strip() if header[:7].lower() == b"bearer " else b""
        ok = hmac.compare_digest(hashlib.sha256(given).digest(), self._digest) and bool(given)
        if not ok:
            await _deny(send, 401, "unauthorized", [(b"www-authenticate", b"Bearer")])
            return
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------- сборка


def allowed_hosts(settings: Settings) -> list[str]:
    port = settings.http_port
    hosts = [f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"]
    bind = settings.http_host
    if not is_loopback(bind) and bind not in ("0.0.0.0", "::"):  # noqa: S104
        hosts.append(f"[{bind}]:{port}" if ":" in bind else f"{bind}:{port}")
    hosts.extend(_split(settings.http_allowed_hosts))
    return hosts


def allowed_origins(settings: Settings) -> list[str]:
    port = settings.http_port
    return [
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
        *_split(settings.http_allowed_origins),
    ]


def build_http_app(
    server: MCPServer[Any], settings: Settings, *, insecure_no_auth: bool = False
) -> ASGIApp:
    host = settings.http_host
    local = is_loopback(host)
    if not local and not settings.http_allow_nonlocal:
        raise ConfigError(
            f"MAX_HTTP_HOST={host}: разрешён только loopback (127.0.0.1). "
            "Иное — только с MAX_HTTP_ALLOW_NONLOCAL=1 и токеном (не рекомендуется)."
        )
    token = resolve_token(settings)
    if token is None:
        if not local:
            raise ConfigError(
                "HTTP не на loopback без токена запрещён: задайте MAX_HTTP_TOKEN_FILE"
            )
        if not insecure_no_auth:
            raise ConfigError(
                "Для HTTP нужен токен: max-mcp http-token (создаст файл 600) или "
                "MAX_HTTP_TOKEN_FILE / MAX_HTTP_TOKEN. Без авторизации — только --insecure-no-auth."
            )
    elif len(token) < 16:
        raise ConfigError("HTTP-токен слишком короткий (нужно ≥ 16 символов): max-mcp http-token")
    hosts = allowed_hosts(settings)
    origins = allowed_origins(settings)
    path = resolve_mcp_path(settings)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )
    app: ASGIApp = server.streamable_http_app(
        streamable_http_path=path,
        transport_security=security,
        host=host,
        max_request_body_size=settings.http_max_body,
    )
    if token is not None:
        app = BearerAuth(app, token)
    else:
        log.warning(
            "HTTP без авторизации (--insecure-no-auth): любой локальный процесс получит доступ к MAX"
        )
    app = BodyLimit(app, settings.http_max_body)
    app = HostOriginGuard(app, hosts=hosts, origins=origins)
    log.info("HTTP: http://%s:%s%s", host, settings.http_port, mask_path(path, settings))
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
