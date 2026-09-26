"""Append-only audit events. Secret fields are scrubbed before insert."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from govcon.models import AuditEvent

_SECRET_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "token",
        "token_hash",
        "api_key",
        "secret",
        "cookie",
        "smtp_pass",
        "authorization",
        "mfa_secret",
    }
)


def scrub(value: Any) -> Any:
    """Return a JSON-ready copy with secret keys removed."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in _SECRET_KEYS:
                cleaned[str(key)] = "[REDACTED]"
            else:
                cleaned[str(key)] = scrub(item)
        return cleaned
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def record_audit(
    session: Session,
    *,
    action_type: str,
    user_id: int | None = None,
    opportunity_id: int | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    old_value: dict | None = None,
    new_value: dict | None = None,
) -> AuditEvent:
    event = AuditEvent(
        user_id=user_id,
        opportunity_id=opportunity_id,
        action_type=action_type,
        entity_type=entity_type,
        entity_id=entity_id,
        old_value=scrub(old_value) if old_value is not None else None,
        new_value=scrub(new_value) if new_value is not None else None,
    )
    session.add(event)
    session.flush()
    return event
