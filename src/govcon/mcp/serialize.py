"""Compact, secret-safe JSON serialization for MCP tool responses."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from govcon.audit import scrub

DEFAULT_DESCRIPTION_LIMIT = 500


def json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def truncate_text(value: str | None, *, limit: int = DEFAULT_DESCRIPTION_LIMIT) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def compact_opportunity(
    row: Any,
    *,
    include_full_description: bool = False,
    description_limit: int = DEFAULT_DESCRIPTION_LIMIT,
) -> dict[str, Any]:
    description = row.description
    if not include_full_description:
        description = truncate_text(description, limit=description_limit)
    return scrub(
        {
            "id": row.id,
            "source": row.source,
            "source_id": row.source_id,
            "solicitation_number": row.solicitation_number,
            "title": row.title,
            "description": description,
            "description_truncated": not include_full_description
            and row.description is not None
            and len(str(row.description)) > description_limit,
            "opportunity_type": row.opportunity_type,
            "psc_code": row.psc_code,
            "naics_code": row.naics_code,
            "set_aside_code": row.set_aside_code,
            "agency_path": row.agency_path,
            "nsn": row.nsn,
            "quantity": row.quantity,
            "unit": row.unit,
            "estimated_value_min": row.estimated_value_min,
            "estimated_value_max": row.estimated_value_max,
            "posted_date": row.posted_date,
            "response_deadline": row.response_deadline,
            "status": row.status,
            "links": row.links,
        }
    )


def success(payload: Any) -> dict[str, Any]:
    return {"ok": True, "data": scrub(json_safe(payload))}


def failure(code: str, message: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
    if extra:
        body["error"].update(scrub(json_safe(extra)))
    return body
