"""Common endpoints and supporting views."""

from __future__ import annotations

import hashlib
import pathlib
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from govcon.collaboration.users import (
    AuthError,
    PermissionDenied,
    can,
    require_permission,
    user_for_token,
)
from govcon.compliance.submission_preflight import ReadinessBlocked
from govcon.concurrency import StaleRecordError
from govcon.db import session_scope
from govcon.models import Notification, User
from govcon.web.security import secure_cookies

_WORKFLOW_ERRORS = (ValueError, RuntimeError, AuthError, StaleRecordError)


_TEMPLATE_DIR = pathlib.Path(__file__).parent.parent / "templates"


_templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


def _money(value: Any, places: int = 2) -> str:
    """``$1,234.50`` — Python ``%`` formatting has no thousands separator."""
    try:
        return f"${float(value):,.{int(places)}f}"
    except (TypeError, ValueError):
        return "—"


def _add_template_globals(templates: Jinja2Templates) -> None:
    from govcon.web.security import csrf_token
    templates.env.globals["csrf_token"] = csrf_token
    templates.env.globals["can"] = can
    templates.env.globals["now"] = lambda: datetime.now(UTC)
    templates.env.filters["money"] = _money


_add_template_globals(_templates)


_COOKIE_NAME = "govcon_session"


def _current_user(request: Request) -> User | None:
    raw = request.cookies.get(_COOKIE_NAME)
    if not raw:
        return None
    with session_scope() as db:
        return user_for_token(db, raw)


def _require_login(request: Request) -> User:
    user = _current_user(request)
    if user is None:
        raise _NeedsLogin()
    return user


def _unread_count(user: User) -> int:
    with session_scope() as db:
        count = db.scalar(
            select(func.count()).select_from(Notification).where(
                Notification.user_id == user.id,
                Notification.read_at.is_(None),
            )
        )
        return count or 0


class _NeedsLogin(Exception):
    pass


def _flash_signer(request: Request) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(request.app.state.csrf_secret, salt="govcon-flash-v1")


def _flash_session(request: Request) -> str:
    return hashlib.sha256(request.cookies.get(_COOKIE_NAME, "").encode()).hexdigest()


def _redirect(url: str, *, request: Request, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    """303 redirect carrying an optional message for the next page."""
    params = []
    if error:
        params.append("error=1")
    if notice:
        params.append("notice=1")
    if params:
        url = url + ("&" if "?" in url else "?") + "&".join(params)
    response = RedirectResponse(url, status_code=303)
    if params:
        payload = {"session": _flash_session(request), "error": (error or "")[:600], "notice": (notice or "")[:300]}
        response.set_cookie("govcon_flash", _flash_signer(request).dumps(payload), max_age=180,
                            httponly=True, samesite="lax", secure=secure_cookies(request))
    return response


def _error_text(exc: Exception) -> str:
    if isinstance(exc, ReadinessBlocked):
        lines = [b.get("description", "") for b in exc.blockers]
        return "Submission readiness is blocked: " + " | ".join(lines)
    if isinstance(exc, PermissionDenied):
        return f"Not permitted: {exc}"
    return str(exc) or exc.__class__.__name__


def _actor(db: OrmSession, user: User, permission: str) -> User:
    """Reload the logged-in user in this transaction and check the permission."""
    actor = db.get(User, user.id)
    if actor is None or not actor.is_active:
        raise PermissionDenied("account is inactive")
    require_permission(actor, permission)
    return actor


def _form_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except ValueError:
        return None


def _render(request: Request, template: str, ctx: dict[str, Any], user: User) -> HTMLResponse:
    ctx["current_user"] = user
    ctx["unread_notification_count"] = _unread_count(user)
    if template in {"ops.html", "settings.html", "invite_user.html", "admin_users.html"}:
        ctx["active_page"] = "admin"
        ctx["admin_section"] = {"ops.html": "operations", "settings.html": "settings", "invite_user.html": "users", "admin_users.html": "users"}[template]
    ctx["source_options"] = [("sam", "SAM.gov"), ("dibbs", "DIBBS")]
    ctx["set_aside_options"] = [("SBA", "Small business"), ("8A", "8(a)"), ("HZC", "HUBZone"),
                                ("SDVOSBC", "Service-disabled veteran-owned"), ("WOSB", "Women-owned"),
                                ("EDWOSB", "Economically disadvantaged women-owned")]
    ctx["flash_error"] = ctx["flash_notice"] = None
    raw = request.cookies.get("govcon_flash")
    if raw:
        try:
            message = _flash_signer(request).loads(raw, max_age=180)
            if isinstance(message, dict) and message.get("session") == _flash_session(request):
                ctx["flash_error"] = message.get("error")
                ctx["flash_notice"] = message.get("notice")
        except BadSignature:
            pass
    response = _templates.TemplateResponse(request, template, ctx)
    if raw:
        response.delete_cookie("govcon_flash", httponly=True, samesite="lax", secure=secure_cookies(request))
    return response
