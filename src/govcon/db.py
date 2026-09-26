"""SQLAlchemy engine and session factory. One database layer for the process."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from govcon.config import Settings, get_settings


class Base(DeclarativeBase):
    pass


def make_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    return create_engine(settings.require_database_url(), pool_pre_ping=True)


def make_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    if engine is None:
        engine = make_engine()
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    factory = make_session_factory(make_engine(settings))
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
    except Exception:
        return False, None
