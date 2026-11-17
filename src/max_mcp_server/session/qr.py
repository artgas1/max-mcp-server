"""QR для входа: ASCII в терминал и SVG-файл (атомарно, 0600). Без внешних URL."""

from __future__ import annotations

import io
from pathlib import Path
from typing import TextIO

import qrcode
import qrcode.image.svg

from max_mcp_server.session.store import write_private_bytes


def render_ascii(url: str, out: TextIO) -> None:
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(out=out, invert=True)


def render_svg(url: str) -> bytes:
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


def write_svg(url: str, path: Path) -> None:
    """Пишет SVG атомарно (страница с автообновлением не увидит полуфайл)."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private_bytes(path, render_svg(url))
