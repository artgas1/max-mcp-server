"""Регрессия security-review M3: трейсбеки и сторонние логгеры не выводят тексты и секреты."""

from __future__ import annotations

import io
import logging

import pytest

from max_mcp_server.logging import SafeFormatter, pin_pymax_level, redact, setup_logging

# --------------------------------------------------------------------------- M3


SECRET = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefgh"


def _capture(level: str = "INFO", *, pymax_debug: bool = False) -> io.StringIO:
    setup_logging(level, pymax_debug=pymax_debug)
    buf = io.StringIO()
    root = logging.getLogger()
    handler = next(h for h in root.handlers if getattr(h, "_max_mcp_handler", False))
    handler.setStream(buf)  # type: ignore[attr-defined]
    return buf


def _raise_with_payload() -> None:
    try:
        raise KeyError("inner chat text: встречаемся у входа")
    except KeyError as inner:
        raise ValueError(f'token="{SECRET}" phone +70001234567 text=секрет 70000001122') from inner


def test_m3_traceback_text_hidden(monkeypatch):
    """PoC poc_log.py: трейсбек pymax с токеном/телефоном/текстом в сообщении исключения."""
    buf = _capture("INFO")
    try:
        _raise_with_payload()
    except ValueError:
        logging.getLogger("pymax.app").exception("failed to dispatch inbound frame")
        logging.getLogger("max_mcp_server.tools").exception("tool=x unexpected error")
    out = buf.getvalue()
    assert "failed to dispatch inbound frame" in out and "builtins.ValueError" in out
    assert "builtins.KeyError" in out and "_raise_with_payload" in out
    for leak in (SECRET, "9991234567", "секрет", "встречаемся", "raise ValueError", "70000001122"):
        assert leak not in out


def test_m3_verbose_tracebacks_still_redacted():
    buf = _capture("INFO", pymax_debug=True)
    try:
        _raise_with_payload()
    except ValueError:
        logging.getLogger("pymax.app").exception("boom")
    out = buf.getvalue()
    assert "Traceback" in out and SECRET not in out and "+70001234567" not in out
    assert "70000001122" not in out
    pin_pymax_level("INFO")


def test_m3_prefilled_exc_text_and_stack_not_leaked():
    f = SafeFormatter("%(message)s")
    rec = logging.LogRecord("pymax", logging.ERROR, "f", 1, "m", None, None)
    rec.exc_text = f"Traceback\nValueError: token={SECRET}"
    rec.stack_info = f"Stack\n  token={SECRET}"
    out = f.format(rec)
    assert SECRET not in out and "<traceback hidden>" in out and "Stack (hidden" in out
    verbose = SafeFormatter("%(message)s", verbose_tracebacks=True).format(rec)
    assert SECRET not in verbose and "Traceback" in verbose


@pytest.mark.parametrize("name", ["pymax", "websockets", "aiohttp", "httpx", "httpcore"])
def test_m3_third_party_loggers_pinned_even_at_debug(name):
    buf = _capture("DEBUG")
    assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
    logging.getLogger(f"{name}.client").debug("> TEXT frame %s", "hello from chat")
    logging.getLogger(f"{name}.client").info("< frame")
    assert buf.getvalue() == ""
    pin_pymax_level("INFO")


def test_m3_redacts_bare_russian_phones_not_ids():
    assert redact("phone 70001234567 / 89991234567") == "phone <phone> / <phone>"
    assert redact("chat -71234567890 user 7123456789 t 1795413693000") == (
        "chat -71234567890 user 7123456789 t 1795413693000"
    )
