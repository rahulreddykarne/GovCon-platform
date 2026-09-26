"""Invite-only users, password hashing, sessions, and role checks.

There is no self-registration path. Sessions store a hash of the token.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.config import Settings, get_settings
from govcon.models import User, UserSession

ROLES = ("owner", "approver", "reviewer", "read_only")
_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset({"read", "review", "approve", "manage_users", "override_review", "override_compliance"}),
    "approver": frozenset({"read", "review", "approve", "override_review", "override_compliance"}),
    "reviewer": frozenset({"read", "review"}),
    "read_only": frozenset({"read"}),
}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_hasher = PasswordHasher()
MIN_PASSWORD_LENGTH = 12


class AuthError(Exception):
    """Base authentication or authorization failure."""


class PermissionDenied(AuthError):
    """The user's role does not allow the action."""


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def _normalize_email(email: str) -> str:
    cleaned = email.strip().lower()
    if not _EMAIL.fullmatch(cleaned):
        raise ValueError("email is not valid")
    return cleaned


def invite_user(
    session: Session,
    *,
    email: str,
    display_name: str,
    password: str,
    role: str = "reviewer",
    actor_user_id: int | None = None,
) -> User:
    """Create a user. This is the only user-creation path."""
    if role not in ROLES:
        raise ValueError(f"role must be one of: {', '.join(ROLES)}")
    name = display_name.strip()
    if not name:
        raise ValueError("display_name is required")
    normalized = _normalize_email(email)
    existing = session.scalar(select(User).where(User.email == normalized))
    if existing is not None:
        raise ValueError("a user with that email already exists")
    user = User(
        email=normalized,
        display_name=name,
        password_hash=hash_password(password),
        role=role,
        is_active=True,
    )
    session.add(user)
    session.flush()
    record_audit(
        session,
        action_type="user_invited",
        user_id=actor_user_id,
        entity_type="user",
        entity_id=user.id,
        new_value={"email": user.email, "role": user.role},
    )
    return user


def authenticate(session: Session, email: str, password: str) -> User | None:
    """Return the user when the password matches and the account is active."""
    try:
        normalized = _normalize_email(email)
    except ValueError:
        return None
    user = session.scalar(select(User).where(User.email == normalized))
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def deactivate_user(session: Session, user: User, *, actor_user_id: int | None = None) -> None:
    previous = user.is_active
    user.is_active = False
    now = datetime.now(UTC)
    sessions = session.scalars(
        select(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
    ).all()
    for stored in sessions:
        stored.revoked_at = now
    record_audit(
        session,
        action_type="user_deactivated",
        user_id=actor_user_id,
        entity_type="user",
        entity_id=user.id,
        old_value={"is_active": previous},
        new_value={"is_active": False},
    )


def create_session(session: Session, user: User, settings: Settings | None = None) -> str:
    """Open a session and return the raw token once. Only the hash is stored."""
    if not user.is_active:
        raise AuthError("inactive users cannot start a session")
    settings = settings or get_settings()
    raw = secrets.token_urlsafe(32)
    stored = UserSession(
        user_id=user.id,
        token_hash=_hash_token(raw),
        expires_at=datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours),
    )
    session.add(stored)
    session.flush()
    record_audit(
        session,
        action_type="session_created",
        user_id=user.id,
        entity_type="user_session",
        entity_id=stored.id,
        new_value={"user_id": user.id},
    )
    return raw


def logout(session: Session, raw_token: str) -> bool:
    stored = _session_for_token(session, raw_token)
    if stored is None or stored.revoked_at is not None:
        return False
    stored.revoked_at = datetime.now(UTC)
    record_audit(
        session,
        action_type="session_revoked",
        user_id=stored.user_id,
        entity_type="user_session",
        entity_id=stored.id,
    )
    return True


def user_for_token(session: Session, raw_token: str) -> User | None:
    stored = _session_for_token(session, raw_token)
    if stored is None or stored.revoked_at is not None:
        return None
    expires_at = stored.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        return None
    user = session.get(User, stored.user_id)
    if user is None or not user.is_active:
        return None
    stored.last_seen_at = datetime.now(UTC)
    return user


def require_permission(user: User, action: str) -> None:
    allowed = _PERMISSIONS.get(user.role, frozenset())
    if action not in allowed:
        raise PermissionDenied(f"role {user.role} cannot {action}")


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _session_for_token(session: Session, raw_token: str) -> UserSession | None:
    if not raw_token:
        return None
    return session.scalar(select(UserSession).where(UserSession.token_hash == _hash_token(raw_token)))
