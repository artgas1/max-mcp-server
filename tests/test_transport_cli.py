"""stdio-сервер в подпроцессе, HTTP (bearer), CLI."""

from __future__ import annotations

import json
import os
import sys

import httpx
import pytest

from max_mcp_server.cli import main
from max_mcp_server.errors import ConfigError
from max_mcp_server.tools.read import READ_TOOL_NAMES
from max_mcp_server.transport.http import BearerAuth, build_http_app


async def test_stdio_server_subprocess(tmp_path):
    from mcp import Client, StdioServerParameters

    env = {k: v for k, v in os.environ.items() if not k.startswith("MAX_")}
    env.update(MAX_STATE_DIR=str(tmp_path / "state"), MAX_TZ="UTC")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "max_mcp_server", "serve", "--transport", "stdio"],
        env=env,
    )
    async with Client(params) as client:
        tools = (await client.list_tools()).tools
        names = {t.name for t in tools}
        res = await client.call_tool("max_list_chats", {})
    # по умолчанию read-only: ни одного инструмента записи
    assert names == set(READ_TOOL_NAMES)
    assert res.is_error
    assert "not_logged_in" in res.content[0].text


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def test_bearer_auth():
    app = BearerAuth(_ok_app, "s3cret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as c:
        assert (await c.get("/mcp")).status_code == 401
        assert (await c.get("/mcp", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await c.get("/mcp", headers={"Authorization": "Bearer s3cret"})).status_code == 200


def test_http_requires_loopback_and_token(settings, fake):
    from max_mcp_server.server import build_server

    server, _ = build_server(settings, fake)
    with pytest.raises(ConfigError, match="токен"):
        build_http_app(server, settings)
    with pytest.raises(ConfigError, match="loopback"):
        build_http_app(
            server, settings.model_copy(update={"http_host": "0.0.0.0", "http_token": "x"})
        )
    assert build_http_app(server, settings.model_copy(update={"http_token": "x" * 20})) is not None
    tok = settings.state_path.parent / "tok"
    tok.parent.mkdir(parents=True, exist_ok=True)
    tok.write_text("from-file\n")
    os.chmod(tok, 0o644)
    from max_mcp_server.errors import InsecurePermissions

    with pytest.raises(InsecurePermissions):
        build_http_app(server, settings.model_copy(update={"http_token_file": tok}))


def test_cli_tools(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_STATE_DIR", str(tmp_path / "s"))
    assert main(["tools", "--json"]) == 0
    tools = json.loads(capsys.readouterr().out)
    assert len(tools) == 20 and all(t["read_only"] for t in tools)
    assert main(["tools"]) == 0
    assert "Инструментов: 20 (только чтение)" in capsys.readouterr().out


def test_cli_status_logout_doctor(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_STATE_DIR", str(tmp_path / "s"))
    assert main(["status", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["session"] == "absent"
    from max_mcp_server.session.store import SecureFileStore

    SecureFileStore(tmp_path / "s").write({"client_kind": "web", "session": {"token": "SYNTH"}})
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "session: present" in out and "SYNTH" not in out
    assert main(["doctor"]) == 0
    assert "[FAIL]" not in capsys.readouterr().out
    assert main(["logout"]) == 0
    assert not (tmp_path / "s" / "session.json").exists()


def test_cli_login_phone_requires_mobile(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_STATE_DIR", str(tmp_path / "s"))
    assert main(["login", "phone", "--phone", "+70000000000"]) == 3
    assert "MAX_CLIENT=mobile" in capsys.readouterr().err
    assert main(["login", "token"]) == 3


def test_no_subcommand_means_serve_stdio(monkeypatch):
    """`uvx max-mcp-server` без аргументов (как в конфигах MCP-клиентов) = `serve` по stdio."""
    import max_mcp_server.cli as cli

    seen = []
    monkeypatch.setitem(cli.COMMANDS, "serve", lambda a: seen.append(a) or 0)
    assert main([]) == 0
    assert main(["--profile", "work"]) == 0
    assert [a.transport for a in seen] == ["stdio", "stdio"] and seen[1].profile == "work"
