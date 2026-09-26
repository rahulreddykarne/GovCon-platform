"""Append-only audit events. Secret fields are removed before insert."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from govcon.models import AuditEvent

# Substrings matched against a normalized key (lower case, hyphens as underscores).
# A hit drops the key so passwords, hashes, and raw tokens never reach JSONB.
_SECRET_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "cookie",
    "authorization",
    "credential",
    "smtp_pass",
)


def _is_secret_key(key: str) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(marker in normalized for marker in _SECRET_MARKERS)


def scrub(value: Any) -> Any:
    """Return a JSON-ready copy with secret keys removed.

    Nested dicts and lists are walked. Non-secret fields are kept unchanged.
    """
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if _is_secret_key(key):
                continue
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
