"""Database session and actor resolution for MCP tools."""

from __future__ import annotations

import os
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.collaboration.users import PermissionDenied
from govcon.config import get_settings
from govcon.db import session_scope
from govcon.mcp.serialize import failure
from govcon.models import User

F = TypeVar("F", bound=Callable[..., Any])


def resolve_actor_email(explicit: str | None = None) -> str | None:
    if explicit and explicit.strip():
        return explicit.strip()
    env_email = os.environ.get("MCP_ACTOR_EMAIL", "").strip()
    if env_email:
        return env_email
    settings = get_settings()
    configured = getattr(settings, "mcp_actor_email", None)
    if configured and str(configured).strip():
        return str(configured).strip()
    return None


def actor_for_email(session: Session, email: str | None) -> User:
    normalized = resolve_actor_email(email)
    if not normalized:
        raise ValueError(
            "actor_email is required (pass the parameter or set MCP_ACTOR_EMAIL)"
        )
    user = session.scalar(
        select(User).where(User.email == normalized.lower(), User.is_active.is_(True))
    )
    if user is None:
        raise ValueError(f"active user not found for email {normalized}")
    return user


def default_actor(session: Session) -> User:
    email = resolve_actor_email(None)
    if email:
        return actor_for_email(session, email)
    owner = session.scalar(
        select(User)
        .where(User.role == "owner", User.is_active.is_(True))
        .order_by(User.id)
        .limit(1)
    )
    if owner is not None:
        return owner
    any_user = session.scalar(
        select(User).where(User.is_active.is_(True)).order_by(User.id).limit(1)
    )
    if any_user is None:
        raise ValueError("no active users exist; invite a user before using write tools")
    return any_user


def mcp_tool(func: F) -> F:
    """Run an MCP operation inside a DB transaction with structured errors."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            with session_scope() as session:
                return func(session, *args, **kwargs)
        except ValueError as exc:
            return failure("validation_error", str(exc))
        except (PermissionError, PermissionDenied) as exc:
            return failure("permission_denied", str(exc))
        except LookupError as exc:
            return failure("not_found", str(exc))
        except Exception as exc:  # noqa: BLE001 - MCP must not leak stack traces
            name = exc.__class__.__name__
            return failure("operation_failed", f"{name}: {exc}")

    return wrapper  # type: ignore[return-value]
