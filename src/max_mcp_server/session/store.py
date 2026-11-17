"""SecureFileStore: сессия MAX в JSON-файле 0600 внутри каталога 0700.

Модуль не знает о PyMax: хранит произвольный ``dict``. Мост к ``pymax.session.StoreProtocol``
живёт в ``adapter/pymax_adapter.py``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
from pathlib import Path
from typing import Any

from max_mcp_server.errors import InsecurePermissions, ProfileLocked

SESSION_FILE = "session.json"
LOCK_FILE = "serve.lock"
DIR_MODE = 0o700
FILE_MODE = 0o600
FORMAT_VERSION = 1


def ensure_private_dir(path: Path, *, fix: bool = False) -> None:
    """Создаёт каталог 0700 или проверяет существующий (владелец, права, не symlink)."""
    if path.is_symlink():
        raise InsecurePermissions(f"Каталог {path} — символьная ссылка; укажите реальный путь")
    if not path.exists():
        path.mkdir(parents=True, mode=DIR_MODE, exist_ok=True)
        os.chmod(path, DIR_MODE)
        return
    st = path.stat()
    if not stat.S_ISDIR(st.st_mode):
        raise InsecurePermissions(f"{path} существует и это не каталог")
    if st.st_uid != os.getuid():
        raise InsecurePermissions(f"Каталог {path} принадлежит другому пользователю")
    if stat.S_IMODE(st.st_mode) & 0o077:
        if fix:
            os.chmod(path, DIR_MODE)
        else:
            raise InsecurePermissions(
                f"Каталог {path} доступен другим (права {oct(stat.S_IMODE(st.st_mode))}); "
                "нужно 700. Исправить: max-mcp status --fix-perms"
            )


def check_private_file(path: Path, *, fix: bool = False) -> None:
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        raise InsecurePermissions(f"{path} — символьная ссылка; отказ")
    if not stat.S_ISREG(st.st_mode):
        raise InsecurePermissions(f"{path} — не обычный файл")
    if st.st_uid != os.getuid():
        raise InsecurePermissions(f"Файл {path} принадлежит другому пользователю")
    if stat.S_IMODE(st.st_mode) & 0o077:
        if fix:
            os.chmod(path, FILE_MODE)
        else:
            raise InsecurePermissions(
                f"Файл {path} доступен другим (права {oct(stat.S_IMODE(st.st_mode))}); "
                "нужно 600. Исправить: max-mcp status --fix-perms"
            )


def write_private_bytes(path: Path, data: bytes) -> None:
    """Атомарная запись: tmp (O_EXCL|O_NOFOLLOW, 0600) → fsync → os.replace → fsync каталога."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class SecureFileStore:
    """Одна сессия на профиль: ``<state_dir>/session.json``."""

    def __init__(self, state_dir: Path) -> None:
        self.dir = state_dir
        self.path = state_dir / SESSION_FILE

    def check(self, *, fix: bool = False) -> None:
        ensure_private_dir(self.dir, fix=fix)
        if self.path.exists() or self.path.is_symlink():
            check_private_file(self.path, fix=fix)

    def exists(self) -> bool:
        return self.path.is_file()

    def read(self) -> dict[str, Any] | None:
        """Читает сессию. ``None`` — файла нет. Битый файл → ``InsecurePermissions``-free ошибка."""
        self.check()
        if not self.path.exists():
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags)
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
            return None
        return data

    def write(self, data: dict[str, Any]) -> None:
        ensure_private_dir(self.dir)
        payload = {"version": FORMAT_VERSION, **{k: v for k, v in data.items() if k != "version"}}
        write_private_bytes(self.path, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def delete(self) -> bool:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True


class ProfileLock:
    """Эксклюзивная блокировка профиля (одно соединение с MAX на профиль)."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / LOCK_FILE
        self._fd: int | None = None

    def acquire(self) -> None:
        ensure_private_dir(self.path.parent)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise ProfileLocked(
                "Профиль уже используется другим процессом max-mcp (одно соединение на профиль). "
                "Остановите его или задайте другой MAX_PROFILE."
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> ProfileLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
