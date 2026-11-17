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
