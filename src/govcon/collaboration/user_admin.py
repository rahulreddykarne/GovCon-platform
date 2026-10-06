"""Audited account management with session revocation and last-owner protection."""

from datetime import UTC, datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.collaboration.users import (
    ROLES,
    PermissionDenied,
    hash_password,
    require_permission,
)
from govcon.models import User, UserSession


def manage_user(session: Session, *, actor_id: int, user_id: int, action: str,
                role: str | None = None, password: str | None = None) -> User:
    if action not in {"role", "deactivate", "activate", "reset_password"}:
        raise ValueError("unknown user action")
    if action == "role" and role not in ROLES:
        raise ValueError("choose a valid role")
    hashed = hash_password(password or "") if action == "reset_password" else None
    # Serialize owner-count decisions, including edits to two different owners.
    session.execute(text("SELECT pg_advisory_xact_lock(742610061)"))
    rows = session.scalars(select(User).where(User.id.in_([actor_id, user_id]))
        .order_by(User.id).with_for_update().execution_options(populate_existing=True)).all()
    users = {user.id: user for user in rows}
    actor, target = users.get(actor_id), users.get(user_id)
    if actor is None or not actor.is_active:
        raise PermissionDenied("account is inactive")
    require_permission(actor, "manage_users")
    if target is None:
        raise ValueError("user not found")
    removing_owner = target.is_active and target.role == "owner" and (
        action == "deactivate" or (action == "role" and role != "owner"))
    if removing_owner and (session.scalar(select(func.count(User.id)).where(
            User.is_active.is_(True), User.role == "owner")) or 0) <= 1:
        raise ValueError("The last active owner cannot be deactivated or demoted.")
    before = {"role": target.role, "is_active": target.is_active}
    if action == "role":
        target.role = role or target.role
    elif action in {"activate", "deactivate"}:
        target.is_active = action == "activate"
    elif hashed is not None:
        target.password_hash = hashed
    if action != "activate":
        session.execute(update(UserSession).where(UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None)).values(revoked_at=datetime.now(UTC)))
    record_audit(session, action_type=f"user_{action}", user_id=actor.id, entity_type="user",
        entity_id=target.id, old_value=before, new_value={"role": target.role, "is_active": target.is_active})
    session.flush()
    return target
