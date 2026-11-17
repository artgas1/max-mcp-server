"""EventBuffer: кольцевой буфер событий с монотонным seq-курсором и ожиданием."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from max_mcp_server.adapter.base import EventDTO, MessageDTO

ChatFilter = Callable[[int | None], bool]


@dataclass(frozen=True)
class EventBatch:
    events: list[EventDTO]
    cursor: int  # seq последнего выданного (или текущего хвоста)
    gap: bool  # часть событий после курсора уже вытеснена из буфера


class EventBuffer:
    def __init__(self, capacity: int = 1000, *, accept: ChatFilter | None = None) -> None:
        self._buf: deque[EventDTO] = deque(maxlen=capacity)
        self._seq = 0
        self._cond = asyncio.Condition()
        self._accept = accept or (lambda _chat_id: True)
        self.dropped = 0

    @property
    def last_seq(self) -> int:
        return self._seq

    def set_filter(self, accept: ChatFilter) -> None:
        self._accept = accept

    async def push(
        self,
        type_: str,
        chat_id: int | None,
        *,
        message: MessageDTO | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> EventDTO | None:
        """Кладёт событие; события чатов вне read-allowlist отбрасываются на входе."""
        if not self._accept(chat_id):
            self.dropped += 1
            return None
        async with self._cond:
            self._seq += 1
            ev = EventDTO(
                seq=self._seq,
                type=type_,
                chat_id=chat_id,
                time_ms=int(time.time() * 1000),
                message=message,
                data=dict(data or {}),
            )
            self._buf.append(ev)
            self._cond.notify_all()
        return ev

    def _collect(
        self, after: int, types: Sequence[str] | None, chat_id: int | None, limit: int
    ) -> tuple[list[EventDTO], int, bool]:
        oldest = self._buf[0].seq if self._buf else self._seq + 1
        gap = after + 1 < oldest and after < self._seq
        out: list[EventDTO] = []
        cursor = max(after, self._seq)  # по умолчанию всё просмотрено
        for ev in self._buf:
            if ev.seq <= after:
                continue
            if types and ev.type not in types:
                continue
            if chat_id is not None and ev.chat_id != chat_id:
                continue
            out.append(ev)
            if len(out) >= limit:
                cursor = ev.seq
                break
        return out, cursor, gap

    async def wait(
        self,
        after: int | None,
        *,
        timeout: float,
        types: Sequence[str] | None = None,
        chat_id: int | None = None,
        limit: int = 50,
    ) -> EventBatch:
        """События с seq > after. ``after=None`` — только новые (с текущего хвоста)."""
        start = self._seq if after is None else max(0, after)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        async with self._cond:
            while True:
                events, cursor, gap = self._collect(start, types, chat_id, limit)
                if events or gap:
                    return EventBatch(events, cursor, gap)
                # отфильтрованные события двигают курсор, чтобы не сканировать их снова
                start = cursor
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return EventBatch([], cursor, False)
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=remaining)
                except TimeoutError:
                    events, cursor, gap = self._collect(start, types, chat_id, limit)
                    return EventBatch(events, cursor, gap)
