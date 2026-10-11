"""Database outages produce a useful response without leaking connection details."""
from unittest.mock import Mock

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from govcon.config import Settings
from govcon.web.app import create_app


def test_database_outage_page(monkeypatch):
    app = create_app(Settings(_env_file=None))

    def unavailable(*args):
        raise OperationalError("SELECT private_data", {}, Exception("password=secret"))

    monkeypatch.setattr("govcon.web.operate_pages._require_login", unavailable)
    # No lifespan: this test intentionally has no database or startup work.
    response = TestClient(app).get("/operate")
    assert response.status_code == 503
    assert "Database unavailable" in response.text
    assert "private_data" not in response.text and "password=secret" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"] == "10"


def test_postgres_timeout_preserves_explicit_configuration(monkeypatch):
    from govcon.db import make_engine

    factory = Mock()
    monkeypatch.setattr("govcon.db.create_engine", factory)
    make_engine(Settings(_env_file=None, database_url="postgresql+psycopg://localhost/db"))
    assert factory.call_args.kwargs["connect_args"] == {"connect_timeout": 5}
    make_engine(Settings(_env_file=None, database_url="postgresql+psycopg://localhost/db?connect_timeout=9"))
    assert factory.call_args.kwargs["connect_args"] == {}
    make_engine(Settings(_env_file=None, database_url="sqlite://"))
    assert factory.call_args.kwargs["connect_args"] == {}
