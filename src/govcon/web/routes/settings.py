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
    from govcon.company.strategy import missing_labels
    from govcon.db import current_settings as get_settings
    from govcon.workflow.app_settings import (
        AUTO_PREPARE,
        AUTO_PURSUE,
        COMPANY_STRATEGY,
        DEADLINE_EXCEPTION,
        OPERATOR_SCHEDULE,
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
            "schedule": _schedule_fields(get_setting(db, OPERATOR_SCHEDULE)),
            "strategy": _strategy_fields(get_setting(db, COMPANY_STRATEGY)),
            "strategy_missing": missing_labels(get_setting(db, COMPANY_STRATEGY)),
            "active_page": "settings",
        }
    return _render(request, "settings.html", ctx, user)


def _strategy_fields(stored: dict) -> list[dict[str, str]]:
    from govcon.company.strategy import FIELDS, display_value, missing_labels

    missing = set(missing_labels(stored))
    return [
        {
            "name": name,
            "label": label,
            "kind": kind,
            "value": display_value(stored, name),
            "missing": "yes" if label in missing else "",
        }
        for name, label, kind in FIELDS
    ]


def _schedule_fields(stored: dict) -> list[dict[str, str]]:
    from govcon.scheduler.schedule import JOBS, clock_label, effective_jobs

    jobs = effective_jobs(stored.get("jobs") if isinstance(stored, dict) else None)
    labels = {
        "morning_ingest": "Morning SAM and DIBBS",
        "usaspending": "USAspending",
        "embeddings": "Embeddings, after morning ingest",
        "midday_check": "Midday deadline check",
        "evening_ingest": "Evening SAM and DIBBS",
        "sunday_sweep": "Sunday sweep",
    }
    fields = []
    for name in JOBS:
        spec = jobs[name]
        fields.append({
            "name": name,
            "label": labels[name],
            "value": f"{int(spec['hour']):02d}:{int(spec['minute']):02d}",
            "shown": clock_label(int(spec["hour"]), int(spec["minute"])),
        })
    return fields


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
    schedule_morning_ingest: Annotated[str | None, Form()] = None,
    schedule_usaspending: Annotated[str | None, Form()] = None,
    schedule_embeddings: Annotated[str | None, Form()] = None,
    schedule_midday_check: Annotated[str | None, Form()] = None,
    schedule_evening_ingest: Annotated[str | None, Form()] = None,
    schedule_sunday_sweep: Annotated[str | None, Form()] = None,
    strategy_form: Annotated[str | None, Form()] = None,
    strategy_products: Annotated[str | None, Form()] = None,
    strategy_naics_codes: Annotated[str | None, Form()] = None,
    strategy_psc_codes: Annotated[str | None, Form()] = None,
    strategy_nsns: Annotated[str | None, Form()] = None,
    strategy_certifications: Annotated[str | None, Form()] = None,
    strategy_agencies: Annotated[str | None, Form()] = None,
    strategy_geography: Annotated[str | None, Form()] = None,
    strategy_past_performance: Annotated[str | None, Form()] = None,
    strategy_suppliers: Annotated[str | None, Form()] = None,
    strategy_bid_capacity: Annotated[str | None, Form()] = None,
    strategy_pricing_assumptions: Annotated[str | None, Form()] = None,
    strategy_minimum_margin_pct: Annotated[str | None, Form()] = None,
) -> Response:
    from govcon.company.strategy import parse_form
    from govcon.workflow.app_settings import (
        AUTO_PREPARE,
        AUTO_PURSUE,
        COMPANY_STRATEGY,
        DEADLINE_EXCEPTION,
        OPERATOR_SCHEDULE,
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
            clocks = {
                "morning_ingest": schedule_morning_ingest,
                "usaspending": schedule_usaspending,
                "embeddings": schedule_embeddings,
                "midday_check": schedule_midday_check,
                "evening_ingest": schedule_evening_ingest,
                "sunday_sweep": schedule_sunday_sweep,
            }
            set_setting(db, OPERATOR_SCHEDULE, {"jobs": _parse_clocks(clocks)}, actor=actor)
            if strategy_form == "on":
                posted = {
                    "products": strategy_products,
                    "naics_codes": strategy_naics_codes,
                    "psc_codes": strategy_psc_codes,
                    "nsns": strategy_nsns,
                    "certifications": strategy_certifications,
                    "agencies": strategy_agencies,
                    "geography": strategy_geography,
                    "past_performance": strategy_past_performance,
                    "suppliers": strategy_suppliers,
                    "bid_capacity": strategy_bid_capacity,
                    "pricing_assumptions": strategy_pricing_assumptions,
                    "minimum_margin_pct": strategy_minimum_margin_pct,
                }
                set_setting(db, COMPANY_STRATEGY, parse_form(posted), actor=actor)
    except _WORKFLOW_ERRORS as exc:
        return _redirect("/settings", error=_error_text(exc), request=request)
    return _redirect("/settings", notice="Settings saved.", request=request)


def _parse_clocks(clocks: dict[str, str | None]) -> dict[str, dict[str, int]]:
    jobs: dict[str, dict[str, int]] = {}
    for name, raw in clocks.items():
        text = (raw or "").strip()
        if not text:
            continue
        hour_text, _, minute_text = text.partition(":")
        if not hour_text.isdigit() or not minute_text.isdigit():
            raise ValueError(f"{name.replace('_', ' ')} needs a time like 06:30")
        jobs[name] = {"hour": int(hour_text), "minute": int(minute_text)}
    return jobs


def model_route_save(
    request: Request,
    action: Annotated[str, Form()],
    provider: Annotated[str | None, Form()] = None,
    model: Annotated[str | None, Form()] = None,
    version: Annotated[str | None, Form()] = None,
) -> Response:
    """Save a new analysis route version, or restore an older one. Does not call a model."""
    from govcon.ai.routing import rollback_route, save_analysis_route
    from govcon.db import current_settings

    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        with session_scope() as db:
            actor = _actor(db, user, "manage_users")
            if action == "save":
                save_analysis_route(db, current_settings(), provider=provider or "", model=model or "", actor=actor)
                notice = "Saved a new model route version. No provider was called."
            elif action == "rollback":
                rollback_route(db, version=version or "", actor=actor)
                notice = "Restored that model route version. No provider was called."
            else:
                raise ValueError("unknown action")
    except _WORKFLOW_ERRORS as exc:
        return _redirect("/operate/integrations", error=_error_text(exc), request=request)
    return _redirect("/operate/integrations", notice=notice, request=request)


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
