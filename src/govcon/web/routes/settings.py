"""Settings endpoints and supporting views."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from govcon.collaboration.users import can
from govcon.db import session_scope
from govcon.models import CompanyRegistration, User
from govcon.web.routes.common import (
    _WORKFLOW_ERRORS,
    _actor,
    _error_text,
    _form_int,
    _NeedsLogin,
    _redirect,
    _render,
    _require_login,
)


def settings_page(request: Request) -> Response:
    """Owner-editable workflow settings; other roles see them read-only."""
    from govcon.db import current_settings as get_settings
    from govcon.workflow.app_settings import (
        AUTO_PREPARE,
        AUTO_PURSUE,
        DEADLINE_EXCEPTION,
        REVIEWER_ASSIGNMENT,
        get_setting,
    )

    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    with session_scope() as db:
        reviewers = db.scalars(
            select(User).where(User.is_active.is_(True), User.role.in_(("owner", "approver", "reviewer")))
            .order_by(User.display_name, User.email)
        ).all()
        ctx = {
            "reviewer_assignment": get_setting(db, REVIEWER_ASSIGNMENT),
            "auto_prepare": get_setting(db, AUTO_PREPARE),
            "auto_pursue": get_setting(db, AUTO_PURSUE),
            "deadline_exception": get_setting(db, DEADLINE_EXCEPTION),
            "short_deadline_days": get_settings().review_short_deadline_days,
            "authorizations": _authorizations(db),
            "registration": _our_registration(db),
            "ai_providers": ("anthropic", "openai", "deepseek"),
            "ai_sharing": {
                "proprietary": get_settings().ai_external_allowed_for_proprietary,
                "fci": get_settings().ai_external_allowed_for_fci,
                "cui": get_settings().ai_external_allowed_for_cui,
            },
            "reviewers": list(reviewers),
            "can_edit": can(user, "manage_users"),
            "active_page": "settings",
        }
    return _render(request, "settings.html", ctx, user)


def _our_registration(db: OrmSession) -> CompanyRegistration | None:
    """The configured company's registration (COMPANY_UEI or the facts file's uei)."""
    from govcon.company.registration import company_uei
    from govcon.compliance.pipeline import CompanyFactsInvalid, read_company_facts_file
    from govcon.db import current_settings

    settings = current_settings()
    try:
        facts = read_company_facts_file(settings)
    except CompanyFactsInvalid:
        facts = {}  # the page still opens; compliance and drafting report the broken file
    uei = company_uei(settings, facts)
    return db.get(CompanyRegistration, uei) if uei else None


def _authorizations(db: OrmSession) -> list[Any]:
    from govcon.models import AISharingAuthorization

    return list(db.scalars(select(AISharingAuthorization).order_by(AISharingAuthorization.id.desc()).limit(20)).all())


def settings_save(
    request: Request,
    reviewer_mode: Annotated[str, Form()],
    reviewer_ids: Annotated[list[str] | None, Form()] = None,
    auto_prepare: Annotated[str | None, Form()] = None,
    auto_pursue: Annotated[str | None, Form()] = None,
    auto_pursue_min_score: Annotated[str | None, Form()] = None,
    auto_pursue_min_days: Annotated[str | None, Form()] = None,
    auto_pursue_max_per_day: Annotated[str | None, Form()] = None,
    deadline_exception: Annotated[str | None, Form()] = None,
) -> Response:
    from govcon.workflow.app_settings import (
        AUTO_PREPARE,
        AUTO_PURSUE,
        DEADLINE_EXCEPTION,
        REVIEWER_ASSIGNMENT,
        get_setting,
        set_setting,
    )

    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        with session_scope() as db:
            actor = _actor(db, user, "manage_users")
            ids = [int(v) for v in (reviewer_ids or []) if str(v).strip().isdigit()]
            set_setting(db, REVIEWER_ASSIGNMENT, {"mode": reviewer_mode, "user_ids": ids}, actor=actor)
            set_setting(db, AUTO_PREPARE, {"enabled": auto_prepare == "on"}, actor=actor)
            current = get_setting(db, AUTO_PURSUE)
            set_setting(db, AUTO_PURSUE, {
                "enabled": auto_pursue == "on",
                "min_score": auto_pursue_min_score if auto_pursue_min_score not in (None, "") else current["min_score"],
                "min_days": auto_pursue_min_days if auto_pursue_min_days not in (None, "") else current["min_days"],
                "max_per_day": auto_pursue_max_per_day if auto_pursue_max_per_day not in (None, "") else current["max_per_day"],
            }, actor=actor)
            set_setting(db, DEADLINE_EXCEPTION, {"enabled": deadline_exception == "on"}, actor=actor)
    except _WORKFLOW_ERRORS as exc:
        return _redirect("/settings", error=_error_text(exc), request=request)
    return _redirect("/settings", notice="Settings saved.", request=request)


def ai_sharing_save(
    request: Request,
    action: Annotated[str, Form()],
    provider: Annotated[str | None, Form()] = None,
    days: Annotated[str | None, Form()] = None,
    reason: Annotated[str | None, Form()] = None,
    authorization_id: Annotated[str | None, Form()] = None,
) -> Response:
    """Owner: grant or revoke AI reading of supplier quotes for one provider (roadmap §6.4)."""
    from govcon.sourcing.records import grant_authorization, revoke_authorization

    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        with session_scope() as db:
            actor = _actor(db, user, "manage_users")
            if action == "grant":
                grant_authorization(db, provider=provider or "", days=_form_int(days) or 0, reason=reason or "",
                                    actor=actor)
                notice = "AI reading of supplier quotes authorized."
            elif action == "revoke":
                revoke_authorization(db, authorization_id=_form_int(authorization_id) or 0, actor=actor)
                notice = "Authorization revoked."
            else:
                raise ValueError("unknown action")
    except _WORKFLOW_ERRORS as exc:
        return _redirect("/settings", error=_error_text(exc), request=request)
    return _redirect("/settings", notice=notice, request=request)
