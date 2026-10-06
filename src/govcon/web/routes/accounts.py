"""Accounts endpoints and supporting views."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from govcon.collaboration.notifications import ACTION_REQUIRED_TYPES
from govcon.collaboration.users import (
    AuthError,
    PermissionDenied,
    authenticate,
    create_session,
    invite_user,
    logout,
    require_permission,
)
from govcon.db import session_scope
from govcon.models import Notification
from govcon.web.presentation import notification_summary
from govcon.web.routes.common import (
    _COOKIE_NAME,
    _actor,
    _current_user,
    _NeedsLogin,
    _redirect,
    _render,
    _require_login,
    _templates,
)
from govcon.web.security import secure_cookies


def login_get(request: Request) -> Response:
    if _current_user(request):
        return RedirectResponse("/", status_code=303)
    return _templates.TemplateResponse(request, "login.html", {"error": None})


def login_post(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    with session_scope() as db:
        user = authenticate(db, email, password)
        if user is None:
            return _templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Invalid email or password.", "prefill_email": email},
                status_code=401,
            )
        raw_token = create_session(db, user, settings=request.app.state.settings)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        _COOKIE_NAME,
        raw_token,
        max_age=request.app.state.settings.session_ttl_hours * 3600,
        httponly=True,
        secure=secure_cookies(request),
        samesite="lax",
    )
    return resp


def logout_post(request: Request) -> Response:
    raw = request.cookies.get(_COOKIE_NAME)
    if raw:
        with session_scope() as db:
            logout(db, raw)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(_COOKIE_NAME)
    return resp


def notifications(request: Request, before: Annotated[int | None, Query(ge=1)] = None) -> Response:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    with session_scope() as db:
        query = select(Notification).where(Notification.user_id == user.id)
        if before is not None:
            query = query.where(Notification.id < before)
        rows = db.scalars(query.order_by(Notification.id.desc()).limit(51)).all()
        next_before = rows[49].id if len(rows) > 50 else None
        items = [{"id": row.id, "title": row.notification_type.replace("_", " ").capitalize(),
                  "opportunity_id": row.opportunity_id, "created_at": row.created_at,
                  "sentence": notification_summary(row)[0], "target": notification_summary(row)[1],
                  "unread": row.read_at is None,
                  "needs_ack": row.notification_type in ACTION_REQUIRED_TYPES and row.acknowledged_at is None,
                  "acknowledged_at": row.acknowledged_at} for row in rows[:50]]
    return _render(request, "notifications.html", {"notifications": items, "next_before": next_before, "active_page": "notifications"}, user)


def notification_read(request: Request, notification_id: int) -> RedirectResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    with session_scope() as db:
        row = db.scalar(select(Notification).where(
            Notification.id == notification_id, Notification.user_id == user.id).with_for_update())
        if row is None:
            raise HTTPException(status_code=404, detail="Notification not found")
        if row.read_at is None:
            row.read_at = datetime.now(UTC)
    return _redirect("/notifications", request=request)


def notification_acknowledge(request: Request, notification_id: int) -> RedirectResponse:
    """Acknowledge an action-required notification (ADR-070)."""
    from govcon.collaboration.notifications import acknowledge

    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        with session_scope() as db:
            acknowledge(db, notification_id=notification_id, user=_actor(db, user, "read"))
    except AuthError:
        return _redirect("/login", error="Account access changed; sign in again.", request=request)
    except ValueError:
        raise HTTPException(status_code=404, detail="Notification not found")
    return _redirect("/notifications", notice="Acknowledged.", request=request)


def admin_invite_get(request: Request) -> Response:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        require_permission(user, "manage_users")
    except PermissionDenied:
        return RedirectResponse("/ops", status_code=303)
    return _render(request, "invite_user.html", {"active_page": "ops"}, user)


async def admin_invite_post(request: Request) -> Response:
    try:
        user = await run_in_threadpool(_require_login, request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        require_permission(user, "manage_users")
    except PermissionDenied:
        return RedirectResponse("/ops", status_code=303)

    form = await request.form()
    values: dict[str, str] = {}
    for field in ("email", "display_name", "password", "role"):
        value = form.get(field)
        if value is not None and not isinstance(value, str):
            await form.close()
            response = _render(request, "invite_user.html", {"error": f"{field} must be text", "active_page": "ops"}, user)
            response.status_code = 400
            return response
        values[field] = value or ""
    email = values["email"].strip()
    display_name = values["display_name"].strip()
    password = values["password"]
    role = (values["role"] or "reviewer").strip()

    def _invite() -> HTMLResponse:  # password hashing and database work stay off the event loop
        try:
            with session_scope() as db:
                invite_user(db, email=email, display_name=display_name, password=password, role=role,
                            actor_user_id=user.id)
            return _render(request, "invite_user.html", {"success": f"User {email} invited.", "active_page": "ops"}, user)
        except (ValueError, AuthError) as exc:
            return _render(request, "invite_user.html", {"error": str(exc), "active_page": "ops"}, user)

    try:
        return await run_in_threadpool(_invite)
    finally:
        await form.close()
