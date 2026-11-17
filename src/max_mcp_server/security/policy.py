"""Политика доступа к чатам: read/write allowlists и deny-list (design.md, 8.2).

Правила:
- deny-list сильнее всего: чат из ``MAX_DENY_CHATS`` нельзя ни читать, ни писать;
- read-allowlist пуст → читать можно все чаты (кроме deny);
- write-allowlist пуст → писать нельзя **никуда**, даже при ``MAX_READ_ONLY=false``;
  ``*`` — явное «все чаты» (кроме deny);
- действия без существующего чата (создание группы) требуют отдельного разрешения:
  ``MAX_ALLOW_CREATE_GROUP=true`` или ``MAX_WRITE_CHATS=*`` (пустой allowlist их тоже запрещает);
- запись в чат требует, чтобы он был и читаемым: иначе модель могла бы писать в чат, который
  ей не разрешено видеть (и, например, удалять там сообщения вслепую).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal, TypeVar

from max_mcp_server.errors import Forbidden

T = TypeVar("T")


@dataclass(frozen=True)
class Policy:
    read_only: bool = True
    read_allow: frozenset[int] = field(default_factory=frozenset)  # пусто = все
    write_allow: frozenset[int] | Literal["*"] = field(default_factory=frozenset)  # пусто = никакие
    deny: frozenset[int] = field(default_factory=frozenset)
    allow_join: bool = False
    allow_create_group: bool = False

    # ----------------------------------------------------------------- чтение

    def can_read(self, chat_id: int | None) -> bool:
        if chat_id is None:
            # событие без chat_id (например, присутствие) — пропускаем только без allowlist
            return not self.read_allow
        if chat_id in self.deny:
            return False
        return not self.read_allow or chat_id in self.read_allow

    def check_read(self, chat_id: int) -> None:
        if not self.can_read(chat_id):
            raise Forbidden(
                f"Чат {chat_id} недоступен для чтения (не входит в MAX_READ_CHATS или в MAX_DENY_CHATS)"
            )

    def filter_readable(self, items: Iterable[T], key: str = "id") -> list[T]:
        return [it for it in items if self.can_read(getattr(it, key))]

    # ----------------------------------------------------------------- запись

    @property
    def write_enabled(self) -> bool:
        """Есть ли вообще чаты, куда можно писать (для подсказок в ошибках/статусе)."""
        return not self.read_only and (self.write_allow == "*" or bool(self.write_allow))

    def can_write(self, chat_id: int) -> bool:
        if self.read_only or chat_id in self.deny or not self.can_read(chat_id):
            return False
        if self.write_allow == "*":
            return True
        return chat_id in self.write_allow

    def check_write(self, chat_id: int) -> None:
        if self.read_only:
            raise Forbidden("Сервер работает только на чтение (MAX_READ_ONLY=true)")
        if not self.can_write(chat_id):
            hint = (
                "write-allowlist пуст: задайте MAX_WRITE_CHATS=<id,...>"
                if self.write_allow != "*" and not self.write_allow
                else "добавьте его в MAX_WRITE_CHATS (и уберите из MAX_DENY_CHATS)"
            )
            raise Forbidden(f"Запись в чат {chat_id} запрещена политикой: {hint}")

    def check_forward(self, from_chat_id: int, to_chat_id: int) -> None:
        """Пересылка: источник должен быть читаемым, цель — разрешённой для записи."""
        self.check_read(from_chat_id)
        self.check_write(to_chat_id)

    def check_join(self) -> None:
        if self.read_only:
            raise Forbidden("Сервер работает только на чтение (MAX_READ_ONLY=true)")
        if not self.allow_join:
            raise Forbidden("Вступление в чаты выключено (MAX_ALLOW_JOIN=false)")

    @property
    def can_create_chats(self) -> bool:
        return not self.read_only and (self.allow_create_group or self.write_allow == "*")

    def check_any_write(self) -> None:
        """Для действий без существующего чата (создание группы): нужен явный
        ``MAX_ALLOW_CREATE_GROUP=true`` или ``MAX_WRITE_CHATS=*``."""
        if self.read_only:
            raise Forbidden("Сервер работает только на чтение (MAX_READ_ONLY=true)")
        if not self.can_create_chats:
            raise Forbidden(
                "Создание групп выключено: задайте MAX_ALLOW_CREATE_GROUP=true "
                "(или MAX_WRITE_CHATS=*)"
            )

    def summary(self) -> dict[str, object]:
        """Для max_get_me: только счётчики, без самих id."""
        return {
            "read_only": self.read_only,
            "read_allowlist": len(self.read_allow) if self.read_allow else "all",
            "write_allowlist": "all" if self.write_allow == "*" else len(self.write_allow),
            "deny_list": len(self.deny),
            "allow_join": self.allow_join,
            "allow_create_group": self.can_create_chats,
        }
