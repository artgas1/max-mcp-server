"""Инструменты чтения (design.md, 6.1). Не меняют состояние аккаунта.

Ни один инструмент здесь не вызывает ``adapter.mark_read`` и не шлёт ``CHAT_MARK``.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from datetime import datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image
from mcp_types import ToolAnnotations
from pydantic import Field

from max_mcp_server import __version__
from max_mcp_server.adapter.base import ChatDTO, MessageDTO
from max_mcp_server.errors import InvalidArgument, NotFound
from max_mcp_server.security.untrusted import UNTRUSTED_NOTICE, normalize_text
from max_mcp_server.serialization import make_ref, parse_ref
from max_mcp_server.tools.common import (
    ToolDeps,
    parse_range,
    parse_when,
    tool_guard,
    unique_ints,
)
from max_mcp_server.tools.cursor import decode_cursor, encode_cursor
from max_mcp_server.tools.history import MAX_KEY, fold, start_key, text_matcher, walk

READ_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)

ChatId = Annotated[int, Field(description="id чата MAX (у групп и каналов отрицательный)")]
DateArg = Annotated[
    str | None,
    Field(description="YYYY-MM-DD или ISO 8601; без зоны — в MAX_TZ"),
]
CursorArg = Annotated[
    str | None, Field(description="next_cursor из предыдущего ответа (продолжить)")
]

CHAT_LIST_PAGES = 10  # не больше 10 страниц fetch_chats за вызов при фильтрации
SEARCH_CHATS_DEFAULT = 20
TIMELINE_SCAN = 20_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _chat_kind_ok(c: ChatDTO, type_: str) -> bool:
    return type_ == "all" or c.type == type_


class ReadTools:
    """Набор инструментов чтения; каждый метод — отдельный MCP-инструмент."""

    def __init__(self, deps: ToolDeps) -> None:
        self.d = deps
        self._timeline_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}

    # ------------------------------------------------------------------ профиль

    async def max_get_me(self) -> dict[str, Any]:
        """Профиль владельца аккаунта MAX, состояние соединения и режим сервера."""
        a = self.d.adapter
        me = await a.me()
        s = await self.d.serializer()
        return {
            "profile": s.user(me, show_phone=self.d.settings.show_own_phone),
            "connection": a.state,
            "server_version": __version__,
            "client": self.d.settings.client,
            "mode": self.d.policy.summary(),
        }

    # -------------------------------------------------------------------- чаты

    async def _scan_chats(
        self,
        *,
        before: int | None,
        want: int,
        keep: Any,
        max_pages: int = CHAT_LIST_PAGES,
    ) -> tuple[list[ChatDTO], int | None]:
        out: list[ChatDTO] = []
        marker = before
        pages = 0
        while len(out) < want and pages < max_pages:
            page, nxt = await self.d.adapter.list_chats(before_ms=marker, limit=min(200, want * 2))
            pages += 1
            for c in page:
                if self.d.policy.can_read(c.id) and keep(c):
                    out.append(c)
                    if len(out) >= want:
                        return out, c.last_event_at or nxt
            if nxt is None or nxt == marker:
                return out, None
            marker = nxt
        return out, marker

    async def max_list_chats(
        self,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        cursor: CursorArg = None,
        type: Literal["all", "dialog", "group", "channel"] = "all",
        unread_only: bool = False,
        folder_id: Annotated[str | None, Field(description="id папки из max_list_folders")] = None,
        query: Annotated[str | None, Field(description="фильтр по названию (подстрока)")] = None,
    ) -> dict[str, Any]:
        """Список чатов (от недавно активных к старым): id, тип, название, непрочитанные,
        последнее сообщение. Для продолжения передайте next_cursor."""
        before = decode_cursor("chats", cursor)["m"] if cursor else None
        folder_ids: set[int] | None = None
        if folder_id is not None:
            folders = {f.id: f for f in await self.d.adapter.list_folders()}
            if folder_id not in folders:
                raise NotFound(f"Папка {folder_id!r} не найдена")
            folder_ids = set(folders[folder_id].chat_ids)
        q = fold(query) if query else None

        def keep(c: ChatDTO) -> bool:
            if not _chat_kind_ok(c, type) or (unread_only and c.unread <= 0):
                return False
            if folder_ids is not None and c.id not in folder_ids:
                return False
            return not (q and q not in fold(c.title))

        chats, nxt = await self._scan_chats(before=before, want=limit, keep=keep)
        s = await self.d.serializer()
        return {
            "chats": [s.chat(c) for c in chats],
            "next_cursor": encode_cursor("chats", {"m": nxt}) if nxt else None,
        }

    async def max_get_chat(self, chat_id: ChatId) -> dict[str, Any]:
        """Подробности чата: описание, ссылка, владелец, админы, закреп, счётчики."""
        self.d.policy.check_read(chat_id)
        chats = await self.d.adapter.get_chats([chat_id])
        if not chats:
            raise NotFound(f"Чат {chat_id} не найден")
        msgs = [m for m in (chats[0].pinned_message, chats[0].last_message) if m]
        s = await self.d.serializer(msgs)
        return {"chat": s.chat(chats[0], detailed=True)}

    async def max_list_folders(self) -> dict[str, Any]:
        """Папки чатов: id, название, явно включённые чаты и фильтры папки."""
        folders = await self.d.adapter.list_folders()
        s = await self.d.serializer()
        out = []
        for f in folders:
            item = s.folder(f)
            item["chat_ids"] = [c for c in f.chat_ids if self.d.policy.can_read(c)]
            out.append(item)
        return {"folders": out}

    async def max_search_chats(
        self,
        query: Annotated[str, Field(min_length=1, max_length=200)],
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """Поиск чатов по названию и контактов по имени (локальный фильтр, без серверного поиска)."""
        q = fold(query)
        chats, _ = await self._scan_chats(
            before=None, want=limit, keep=lambda c: q in fold(c.title), max_pages=CHAT_LIST_PAGES
        )
        contacts = [u for u in await self.d.adapter.contacts() if q in fold(u.name)][:limit]
        s = await self.d.serializer()
        return {
            "chats": [s.chat(c) for c in chats],
            "contacts": [s.user(u) for u in contacts],
            "method": "filter",
        }

    # --------------------------------------------------------------- сообщения

    async def _page(
        self, messages: list[MessageDTO], fmt: str, order: str
    ) -> tuple[Any, list[MessageDTO]]:
        ordered = sorted(messages, key=lambda m: m.key, reverse=order == "desc")
        s = await self.d.serializer(ordered)
        if fmt == "compact":
            return s.compact(ordered), ordered
        return [s.message(m) for m in ordered], ordered

    async def max_read_messages(
        self,
        chat_id: ChatId,
        date_from: DateArg = None,
        date_to: DateArg = None,
        limit: Annotated[
            int, Field(ge=1, le=5000, description="≤1000 (full), ≤5000 (compact)")
        ] = 100,
        cursor: CursorArg = None,
        order: Literal["asc", "desc"] = "asc",
        sender_id: Annotated[int | None, Field(description="только сообщения этого автора")] = None,
        format: Literal["full", "compact"] = "full",
    ) -> dict[str, Any]:
        """История чата за период. Без date_from — последние сообщения (next_cursor ведёт к более
        старым); с date_from и order=asc — от начала периода вперёд. Не отмечает прочитанным."""
        self.d.policy.check_read(chat_id)
        if format == "full" and limit > 1000:
            raise InvalidArgument("limit ≤ 1000 для format=full (для compact ≤ 5000)")
        if cursor:
            c = decode_cursor("hist", cursor)
            if c["c"] != chat_id:
                raise InvalidArgument("cursor относится к другому чату")
            lo, hi, direction = c["f"], c["t"], c["d"]
            after = (c["kt"], c["ki"])
            sender_id = c.get("s")
        else:
            lo, hi = parse_range(date_from, date_to, self.d.tz, _now_ms())
            direction = "fwd" if (lo is not None and order == "asc") else "back"
            after = start_key(direction, lo, hi)
        pred = (lambda m: m.sender_id == sender_id) if sender_id is not None else None
        res = await walk(
            self.d.adapter,
            chat_id,
            direction=direction,
            after=after,
            from_ms=lo,
            to_ms=hi,
            want=limit,
            predicate=pred,
            max_scan=max(limit * 20, 5000) if pred else None,
        )
        body, ordered = await self._page(res.items, format, order)
        has_more = not res.exhausted and res.last_key is not None
        nxt = None
        if has_more and res.last_key is not None:
            nxt = encode_cursor(
                "hist",
                {
                    "c": chat_id,
                    "f": lo,
                    "t": hi,
                    "d": direction,
                    "kt": res.last_key[0],
                    "ki": res.last_key[1],
                    "s": sender_id,
                },
            )
        s = await self.d.serializer()
        out: dict[str, Any] = {
            "chat_id": chat_id,
            "count": len(ordered),
            "direction": "older" if direction == "back" else "newer",
            "range": {
                "from": s.iso(ordered[0].time_ms) if ordered else None,
                "to": s.iso(ordered[-1].time_ms) if ordered else None,
            },
            "has_more": has_more,
            "next_cursor": nxt,
        }
        out["messages" if format == "full" else "text"] = body
        return out

    async def max_messages_around(
        self,
        chat_id: ChatId,
        date: DateArg = None,
        message_id: Annotated[int | None, Field(description="якорь — id сообщения")] = None,
        before: Annotated[int, Field(ge=0, le=200)] = 20,
        after: Annotated[int, Field(ge=0, le=200)] = 20,
        format: Literal["full", "compact"] = "full",
    ) -> dict[str, Any]:
        """Сообщения вокруг даты или сообщения (контекст). Укажите date или message_id."""
        self.d.policy.check_read(chat_id)
        if (date is None) == (message_id is None):
            raise InvalidArgument("Укажите ровно одно: date или message_id")
        if message_id is not None:
            found = await self.d.adapter.get_messages(chat_id, [message_id])
            if not found:
                raise NotFound(f"Сообщение {message_id} в чате {chat_id} не найдено")
            anchor_key = found[0].key
        else:
            assert date is not None
            anchor_key = (parse_when(date, self.d.tz), -1)
        older = (
            await walk(
                self.d.adapter,
                chat_id,
                direction="back",
                after=(anchor_key[0], anchor_key[1] if message_id is not None else -1),
                from_ms=None,
                to_ms=anchor_key[0],
                want=before,
                page=max(before, 1) + 5,
            )
            if before
            else None
        )
        newer = await walk(
            self.d.adapter,
            chat_id,
            direction="fwd",
            after=(anchor_key[0], anchor_key[1] - 1 if message_id is not None else -1),
            from_ms=None,
            to_ms=MAX_KEY,
            want=after + (1 if message_id is not None else 0),
            page=max(after, 1) + 5,
        )
        msgs = {m.id: m for m in (older.items if older else []) + newer.items}
        anchor_id = message_id
        if anchor_id is None:
            anchor_id = newer.items[0].id if newer.items else None
        body, ordered = await self._page(list(msgs.values()), format, "asc")
        out: dict[str, Any] = {
            "chat_id": chat_id,
            "anchor_message_id": anchor_id,
            "count": len(ordered),
        }
        out["messages" if format == "full" else "text"] = body
        return out

    async def max_get_messages(
        self,
        chat_id: ChatId,
        message_ids: Annotated[list[int], Field(min_length=1, max_length=100)],
    ) -> dict[str, Any]:
        """Сообщения по id (до 100)."""
        self.d.policy.check_read(chat_id)
        ids = unique_ints(message_ids, max_items=100, name="message_ids")
        msgs = await self.d.adapter.get_messages(chat_id, ids)
        body, _ = await self._page(msgs, "full", "asc")
        found = {m.id for m in msgs}
        return {"messages": body, "missing": [i for i in ids if i not in found]}

    async def max_chat_timeline(
        self,
        chat_id: ChatId,
        granularity: Literal["day", "month"] = "month",
        date_from: DateArg = None,
        date_to: DateArg = None,
    ) -> dict[str, Any]:
        """Гистограмма активности чата (период → число сообщений), чтобы найти, когда шла
        переписка. Сканирует до 20 000 последних сообщений периода."""
        self.d.policy.check_read(chat_id)
        lo, hi = parse_range(date_from, date_to, self.d.tz, _now_ms())
        key = (chat_id, granularity, lo, hi // 60_000)
        cached = self._timeline_cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        counts: Counter[str] = Counter()
        fmt = "%Y-%m-%d" if granularity == "day" else "%Y-%m"
        tz = self.d.tz

        def count(m: MessageDTO) -> bool:
            counts[datetime.fromtimestamp(m.time_ms / 1000, tz=tz).strftime(fmt)] += 1
            return False

        res = await walk(
            self.d.adapter,
            chat_id,
            direction="back",
            after=start_key("back", lo, hi),
            from_ms=lo,
            to_ms=hi,
            want=1,
            max_scan=TIMELINE_SCAN,
            predicate=count,
        )
        out = {
            "chat_id": chat_id,
            "granularity": granularity,
            "periods": [{"period": p, "messages": n} for p, n in sorted(counts.items())],
            "total": sum(counts.values()),
            "complete": res.exhausted,
        }
        self._timeline_cache[key] = (time.monotonic(), out)
        return out

    async def max_search_messages(
        self,
        query: Annotated[str, Field(min_length=1, max_length=500)],
        chat_id: Annotated[int | None, Field(description="искать только в этом чате")] = None,
        date_from: DateArg = None,
        date_to: DateArg = None,
        sender_id: int | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        cursor: CursorArg = None,
        scan_limit: Annotated[int, Field(ge=100, le=20_000)] = 5000,
    ) -> dict[str, Any]:
        """Поиск сообщений по словам (все слова, без учёта регистра и ё/е). Сканирует историю
        за период; без chat_id — до 20 последних активных чатов. Для продолжения — next_cursor."""
        if cursor:
            c = decode_cursor("search", cursor)
            if c["q"] != fold(query):
                raise InvalidArgument("cursor относится к другому запросу")
            chats: list[int] = c["chats"]
            idx, lo, hi = c["i"], c["f"], c["t"]
            after: tuple[int, int] | None = (c["kt"], c["ki"]) if c["kt"] is not None else None
            sender_id, scan_limit = c.get("s"), c["sl"]
        else:
            lo, hi = parse_range(date_from, date_to, self.d.tz, _now_ms())
            if chat_id is not None:
                self.d.policy.check_read(chat_id)
                chats = [chat_id]
            else:
                found, _ = await self._scan_chats(
                    before=None,
                    want=SEARCH_CHATS_DEFAULT,
                    keep=lambda c: lo is None or c.last_event_at == 0 or c.last_event_at >= lo,
                    max_pages=3,
                )
                chats = [c.id for c in found]
            idx, after = 0, None
        pred = text_matcher(query, sender_id)
        results: list[MessageDTO] = []
        scanned = 0
        next_state: dict[str, Any] | None = None
        while idx < len(chats):
            cid = chats[idx]
            if not self.d.policy.can_read(cid):
                idx, after = idx + 1, None
                continue
            res = await walk(
                self.d.adapter,
                cid,
                direction="back",
                after=after or start_key("back", lo, hi),
                from_ms=lo,
                to_ms=hi,
                want=limit - len(results),
                max_scan=scan_limit - scanned,
                predicate=pred,
            )
            results.extend(res.items)
            scanned += res.scanned
            if not res.exhausted and res.last_key is not None:
                after = res.last_key
                if len(results) >= limit or scanned >= scan_limit:
                    next_state = {"i": idx, "kt": after[0], "ki": after[1]}
                    break
            idx, after = idx + 1, None
            if len(results) >= limit or scanned >= scan_limit:
                if idx < len(chats):
                    next_state = {"i": idx, "kt": None, "ki": None}
                break
        s = await self.d.serializer(results)
        nxt = None
        if next_state is not None:
            nxt = encode_cursor(
                "search",
                {
                    "q": fold(query),
                    "chats": chats,
                    "f": lo,
                    "t": hi,
                    "s": sender_id,
                    "sl": scan_limit,
                    **next_state,
                },
            )
        return {
            "method": "scan",
            "messages": [s.message(m) for m in results],
            "count": len(results),
            "scanned": scanned,
            "chats_searched": min(idx + 1, len(chats)) if chats else 0,
            "exhausted": nxt is None,
            "next_cursor": nxt,
        }

    async def max_unread_summary(
        self,
        limit_chats: Annotated[int, Field(ge=1, le=100)] = 30,
        per_chat: Annotated[int, Field(ge=0, le=50)] = 5,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        """Чаты с непрочитанными и последние непрочитанные сообщения в каждом. Ничего не
        отмечает прочитанным (счётчики останутся как были)."""
        folder_ids: set[int] | None = None
        if folder_id is not None:
            folders = {f.id: f for f in await self.d.adapter.list_folders()}
            if folder_id not in folders:
                raise NotFound(f"Папка {folder_id!r} не найдена")
            folder_ids = set(folders[folder_id].chat_ids)
        chats, _ = await self._scan_chats(
            before=None,
            want=limit_chats,
            keep=lambda c: c.unread > 0 and (folder_ids is None or c.id in folder_ids),
            max_pages=5,
        )
        items = []
        all_msgs: list[MessageDTO] = []
        per: dict[int, list[MessageDTO]] = {}
        for c in chats:
            n = min(c.unread, per_chat)
            msgs: list[MessageDTO] = []
            if n > 0:
                page = await self.d.adapter.history(
                    c.id, anchor_ms=_now_ms() + 60_000, backward=n, forward=0
                )
                msgs = sorted(page, key=lambda m: m.key)[-n:]
            per[c.id] = msgs
            all_msgs.extend(msgs)
        s = await self.d.serializer(all_msgs)
        for c in chats:
            item = s.chat(c)
            item.pop("last_message", None)
            item["messages"] = [s.message(m) for m in per[c.id]]
            items.append(item)
        return {
            "chats": items,
            "total_unread": sum(c.unread for c in chats),
            "note": "Непрочитанное не изменено: max_unread_summary не отмечает сообщения прочитанными.",
        }

    async def max_list_comments(
        self,
        chat_id: Annotated[int, Field(description="id канала")],
        post_id: Annotated[int, Field(description="id поста (сообщения канала)")],
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
        cursor: CursorArg = None,
    ) -> dict[str, Any]:
        """Комментарии к посту канала (от новых к старым, страницами)."""
        self.d.policy.check_read(chat_id)
        if cursor:
            c = decode_cursor("comments", cursor)
            if (c["c"], c["p"]) != (chat_id, post_id):
                raise InvalidArgument("cursor относится к другому посту")
            bound: tuple[int, int] = (c["kt"], c["ki"])
        else:
            bound = (_now_ms() + 60_000, MAX_KEY)
        got: list[MessageDTO] = []
        seen: set[int] = set()
        anchor = bound[0]
        exhausted = False
        while len(got) < limit:
            page = await self.d.adapter.comments(
                chat_id,
                post_id,
                anchor_ms=anchor,
                backward=min(100, limit - len(got) + 1),
                forward=0,
            )
            fresh = sorted(
                (m for m in page if m.key < bound and m.id not in seen),
                key=lambda m: m.key,
                reverse=True,
            )
            if not fresh:
                exhausted = True
                break
            for m in fresh:
                seen.add(m.id)
                got.append(m)
                bound = m.key
                if len(got) >= limit:
                    break
            anchor = bound[0]
        total = await self.d.adapter.comments_count(chat_id, post_id)
        s = await self.d.serializer(got)
        nxt = None
        if not exhausted and got:
            nxt = encode_cursor(
                "comments", {"c": chat_id, "p": post_id, "kt": bound[0], "ki": bound[1]}
            )
        return {
            "chat_id": chat_id,
            "post_id": post_id,
            "total": total,
            "comments": [s.message(m) for m in got],
            "next_cursor": nxt,
        }

    # ----------------------------------------------------------- люди и ссылки

    async def max_list_members(
        self,
        chat_id: ChatId,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        cursor: CursorArg = None,
    ) -> dict[str, Any]:
        """Участники группы/канала: id, имя, админ, последний визит (без телефонов)."""
        self.d.policy.check_read(chat_id)
        marker = None
        if cursor:
            c = decode_cursor("members", cursor)
            if c["c"] != chat_id:
                raise InvalidArgument("cursor относится к другому чату")
            marker = c["m"]
        members, nxt = await self.d.adapter.members(chat_id, marker=marker, limit=limit)
        s = await self.d.serializer()
        return {
            "members": [s.member(m) for m in members],
            "next_cursor": encode_cursor("members", {"c": chat_id, "m": nxt}) if nxt else None,
        }

    async def max_get_users(
        self, user_ids: Annotated[list[int], Field(min_length=1, max_length=100)]
    ) -> dict[str, Any]:
        """Пользователи по id: имя, ник, описание (телефоны не выдаются)."""
        ids = unique_ints(user_ids, max_items=100, name="user_ids")
        users = await self.d.adapter.users(ids)
        s = await self.d.serializer()
        found = {u.id for u in users}
        return {"users": [s.user(u) for u in users], "missing": [i for i in ids if i not in found]}

    async def max_list_contacts(
        self,
        query: Annotated[str | None, Field(description="фильтр по имени")] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
        cursor: CursorArg = None,
    ) -> dict[str, Any]:
        """Контакты аккаунта (те, что сервер прислал при входе), с фильтром по имени."""
        offset = decode_cursor("contacts", cursor)["o"] if cursor else 0
        users = await self.d.adapter.contacts()
        if query:
            q = fold(query)
            users = [
                u for u in users if q in fold(u.name) or (u.username and q in fold(u.username))
            ]
        users = sorted(users, key=lambda u: fold(u.name))
        page = users[offset : offset + limit]
        s = await self.d.serializer()
        more = offset + limit < len(users)
        return {
            "contacts": [s.user(u) for u in page],
            "total": len(users),
            "next_cursor": encode_cursor("contacts", {"o": offset + limit}) if more else None,
        }

    async def max_resolve_link(
        self,
        link: Annotated[
            str,
            Field(
                min_length=2, max_length=300, description="https://max.ru/…, max.ru/join/… или @ник"
            ),
        ],
    ) -> dict[str, Any]:
        """Что за чат/канал/пользователь по ссылке. Только смотрит — НЕ вступает в чат."""
        target = await self.d.adapter.resolve_link(link.strip())
        s = await self.d.serializer()
        if target.chat is not None:
            if not self.d.policy.can_read(target.chat.id):
                return {"type": "chat", "id": target.chat.id, "restricted": True}
            return {"type": "chat", "chat": s.chat(target.chat, detailed=True)}
        if target.user is not None:
            return {"type": "user", "user": s.user(target.user)}
        raise NotFound("По ссылке ничего не найдено")

    # ----------------------------------------------------------------- вложения

    async def _attachment(self, ref: str) -> tuple[int, MessageDTO, int]:
        chat_id, msg_id, idx = parse_ref(ref)
        self.d.policy.check_read(chat_id)
        msgs = await self.d.adapter.get_messages(chat_id, [msg_id])
        if not msgs:
            raise NotFound("Сообщение для ref не найдено")
        msg = msgs[0]
        if not 0 <= idx < len(msg.attachments):
            raise NotFound("Вложение для ref не найдено")
        return chat_id, msg, idx

    async def max_get_attachment_info(
        self,
        ref: Annotated[str, Field(description="ref вложения вида max-att:<chat>:<msg>:<index>")],
    ) -> dict[str, Any]:
        """Подробности вложения: тип, имя, размер, длительность, расшифровка голосового."""
        _, msg, idx = await self._attachment(ref)
        s = await self.d.serializer([msg])
        return {"ref": ref, "attachment": s.attachment(msg.attachments[idx], msg)}

    async def max_download_media(
        self,
        ref: Annotated[str, Field(description="ref вложения вида max-att:<chat>:<msg>:<index>")],
        inline: Annotated[
            bool, Field(description="вернуть картинку прямо в ответе (≤ 1 МБ)")
        ] = False,
    ) -> Any:
        """Скачивает фото/файл/голосовое/видео в MAX_DOWNLOAD_DIR и возвращает путь, размер,
        тип, sha256. inline=true для картинок — вернуть изображение в ответе."""
        chat_id, msg, idx = await self._attachment(ref)
        att = msg.attachments[idx]
        if not att.downloadable:
            raise InvalidArgument(f"Вложение типа {att.kind} нельзя скачать")
        settings = self.d.settings
        url = await self.d.adapter.media_url(chat_id, msg.id, att)
        safe_name = (
            "".join(
                ch if ch.isalnum() or ch in "._-" else "_"
                for ch in normalize_text(att.name or att.kind)
            )[:80].lstrip(".")
            or att.kind
        )
        dest = settings.download_path / f"{chat_id}_{msg.id}_{idx}_{safe_name}"
        blob = await self.d.adapter.download(url, dest, max_bytes=settings.download_max_bytes)
        info = {
            "ref": make_ref(chat_id, msg.id, idx),
            "path": str(blob.path),
            "size": blob.size,
            "mime": blob.mime,
            "sha256": blob.sha256,
        }
        if inline:
            if not blob.mime.startswith("image/"):
                raise InvalidArgument("inline=true доступно только для изображений")
            if blob.size > settings.inline_image_bytes:
                raise InvalidArgument(
                    f"Картинка больше {settings.inline_image_bytes} байт — используйте путь из ответа"
                )
            data = await asyncio.to_thread(blob.path.read_bytes)
            return [info, Image(data=data, format=blob.mime.split("/", 1)[1])]
        return info

    # ----------------------------------------------------------------- события

    async def max_wait_updates(
        self,
        cursor: Annotated[
            str | None,
            Field(description="cursor из прошлого ответа; без него — только новые события"),
        ] = None,
        timeout_s: Annotated[int, Field(ge=0, le=120)] = 30,
        types: Annotated[
            list[Literal["message", "edit", "delete", "read", "reaction", "chat"]] | None,
            Field(description="типы событий (по умолчанию message, edit, delete)"),
        ] = None,
        chat_id: int | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """Ждёт новые события (сообщения, правки, удаления…) после cursor до timeout_s секунд.
        Ничего не исполняет и не отмечает прочитанным."""
        if chat_id is not None:
            self.d.policy.check_read(chat_id)
        after = None
        if cursor:
            after = decode_cursor("events", cursor)["seq"]
        batch = await self.d.adapter.events().wait(
            after,
            timeout=float(timeout_s),
            types=types or ["message", "edit", "delete"],
            chat_id=chat_id,
            limit=limit,
        )
        msgs = [e.message for e in batch.events if e.message is not None]
        s = await self.d.serializer(msgs)
        return {
            "events": [s.event(e) for e in batch.events],
            "cursor": encode_cursor("events", {"seq": batch.cursor}),
            "gap": batch.gap,
            "connection": self.d.adapter.state,
        }

    async def max_get_reactions(
        self,
        chat_id: ChatId,
        message_ids: Annotated[list[int], Field(min_length=1, max_length=100)],
    ) -> dict[str, Any]:
        """Реакции на сообщения: эмодзи → количество, и моя реакция."""
        self.d.policy.check_read(chat_id)
        ids = unique_ints(message_ids, max_items=100, name="message_ids")
        data = await self.d.adapter.reactions(chat_id, ids)
        return {
            "reactions": [
                {"message_id": mid, "counters": [[r, n] for r, n in counters], "my_reaction": mine}
                for mid, (counters, mine) in sorted(data.items())
            ]
        }


# инструменты, выдающие чужой текст: в описание добавляется предупреждение о недоверенных данных
UNTRUSTED_OUTPUT: frozenset[str] = frozenset(
    {
        "max_list_folders",
        "max_list_chats",
        "max_get_chat",
        "max_search_chats",
        "max_read_messages",
        "max_messages_around",
        "max_get_messages",
        "max_search_messages",
        "max_unread_summary",
        "max_list_comments",
        "max_list_members",
        "max_get_users",
        "max_list_contacts",
        "max_resolve_link",
        "max_get_attachment_info",
        "max_wait_updates",
    }
)

READ_TOOL_NAMES: tuple[str, ...] = (
    "max_get_me",
    "max_list_chats",
    "max_get_chat",
    "max_list_folders",
    "max_read_messages",
    "max_messages_around",
    "max_get_messages",
    "max_chat_timeline",
    "max_search_messages",
    "max_search_chats",
    "max_unread_summary",
    "max_list_comments",
    "max_list_members",
    "max_get_users",
    "max_list_contacts",
    "max_resolve_link",
    "max_get_attachment_info",
    "max_download_media",
    "max_wait_updates",
    "max_get_reactions",
)

_TITLES = {
    "max_get_me": "MAX: мой профиль и состояние",
    "max_list_chats": "MAX: список чатов",
    "max_get_chat": "MAX: подробности чата",
    "max_list_folders": "MAX: папки",
    "max_read_messages": "MAX: читать историю",
    "max_messages_around": "MAX: сообщения вокруг точки",
    "max_get_messages": "MAX: сообщения по id",
    "max_chat_timeline": "MAX: активность чата по периодам",
    "max_search_messages": "MAX: поиск сообщений",
    "max_search_chats": "MAX: поиск чатов и контактов",
    "max_unread_summary": "MAX: сводка непрочитанного",
    "max_list_comments": "MAX: комментарии к посту",
    "max_list_members": "MAX: участники чата",
    "max_get_users": "MAX: пользователи по id",
    "max_list_contacts": "MAX: контакты",
    "max_resolve_link": "MAX: что по ссылке",
    "max_get_attachment_info": "MAX: информация о вложении",
    "max_download_media": "MAX: скачать вложение",
    "max_wait_updates": "MAX: ждать новые события",
    "max_get_reactions": "MAX: реакции",
}


def _description(name: str) -> str:
    doc = " ".join((getattr(ReadTools, name).__doc__ or "").split())
    if name in UNTRUSTED_OUTPUT:
        doc += " " + UNTRUSTED_NOTICE
    return doc


def register_read_tools(server: MCPServer[Any], deps: ToolDeps) -> ReadTools:
    tools = ReadTools(deps)
    for name in READ_TOOL_NAMES:
        fn = tool_guard(name)(getattr(tools, name))
        server.add_tool(
            fn,
            name=name,
            title=_TITLES[name],
            description=_description(name),
            annotations=READ_ANNOTATIONS.model_copy(update={"title": _TITLES[name]}),
            structured_output=False if name == "max_download_media" else None,
        )
    return tools
