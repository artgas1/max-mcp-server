"""PyMaxAdapter — единственное место (вместе с raw.py), где используется PyMax.

Ключевые решения (design.md, 1.3–1.9):
- ``telemetry=False`` всегда; ``VersionCatalog(remote=False)``; ``relogin=False``;
- ``reconnect=False`` у PyMax + свой экспоненциальный backoff (1→60 с, джиттер);
- в ``serve`` никакой интерактивной авторизации (AuthFlow сразу бросает SessionExpired);
- ``interactive`` в пинге = ``MAX_APPEAR_ONLINE`` (по умолчанию False);
- сессия — в SecureFileStore (0600/0700), запись только после успешного LOGIN;
- одно соединение на профиль (fcntl-lock).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import random
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

from pymax import Client, ExtraConfig, WebClient
from pymax.auth import QrAuthFlow, SmsAuthFlow
from pymax.auth.models import AuthResult
from pymax.config import ClientConfig
from pymax.exceptions import ApiError, PyMaxError
from pymax.protocol.enums import Opcode
from pymax.session import SessionInfo
from pymax.types import Chat, Message, User
from pymax.types.domain.enums import ChatType as PmChatType
from pymax.types.domain.enums import LinkType, MessageStatus
from pymax.versions.catalog import VersionCatalog

from max_mcp_server.adapter import raw
from max_mcp_server.adapter.base import (
    AdapterState,
    AttachmentDTO,
    ChatDTO,
    ChatType,
    FolderDTO,
    ForwardDTO,
    LinkTarget,
    MediaBlob,
    MemberDTO,
    MessageDTO,
    UserDTO,
)
from max_mcp_server.adapter.events import EventBuffer
from max_mcp_server.config import Settings
from max_mcp_server.errors import (
    ConfigError,
    Forbidden,
    InvalidArgument,
    MaxMcpError,
    NotFound,
    NotLoggedIn,
    RateLimited,
    SessionExpired,
    UpstreamError,
)
from max_mcp_server.logging import pymax_level
from max_mcp_server.security.ratelimit import TokenBucket
from max_mcp_server.session.store import ProfileLock, SecureFileStore, ensure_private_dir

log = logging.getLogger("max_mcp_server.adapter")
T = TypeVar("T")

BACKOFF_MIN = 1.0
BACKOFF_MAX = 60.0
READY_WAIT = 10.0
PASSWORD_ATTEMPTS = 3

# ---------------------------------------------------------------------------- ошибки

_FLOOD_RE = re.compile(r"flood|too.?many|limit|rate", re.I)
_NOT_FOUND_RE = re.compile(r"not.?found|no.?such|unknown.?(chat|message|user)", re.I)
_FORBIDDEN_RE = re.compile(r"access|denied|forbidden|permission|not.?allowed|privacy", re.I)


def is_session_revoked(exc: BaseException) -> bool:
    return (
        isinstance(exc, ApiError)
        and int(exc.opcode) == int(Opcode.LOGIN)
        and any(err in (exc.error, exc.message) for err in ("FAIL_LOGIN_TOKEN", "FAIL_LOGOUT_ALL"))
    )


def translate_error(exc: BaseException) -> MaxMcpError:
    """Исключения PyMax/сети → наши. Сырые error/title логируются для сбора кодов (без payload)."""
    if isinstance(exc, MaxMcpError):
        return exc
    if isinstance(exc, ApiError):
        if is_session_revoked(exc):
            return SessionExpired()
        code = exc.error or ""
        log.info("MAX ApiError opcode=%s error=%r title=%r", exc.opcode, code, exc.title)
        probe = f"{code} {exc.title or ''}"
        if _FLOOD_RE.search(probe):
            return RateLimited(
                "MAX ограничил частоту запросов. Подождите минуту и повторите (лучше реже и меньшими страницами)."
            )
        if _NOT_FOUND_RE.search(probe):
            return NotFound("MAX: объект не найден (или нет доступа)")
        if _FORBIDDEN_RE.search(probe):
            return Forbidden("MAX: нет доступа к этому объекту")
        safe_code = re.sub(r"[^A-Za-z0-9_.\-]", "", code)[:60] or "unknown"
        return UpstreamError(f"MAX вернул ошибку ({safe_code}). Запрос не выполнен.")
    if isinstance(exc, TimeoutError):
        return UpstreamError("MAX не ответил вовремя; повторите запрос позже.", retryable=True)
    if isinstance(exc, ConnectionError | OSError | EOFError):
        return UpstreamError("Нет соединения с MAX; идёт переподключение.", retryable=True)
    if isinstance(exc, ValueError):
        return InvalidArgument(f"Неверный аргумент: {str(exc)[:200]}")
    if isinstance(exc, RuntimeError) and "expired" in str(exc).lower():
        return UpstreamError("QR-код истёк до подтверждения.")
    if isinstance(exc, PyMaxError):
        return UpstreamError("Ошибка клиента MAX; подробности в логе сервера.")
    log.error("unexpected error in adapter", exc_info=exc)
    return UpstreamError("Неожиданная ошибка при обращении к MAX; подробности в логе сервера.")


# ---------------------------------------------------------------------------- хранилище


class PendingStore:
    """StoreProtocol в памяти: сессия попадает на диск только после успешного входа."""

    def __init__(self, initial: SessionInfo | None = None) -> None:
        self.session = initial

    async def save_session(self, session_info: SessionInfo) -> None:
        self.session = session_info

    async def update_token(self, old_token: str, new_token: str, /) -> None:
        if self.session is not None:
            self.session.token = new_token

    async def load_session(self) -> SessionInfo | None:
        return self.session

    async def load_session_by_device_id(self, device_id: str) -> SessionInfo | None:
        return self.session if self.session and self.session.device_id == device_id else None

    async def load_session_by_phone(self, phone: str) -> SessionInfo | None:
        return self.session if self.session and self.session.phone == phone else None

    async def delete_session(self, token: str, /) -> None:
        if self.session is not None and self.session.token == token:
            self.session = None

    async def close(self) -> None:
        return None


def session_to_record(info: SessionInfo, client_kind: str) -> dict[str, Any]:
    return {"client_kind": client_kind, "session": info.model_dump(mode="json", by_alias=True)}


def record_to_session(record: dict[str, Any]) -> tuple[SessionInfo, str]:
    try:
        info = SessionInfo.model_validate(record["session"])
    except (KeyError, ValueError) as exc:
        raise NotLoggedIn("Файл сессии повреждён. Войдите заново: max-mcp login qr") from exc
    kind = record.get("client_kind", "web")
    return info, kind if kind in ("web", "mobile") else "web"


class FileStoreBridge(PendingStore):
    """StoreProtocol поверх SecureFileStore: изменения токена/sync сразу пишутся на диск."""

    def __init__(self, store: SecureFileStore, client_kind: str) -> None:
        record = store.read()
        if record is None:
            raise NotLoggedIn()
        info, _ = record_to_session(record)
        super().__init__(info)
        self._store = store
        self._kind = client_kind

    def _flush(self) -> None:
        if self.session is not None:
            self._store.write(session_to_record(self.session, self._kind))

    async def save_session(self, session_info: SessionInfo) -> None:
        await super().save_session(session_info)
        self._flush()

    async def update_token(self, old_token: str, new_token: str, /) -> None:
        await super().update_token(old_token, new_token)
        self._flush()

    async def delete_session(self, token: str, /) -> None:
        # relogin выключен; удаление сессии делает только CLI logout
        return None


# ---------------------------------------------------------------------------- клиенты


class _NoInteractiveAuth:
    """В serve интерактивный вход запрещён (stdio-протокол испортился бы)."""

    async def authenticate(self, app: Any) -> AuthResult:
        raise SessionExpired()


def _quiet(config: ClientConfig, appear_online: bool) -> ClientConfig:
    config.interactive = appear_online
    config.telemetry = False
    config.relogin = False
    return config


class QuietWebClient(WebClient):
    appear_online: bool = False

    async def _prepare_config(self) -> ClientConfig:
        return _quiet(await super()._prepare_config(), self.appear_online)


class QuietMobileClient(Client):
    appear_online: bool = False

    async def _prepare_config(self) -> ClientConfig:
        return _quiet(await super()._prepare_config(), self.appear_online)


def build_extra_config(settings: Settings, store: Any, *, token: str | None = None) -> ExtraConfig:
    return ExtraConfig(
        token=token,
        reconnect=False,
        relogin=False,
        telemetry=False,
        persist_session=True,
        store=store,
        request_timeout=settings.request_timeout,
        password_max_attempts=PASSWORD_ATTEMPTS,
        log_level=pymax_level(settings.log_level, pymax_debug=settings.log_pymax_debug),
    )


def build_client(
    settings: Settings,
    kind: str,
    store: Any,
    *,
    token: str | None = None,
    auth_flow: Any = None,
    phone: str = "",
) -> WebClient | Client:
    extra = build_extra_config(settings, store, token=token)
    work_dir = str(settings.state_path)
    client: WebClient | Client
    if kind == "mobile":
        client = QuietMobileClient(
            phone=phone,
            work_dir=work_dir,
            extra_config=extra,
            auth_flow=auth_flow,
            catalog=VersionCatalog(remote=False),
        )
    else:
        client = QuietWebClient(work_dir=work_dir, extra_config=extra, auth_flow=auth_flow)
    client.appear_online = settings.appear_online  # type: ignore[union-attr]
    # PyMax.configure_logging мог поставить свой обработчик — возвращаем наш (с редактором)
    from max_mcp_server.logging import pin_pymax_level

    pin_pymax_level(settings.log_level, pymax_debug=settings.log_pymax_debug)
    return client


# ---------------------------------------------------------------------------- маппинг


def _user_name(u: User | None) -> str:
    if u is None:
        return ""
    for n in u.names:
        if n.name:
            return n.name
        full = " ".join(p for p in (n.first_name, n.last_name) if p)
        if full:
            return full
    return ""


def map_user(u: User, *, own: bool = False) -> UserDTO:
    link = u.link if isinstance(u.link, str) else None
    username = None
    if link:
        username = link.rstrip("/").rsplit("/", 1)[-1] or None
    return UserDTO(
        id=u.id,
        name=_user_name(u) or f"id{u.id}",
        username=username,
        description=u.description,
        is_bot=bool(u.web_app) or "BOT" in (u.options or []),
        phone=(f"+{u.phone}" if u.phone else None) if own else None,
    )


def _enum_value(v: Any) -> str:
    return str(getattr(v, "value", v) or "")


def map_attachment(att: Any, index: int) -> AttachmentDTO:
    kind = _enum_value(getattr(att, "type", "")).upper()
    extra_fields = getattr(att, "model_extra", None) or {}
    if kind == "PHOTO":
        return AttachmentDTO(
            kind="photo",
            index=index,
            id=str(att.photo_id),
            width=att.width,
            height=att.height,
            downloadable=bool(att.base_url),
            source={"url": att.base_url},
        )
    if kind == "VIDEO":
        return AttachmentDTO(
            kind="video",
            index=index,
            id=str(att.video_id),
            width=att.width,
            height=att.height,
            duration_s=(
                att.duration / 1000 if att.duration and att.duration > 1000 else att.duration
            ),
            downloadable=True,
            source={"video_id": att.video_id},
        )
    if kind == "FILE":
        return AttachmentDTO(
            kind="file",
            index=index,
            id=str(att.file_id),
            name=att.name,
            size=att.size,
            downloadable=True,
            source={"file_id": att.file_id},
        )
    if kind == "AUDIO":
        transcript = None
        for key in ("transcription", "transcriptionText", "text"):
            val = extra_fields.get(key)
            if isinstance(val, str) and val.strip():
                transcript = val
                break
        dur = att.duration
        return AttachmentDTO(
            kind="voice",
            index=index,
            id=str(att.audio_id) if att.audio_id else None,
            duration_s=(dur / 1000 if dur and dur > 1000 else dur),
            downloadable=bool(att.url),
            transcript=transcript,
            extra={"transcription_status": _enum_value(att.transcription_status) or None},
            source={"url": att.url},
        )
    if kind == "STICKER":
        return AttachmentDTO(
            kind="sticker",
            index=index,
            id=str(att.sticker_id),
            width=att.width,
            height=att.height,
            extra={"sticker_id": att.sticker_id},
        )
    if kind == "CONTACT":
        name = att.name or " ".join(p for p in (att.first_name, att.last_name) if p) or None
        return AttachmentDTO(
            kind="contact", index=index, extra={"contact_id": att.contact_id, "contact_name": name}
        )
    if kind == "SHARE":
        return AttachmentDTO(
            kind="link",
            index=index,
            extra={"url": att.url, "title": att.title, "description": att.description},
        )
    if kind == "POLL":
        votes: dict[int, int] = {}
        if att.state and att.state.result:
            votes = {r.answer_id: r.vote_count for r in att.state.result}
        answers = [
            {"id": a.answer_id, "text": a.text, "votes": votes.get(a.answer_id or -1)}
            for a in att.answers
        ]
        return AttachmentDTO(
            kind="poll",
            index=index,
            id=str(att.poll_id),
            extra={
                "title": att.title,
                "answers": answers,
                "total_votes": att.state.total if att.state else None,
            },
        )
    if kind == "CALL":
        return AttachmentDTO(
            kind="call",
            index=index,
            duration_s=att.duration,
            extra={
                "call_type": _enum_value(att.call_type) or None,
                "hangup": _enum_value(att.hangup_type) or None,
            },
        )
    if kind == "CONTROL":
        return AttachmentDTO(
            kind="service",
            index=index,
            extra={"event": att.event, "title": att.title, "user_ids": extra_fields.get("userIds")},
        )
    if kind == "INLINE_KEYBOARD":
        return AttachmentDTO(kind="keyboard", index=index)
    return AttachmentDTO(kind="unknown", index=index, extra={"type": kind[:40] or None})


def map_message(m: Message, chat_id: int | None = None) -> MessageDTO:
    cid = m.chat_id if m.chat_id is not None else (chat_id if chat_id is not None else 0)
    reply_to = None
    fwd = None
    link = m.link
    if link is not None:
        if link.type == LinkType.REPLY:
            reply_to = link.message.id
        elif link.type == LinkType.FORWARD:
            src = link.message
            fwd = ForwardDTO(
                chat_id=getattr(link, "chat_id", None),
                chat_title=getattr(link, "chat_name", None),
                message_id=src.id,
                sender_id=src.sender,
                time_ms=src.time,
                text=src.text or "",
                attachment_kinds=tuple(
                    map_attachment(a, i).kind for i, a in enumerate(src.attaches)
                ),
            )
    attachments = tuple(map_attachment(a, i) for i, a in enumerate(m.attaches))
    kind = "text"
    if any(a.kind == "service" for a in attachments):
        kind = "service"
    elif fwd is not None:
        kind = "forward"
    elif attachments and not m.text:
        kind = attachments[0].kind
    reactions: tuple[tuple[str, int], ...] = ()
    mine = None
    if m.reaction_info is not None:
        reactions = tuple((c.reaction, c.count) for c in m.reaction_info.counters)
        mine = m.reaction_info.your_reaction
    return MessageDTO(
        id=m.id,
        chat_id=cid,
        time_ms=m.time,
        text=m.text or "",
        sender_id=m.sender,
        post_id=m.post_id,
        kind=kind,
        edited=m.status == MessageStatus.EDITED,
        deleted=m.status == MessageStatus.REMOVED,
        reply_to=reply_to,
        forwarded_from=fwd,
        attachments=attachments,
        reactions=reactions,
        my_reaction=mine,
    )


_CHAT_TYPES: dict[str, ChatType] = {
    PmChatType.DIALOG.value: "dialog",
    PmChatType.CHAT.value: "group",
    PmChatType.CHANNEL.value: "channel",
}


def map_chat(c: Chat, me_id: int | None, names: dict[int, str] | None = None) -> ChatDTO:
    ctype = _CHAT_TYPES.get(_enum_value(c.type), "unknown")
    peer = None
    title = c.title or ""
    if ctype == "dialog":
        others = [uid for uid in c.participants if uid != me_id]
        peer = others[0] if others else me_id
        if not title and peer == me_id:
            title = "Избранное"
        elif not title:
            title = (names or {}).get(peer or 0) or f"id{peer}"
    return ChatDTO(
        id=c.id,
        type=ctype,
        title=title,
        unread=c.new_messages or 0,
        members_count=c.participants_count or (len(c.participants) or None),
        last_message=map_message(c.last_message, c.id) if c.last_message else None,
        last_event_at=c.last_event_time or 0,
        peer_user_id=peer,
        description=c.description,
        link=c.link,
        owner_id=c.owner,
        admin_ids=tuple(c.admins or ()),
        pinned_message=map_message(c.pinned_message, c.id) if c.pinned_message else None,
        messages_count=c.messages_count or None,
        created_ms=c.created or None,
    )


_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"OggS", "audio/ogg"),
    (b"ID3", "audio/mpeg"),
)


def sniff_mime(head: bytes, fallback: str = "application/octet-stream") -> str:
    for sig, mime in _SIGNATURES:
        if head.startswith(sig):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    return fallback


def check_download_url(url: str, suffixes: Sequence[str]) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        raise Forbidden("Скачивание разрешено только по https с серверов MAX")
    if not any(host == s or host.endswith("." + s) for s in suffixes):
        raise Forbidden(f"Хост {host} не входит в allowlist загрузок MAX (MAX_DOWNLOAD_HOSTS)")
    return host


# ---------------------------------------------------------------------------- адаптер


class PyMaxAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._store = SecureFileStore(settings.state_path)
        self._lock = ProfileLock(settings.state_path)
        self._events = EventBuffer()
        self._bucket = TokenBucket(settings.rps, burst=max(2.0, settings.rps * 2))
        self._client: WebClient | Client | None = None
        self._state: AdapterState = "idle"
        self._ready = asyncio.Event()
        self._fatal: MaxMcpError | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._stopping = False
        self._kind = settings.client
        self._chat_cache: dict[int, Chat] = {}
        self._names: dict[int, str] = {}

    # ---------------------------------------------------------------- состояние

    @property
    def state(self) -> AdapterState:
        return self._state

    def events(self) -> EventBuffer:
        return self._events

    async def start(self) -> None:
        """Проверяет сессию, берёт lock профиля и запускает фоновое соединение.

        NotLoggedIn / SessionExpired / ProfileLocked / InsecurePermissions — сразу.
        Сетевые сбои не роняют старт: адаптер переподключается в фоне.
        """
        if self._supervisor is not None:
            return
        self._store.check()
        record = self._store.read()
        if record is None:
            self._fatal = NotLoggedIn()
            self._state = "expired"
            raise self._fatal
        _, kind = record_to_session(record)
        if kind != self.settings.client:
            log.warning(
                "session was created by %s client; using it (MAX_CLIENT=%s ignored)",
                kind,
                self.settings.client,
            )
        self._kind = kind
        self._lock.acquire()
        self._stopping = False
        self._state = "starting"
        self._supervisor = asyncio.create_task(self._supervise(), name="max-supervisor")
        waiter = asyncio.create_task(self._ready.wait())
        await asyncio.wait(
            {waiter, self._supervisor},
            timeout=self.settings.connect_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        waiter.cancel()
        if self._fatal is not None:
            fatal = self._fatal
            await self.stop()
            self._fatal, self._state = fatal, "expired"
            raise fatal
        if not self._ready.is_set():
            log.warning(
                "MAX not ready after %.0fs; continuing to reconnect in background",
                self.settings.connect_timeout,
            )

    async def stop(self) -> None:
        self._stopping = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._supervisor
            self._supervisor = None
        await self._close_client()
        self._lock.release()
        self._state = "stopped"

    async def _close_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    def _make_client(self) -> WebClient | Client:
        bridge = FileStoreBridge(self._store, self._kind)
        phone = bridge.session.phone if bridge.session else ""
        client = build_client(
            self.settings, self._kind, bridge, auth_flow=_NoInteractiveAuth(), phone=phone
        )
        client.on_start()(self._on_start)
        client.on_message()(self._on_message)
        client.on_message_edit()(self._on_edit)
        client.on_message_delete()(self._on_delete)
        client.on_message_read()(self._on_read)
        client.on_reaction_update()(self._on_reaction)
        client.on_chat_update()(self._on_chat)
        return client

    async def _supervise(self) -> None:
        delay = BACKOFF_MIN
        while not self._stopping:
            self._ready.clear()
            client = self._make_client()
            self._client = client
            started_at = time.monotonic()
            try:
                await client.start()  # возвращается при закрытии соединения
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                err = translate_error(exc)
                if isinstance(err, SessionExpired | NotLoggedIn):
                    self._fatal = err
                    self._state = "expired"
                    log.error("MAX session revoked; run `max-mcp login` again")
                    await self._close_client()
                    return
                log.warning("MAX connection lost (%s)", type(exc).__name__)
            finally:
                self._ready.clear()
            await self._close_client()
            if self._stopping:
                return
            if time.monotonic() - started_at > 120:
                delay = BACKOFF_MIN
            self._state = "reconnecting"
            sleep = min(BACKOFF_MAX, delay) * random.uniform(0.7, 1.3)  # noqa: S311
            log.info("reconnecting to MAX in %.1fs", sleep)
            await asyncio.sleep(sleep)
            delay = min(BACKOFF_MAX, delay * 2)

    async def _on_start(self, client: Any = None) -> None:
        self._state = "ready"
        self._ready.set()
        log.info("MAX connected")

    # ---------------------------------------------------------------- события

    async def _on_message(self, msg: Message, client: Any = None) -> None:
        dto = map_message(msg)
        await self._events.push("message", msg.chat_id, message=dto)

    async def _on_edit(self, msg: Message, client: Any = None) -> None:
        await self._events.push("edit", msg.chat_id, message=map_message(msg))

    async def _on_delete(self, ev: Any, client: Any = None) -> None:
        ids = list(getattr(ev, "message_ids", None) or [])
        if not ids and isinstance(ev, Message):
            ids = [ev.id]
        await self._events.push("delete", getattr(ev, "chat_id", None), data={"message_ids": ids})

    async def _on_read(self, ev: Any, client: Any = None) -> None:
        await self._events.push(
            "read",
            ev.chat_id,
            data={"user_id": ev.user_id, "mark": ev.mark, "set_as_unread": ev.set_as_unread},
        )

    async def _on_reaction(self, ev: Any, client: Any = None) -> None:
        counters = [[c.reaction, c.count] for c in (ev.counters or [])]
        await self._events.push(
            "reaction",
            ev.chat_id,
            data={"message_id": ev.message_id, "counters": counters, "total": ev.total_count},
        )

    async def _on_chat(self, chat: Chat, client: Any = None) -> None:
        self._chat_cache[chat.id] = chat
        await self._events.push("chat", chat.id, data={"unread": chat.new_messages})

    # ---------------------------------------------------------------- вызовы

    async def _client_ready(self) -> WebClient | Client:
        if self._fatal is not None:
            raise self._fatal
        if self._supervisor is None:
            raise NotLoggedIn(
                "Соединение с MAX не запущено (нет сессии или сервер останавливается)."
            )
        if not self._ready.is_set():
            try:
                await asyncio.wait_for(self._ready.wait(), timeout=READY_WAIT)
            except TimeoutError:
                if self._fatal is not None:
                    raise self._fatal from None
                raise UpstreamError(
                    "Нет соединения с MAX (идёт переподключение). Повторите через минуту.",
                    retryable=True,
                ) from None
        assert self._client is not None
        return self._client

    async def _call(self, fn: Callable[[WebClient | Client], Awaitable[T]]) -> T:
        client = await self._client_ready()
        await self._bucket.acquire()
        try:
            return await fn(client)
        except MaxMcpError:
            raise
        except Exception as exc:
            err = translate_error(exc)
            if isinstance(err, SessionExpired):
                self._fatal = err
                self._state = "expired"
            raise err from exc

    def _me_id(self) -> int | None:
        c = self._client
        try:
            return c.me.contact.id if c is not None and c.me is not None else None
        except RuntimeError:
            return None

    async def me(self) -> UserDTO:
        client = await self._client_ready()
        if client.me is None:
            raise UpstreamError("Профиль ещё не получен", retryable=True)
        return map_user(client.me.contact, own=True)

    async def _ensure_names(self, chats: Sequence[Chat]) -> dict[int, str]:
        me = self._me_id()
        missing = set()
        for c in chats:
            if _enum_value(c.type) == PmChatType.DIALOG.value and not c.title:
                for uid in c.participants:
                    if uid != me and uid not in self._names:
                        missing.add(uid)
        if missing:
            try:
                users = await self._call(lambda cl: cl.get_users(sorted(missing)))
                for u in users:
                    self._names[u.id] = _user_name(u)
            except MaxMcpError:
                log.debug("dialog names lookup failed", exc_info=True)
        return self._names

    async def _map_chats(self, chats: Sequence[Chat]) -> list[ChatDTO]:
        for c in chats:
            self._chat_cache[c.id] = c
        names = await self._ensure_names(chats)
        me = self._me_id()
        return [map_chat(c, me, names) for c in chats]

    async def list_chats(
        self, *, before_ms: int | None, limit: int
    ) -> tuple[list[ChatDTO], int | None]:
        chats = await self._call(lambda c: c.fetch_chats(marker=before_ms))
        chats = sorted(chats, key=lambda c: c.last_event_time or 0, reverse=True)
        nxt = min((c.last_event_time for c in chats if c.last_event_time), default=None)
        if nxt is not None and before_ms is not None and nxt >= before_ms:
            nxt = None
        return await self._map_chats(chats), nxt

    async def get_chats(self, chat_ids: Sequence[int]) -> list[ChatDTO]:
        chats = await self._call(lambda c: c.get_chats(list(chat_ids)))
        return await self._map_chats([c for c in chats if c is not None])

    async def list_folders(self) -> list[FolderDTO]:
        fl = await self._call(lambda c: c.get_folders())
        order = {fid: i for i, fid in enumerate(fl.folders_order)}
        folders = sorted(fl.folders, key=lambda f: order.get(f.id, len(order)))
        return [
            FolderDTO(
                id=f.id,
                title=f.title,
                chat_ids=tuple(f.include),
                filters=tuple(str(x)[:40] for x in f.filters),
            )
            for f in folders
        ]

    async def history(
        self, chat_id: int, *, anchor_ms: int, backward: int, forward: int
    ) -> list[MessageDTO]:
        msgs = await self._call(
            lambda c: c.fetch_history(
                chat_id,
                forward=forward,
                backward=backward,
                from_time=max(1, anchor_ms),
                interactive=False,
            )
        )
        return [map_message(m, chat_id) for m in msgs]

    async def get_messages(self, chat_id: int, ids: Sequence[int]) -> list[MessageDTO]:
        msgs = await self._call(lambda c: c.get_messages(chat_id, list(ids)))
        return [map_message(m, chat_id) for m in msgs if m is not None]

    async def comments(
        self, chat_id: int, post_id: int, *, anchor_ms: int | None, backward: int, forward: int
    ) -> list[MessageDTO]:
        msgs = await self._call(
            lambda c: c.fetch_comments(
                chat_id, post_id, from_=anchor_ms, backward=backward, forward=forward
            )
        )
        out = []
        for m in msgs:
            if m.post_id is None:
                m.post_id = post_id
            out.append(map_message(m, chat_id))
        return out

    async def comments_count(self, chat_id: int, post_id: int) -> int | None:
        try:
            infos = await self._call(lambda c: c.get_comments_info(chat_id, [post_id]))
        except MaxMcpError:
            return None
        for info in infos:
            if info.post_id == post_id and info.comments_info is not None:
                return info.comments_info.total_count
        return None

    async def members(
        self, chat_id: int, *, marker: int | None, limit: int
    ) -> tuple[list[MemberDTO], int | None]:
        members, nxt = await self._call(
            lambda c: c.get_chat_members(chat_id, marker=marker, count=limit)
        )
        chat = self._chat_cache.get(chat_id)
        if chat is None:
            with contextlib.suppress(MaxMcpError):
                chat = await self._call(lambda c: c.get_chat(chat_id))
        admins = set(chat.admins or []) | set(chat.admin_participants or {}) if chat else set()
        owner = chat.owner if chat else None
        out = [
            MemberDTO(
                user=map_user(m.contact),
                is_admin=m.contact.id in admins or m.contact.id == owner,
                is_owner=m.contact.id == owner,
                last_seen_ms=(
                    m.presence.seen * 1000
                    if m.presence and m.presence.seen and m.presence.seen < 10**11
                    else (m.presence.seen if m.presence else None)
                ),
            )
            for m in members
        ]
        return out, (nxt or None) if members else None

    async def users(self, ids: Sequence[int]) -> list[UserDTO]:
        users = await self._call(lambda c: c.get_users(list(ids)))
        for u in users:
            if u is not None:
                self._names[u.id] = _user_name(u)
        return [map_user(u) for u in users if u is not None]

    async def contacts(self) -> list[UserDTO]:
        client = await self._client_ready()
        return [map_user(u) for u in (client.contacts or []) if u is not None]

    async def resolve_link(self, link: str) -> LinkTarget:
        if "join/" in link:
            chat = await self._call(lambda c: c.resolve_group_by_link(link))
            if chat is None:
                return LinkTarget()
            return LinkTarget(chat=(await self._map_chats([chat]))[0])
        payload = await self._call(lambda c: raw.link_info(c, link))
        if isinstance(payload.get("chat"), dict):
            chat = Chat.model_validate(payload["chat"])
            return LinkTarget(chat=(await self._map_chats([chat]))[0])
        for key in ("user", "contact"):
            if isinstance(payload.get(key), dict):
                return LinkTarget(user=map_user(User.model_validate(payload[key])))
        return LinkTarget()

    async def reactions(
        self, chat_id: int, message_ids: Sequence[int]
    ) -> dict[int, tuple[tuple[tuple[str, int], ...], str | None]]:
        data = await self._call(lambda c: c.get_reactions(chat_id, list(message_ids)))
        out: dict[int, tuple[tuple[tuple[str, int], ...], str | None]] = {}
        for key, info in (data or {}).items():
            out[int(key)] = (
                tuple((c.reaction, c.count) for c in info.counters),
                info.your_reaction,
            )
        return out

    async def media_url(self, chat_id: int, message_id: int, att: AttachmentDTO) -> str:
        src = att.source
        if att.kind in ("photo", "voice"):
            url = src.get("url")
            if not url:
                raise NotFound("У вложения нет ссылки на файл")
            return str(url)
        if att.kind == "file":
            req = await self._call(
                lambda c: c.get_file_by_id(chat_id, message_id, int(src["file_id"]))
            )
            if req is None or not req.url:
                raise NotFound("MAX не выдал ссылку на файл")
            if req.unsafe:
                log.info("MAX marked file as unsafe chat=%s msg=%s", chat_id, message_id)
            return req.url
        if att.kind == "video":
            vreq = await self._call(
                lambda c: c.get_video_by_id(chat_id, message_id, int(src["video_id"]))
            )
            if vreq is None:
                raise NotFound("MAX не выдал ссылку на видео")
            if vreq.url:
                return vreq.url
            for val in (vreq.model_extra or {}).values():
                if isinstance(val, str) and val.startswith("https://"):
                    return val
            raise NotFound("MAX не выдал ссылку на видео")
        raise InvalidArgument(f"Вложение типа {att.kind} нельзя скачать")

    async def download(self, url: str, dest: Path, *, max_bytes: int) -> MediaBlob:
        """GET с allowlist хостов, без редиректов, с лимитом по потоку. Пишет файл 0600.

        TODO(тред 3): закрепление IP после проверки (DNS rebinding) и запрет private-адресов.
        """
        import aiohttp

        check_download_url(url, self.settings.download_host_suffixes)
        ensure_private_dir(dest.parent)
        await self._bucket.acquire()
        tmp = dest.with_name(f".{dest.name}.part")
        digest = hashlib.sha256()
        size = 0
        head = b""
        timeout = aiohttp.ClientTimeout(total=300, sock_read=60)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout, trust_env=False) as http,
                http.get(url, allow_redirects=False) as resp,
            ):
                if resp.status != 200:
                    raise UpstreamError(f"Сервер файлов MAX ответил {resp.status}")
                if resp.content_length and resp.content_length > max_bytes:
                    raise InvalidArgument(
                        f"Файл больше лимита {max_bytes} байт (MAX_DOWNLOAD_MAX_BYTES)"
                    )
                fd = os.open(
                    tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600
                )
                with os.fdopen(fd, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > max_bytes:
                            raise InvalidArgument(
                                f"Файл больше лимита {max_bytes} байт (MAX_DOWNLOAD_MAX_BYTES)"
                            )
                        if len(head) < 16:
                            head += chunk[: 16 - len(head)]
                        digest.update(chunk)
                        fh.write(chunk)
                header_mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            os.replace(tmp, dest)
        except MaxMcpError:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
            raise
        except Exception as exc:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
            raise translate_error(exc) from exc
        mime = sniff_mime(head, header_mime or "application/octet-stream")
        return MediaBlob(path=dest, size=size, mime=mime, sha256=digest.hexdigest())

    async def mark_read(self, chat_id: int, message_id: int) -> int:
        state = await self._call(lambda c: c.read_message(message_id, chat_id))
        return int(state.unread)


# ---------------------------------------------------------------------------- вход (CLI)


async def _login(
    settings: Settings,
    kind: str,
    make: Callable[[PendingStore], WebClient | Client],
    *,
    timeout: float,
) -> UserDTO:
    store = SecureFileStore(settings.state_path)
    store.check()
    pending = PendingStore()
    client = make(pending)
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout)
        if client.me is None or pending.session is None:
            raise UpstreamError("Вход не завершён: MAX не вернул профиль")
        me = map_user(client.me.contact, own=True)
        store.write(session_to_record(pending.session, kind))
        return me
    except MaxMcpError:
        raise
    except Exception as exc:
        raise translate_error(exc) from exc
    finally:
        with contextlib.suppress(Exception):
            await client.close()


async def login_qr(
    settings: Settings, qr_handler: Any, password_provider: Any, *, timeout: float
) -> UserDTO:
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        remaining = deadline - time.monotonic()
        if remaining <= 5:
            raise UpstreamError(
                "Не дождались подтверждения QR. Запустите max-mcp login qr ещё раз."
            )
        flow = QrAuthFlow(qr_handler, password_provider)

        def make(store: PendingStore, flow: QrAuthFlow = flow) -> WebClient | Client:
            return build_client(settings, "web", store, auth_flow=flow)

        try:
            return await _login(settings, "web", make, timeout=remaining)
        except UpstreamError as exc:
            cause = exc.__cause__
            if isinstance(cause, RuntimeError) and "expired" in str(cause).lower() and attempt < 10:
                log.info("QR expired, generating a new one (attempt %d)", attempt + 1)
                continue
            raise
        except TimeoutError as exc:
            raise UpstreamError("Не дождались подтверждения QR.") from exc


async def login_token(
    settings: Settings, token: str, *, kind: str = "web", timeout: float = 60
) -> UserDTO:
    token = token.strip()
    if not token or any(ch.isspace() for ch in token):
        raise ConfigError("Токен пустой или содержит пробелы")

    def make(store: PendingStore) -> WebClient | Client:
        return build_client(settings, kind, store, token=token, auth_flow=_NoInteractiveAuth())

    try:
        return await _login(settings, kind, make, timeout=timeout)
    except SessionExpired as exc:
        raise SessionExpired("MAX отклонил токен (недействителен или отозван).") from exc


async def login_phone(
    settings: Settings,
    phone: str,
    sms_provider: Any,
    password_provider: Any,
    *,
    timeout: float = 300,
) -> UserDTO:
    phone = re.sub(r"[\s()-]", "", phone)
    if not re.fullmatch(r"\+?\d{10,15}", phone):
        raise ConfigError("Телефон должен быть в международном формате, например +7XXXXXXXXXX")

    def make(store: PendingStore) -> WebClient | Client:
        flow = SmsAuthFlow(sms_provider, password_provider)
        return build_client(settings, "mobile", store, auth_flow=flow, phone=phone)

    return await _login(settings, "mobile", make, timeout=timeout)


async def probe_session(settings: Settings, *, timeout: float = 30) -> UserDTO:
    """Пробное подключение существующей сессией (для status/doctor). Ничего не меняет."""
    store = SecureFileStore(settings.state_path)
    record = store.read()
    if record is None:
        raise NotLoggedIn()
    _, kind = record_to_session(record)
    bridge = FileStoreBridge(store, kind)
    client = build_client(
        settings,
        kind,
        bridge,
        auth_flow=_NoInteractiveAuth(),
        phone=bridge.session.phone if bridge.session else "",
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout)
        if client.me is None:
            raise UpstreamError("MAX не вернул профиль")
        return map_user(client.me.contact, own=True)
    except MaxMcpError:
        raise
    except Exception as exc:
        raise translate_error(exc) from exc
    finally:
        with contextlib.suppress(Exception):
            await client.close()


async def logout_remote(settings: Settings, *, timeout: float = 30) -> bool:
    store = SecureFileStore(settings.state_path)
    record = store.read()
    if record is None:
        return False
    _, kind = record_to_session(record)
    bridge = FileStoreBridge(store, kind)
    client = build_client(
        settings,
        kind,
        bridge,
        auth_flow=_NoInteractiveAuth(),
        phone=bridge.session.phone if bridge.session else "",
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout)
        return bool(await client.logout())
    except Exception as exc:
        raise translate_error(exc) from exc
    finally:
        with contextlib.suppress(Exception):
            await client.close()
