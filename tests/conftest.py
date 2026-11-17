"""Общие фикстуры. Сетевой страж: любое подключение не к loopback/unix → падение теста."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from max_mcp_server.config import Settings
from max_mcp_server.security.policy import Policy
from max_mcp_server.server import build_server
from tests.fakes.fake_adapter import FakeMaxAdapter

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


class NetworkBlocked(AssertionError):
    pass


def _allowed(sock: socket.socket, address: Any) -> bool:
    if sock.family == getattr(socket, "AF_UNIX", object()):
        return True
    host = address[0] if isinstance(address, tuple) else address
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _guard_connect(self: socket.socket, address: Any) -> Any:
    if not _allowed(self, address):
        raise NetworkBlocked(f"тест пытается выйти в сеть: {address!r}")
    return _real_connect(self, address)


def _guard_connect_ex(self: socket.socket, address: Any) -> Any:
    if not _allowed(self, address):
        raise NetworkBlocked(f"тест пытается выйти в сеть: {address!r}")
    return _real_connect_ex(self, address)


def _guard_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    if host not in (None, "localhost", "127.0.0.1", "::1", b"localhost"):
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise NetworkBlocked(f"тест резолвит внешний хост: {host!r}")
        except ValueError as exc:
            raise NetworkBlocked(f"тест резолвит внешний хост: {host!r}") from exc
    return _real_getaddrinfo(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _guard_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guard_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", _guard_getaddrinfo)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Никаких настоящих MAX_*/XDG из окружения разработчика."""
    import os

    for key in list(os.environ):
        if key.startswith("MAX_") or key == "QR_SVG":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("MAX_TZ", "UTC")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(tz="UTC", state_dir=tmp_path / "st", download_dir=tmp_path / "dl")


@pytest.fixture
def fake() -> FakeMaxAdapter:
    return FakeMaxAdapter()


@pytest.fixture
def make_client(settings: Settings, fake: FakeMaxAdapter) -> Any:
    """Фабрика in-process MCP-клиента над FakeMaxAdapter с заданной политикой."""
    from contextlib import asynccontextmanager

    from mcp import Client

    @asynccontextmanager
    async def factory(policy: Policy | None = None, **overrides: Any) -> AsyncIterator[Any]:
        s = settings.model_copy(update=overrides) if overrides else settings
        server, _ = build_server(s, fake, policy)
        async with Client(server) as client:
            yield client

    return factory
