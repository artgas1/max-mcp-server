"""Каждый read-инструмент на FakeMaxAdapter через настоящий MCP-клиент (in-process)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from max_mcp_server.security.policy import Policy
from max_mcp_server.tools.read import READ_TOOL_NAMES
from tests.fakes.fake_adapter import (
    ALICE,
    CHANNEL_ID,
    DAY,
    DIALOG_ID,
    GROUP_ID,
    T0,
    FakeMaxAdapter,
)
from tests.helpers import error_text, payload

# инструменты записи из design.md 6.2 — ни одного не должно быть в read-only
WRITE_TOOLS = {
    "max_send_message",
    "max_send_file",
    "max_forward_messages",
    "max_edit_message",
    "max_delete_messages",
    "max_react",
    "max_mark_read",
    "max_send_poll",
    "max_vote_poll",
    "max_pin_message",
    "max_send_comment",
    "max_create_group",
    "max_add_members",
    "max_remove_members",
    "max_update_group",
    "max_join_chat",
    "max_leave_chat",
}


async def _read_all(c, args, *, key="messages"):
    out, cursor, pages = [], None, 0
    while True:
        r = payload(
            await c.call_tool(
                "max_read_messages", {**args, **({"cursor": cursor} if cursor else {})}
            )
        )
        out.extend(r[key])
        pages += 1
        cursor = r["next_cursor"]
        if not cursor:
            return out, pages
        assert pages < 100


async def test_list_tools_read_only(make_client):
    async with make_client() as c:
        tools = (await c.list_tools()).tools
    names = {t.name for t in tools}
    assert names == set(READ_TOOL_NAMES)
    assert len(names) == 20
    for t in tools:
        assert t.annotations is not None and t.annotations.read_only_hint is True
        assert t.annotations.destructive_hint is False
    assert not names & WRITE_TOOLS


async def test_write_tool_unknown(make_client):
    async with make_client() as c:
        r = await c.call_tool("max_send_message", {"chat_id": DIALOG_ID, "text": "x"})
    assert r.is_error


async def test_get_me(make_client):
    async with make_client() as c:
        p = payload(await c.call_tool("max_get_me", {}))
    assert p["profile"]["id"] == 1 and "phone" not in p["profile"]
    assert p["mode"]["read_only"] is True
    assert p["connection"] == "ready"


async def test_get_me_phone_only_when_enabled(make_client):
    async with make_client(show_own_phone=True) as c:
        p = payload(await c.call_tool("max_get_me", {}))
    assert p["profile"]["phone"].startswith("+")


async def test_list_chats_cursor_pagination(make_client):
    async with make_client() as c:
        seen, cursor = [], None
        for _ in range(10):
            args = {"limit": 2, **({"cursor": cursor} if cursor else {})}
            p = payload(await c.call_tool("max_list_chats", args))
            seen.extend(ch["id"] for ch in p["chats"])
            cursor = p["next_cursor"]
            if not cursor:
                break
    assert len(seen) == len(set(seen)) == 8
    assert seen[0] == DIALOG_ID  # самый свежий


async def test_list_chats_filters(make_client):
    async with make_client() as c:
        groups = payload(await c.call_tool("max_list_chats", {"type": "group"}))["chats"]
        unread = payload(await c.call_tool("max_list_chats", {"unread_only": True}))["chats"]
        folder = payload(await c.call_tool("max_list_chats", {"folder_id": "work"}))["chats"]
        q = payload(await c.call_tool("max_list_chats", {"query": "синтетика"}))["chats"]
    assert groups and all(ch["type"] == "group" for ch in groups)
    assert {ch["id"] for ch in unread} == {DIALOG_ID, GROUP_ID, 1002, -3002}
    assert {ch["id"] for ch in folder} == {GROUP_ID, CHANNEL_ID, 1002}
    assert {ch["id"] for ch in q} == {GROUP_ID, CHANNEL_ID}
    assert all(ch["title"].startswith("<untrusted>") for ch in q)


async def test_get_chat_and_folders(make_client):
    async with make_client() as c:
        p = payload(await c.call_tool("max_get_chat", {"chat_id": GROUP_ID}))
        f = payload(await c.call_tool("max_list_folders", {}))
    assert p["chat"]["owner_id"] == 1 and p["chat"]["pinned_message"]["id"] == 100_000
    assert [x["id"] for x in f["folders"]] == ["all.chat.folder", "work"]


@pytest.mark.parametrize("order", ["asc", "desc"])
async def test_read_messages_full_depth_no_dups_or_gaps(make_client, fake: FakeMaxAdapter, order):
    expected = sorted(fake.messages[GROUP_ID], key=lambda m: m.key)
    async with make_client() as c:
        args = {"chat_id": GROUP_ID, "limit": 97, "order": order}
        if order == "asc":
            args["date_from"] = "2025-12-01"
        got, pages = await _read_all(c, args)
    ids = [m["id"] for m in got]
    assert len(ids) == len(set(ids)) == 1500
    assert set(ids) == {m.id for m in expected}
    assert pages >= 15
    # одинаковое время у соседних сообщений действительно есть
    times = [m.time_ms for m in expected]
    assert len(set(times)) < len(times)


async def test_read_messages_period_and_order(make_client, fake):
    lo, hi = "2026-03-01", "2026-03-10"
    async with make_client() as c:
        asc, _ = await _read_all(
            c, {"chat_id": GROUP_ID, "date_from": lo, "date_to": hi, "limit": 7}
        )
        desc, _ = await _read_all(
            c, {"chat_id": GROUP_ID, "date_from": lo, "date_to": hi, "limit": 7, "order": "desc"}
        )
    lo_ms = T0 + 59 * DAY  # 2026-03-01
    hi_ms = T0 + 69 * DAY  # 2026-03-11 00:00
    exp = [
        m.id
        for m in sorted(fake.messages[GROUP_ID], key=lambda m: m.key)
        if lo_ms <= m.time_ms < hi_ms
    ]
    assert exp
    assert [m["id"] for m in asc] == exp
    assert {m["id"] for m in desc} == set(exp)
    assert all(m["time"].startswith("2026-03") for m in asc)


async def test_read_messages_latest_default_and_sender(make_client, fake):
    async with make_client() as c:
        p = payload(await c.call_tool("max_read_messages", {"chat_id": DIALOG_ID, "limit": 5}))
        s = payload(
            await c.call_tool(
                "max_read_messages", {"chat_id": DIALOG_ID, "sender_id": ALICE.id, "limit": 100}
            )
        )
    assert [m["id"] for m in p["messages"]] == [200_025 + i for i in range(5)]
    assert p["direction"] == "older" and p["has_more"]
    assert s["count"] == 15 and all(m["sender"]["id"] == ALICE.id for m in s["messages"])


async def test_read_messages_compact(make_client):
    async with make_client() as c:
        p = payload(
            await c.call_tool(
                "max_read_messages", {"chat_id": GROUP_ID, "format": "compact", "limit": 2000}
            )
        )
    assert "text" in p and "messages" not in p
    lines = p["text"].split("\n")
    assert len(lines) == p["count"] == 1500
    # у каждого сообщения своя обёртка; автор — в заголовке вне неё
    assert all(" <untrusted>" in ln and ln.endswith("</untrusted>") for ln in lines)
    assert lines[0].split(" <untrusted>")[0].endswith("[me]")
    assert "Алиса Тестова" in p["text"]


async def test_read_messages_cursor_tamper_and_other_chat(make_client):
    async with make_client() as c:
        p = payload(await c.call_tool("max_read_messages", {"chat_id": GROUP_ID, "limit": 3}))
        bad = p["next_cursor"][:-3] + ("AAA" if not p["next_cursor"].endswith("AAA") else "BBB")
        e1 = error_text(
            await c.call_tool("max_read_messages", {"chat_id": GROUP_ID, "cursor": bad})
        )
        e2 = error_text(
            await c.call_tool(
                "max_read_messages", {"chat_id": DIALOG_ID, "cursor": p["next_cursor"]}
            )
        )
    assert "cursor" in e1 and "другому чату" in e2


async def test_messages_around_by_id_and_date(make_client, fake):
    msgs = sorted(fake.messages[GROUP_ID], key=lambda m: m.key)
    target = msgs[701]  # из пачки с одинаковым временем
    async with make_client() as c:
        by_id = payload(
            await c.call_tool(
                "max_messages_around",
                {"chat_id": GROUP_ID, "message_id": target.id, "before": 5, "after": 5},
            )
        )
        by_date = payload(
            await c.call_tool(
                "max_messages_around",
                {"chat_id": GROUP_ID, "date": "2026-06-01", "before": 3, "after": 3},
            )
        )
        err = error_text(await c.call_tool("max_messages_around", {"chat_id": GROUP_ID}))
    ids = [m["id"] for m in by_id["messages"]]
    assert ids == [m.id for m in msgs[696:707]]
    assert by_id["anchor_message_id"] == target.id
    assert by_date["count"] == 6
    assert by_date["messages"][3]["id"] == by_date["anchor_message_id"]
    assert by_date["messages"][2]["time"] < "2026-06-01" <= by_date["messages"][3]["time"]
    assert "ровно одно" in err


async def test_get_messages_and_missing(make_client):
    async with make_client() as c:
        p = payload(
            await c.call_tool(
                "max_get_messages", {"chat_id": GROUP_ID, "message_ids": [100_010, 100_020, 1]}
            )
        )
    assert [m["id"] for m in p["messages"]] == [100_010, 100_020]
    assert p["missing"] == [1]
    fwd = p["messages"][1]["forwarded_from"]
    assert fwd["message_id"] == 5000 and "пересланный" in fwd["text"]


async def test_attachments_serialized_safely(make_client):
    async with make_client() as c:
        p = payload(
            await c.call_tool("max_get_messages", {"chat_id": GROUP_ID, "message_ids": [100_010]})
        )
    raw = json.dumps(p, ensure_ascii=False)
    for secret in ("SYNTHETIC_PHOTO_TOKEN", "oneme.ru", "preview", "bytes"):
        assert secret not in raw
    atts = p["messages"][0]["attachments"]
    assert [a["kind"] for a in atts] == [
        "photo",
        "file",
        "voice",
        "video",
        "link",
        "poll",
        "service",
        "sticker",
        "unknown",
    ]
    assert atts[0]["ref"] == f"max-att:{GROUP_ID}:100010:0"
    assert "<\\/untrusted>" in atts[4]["description"]
    assert atts[5]["answers"][0]["votes"] == 3
    assert "голосового" in atts[2]["transcript"]


async def test_text_normalized(make_client):
    async with make_client() as c:
        p = payload(
            await c.call_tool("max_get_messages", {"chat_id": GROUP_ID, "message_ids": [100_333]})
        )
    text = p["messages"][0]["text"]
    assert "‮" not in text and "\x07" not in text and "перевёрнутый" in text


async def test_chat_timeline(make_client, fake):
    async with make_client() as c:
        p = payload(await c.call_tool("max_chat_timeline", {"chat_id": GROUP_ID}))
        d = payload(
            await c.call_tool(
                "max_chat_timeline",
                {
                    "chat_id": GROUP_ID,
                    "granularity": "day",
                    "date_from": "2026-02-01",
                    "date_to": "2026-02-03",
                },
            )
        )
    assert p["total"] == 1500 and p["complete"] is True
    assert p["periods"][0]["period"] == "2026-01"
    assert all(x["period"].startswith("2026-02-0") for x in d["periods"]) and d["total"] > 0


async def test_search_scan_single_chat_and_cursor(make_client, fake):
    expected = {m.id for m in fake.messages[GROUP_ID] if "ёлка" in m.text.lower()}
    async with make_client() as c:
        found, cursor = [], None
        while True:
            args = {"query": "ЕЛКА отчёт", "chat_id": GROUP_ID, "limit": 7}
            if cursor:
                args["cursor"] = cursor
            p = payload(await c.call_tool("max_search_messages", args))
            assert p["method"] == "scan"
            found += [m["id"] for m in p["messages"]]
            cursor = p["next_cursor"]
            if not cursor:
                break
    assert len(found) == len(set(found)) and set(found) == expected == {
        100_000 + i for i in range(0, 1500, 50)
    }


async def test_search_all_chats_and_scan_limit(make_client):
    async with make_client() as c:
        p = payload(await c.call_tool("max_search_messages", {"query": "диалог 7"}))
        lim = payload(
            await c.call_tool(
                "max_search_messages", {"query": "нетакого", "chat_id": GROUP_ID, "scan_limit": 100}
            )
        )
        fwd = payload(
            await c.call_tool("max_search_messages", {"query": "пересланный", "chat_id": GROUP_ID})
        )
    # подстрочный поиск: «диалог 7» находит 7, 17, 27 (новые первыми)
    assert [m["id"] for m in p["messages"]] == [200_027, 200_017, 200_007]
    assert lim["scanned"] == 100 and lim["next_cursor"]
    assert [m["id"] for m in fwd["messages"]] == [100_020]


async def test_unread_summary_has_no_side_effects(make_client, fake: FakeMaxAdapter):
    before = dict(fake.unread)
    async with make_client() as c:
        p = payload(await c.call_tool("max_unread_summary", {"per_chat": 2}))
    assert fake.calls["mark_read"] == 0
    assert fake.unread == before
    by_id = {ch["id"]: ch for ch in p["chats"]}
    assert set(by_id) == {DIALOG_ID, GROUP_ID, 1002, -3002}
    assert len(by_id[DIALOG_ID]["messages"]) == 2
    assert len(by_id[-3002]["messages"]) == 1
    assert by_id[DIALOG_ID]["messages"][-1]["id"] == 200_029
    assert p["total_unread"] == 13


async def test_comments_pagination(make_client, fake):
    post = 300_009
    async with make_client() as c:
        got, cursor = [], None
        while True:
            args = {"chat_id": CHANNEL_ID, "post_id": post, "limit": 25}
            if cursor:
                args["cursor"] = cursor
            p = payload(await c.call_tool("max_list_comments", args))
            got += [m["id"] for m in p["comments"]]
            cursor = p["next_cursor"]
            if not cursor:
                break
    assert p["total"] == 120
    assert len(got) == len(set(got)) == 120


async def test_members_users_contacts(make_client):
    async with make_client() as c:
        m1 = payload(await c.call_tool("max_list_members", {"chat_id": GROUP_ID, "limit": 3}))
        m2 = payload(
            await c.call_tool(
                "max_list_members", {"chat_id": GROUP_ID, "cursor": m1["next_cursor"]}
            )
        )
        u = payload(await c.call_tool("max_get_users", {"user_ids": [2001, 2001, 9]}))
        ct = payload(await c.call_tool("max_list_contacts", {"query": "вера"}))
        all_ct = payload(await c.call_tool("max_list_contacts", {"limit": 3}))
    assert len(m1["members"]) == 3 and len(m2["members"]) == 1 and m2["next_cursor"] is None
    assert any(x["is_admin"] for x in m1["members"])
    assert [x["id"] for x in u["users"]] == [2001] and u["missing"] == [9]
    assert "phone" not in json.dumps(u)
    assert [x["id"] for x in ct["contacts"]] == [2003]
    assert all_ct["total"] == 4 and all_ct["next_cursor"]


async def test_search_chats_and_resolve_link(make_client):
    async with make_client() as c:
        s = payload(await c.call_tool("max_search_chats", {"query": "алиса"}))
        ch = payload(
            await c.call_tool("max_resolve_link", {"link": "https://max.ru/synthetic_channel"})
        )
        us = payload(await c.call_tool("max_resolve_link", {"link": "@alice_test"}))
        nf = error_text(await c.call_tool("max_resolve_link", {"link": "@nobody"}))
    assert [x["id"] for x in s["chats"]] == [DIALOG_ID] and [x["id"] for x in s["contacts"]] == [
        2001
    ]
    assert ch["type"] == "chat" and ch["chat"]["id"] == CHANNEL_ID
    assert us["user"]["id"] == 2001
    assert "not_found" in nf


async def test_attachment_info_and_download(make_client, settings, fake):
    ref = f"max-att:{GROUP_ID}:100010:1"
    async with make_client() as c:
        info = payload(await c.call_tool("max_get_attachment_info", {"ref": ref}))
        dl = payload(await c.call_tool("max_download_media", {"ref": ref}))
        img = await c.call_tool(
            "max_download_media", {"ref": f"max-att:{GROUP_ID}:100010:0", "inline": True}
        )
        bad = error_text(await c.call_tool("max_get_attachment_info", {"ref": "max-att:x"}))
        not_dl = error_text(
            await c.call_tool("max_download_media", {"ref": f"max-att:{GROUP_ID}:100010:4"})
        )
    assert info["attachment"]["kind"] == "file" and info["attachment"]["size"] == 52311
    path = Path(dl["path"])
    assert path.parent == settings.download_path and path.read_bytes().startswith(b"%PDF")
    assert dl["mime"] == "application/pdf" and len(dl["sha256"]) == 64
    assert not img.is_error
    assert any(
        getattr(b, "type", "") == "image" and b.mime_type == "image/png" for b in img.content
    )
    assert "ref" in bad and "нельзя скачать" in not_dl


async def test_wait_updates(make_client, fake: FakeMaxAdapter):
    from max_mcp_server.adapter.base import MessageDTO

    async with make_client() as c:
        first = payload(await c.call_tool("max_wait_updates", {"timeout_s": 0}))
        assert first["events"] == []

        async def later():
            await asyncio.sleep(0.05)
            await fake.emit(
                "message",
                DIALOG_ID,
                message=MessageDTO(
                    id=7, chat_id=DIALOG_ID, time_ms=T0, text="новое", sender_id=2001
                ),
            )

        task = asyncio.create_task(later())
        got = payload(
            await c.call_tool("max_wait_updates", {"cursor": first["cursor"], "timeout_s": 5})
        )
        await task
        assert [e["message"]["id"] for e in got["events"]] == [7]
        again = payload(
            await c.call_tool("max_wait_updates", {"cursor": got["cursor"], "timeout_s": 0})
        )
        assert again["events"] == []


async def test_get_reactions(make_client):
    async with make_client() as c:
        p = payload(
            await c.call_tool(
                "max_get_reactions", {"chat_id": GROUP_ID, "message_ids": [100_010, 100_011]}
            )
        )
    assert p["reactions"] == [{"message_id": 100_010, "counters": [["👍", 2]], "my_reaction": None}]


# ------------------------------------------------------------------ allowlist


async def test_read_allowlist_blocks_and_filters(make_client, fake: FakeMaxAdapter):
    policy = Policy(read_allow=frozenset({DIALOG_ID, CHANNEL_ID}), deny=frozenset({CHANNEL_ID}))
    async with make_client(policy) as c:
        chats = payload(await c.call_tool("max_list_chats", {"limit": 50}))["chats"]
        assert [ch["id"] for ch in chats] == [DIALOG_ID]
        for tool, args in [
            ("max_read_messages", {"chat_id": GROUP_ID}),
            ("max_get_chat", {"chat_id": GROUP_ID}),
            ("max_get_messages", {"chat_id": GROUP_ID, "message_ids": [100_010]}),
            ("max_chat_timeline", {"chat_id": GROUP_ID}),
            ("max_messages_around", {"chat_id": GROUP_ID, "message_id": 100_010}),
            ("max_search_messages", {"query": "x", "chat_id": GROUP_ID}),
            ("max_list_members", {"chat_id": GROUP_ID}),
            ("max_get_reactions", {"chat_id": GROUP_ID, "message_ids": [1]}),
            ("max_read_messages", {"chat_id": CHANNEL_ID}),  # deny поверх allow
            ("max_get_attachment_info", {"ref": f"max-att:{GROUP_ID}:100010:0"}),
            ("max_wait_updates", {"chat_id": GROUP_ID, "timeout_s": 0}),
        ]:
            assert "forbidden" in error_text(await c.call_tool(tool, args)), tool
        summary = payload(await c.call_tool("max_unread_summary", {}))
        assert [ch["id"] for ch in summary["chats"]] == [DIALOG_ID]
        found = payload(await c.call_tool("max_search_messages", {"query": "сообщение"}))
        assert found["messages"] == []
        folders = payload(await c.call_tool("max_list_folders", {}))
        assert folders["folders"][1]["chat_ids"] == []
        link = payload(await c.call_tool("max_resolve_link", {"link": "https://max.ru/denied"}))
        assert link == {"type": "chat", "id": -3003, "restricted": True}

        # события чужих чатов отбрасываются на входе буфера
        start = payload(await c.call_tool("max_wait_updates", {"timeout_s": 0}))
        await fake.emit("message", GROUP_ID)
        await fake.emit("message", CHANNEL_ID)
        await fake.emit("delete", DIALOG_ID, data={"message_ids": [1]})
        got = payload(
            await c.call_tool("max_wait_updates", {"cursor": start["cursor"], "timeout_s": 0})
        )
    assert [(e["type"], e["chat_id"]) for e in got["events"]] == [("delete", DIALOG_ID)]
    assert fake.events().dropped == 2


async def test_not_logged_in_is_clear_error(make_client, fake: FakeMaxAdapter):
    fake.logged_in = False
    async with make_client() as c:
        e = error_text(await c.call_tool("max_list_chats", {}))
    assert "not_logged_in" in e and "max-mcp login" in e


async def test_internal_errors_hidden(make_client, fake: FakeMaxAdapter, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(fake, "list_folders", boom)
    async with make_client() as c:
        e = error_text(await c.call_tool("max_list_folders", {}))
    assert "internal_error" in e and "secret" not in e


async def test_limits_validated(make_client):
    async with make_client() as c:
        r1 = await c.call_tool("max_read_messages", {"chat_id": GROUP_ID, "limit": 1001})
        r2 = await c.call_tool("max_list_chats", {"limit": 500})
        r3 = await c.call_tool("max_wait_updates", {"timeout_s": 600})
        r4 = await c.call_tool("max_read_messages", {"chat_id": GROUP_ID, "date_from": "вчера"})
    assert r1.is_error and r2.is_error and r3.is_error and r4.is_error
