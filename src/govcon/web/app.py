"""FastAPI application factory — Phase 14 full web UI.

Serves the entire GovCon collaborative workspace at http://localhost:8000.
Binds to 127.0.0.1 by default (§24 security requirement).
"""

from __future__ import annotations

import pathlib

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from govcon.config import Settings, get_settings

_STATIC_DIR = pathlib.Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="GovCon", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings

    # Static files
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Import routes (late import to avoid circular deps at module load time)
    from govcon.web.routes import (
        admin_invite_get,
        admin_invite_post,
        inbox,
        inbox_action,
        learning,
        login_get,
        login_post,
        logout_post,
        opp_detail,
        opp_start_workspace,
        ops,
        pipeline,
        search,
        vendors,
        watchlist_edit_get,
        watchlist_edit_post,
        watchlist_new_get,
        watchlist_new_post,
        watchlist_rebuild,
        watchlist_toggle,
        watchlists,
        workspace,
        workspace_award_sample,
        workspace_pricing_save,
        workspace_run,
        workspace_approve,
        workspace_comment,
        workspace_complete_review,
        workspace_proposal_approve,
        workspace_record_outcome,
        workspace_submission_approve,
    )

    # Auth
    app.add_api_route("/login",  login_get,   methods=["GET"])
    app.add_api_route("/login",  login_post,  methods=["POST"])
    app.add_api_route("/logout", logout_post, methods=["POST"])

    # Inbox
    app.add_api_route("/",             inbox,        methods=["GET"])
    app.add_api_route("/inbox/action", inbox_action, methods=["POST"])

    # Search
    app.add_api_route("/search", search, methods=["GET"])

    # Opportunity detail
    app.add_api_route("/opp/{opp_id}",               opp_detail,          methods=["GET"])
    app.add_api_route("/opp/{opp_id}/start-workspace", opp_start_workspace, methods=["POST"])

    # Workspace
    app.add_api_route("/workspace/{opp_id}",                    workspace,                  methods=["GET"])
    app.add_api_route("/workspace/{opp_id}/run/{action}",       workspace_run,              methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/pricing",            workspace_pricing_save,     methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/award-sample",       workspace_award_sample,     methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/comment",            workspace_comment,          methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/complete-review",    workspace_complete_review,  methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/approve",            workspace_approve,          methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/proposal/approve",   workspace_proposal_approve, methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/submission/approve", workspace_submission_approve,methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/record-outcome",     workspace_record_outcome,   methods=["POST"])

    # Pipeline
    app.add_api_route("/pipeline", pipeline, methods=["GET"])

    # Watchlists
    app.add_api_route("/watchlists",                    watchlists,          methods=["GET"])
    app.add_api_route("/watchlists/new",                watchlist_new_get,   methods=["GET"])
    app.add_api_route("/watchlists/new",                watchlist_new_post,  methods=["POST"])
    app.add_api_route("/watchlists/{wl_id}/edit",       watchlist_edit_get,  methods=["GET"])
    app.add_api_route("/watchlists/{wl_id}/edit",       watchlist_edit_post, methods=["POST"])
    app.add_api_route("/watchlists/{wl_id}/toggle",     watchlist_toggle,    methods=["POST"])
    app.add_api_route("/watchlists/{wl_id}/rebuild",    watchlist_rebuild,   methods=["POST"])

    # Vendors
    app.add_api_route("/vendors", vendors, methods=["GET"])

    # Ops
    app.add_api_route("/ops", ops, methods=["GET"])

    # Learning
    app.add_api_route("/learning", learning, methods=["GET"])

    # Admin
    app.add_api_route("/admin/users/invite", admin_invite_get,  methods=["GET"])
    app.add_api_route("/admin/users/invite", admin_invite_post, methods=["POST"])

    return app


def bind_host(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return settings.web_bind_host
