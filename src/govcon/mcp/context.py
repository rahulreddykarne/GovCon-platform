"""Database session and actor resolution for MCP tools.

The MCP actor is fixed when the server starts (``MCP_ACTOR_EMAIL``). Tools
never accept an actor or user email per call, and there is no fallback to an
owner account: a prompt-injected or misbehaving client cannot act as someone
else. Write tools run as that one configured user and its role permissions.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.collaboration.users import PermissionDenied, require_permission
from govcon.config import get_settings
from govcon.db import session_scope
from govcon.mcp.serialize import failure
from govcon.models import User

F = TypeVar("F", bound=Callable[..., Any])

_SERVER_ACTOR_EMAIL: str | None = None


def configured_actor_email() -> str | None:
    """The actor fixed at server start, else ``MCP_ACTOR_EMAIL`` from the environment/settings."""
    if _SERVER_ACTOR_EMAIL:
        return _SERVER_ACTOR_EMAIL
    env_email = os.environ.get("MCP_ACTOR_EMAIL", "").strip()
    if env_email:
        return env_email.lower()
    configured = getattr(get_settings(), "mcp_actor_email", None)
    if configured and str(configured).strip():
        return str(configured).strip().lower()
    return None


def configure_server_actor(email: str | None = None) -> str:
    """Pin the MCP actor for the life of the server process and verify it exists."""
    global _SERVER_ACTOR_EMAIL
    resolved = (email or configured_actor_email() or "").strip().lower()
    if not resolved:
        raise ValueError("MCP_ACTOR_EMAIL must be set before starting the MCP server")
    with session_scope() as session:
        _active_user(session, resolved)
    _SERVER_ACTOR_EMAIL = resolved
    return resolved


def reset_server_actor() -> None:
    """Forget the pinned actor (tests only)."""
    global _SERVER_ACTOR_EMAIL
    _SERVER_ACTOR_EMAIL = None


def _active_user(session: Session, email: str) -> User:
    user = session.scalar(
        select(User).where(User.email == email.strip().lower(), User.is_active.is_(True))
    )
    if user is None:
        raise ValueError(f"MCP actor {email} is not an active user")
    return user


def current_actor(session: Session, permission: str | None = None) -> User:
    """The configured MCP actor, loaded in this transaction, with an optional permission check."""
    email = configured_actor_email()
    if not email:
        raise PermissionDenied(
            "MCP write tools are disabled: set MCP_ACTOR_EMAIL to the user the MCP client acts as"
        )
    user = _active_user(session, email)
    if permission is not None:
        require_permission(user, permission)
    return user


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
