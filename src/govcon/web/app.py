"""FastAPI application factory — Phase 14 full web UI.

Serves the entire GovCon collaborative workspace at http://localhost:8000.
Binds to 127.0.0.1 by default (§24 security requirement).
"""

from __future__ import annotations

import pathlib
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from govcon.config import Settings, get_settings

_STATIC_DIR = pathlib.Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    from govcon.db import dispose_engines, settings_scope
    from govcon.web.security import (
        LoginThrottle,
        PackageUploadLimitMiddleware,
        csrf_cookie_middleware,
        protect_mutation,
    )

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            dispose_engines(settings)

    app = FastAPI(title="GovCon", docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(protect_mutation)], lifespan=lifespan)
    app.state.settings = settings
    app.state.csrf_secret = settings.web_csrf_secret.encode() if settings.web_csrf_secret else secrets.token_bytes(32)
    app.state.login_throttle = LoginThrottle()
    app.middleware("http")(csrf_cookie_middleware)

    @app.middleware("http")
    async def bind_settings(request, call_next):
        with settings_scope(settings):
            return await call_next(request)

    app.add_middleware(PackageUploadLimitMiddleware)

    # Static files
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/health")
    def health() -> JSONResponse:
        """Liveness and database connectivity. The body never includes secrets or rows."""
        from sqlalchemy import text as sql_text

        from govcon.db import session_scope

        try:
            with session_scope(settings) as db:
                db.execute(sql_text("SELECT 1"))
                from govcon.ops.health import collect_health, record_heartbeat

                record_heartbeat(db, role="web", instance_id="web")
                report = collect_health(db, settings)
        except Exception:  # noqa: BLE001  boundary must record any failure
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok", "checks": report["checks"]})

    # Import routes (late import to avoid circular deps at module load time)
    from govcon.web.bot_pages import bots_decide, bots_page, bots_run
    from govcon.web.routes import (
        admin_invite_get,
        admin_invite_post,
        ai_sharing_save,
        inbox,
        inbox_action,
        learning,
        login_get,
        login_post,
        logout_post,
        notification_acknowledge,
        notification_read,
        notifications,
        opp_detail,
        opp_start_workspace,
        ops,
        ops_task_action,
        pipeline,
        search,
        settings_page,
        settings_save,
        suppliers_page,
        suppliers_save,
        vendors,
        watchlist_delete,
        watchlist_edit_get,
        watchlist_edit_post,
        watchlist_new_get,
        watchlist_new_post,
        watchlist_rebuild,
        watchlist_toggle,
        watchlists,
        workspace,
        workspace_add_quote,
        workspace_approve,
        workspace_assign_reviewer,
        workspace_comment,
        workspace_complete_review,
        workspace_draft_rfq,
        workspace_outcome_suggestion,
        workspace_prepare,
        workspace_proposal_approve,
        workspace_proposal_retry,
        workspace_proposal_status,
        workspace_pursuit_facts,
        workspace_record_outcome,
        workspace_run_analysis,
        workspace_submission_approve,
        workspace_use_quote,
    )

    # Auth
    app.add_api_route("/login",  login_get,   methods=["GET"])
    app.add_api_route("/login",  login_post,  methods=["POST"])
    app.add_api_route("/logout", logout_post, methods=["POST"])

    # Inbox
    app.add_api_route("/",             inbox,        methods=["GET"])
    app.add_api_route("/inbox/action", inbox_action, methods=["POST"])
    app.add_api_route("/notifications", notifications, methods=["GET"])
    app.add_api_route("/notifications/{notification_id}/read", notification_read, methods=["POST"])
    app.add_api_route("/notifications/{notification_id}/acknowledge", notification_acknowledge, methods=["POST"])

    # Search
    app.add_api_route("/search", search, methods=["GET"])

    # Opportunity detail
    app.add_api_route("/opp/{opp_id}",               opp_detail,          methods=["GET"])
    app.add_api_route("/opp/{opp_id}/start-workspace", opp_start_workspace, methods=["POST"])

    # Workspace
    from govcon.web.package_actions import workspace_package_action
    from govcon.web.progress import workspace_progress
    from govcon.web.workspace_actions import workspace_proposal_revise

    app.add_api_route("/workspace/{opp_id}/proposal/revise", workspace_proposal_revise, methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/progress", workspace_progress, methods=["GET"])
    app.add_api_route("/workspace/{opp_id}/submission/package/{action}", workspace_package_action, methods=["POST"])
    app.add_api_route("/workspace/{opp_id}",                    workspace,                  methods=["GET"])
    app.add_api_route("/workspace/{opp_id}/comment",            workspace_comment,          methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/assign",             workspace_assign_reviewer,  methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/complete-review",    workspace_complete_review,  methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/approve",            workspace_approve,          methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/proposal/approve",   workspace_proposal_approve, methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/proposal/retry",     workspace_proposal_retry,   methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/proposal/status",    workspace_proposal_status,  methods=["GET"])
    app.add_api_route("/workspace/{opp_id}/submission/approve", workspace_submission_approve,methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/record-outcome",     workspace_record_outcome,   methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/analyze/{kind}",     workspace_run_analysis,     methods=["POST"])

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
    app.add_api_route("/watchlists/{wl_id}/delete", watchlist_delete, methods=["POST"])

    # Vendors
    app.add_api_route("/vendors", vendors, methods=["GET"])

    # Ops
    app.add_api_route("/ops", ops, methods=["GET"])
    app.add_api_route("/bots", bots_page, methods=["GET"])
    app.add_api_route("/bots/run/{bot_name}", bots_run, methods=["POST"])
    app.add_api_route("/bots/approvals/{approval_id}/{action}", bots_decide, methods=["POST"])
    app.add_api_route("/settings", settings_page, methods=["GET"])
    app.add_api_route("/workspace/{opp_id}/outcome-suggestions/{suggestion_id}/{action}",
                      workspace_outcome_suggestion, methods=["POST"])
    app.add_api_route("/settings", settings_save, methods=["POST"])
    app.add_api_route("/settings/ai-sharing", ai_sharing_save, methods=["POST"])
    app.add_api_route("/suppliers", suppliers_page, methods=["GET"])
    app.add_api_route("/suppliers", suppliers_save, methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/quotes", workspace_add_quote, methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/quotes/{quote_id}/use", workspace_use_quote, methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/pursuit-facts", workspace_pursuit_facts, methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/rfq", workspace_draft_rfq, methods=["POST"])
    app.add_api_route("/workspace/{opp_id}/prepare", workspace_prepare, methods=["POST"])
    app.add_api_route("/ops/tasks/{task_id}/{action}", ops_task_action, methods=["POST"])

    # Learning
    app.add_api_route("/learning", learning, methods=["GET"])

    # Admin
    from govcon.web.admin import admin_page, admin_user_action

    app.add_api_route("/admin", admin_page, methods=["GET"])
    app.add_api_route("/admin/users/{user_id}/{action}", admin_user_action, methods=["POST"])
    app.add_api_route("/admin/users/invite", admin_invite_get,  methods=["GET"])
    app.add_api_route("/admin/users/invite", admin_invite_post, methods=["POST"])

    return app


def bind_host(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return settings.web_bind_host
