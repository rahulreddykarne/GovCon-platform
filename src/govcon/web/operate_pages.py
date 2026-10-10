"""Mission-control routes. The four views match the owner's preview and read stored rows."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.exc import IntegrityError

from govcon.collaboration.users import PermissionDenied, can
from govcon.config import get_settings
from govcon.db import session_scope
from govcon.models import AIModelPrice
from govcon.operating.board import agent_board, architecture_board, overview
from govcon.operating.guide import guided_walk
from govcon.operating.integrations import integration_cards
from govcon.web.routes.common import (
    _error_text,
    _NeedsLogin,
    _redirect,
    _render,
    _require_login,
)

_VIEWS = ("overview", "agents", "architecture", "integrations", "usage", "how")


def _usage(db):
    from govcon.ai.usage_log import usage_page

    return usage_page(db)


def _routing(db, settings):
    from govcon.ai.routing import describe_route

    return describe_route(db, settings)


def operate(request: Request, view: str = "overview") -> Response:
    if view not in _VIEWS:
        return RedirectResponse("/operate", status_code=303)
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    settings = get_settings()
    selected = request.query_params.get("node")
    requested = request.query_params.get("opp")
    opp_id = int(requested) if requested and requested.isdigit() else None
    with session_scope() as db:
        ctx = {
            "view": view,
            "active_page": "operate",
            "overview": overview(db, settings) if view == "overview" else None,
            "agents": agent_board(db, selected) if view == "agents" else None,
            "architecture": architecture_board(db, settings, selected) if view == "architecture" else None,
            "integrations": integration_cards(db, settings) if view == "integrations" else None,
            "routing": _routing(db, settings) if view == "integrations" else None,
            "usage": _usage(db) if view == "usage" else None,
            "guide": guided_walk(db, opp_id) if view == "how" else None,
        }
    return _render(request, "operate.html", ctx, user)


def usage_price_save(
    request: Request,
    price_id: Annotated[str | None, Form()] = None,
    provider: Annotated[str, Form()] = "",
    model: Annotated[str, Form()] = "",
    input_usd_per_million: Annotated[str, Form()] = "",
    output_usd_per_million: Annotated[str, Form()] = "",
    cached_usd_per_million: Annotated[str, Form()] = "",
    cache_write_usd_per_million: Annotated[str, Form()] = "",
    web_search_usd_per_thousand: Annotated[str, Form()] = "",
    source_url: Annotated[str, Form()] = "",
    effective_as_of: Annotated[str, Form()] = "",
) -> Response:
    """Save an editable model price. Does not call a provider or change sharing policy."""
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    if not can(user, "manage_users"):
        return _redirect("/operate/usage", error="Not permitted: managing prices needs manage_users.", request=request)
    from govcon.ai.usage_log import parse_price_amount

    try:
        provider_name = provider.strip().lower()
        model_name = model.strip()
        url = source_url.strip()
        if not provider_name or not model_name:
            raise ValueError("provider and model are required")
        if not url.startswith("https://") or " " in url:
            raise ValueError("source URL must be an https address")
        effective = date.fromisoformat(effective_as_of)
        values = {
            "provider": provider_name,
            "model": model_name,
            "input_usd_per_million": parse_price_amount(input_usd_per_million, required=True),
            "output_usd_per_million": parse_price_amount(output_usd_per_million, required=True),
            "cached_usd_per_million": parse_price_amount(cached_usd_per_million, required=False),
            "cache_write_usd_per_million": parse_price_amount(cache_write_usd_per_million, required=False),
            "web_search_usd_per_thousand": parse_price_amount(web_search_usd_per_thousand, required=False),
            "source_url": url,
            "effective_as_of": effective,
        }
        with session_scope() as db:
            row = None
            raw_id = (price_id or "").strip()
            if raw_id:
                if not raw_id.isdigit():
                    raise ValueError("price id must be a number")
                row = db.get(AIModelPrice, int(raw_id))
                if row is None:
                    raise ValueError("that price row is not stored")
            if row is None:
                row = AIModelPrice(note="Edited from AI usage & cost.")
                db.add(row)
            for key, value in values.items():
                setattr(row, key, value)
    except (ValueError, PermissionDenied, IntegrityError) as exc:
        message = "A price for that provider and model is already stored." if isinstance(exc, IntegrityError) else _error_text(exc)
        return _redirect("/operate/usage", error=message, request=request)
    return _redirect("/operate/usage", notice="Price saved. It applies to later calls.", request=request)
