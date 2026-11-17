"""Настройки из переменных окружения ``MAX_*`` (и пары исторических имён без префикса)."""

from __future__ import annotations

import os
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from max_mcp_server.errors import ConfigError

APP_DIR = "max-mcp-server"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _xdg(var: str, fallback: str) -> Path:
    raw = os.environ.get(var)
    return Path(raw) if raw else Path.home() / fallback


def parse_id_list(raw: str) -> frozenset[int]:
    """``"1, -2,3"`` → {1, -2, 3}. Пустая строка → пустое множество."""
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError as exc:
            raise ConfigError(f"Неверный id чата в списке: {part!r}") from exc
    return frozenset(out)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MAX_", extra="ignore", populate_by_name=True, frozen=False
    )

    profile: str = "default"
    state_dir: Path | None = None
    download_dir: Path | None = None
    upload_dir: Path | None = None

    client: Literal["web", "mobile"] = "web"
    read_only: bool = True

    read_chats: str = ""
    write_chats: str = ""
    deny_chats: str = ""

    rps: float = Field(default=5.0, gt=0, le=50)
    untrusted_wrap: Literal["tags", "json", "off"] = "tags"
    text_limit: int = Field(default=4000, ge=100, le=100_000)
    tz: str | None = None
    appear_online: bool = False
    show_own_phone: bool = False

    download_max_bytes: int = 50 * 1024 * 1024
    inline_image_bytes: int = 1024 * 1024
    download_hosts: str = ""

    http_host: str = "127.0.0.1"
    http_port: int = Field(default=13430, ge=1, le=65535)
    http_path: str = "/mcp"
    http_token: str | None = None
    http_token_file: Path | None = None
    http_allow_nonlocal: bool = False
    http_allowed_origins: str = ""

    login_token: str | None = None
    phone: str | None = None
    twofa_password_file: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("MAX_2FA_PASSWORD_FILE", "twofa_password_file"),
    )
    qr_svg: Path | None = Field(default=None, validation_alias=AliasChoices("QR_SVG", "qr_svg"))

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_content: bool = False
    log_pymax_debug: bool = False

    request_timeout: float = Field(default=30.0, gt=0, le=300)
    connect_timeout: float = Field(default=20.0, gt=0, le=300)

    @field_validator("profile")
    @classmethod
    def _profile_safe(cls, v: str) -> str:
        if not v or not all(c.isalnum() or c in "-_." for c in v) or v.startswith("."):
            raise ValueError("MAX_PROFILE: только буквы, цифры и -_.")
        return v

    @field_validator("http_path")
    @classmethod
    def _path_slash(cls, v: str) -> str:
        return v if v.startswith("/") else "/" + v

    # --- производные значения -------------------------------------------------

    @property
    def state_path(self) -> Path:
        if self.state_dir is not None:
            return self.state_dir.expanduser()
        return _xdg("XDG_STATE_HOME", ".local/state") / APP_DIR / self.profile

    @property
    def download_path(self) -> Path:
        if self.download_dir is not None:
            return self.download_dir.expanduser()
        return _xdg("XDG_CACHE_HOME", ".cache") / APP_DIR / self.profile / "media"

    @property
    def read_allow(self) -> frozenset[int]:
        return parse_id_list(self.read_chats)

    @property
    def write_allow(self) -> frozenset[int] | Literal["*"]:
        if self.write_chats.strip() == "*":
            return "*"
        return parse_id_list(self.write_chats)

    @property
    def deny(self) -> frozenset[int]:
        return parse_id_list(self.deny_chats)

    @property
    def timezone(self) -> tzinfo:
        if self.tz:
            try:
                return ZoneInfo(self.tz)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ConfigError(f"MAX_TZ: неизвестный часовой пояс {self.tz!r}") from exc
        local = datetime.now().astimezone().tzinfo
        assert local is not None
        return local

    @property
    def download_host_suffixes(self) -> tuple[str, ...]:
        extra = tuple(
            h.strip().lower().lstrip(".") for h in self.download_hosts.split(",") if h.strip()
        )
        return DEFAULT_DOWNLOAD_HOSTS + extra

    def validate_runtime(self) -> None:
        """Проверки, которые нельзя выразить валидаторами полей."""
        _ = self.read_allow, self.write_allow, self.deny, self.timezone


# Хосты CDN MAX — предварительно, сверить на живом аккаунте (docs/protocol-notes.md).
DEFAULT_DOWNLOAD_HOSTS: tuple[str, ...] = ("oneme.ru", "max.ru", "okcdn.ru", "mycdn.me")


def load_settings(**overrides: object) -> Settings:
    try:
        settings = Settings(**overrides)  # type: ignore[arg-type]
        settings.validate_runtime()
    except ConfigError:
        raise
    except ValueError as exc:
        raise ConfigError(f"Ошибка конфигурации: {exc}") from exc
    return settings
