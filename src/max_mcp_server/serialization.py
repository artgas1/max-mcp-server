"""DTO → JSON-совместимые dict для ответов MCP.

Правила (design.md, раздел 5): только белый список полей; никаких bytes, токенов, base_url и
CDN-ссылок; всё, что написали другие люди, — через ``security.untrusted.wrap``; время ISO 8601.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any

from max_mcp_server.adapter.base import (
    AttachmentDTO,
    ChatDTO,
    EventDTO,
    FolderDTO,
    ForwardDTO,
    MemberDTO,
    MessageDTO,
    UserDTO,
)
from max_mcp_server.security.untrusted import WrapMode, normalize_text, truncate, wrap

ATT_PREFIX = "max-att"
# какие ключи AttachmentDTO.extra можно выводить и являются ли они недоверенным текстом
_EXTRA_FIELDS: dict[str, bool] = {
    "title": True,
    "description": True,
    "text": True,
    "first_name": True,
    "last_name": True,
    "contact_name": True,
    "event": False,
    "type": False,
    "url": True,
    "call_type": False,
    "hangup": False,
    "total_votes": False,
    "contact_id": False,
    "user_ids": False,
    "sticker_id": False,
    "transcription_status": False,
    "buttons": True,
}


def scrub(value: Any) -> Any:
    """Страховочный обход: bytes → {"bytes": n}, неизвестные типы → str."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, bytes | bytearray | memoryview):
        return {"bytes": len(value)}
    if isinstance(value, Mapping):
        return {str(k): scrub(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [scrub(v) for v in value]
    return str(value)


def make_ref(chat_id: int, message_id: int, index: int) -> str:
    return f"{ATT_PREFIX}:{chat_id}:{message_id}:{index}"


def parse_ref(ref: str) -> tuple[int, int, int]:
    from max_mcp_server.errors import InvalidArgument

    parts = ref.strip().split(":")
    if len(parts) != 4 or parts[0] != ATT_PREFIX:
        raise InvalidArgument("ref должен иметь вид max-att:<chat_id>:<message_id>:<index>")
    try:
        return int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError as exc:
        raise InvalidArgument("ref: chat_id, message_id и index должны быть числами") from exc


@dataclass
class Serializer:
    wrap_mode: WrapMode = "tags"
    text_limit: int = 4000
    tz: tzinfo | None = None
    me_id: int | None = None
    names: dict[int, str] = field(default_factory=dict)

    # --- примитивы -------------------------------------------------------------

    def u(self, value: str | None, source: str | None = None) -> Any:
        return wrap(value, self.wrap_mode, source=source)

    def iso(self, ms: int | None) -> str | None:
        if ms is None or ms <= 0:
            return None
        return datetime.fromtimestamp(ms / 1000, tz=self.tz).isoformat(timespec="seconds")

    def short_time(self, ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=self.tz).strftime("%Y-%m-%d %H:%M")

    # --- пользователи и чаты --------------------------------------------------

    def user(self, u: UserDTO, *, show_phone: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": u.id,
            "name": self.u(u.name),
            "username": u.username,
            "is_bot": u.is_bot,
            "is_me": u.id == self.me_id if self.me_id is not None else None,
        }
        if u.description:
            out["description"] = self.u(truncate(normalize_text(u.description), 1000)[0])
        if show_phone and u.phone:
            out["phone"] = u.phone
        return out

    def member(self, m: MemberDTO) -> dict[str, Any]:
        out = self.user(m.user)
        out.update(is_admin=m.is_admin, is_owner=m.is_owner, last_seen=self.iso(m.last_seen_ms))
        return out

    def sender(self, sender_id: int | None) -> dict[str, Any] | None:
        if sender_id is None:
            return None
        name = self.names.get(sender_id)
        return {
            "id": sender_id,
            "name": self.u(name) if name else None,
            "is_me": sender_id == self.me_id,
        }

    def chat(self, c: ChatDTO, *, detailed: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": c.id,
            "type": c.type,
            "title": self.u(c.title),
            "unread": c.unread,
            "members_count": c.members_count,
            "last_event": self.iso(c.last_event_at),
        }
        if c.peer_user_id is not None:
            out["peer_user_id"] = c.peer_user_id
        if c.last_message is not None:
            out["last_message"] = self.message_brief(c.last_message)
        if detailed:
            out.update(
                description=self.u(c.description) if c.description else None,
                link=c.link,
                owner_id=c.owner_id,
                admin_ids=list(c.admin_ids),
                messages_count=c.messages_count,
                created=self.iso(c.created_ms),
                pinned_message=self.message(c.pinned_message) if c.pinned_message else None,
            )
        return out

    def folder(self, f: FolderDTO) -> dict[str, Any]:
        return {
            "id": f.id,
            "title": self.u(f.title),
            "chat_ids": list(f.chat_ids),
            "filters": list(f.filters),
        }

    # --- сообщения -------------------------------------------------------------

    def _text(self, m: MessageDTO) -> tuple[Any, bool]:
        text, cut = truncate(normalize_text(m.text), self.text_limit)
        if not text:
            return None, False
        return self.u(text, source=f"max:msg:{m.chat_id}:{m.id}"), cut

    def attachment(self, a: AttachmentDTO, m: MessageDTO | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": a.kind}
        if m is not None and a.downloadable and m.post_id is None:
            out["ref"] = make_ref(m.chat_id, m.id, a.index)
        if a.name is not None:
            out["name"] = self.u(a.name)
        for key in ("size", "mime", "width", "height", "duration_s"):
            val = getattr(a, key)
            if val is not None:
                out[key] = val
        if a.kind in ("photo", "video", "file", "voice"):
            out["downloadable"] = a.downloadable
        if a.transcript:
            out["transcript"] = self.u(truncate(normalize_text(a.transcript), self.text_limit)[0])
        for key, val in a.extra.items():
            if key not in _EXTRA_FIELDS or val is None:
                continue
            if key == "answers" or isinstance(val, list | tuple):
                continue
            if _EXTRA_FIELDS[key] and isinstance(val, str):
                limit = 500 if key == "url" else 1000
                out[key] = self.u(truncate(normalize_text(val), limit)[0])
            else:
                out[key] = scrub(val)
        answers = a.extra.get("answers")
        if isinstance(answers, list | tuple):
            out["answers"] = [
                {
                    "id": ans.get("id"),
                    "text": self.u(str(ans.get("text") or "")),
                    "votes": ans.get("votes"),
                }
                for ans in answers
                if isinstance(ans, Mapping)
            ]
        return out

    def forward(self, f: ForwardDTO) -> dict[str, Any]:
        text, _ = truncate(normalize_text(f.text), self.text_limit)
        return {
            "chat_id": f.chat_id,
            "chat_title": self.u(f.chat_title) if f.chat_title else None,
            "message_id": f.message_id,
            "sender": self.sender(f.sender_id),
            "time": self.iso(f.time_ms),
            "text": self.u(text) if text else None,
            "attachments": list(f.attachment_kinds),
        }

    def message(self, m: MessageDTO) -> dict[str, Any]:
        text, cut = self._text(m)
        out: dict[str, Any] = {
            "id": m.id,
            "chat_id": m.chat_id,
            "time": self.iso(m.time_ms),
            "time_ms": m.time_ms,
            "sender": self.sender(m.sender_id),
            "kind": m.kind,
            "text": text,
        }
        if cut:
            out["truncated"] = True
        if m.post_id is not None:
            out["post_id"] = m.post_id
        if m.reply_to is not None:
            out["reply_to"] = m.reply_to
        if m.forwarded_from is not None:
            out["forwarded_from"] = self.forward(m.forwarded_from)
        if m.edited:
            out["edited"] = True
        if m.deleted:
            out["deleted"] = True
        if m.attachments:
            out["attachments"] = [self.attachment(a, m) for a in m.attachments]
        if m.reactions:
            out["reactions"] = [[r, n] for r, n in m.reactions]
        if m.my_reaction:
            out["my_reaction"] = m.my_reaction
        return scrub(out)

    def message_brief(self, m: MessageDTO) -> dict[str, Any]:
        text, _ = truncate(normalize_text(m.text), 200)
        return {
            "id": m.id,
            "time": self.iso(m.time_ms),
            "sender_id": m.sender_id,
            "text": self.u(text) if text else None,
            "attachments": [a.kind for a in m.attachments],
        }

    @staticmethod
    def _one_line(value: str) -> str:
        """Текст в одну строку: все переводы строк (в том числе U+2028/U+2029, см.
        ``normalize_text``) → ``⏎``, чтобы чужой текст не начинал «новую строку» страницы."""
        return normalize_text(value).replace("\n", " ⏎ ").replace("\t", " ")

    def compact_parts(self, m: MessageDTO) -> tuple[str, str]:
        """(служебный заголовок, недоверенное тело) одной строки compact-формата.

        Заголовок строит только сервер: время, ``#id`` и автор — ``[me]`` для владельца или
        ``[id<sender_id>]``. Имя автора и всё, что написали люди, — только в теле. Тело потом
        оборачивается отдельно для каждого сообщения, поэтому подделать заголовок (и «строку
        от владельца») из имени или текста нельзя (M1, security-review)."""
        mine = m.sender_id is not None and m.sender_id == self.me_id
        who = "[me]" if mine else (f"[id{m.sender_id}]" if m.sender_id is not None else "[?]")
        head = [f"[{self.short_time(m.time_ms)}] #{m.id} {who}"]
        if m.reply_to is not None:
            head.append(f"[↩ #{m.reply_to}]")
        if m.attachments:
            head.append("[📎 " + ", ".join(a.kind for a in m.attachments) + "]")
        body: list[str] = []
        if not mine:
            name = self.names.get(m.sender_id or 0)
            if name:
                body.append(truncate(self._one_line(name), 64)[0] + ":")
        text, _ = truncate(self._one_line(m.text), self.text_limit)
        if text:
            body.append(text)
        if m.forwarded_from is not None:
            fwd, _ = truncate(self._one_line(m.forwarded_from.text), 300)
            body.append(f"[↪ переслано: {fwd}]")
        return " ".join(head), " ".join(body)

    def compact_line(self, m: MessageDTO) -> str:
        head, body = self.compact_parts(m)
        if not body:
            return head
        if self.wrap_mode == "off":
            return f"{head} {body}"
        return f"{head} {wrap(body, 'tags')}"

    def compact(self, messages: Sequence[MessageDTO]) -> Any:
        """Строка на сообщение; у каждой своя untrusted-обёртка, заголовок — вне её.

        ``wrap_mode=json`` → список ``{"h": заголовок, "untrusted": тело}``."""
        if self.wrap_mode == "json":
            out: list[dict[str, Any]] = []
            for m in messages:
                head, body = self.compact_parts(m)
                item: dict[str, Any] = {"h": head}
                if body:
                    item["untrusted"] = normalize_text(body)
                out.append(item)
            return out
        return "\n".join(self.compact_line(m) for m in messages)

    def event(self, e: EventDTO) -> dict[str, Any]:
        out: dict[str, Any] = {
            "seq": e.seq,
            "type": e.type,
            "chat_id": e.chat_id,
            "time": self.iso(e.time_ms),
        }
        if e.message is not None:
            out["message"] = self.message(e.message)
        if e.data:
            out["data"] = scrub(dict(e.data))
        return out
