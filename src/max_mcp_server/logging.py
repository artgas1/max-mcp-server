"""Логи только в stderr; редактирование секретов; принудительный уровень сторонних логгеров.

Трейсбеки по умолчанию выводятся без текста исключения и без исходных строк кадров: только
файл, строка, функция и тип исключения (M3, security-review). Текст исключения (pydantic
ValidationError с ``input_value``, тексты чужих сообщений, телефоны, URL CDN) печатается только
с ``MAX_LOG_PYMAX_DEBUG=1``, и тогда тоже проходит через ``redact``.
"""

from __future__ import annotations

import logging
import re
import sys
import traceback
from types import TracebackType

PYMAX_LOGGER = "pymax"
# логгеры, которые на DEBUG/INFO пишут кадры протокола, заголовки или тела запросов
THIRD_PARTY_LOGGERS = ("pymax", "websockets", "aiohttp", "httpx", "httpcore", "hpack")
_HANDLER_FLAG = "_max_mcp_handler"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # token=..., "token": "...", Authorization: Bearer ...
    (re.compile(r"(?i)(\"?(?:login_?)?token\"?\s*[:=]\s*\"?)[^\s\",'}]+"), r"\1***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1***"),
    (re.compile(r"(?i)(password\"?\s*[:=]\s*\"?)[^\s\",'}]+"), r"\1***"),
    # телефоны в международном формате (+7 999 …); голые числа не трогаем — это id и время
    (re.compile(r"\+\d[\d\s()-]{8,16}\d(?!\d)"), "<phone>"),
    # российские номера без «+» (так их отдаёт MAX: 70001234567); id чатов отрицательные и
    # с минусом, время — 13 цифр, поэтому ровно 11 цифр с 7/8 в начале — почти всегда телефон
    (re.compile(r"(?<![\w+-])[78]\d{10}(?![\w])"), "<phone>"),
    # длинные base64/hex-подобные строки (токены сессий)
    (re.compile(r"[A-Za-z0-9_\-]{40,}"), "<redacted>"),
)


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    """Переписывает сообщение записи, вырезая токены, пароли и телефоны.

    Трейсбек здесь ещё не отформатирован (``exc_text`` заполняет Formatter позже), поэтому
    его обезвреживает ``SafeFormatter``; фильтр лишь сбрасывает чужой кэш ``exc_text``."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def _exc_chain(exc: BaseException | None) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen and len(chain) < 8:
        seen.add(id(exc))
        chain.append(exc)
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return chain


def safe_traceback(
    exc_info: tuple[type[BaseException], BaseException, TracebackType | None] | tuple[None, ...],
) -> str:
    """Трейсбек без текста исключения и без исходных строк: ``File:line in func`` + тип."""
    _, exc, _ = exc_info
    if exc is None:
        return ""
    blocks: list[str] = []
    for i, e in enumerate(reversed(_exc_chain(exc))):
        lines = ["Traceback (most recent call last; details hidden, MAX_LOG_PYMAX_DEBUG=1):"]
        for fr in traceback.extract_tb(e.__traceback__)[-20:]:
            lines.append(f'  File "{fr.filename}", line {fr.lineno}, in {fr.name}')
        lines.append(f"{type(e).__module__}.{type(e).__qualname__}")
        if i:
            blocks.append("The above exception led to:")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


class SafeFormatter(logging.Formatter):
    """Formatter, который редактирует **весь** результат (сообщение, трейсбек, стек) и по
    умолчанию не выводит текст исключений и исходные строки кадров."""

    def __init__(self, fmt: str | None = None, *, verbose_tracebacks: bool = False) -> None:
        super().__init__(fmt)
        self.verbose_tracebacks = verbose_tracebacks

    def formatException(self, ei):  # type: ignore[no-untyped-def]
        if self.verbose_tracebacks:
            return redact(super().formatException(ei))
        return safe_traceback(ei)

    def formatStack(self, stack_info: str) -> str:
        if self.verbose_tracebacks:
            return redact(stack_info)
        return "Stack (hidden; MAX_LOG_PYMAX_DEBUG=1)"

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        if self.usesTime():
            record.asctime = self.formatTime(record, self.datefmt)
        out = self.formatMessage(record)
        # кэш record.exc_text не используем: его мог заполнить чужой Formatter без редактирования
        if record.exc_info:
            tb = self.formatException(record.exc_info)
            if tb:
                out += "\n" + tb
        elif record.exc_text:
            out += "\n" + (
                redact(record.exc_text) if self.verbose_tracebacks else "<traceback hidden>"
            )
        if record.stack_info:
            out += "\n" + self.formatStack(record.stack_info)
        return redact(out)


def setup_logging(level: str = "INFO", *, pymax_debug: bool = False) -> None:
    """Настраивает корневой логгер: stderr, редактор секретов. Идемпотентно."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_FLAG, False):
            root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    setattr(handler, _HANDLER_FLAG, True)
    handler.setFormatter(
        SafeFormatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s", verbose_tracebacks=pymax_debug
        )
    )
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    root.setLevel(level)
    pin_pymax_level(level, pymax_debug=pymax_debug)


def pymax_level(level: str, *, pymax_debug: bool) -> str:
    """pymax на DEBUG пишет полные кадры (токен, тексты) — только с явным флагом."""
    if pymax_debug:
        return level
    order = ["DEBUG", "INFO", "WARNING", "ERROR"]
    return order[max(order.index(level) if level in order else 2, 2)]


def pin_pymax_level(level: str, *, pymax_debug: bool = False) -> None:
    """pymax и транспортные библиотеки (websockets, aiohttp, httpx…) — не ниже WARNING, даже
    при ``MAX_LOG_LEVEL=DEBUG``: на DEBUG они пишут кадры протокола с токеном и текстами."""
    pinned = pymax_level(level, pymax_debug=pymax_debug)
    for name in THIRD_PARTY_LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(pinned)
        # свои обработчики убираем — пусть всё идёт через наш (с редактором) в stderr
        lg.handlers.clear()
        lg.propagate = True
    if pymax_debug:
        logging.getLogger(__name__).warning(
            "MAX_LOG_PYMAX_DEBUG=1: pymax пишет полные кадры протокола; не публикуйте этот лог"
        )
