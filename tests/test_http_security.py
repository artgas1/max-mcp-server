"""HTTP-транспорт: токен, Host/Origin (DNS rebinding), секретный путь, лимит тела, не-loopback.

Всё через ``httpx.ASGITransport`` — без сокетов; lifespan приложения запускается вручную.
"""

from __future__ import annotations

import asyncio
import os
import stat
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from max_mcp_server.cli import main
from max_mcp_server.errors import ConfigError
from max_mcp_server.server import build_server
from max_mcp_server.transport.http import (
    BearerAuth,
    BodyLimit,
    build_http_app,
    ensure_secret_file,
    resolve_token,
)

TOKEN = "t" * 32
PORT = 13430
GOOD = {"host": f"127.0.0.1:{PORT}", "authorization": f"Bearer {TOKEN}"}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
ACCEPT = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@asynccontextmanager
async def lifespan(app):
    inbox: asyncio.Queue = asyncio.Queue()
    outbox: asyncio.Queue = asyncio.Queue()
    await inbox.put({"type": "lifespan.startup"})
    task = asyncio.create_task(
        app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, inbox.get, outbox.put)
    )
    msg = await asyncio.wait_for(outbox.get(), 5)
    assert msg["type"] == "lifespan.startup.complete", msg
    try:
        yield
    finally:
        await inbox.put({"type": "lifespan.shutdown"})
        await asyncio.wait_for(outbox.get(), 5)
        await asyncio.wait_for(task, 5)


@asynccontextmanager
async def client_for(settings, fake, **upd):
    s = settings.model_copy(update={"http_token": TOKEN, **upd})
    server, _ = build_server(s, fake)
    app = build_http_app(server, s)
    async with (
        lifespan(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"http://127.0.0.1:{PORT}"
        ) as c,
    ):
        yield c


async def test_http_full_flow(settings, fake):
    async with client_for(settings, fake) as c:
        # нет токена → 401
        r = await c.post("/mcp", json=INIT, headers={**ACCEPT, "host": GOOD["host"]})
        assert r.status_code == 401
        # неверный токен → 401
        r = await c.post(
            "/mcp", json=INIT, headers={**ACCEPT, **GOOD, "authorization": "Bearer nope"}
        )
        assert r.status_code == 401
        # чужой Host (DNS rebinding) → 403, даже с правильным токеном
        r = await c.post("/mcp", json=INIT, headers={**ACCEPT, **GOOD, "host": "evil.example"})
        assert r.status_code == 403
        r = await c.post(
            "/mcp", json=INIT, headers={**ACCEPT, **GOOD, "host": f"evil.example:{PORT}"}
        )
        assert r.status_code == 403
        # чужой Origin (страница в браузере) → 403
        r = await c.post(
            "/mcp", json=INIT, headers={**ACCEPT, **GOOD, "origin": "https://evil.example"}
        )
        assert r.status_code == 403
        # чужой путь → 404
        r = await c.post("/other", json=INIT, headers={**ACCEPT, **GOOD})
        assert r.status_code == 404
        # правильный запрос (Origin loopback допустим)
        r = await c.post(
            "/mcp",
            json=INIT,
            headers={**ACCEPT, **GOOD, "origin": f"http://localhost:{PORT}"},
        )
        assert r.status_code == 200, r.text
        assert "max-mcp-server" in r.text


async def test_http_body_limit(settings, fake):
    async with client_for(settings, fake, http_max_body=2048) as c:
        big = {**INIT, "pad": "x" * 5000}
        r = await c.post("/mcp", json=big, headers={**ACCEPT, **GOOD})
        assert r.status_code == 413


async def test_body_limit_chunked():
    seen = []

    async def app(scope, receive, send):
        seen.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    limited = BodyLimit(app, 10)

    async def chunks():
        for _ in range(5):
            yield b"abcd"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=limited), base_url="http://127.0.0.1"
    ) as c:
        assert (await c.post("/", content=chunks())).status_code == 413
        assert (await c.post("/", content=b"12345")).status_code == 200
    assert seen[-1]["body"] == b"12345"


async def test_http_secret_path(settings, fake):
    s = settings.model_copy(update={"http_secret_path": True})
    async with client_for(s, fake) as c:
        secret = s.http_secret_file.read_text().strip()
        assert len(secret) >= 40
        assert stat.S_IMODE(s.http_secret_file.stat().st_mode) == 0o600
        r = await c.post("/mcp", json=INIT, headers={**ACCEPT, **GOOD})
        assert r.status_code == 404
        r = await c.post(f"/mcp/{secret}", json=INIT, headers={**ACCEPT, **GOOD})
        assert r.status_code == 200


async def test_bearer_constant_time_and_empty():
    async def ok(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = BearerAuth(ok, TOKEN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as c:
        assert (await c.get("/", headers={"authorization": "Bearer "})).status_code == 401
        assert (await c.get("/", headers={"authorization": f"Basic {TOKEN}"})).status_code == 401
        assert (await c.get("/", headers={"authorization": f"Bearer {TOKEN}"})).status_code == 200


def test_http_config_refusals(settings, fake):
    server, _ = build_server(settings, fake)
    with pytest.raises(ConfigError, match="токен"):
        build_http_app(server, settings)
    # без токена с флагом — можно только на loopback
    assert build_http_app(server, settings, insecure_no_auth=True) is not None
    nonlocal_ = settings.model_copy(update={"http_host": "192.168.10.20"})
    with pytest.raises(ConfigError, match="loopback"):
        build_http_app(server, nonlocal_.model_copy(update={"http_token": TOKEN}))
    allowed = nonlocal_.model_copy(update={"http_allow_nonlocal": True})
    with pytest.raises(ConfigError, match="без токена"):
        build_http_app(server, allowed, insecure_no_auth=True)
    assert build_http_app(server, allowed.model_copy(update={"http_token": TOKEN})) is not None
    with pytest.raises(ConfigError, match="короткий"):
        build_http_app(server, settings.model_copy(update={"http_token": "short"}))


async def test_nonlocal_host_header_allowed(settings, fake):
    s = settings.model_copy(
        update={"http_host": "192.168.10.20", "http_allow_nonlocal": True, "http_token": TOKEN}
    )
    server, _ = build_server(s, fake)
    app = build_http_app(server, s)
    async with (
        lifespan(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://192.168.10.20:13430"
        ) as c,
    ):
        hdr = {**ACCEPT, "authorization": f"Bearer {TOKEN}", "host": "192.168.10.20:13430"}
        assert (await c.post("/mcp", json=INIT, headers=hdr)).status_code == 200
        hdr["host"] = "attacker.example:13430"
        assert (await c.post("/mcp", json=INIT, headers=hdr)).status_code == 403


def test_token_resolution_and_file(settings, tmp_path: Path):
    assert resolve_token(settings) is None
    tok = ensure_secret_file(settings.http_token_default_file)
    assert stat.S_IMODE(settings.http_token_default_file.stat().st_mode) == 0o600
    assert resolve_token(settings) == tok  # файл http-token подхватывается сам
    assert ensure_secret_file(settings.http_token_default_file) == tok
    assert ensure_secret_file(settings.http_token_default_file, rotate=True) != tok
    f = tmp_path / "tokfile"
    f.write_text("x" * 20)
    os.chmod(f, 0o644)
    from max_mcp_server.errors import InsecurePermissions

    with pytest.raises(InsecurePermissions):
        resolve_token(settings.model_copy(update={"http_token_file": f}))


def test_cli_http_token(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MAX_STATE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("MAX_HTTP_SECRET_PATH", "true")
    assert main(["http-token"]) == 0
    out = capsys.readouterr().out
    token = (tmp_path / "s" / "http_token").read_text().strip()
    secret = (tmp_path / "s" / "http_path_secret").read_text().strip()
    assert token not in out and secret not in out and "/mcp/<secret>" in out
    assert main(["http-token", "--show"]) == 0
    out = capsys.readouterr().out
    assert token in out and secret in out
    assert main(["http-token", "--rotate"]) == 0
    assert (tmp_path / "s" / "http_token").read_text().strip() != token
