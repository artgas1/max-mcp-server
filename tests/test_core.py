"""config, logging, cursor, events, policy, ratelimit, даты."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC

import pytest

from max_mcp_server.adapter.events import EventBuffer
from max_mcp_server.config import Settings, load_settings, parse_id_list
from max_mcp_server.errors import ConfigError, Forbidden, InvalidArgument
from max_mcp_server.logging import RedactingFilter, pin_pymax_level, pymax_level, redact
from max_mcp_server.security.policy import Policy
from max_mcp_server.security.ratelimit import TokenBucket
from max_mcp_server.tools.common import parse_range, parse_when
from max_mcp_server.tools.cursor import decode_cursor, encode_cursor


def test_settings_defaults(monkeypatch, tmp_path):
    s = load_settings()
    assert s.read_only is True and s.client == "web" and s.appear_online is False
    assert s.write_allow == frozenset() and s.read_allow == frozenset()
    assert s.state_path == tmp_path / "state" / "max-mcp-server" / "default"
    assert s.http_host == "127.0.0.1"


def test_settings_env(monkeypatch):
    monkeypatch.setenv("MAX_READ_CHATS", "1, -2,3")
    monkeypatch.setenv("MAX_WRITE_CHATS", "*")
    monkeypatch.setenv("MAX_2FA_PASSWORD_FILE", "/tmp/x")
    monkeypatch.setenv("QR_SVG", "/tmp/q.svg")
    monkeypatch.setenv("MAX_READ_ONLY", "false")
    s = load_settings()
    assert s.read_allow == {1, -2, 3} and s.write_allow == "*" and s.read_only is False
    assert str(s.twofa_password_file) == "/tmp/x" and str(s.qr_svg) == "/tmp/q.svg"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("MAX_PROFILE", "../x"),
        ("MAX_TZ", "Mars/Base"),
        ("MAX_READ_CHATS", "abc"),
        ("MAX_CLIENT", "tcp"),
    ],
)
def test_settings_errors(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError):
        load_settings()


def test_parse_id_list():
    assert parse_id_list(" ; 5,") == {5}


def test_redaction():
    line = 'login token=abc.DEF-123 "token": "zzz" Authorization: Bearer qwe.rty phone +7 (999) 123-45-67 password=hunter2'
    out = redact(line)
    for secret in ("abc.DEF-123", "zzz", "qwe.rty", "123-45-67", "hunter2"):
        assert secret not in out
    assert redact("chat -71234567890 time 1795413693000") == "chat -71234567890 time 1795413693000"
    assert "<redacted>" in redact("x" * 50)


def test_redacting_filter_applies_args():
    rec = logging.LogRecord("t", logging.INFO, "f", 1, "token=%s", ("SECRETVALUE",), None)
    RedactingFilter().filter(rec)
    assert "SECRETVALUE" not in rec.getMessage()


def test_pymax_level_pinned():
    assert pymax_level("DEBUG", pymax_debug=False) == "WARNING"
    assert pymax_level("ERROR", pymax_debug=False) == "ERROR"
    assert pymax_level("DEBUG", pymax_debug=True) == "DEBUG"
    pin_pymax_level("DEBUG")
    assert logging.getLogger("pymax").level == logging.WARNING


def test_cursor_roundtrip_and_tamper():
    c = encode_cursor("hist", {"c": 1})
    assert decode_cursor("hist", c)["c"] == 1
    with pytest.raises(InvalidArgument):
        decode_cursor("chats", c)
    with pytest.raises(InvalidArgument):
        decode_cursor("hist", c[:-2] + ("AA" if not c.endswith("AA") else "BB"))
    with pytest.raises(InvalidArgument):
        decode_cursor("hist", "!!!")


def test_dates():
    assert parse_when("2026-01-01", UTC) == 1_767_225_600_000
    assert parse_when("2026-01-01", UTC, end_of_day=True) == 1_767_225_600_000 + 86_400_000 - 1
    assert parse_when("2026-01-01T03:00:00+03:00", UTC) == 1_767_225_600_000
    with pytest.raises(InvalidArgument):
        parse_range("2026-02-01", "2026-01-01", UTC, 0)


def test_policy():
    p = Policy(read_allow=frozenset({1, 2}), deny=frozenset({2}), write_allow=frozenset({1}))
    assert p.can_read(1) and not p.can_read(2) and not p.can_read(3) and not p.can_read(None)
    assert not p.can_write(1)  # read_only
    assert Policy(read_only=False, write_allow="*").can_write(9)
    assert not Policy(read_only=False).can_write(9)
    with pytest.raises(Forbidden):
        p.check_read(3)
    assert p.summary()["read_allowlist"] == 2


async def test_token_bucket_fake_clock():
    now = [0.0]
    b = TokenBucket(2, burst=2, clock=lambda: now[0])
    assert b.try_acquire() and b.try_acquire() and not b.try_acquire()
    now[0] += 0.5
    assert b.try_acquire() and not b.try_acquire()


async def test_event_buffer_cursor_timeout_filter_gap():
    buf = EventBuffer(capacity=5, accept=lambda cid: cid != 666)
    empty = await buf.wait(None, timeout=0)
    assert empty.events == [] and empty.cursor == 0
    assert await buf.push("message", 666) is None and buf.dropped == 1
    for i in range(3):
        await buf.push("message", i)
    batch = await buf.wait(0, timeout=0, limit=2)
    assert [e.chat_id for e in batch.events] == [0, 1] and batch.cursor == 2
    rest = await buf.wait(batch.cursor, timeout=0)
    assert [e.chat_id for e in rest.events] == [2]
    only = await buf.wait(0, timeout=0, types=["edit"])
    assert only.events == [] and only.cursor == 3

    async def later():
        await asyncio.sleep(0.02)
        await buf.push("edit", 9)

    t = asyncio.create_task(later())
    got = await buf.wait(3, timeout=2, types=["edit"])
    await t
    assert [e.type for e in got.events] == ["edit"]
    for i in range(10):
        await buf.push("message", i)
    overflow = await buf.wait(1, timeout=0)
    assert overflow.gap is True and len(overflow.events) == 5


def test_settings_is_frozen_copyable():
    s = Settings(tz="UTC")
    assert s.model_copy(update={"read_only": False}).read_only is False
