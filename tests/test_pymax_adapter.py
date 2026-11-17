"""Контракт PyMaxAdapter: безопасные флаги PyMax, маппинг настоящих моделей, ошибки, жизненный цикл."""

from __future__ import annotations

import asyncio

import pytest
from pymax.exceptions import ApiError
from pymax.protocol.enums import Opcode
from pymax.session import SessionInfo
from pymax.versions.catalog import VersionCatalog

from max_mcp_server.adapter import pymax_adapter as pa
from max_mcp_server.errors import (
    Forbidden,
    InvalidArgument,
    NotFound,
    NotLoggedIn,
    RateLimited,
    SessionExpired,
    UpstreamError,
)
from max_mcp_server.session.store import SecureFileStore
from tests.fakes.fake_pymax import FakePymaxClient, make_message


def test_extra_config_safe_flags(settings):
    extra = pa.build_extra_config(settings, pa.PendingStore())
    assert extra.telemetry is False and extra.relogin is False and extra.reconnect is False
    assert extra.persist_session is True and extra.password_max_attempts == 3
    assert extra.log_level == "WARNING"


async def test_web_client_config_quiet(settings):
    client = pa.build_client(settings, "web", pa.PendingStore())
    assert isinstance(client, pa.QuietWebClient)
    cfg = await client._prepare_config()
    assert cfg.interactive is False and cfg.telemetry is False and cfg.relogin is False
    online = pa.build_client(
        settings.model_copy(update={"appear_online": True}), "web", pa.PendingStore()
    )
    assert (await online._prepare_config()).interactive is True


async def test_mobile_client_uses_builtin_catalog(settings):
    client = pa.build_client(settings, "mobile", pa.PendingStore(), phone="+70000000000")
    assert isinstance(client, pa.QuietMobileClient)
    assert isinstance(client.catalog, VersionCatalog) and client.catalog.remote is False
    cfg = await client._prepare_config()  # встроенный каталог — без сети
    assert cfg.telemetry is False and cfg.interactive is False


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ApiError(opcode=int(Opcode.LOGIN), error="FAIL_LOGIN_TOKEN"), SessionExpired),
        (ApiError(opcode=64, error="too.many.requests"), RateLimited),
        (ApiError(opcode=64, error="flood.wait"), RateLimited),
        (ApiError(opcode=48, error="chat.not.found"), NotFound),
        (ApiError(opcode=48, error="access.denied"), Forbidden),
        (ApiError(opcode=48, error="weird", payload={"secret": "x"}), UpstreamError),
        (TimeoutError(), UpstreamError),
        (ConnectionResetError(), UpstreamError),
        (ValueError("Invalid group link"), InvalidArgument),
    ],
)
def test_translate_error(exc, expected):
    err = pa.translate_error(exc)
    assert isinstance(err, expected)
    assert "secret" not in err.public_message


def test_session_record_roundtrip(settings):
    ua = pa.build_extra_config(settings, None).generate_web_user_agent()
    info = SessionInfo(
        token="SYNTHETIC", device_id="d", phone="", mt_instance_id="m", user_agent=ua
    )
    record = pa.session_to_record(info, "web")
    back, kind = pa.record_to_session(record)
    assert kind == "web" and back.token == "SYNTHETIC" and back.user_agent == ua
    with pytest.raises(NotLoggedIn):
        pa.record_to_session({"session": {"nope": 1}})


async def test_file_store_bridge_persists_token(settings):
    store = SecureFileStore(settings.state_path)
    with pytest.raises(NotLoggedIn):
        pa.FileStoreBridge(store, "web")
    store.write(pa.session_to_record(SessionInfo(token="A", device_id="d", phone=""), "web"))
    bridge = pa.FileStoreBridge(store, "web")
    await bridge.update_token("A", "B")
    assert store.read()["session"]["token"] == "B"
    await bridge.delete_session("B")  # serve не удаляет сессию сам
    assert store.exists()


def test_map_message_reply_forward_and_chat():
    reply = make_message(
        5,
        link={"type": "REPLY", "chatId": -9, "message": make_message(4).model_dump(by_alias=True)},
    )
    assert pa.map_message(reply).reply_to == 4
    fwd = make_message(
        6,
        text="",
        link={
            "type": "FORWARD",
            "chatId": -1,
            "chatName": "Канал",
            "message": make_message(3, -1).model_dump(by_alias=True),
        },
    )
    dto = pa.map_message(fwd)
    assert dto.kind == "forward" and dto.forwarded_from.chat_title == "Канал"
    ev = make_message(7)
    ev.chat_id = None
    assert pa.map_message(ev, 42).chat_id == 42


def test_check_download_url():
    sfx = ("oneme.ru", "okcdn.ru")
    assert pa.check_download_url("https://i.oneme.ru/x", sfx) == "i.oneme.ru"
    for bad in (
        "http://i.oneme.ru/x",
        "https://evil.com/x",
        "https://oneme.ru.evil.com/",
        "file:///etc/passwd",
    ):
        with pytest.raises(Forbidden):
            pa.check_download_url(bad, sfx)


def test_sniff_mime():
    assert pa.sniff_mime(b"\x89PNG\r\n\x1a\nxxxx") == "image/png"
    assert pa.sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert pa.sniff_mime(b"????", "text/plain") == "text/plain"


async def test_start_without_session_no_network(settings):
    adapter = pa.PyMaxAdapter(settings)
    with pytest.raises(NotLoggedIn):
        await adapter.start()
    assert adapter.state == "expired"
    with pytest.raises(NotLoggedIn):
        await adapter.history(1, anchor_ms=1, backward=1, forward=0)


def _with_session(settings):
    SecureFileStore(settings.state_path).write(
        pa.session_to_record(SessionInfo(token="SYNTHETIC", device_id="d", phone=""), "web")
    )


async def test_lifecycle_with_fake_pymax(settings, monkeypatch):
    _with_session(settings)
    fake = FakePymaxClient()
    monkeypatch.setattr(pa, "build_client", lambda *a, **k: fake)
    adapter = pa.PyMaxAdapter(settings)
    await adapter.start()
    try:
        assert adapter.state == "ready"
        me = await adapter.me()
        assert me.id == 1 and me.phone == "+70000000000"
        msgs = await adapter.history(-9, anchor_ms=0, backward=10, forward=0)
        assert [m.id for m in msgs] == [0, 1, 2]
        kw = fake.calls[-1][1]
        assert kw["interactive"] is False and kw["from_time"] == 1 and kw["backward"] == 10
        chats, nxt = await adapter.list_chats(before_ms=None, limit=50)
        assert [c.id for c in chats] == [-9, 10] and nxt == 5
        assert chats[1].title == "Алиса Т" and chats[1].peer_user_id == 2 and chats[1].unread == 2
        # события из обработчиков PyMax попадают в буфер
        for h in fake.handlers["on_message"]:
            await h(make_message(99), fake)
        batch = await adapter.events().wait(0, timeout=0)
        assert batch.events[0].message.id == 99
        # второй процесс на тот же профиль не стартует
        from max_mcp_server.errors import ProfileLocked

        with pytest.raises(ProfileLocked):
            await pa.PyMaxAdapter(settings).start()
    finally:
        await adapter.stop()
    assert adapter.state == "stopped"


async def test_revoked_session_marks_expired(settings, monkeypatch):
    _with_session(settings)
    monkeypatch.setattr(
        pa,
        "build_client",
        lambda *a, **k: FakePymaxClient(
            fail_with=ApiError(opcode=int(Opcode.LOGIN), error="FAIL_LOGIN_TOKEN")
        ),
    )
    adapter = pa.PyMaxAdapter(settings)
    with pytest.raises(SessionExpired):
        await adapter.start()
    assert adapter.state == "expired"
    with pytest.raises(SessionExpired):
        await adapter.me()


async def test_network_failure_backs_off(settings, monkeypatch):
    _with_session(settings)
    attempts = []

    def factory(*a, **k):
        attempts.append(1)
        return FakePymaxClient(fail_with=ConnectionResetError())

    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(d):
        sleeps.append(d)
        await real_sleep(0)

    monkeypatch.setattr(pa, "build_client", factory)
    monkeypatch.setattr(pa.asyncio, "sleep", fake_sleep)
    adapter = pa.PyMaxAdapter(settings.model_copy(update={"connect_timeout": 0.05}))
    await adapter.start()  # не падает: переподключается в фоне
    await real_sleep(0.05)
    await adapter.stop()
    assert len(attempts) >= 3
    assert sleeps[0] < sleeps[2]  # экспоненциальный рост
    assert max(sleeps) <= pa.BACKOFF_MAX * 1.3
