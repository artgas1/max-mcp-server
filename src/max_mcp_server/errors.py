"""Иерархия ошибок сервера. Тексты — для человека/модели, без внутренних payload."""

from __future__ import annotations

# коды выхода CLI (design.md, 7.2)
EXIT_OK = 0
EXIT_NO_SESSION = 2
EXIT_CONFIG = 3
EXIT_NETWORK = 4


class MaxMcpError(Exception):
    """Базовая ошибка. ``public_message`` безопасно показывать модели."""

    code = "error"
    exit_code = 1

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.public_message = message


class ConfigError(MaxMcpError):
    code = "config_error"
    exit_code = EXIT_CONFIG


class InsecurePermissions(ConfigError):
    code = "insecure_permissions"


class NotLoggedIn(MaxMcpError):
    code = "not_logged_in"
    exit_code = EXIT_NO_SESSION

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message or "Вход в MAX не выполнен. Запустите в терминале: max-mcp login qr"
        )


class SessionExpired(MaxMcpError):
    code = "session_expired"
    exit_code = EXIT_NO_SESSION

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or "Сессия MAX отозвана или истекла. Войдите заново в терминале: max-mcp login qr"
        )


class ProfileLocked(MaxMcpError):
    code = "profile_locked"
    exit_code = EXIT_CONFIG


class RateLimited(MaxMcpError):
    code = "rate_limited"

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Forbidden(MaxMcpError):
    code = "forbidden"


class NotFound(MaxMcpError):
    code = "not_found"


class InvalidArgument(MaxMcpError):
    code = "invalid_argument"


class Unsupported(MaxMcpError):
    code = "unsupported"


class UpstreamError(MaxMcpError):
    code = "upstream_error"
    exit_code = EXIT_NETWORK

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
