"""Непрозрачные курсоры: base64url(JSON) + HMAC (ключ случайный на процесс)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any

from max_mcp_server.errors import InvalidArgument

_KEY = secrets.token_bytes(32)
_MAC_LEN = 12


def encode_cursor(kind: str, data: dict[str, Any]) -> str:
    body = json.dumps({"v": 1, "k": kind, **data}, separators=(",", ":"), sort_keys=True).encode()
    mac = hmac.new(_KEY, body, hashlib.sha256).digest()[:_MAC_LEN]
    return base64.urlsafe_b64encode(mac + body).decode().rstrip("=")


def decode_cursor(kind: str, cursor: str) -> dict[str, Any]:
    bad = InvalidArgument(
        "Неверный или устаревший cursor (курсоры действуют до перезапуска сервера). "
        "Повторите запрос без cursor."
    )
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    except (ValueError, TypeError) as exc:
        raise bad from exc
    mac, body = raw[:_MAC_LEN], raw[_MAC_LEN:]
    expected = hmac.new(_KEY, body, hashlib.sha256).digest()[:_MAC_LEN]
    if not hmac.compare_digest(mac, expected):
        raise bad
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise bad from exc
    if not isinstance(data, dict) or data.get("v") != 1 or data.get("k") != kind:
        raise bad
    return data
