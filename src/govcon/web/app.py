"""FastAPI application factory.

Phase 0 exposes no product routes. The process binds to loopback unless the
operator sets WEB_BIND_ALLOW_PUBLIC.
"""

from __future__ import annotations

from fastapi import FastAPI

from govcon.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="GovCon", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    return app


def bind_host(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return settings.web_bind_host
