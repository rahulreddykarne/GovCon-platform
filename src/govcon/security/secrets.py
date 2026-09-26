"""Rules for credentials that must never be written to PostgreSQL."""

from __future__ import annotations

DISALLOWED_DATABASE_SECRET_KINDS = frozenset(
    {
        "sam_password",
        "piee_password",
        "portal_cookie",
        "mfa_secret",
        "browser_session_token",
    }
)


class SecretStorageError(RuntimeError):
    """Raised when code attempts to persist a portal credential in the database."""


def reject_database_secret(kind: str) -> None:
    """Refuse to store government-portal credentials in PostgreSQL."""
    if kind in DISALLOWED_DATABASE_SECRET_KINDS:
        raise SecretStorageError(
            f"{kind} must not be stored in PostgreSQL; "
            "use environment variables or a secret manager"
        )
