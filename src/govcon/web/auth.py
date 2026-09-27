"""Session-based auth dependency and middleware helpers for FastAPI routes.

The session cookie stores a raw token. The server stores only the hash (SHA-256).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Cookie, Depends, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as OrmSession

from govcon.collaboration.users import user_for_token
from govcon.db import session_scope
from govcon.models import User


def _raw_token(session: str | None) -> str | None:
    return session if session else None


def get_current_user(
    request: Request,
    session: Annotated[str | None, Cookie(alias="govcon_session")] = None,
) -> User | None:
    """Return the authenticated User or None (does not redirect)."""
    raw = _raw_token(session)
    if not raw:
        return None
    with session_scope() as db:
        return user_for_token(db, raw)


def require_user(
    request: Request,
    session: Annotated[str | None, Cookie(alias="govcon_session")] = None,
) -> User:
    """Return the authenticated User; redirect to /login if not authenticated."""
    raw = _raw_token(session)
    if raw:
        with session_scope() as db:
            user = user_for_token(db, raw)
            if user:
                return user
    # Redirect rather than raise so we get a proper login page
    raise _redirect_to_login(request)


def _redirect_to_login(request: Request) -> Exception:
    from fastapi import HTTPException
    # We use a redirect response stored as an attribute on a minimal exception
    # so route handlers can catch it and return it.
    class _LoginRedirect(Exception):
        response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return _LoginRedirect()


RequireUser = Annotated[User, Depends(require_user)]
OptionalUser = Annotated[User | None, Depends(get_current_user)]
