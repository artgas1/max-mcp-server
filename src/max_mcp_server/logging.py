"""Логи только в stderr; редактирование секретов; принудительный уровень логгера pymax."""

from __future__ import annotations

import logging
import re
import sys

PYMAX_LOGGER = "pymax"
_HANDLER_FLAG = "_max_mcp_handler"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # token=..., "token": "...", Authorization: Bearer ...
    (re.compile(r"(?i)(\"?(?:login_?)?token\"?\s*[:=]\s*\"?)[^\s\",'}]+"), r"\1***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1***"),
    (re.compile(r"(?i)(password\"?\s*[:=]\s*\"?)[^\s\",'}]+"), r"\1***"),
    # телефоны в международном формате (+7 999 …); голые числа не трогаем — это id и время
    (re.compile(r"\+\d[\d\s()-]{8,16}\d(?!\d)"), "<phone>"),
    # длинные base64/hex-подобные строки (токены сессий)
    (re.compile(r"[A-Za-z0-9_\-]{40,}"), "<redacted>"),
)


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    """Переписывает сообщение записи, вырезая токены, пароли и телефоны."""

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


def setup_logging(level: str = "INFO", *, pymax_debug: bool = False) -> None:
    """Настраивает корневой логгер: stderr, редактор секретов. Идемпотентно."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_FLAG, False):
            root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    setattr(handler, _HANDLER_FLAG, True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
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
    lg = logging.getLogger(PYMAX_LOGGER)
    lg.setLevel(pymax_level(level, pymax_debug=pymax_debug))
    # свои обработчики pymax убираем — пусть всё идёт через наш (с редактором) в stderr
    lg.handlers.clear()
    lg.propagate = True
    if pymax_debug:
        logging.getLogger(__name__).warning(
            "MAX_LOG_PYMAX_DEBUG=1: pymax пишет полные кадры протокола; не публикуйте этот лог"
        )
