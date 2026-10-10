"""Sourcing endpoints and supporting views."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from govcon.collaboration.users import can
from govcon.db import session_scope
from govcon.models import Task, User
from govcon.sourcing.intake import MAX_QUOTE_BYTES
from govcon.sourcing.records import MAX_CATALOG_BYTES
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
from govcon.workflow.invalidation import lock_opportunity

logger = logging.getLogger("govcon.web.sourcing")


def _sourcing_context(db: OrmSession, opp_id: int) -> dict[str, Any]:
    from govcon.models import RfqDraft, Supplier, SupplierQuote, SupplierQuoteLine
    from govcon.sourcing.records import is_current, usable_quote_totals

    usable_ids = {q.id for q in usable_quote_totals(db, opp_id)}
    counts = dict(db.execute(select(SupplierQuoteLine.quote_id, func.count(SupplierQuoteLine.id))
        .join(SupplierQuote, SupplierQuoteLine.quote_id == SupplierQuote.id)
        .where(SupplierQuote.opportunity_id == opp_id).group_by(SupplierQuoteLine.quote_id)).all())
    quotes = []
    for quote_row, supplier in db.execute(
        select(SupplierQuote, Supplier).join(Supplier, Supplier.id == SupplierQuote.supplier_id)
        .where(SupplierQuote.opportunity_id == opp_id).order_by(SupplierQuote.id.desc())
    ):
        quotes.append({
            "id": quote_row.id, "can_use": quote_row.id in usable_ids, "status": quote_row.status,
            "supplier": supplier.name, "total_price": quote_row.total_price, "valid_until": quote_row.valid_until,
            "lines": counts.get(quote_row.id, 0),
            "method": quote_row.extraction_method, "filename": quote_row.source_filename, "current": is_current(quote_row),
            "notes": quote_row.notes,
        })
    quote_tasks = db.scalars(
        select(Task).where(Task.opportunity_id == opp_id, Task.task_type == "quote_extraction",
                           Task.status != "succeeded").order_by(Task.id.desc()).limit(10)
    ).all()
    return {
        **_market_price_context(db, opp_id),
        "quotes": quotes,
        "quote_tasks": list(quote_tasks),
        "suppliers": list(db.scalars(select(Supplier).order_by(Supplier.name)).all()),
        "rfq_drafts": list(db.scalars(select(RfqDraft).where(RfqDraft.opportunity_id == opp_id)
                                      .order_by(RfqDraft.id.desc()).limit(10)).all()),
    }


def workspace_use_quote(request: Request, opp_id: int, quote_id: int, expected_version: Annotated[int, Form(ge=1)]) -> Response:
    from govcon.models import Supplier
    from govcon.sourcing.records import usable_quote_totals
    from govcon.workflow.commercial import update_commercial_facts

    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=sourcing"
    try:
        with session_scope() as db:
            actor = _actor(db, user, "review")
            if lock_opportunity(db, opp_id) is None:
                raise ValueError("opportunity not found")
            quote = next((q for q in usable_quote_totals(db, opp_id) if q.id == quote_id), None)
            if quote is None:
                raise ValueError("This quote is expired, incomplete, mismatches the required quantity/unit, or belongs to another opportunity.")
            supplier = db.get(Supplier, quote.supplier_id)
            if supplier is None:
                raise ValueError("The quote's supplier no longer exists.")
            update_commercial_facts(db, opp_id, actor=actor, expected_version=expected_version,
                changes={"supplier": supplier.name, "sourcing_cost": quote.total_price}, via="web_quote")
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, request=request, error=_error_text(exc))
    return _redirect(target, request=request, notice="Supplier and sourcing cost copied from the quote. Earlier decisions were reopened if the facts changed.")


async def workspace_add_quote(request: Request, opp_id: int) -> Response:
    """Record a supplier quote: a CSV/XLSX table, a document for authorized AI reading, or a total."""
    try:
        user = await run_in_threadpool(_require_login, request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=products"
    form = await request.form()
    upload = form.get("quote_file")
    data = await upload.read(MAX_QUOTE_BYTES + 1) if isinstance(upload, UploadFile) and upload.filename else b""
    # File storage (fsync), spreadsheet parsing and database work run in the thread pool.
    return await run_in_threadpool(_add_quote_sync, request, user, opp_id, target, form, upload, data)


def _add_quote_sync(
    request: Request, user: User, opp_id: int, target: str, form: Any, upload: Any, data: bytes
) -> Response:
    from govcon.sourcing.intake import receive_quote_file
    from govcon.sourcing.records import get_or_create_supplier, record_quote

    try:
        with session_scope() as db:
            actor = _actor(db, user, "review")
            if lock_opportunity(db, opp_id) is None:
                raise ValueError("opportunity not found")
            supplier, _ = get_or_create_supplier(db, name=str(form.get("supplier_name") or ""), actor=actor,
                                                 provenance=f"entered by {actor.email} with a quote")
            valid_until = str(form.get("valid_until") or "").strip() or None
            if data:
                intake = receive_quote_file(db, opportunity_id=opp_id, supplier_id=supplier.id, data=data,
                                            filename=upload.filename, valid_until=valid_until, actor=actor)
                notice = ("Quote recorded." if intake.quote is not None
                          else "Quote document stored; it will be read by AI if an owner has authorized it.")
            else:
                total = str(form.get("total_price") or "").strip()
                if not total:
                    raise ValueError("upload a quote file or enter the quote's total price")
                record_quote(db, opportunity_id=opp_id, supplier_id=supplier.id, actor=actor, method="manual",
                             total_price=total, valid_until=valid_until, notes=str(form.get("notes") or "") or None)
                notice = "Quote recorded."
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc), request=request)
    return _redirect(target, notice=notice, request=request)


def workspace_pursuit_facts(
    request: Request,
    opp_id: int,
    expected_version: Annotated[str, Form()],
    quote_price: Annotated[str | None, Form()] = None,
    sourcing_cost: Annotated[str | None, Form()] = None,
    supplier: Annotated[str | None, Form()] = None,
    notes: Annotated[str | None, Form()] = None,
    return_tab: Annotated[str | None, Form()] = None,
) -> Response:
    """Record the pursuit's quote price, sourcing cost, supplier and notes; a blank field clears it."""
    from govcon.workflow.commercial import update_commercial_facts

    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    tab = return_tab if return_tab in ("products", "pricing", "overview") else "products"
    target = f"/workspace/{opp_id}?tab={tab}"

    def clean_text(value: str | None) -> str | None:
        return (value.strip() or None) if value is not None else None

    def clean_money(value: str | None) -> str | None:
        return (clean_text(value) or "").replace("$", "").replace(",", "") or None

    try:
        version = _form_int(expected_version)
        if version is None:
            raise ValueError("reload the page and enter the values again")
        with session_scope() as db:
            actor = _actor(db, user, "review")
            _, changed = update_commercial_facts(
                db, opp_id, actor=actor, expected_version=version, via="web",
                changes={"quote_price": clean_money(quote_price), "sourcing_cost": clean_money(sourcing_cost),
                         "supplier": clean_text(supplier), "notes": clean_text(notes)},
            )
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc), request=request)
    if not changed:
        return _redirect(target, notice="Nothing changed.", request=request)
    return _redirect(target, notice="Pursuit commercial facts saved. Earlier review, proposal and submission "
                                    "decisions that relied on them were reopened.", request=request)


def _market_price_context(db: OrmSession, opp_id: int) -> dict[str, Any]:
    from govcon.models import Opportunity, Pursuit
    from govcon.sourcing.market_prices import (
        MARKET_PRICE_TASK,
        effective_cost_basis,
        latest_estimate,
        latest_run,
    )
    from govcon.sourcing.product_facts import effective_product_facts

    run = latest_run(db, opp_id)
    current = latest_estimate(db, opp_id)
    in_use = False
    opportunity = db.get(Opportunity, opp_id)
    if current is not None and run is not None and current.id == run.id and opportunity is not None:
        pursuit = db.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
        basis = effective_cost_basis(db, opportunity, pursuit, effective_product_facts(db, opportunity).quantity)
        in_use = basis.market_price_run_id == run.id
    tasks = db.scalars(
        select(Task).where(Task.opportunity_id == opp_id, Task.task_type == MARKET_PRICE_TASK,
                           Task.status.in_(("queued", "running", "retrying", "failed")))
        .order_by(Task.id.desc()).limit(3)
    ).all()
    # A failed search older than the latest run is history, not a pending state.
    tasks = [t for t in tasks if t.status != "failed" or run is None or t.id > (run.task_id or 0)]
    return {
        "market_run": run,
        "market_estimate_current": current is not None and run is not None and current.id == run.id,
        "market_estimate_in_use": in_use,
        "market_tasks": tasks,
    }


def workspace_market_prices(request: Request, opp_id: int) -> Response:
    """Queue a fresh web price search for this opportunity."""
    from govcon.sourcing.market_prices import queue_market_price_research

    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=products"
    try:
        with session_scope() as db:
            actor = _actor(db, user, "review")
            lock_opportunity(db, opp_id)
            task, created = queue_market_price_research(db, opportunity_id=opp_id, actor_user_id=actor.id)
            task_id = task.id
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc), request=request)
    logger.info("web price search queued from the workspace opportunity=%s task=%s user=%s created=%s",
                opp_id, task_id, user.id, created)
    return _redirect(target, notice="Web price search queued; results appear here when it finishes.", request=request)


def workspace_draft_rfq(
    request: Request, opp_id: int, supplier_id: Annotated[str | None, Form()] = None
) -> Response:
    from govcon.sourcing.records import draft_rfq

    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=products"
    try:
        with session_scope() as db:
            actor = _actor(db, user, "review")
            draft_rfq(db, opportunity_id=opp_id, supplier_id=_form_int(supplier_id), actor=actor)
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc), request=request)
    return _redirect(target, notice="RFQ drafted below; copy it to send it yourself.", request=request)


def suppliers_page(request: Request) -> Response:
    from govcon.models import CatalogImport, Supplier, SupplierProduct

    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    with session_scope() as db:
        rows = []
        for supplier in db.scalars(select(Supplier).order_by(Supplier.name)):
            rows.append({
                "supplier": supplier,
                "products": db.scalar(select(func.count()).select_from(SupplierProduct)
                                      .where(SupplierProduct.supplier_id == supplier.id)),
                "last_import": db.scalar(select(CatalogImport).where(CatalogImport.supplier_id == supplier.id)
                                         .order_by(CatalogImport.id.desc()).limit(1)),
            })
        return _render(request, "suppliers.html", {
            "rows": rows, "can_edit": can(user, "review"), "active_page": "suppliers",
        }, user)


async def suppliers_save(request: Request) -> Response:
    """Add a supplier, or import a supplier's catalog CSV."""
    try:
        user = await run_in_threadpool(_require_login, request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    upload = form.get("catalog_file")
    data = await upload.read(MAX_CATALOG_BYTES + 1) if isinstance(upload, UploadFile) and upload.filename else b""
    # Catalog parsing and database work run in the thread pool.
    return await run_in_threadpool(_suppliers_save_sync, request, user, form, upload, data)


def _suppliers_save_sync(request: Request, user: User, form: Any, upload: Any, data: bytes) -> Response:
    from govcon.sourcing.records import get_or_create_supplier, import_catalog_csv

    try:
        with session_scope() as db:
            actor = _actor(db, user, "review")
            fields = {k: str(form.get(k) or "") for k in ("uei", "cage_code", "contact_name", "contact_email", "phone")}
            supplier, created = get_or_create_supplier(db, name=str(form.get("name") or ""), actor=actor,
                                                       provenance=f"entered by {actor.email}", **fields)
            if data:
                result = import_catalog_csv(db, supplier_id=supplier.id, data=data, filename=upload.filename, actor=actor)
                notice = f"Imported {result.rows_imported} of {result.rows_total} catalog rows for {supplier.name}."
                if result.errors:
                    notice += " Skipped: " + "; ".join(f"line {e['line']}: {e['error']}" for e in result.errors[:5])
            else:
                notice = f"Supplier {supplier.name} {'added' if created else 'already exists'}."
    except _WORKFLOW_ERRORS as exc:
        return _redirect("/suppliers", error=_error_text(exc), request=request)
    return _redirect("/suppliers", notice=notice, request=request)
