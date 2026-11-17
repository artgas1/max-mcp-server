"""Общее для инструментов: перевод ошибок в ToolError, разбор дат, зависимости."""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, ParamSpec, TypeVar

from mcp.server.mcpserver.exceptions import ToolError

from max_mcp_server.adapter.base import MaxAdapter, MessageDTO
from max_mcp_server.config import Settings
from max_mcp_server.errors import InvalidArgument, MaxMcpError
from max_mcp_server.security.policy import Policy
from max_mcp_server.serialization import Serializer

log = logging.getLogger("max_mcp_server.tools")
P = ParamSpec("P")
R = TypeVar("R")


def tool_guard(name: str) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Наши исключения → понятный ToolError; прочие → общий текст без внутренностей."""

    def deco(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return await fn(*args, **kwargs)
            except ToolError:
                raise
            except MaxMcpError as exc:
                log.info("tool=%s error=%s", name, exc.code)
                raise ToolError(f"[{exc.code}] {exc.public_message}") from exc
            except Exception as exc:
                log.exception("tool=%s unexpected error", name)
                raise ToolError(
                    "[internal_error] Внутренняя ошибка сервера max-mcp; подробности в его логе (stderr)."
                ) from exc

        return wrapper

    return deco


@dataclass
class ToolDeps:
    adapter: MaxAdapter
    policy: Policy
    settings: Settings
    _me_id: int | None = field(default=None, init=False)

    @property
    def tz(self) -> tzinfo:
        return self.settings.timezone

    async def me_id(self) -> int | None:
        if self._me_id is None:
            try:
                self._me_id = (await self.adapter.me()).id
            except MaxMcpError:
                return None
        return self._me_id

    async def serializer(self, messages: Iterable[MessageDTO] = ()) -> Serializer:
        """Сериализатор с именами отправителей (одним запросом на страницу)."""
        ids: set[int] = set()
        for m in messages:
            if m.sender_id is not None:
                ids.add(m.sender_id)
            if m.forwarded_from is not None and m.forwarded_from.sender_id is not None:
                ids.add(m.forwarded_from.sender_id)
        names: dict[int, str] = {}
        if ids:
            try:
                names = {u.id: u.name for u in await self.adapter.users(sorted(ids))}
            except MaxMcpError:
                log.debug("names lookup failed", exc_info=True)
        return Serializer(
            wrap_mode=self.settings.untrusted_wrap,
            text_limit=self.settings.text_limit,
            tz=self.tz,
            me_id=await self.me_id(),
            names=names,
        )


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_when(value: str, tz: tzinfo, *, end_of_day: bool = False) -> int:
    """ISO 8601 или YYYY-MM-DD (в MAX_TZ) → мс. Для даты без времени ``end_of_day`` даёт 23:59:59.999."""
    raw = value.strip()
    try:
        if _DATE_ONLY.match(raw):
            d = date.fromisoformat(raw)
            if end_of_day:
                dt = datetime.combine(d + timedelta(days=1), time(0), tzinfo=tz)
                return int(dt.timestamp() * 1000) - 1
            return int(datetime.combine(d, time(0), tzinfo=tz).timestamp() * 1000)
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidArgument(
            f"Неверная дата {value!r}: ожидается YYYY-MM-DD или ISO 8601 (2026-11-17T10:00)"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp() * 1000)


def parse_range(
    date_from: str | None, date_to: str | None, tz: tzinfo, now_ms: int
) -> tuple[int | None, int]:
    lo = parse_when(date_from, tz) if date_from else None
    hi = parse_when(date_to, tz, end_of_day=True) if date_to else now_ms + 60_000
    if lo is not None and lo > hi:
        raise InvalidArgument("date_from позже date_to")
    return lo, hi


def clamp(value: int, lo: int, hi: int, name: str) -> int:
    if value < lo or value > hi:
        raise InvalidArgument(f"{name} должен быть в диапазоне {lo}…{hi}")
    return value


def unique_ints(values: Iterable[int], *, max_items: int, name: str) -> list[int]:
    out = list(dict.fromkeys(int(v) for v in values))
    if not out:
        raise InvalidArgument(f"{name}: нужен хотя бы один id")
    if len(out) > max_items:
        raise InvalidArgument(f"{name}: не больше {max_items} id за вызов")
    return out


def as_dict(**kwargs: Any) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if v is not None}
