"""Shared fixtures. Integration tests use the Compose database."""

from __future__ import annotations

import os

import pytest

DEFAULT_DATABASE_URL = "postgresql+psycopg://govcon:govcon@localhost:5432/govcon"
os.environ.setdefault("DATABASE_URL", DEFAULT_DATABASE_URL)


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from govcon.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


@pytest.fixture(scope="session")
def upgraded_engine(database_url: str):
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
