"""Ограничение частоты: TokenBucket на все запросы к MAX и SendLimiter на отправки.

``MAX_RPS`` — общий token bucket (все вызовы адаптера, включая скачивание).
``MAX_SEND_LIMIT`` — скользящие окна для «исходящих» действий (отправка, файл, пересылка,
комментарий, опрос, создание группы): ``"20/h,5/min"`` = не больше 5 за минуту и 20 за час.
Превышение → ``RateLimited`` с ``retry_after`` (секунды до освобождения слота), без ожидания:
модель получает понятный отказ, а не зависший вызов.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections import deque
from collections.abc import Callable

from max_mcp_server.errors import ConfigError, RateLimited


class TokenBucket:
    def __init__(
        self,
        rate: float,
        burst: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate = rate
        self.capacity = burst if burst is not None else max(1.0, rate)
        self._tokens = self.capacity
        self._clock = clock
        self._last = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def try_acquire(self, n: float = 1.0) -> bool:
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False

    async def acquire(self, n: float = 1.0) -> None:
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= n:
                    self._tokens -= n
                    return
                await asyncio.sleep((n - self._tokens) / self.rate)


_UNITS = {
    "s": 1,
    "sec": 1,
    "m": 60,
    "min": 60,
    "h": 3600,
    "hour": 3600,
    "d": 86400,
    "day": 86400,
}
_LIMIT_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d*)\s*([a-z]+)\s*$", re.I)


def parse_send_limit(raw: str) -> tuple[tuple[int, float], ...]:
    """``"20/h,5/min"`` → ((20, 3600), (5, 60)). ``"0"``/``"off"`` — без лимита (не советуем)."""
    raw = raw.strip()
    if raw.lower() in ("", "0", "off", "none"):
        return ()
    out: list[tuple[int, float]] = []
    for part in raw.split(","):
        if not part.strip():
            continue
        m = _LIMIT_RE.match(part)
        if not m or m.group(3).lower() not in _UNITS:
            raise ConfigError(
                f"MAX_SEND_LIMIT: не понял {part.strip()!r}; формат '20/h,5/min' (s|min|h|d)"
            )
        count = int(m.group(1))
        mult = int(m.group(2) or 1)
        if count < 1 or mult < 1:
            raise ConfigError(f"MAX_SEND_LIMIT: {part.strip()!r} должно быть больше нуля")
        out.append((count, float(mult * _UNITS[m.group(3).lower()])))
    return tuple(out)


class SendLimiter:
    """Скользящие окна «N действий за период». Слот занимается при попытке (до вызова MAX)."""

    def __init__(
        self,
        limits: tuple[tuple[int, float], ...],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits
        self._clock = clock
        longest = max((w for _, w in limits), default=0.0)
        self._window = longest
        self._stamps: deque[float] = deque()

    @classmethod
    def from_spec(cls, spec: str, **kw: Callable[[], float]) -> SendLimiter:
        return cls(parse_send_limit(spec), **kw)

    def _prune(self, now: float) -> None:
        while self._stamps and now - self._stamps[0] >= self._window:
            self._stamps.popleft()

    def retry_after(self, n: int = 1) -> float:
        """Сколько секунд ждать, чтобы освободилось ``n`` слотов (0 — можно сейчас)."""
        now = self._clock()
        self._prune(now)
        wait = 0.0
        for count, window in self.limits:
            if n > count:
                return float("inf")
            recent = [t for t in self._stamps if now - t < window]
            excess = len(recent) + n - count
            if excess > 0:
                # освободится, когда «выпадут» excess самых старых отметок окна
                wait = max(wait, recent[excess - 1] + window - now)
        return wait

    def check(self, n: int = 1) -> None:
        """Занять ``n`` слотов или бросить ``RateLimited(retry_after)``."""
        wait = self.retry_after(n)
        if wait == float("inf"):
            raise RateLimited(
                f"За один вызов нельзя больше, чем позволяет MAX_SEND_LIMIT ({self.describe()})"
            )
        if wait > 0:
            secs = max(1, math.ceil(wait))
            raise RateLimited(
                f"Превышен лимит отправок MAX_SEND_LIMIT ({self.describe()}). "
                f"Повторите не раньше чем через {secs} с.",
                retry_after=secs,
            )
        now = self._clock()
        self._stamps.extend([now] * n)

    def release(self, n: int = 1) -> None:
        """Вернуть слоты, если действие отклонено до отправки (неверный аргумент, политика)."""
        for _ in range(min(n, len(self._stamps))):
            self._stamps.pop()

    def describe(self) -> str:
        if not self.limits:
            return "без лимита"
        return ", ".join(f"{c} за {int(w)} с" for c, w in self.limits)
