"""Postgres readiness helper: URL parsing, redaction, mocked SELECT 1."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SECRET = "super-secret-db-pass"
_URL = f"postgresql+psycopg://govcon:{_SECRET}@127.0.0.1:5433/govcon"


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "windows" / "wait_postgres.py"
    spec = importlib.util.spec_from_file_location("wait_postgres", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def wait_pg():
    return _load()


def test_connect_kwargs_parses_user_db_host_port(wait_pg) -> None:
    kwargs = wait_pg.connect_kwargs(_URL, 12)
    assert kwargs["user"] == "govcon"
    assert kwargs["password"] == _SECRET
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 5433
    assert kwargs["dbname"] == "govcon"
    assert kwargs["connect_timeout"] == 12
    assert kwargs["autocommit"] is True


def test_connect_kwargs_decodes_percent_encoded_password(wait_pg) -> None:
    url = "postgresql://govcon:p%40ssword@localhost/govcon"
    kwargs = wait_pg.connect_kwargs(url, 5)
    assert kwargs["password"] == "p@ssword"
    assert "p@ssword" not in wait_pg.redacted_url(url)


def test_connect_kwargs_requires_username(wait_pg) -> None:
    with pytest.raises(ValueError, match="username"):
        wait_pg.connect_kwargs("postgresql://127.0.0.1:5433/govcon", 5)


def test_redacted_url_hides_password(wait_pg) -> None:
    hidden = wait_pg.redacted_url(_URL)
    assert _SECRET not in hidden
    assert "govcon" in hidden
    assert "5433" in hidden
    assert _URL not in hidden


def test_safe_error_redacts_password_and_url(wait_pg) -> None:
    exc = RuntimeError(f"FATAL password={_SECRET} url={_URL}")
    text = wait_pg.safe_error("SELECT 1 failed", _URL, exc)
    assert "SELECT 1 failed" in text
    assert _SECRET not in text
    assert _URL not in text
    assert "password=[REDACTED]" in text or "[REDACTED]" in text


def test_select_one_success_with_mocked_connect(wait_pg) -> None:
    seen: dict[str, object] = {}

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql):
            seen["sql"] = sql

    def connect(**kwargs):
        seen["kwargs"] = kwargs
        return Conn()

    wait_pg.select_one(_URL, 9, connect=connect)
    assert seen["sql"] == "SELECT 1"
    assert seen["kwargs"]["user"] == "govcon"
    assert seen["kwargs"]["password"] == _SECRET
    assert seen["kwargs"]["connect_timeout"] == 9


def test_select_one_failure_with_mocked_connect(wait_pg) -> None:
    def connect(**kwargs):
        raise RuntimeError(f'FATAL: password authentication failed for user "govcon" {_SECRET}')

    with pytest.raises(RuntimeError, match="authentication failed"):
        wait_pg.select_one(_URL, 9, connect=connect)


def test_main_success_uses_settings_url(wait_pg, monkeypatch, capsys) -> None:
    monkeypatch.setattr(wait_pg, "load_database_target", lambda: (_URL, 30))
    monkeypatch.setattr(wait_pg, "select_one", lambda *a, **k: None)
    assert wait_pg.main(["wait_postgres.py"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _SECRET not in captured.err
    assert _URL not in captured.err


def test_main_failure_redacts_and_uses_timeout_override(wait_pg, monkeypatch, capsys) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(wait_pg, "load_database_target", lambda: (_URL, 30))

    def boom(url, timeout, **kwargs):
        seen["url"] = url
        seen["timeout"] = timeout
        raise RuntimeError(f"FATAL password={_SECRET} url={_URL}")

    monkeypatch.setattr(wait_pg, "select_one", boom)
    assert wait_pg.main(["wait_postgres.py", "--timeout", "7"]) == 1
    assert seen["timeout"] == 7
    err = capsys.readouterr().err
    assert "SELECT 1 failed" in err
    assert _SECRET not in err
    assert _URL not in err


def test_main_missing_database_url(wait_pg, monkeypatch, capsys) -> None:
    from govcon.config import ConfigError

    def missing():
        raise ConfigError("DATABASE_URL is required for this command")

    monkeypatch.setattr(wait_pg, "load_database_target", missing)
    assert wait_pg.main(["wait_postgres.py"]) == 2
    err = capsys.readouterr().err
    assert "DATABASE_URL is not available" in err
    assert _SECRET not in err


def test_load_database_target_uses_govcon_settings(wait_pg, monkeypatch) -> None:
    settings = SimpleNamespace(
        require_database_url=lambda: _URL,
        db_connect_timeout_seconds=14,
    )
    monkeypatch.setattr("govcon.config.get_settings", lambda: settings)
    url, timeout = wait_pg.load_database_target()
    assert url == _URL
    assert timeout == 14


def test_start_govcon_probe_uses_wait_postgres_not_bare_psql() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "windows" / "Start-GovCon.ps1"
    text = script.read_text(encoding="utf-8")
    assert "wait_postgres.py" in text
    assert "--timeout" in text
    assert "DbConnectTimeoutSeconds" in text
    assert "psql.exe" not in text
    assert "-d postgres" not in text
    assert "PGPASSWORD" not in text
