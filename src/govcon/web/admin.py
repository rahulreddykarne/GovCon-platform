"""Admin navigation and account-management endpoints."""

from typing import Annotated

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from govcon.collaboration.user_admin import manage_user
from govcon.collaboration.users import ROLES, require_permission
from govcon.db import session_scope
from govcon.models import User
from govcon.web.routes import (
    _WORKFLOW_ERRORS,
    _error_text,
    _NeedsLogin,
    _redirect,
    _render,
    _require_login,
)


def admin_page(request: Request) -> Response:
    from govcon.web.routes import ops, settings_page

    section = request.query_params.get("section", "operations")
    if section == "operations":
        return ops(request)
    if section == "settings":
        return settings_page(request)
    if section == "users":
        return admin_users(request)
    return HTMLResponse("Unknown Admin section", status_code=422)


def admin_users(request: Request) -> Response:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        require_permission(user, "manage_users")
    except _WORKFLOW_ERRORS as exc:
        return HTMLResponse(_error_text(exc), status_code=403)
    with session_scope() as db:
        users = db.scalars(select(User).order_by(User.is_active.desc(), User.email)).all()
        return _render(request, "admin_users.html", {"users": users, "roles": ROLES}, user)


def admin_user_action(request: Request, user_id: int, action: str,
                      role: Annotated[str | None, Form()] = None,
                      password: Annotated[str | None, Form()] = None) -> Response:
    try:
        actor = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        require_permission(actor, "manage_users")
        with session_scope() as db:
            manage_user(db, actor_id=actor.id, user_id=user_id, action=action, role=role, password=password)
    except _WORKFLOW_ERRORS as exc:
        return _redirect("/admin?section=users", request=request, error=_error_text(exc))
    return _redirect("/admin?section=users", request=request, notice="Account updated. Changed credentials or permissions require a new sign-in.")
