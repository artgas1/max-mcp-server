"""Провайдеры для интерактивного входа (только CLI ``max-mcp login``).

Классы совместимы по форме с протоколами PyMax (``QrHandler``, ``PasswordProvider``,
``SmsCodeProvider``), но модуль PyMax не импортирует. Сам вход — в
``adapter/pymax_adapter.py`` (``login_qr`` / ``login_token`` / ``login_phone``).
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import os
import stat
import sys
from pathlib import Path
from typing import TextIO

from max_mcp_server.errors import ConfigError, InsecurePermissions
from max_mcp_server.session.qr import render_ascii, write_svg

QR_HINT = (
    "Телефон: MAX → Настройки → Устройства → «Подключить устройство» → отсканируйте QR.\n"
    "QR живёт 1–2 минуты и обновляется сам. Ждём подтверждения…"
)


class TerminalQrHandler:
    """Показывает QR в терминале (stderr) и, если задан путь, пишет SVG."""

    def __init__(self, svg_path: Path | None = None, out: TextIO | None = None) -> None:
        self.svg_path = svg_path
        self.out = out or sys.stderr
        self.shown = 0

    async def show_qr(self, qr_url: str) -> None:
        self.shown += 1
        if self.out.isatty():
            self.out.write("\x1b[2J\x1b[H")
        render_ascii(qr_url, self.out)
        if self.svg_path is not None:
            if self.shown == 1:
                self._warn_shared_dir()
            write_svg(qr_url, self.svg_path)
            self.out.write(f"\nQR также записан в {self.svg_path}\n")
        self.out.write("\n" + QR_HINT + "\n")
        self.out.flush()

    def _warn_shared_dir(self) -> None:
        assert self.svg_path is not None
        parent = self.svg_path.expanduser().parent
        with contextlib.suppress(OSError):
            if stat.S_IMODE(parent.stat().st_mode) & 0o077:
                self.out.write(
                    f"ВНИМАНИЕ: каталог {parent} доступен другим пользователям. QR — это вход в "
                    "аккаунт: не раздавайте SVG через веб-сервер и общие каталоги.\n"
                )

    def cleanup(self) -> None:
        """Удаляет SVG после входа, тайм-аута или Ctrl-C (L5)."""
        if self.svg_path is not None:
            with contextlib.suppress(OSError):
                self.svg_path.expanduser().unlink(missing_ok=True)


MAX_SECRET_BYTES = 64 * 1024


def read_secret_file(path: Path) -> str:
    """Читает секрет из файла с правами 600 (не шире).

    Без TOCTOU (L6): файл открывается с ``O_NOFOLLOW`` (symlink → отказ), а владелец, тип и
    права проверяются ``fstat`` уже открытого дескриптора — подмена после проверки не поможет."""
    path = path.expanduser()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise ConfigError(f"Файл {path} не найден") from exc
    except OSError as exc:  # ELOOP: символьная ссылка
        raise InsecurePermissions(f"{path}: не удалось открыть без перехода по ссылке") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise InsecurePermissions(f"{path} — не обычный файл")
        if st.st_uid != os.getuid():
            raise InsecurePermissions(f"Файл {path} принадлежит другому пользователю")
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise InsecurePermissions(
                f"Файл {path} доступен другим (права {oct(stat.S_IMODE(st.st_mode))}); нужно 600"
            )
        data = os.read(fd, MAX_SECRET_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_SECRET_BYTES:
        raise ConfigError(f"Файл {path} слишком большой для секрета")
    value = data.decode("utf-8").strip()
    if not value:
        raise ConfigError(f"Файл {path} пуст")
    return value


class PasswordProvider:
    """Пароль 2FA: из файла ``MAX_2FA_PASSWORD_FILE`` или из TTY (getpass)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.calls = 0

    async def get_password(self, hint: str | None = None) -> str:
        self.calls += 1
        if self.path is not None:
            if self.calls > 1:
                # файл не меняется между попытками — повторять бессмысленно
                return ""
            return read_secret_file(self.path)
        if not sys.stdin.isatty():
            raise ConfigError(
                "Нужен пароль 2FA, но нет терминала. Задайте MAX_2FA_PASSWORD_FILE (права 600)."
            )
        prompt = "Пароль 2FA MAX" + (f" (подсказка: {hint})" if hint else "") + ": "
        return (await asyncio.to_thread(getpass.getpass, prompt)).strip()


class TtySmsCodeProvider:
    """Код из SMS — только интерактивно."""

    async def get_code(self, phone: str) -> str:
        if not sys.stdin.isatty():
            raise ConfigError("Код из SMS можно ввести только в интерактивном терминале")
        masked = phone[:2] + "…" + phone[-2:] if len(phone) > 4 else "…"
        sys.stderr.write(f"Код из SMS для {masked}: ")
        sys.stderr.flush()
        return (await asyncio.to_thread(sys.stdin.readline)).strip()
