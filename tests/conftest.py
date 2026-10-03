"""Shared fixtures. Integration tests use the Compose database."""

from __future__ import annotations

import os

import pytest

DEFAULT_DATABASE_URL = "postgresql+psycopg://govcon:govcon@localhost:5432/govcon"
os.environ.setdefault("DATABASE_URL", DEFAULT_DATABASE_URL)


@pytest.fixture(autouse=True)
def _clear_settings_cache(monkeypatch):
    from govcon.config import get_settings

    get_settings.cache_clear()
    # Existing fixtures exercise offline replay/bootstrap with synthetic data.
    # Candidate behavioral enforcement is tested separately with explicit True.
    monkeypatch.setenv("PROMPT_REQUIRE_BEHAVIORAL_EVALUATION", "false")
    yield
    get_settings.cache_clear()


@pytest.fixture()
def allow_proprietary_ai(monkeypatch):
    """Opt in to sending PROPRIETARY data to the (mocked) AI provider.

    The default policy blocks it; tests/test_workflow_gates.py checks that.
    Modules that exercise drafting, red-team, review, or validation prompts
    with a fake provider use this fixture.
    """
    from govcon.config import get_settings

    monkeypatch.setenv("AI_EXTERNAL_ALLOWED_FOR_PROPRIETARY", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def database_url():
    """Run even legacy destructive fixtures only in a fresh disposable database."""
    configured = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    if NO_DB:
        yield configured
        return
    from uuid import uuid4

    import psycopg
    from psycopg import sql
    from sqlalchemy.engine import make_url

    from govcon.db import dispose_engines
    url = make_url(configured)
    name = "govcon_test_" + uuid4().hex
    with psycopg.connect(url.set(drivername="postgresql").render_as_string(hide_password=False),
                         autocommit=True, connect_timeout=5) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        isolated = url.set(database=name).render_as_string(hide_password=False)
        os.environ["DATABASE_URL"] = isolated
        try:
            yield isolated
        finally:
            dispose_engines()
            os.environ["DATABASE_URL"] = configured
            # Identifier is a fixed prefix plus our UUID; never a caller path/name.
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


NO_DB = os.environ.get("GOVCON_TEST_NO_DB", "").strip().lower() in {"1", "true", "yes"}


@pytest.fixture(scope="session")
def upgraded_engine(database_url: str):
    if NO_DB:
        pytest.skip("GOVCON_TEST_NO_DB is set: database-backed tests are skipped (no pgvector here)")
    os.environ["DATABASE_URL"] = database_url
    os.environ.pop("GOVCON_ALEMBIC_URL", None)
    from govcon.config import get_settings
    from govcon.db import make_engine

    get_settings.cache_clear()
    from alembic import command
    from govcon.cli import alembic_config

    command.upgrade(alembic_config(), "head")
    engine = make_engine()
    yield engine
    engine.dispose()
