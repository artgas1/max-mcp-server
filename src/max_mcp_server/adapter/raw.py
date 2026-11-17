"""Сырые опкоды MAX для пробелов PyMax 2.5.0 (design.md, 1.10).

Всё здесь НЕ подтверждено на живом аккаунте — см. docs/protocol-notes.md. Каждая функция
имеет безопасный фолбэк в вызывающем коде.
"""

from __future__ import annotations

from typing import Any

from pymax.protocol.enums import Opcode

# Подтверждённые в PyMax 2.5.0 опкоды, которые мы используем сырым invoke.
LINK_INFO = int(Opcode.LINK_INFO)
# Кандидаты на будущее (payload неизвестен): MSG_SEARCH, CONTACT_LIST, TRANSCRIBE_MEDIA.
MSG_SEARCH = int(Opcode.MSG_SEARCH)
CONTACT_LIST = int(Opcode.CONTACT_LIST)
TRANSCRIBE_MEDIA = int(Opcode.TRANSCRIBE_MEDIA)


def normalize_public_link(link: str) -> str:
    """``@nick`` / ``max.ru/nick`` → ``https://max.ru/nick``. Остальное — как есть."""
    link = link.strip()
    if link.startswith("@"):
        return "https://max.ru/" + link[1:]
    if link.startswith("max.ru/"):
        return "https://" + link
    return link


async def link_info(client: Any, link: str) -> dict[str, Any]:
    """``LINK_INFO`` для публичной ссылки (не invite). Возвращает сырой payload ответа.

    PyMax 2.5.0 вызывает этот опкод только для invite-ссылок (``join/…``); формат для
    публичных каналов/ников — предположение (тот же ``{"link": …}``).
    """
    frame = await client._app.invoke(LINK_INFO, {"link": normalize_public_link(link)})
    payload = frame.payload
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------- запись (тред 3)
#
# PyMax 2.5.0 всегда прогоняет текст через свой markdown-парсер (без экранирования) и шлёт
# ``ForwardLink.message_id`` строкой (MAX отвечает «Expected number»). Поэтому отправку, правку
# и пересылку собираем здесь сами из моделей payload PyMax (``pymax.api.messages.payloads``), а
# загрузку вложений и cid берём у его ``MessageService``.


def _service(client: Any) -> Any:
    return client._app.api.messages


def _message_from(payload: Any, chat_id: int) -> Any:
    from pymax.types import Message

    if not isinstance(payload, dict) or not payload:
        raise ValueError("MAX вернул пустой ответ на отправку")
    msg = Message.model_validate(payload)
    if msg.chat_id is None:
        msg.chat_id = chat_id
    return msg


async def _invoke_send(client: Any, opcode: int, payload: dict[str, Any], attaches: Any) -> Any:
    from pymax.exceptions import ApiError

    try:
        return await client._app.invoke(opcode, payload)
    except ApiError as exc:
        # голосовые/видео обрабатываются сервером асинхронно — ждём сигнала, как PyMax
        if exc.error == "attachment.not.ready":
            await _service(client)._process_attachment_error(attaches)
            return await client._app.invoke(opcode, payload)
        raise


async def send_message(
    client: Any,
    chat_id: int,
    text: str | None,
    *,
    reply_to: int | None = None,
    attachments: Any = None,
    notify: bool = True,
    markdown: bool = False,
    post_id: int | None = None,
) -> Any:
    """``MSG_SEND`` (сообщение или комментарий к посту). ``markdown=False`` — текст как есть."""
    from pymax.api.messages.payloads import (
        ReplyLink,
        SendCommentPayload,
        SendMessagePayload,
        SendMessagePayloadMessage,
    )
    from pymax.protocol.enums import Opcode

    svc = _service(client)
    if markdown:
        message, attaches = await svc._prepare_message(text, reply_to, attachments)
    else:
        if not text and not attachments:
            raise ValueError("нужен текст или вложение")
        attaches = await svc._upload_attachments(attachments)
        message = SendMessagePayloadMessage(
            text=text or None,
            cid=svc._next_cid(),
            elements=[],
            attaches=attaches,
            link=ReplyLink(message_id=int(reply_to)) if reply_to else None,
        )
    frame: Any
    if post_id is not None:
        frame = SendCommentPayload(
            chat_id=chat_id, post_id=int(post_id), message=message, notify=notify
        )
    else:
        frame = SendMessagePayload(chat_id=chat_id, message=message, notify=notify)
    resp = await _invoke_send(client, int(Opcode.MSG_SEND), frame.to_payload(), attaches)
    return _message_from(resp.payload, chat_id)


async def edit_message(
    client: Any, chat_id: int, message_id: int, text: str, *, markdown: bool = False
) -> Any:
    from pymax.api.messages.payloads import EditMessagePayload
    from pymax.formatting.markdown import Formatter
    from pymax.protocol.enums import Opcode

    if markdown:
        clean, elements = Formatter.format_markdown(text)
    else:
        clean, elements = text, []
    frame = EditMessagePayload(
        chat_id=chat_id, message_id=int(message_id), text=clean, elements=elements
    )
    resp = await client._app.invoke(int(Opcode.MSG_EDIT), frame.to_payload())
    return _message_from(resp.payload, chat_id)


def forward_payload(
    to_chat_id: int, from_chat_id: int, message_id: int, *, cid: int, notify: bool
) -> dict[str, Any]:
    """Payload пересылки с ``messageId`` числом (обход бага PyMax 2.5.0, design.md 2.2)."""
    from pymax.api.messages.payloads import (
        ForwardLink,
        ForwardMessagePayload,
        ForwardMessagePayloadMessage,
    )

    payload = ForwardMessagePayload(
        chat_id=to_chat_id,
        message=ForwardMessagePayloadMessage(
            cid=cid, link=ForwardLink(message_id=str(message_id), chat_id=from_chat_id)
        ),
        notify=notify,
    ).to_payload()
    payload["message"]["link"]["messageId"] = int(message_id)
    return payload


async def forward_message(
    client: Any, to_chat_id: int, from_chat_id: int, message_id: int, *, notify: bool = True
) -> Any:
    from pymax.protocol.enums import Opcode

    svc = _service(client)
    payload = forward_payload(
        to_chat_id, from_chat_id, message_id, cid=-svc._next_cid(), notify=notify
    )
    resp = await client._app.invoke(int(Opcode.MSG_SEND), payload)
    return _message_from(resp.payload, to_chat_id)
