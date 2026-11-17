"""Контракт PyMaxAdapter на синтетических JSON-фикстурах (design.md, 9.1, слой 3).

Фикстуры в ``tests/fixtures/pymax/`` повторяют форму кадров MAX (camelCase) и превращаются в
**настоящие** модели ``pymax.types`` через ``model_validate``. Если обновление PyMax поменяет
модели (имена полей, обязательность, enum-ы), эти тесты упадут раньше, чем живой аккаунт.
Все данные синтетические: id маленькие, хосты — ``example`` поддомены CDN.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pymax.session import SessionInfo
from pymax.types import (
    Chat,
    CommentsInfoUpdate,
    FileRequest,
    FolderList,
    Member,
    Message,
    ReactionInfo,
    VideoRequest,
)

from max_mcp_server.adapter import pymax_adapter as pa
from max_mcp_server.errors import InvalidArgument, NotFound
from max_mcp_server.serialization import Serializer
from max_mcp_server.session.store import SecureFileStore
from tests.fakes.fake_pymax import FakePymaxClient

FIX = Path(__file__).parent / "fixtures" / "pymax"
MSGS = json.loads((FIX / "messages.json").read_text(encoding="utf-8"))
CHATS = json.loads((FIX / "chats.json").read_text(encoding="utf-8"))


def msg(key: str) -> Message:
    return Message.model_validate(MSGS[key])


# ---------------------------------------------------------------- чистый маппинг моделей


def test_all_attachment_kinds_map():
    dto = pa.map_message(msg("attachments_message"))
    kinds = [a.kind for a in dto.attachments]
    assert kinds == [
        "photo",
        "video",
        "file",
        "voice",
        "sticker",
        "contact",
        "link",
        "poll",
        "call",
        "unknown",
    ]
    photo, video, file, voice, sticker, contact, link, poll, call, unknown = dto.attachments
    assert photo.downloadable and photo.width == 640 and photo.source["url"].startswith("https://")
    assert video.duration_s == 15 and video.source == {"video_id": 12}
    assert file.name == "report.pdf" and file.size == 2048 and file.source == {"file_id": 13}
    assert voice.duration_s == 4.2 and voice.transcript == "синтетическая расшифровка"
    assert voice.extra["transcription_status"] == "SUCCESS"
    assert sticker.extra == {"sticker_id": 15}
    assert contact.extra["contact_name"] == "Синтетический Контакт"
    assert link.extra["url"] == "https://example.org/"
    assert poll.extra["total_votes"] == 3
    assert [a["votes"] for a in poll.extra["answers"]] == [2, 1]
    assert call.extra == {"call_type": "AUDIO", "hangup": "HUNGUP"} and call.duration_s == 60
    assert unknown.extra == {"type": "SOMETHING_NEW"}
    # сообщение без текста с вложениями получает тип первого вложения; статус/реакции
    assert dto.kind == "photo" and dto.edited and not dto.deleted
    assert dto.reactions == (("👍", 2),) and dto.my_reaction == "👍"


def test_tokens_and_preview_bytes_never_reach_output():
    """Токены вложений и ``preview_data`` (bytes) не попадают в DTO и сериализацию."""
    dto = pa.map_message(msg("attachments_message"))
    text = json.dumps(
        Serializer(wrap_mode="tags", text_limit=4000).message(dto), ensure_ascii=False
    )
    assert "synthetic-" not in text and "previewData" not in text and "\\x" not in text


def test_service_and_removed_messages():
    svc = pa.map_message(msg("service_message"))
    assert svc.kind == "service" and svc.attachments[0].extra["user_ids"] == [3]
    assert pa.map_message(msg("removed_message")).deleted
    # комментарий без chatId получает chat_id из контекста
    assert pa.map_message(msg("comment"), -200).chat_id == -200


def test_chats_map_dialog_saved_group_channel():
    me = 1
    chats = [Chat.model_validate(c) for c in CHATS["chats"]]
    dialog, saved, group, channel = (pa.map_chat(c, me, {2: "Собеседник"}) for c in chats)
    assert dialog.type == "dialog" and dialog.peer_user_id == 2 and dialog.title == "Собеседник"
    assert dialog.unread == 1 and dialog.last_message.text == "привет"
    assert saved.title == "Избранное" and saved.peer_user_id == me
    assert group.type == "group" and group.members_count == 3 and group.admin_ids == (2,)
    assert group.pinned_message.id == 7 and group.messages_count == 42
    assert channel.type == "channel" and channel.link.startswith("https://max.ru/")
    # без известного имени — заглушка по id
    assert pa.map_chat(chats[0], me).title == "id2"


def test_users_have_no_phone_unless_own():
    u = Member.model_validate(CHATS["members"][1]).contact
    dto = pa.map_user(u)
    assert dto.name == "Админ Т" and dto.username == "synthetic_admin" and dto.phone is None
    assert pa.map_user(Member.model_validate(CHATS["members"][2]).contact).is_bot


# ---------------------------------------------------------------- адаптер на фейковом клиенте


class FixtureApp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.invokes: list[tuple[int, dict[str, Any]]] = []

    async def invoke(self, opcode: int, payload: dict[str, Any], timeout: Any = None) -> Any:
        from pymax.protocol.models import InboundFrame

        self.invokes.append((int(opcode), payload))
        return InboundFrame(opcode=int(opcode), payload=self.payload)


class FixtureClient(FakePymaxClient):
    """Отвечает моделями PyMax, построенными из JSON-фикстур."""

    def __init__(self) -> None:
        super().__init__()
        self._app = FixtureApp(CHATS["link_info_user"])
        self.chats = {c["id"]: Chat.model_validate(c) for c in CHATS["chats"]}

    async def get_chats(self, ids: list[int]) -> list[Chat]:
        return [self.chats[i] for i in ids if i in self.chats]

    async def get_chat(self, chat_id: int) -> Chat:
        return self.chats[chat_id]

    async def get_folders(self) -> FolderList:
        return FolderList.model_validate(CHATS["folders"])

    async def get_messages(self, chat_id: int, ids: list[int]) -> list[Message | None]:
        return [msg("attachments_message"), None]

    async def fetch_comments(self, chat_id: int, post_id: int, **kw: Any) -> list[Message]:
        self.calls.append(("fetch_comments", kw))
        return [msg("comment")]

    async def get_comments_info(self, chat_id: int, post_ids: list[int]) -> list[Any]:
        return [CommentsInfoUpdate.model_validate(x) for x in CHATS["comments_info"]]

    async def get_chat_members(
        self, chat_id: int, marker: int | None = None, count: int = 50
    ) -> tuple[list[Member], int]:
        return [Member.model_validate(m) for m in CHATS["members"]], 3

    async def get_reactions(self, chat_id: int, ids: list[int]) -> dict[str, ReactionInfo]:
        return {k: ReactionInfo.model_validate(v) for k, v in CHATS["reactions"].items()}

    async def get_file_by_id(self, chat_id: int, message_id: int, file_id: int) -> FileRequest:
        return FileRequest.model_validate(CHATS["file_request"])

    async def get_video_by_id(self, chat_id: int, message_id: int, video_id: int) -> VideoRequest:
        return VideoRequest.model_validate(CHATS["video_request"])

    async def resolve_group_by_link(self, link: str) -> Chat | None:
        return self.chats[-100] if link.endswith("join/ok") else None


@pytest.fixture
async def adapter(settings, monkeypatch):
    SecureFileStore(settings.state_path).write(
        pa.session_to_record(SessionInfo(token="SYNTHETIC", device_id="d", phone=""), "web")
    )
    client = FixtureClient()
    monkeypatch.setattr(pa, "build_client", lambda *a, **k: client)
    a = pa.PyMaxAdapter(settings)
    await a.start()
    a.client = client  # type: ignore[attr-defined]
    yield a
    await a.stop()


async def test_adapter_chats_folders_messages(adapter):
    chats = await adapter.get_chats([-100, -200, 999])
    assert [c.type for c in chats] == ["group", "channel"]
    folders = await adapter.list_folders()
    assert [f.id for f in folders] == ["f2", "f1"]  # порядок из foldersOrder
    assert folders[0].chat_ids == (10,) and folders[0].filters == ("UNREAD",)
    got = await adapter.get_messages(-100, [501, 404])
    assert [m.id for m in got] == [501]  # None от MAX отбрасывается


async def test_adapter_comments(adapter):
    comments = await adapter.comments(-200, 77, anchor_ms=None, backward=0, forward=10)
    assert comments[0].post_id == 77 and comments[0].chat_id == -200
    assert await adapter.comments_count(-200, 77) == 5
    assert await adapter.comments_count(-200, 78) is None


async def test_adapter_members_admins_and_presence(adapter):
    members, nxt = await adapter.members(-100, marker=None, limit=50)
    owner, admin, bot = members
    assert nxt == 3
    assert owner.is_owner and owner.is_admin and owner.last_seen_ms == 1735689000000
    assert admin.is_admin and not admin.is_owner and admin.last_seen_ms == 1735689000000
    assert not bot.is_admin and bot.last_seen_ms is None and bot.user.is_bot


async def test_adapter_reactions_and_users(adapter):
    assert await adapter.reactions(-100, [501]) == {501: ((("🔥", 3),), None)}
    users = await adapter.users([2])
    assert users[0].name == "Алиса Т"


async def test_adapter_media_url_per_kind(adapter):
    atts = pa.map_message(msg("attachments_message")).attachments
    photo, video, file, voice, sticker = atts[:5]
    assert (await adapter.media_url(-100, 501, photo)).startswith("https://i.example")
    assert (await adapter.media_url(-100, 501, voice)).startswith("https://audio.example")
    assert (await adapter.media_url(-100, 501, file)).startswith("https://files.example")
    # у видео url=None → берём https-ссылку качества из model_extra
    assert (await adapter.media_url(-100, 501, video)).startswith("https://video.example")
    with pytest.raises(InvalidArgument):
        await adapter.media_url(-100, 501, sticker)
    from dataclasses import replace

    with pytest.raises(NotFound):
        await adapter.media_url(-100, 501, replace(photo, source={"url": None}))


async def test_adapter_resolve_link(adapter):
    user = await adapter.resolve_link("@synthetic_user")
    assert user.user is not None and user.user.username == "synthetic_user" and user.chat is None
    _op, payload = adapter.client._app.invokes[-1]
    assert payload == {"link": "https://max.ru/synthetic_user"}
    adapter.client._app.payload = CHATS["link_info_chat"]
    chan = await adapter.resolve_link("https://max.ru/public")
    assert chan.chat is not None and chan.chat.type == "channel"
    adapter.client._app.payload = {}
    assert (await adapter.resolve_link("https://max.ru/nobody")).chat is None
    invite = await adapter.resolve_link("https://max.ru/join/ok")
    assert invite.chat.id == -100
    assert (await adapter.resolve_link("https://max.ru/join/bad")).chat is None
