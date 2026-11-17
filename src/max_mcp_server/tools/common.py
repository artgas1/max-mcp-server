"""Общее для инструментов: tool_guard (политика, лимиты, ошибки, аудит), разбор дат, зависимости."""

from __future__ import annotations

import functools
import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, ParamSpec, TypeVar

from mcp.server.mcpserver.exceptions import ToolError

from max_mcp_server.adapter.base import MaxAdapter, MessageDTO
from max_mcp_server.config import Settings
from max_mcp_server.errors import (
    Forbidden,
    InvalidArgument,
    MaxMcpError,
    NotFound,
    NotLoggedIn,
    SessionExpired,
)
from max_mcp_server.security.policy import Policy
from max_mcp_server.security.ratelimit import SendLimiter
from max_mcp_server.serialization import Serializer

log = logging.getLogger("max_mcp_server.tools")
P = ParamSpec("P")
R = TypeVar("R")


AUDIT_ARGS = (
    "chat_id",
    "from_chat_id",
    "to_chat_id",
    "post_id",
    "message_id",
    "message_ids",
    "user_ids",
    "answer_ids",
    "reply_to",
)
audit_log = logging.getLogger("max_mcp_server.audit")


@dataclass(frozen=True)
class WriteSpec:
    """Что проверить до вызова адаптера (design.md, 8.2). Проверки идут ДО любого запроса к MAX."""

    write_chat: str | None = "chat_id"  # аргумент: чат должен быть в write-allowlist
    read_chat: str | None = None  # аргумент: чат-источник должен быть читаемым (forward)
    sends: bool = False  # занимает слот SendLimiter
    sends_per: str | None = None  # аргумент-список: по слоту на элемент (пересылка)
    join: bool = False  # нужен MAX_ALLOW_JOIN
    no_chat: bool = False  # действие без существующего чата (create_group)


def _error_text(exc: MaxMcpError) -> str:
    text = f"[{exc.code}] {exc.public_message}"
    retry_after = getattr(exc, "retry_after", None)
    if retry_after:
        text += f" (retry_after={int(retry_after)} с)"
    return text


def _audit(name: str, bound: dict[str, Any], ok: bool, err: str | None, *, content: bool) -> None:
    parts = [f"audit tool={name}"]
    for key in AUDIT_ARGS:
        val = bound.get(key)
        if val is not None:
            parts.append(f"{key}={val}")
    text = bound.get("text") if "text" in bound else bound.get("question")
    if isinstance(text, str):
        parts.append(f"text_len={len(text)}")
        if content:
            parts.append(f"text={text[:200]!r}")
    parts.append(f"ok={'true' if ok else 'false'}")
    if err:
        parts.append(f"error={err}")
    audit_log.info(" ".join(parts))


def tool_guard(
    name: str,
    *,
    deps: ToolDeps | None = None,
    spec: WriteSpec | None = None,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Обёртка инструмента.

    - ``spec`` (инструменты записи): проверка политики и SendLimiter до вызова адаптера,
      аудит-строка в stderr (без текстов, если не ``MAX_LOG_CONTENT``);
    - наши исключения → ``ToolError`` с кодом и понятным текстом (``retry_after`` для флуда,
      «войдите заново» для истёкшей сессии); прочие → общий текст без внутренностей.
    """

    def deco(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            bound: dict[str, Any] = {}
            ok, err, reserved = False, None, 0
            try:
                if spec is not None:
                    assert deps is not None
                    ba = sig.bind(*args, **kwargs)
                    ba.apply_defaults()
                    bound = dict(ba.arguments)
                    reserved = _check_spec(deps, spec, bound)
                result = await fn(*args, **kwargs)
                ok = True
                return result
            except ToolError:
                err = "tool_error"
                raise
            except MaxMcpError as exc:
                err = exc.code
                if reserved and isinstance(exc, _REFUNDABLE):
                    assert deps is not None
                    deps.send_limiter.release(reserved)
                log.info("tool=%s error=%s", name, exc.code)
                raise ToolError(_error_text(exc)) from exc
            except Exception as exc:
                err = "internal_error"
                log.exception("tool=%s unexpected error", name)
                raise ToolError(
                    "[internal_error] Внутренняя ошибка сервера max-mcp; подробности в его логе (stderr)."
                ) from exc
            finally:
                if spec is not None and deps is not None:
                    _audit(name, bound, ok, err, content=deps.settings.log_content)

        return wrapper

    return deco


# отказ до/без отправки: слот лимита возвращается (таймаут/сетевая ошибка — нет: могло уйти)
_REFUNDABLE = (Forbidden, InvalidArgument, NotFound, NotLoggedIn, SessionExpired)


def _check_spec(deps: ToolDeps, spec: WriteSpec, bound: dict[str, Any]) -> int:
    """Проверки политики и лимита; возвращает число занятых слотов SendLimiter."""
    policy = deps.policy
    if spec.join:
        policy.check_join()
    elif spec.no_chat:
        policy.check_any_write()
    if spec.read_chat is not None:
        policy.check_read(int(bound[spec.read_chat]))
    if spec.write_chat is not None:
        policy.check_write(int(bound[spec.write_chat]))
    if not spec.sends:
        return 0
    n = max(1, len(bound[spec.sends_per] or ()) if spec.sends_per else 1)
    deps.send_limiter.check(n)
    return n


@dataclass
class ToolDeps:
    adapter: MaxAdapter
    policy: Policy
    settings: Settings
    send_limiter: SendLimiter = field(default=None)  # type: ignore[assignment]
    _me_id: int | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.send_limiter is None:
            self.send_limiter = SendLimiter.from_spec(self.settings.send_limit)

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
