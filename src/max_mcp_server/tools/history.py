"""Обход истории поверх ``adapter.history`` (точка + backward/forward).

Порядок сообщений — по ключу (time_ms, id). Курсор хранит исключающую границу ключа, поэтому
страницы не дублируются и не теряют сообщения с одинаковым временем. Сервер может вернуть
пересекающиеся страницы — дубли отбрасываются по ключу.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from max_mcp_server.adapter.base import MaxAdapter, MessageDTO

PAGE = 100
MAX_KEY = 2**63
Direction = Literal["back", "fwd"]
Key = tuple[int, int]


@dataclass
class WalkResult:
    items: list[MessageDTO] = field(default_factory=list)
    last_key: Key | None = None  # граница для продолжения
    exhausted: bool = False  # дошли до конца диапазона/истории
    scanned: int = 0


def start_key(direction: Direction, from_ms: int | None, to_ms: int) -> Key:
    if direction == "back":
        return (to_ms, MAX_KEY)
    return (from_ms or 0, -1)


async def walk(
    adapter: MaxAdapter,
    chat_id: int,
    *,
    direction: Direction,
    after: Key,
    from_ms: int | None,
    to_ms: int,
    want: int,
    max_scan: int | None = None,
    predicate: Callable[[MessageDTO], bool] | None = None,
    page: int = PAGE,
) -> WalkResult:
    """Идёт от границы ``after`` (исключительно) в направлении ``direction``.

    Останавливается, когда набрано ``want`` подходящих, просмотрено ``max_scan``, вышли за
    диапазон [from_ms, to_ms] или история кончилась.
    """
    res = WalkResult(last_key=after)
    cur = after
    anchor = after[0]
    while True:
        if direction == "back":
            batch = await adapter.history(
                chat_id, anchor_ms=max(anchor, 1), backward=page, forward=0
            )
            fresh = sorted((m for m in batch if m.key < cur), key=lambda m: m.key, reverse=True)
        else:
            batch = await adapter.history(
                chat_id, anchor_ms=max(anchor, 1), backward=0, forward=page
            )
            fresh = sorted((m for m in batch if m.key > cur), key=lambda m: m.key)
        if not fresh:
            res.exhausted = True
            break
        for m in fresh:
            if direction == "back":
                if from_ms is not None and m.time_ms < from_ms:
                    res.exhausted = True
                    break
                if m.time_ms > to_ms:
                    cur = m.key
                    continue
            else:
                if m.time_ms > to_ms:
                    res.exhausted = True
                    break
                if from_ms is not None and m.time_ms < from_ms:
                    cur = m.key
                    continue
            cur = m.key
            res.last_key = cur
            res.scanned += 1
            if predicate is None or predicate(m):
                res.items.append(m)
                if len(res.items) >= want:
                    return res
            if max_scan is not None and res.scanned >= max_scan:
                return res
        if res.exhausted:
            break
        new_anchor = fresh[-1].time_ms
        # защита от зацикливания, если в одну миллисекунду больше ``page`` сообщений
        if new_anchor == anchor and len(fresh) < page // 2:
            res.exhausted = True
            break
        anchor = new_anchor
    res.last_key = cur
    return res


def fold(text: str) -> str:
    """Нормализация для поиска: NFKC, casefold, ё→е."""
    return unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")


def text_matcher(query: str, sender_id: int | None = None) -> Callable[[MessageDTO], bool]:
    """Все слова запроса должны встретиться в тексте (или пересланном тексте/имени файла)."""
    words = [w for w in fold(query).split() if w]

    def match(m: MessageDTO) -> bool:
        if sender_id is not None and m.sender_id != sender_id:
            return False
        hay = [m.text]
        if m.forwarded_from is not None:
            hay.append(m.forwarded_from.text)
        hay.extend(a.name or "" for a in m.attachments)
        hay.extend(a.transcript or "" for a in m.attachments)
        blob = fold(" ".join(hay))
        return all(w in blob for w in words)

    return match
