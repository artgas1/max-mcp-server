"""Безопасная работа с локальными файлами (design.md, 8.2 «чтение/запись файлов вне каталогов»).

- Отправка файлов — только из ``MAX_UPLOAD_DIR``: путь разрешается через ``realpath`` и обязан
  остаться внутри каталога (``..`` и симлинки наружу отклоняются); файл открывается с
  ``O_NOFOLLOW``, проверяется уже открытый дескриптор (обычный файл, размер, на Linux — реальный
  путь через ``/proc/self/fd``), и байты читаются из этого же дескриптора. Так подмена файла
  симлинком между проверкой и чтением ничего не даёт. В PyMax уходят байты, а не путь.
- Скачивание — только в ``MAX_DOWNLOAD_DIR`` (0700), имя файла санитизируется.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from max_mcp_server.errors import Forbidden, InvalidArgument, NotFound
from max_mcp_server.security.untrusted import normalize_text

_PROC_FD = Path("/proc/self/fd")


def _inside(child: str, base: str) -> bool:
    try:
        return os.path.commonpath([child, base]) == base
    except ValueError:
        return False


@dataclass(frozen=True)
class UploadFile:
    name: str
    data: bytes
    path: Path  # реальный путь (для аудита/ответа — относительный к каталогу)
    rel: str


def read_upload(raw_path: str, upload_dir: Path | None, *, max_bytes: int) -> UploadFile:
    """Проверяет путь и читает файл из ``MAX_UPLOAD_DIR``. Ошибки — понятные для модели."""
    if upload_dir is None:
        raise Forbidden("Отправка файлов выключена: задайте MAX_UPLOAD_DIR (каталог для отправки)")
    if not raw_path or "\x00" in raw_path:
        raise InvalidArgument("Пустой или некорректный путь")
    if "://" in raw_path:
        raise InvalidArgument("URL не принимаются — только путь к файлу внутри MAX_UPLOAD_DIR")
    base_real = os.path.realpath(upload_dir.expanduser())
    if not os.path.isdir(base_real):
        raise Forbidden("MAX_UPLOAD_DIR не существует или это не каталог")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(base_real) / candidate
    real = os.path.realpath(candidate)
    if not _inside(real, base_real) or real == base_real:
        raise Forbidden("Путь вне MAX_UPLOAD_DIR (в том числе через .. или символьную ссылку)")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)  # FIFO не должен повесить сервер на open()
    try:
        fd = os.open(real, flags)
    except FileNotFoundError as exc:
        raise NotFound("Файл не найден в MAX_UPLOAD_DIR") from exc
    except OSError as exc:
        raise Forbidden("Файл нельзя открыть (символьная ссылка или нет прав)") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Forbidden("Можно отправлять только обычные файлы")
        if st.st_size > max_bytes:
            raise InvalidArgument(f"Файл больше лимита {max_bytes} байт (MAX_UPLOAD_MAX_BYTES)")
        if _PROC_FD.is_dir():
            try:
                opened = os.readlink(_PROC_FD / str(fd))
            except OSError:
                opened = real
            if not _inside(opened, base_real):
                raise Forbidden("Путь вне MAX_UPLOAD_DIR (подмена во время открытия)")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise InvalidArgument(f"Файл больше лимита {max_bytes} байт (MAX_UPLOAD_MAX_BYTES)")
            chunks.append(chunk)
    finally:
        os.close(fd)
    if total == 0:
        raise InvalidArgument("Файл пустой")
    rp = Path(real)
    return UploadFile(
        name=safe_filename(rp.name, fallback="file"),
        data=b"".join(chunks),
        path=rp,
        rel=os.path.relpath(real, base_real),
    )


def safe_filename(name: str, *, fallback: str = "file", limit: int = 80) -> str:
    """Имя без разделителей пути, управляющих символов и ведущих точек."""
    cleaned = "".join(
        ch if ch.isalnum() or ch in "._-" else "_" for ch in normalize_text(name or "")
    )[:limit].lstrip(".")
    return cleaned or fallback


def download_target(download_dir: Path, name: str) -> Path:
    """Путь для скачанного файла строго внутри ``MAX_DOWNLOAD_DIR``."""
    base = download_dir.expanduser()
    target = base / safe_filename(name, limit=160)
    if target.parent != base:
        raise Forbidden("Некорректное имя файла для скачивания")
    return target
