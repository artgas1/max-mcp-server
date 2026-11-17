from __future__ import annotations

import json
from typing import Any


def payload(result: Any) -> Any:
    """Структурированный ответ инструмента (или JSON из текстового блока)."""
    assert not result.is_error, _text(result)
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(_text(result))


def _text(result: Any) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


def error_text(result: Any) -> str:
    assert result.is_error, "ожидалась ошибка инструмента"
    return _text(result)
