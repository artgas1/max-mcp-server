"""CLI: ``max-mcp serve | login {qr,token,phone} | status | logout | tools | doctor``."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from max_mcp_server import __version__
from max_mcp_server.config import Settings, load_settings
from max_mcp_server.errors import EXIT_CONFIG, EXIT_NO_SESSION, EXIT_OK, MaxMcpError, NotLoggedIn
from max_mcp_server.logging import setup_logging


def _err(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="max-mcp", description="MCP-сервер для мессенджера MAX")
    p.add_argument("--version", action="version", version=f"max-mcp-server {__version__}")
    p.add_argument("--profile", help="профиль (MAX_PROFILE)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="запустить MCP-сервер")
    s.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    ro = s.add_mutually_exclusive_group()
    ro.add_argument("--read-only", dest="read_only", action="store_true", default=None)
    ro.add_argument("--no-read-only", dest="read_only", action="store_false")
    s.add_argument(
        "--insecure-no-auth", action="store_true", help="HTTP без токена (только отладка)"
    )

    lg = sub.add_parser("login", help="войти в MAX (интерактивно, в терминале)")
    lsub = lg.add_subparsers(dest="method", required=True)
    q = lsub.add_parser("qr", help="по QR-коду (web-клиент)")
    q.add_argument("--svg", help="куда писать QR как SVG (или QR_SVG)")
    q.add_argument("--timeout", type=int, default=300)
    t = lsub.add_parser("token", help="по токену существующей сессии")
    src = t.add_mutually_exclusive_group()
    src.add_argument("--from-env", action="store_true", help="из MAX_LOGIN_TOKEN (по умолчанию)")
    src.add_argument("--from-file", help="из файла (права 600)")
    src.add_argument("--stdin", action="store_true", help="со stdin")
    t.add_argument("--client", choices=["web", "mobile"], default=None)
    ph = lsub.add_parser("phone", help="по SMS (только MAX_CLIENT=mobile)")
    ph.add_argument("--phone", help="номер (или MAX_PHONE)")

    st = sub.add_parser("status", help="состояние сессии")
    st.add_argument("--json", action="store_true")
    st.add_argument("--probe", action="store_true", help="пробное подключение к MAX")
    st.add_argument("--fix-perms", action="store_true", help="исправить права каталога/файла")

    lo = sub.add_parser("logout", help="удалить локальную сессию")
    lo.add_argument("--remote", action="store_true", help="ещё и завершить сессию на сервере MAX")

    tl = sub.add_parser("tools", help="список инструментов при текущем конфиге")
    tl.add_argument("--json", action="store_true")
    tl.add_argument("--no-read-only", dest="read_only", action="store_false", default=None)

    sub.add_parser("doctor", help="проверка окружения (без сети)")
    return p


def _settings(args: argparse.Namespace, **extra: Any) -> Settings:
    overrides: dict[str, Any] = {k: v for k, v in extra.items() if v is not None}
    if args.profile:
        overrides["profile"] = args.profile
    return load_settings(**overrides)


# ---------------------------------------------------------------------------- команды


def cmd_serve(args: argparse.Namespace) -> int:
    settings = _settings(args, read_only=args.read_only, http_host=args.host, http_port=args.port)
    setup_logging(settings.log_level, pymax_debug=settings.log_pymax_debug)
    os.umask(0o077)
    from max_mcp_server.adapter.pymax_adapter import PyMaxAdapter
    from max_mcp_server.server import build_server

    adapter = PyMaxAdapter(settings)
    server, _ = build_server(settings, adapter)
    if args.transport == "http":
        from max_mcp_server.transport.http import build_http_app, run_http

        build_http_app(
            server, settings, insecure_no_auth=args.insecure_no_auth
        )  # проверка конфига до старта
        asyncio.run(run_http(server, adapter, settings, insecure_no_auth=args.insecure_no_auth))
    else:
        from max_mcp_server.transport.stdio import run_stdio

        asyncio.run(run_stdio(server, adapter))
    return EXIT_OK


def _print_me(me: Any) -> None:
    print(f"OK: вход выполнен, id {me.id}. Сессия сохранена (права 600).")


def cmd_login(args: argparse.Namespace) -> int:
    settings = _settings(args)
    setup_logging(
        "WARNING" if settings.log_level == "INFO" else settings.log_level,
        pymax_debug=settings.log_pymax_debug,
    )
    os.umask(0o077)
    from max_mcp_server.adapter import pymax_adapter as pa
    from max_mcp_server.session.auth import (
        PasswordProvider,
        TerminalQrHandler,
        TtySmsCodeProvider,
        read_secret_file,
    )
    from max_mcp_server.session.store import ProfileLock

    lock = ProfileLock(settings.state_path)
    lock.acquire()  # не входим, пока работает serve этого профиля
    try:
        pwd = PasswordProvider(settings.twofa_password_file)
        if args.method == "qr":
            from pathlib import Path

            svg = Path(args.svg) if args.svg else settings.qr_svg
            me = asyncio.run(
                pa.login_qr(settings, TerminalQrHandler(svg), pwd, timeout=args.timeout)
            )
        elif args.method == "token":
            if args.from_file:
                from pathlib import Path

                token = read_secret_file(Path(args.from_file))
            elif args.stdin:
                token = sys.stdin.readline().strip()
            else:
                token = settings.login_token or ""
                if not token:
                    _err("Задайте MAX_LOGIN_TOKEN, --from-file или --stdin")
                    return EXIT_CONFIG
            me = asyncio.run(pa.login_token(settings, token, kind=args.client or settings.client))
        else:
            if settings.client != "mobile":
                _err(
                    "Вход по SMS доступен только с MAX_CLIENT=mobile (web-версия MAX закрыта капчей)."
                )
                return EXIT_CONFIG
            phone = args.phone or settings.phone
            if not phone:
                _err("Укажите --phone или MAX_PHONE")
                return EXIT_CONFIG
            me = asyncio.run(pa.login_phone(settings, phone, TtySmsCodeProvider(), pwd))
    finally:
        lock.release()
    _print_me(me)
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    settings = _settings(args)
    setup_logging("WARNING")
    from max_mcp_server.session.store import SecureFileStore

    store = SecureFileStore(settings.state_path)
    info: dict[str, Any] = {"profile": settings.profile, "state_dir": str(settings.state_path)}
    code = EXIT_OK
    try:
        if settings.state_path.exists():
            store.check(fix=args.fix_perms)
        info["permissions"] = "ok"
        record = store.read() if settings.state_path.exists() else None
        info["session"] = "present" if record else "absent"
        if record:
            info["client"] = record.get("client_kind", "web")
        if not record:
            code = EXIT_NO_SESSION
        elif args.probe:
            from max_mcp_server.adapter.pymax_adapter import probe_session

            me = asyncio.run(probe_session(settings))
            info["connection"] = "ok"
            info["me_id"] = me.id
    except MaxMcpError as exc:
        info["error"] = exc.public_message
        code = exc.exit_code
    if args.json:
        print(json.dumps(info, ensure_ascii=False))
    else:
        for k, v in info.items():
            print(f"{k}: {v}")
    return code


def cmd_logout(args: argparse.Namespace) -> int:
    settings = _settings(args)
    setup_logging("WARNING")
    from max_mcp_server.session.store import ProfileLock, SecureFileStore

    store = SecureFileStore(settings.state_path)
    if not store.exists():
        print("Локальной сессии нет.")
        return EXIT_OK
    with ProfileLock(settings.state_path):
        if args.remote:
            from max_mcp_server.adapter.pymax_adapter import logout_remote

            try:
                ok = asyncio.run(logout_remote(settings))
                print("Сессия на сервере MAX завершена." if ok else "MAX не подтвердил выход.")
            except MaxMcpError as exc:
                _err(f"Не удалось завершить сессию на сервере: {exc.public_message}")
        store.delete()
    print("Локальная сессия удалена.")
    return EXIT_OK


def cmd_tools(args: argparse.Namespace) -> int:
    settings = _settings(args, read_only=args.read_only)
    setup_logging("WARNING")
    from max_mcp_server.adapter.pymax_adapter import PyMaxAdapter
    from max_mcp_server.server import build_server

    server, _ = build_server(settings, PyMaxAdapter(settings))
    tools = asyncio.run(server.list_tools())
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": t.name,
                        "title": t.title,
                        "read_only": bool(t.annotations and t.annotations.read_only_hint),
                        "description": t.description,
                    }
                    for t in tools
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        mode = "только чтение" if settings.read_only else "чтение и запись"
        print(f"Инструментов: {len(tools)} ({mode})")
        for t in tools:
            ro = t.annotations is not None and t.annotations.read_only_hint
            print(f"  {'R' if ro else 'W'}  {t.name:28} {t.title or ''}")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    setup_logging("WARNING")
    checks: list[tuple[str, bool, str]] = []
    try:
        settings = _settings(args)
        checks.append(
            ("конфигурация", True, f"профиль {settings.profile}, клиент {settings.client}")
        )
    except MaxMcpError as exc:
        print(f"[FAIL] конфигурация: {exc.public_message}")
        return EXIT_CONFIG
    try:
        pymax_v = version("maxapi-python")
    except PackageNotFoundError:
        pymax_v = "не установлен"
    checks.append(("PyMax", pymax_v == "2.5.0", f"maxapi-python {pymax_v} (нужен 2.5.0)"))
    from max_mcp_server.adapter.pymax_adapter import PendingStore, build_extra_config

    extra = build_extra_config(settings, PendingStore())
    checks.append(
        (
            "телеметрия PyMax",
            extra.telemetry is False,
            "выключена" if not extra.telemetry else "ВКЛЮЧЕНА",
        )
    )
    checks.append(("relogin", extra.relogin is False, "выключен (сервер не спрашивает вход)"))
    from pymax.versions.catalog import VersionCatalog

    checks.append(
        ("каталог версий", VersionCatalog(remote=False).remote is False, "только встроенный")
    )
    from max_mcp_server.session.store import SecureFileStore

    store = SecureFileStore(settings.state_path)
    try:
        if settings.state_path.exists():
            store.check()
        checks.append(("права state_dir", True, str(settings.state_path)))
        checks.append(
            ("сессия", store.exists(), "есть" if store.exists() else "нет — max-mcp login qr")
        )
    except MaxMcpError as exc:
        checks.append(("права state_dir", False, exc.public_message))
    checks.append(("режим", True, "только чтение" if settings.read_only else "запись разрешена"))
    checks.append(
        (
            "в сети",
            True,
            "показывать" if settings.appear_online else "не показывать (MAX_APPEAR_ONLINE=false)",
        )
    )
    ok = True
    for name, passed, detail in checks:
        ok &= passed
        print(f"[{'OK' if passed else 'FAIL'}] {name}: {detail}")
    print("Сетевая проверка не выполняется; для пробного подключения: max-mcp status --probe")
    return EXIT_OK if ok else EXIT_CONFIG


COMMANDS = {
    "serve": cmd_serve,
    "login": cmd_login,
    "status": cmd_status,
    "logout": cmd_logout,
    "tools": cmd_tools,
    "doctor": cmd_doctor,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return COMMANDS[args.cmd](args)
    except NotLoggedIn as exc:
        _err(exc.public_message)
        return exc.exit_code
    except MaxMcpError as exc:
        _err(f"Ошибка: {exc.public_message}")
        return exc.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
