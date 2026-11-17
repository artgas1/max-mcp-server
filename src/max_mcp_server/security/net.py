"""Безопасное скачивание медиа MAX: allowlist хостов + защита от SSRF и DNS rebinding.

Почему недостаточно allowlist суффиксов: DNS-имя из allowlist может указывать (или начать
указывать между проверкой и подключением — DNS rebinding) на 127.0.0.1, 10.x, 169.254.169.254 и т.п.
Поэтому:

1. URL: только ``https`` (порт 443), без userinfo, хост — из allowlist суффиксов; IP-литералы
   проверяются как адреса (aiohttp не зовёт резолвер для IP-литерала).
2. Резолвинг делает ``VettingResolver``: каждый адрес обязан быть глобальным (не private /
   loopback / link-local / CGNAT / multicast / reserved), иначе — отказ. Исключение — сети из
   явного ``MAX_DOWNLOAD_ALLOW_IPS``. Подключение идёт ровно к проверенным адресам (кэш DNS
   aiohttp выключен, второго резолвинга нет) — это и есть «закрепление» IP.
3. Редиректы не следуют автоматически: каждый шаг (не больше 3) заново проходит пункты 1–2.
4. Размер ограничивается по потоку; файл пишется во временный ``.part`` с ``O_NOFOLLOW`` 0600 и
   атомарно переименовывается.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import os
import socket
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from max_mcp_server.adapter.base import MediaBlob
from max_mcp_server.errors import (
    ConfigError,
    Forbidden,
    InvalidArgument,
    MaxMcpError,
    UpstreamError,
)

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

MAX_REDIRECTS = 3
_REDIRECTS = frozenset({301, 302, 303, 307, 308})

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"OggS", "audio/ogg"),
    (b"ID3", "audio/mpeg"),
)
INLINE_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def sniff_mime(head: bytes, fallback: str = "application/octet-stream") -> str:
    """Тип по сигнатуре (а не по заголовку сервера)."""
    for sig, mime in _SIGNATURES:
        if head.startswith(sig):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    # заголовку сервера не доверяем для image/* — иначе inline отдаст не-картинку
    if fallback.startswith("image/"):
        return "application/octet-stream"
    return fallback


def parse_networks(raw: str) -> tuple[IPNetwork, ...]:
    out: list[IPNetwork] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(ipaddress.ip_network(part, strict=False))
        except ValueError as exc:
            raise ConfigError(f"MAX_DOWNLOAD_ALLOW_IPS: неверная сеть {part!r}") from exc
    return tuple(out)


def ip_allowed(ip: IPAddress, allow: Sequence[IPNetwork] = ()) -> bool:
    """Глобальный unicast-адрес или явно разрешённая сеть."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if any(ip in net for net in allow):
        return True
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def host_allowed(host: str, suffixes: Sequence[str]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


class VettingResolver:
    """aiohttp-резолвер, который пропускает только разрешённые адреса (и только их отдаёт)."""

    def __init__(
        self,
        allow: Sequence[IPNetwork] = (),
        getaddrinfo: Callable[..., Any] | None = None,
    ) -> None:
        self.allow = tuple(allow)
        self._getaddrinfo = getaddrinfo
        self.resolved: list[tuple[str, str]] = []  # (host, ip) — для тестов/диагностики

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[dict[str, Any]]:
        gai = self._getaddrinfo or socket.getaddrinfo
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.run_in_executor(
                None, lambda: gai(host, port, family, socket.SOCK_STREAM)
            )
        except OSError as exc:
            raise UpstreamError(f"Не удалось разрешить имя {host}", retryable=True) from exc
        out: list[dict[str, Any]] = []
        for fam, _type, proto, _canon, sockaddr in infos:
            addr = str(sockaddr[0]).split("%", 1)[0]
            ip = ipaddress.ip_address(addr)
            if not ip_allowed(ip, self.allow):
                raise Forbidden(
                    f"Хост {host} указывает на внутренний/служебный адрес — скачивание запрещено"
                )
            out.append(
                {
                    "hostname": host,
                    "host": addr,
                    "port": int(sockaddr[1]) or port,
                    "family": fam,
                    "proto": proto,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
            self.resolved.append((host, addr))
        if not out:
            raise UpstreamError(f"Не удалось разрешить имя {host}", retryable=True)
        return out

    async def close(self) -> None:
        return None


def _find_our_error(exc: BaseException) -> MaxMcpError | None:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, MaxMcpError):
            return cur
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return None


class SafeDownloader:
    def __init__(
        self,
        *,
        host_suffixes: Sequence[str],
        allow_networks: Sequence[IPNetwork] = (),
        require_https: bool = True,
        max_redirects: int = MAX_REDIRECTS,
        getaddrinfo: Callable[..., Any] | None = None,
        total_timeout: float = 300,
    ) -> None:
        self.host_suffixes = tuple(s.lower() for s in host_suffixes)
        self.allow = tuple(allow_networks)
        self.require_https = require_https
        self.max_redirects = max_redirects
        self.getaddrinfo = getaddrinfo
        self.total_timeout = total_timeout
        self.last_resolver: VettingResolver | None = None

    def check_url(self, url: str) -> str:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        scheme_ok = parts.scheme == "https" or (not self.require_https and parts.scheme == "http")
        if not scheme_ok or not host:
            raise Forbidden("Скачивание разрешено только по https с серверов MAX")
        if parts.username is not None or parts.password is not None:
            raise Forbidden("URL с логином/паролем не допускаются")
        try:
            port = parts.port
        except ValueError as exc:
            raise Forbidden("Некорректный порт в URL") from exc
        if self.require_https and port not in (None, 443):
            raise Forbidden("Скачивание разрешено только с порта 443")
        try:
            literal: IPAddress | None = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            literal = None
        if literal is not None:
            if not ip_allowed(literal, self.allow):
                raise Forbidden("Скачивание с внутреннего/служебного адреса запрещено")
            if not any(literal in n for n in self.allow):
                raise Forbidden("Скачивание по IP-адресу запрещено — только хосты MAX")
            return host
        if not host_allowed(host, self.host_suffixes):
            raise Forbidden(f"Хост {host} не входит в allowlist загрузок MAX (MAX_DOWNLOAD_HOSTS)")
        return host

    async def fetch(self, url: str, dest: Path, *, max_bytes: int) -> MediaBlob:
        import aiohttp

        resolver = VettingResolver(self.allow, self.getaddrinfo)
        self.last_resolver = resolver
        tmp = dest.with_name(f".{dest.name}.part")
        digest = hashlib.sha256()
        size = 0
        head = b""
        timeout = aiohttp.ClientTimeout(total=self.total_timeout, sock_read=60)
        connector = aiohttp.TCPConnector(
            resolver=resolver,  # type: ignore[arg-type]
            use_dns_cache=False,
            limit=1,
        )
        try:
            async with aiohttp.ClientSession(
                timeout=timeout, trust_env=False, connector=connector
            ) as http:
                hops = 0
                while True:
                    self.check_url(url)
                    async with http.get(
                        url, allow_redirects=False, headers={"Accept-Encoding": "identity"}
                    ) as resp:
                        if resp.status in _REDIRECTS:
                            location = resp.headers.get("Location")
                            hops += 1
                            if not location or hops > self.max_redirects:
                                raise Forbidden("Слишком много перенаправлений при скачивании")
                            url = urljoin(url, location)
                            continue
                        if resp.status != 200:
                            raise UpstreamError(f"Сервер файлов MAX ответил {resp.status}")
                        if resp.content_length and resp.content_length > max_bytes:
                            raise InvalidArgument(
                                f"Файл больше лимита {max_bytes} байт (MAX_DOWNLOAD_MAX_BYTES)"
                            )
                        fd = os.open(
                            tmp,
                            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                            0o600,
                        )
                        with os.fdopen(fd, "wb") as fh:
                            async for chunk in resp.content.iter_chunked(65536):
                                size += len(chunk)
                                if size > max_bytes:
                                    raise InvalidArgument(
                                        f"Файл больше лимита {max_bytes} байт (MAX_DOWNLOAD_MAX_BYTES)"
                                    )
                                if len(head) < 16:
                                    head += chunk[: 16 - len(head)]
                                digest.update(chunk)
                                fh.write(chunk)
                        header_mime = (resp.headers.get("Content-Type") or "").split(";")[0]
                        break
            os.replace(tmp, dest)
        except BaseException as exc:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
            if isinstance(exc, MaxMcpError):
                raise
            ours = _find_our_error(exc) if isinstance(exc, Exception) else None
            if ours is not None:
                raise ours from exc
            if isinstance(exc, Exception):
                raise UpstreamError(
                    "Не удалось скачать файл с сервера MAX", retryable=True
                ) from exc
            raise
        mime = sniff_mime(head, header_mime.strip() or "application/octet-stream")
        return MediaBlob(path=dest, size=size, mime=mime, sha256=digest.hexdigest())
