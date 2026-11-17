"""Контракт записи PyMaxAdapter на фейковом PyMax: сырые payload-ы и обходы багов 2.5.0."""

from __future__ import annotations

import pytest
from pymax.protocol.enums import Opcode
from pymax.session import SessionInfo

from max_mcp_server.adapter import pymax_adapter as pa
from max_mcp_server.adapter.base import OutgoingFile
from max_mcp_server.adapter.raw import forward_payload
from max_mcp_server.errors import NotFound
from max_mcp_server.session.store import SecureFileStore
from tests.fakes.fake_pymax import FakeWriteClient


def test_forward_payload_message_id_is_int():
    p = forward_payload(5, -3, 7, cid=-1, notify=True)
    assert p["message"]["link"] == {"type": "FORWARD", "messageId": 7, "chatId": -3}
    assert isinstance(p["message"]["link"]["messageId"], int)


@pytest.fixture
async def adapter(settings, monkeypatch):
    SecureFileStore(settings.state_path).write(
        pa.session_to_record(SessionInfo(token="SYNTHETIC", device_id="d", phone=""), "web")
    )
    fake = FakeWriteClient()
    monkeypatch.setattr(pa, "build_client", lambda *a, **k: fake)
    a = pa.PyMaxAdapter(settings)
    await a.start()
    a.fake = fake  # type: ignore[attr-defined]
    yield a
    await a.stop()


async def test_send_plain_text_keeps_markdown_chars(adapter):
    app = adapter.fake._app
    msg = await adapter.send(10, "a **b** _c_", markdown=False)
    op, payload = app.invokes[-1]
    assert op == int(Opcode.MSG_SEND)
    assert payload["message"]["text"] == "a **b** _c_" and payload["message"]["elements"] == []
    assert msg.chat_id == 10 and msg.text == "a **b** _c_"
    await adapter.send(10, "a **b**", markdown=True, reply_to=5)
    _, payload = app.invokes[-1]
    assert payload["message"]["text"] == "a b" and payload["message"]["elements"]
    assert payload["message"]["link"] == {"type": "REPLY", "messageId": 5}


async def test_forward_uses_int_message_id(adapter):
    msgs = await adapter.forward(10, -9, [7, 8], notify=False)
    sent = [p for op, p in adapter.fake._app.invokes if op == int(Opcode.MSG_SEND)]
    assert [p["message"]["link"]["messageId"] for p in sent] == [7, 8]
    assert all(p["notify"] is False for p in sent)
    assert msgs[0].forwarded_from is not None and msgs[0].forwarded_from.chat_id == -9


async def test_edit_and_comment_payloads(adapter):
    await adapter.edit(10, 5, "x *y*", markdown=False)
    op, p = adapter.fake._app.invokes[-1]
    assert op == int(Opcode.MSG_EDIT) and p["text"] == "x *y*" and p["messageId"] == 5
    c = await adapter.send_comment(-4, 300, "коммент", reply_to=None, markdown=False)
    op, p = adapter.fake._app.invokes[-1]
    assert p["postId"] == 300 and c.post_id == 300


async def test_poll_only_revote_flag(adapter):
    from pymax.types.domain.attachments.enums import PollFlags

    await adapter.send_poll(10, "Q?", ["a", "b"], revote=True, notify=True)
    poll = adapter.fake.calls[-1][1]["attachments"][0]
    assert poll.settings == PollFlags.FLAG_SETTINGS_REVOTE
    await adapter.send_poll(10, "Q?", ["a", "b"], revote=False, notify=True)
    poll = adapter.fake.calls[-1][1]["attachments"][0]
    assert int(poll.settings) == 0
    res = await adapter.vote_poll(10, 5, [1])
    assert adapter.fake.calls[-1] == ("vote_poll", (10, 5, 55, [1]))
    assert res.total == 1 and res.answers == ((1, 1),)


async def test_vote_without_poll(adapter, monkeypatch):
    async def none(chat_id, ids):
        return []

    monkeypatch.setattr(adapter.fake, "get_messages", none)
    with pytest.raises(NotFound):
        await adapter.vote_poll(10, 5, [1])


async def test_delete_for_everyone_maps_for_me(adapter):
    await adapter.delete(10, [1, 2], for_everyone=False)
    assert adapter.fake.calls[-1] == ("delete_message", (10, [1, 2], True))
    await adapter.delete(10, [1], for_everyone=True)
    assert adapter.fake.calls[-1] == ("delete_message", (10, [1], False))


def test_outgoing_files_become_pymax_bytes():
    files = pa.PyMaxAdapter._pymax_files(
        [
            OutgoingFile(name="a.png", data=b"\x89PNG\r\n\x1a\n", kind="photo"),
            OutgoingFile(name="b.pdf", data=b"%PDF", kind="file"),
        ]
    )
    from pymax.files import File, Photo

    assert isinstance(files[0], Photo) and isinstance(files[1], File)
