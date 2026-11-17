"""Политика доступа к чатам. Тред 2: чтение и deny-list; запись дополняет тред 3."""

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

    def can_write(self, chat_id: int) -> bool:
        if self.read_only or chat_id in self.deny:
            return False
        if self.write_allow == "*":
            return True
        return chat_id in self.write_allow

    def summary(self) -> dict[str, object]:
        """Для max_get_me: только счётчики, без самих id."""
        return {
            "read_only": self.read_only,
            "read_allowlist": len(self.read_allow) if self.read_allow else "all",
            "write_allowlist": "all" if self.write_allow == "*" else len(self.write_allow),
            "deny_list": len(self.deny),
        }
