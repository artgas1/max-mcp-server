"""Интерфейс адаптера MAX и собственные неизменяемые DTO (не модели PyMax).

``tools/`` работает только с этими типами — это позволяет тестировать без сети и
менять версию PyMax в одном месте (``adapter/pymax_adapter.py``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from max_mcp_server.adapter.events import EventBuffer

ChatType = Literal["dialog", "group", "channel", "unknown"]
AdapterState = Literal["idle", "starting", "ready", "reconnecting", "expired", "stopped"]


@dataclass(frozen=True)
class UserDTO:
    id: int
    name: str
    username: str | None = None
    description: str | None = None
    is_bot: bool = False
    phone: str | None = None  # только для собственного профиля; в выводе по умолчанию скрыт


@dataclass(frozen=True)
class AttachmentDTO:
    kind: str  # photo|video|file|voice|sticker|contact|link|poll|call|service|keyboard|unknown
    index: int
    id: str | None = None
    name: str | None = None
    size: int | None = None
    mime: str | None = None
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    downloadable: bool = False
    transcript: str | None = None
    # видимые поля, тексты в которых недоверенные (заголовки ссылок, текст опроса и т.п.)
    extra: Mapping[str, Any] = field(default_factory=dict)
    # служебное для скачивания (url/token/id) — НИКОГДА не сериализуется в ответ
    source: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class ForwardDTO:
    chat_id: int | None
    chat_title: str | None
    message_id: int | None
    sender_id: int | None
    time_ms: int | None
    text: str
    attachment_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class MessageDTO:
    id: int
    chat_id: int
    time_ms: int
    text: str = ""
    sender_id: int | None = None
    post_id: int | None = None
    kind: str = "text"
    edited: bool = False
    deleted: bool = False
    reply_to: int | None = None
    forwarded_from: ForwardDTO | None = None
    attachments: tuple[AttachmentDTO, ...] = ()
    reactions: tuple[tuple[str, int], ...] = ()
    my_reaction: str | None = None

    @property
    def key(self) -> tuple[int, int]:
        """Полный порядок сообщений в чате: (время, id)."""
        return (self.time_ms, self.id)


@dataclass(frozen=True)
class ChatDTO:
    id: int
    type: ChatType
    title: str
    unread: int = 0
    members_count: int | None = None
    last_message: MessageDTO | None = None
    last_event_at: int = 0
    peer_user_id: int | None = None
    # подробности (max_get_chat)
    description: str | None = None
    link: str | None = None
    owner_id: int | None = None
    admin_ids: tuple[int, ...] = ()
    pinned_message: MessageDTO | None = None
    messages_count: int | None = None
    created_ms: int | None = None


@dataclass(frozen=True)
class FolderDTO:
    id: str
    title: str
    chat_ids: tuple[int, ...] = ()
    filters: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemberDTO:
    user: UserDTO
    is_admin: bool = False
    is_owner: bool = False
    last_seen_ms: int | None = None


@dataclass(frozen=True)
class EventDTO:
    seq: int
    type: str  # message|edit|delete|read|reaction|chat
    chat_id: int | None
    time_ms: int
    message: MessageDTO | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MediaBlob:
    path: Path
    size: int
    mime: str
    sha256: str


@dataclass(frozen=True)
class LinkTarget:
    """Результат разбора ссылки: чат/канал или пользователь."""

    chat: ChatDTO | None = None
    user: UserDTO | None = None


@runtime_checkable
class MaxAdapter(Protocol):
    """Всё, что нужно инструментам чтения. Запись добавляет тред 3."""

    # жизненный цикл
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    @property
    def state(self) -> AdapterState: ...
    async def me(self) -> UserDTO: ...

    # чтение
    async def list_chats(
        self, *, before_ms: int | None, limit: int
    ) -> tuple[list[ChatDTO], int | None]: ...
    async def get_chats(self, chat_ids: Sequence[int]) -> list[ChatDTO]: ...
    async def list_folders(self) -> list[FolderDTO]: ...
    async def history(
        self, chat_id: int, *, anchor_ms: int, backward: int, forward: int
    ) -> list[MessageDTO]: ...
    async def get_messages(self, chat_id: int, ids: Sequence[int]) -> list[MessageDTO]: ...
    async def comments(
        self, chat_id: int, post_id: int, *, anchor_ms: int | None, backward: int, forward: int
    ) -> list[MessageDTO]: ...
    async def comments_count(self, chat_id: int, post_id: int) -> int | None: ...
    async def members(
        self, chat_id: int, *, marker: int | None, limit: int
    ) -> tuple[list[MemberDTO], int | None]: ...
    async def users(self, ids: Sequence[int]) -> list[UserDTO]: ...
    async def contacts(self) -> list[UserDTO]: ...
    async def resolve_link(self, link: str) -> LinkTarget: ...
    async def reactions(
        self, chat_id: int, message_ids: Sequence[int]
    ) -> dict[int, tuple[tuple[tuple[str, int], ...], str | None]]: ...
    async def media_url(self, chat_id: int, message_id: int, att: AttachmentDTO) -> str: ...
    async def download(self, url: str, dest: Path, *, max_bytes: int) -> MediaBlob: ...
    def events(self) -> EventBuffer: ...

    # единственный метод, меняющий непрочитанное; инструменты чтения его не вызывают
    async def mark_read(self, chat_id: int, message_id: int) -> int: ...
