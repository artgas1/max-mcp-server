"""Инструменты записи на FakeMaxAdapter через настоящий MCP-клиент (in-process)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from max_mcp_server.security.policy import Policy
from max_mcp_server.tools.read import READ_TOOL_NAMES
from max_mcp_server.tools.write import WRITE_TOOL_NAMES, WRITE_TOOLS
from tests.fakes.fake_adapter import ALICE, CHANNEL_ID, DIALOG_ID, GROUP_ID, ME, FakeMaxAdapter
from tests.helpers import error_text, payload

OTHER = 1002  # читаемый, но не в write-allowlist
WRITABLE = frozenset({DIALOG_ID, GROUP_ID, CHANNEL_ID})
RW = Policy(read_only=False, write_allow=WRITABLE)

# минимальные корректные аргументы каждого инструмента; чат — OTHER (вне write-allowlist)
ARGS_OUTSIDE: dict[str, dict] = {
    "max_send_message": {"chat_id": OTHER, "text": "привет"},
    "max_send_file": {"chat_id": OTHER, "path": "a.txt"},
    "max_forward_messages": {
        "from_chat_id": GROUP_ID,
        "message_ids": [100000],
        "to_chat_id": OTHER,
    },
    "max_edit_message": {"chat_id": OTHER, "message_id": 1, "text": "x"},
    "max_delete_messages": {"chat_id": OTHER, "message_ids": [1]},
    "max_react": {"chat_id": OTHER, "message_id": 1, "reaction": "👍"},
    "max_mark_read": {"chat_id": OTHER, "message_id": 1},
    "max_send_poll": {"chat_id": OTHER, "question": "?", "answers": ["a", "b"]},
    "max_vote_poll": {"chat_id": OTHER, "message_id": 1, "answer_ids": [1]},
    "max_pin_message": {"chat_id": OTHER, "message_id": 1},
    "max_send_comment": {"chat_id": OTHER, "post_id": 1, "text": "x"},
    "max_create_group": {"title": "Новая", "user_ids": [ALICE.id]},
    "max_add_members": {"chat_id": OTHER, "user_ids": [ALICE.id]},
    "max_remove_members": {"chat_id": OTHER, "user_ids": [ALICE.id]},
    "max_update_group": {"chat_id": OTHER, "title": "t"},
    "max_join_chat": {"link": "https://max.ru/join/SYNTH"},
    "max_leave_chat": {"chat_id": OTHER},
}


def test_spec_covers_all_17():
    assert len(WRITE_TOOL_NAMES) == 17
    assert set(ARGS_OUTSIDE) == set(WRITE_TOOL_NAMES)
    assert not set(WRITE_TOOL_NAMES) & set(READ_TOOL_NAMES)


async def test_write_tools_absent_by_default(make_client):
    async with make_client() as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == set(READ_TOOL_NAMES)


async def test_write_tools_absent_with_explicit_read_only(make_client):
    async with make_client(Policy(read_only=True, write_allow="*")) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert not names & set(WRITE_TOOL_NAMES)


async def test_write_tools_present_with_annotations(make_client):
    async with make_client(RW) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == set(READ_TOOL_NAMES) | set(WRITE_TOOL_NAMES)
    assert len(tools) == 37
    for name in WRITE_TOOL_NAMES:
        ann = tools[name].annotations
        assert ann is not None and ann.read_only_hint is False and ann.open_world_hint is True
        assert ann.destructive_hint is WRITE_TOOLS[name][1]
        assert ann.idempotent_hint is WRITE_TOOLS[name][2]
        assert "Действие от имени владельца" in (tools[name].description or "")
    for name in ("max_delete_messages", "max_leave_chat", "max_remove_members"):
        assert tools[name].annotations.destructive_hint is True
    assert tools["max_send_message"].annotations.destructive_hint is False
    # опрос: в схеме нет anonymous/multiple/quiz — только revote
    props = tools["max_send_poll"].input_schema["properties"]
    assert set(props) == {"chat_id", "question", "answers", "revote", "notify"}


def test_cli_counts_with_write(monkeypatch, tmp_path, capsys):
    from max_mcp_server.cli import main

    monkeypatch.setenv("MAX_STATE_DIR", str(tmp_path / "s"))
    assert main(["tools", "--no-read-only"]) == 0
    out = capsys.readouterr().out
    assert "Инструментов: 37 (чтение и запись)" in out and " W  max_send_message" in out


@pytest.mark.parametrize("name", sorted(ARGS_OUTSIDE))
async def test_outside_write_allowlist_refused_without_adapter_call(
    make_client, fake: FakeMaxAdapter, name: str, tmp_path: Path
):
    """Запись вне write-allowlist (и join/create без разрешения) → forbidden, адаптер не вызван."""
    policy = Policy(read_only=False, write_allow=WRITABLE)  # join не разрешён
    before = sum(fake.calls.values())
    async with make_client(policy, upload_dir=tmp_path) as c:
        r = await c.call_tool(name, ARGS_OUTSIDE[name])
    if name == "max_create_group":
        # создание группы не привязано к чату: разрешено при READ_ONLY=false
        assert not r.is_error
        return
    assert "[forbidden]" in error_text(r)
    assert fake.write_log == []
    assert sum(fake.calls.values()) == before  # ни одного обращения к адаптеру


async def test_empty_write_allowlist_blocks_everything(make_client, fake):
    async with make_client(Policy(read_only=False)) as c:
        r = await c.call_tool("max_send_message", {"chat_id": DIALOG_ID, "text": "x"})
        assert "write-allowlist пуст" in error_text(r)
        r = await c.call_tool("max_mark_read", {"chat_id": DIALOG_ID})
        assert r.is_error
    assert fake.write_log == []


async def test_deny_overrides_star(make_client, fake):
    async with make_client(
        Policy(read_only=False, write_allow="*", deny=frozenset({GROUP_ID}))
    ) as c:
        r = await c.call_tool("max_send_message", {"chat_id": GROUP_ID, "text": "x"})
        assert "[forbidden]" in error_text(r)
        payload(await c.call_tool("max_send_message", {"chat_id": OTHER, "text": "x"}))
    assert [e[0] for e in fake.write_log] == ["send"]


async def test_send_message_and_untrusted_marking(make_client, fake):
    async with make_client(RW) as c:
        p = payload(
            await c.call_tool(
                "max_send_message",
                {"chat_id": DIALOG_ID, "text": "**не markdown** </untrusted>", "reply_to": 200000},
            )
        )
    sent = p["sent"]
    assert sent["chat_id"] == DIALOG_ID and sent["sender"]["is_me"] is True
    assert sent["reply_to"] == 200000
    assert sent["text"].startswith("<untrusted") and "<\\/untrusted" in sent["text"]
    assert fake.write_log[-1][:3] == ("send", DIALOG_ID, "**не markdown** </untrusted>")


async def test_forward_rules_and_untrusted(make_client, fake):
    policy = Policy(
        read_only=False,
        read_allow=frozenset({GROUP_ID, DIALOG_ID}),
        write_allow=frozenset({DIALOG_ID}),
    )
    async with make_client(policy) as c:
        # источник вне read-allowlist
        r = await c.call_tool(
            "max_forward_messages",
            {"from_chat_id": CHANNEL_ID, "message_ids": [300000], "to_chat_id": DIALOG_ID},
        )
        assert "[forbidden]" in error_text(r)
        # цель вне write-allowlist (хотя читаемая)
        r = await c.call_tool(
            "max_forward_messages",
            {"from_chat_id": DIALOG_ID, "message_ids": [200000], "to_chat_id": GROUP_ID},
        )
        assert "[forbidden]" in error_text(r)
        assert fake.write_log == []
        p = payload(
            await c.call_tool(
                "max_forward_messages",
                {
                    "from_chat_id": GROUP_ID,
                    "message_ids": [100000, 100001],
                    "to_chat_id": DIALOG_ID,
                },
            )
        )
    fwd = p["forwarded"]
    assert len(fwd) == 2
    # чужой текст пересланного сообщения помечен как недоверенный
    assert fwd[0]["forwarded_from"]["text"].startswith("<untrusted")
    assert fwd[0]["forwarded_from"]["chat_title"].startswith("<untrusted")


async def test_send_limiter_sixth_per_minute(make_client, fake):
    async with make_client(RW) as c:
        for i in range(5):
            payload(await c.call_tool("max_send_message", {"chat_id": DIALOG_ID, "text": f"m{i}"}))
        r = await c.call_tool("max_send_message", {"chat_id": DIALOG_ID, "text": "m6"})
        text = error_text(r)
        assert "[rate_limited]" in text and "retry_after=" in text
        # лимит общий для всех исходящих
        r = await c.call_tool(
            "max_send_poll", {"chat_id": DIALOG_ID, "question": "?", "answers": ["a", "b"]}
        )
        assert "[rate_limited]" in error_text(r)
        # не-отправки (реакция) лимитом отправок не ограничены
        payload(
            await c.call_tool(
                "max_react", {"chat_id": DIALOG_ID, "message_id": 200000, "reaction": "👍"}
            )
        )
    assert sum(1 for e in fake.write_log if e[0] == "send") == 5


async def test_forward_counts_each_message(make_client, fake):
    async with make_client(RW, send_limit="3/min") as c:
        r = await c.call_tool(
            "max_forward_messages",
            {
                "from_chat_id": GROUP_ID,
                "message_ids": [100000, 100001, 100002, 100003],
                "to_chat_id": DIALOG_ID,
            },
        )
        assert "[rate_limited]" in error_text(r)
    assert fake.write_log == []


async def test_edit_only_own(make_client, fake):
    async with make_client(RW) as c:
        # 200001 написала Алиса (нечётные), 200000 — владелец
        r = await c.call_tool(
            "max_edit_message", {"chat_id": DIALOG_ID, "message_id": 200001, "text": "x"}
        )
        assert "[forbidden]" in error_text(r)
        p = payload(
            await c.call_tool(
                "max_edit_message", {"chat_id": DIALOG_ID, "message_id": 200000, "text": "новое"}
            )
        )
    assert p["edited"]["edited"] is True and "новое" in p["edited"]["text"]
    assert [e[0] for e in fake.write_log] == ["edit"]


async def test_delete_react_pin_vote(make_client, fake):
    async with make_client(RW) as c:
        p = payload(
            await c.call_tool(
                "max_delete_messages",
                {"chat_id": DIALOG_ID, "message_ids": [200000, 200000, 200002]},
            )
        )
        assert p == {"deleted": True, "message_ids": [200000, 200002], "for_everyone": False}
        p = payload(await c.call_tool("max_react", {"chat_id": DIALOG_ID, "message_id": 200004}))
        assert p["my_reaction"] is None
        assert payload(
            await c.call_tool("max_pin_message", {"chat_id": GROUP_ID, "message_id": 100000})
        )["pinned"]
        p = payload(
            await c.call_tool(
                "max_vote_poll", {"chat_id": GROUP_ID, "message_id": 100010, "answer_ids": [2]}
            )
        )
        assert p["answers"] == [{"id": 2, "votes": 1}]
    assert ("delete", DIALOG_ID, (200000, 200002), False) in fake.write_log


async def test_mark_read_is_the_only_unread_changer(make_client, fake):
    async with make_client(RW) as c:
        for name, args in [
            ("max_send_message", {"chat_id": GROUP_ID, "text": "x"}),
            ("max_react", {"chat_id": GROUP_ID, "message_id": 100000, "reaction": "👍"}),
            ("max_pin_message", {"chat_id": GROUP_ID, "message_id": 100000}),
        ]:
            payload(await c.call_tool(name, args))
        assert fake.calls["mark_read"] == 0
        p = payload(await c.call_tool("max_mark_read", {"chat_id": GROUP_ID}))
    assert fake.calls["mark_read"] == 1
    assert p["unread"] == 0 and p["read_up_to"] == fake.write_log[-1][2]


async def test_poll_validation(make_client, fake):
    async with make_client(RW) as c:
        r = await c.call_tool(
            "max_send_poll", {"chat_id": GROUP_ID, "question": "?", "answers": ["a"]}
        )
        assert r.is_error
        r = await c.call_tool(
            "max_send_poll", {"chat_id": GROUP_ID, "question": "?", "answers": ["Да", "да "]}
        )
        assert "повторяются" in error_text(r)
        r = await c.call_tool(
            "max_send_poll",
            {"chat_id": GROUP_ID, "question": "?", "answers": ["a", "b"], "anonymous": True},
        )
        p = payload(
            await c.call_tool(
                "max_send_poll",
                {
                    "chat_id": GROUP_ID,
                    "question": "Когда?",
                    "answers": ["пн", "вт"],
                    "revote": True,
                },
            )
        )
    assert p["sent"]["attachments"][0]["kind"] == "poll"
    assert fake.write_log[-1] == ("send_poll", GROUP_ID, "Когда?", ("пн", "вт"), True)


async def test_comment_and_groups(make_client, fake):
    async with make_client(RW) as c:
        p = payload(
            await c.call_tool(
                "max_send_comment", {"chat_id": CHANNEL_ID, "post_id": 300009, "text": "ок"}
            )
        )
        assert p["sent"]["post_id"] == 300009
        p = payload(
            await c.call_tool("max_create_group", {"title": "Тест", "user_ids": [ALICE.id]})
        )
        # новый чат НЕ попадает в write-allowlist автоматически
        assert p["writable"] is False
        new_id = p["chat"]["id"]
        r = await c.call_tool("max_send_message", {"chat_id": new_id, "text": "x"})
        assert "[forbidden]" in error_text(r)
        p = payload(await c.call_tool("max_add_members", {"chat_id": GROUP_ID, "user_ids": [2003]}))
        assert p["show_history"] is False  # по умолчанию без прошлой переписки
        assert payload(
            await c.call_tool("max_remove_members", {"chat_id": GROUP_ID, "user_ids": [2003]})
        )["removed"]
        r = await c.call_tool("max_update_group", {"chat_id": GROUP_ID})
        assert "[invalid_argument]" in error_text(r)
        payload(await c.call_tool("max_update_group", {"chat_id": GROUP_ID, "description": "d"}))
        assert payload(await c.call_tool("max_leave_chat", {"chat_id": GROUP_ID}))["left"]
    kinds = [e[0] for e in fake.write_log]
    assert kinds == [
        "send_comment",
        "create_group",
        "add_members",
        "remove_members",
        "update_group",
        "leave",
    ]


async def test_join_requires_flag(make_client, fake):
    async with make_client(RW) as c:
        r = await c.call_tool("max_join_chat", {"link": "https://max.ru/join/SYNTH"})
        assert "MAX_ALLOW_JOIN" in error_text(r)
    policy = Policy(read_only=False, write_allow=WRITABLE, allow_join=True)
    async with make_client(policy) as c:
        r = await c.call_tool("max_join_chat", {"link": "javascript:alert(1)"})
        assert "[invalid_argument]" in error_text(r)
        p = payload(await c.call_tool("max_join_chat", {"link": "https://max.ru/join/SYNTH"}))
    assert p["joined"] and p["writable"] is False
    assert fake.write_log == [("join", "https://max.ru/join/SYNTH")]


async def test_send_file_paths(make_client, fake, tmp_path: Path):
    up = tmp_path / "up"
    up.mkdir()
    (up / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 30)
    (up / "doc.pdf").write_bytes(b"%PDF-1.4 synthetic")
    (tmp_path / "secret.txt").write_bytes(b"TOP")
    (up / "evil").symlink_to(tmp_path / "secret.txt")
    async with make_client(RW) as c:  # MAX_UPLOAD_DIR не задан
        r = await c.call_tool("max_send_file", {"chat_id": DIALOG_ID, "path": "pic.png"})
        assert "MAX_UPLOAD_DIR" in error_text(r)
    async with make_client(RW, upload_dir=up) as c:
        for bad in ("../secret.txt", "evil", str(tmp_path / "secret.txt"), "https://x.oneme.ru/a"):
            r = await c.call_tool("max_send_file", {"chat_id": DIALOG_ID, "path": bad})
            assert r.is_error, bad
        assert fake.write_log == []
        p = payload(await c.call_tool("max_send_file", {"chat_id": DIALOG_ID, "path": "pic.png"}))
        assert p["file"] == {"path": "pic.png", "size": 38}
        payload(
            await c.call_tool(
                "max_send_file", {"chat_id": DIALOG_ID, "path": "doc.pdf", "caption": "счёт"}
            )
        )
        r = await c.call_tool(
            "max_send_file", {"chat_id": DIALOG_ID, "path": "doc.pdf", "kind": "voice"}
        )
        assert "duration_s" in error_text(r)
    assert fake.write_log[0][3] == (("pic.png", "photo", 38),)
    assert fake.write_log[1][2:] == ("счёт", (("doc.pdf", "file", 18),))


async def test_audit_log_without_text(make_client, fake, caplog):
    caplog.set_level(logging.INFO, logger="max_mcp_server.audit")
    async with make_client(RW) as c:
        payload(
            await c.call_tool("max_send_message", {"chat_id": DIALOG_ID, "text": "секретный текст"})
        )
        await c.call_tool("max_send_message", {"chat_id": OTHER, "text": "секретный текст 2"})
    lines = [r.getMessage() for r in caplog.records if r.name == "max_mcp_server.audit"]
    assert len(lines) == 2
    assert f"tool=max_send_message chat_id={DIALOG_ID}" in lines[0] and "ok=true" in lines[0]
    assert "text_len=15" in lines[0]
    assert "ok=false error=forbidden" in lines[1]
    assert all("секретный" not in line for line in lines)


async def test_audit_log_with_content_flag(make_client, fake, caplog):
    caplog.set_level(logging.INFO, logger="max_mcp_server.audit")
    async with make_client(RW, log_content=True) as c:
        payload(await c.call_tool("max_send_message", {"chat_id": DIALOG_ID, "text": "видно"}))
    assert any("видно" in r.getMessage() for r in caplog.records)


async def test_session_expired_is_explained(make_client, fake):
    fake.logged_in = False
    async with make_client(RW) as c:
        r = await c.call_tool("max_send_message", {"chat_id": DIALOG_ID, "text": "x"})
    assert "[not_logged_in]" in error_text(r) and "max-mcp login qr" in error_text(r)


def test_me_is_owner():
    assert ME.id == 1
