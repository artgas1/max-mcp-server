from __future__ import annotations

import io
import os
import socket
import stat

import pytest
from pymax.session import SessionInfo

from max_mcp_server.adapter import pymax_adapter as pa
from max_mcp_server.errors import ConfigError, InsecurePermissions, UpstreamError
from max_mcp_server.session.auth import PasswordProvider, TerminalQrHandler
from max_mcp_server.session.store import SecureFileStore
from tests.conftest import NetworkBlocked
from tests.fakes.fake_pymax import FakePymaxClient


def test_network_guard_blocks_external():
    s = socket.socket()
    try:
        with pytest.raises(NetworkBlocked):
            s.connect(("192.0.2.1", 443))
    finally:
        s.close()
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("api.oneme.ru", 443)


async def test_qr_handler_writes_svg(tmp_path):
    out = io.StringIO()
    svg = tmp_path / "q" / "qr.svg"
    await TerminalQrHandler(svg, out=out).show_qr("https://example.invalid/qr/SYNTHETIC")
    assert svg.read_bytes().lstrip().startswith(b"<?xml") or b"<svg" in svg.read_bytes()
    assert stat.S_IMODE(os.stat(svg).st_mode) == 0o600
    assert "Подключить устройство" in out.getvalue()


async def test_password_from_file(tmp_path):
    f = tmp_path / "pw"
    f.write_text("  synthetic-pass \n")
    os.chmod(f, 0o644)
    with pytest.raises(InsecurePermissions):
        await PasswordProvider(f).get_password()
    os.chmod(f, 0o600)
    p = PasswordProvider(f)
    assert await p.get_password("hint") == "synthetic-pass"
    assert await p.get_password() == ""  # повтор того же файла не имеет смысла
    with pytest.raises(ConfigError):
        await PasswordProvider(tmp_path / "missing").get_password()


class _LoginClient(FakePymaxClient):
    def __init__(self, store, ok: bool) -> None:
        super().__init__()
        self.store, self.ok = store, ok

    async def connect(self) -> None:
        await self.store.save_session(SessionInfo(token="SYNTHETIC", device_id="d", phone=""))
        if not self.ok:
            raise RuntimeError("QR authentication expired")


async def test_session_written_only_after_success(settings, monkeypatch):
    store = SecureFileStore(settings.state_path)
    monkeypatch.setattr(pa, "build_client", lambda s, kind, st, **k: _LoginClient(st, ok=False))
    with pytest.raises(UpstreamError):
        await pa.login_token(settings, "SYNTHETIC")
    assert not store.exists()  # грабля tg-max-mcp: пустая/полусессия не остаётся

    monkeypatch.setattr(pa, "build_client", lambda s, kind, st, **k: _LoginClient(st, ok=True))
    me = await pa.login_token(settings, "SYNTHETIC")
    assert me.id == 1
    rec = store.read()
    assert rec["client_kind"] == "web" and rec["session"]["token"] == "SYNTHETIC"
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


async def test_qr_login_retries_expired(settings, monkeypatch):
    results = iter([False, False, True])
    monkeypatch.setattr(
        pa, "build_client", lambda s, kind, st, **k: _LoginClient(st, ok=next(results))
    )
    me = await pa.login_qr(
        settings, TerminalQrHandler(out=io.StringIO()), PasswordProvider(), timeout=60
    )
    assert me.id == 1 and SecureFileStore(settings.state_path).exists()


async def test_login_token_rejects_garbage(settings):
    with pytest.raises(ConfigError):
        await pa.login_token(settings, "a b")
