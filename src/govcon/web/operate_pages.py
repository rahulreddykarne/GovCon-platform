"""Mission-control routes. The four views match the owner's preview and read stored rows."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from govcon.config import get_settings
from govcon.db import session_scope
from govcon.operating.board import agent_board, architecture_board, overview
from govcon.operating.guide import guided_walk
from govcon.operating.integrations import integration_cards
from govcon.web.routes.common import _NeedsLogin, _render, _require_login

_VIEWS = ("overview", "agents", "architecture", "integrations", "how")


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
            "guide": guided_walk(db, opp_id) if view == "how" else None,
        }
    return _render(request, "operate.html", ctx, user)
