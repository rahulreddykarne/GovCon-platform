"""SQLAlchemy engine and session factory. One database layer for the process."""

from __future__ import annotations

import atexit
import os
from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from govcon.config import Settings, get_settings, settings_context


class Base(DeclarativeBase):
    pass


_settings_context = settings_context
_factories: dict[tuple[int, str], sessionmaker[Session]] = {}
_factory_lock = RLock()


def current_settings() -> Settings:
    return _settings_context.get() or get_settings()


@contextmanager
def settings_scope(settings: Settings) -> Iterator[None]:
    """Bind app configuration to request helpers, including sync worker threads."""
    token = _settings_context.set(settings)
    try:
        yield
    finally:
        _settings_context.reset(token)


def shared_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    settings = settings or current_settings()
    key = (os.getpid(), settings.require_database_url())
    with _factory_lock:
        if key not in _factories:
            _factories[key] = make_session_factory(make_engine(settings))
        return _factories[key]


def dispose_engines(settings: Settings | None = None) -> None:
    """Release cached pools at app/process shutdown; future scopes recreate them."""
    url = settings.require_database_url() if settings and settings.database_url else None
    with _factory_lock:
        for key in list(_factories):
            if settings is None or key == (os.getpid(), url):
                factory = _factories.pop(key)
                factory.kw["bind"].dispose()


atexit.register(dispose_engines)


def _after_fork() -> None:
    global _factory_lock
    # Child workers must not share the parent's live DBAPI connections.
    for factory in _factories.values():
        factory.kw["bind"].dispose(close=False)
    _factories.clear()
    _factory_lock = RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def make_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    return create_engine(settings.require_database_url(), pool_pre_ping=True)


def make_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    if engine is None:
        return shared_session_factory()
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    factory = shared_session_factory(settings)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_connectivity(engine: Engine) -> tuple[bool, str | None]:
    """Return connectivity and the Alembic revision, or ``(False, None)``."""
    from alembic.runtime.migration import MigrationContext

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            context = MigrationContext.configure(connection)
            return True, context.get_current_revision()
    except Exception:  # noqa: BLE001  boundary must record any failure
        return False, None
