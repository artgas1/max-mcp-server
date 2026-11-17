"""security/*: политика, лимиты, пути, SSRF/DNS rebinding при скачивании (без внешней сети)."""

from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path

import pytest

from max_mcp_server.config import Settings, load_settings
from max_mcp_server.errors import (
    ConfigError,
    Forbidden,
    InvalidArgument,
    NotFound,
    RateLimited,
    UpstreamError,
)
from max_mcp_server.security.net import (
    SafeDownloader,
    VettingResolver,
    ip_allowed,
    parse_networks,
    sniff_mime,
)
from max_mcp_server.security.paths import download_target, read_upload, safe_filename
from max_mcp_server.security.policy import Policy
from max_mcp_server.security.ratelimit import SendLimiter, parse_send_limit

# --------------------------------------------------------------------------- policy


def test_policy_write_allowlist_empty_by_default():
    p = Policy(read_only=False)
    assert not p.can_write(1001)
    with pytest.raises(Forbidden, match="write-allowlist пуст"):
        p.check_write(1001)
    assert not p.write_enabled


def test_policy_read_only_blocks_even_with_allowlist():
    p = Policy(read_only=True, write_allow=frozenset({1001}))
    assert not p.can_write(1001)
    with pytest.raises(Forbidden, match="MAX_READ_ONLY"):
        p.check_write(1001)


def test_policy_star_and_deny():
    p = Policy(read_only=False, write_allow="*", deny=frozenset({-3001}))
    assert p.can_write(1001)
    assert not p.can_write(-3001)  # deny сильнее «*»
    assert not p.can_read(-3001)


def test_policy_write_requires_read():
    p = Policy(read_only=False, read_allow=frozenset({1}), write_allow=frozenset({1, 2}))
    assert p.can_write(1)
    assert not p.can_write(2)  # писать вслепую в нечитаемый чат нельзя


def test_policy_forward_and_join():
    p = Policy(read_only=False, read_allow=frozenset({1, 2}), write_allow=frozenset({2}))
    p.check_forward(1, 2)
    with pytest.raises(Forbidden):
        p.check_forward(3, 2)  # источник не читаемый
    with pytest.raises(Forbidden):
        p.check_forward(2, 1)  # цель не в write-allowlist
    with pytest.raises(Forbidden, match="MAX_ALLOW_JOIN"):
        p.check_join()
    Policy(read_only=False, allow_join=True).check_join()


# --------------------------------------------------------------------------- send limiter


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_parse_send_limit():
    assert parse_send_limit("20/h,5/min") == ((20, 3600.0), (5, 60.0))
    assert parse_send_limit("3/10s") == ((3, 10.0),)
    assert parse_send_limit("off") == ()
    for bad in ("5", "x/min", "5/week", "0/min"):
        with pytest.raises(ConfigError):
            parse_send_limit(bad)
    with pytest.raises(ConfigError):
        load_settings(send_limit="много")


def test_send_limiter_minute_and_hour():
    clock = Clock()
    lim = SendLimiter.from_spec("20/h,5/min", clock=clock)
    for _ in range(5):
        lim.check()
    with pytest.raises(RateLimited) as ei:
        lim.check()
    assert ei.value.retry_after == 60
    clock.t += 30
    with pytest.raises(RateLimited) as ei:
        lim.check()
    assert ei.value.retry_after == 30
    clock.t += 30  # минута прошла
    lim.check()
    # часовой лимит: 20 за час (уже 6)
    for _ in range(14):
        clock.t += 13
        lim.check()
    clock.t += 61
    with pytest.raises(RateLimited) as ei:
        lim.check()
    assert ei.value.retry_after > 60  # ждать уже не минуту, а до выхода из часового окна


def test_send_limiter_batch():
    clock = Clock()
    lim = SendLimiter.from_spec("5/min", clock=clock)
    lim.check(3)
    with pytest.raises(RateLimited):
        lim.check(3)
    lim.check(2)
    with pytest.raises(RateLimited, match="За один вызов"):
        SendLimiter.from_spec("5/min", clock=clock).check(6)


# --------------------------------------------------------------------------- paths


@pytest.fixture
def updir(tmp_path: Path) -> Path:
    d = tmp_path / "up"
    d.mkdir()
    (d / "ok.txt").write_bytes(b"hello")
    (d / "sub").mkdir()
    (d / "sub" / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
    (tmp_path / "secret.txt").write_bytes(b"TOP SECRET")
    return d


def test_read_upload_ok(updir: Path):
    f = read_upload("ok.txt", updir, max_bytes=100)
    assert f.data == b"hello" and f.name == "ok.txt" and f.rel == "ok.txt"
    f2 = read_upload(str(updir / "sub" / "pic.png"), updir, max_bytes=100)
    assert f2.rel == os.path.join("sub", "pic.png")


@pytest.mark.parametrize(
    "path",
    ["../secret.txt", "sub/../../secret.txt", "/etc/passwd", "~/x", ".", "sub"],
)
def test_read_upload_escape(updir: Path, path: str):
    with pytest.raises((Forbidden, NotFound)):
        read_upload(path, updir, max_bytes=100)


def test_read_upload_symlink_escape(updir: Path, tmp_path: Path):
    (updir / "link.txt").symlink_to(tmp_path / "secret.txt")
    (updir / "dirlink").symlink_to(tmp_path)
    with pytest.raises(Forbidden):
        read_upload("link.txt", updir, max_bytes=100)
    with pytest.raises(Forbidden):
        read_upload("dirlink/secret.txt", updir, max_bytes=100)
    # симлинк внутрь каталога — допустим (реальный путь внутри)
    (updir / "inner").symlink_to(updir / "ok.txt")
    assert read_upload("inner", updir, max_bytes=100).data == b"hello"


def test_read_upload_limits_and_types(updir: Path):
    with pytest.raises(InvalidArgument, match="лимит"):
        read_upload("ok.txt", updir, max_bytes=3)
    (updir / "empty").write_bytes(b"")
    with pytest.raises(InvalidArgument, match="пустой"):
        read_upload("empty", updir, max_bytes=3)
    os.mkfifo(updir / "fifo")
    with pytest.raises(Forbidden, match="обычные"):
        read_upload("fifo", updir, max_bytes=3)
    with pytest.raises(Forbidden, match="MAX_UPLOAD_DIR"):
        read_upload("ok.txt", None, max_bytes=3)
    with pytest.raises(InvalidArgument, match="URL"):
        read_upload("https://evil.example/x", updir, max_bytes=3)
    with pytest.raises(NotFound):
        read_upload("nope.txt", updir, max_bytes=3)


def test_download_target_and_names(tmp_path: Path):
    assert download_target(tmp_path, "../../etc/passwd").parent == tmp_path
    assert download_target(tmp_path, "a/b\\c").name == "a_b_c"
    assert safe_filename("...") == "file"
    assert safe_filename("‮exe.txt") == "exe.txt"


# --------------------------------------------------------------------------- net / SSRF


def test_ip_allowed():
    ok = ipaddress.ip_address("93.184.216.34")
    assert ip_allowed(ok)
    for bad in (
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.10.20",
        "169.254.169.254",
        "100.100.10.20",  # CGNAT (NetBird/Tailscale)
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fd00::1",
        "::ffff:127.0.0.1",
    ):
        assert not ip_allowed(ipaddress.ip_address(bad)), bad
    nets = parse_networks("10.0.0.0/8")
    assert ip_allowed(ipaddress.ip_address("10.1.2.3"), nets)
    with pytest.raises(ConfigError):
        parse_networks("10.0.0.0/99")


def _dl(**kw) -> SafeDownloader:
    return SafeDownloader(host_suffixes=("oneme.ru", "okcdn.ru"), **kw)


def test_check_url():
    d = _dl()
    assert d.check_url("https://i.oneme.ru/x") == "i.oneme.ru"
    for bad in (
        "http://i.oneme.ru/x",
        "https://evil.com/x",
        "https://oneme.ru.evil.com/",
        "file:///etc/passwd",
        "https://user:pw@i.oneme.ru/x",
        "https://i.oneme.ru:8443/x",
        "https://127.0.0.1/x",
        "https://[::1]/x",
        "https://169.254.169.254/latest",
        "https://8.8.8.8/x",  # IP-литерал без явного allowlist
    ):
        with pytest.raises(Forbidden):
            d.check_url(bad)


def _gai(ip: str):
    def fake(host, port, family=0, type_=0, *a, **k):
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, port))]

    return fake


async def test_resolver_rejects_private_ip():
    r = VettingResolver(getaddrinfo=_gai("10.0.0.5"))
    with pytest.raises(Forbidden, match="внутренний"):
        await r.resolve("i.oneme.ru", 443)
    r = VettingResolver(getaddrinfo=_gai("169.254.169.254"))
    with pytest.raises(Forbidden):
        await r.resolve("i.oneme.ru", 443)
    ok = VettingResolver(getaddrinfo=_gai("93.184.216.34"))
    res = await ok.resolve("i.oneme.ru", 443)
    assert res[0]["host"] == "93.184.216.34" and res[0]["hostname"] == "i.oneme.ru"


async def test_download_to_private_ip_refused_without_connect(tmp_path: Path):
    d = _dl(getaddrinfo=_gai("192.168.1.1"))
    with pytest.raises(Forbidden):
        await d.fetch("https://i.oneme.ru/p", tmp_path / "f", max_bytes=100)
    assert not list(tmp_path.iterdir())  # ни файла, ни .part


def test_sniff_mime():
    assert sniff_mime(b"\x89PNG\r\n\x1a\nxxxx") == "image/png"
    assert sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_mime(b"????", "text/plain") == "text/plain"
    # заголовок сервера image/* без сигнатуры не превращает файл в картинку
    assert sniff_mime(b"<svg", "image/svg+xml") == "application/octet-stream"


# --- SafeDownloader на локальном http-сервере (loopback, без внешней сети) ---------


@pytest.fixture
async def local_server():
    from aiohttp import web

    async def big(request):
        return web.Response(body=b"x" * 5000)

    async def png(request):
        return web.Response(body=b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, content_type="image/png")

    async def to_png(request):
        raise web.HTTPFound("/png")

    async def to_evil(request):
        raise web.HTTPFound("http://evil.example/x")

    async def loop_(request):
        raise web.HTTPFound("/loop")

    async def chunked(request):
        resp = web.StreamResponse()
        await resp.prepare(request)
        for _ in range(10):
            await resp.write(b"y" * 1000)
        return resp

    app = web.Application()
    app.add_routes(
        [
            web.get("/big", big),
            web.get("/png", png),
            web.get("/to_png", to_png),
            web.get("/to_evil", to_evil),
            web.get("/loop", loop_),
            web.get("/chunked", chunked),
        ]
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://localhost:{port}"
    await runner.cleanup()


def _local_dl(**kw) -> SafeDownloader:
    return SafeDownloader(
        host_suffixes=("localhost",),
        require_https=False,
        getaddrinfo=_gai("127.0.0.1"),
        **kw,
    )


async def test_local_download_requires_explicit_allowlist(local_server, tmp_path):
    with pytest.raises(Forbidden):  # loopback без MAX_DOWNLOAD_ALLOW_IPS
        await _local_dl().fetch(local_server + "/png", tmp_path / "a", max_bytes=1000)
    d = _local_dl(allow_networks=parse_networks("127.0.0.0/8"))
    blob = await d.fetch(local_server + "/png", tmp_path / "a.png", max_bytes=1000)
    assert blob.mime == "image/png" and blob.size == 28
    assert oct((tmp_path / "a.png").stat().st_mode & 0o777) == "0o600"
    assert d.last_resolver is not None and d.last_resolver.resolved == [("localhost", "127.0.0.1")]


async def test_local_download_redirects(local_server, tmp_path):
    d = _local_dl(allow_networks=parse_networks("127.0.0.0/8"))
    blob = await d.fetch(local_server + "/to_png", tmp_path / "b", max_bytes=1000)
    assert blob.mime == "image/png"
    with pytest.raises(Forbidden, match="allowlist"):  # редирект на чужой хост
        await d.fetch(local_server + "/to_evil", tmp_path / "c", max_bytes=1000)
    with pytest.raises(Forbidden, match="перенаправлений"):
        await d.fetch(local_server + "/loop", tmp_path / "d", max_bytes=1000)
    assert not (tmp_path / "c").exists() and not (tmp_path / "d").exists()


async def test_local_download_size_limits(local_server, tmp_path):
    d = _local_dl(allow_networks=parse_networks("127.0.0.0/8"))
    with pytest.raises(InvalidArgument):  # по Content-Length
        await d.fetch(local_server + "/big", tmp_path / "e", max_bytes=1000)
    with pytest.raises(InvalidArgument):  # по потоку (chunked, без Content-Length)
        await d.fetch(local_server + "/chunked", tmp_path / "f", max_bytes=5000)
    assert list(tmp_path.iterdir()) == []


async def test_pymax_adapter_download_uses_safe_downloader(tmp_path):
    from max_mcp_server.adapter.pymax_adapter import PyMaxAdapter

    s = Settings(tz="UTC", state_dir=tmp_path / "st", download_dir=tmp_path / "dl")
    a = PyMaxAdapter(s)
    with pytest.raises(Forbidden):
        await a.download("https://evil.example/x", tmp_path / "dl" / "x", max_bytes=10)
    a._downloader.getaddrinfo = _gai("10.0.0.1")
    with pytest.raises(Forbidden):
        await a.download("https://i.oneme.ru/x", tmp_path / "dl" / "x", max_bytes=10)


async def test_resolver_dns_failure():
    def boom(*a, **k):
        raise socket.gaierror("no")

    with pytest.raises(UpstreamError):
        await VettingResolver(getaddrinfo=boom).resolve("i.oneme.ru", 443)


async def test_concurrent_downloads_use_unique_part_files(local_server, tmp_path, monkeypatch):
    """I6: .part с уникальным именем и O_EXCL — два скачивания одного ref не мешают друг другу."""
    import asyncio

    opened: list[tuple[str, int]] = []
    real_open = os.open

    def spy(path, flags, *a, **k):
        if str(path).endswith(".part"):
            opened.append((os.path.basename(path), flags))
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", spy)
    d = _local_dl(allow_networks=parse_networks("127.0.0.0/8"))
    dest = tmp_path / "same.png"
    a, b = await asyncio.gather(
        d.fetch(local_server + "/png", dest, max_bytes=1000),
        d.fetch(local_server + "/png", dest, max_bytes=1000),
    )
    assert a.size == b.size == 28 and dest.exists()
    assert len({name for name, _ in opened}) == 2
    assert all(flags & os.O_EXCL for _, flags in opened)
    assert [p.name for p in tmp_path.iterdir()] == ["same.png"]
