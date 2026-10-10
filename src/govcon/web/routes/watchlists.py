"""Watchlists endpoints and supporting views."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from govcon.audit import record_audit
from govcon.collaboration.users import PermissionDenied, require_permission
from govcon.db import session_scope
from govcon.models import Match, User, Watchlist
from govcon.web.presentation import ViewRow
from govcon.web.routes.common import (
    _WORKFLOW_ERRORS,
    _actor,
    _error_text,
    _NeedsLogin,
    _redirect,
    _render,
    _require_login,
)
from govcon.workflow.invalidation import lock_one


def watchlists(request: Request) -> Response:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    with session_scope() as db:
        wls = db.scalars(select(Watchlist).order_by(Watchlist.name)).all()
        counts = dict(db.execute(select(Match.watchlist_id, func.count(Match.id))
            .where(Match.active.is_(True)).group_by(Match.watchlist_id)).all())
        return _render(request, "watchlists.html", {"watchlists": list(wls), "match_counts": counts, "active_page": "watchlists"}, user)


def watchlist_new_get(request: Request) -> Response:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    return _render(request, "watchlist_edit.html", {"editing": False, "wl": None, "active_page": "watchlists"}, user)


async def watchlist_new_post(request: Request) -> Response:
    try:
        user = await run_in_threadpool(_require_login, request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        require_permission(user, "manage_watchlists")
    except PermissionDenied as exc:
        return _redirect("/watchlists", error=_error_text(exc), request=request)
    return await _watchlist_save(request, user, wl_id=None)


def watchlist_edit_get(request: Request, wl_id: int) -> Response:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    with session_scope() as db:
        wl = db.get(Watchlist, wl_id)
        if wl is None:
            return HTMLResponse("Not found", status_code=404)
        return _render(request, "watchlist_edit.html", {"editing": True, "wl": wl, "active_page": "watchlists"}, user)


async def watchlist_edit_post(request: Request, wl_id: int) -> Response:
    try:
        user = await run_in_threadpool(_require_login, request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        require_permission(user, "manage_watchlists")
    except PermissionDenied as exc:
        return _redirect("/watchlists", error=_error_text(exc), request=request)
    return await _watchlist_save(request, user, wl_id=wl_id)


async def _watchlist_save(request: Request, user: User, wl_id: int | None) -> Response:
    form = await request.form()
    return await run_in_threadpool(_watchlist_save_sync, request, user, wl_id, form)


def _watchlist_save_sync(request: Request, user: User, wl_id: int | None, form: Any) -> Response:
    from decimal import Decimal, InvalidOperation

    name = (form.get("name") or "").strip()

    def parse_list(val: str | None) -> list[str] | None:
        if not val or not val.strip():
            return None
        return [x.strip() for x in val.split(",") if x.strip()]

    def parse_checks(key: str) -> list[str] | None:
        values = [value for raw in form.getlist(key) for value in (parse_list(str(raw)) or [])]
        return list(dict.fromkeys(values)) or None

    errors: list[str] = []
    if not name:
        errors.append("Name is required.")
    values: dict[str, Decimal | None] = {}
    for key in ("min_value", "max_value"):
        raw = str(form.get(key) or "").strip()
        if not raw:
            values[key] = None
            continue
        try:
            value = Decimal(raw)
        except InvalidOperation:
            errors.append("Enter a finite dollar amount.")
            values[key] = None
            continue
        if not value.is_finite() or value < 0:
            errors.append("Enter a finite, non-negative dollar amount.")
            values[key] = None
            continue
        exponent = value.as_tuple().exponent
        if not isinstance(exponent, int) or value.adjusted() > 131_071 or exponent < -16_383:
            errors.append("Enter a dollar amount within the supported numeric range.")
            values[key] = None
            continue
        values[key] = value
    minimum, maximum = values["min_value"], values["max_value"]
    if minimum is not None and maximum is not None and minimum > maximum:
        errors.append("Minimum value cannot exceed maximum value.")
    days_raw = str(form.get("min_deadline_days") or "").strip()
    days: int | None = None
    if days_raw and (not days_raw.isascii() or not days_raw.isdigit() or len(days_raw) > 10 or int(days_raw) > 2_147_483_647):
        errors.append("Minimum deadline days must be a nonnegative whole number within the supported range.")
    elif days_raw:
        days = int(days_raw)
    if errors or not name:
        preserved: dict[str, Any] = {key: str(form.get(key) or "") for key in ("name", "notes", "min_value", "max_value", "min_deadline_days")}
        preserved.update({key: parse_list(form.get(key)) for key in ("psc_codes", "naics_codes", "keywords", "exclude_keywords", "nsn_list", "set_asides", "sources")})
        preserved.update({key: parse_checks(key) for key in ("set_asides", "sources")})
        message = " ".join(dict.fromkeys(errors)) if errors else "Name is required."
        response = _render(request, "watchlist_edit.html", {
            "editing": wl_id is not None, "wl": ViewRow({"id": wl_id, **preserved}),
            "error": message, "active_page": "watchlists",
        }, user)
        response.status_code = 422
        return response

    with session_scope() as db:
        actor = _actor(db, user, "manage_watchlists")
        if wl_id:
            wl = lock_one(db, select(Watchlist).where(Watchlist.id == wl_id))
            if wl is None:
                return HTMLResponse("Not found", status_code=404)
            old_value = {
                "name": wl.name,
                "psc_codes": wl.psc_codes,
                "naics_codes": wl.naics_codes,
                "keywords": wl.keywords,
                "exclude_keywords": wl.exclude_keywords,
                "nsn_list": wl.nsn_list,
                "set_asides": wl.set_asides,
                "sources": wl.sources,
            }
        else:
            wl = Watchlist()
            db.add(wl)
            old_value = None

        wl.name = name
        wl.psc_codes = parse_list(form.get("psc_codes"))
        wl.naics_codes = parse_list(form.get("naics_codes"))
        wl.keywords = parse_list(form.get("keywords"))
        wl.exclude_keywords = parse_list(form.get("exclude_keywords"))
        wl.nsn_list = parse_list(form.get("nsn_list"))
        wl.set_asides = parse_checks("set_asides")
        wl.min_value = minimum
        wl.max_value = maximum
        wl.min_deadline_days = days
        wl.sources = parse_checks("sources")
        wl.notes = (form.get("notes") or "").strip() or None
        db.flush()
        from govcon.matching.engine import run_matching

        run_matching(db, watchlist_id=wl.id, rebuild=True)
        record_audit(
            db,
            action_type="watchlist_updated" if wl_id else "watchlist_created",
            user_id=actor.id,
            entity_type="watchlists",
            entity_id=wl.id,
            old_value=old_value,
            new_value={
                "name": wl.name,
                "psc_codes": wl.psc_codes,
                "naics_codes": wl.naics_codes,
                "keywords": wl.keywords,
                "exclude_keywords": wl.exclude_keywords,
                "nsn_list": wl.nsn_list,
                "set_asides": wl.set_asides,
                "sources": wl.sources,
            },
        )

    return RedirectResponse("/watchlists", status_code=303)


def watchlist_delete(request: Request, wl_id: int) -> Response:
    from sqlalchemy import delete
    from sqlalchemy.exc import IntegrityError

    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        with session_scope() as db:
            actor = _actor(db, user, "manage_watchlists")
            wl = lock_one(db, select(Watchlist).where(Watchlist.id == wl_id))
            if wl is None:
                return HTMLResponse("Watchlist not found", status_code=404)
            record_audit(db, action_type="watchlist_deleted", user_id=actor.id,
                         entity_type="watchlists", entity_id=wl.id, old_value={"name": wl.name})
            db.execute(delete(Match).where(Match.watchlist_id == wl.id))
            db.delete(wl)
    except IntegrityError:
        return _redirect("/watchlists", request=request, error="Matches changed while deleting the watchlist. Retry the deletion.")
    except _WORKFLOW_ERRORS as exc:
        return _redirect("/watchlists", request=request, error=_error_text(exc))
    return _redirect("/watchlists", request=request, notice="Watchlist deleted. Pursuits remain in Pipeline.")


def watchlist_toggle(
    request: Request,
    wl_id: int,
    enabled: Annotated[str, Form()],
) -> Response:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    try:
        with session_scope() as db:
            actor = _actor(db, user, "manage_watchlists")
            wl = db.get(Watchlist, wl_id)
            if wl:
                old = wl.enabled
                wl.enabled = enabled.lower() == "true"
                record_audit(
                    db,
                    action_type="watchlist_toggled",
                    user_id=actor.id,
                    entity_type="watchlists",
                    entity_id=wl.id,
                    old_value={"enabled": old},
                    new_value={"enabled": wl.enabled},
                )
    except PermissionDenied as exc:
        return _redirect("/watchlists", error=_error_text(exc), request=request)
    return RedirectResponse("/watchlists", status_code=303)


def watchlist_rebuild(request: Request, wl_id: int) -> Response:
    """Re-evaluate one watchlist and drop matches it no longer produces."""
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    from govcon.matching.engine import run_matching

    try:
        with session_scope() as db:
            actor = _actor(db, user, "manage_watchlists")
            stats = run_matching(db, watchlist_id=wl_id, rebuild=True)
            record_audit(
                db,
                action_type="watchlist_rebuilt",
                user_id=actor.id,
                entity_type="watchlists",
                entity_id=wl_id,
                new_value={
                    "matched": stats.matched,
                    "inserted": stats.inserted,
                    "updated": stats.updated,
                    "removed": stats.removed,
                },
            )
    except _WORKFLOW_ERRORS as exc:
        return _redirect("/watchlists", error=f"Rebuild failed: {_error_text(exc)}", request=request)
    return _redirect(
        "/watchlists",
        notice=f"Rebuilt: {stats.matched} matched, {stats.inserted} new, {stats.removed} removed.",
        request=request,
    )
