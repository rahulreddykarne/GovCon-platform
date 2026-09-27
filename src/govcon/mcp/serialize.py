"""Compact JSON helpers for MCP tool responses.

Safety rules from Phase 12:
- compact structured output
- descriptions truncated unless explicitly requested
- secret keys scrubbed
- no stack traces in error payloads
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from govcon.audit import scrub

DEFAULT_DESCRIPTION_LIMIT = 280


def truncate_text(value: str | None, *, limit: int = DEFAULT_DESCRIPTION_LIMIT) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def to_jsonable(value: Any) -> Any:
    """Convert common Python/SQLAlchemy values into JSON-ready structures."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump(mode="json"))
    return str(value)


def ok(data: Any = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True}
    if data is not None:
        payload["data"] = scrub(to_jsonable(data))
    for key, value in extra.items():
        payload[key] = scrub(to_jsonable(value))
    return payload


def err(error_type: str, message: str, **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error": {
            "type": error_type,
            "message": str(message),
        },
    }
    if details:
        payload["error"]["details"] = scrub(to_jsonable(details))
    return payload


def not_implemented(tool: str, phase: int, message: str) -> dict[str, Any]:
    return err(
        "NotImplemented",
        message,
        tool=tool,
        phase=phase,
        status="not_implemented",
    )
