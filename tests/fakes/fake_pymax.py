"""Фейковый клиент PyMax (тот же интерфейс, настоящие модели pymax.types, без сети)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pymax.types import Chat, Message, Profile, User

ME_JSON = {"id": 1, "names": [{"name": "Тест Владелец", "type": "ONEME"}], "phone": 70000000000}


def make_message(i: int, chat_id: int = -9, **extra: Any) -> Message:
    return Message.model_validate(
        {
            "id": i,
            "chatId": chat_id,
            "sender": 2,
            "text": f"m{i}",
            "time": 1_700_000_000_000 + i * 1000,
            "type": "USER",
            **extra,
        }
    )


class FakePymaxClient:
    def __init__(self, *, fail_with: BaseException | None = None) -> None:
        self.handlers: dict[str, list[Callable[..., Any]]] = {}
        self.me = Profile.model_validate({"contact": ME_JSON})
        self.contacts = [
            User.model_validate({"id": 2, "names": [{"firstName": "Алиса", "lastName": "Т"}]})
        ]
        self.fail_with = fail_with
        self.closed = asyncio.Event()
        self.calls: list[tuple[str, Any]] = []

    def _reg(self, name: str) -> Callable[..., Any]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.handlers.setdefault(name, []).append(fn)
            return fn

        return lambda *filters: deco

    def __getattr__(self, name: str) -> Any:
        if name.startswith("on_"):
            return self._reg(name)
        raise AttributeError(name)

    async def start(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        for h in self.handlers.get("on_start", []):
            await h(self)
        await self.closed.wait()

    async def close(self) -> None:
        self.closed.set()

    async def fetch_history(self, chat_id: int, **kw: Any) -> list[Message]:
        self.calls.append(("fetch_history", kw))
        return [make_message(i, chat_id) for i in range(3)]

    async def fetch_chats(self, marker: int | None = None) -> list[Chat]:
        self.calls.append(("fetch_chats", marker))
        return [
            Chat.model_validate(
                {
                    "id": 10,
                    "type": "DIALOG",
                    "status": "ACTIVE",
                    "owner": 1,
                    "participants": {"1": 0, "2": 0},
                    "lastEventTime": 5,
                    "newMessages": 2,
                }
            ),
            Chat.model_validate(
                {
                    "id": -9,
                    "type": "CHAT",
                    "status": "ACTIVE",
                    "owner": 1,
                    "title": "Группа",
                    "lastEventTime": 7,
                    "participantsCount": 3,
                }
            ),
        ]

    async def get_users(self, ids: list[int]) -> list[User]:
        self.calls.append(("get_users", ids))
        return [u for u in self.contacts if u.id in ids]

    async def read_message(self, message_id: int, chat_id: int) -> Any:
        raise AssertionError("read_message must not be called by read paths")
