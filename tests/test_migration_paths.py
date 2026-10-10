"""Both databases that carry revision f8a9b0c1d2e3 reach the same head schema.

Fresh path: an empty database upgraded to head.
Laptop path: a database built by the AI-usage branch, which used f8a9b0c1d2e3 for
``ai_provider_calls`` / ``ai_model_prices`` and never created ``market_price_runs``.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine, make_url

ROOT = Path(__file__).parents[1]
BEFORE_SPLIT = "e7f8a9b0c1d2"
HEAD = "b0c1d2e3f4a5"


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def scratch_db(upgraded_engine, monkeypatch) -> Iterator[tuple[str, Engine]]:
    """An empty disposable database; Alembic is pointed at it through GOVCON_ALEMBIC_URL."""
    import psycopg
    from psycopg import sql

    url = make_url(upgraded_engine.url.render_as_string(hide_password=False))
    name = "govcon_test_mig_" + uuid4().hex
    admin_url = url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    target = url.set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("GOVCON_ALEMBIC_URL", target)
    engine = sa.create_engine(target)
    try:
        yield target, engine
    finally:
        engine.dispose()
        with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def _alembic(command_name: str, revision: str) -> None:
    from alembic import command
    from govcon.cli import alembic_config

    getattr(command, command_name)(alembic_config(), revision)


def _run(engine: Engine, migration) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        migration.upgrade()


def _schema(engine: Engine) -> dict[str, object]:
    """Tables, columns, indexes, and constraint definitions; enough to tell two schemas apart."""
    inspector = sa.inspect(engine)
    tables: dict[str, object] = {}
    for table in sorted(inspector.get_table_names()):
        if table == "alembic_version":
            continue
        tables[table] = {
            "columns": sorted((c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns(table)),
            "indexes": sorted((i["name"], tuple(i["column_names"])) for i in inspector.get_indexes(table)),
        }
    with engine.connect() as conn:
        constraints = sorted(
            (row.table, row.name, row.definition)
            for row in conn.execute(sa.text(
                "SELECT t.relname AS table, c.conname AS name, pg_get_constraintdef(c.oid) AS definition "
                "FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = current_schema()"
            ))
        )
    return {"tables": tables, "constraints": constraints}


def _version(engine: Engine) -> str:
    with engine.connect() as conn:
        return str(conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one())


def _assert_head_tables(engine: Engine) -> None:
    inspector = sa.inspect(engine)
    for table in ("market_price_runs", "ai_provider_calls", "ai_model_prices"):
        assert inspector.has_table(table), table
    calls = {c["name"] for c in inspector.get_columns("ai_provider_calls")}
    assert {"web_search_requests", "web_fetch_requests", "input_tokens", "cost_usd"} <= calls
    assert "web_search_usd_per_thousand" in {c["name"] for c in inspector.get_columns("ai_model_prices")}
    with engine.connect() as conn:
        task_check = conn.execute(sa.text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_tasks_task_type'"
        )).scalar_one()
        assert "market_price_research" in task_check
        rates = dict(conn.execute(sa.text(
            "SELECT model, web_search_usd_per_thousand FROM ai_model_prices WHERE provider = 'anthropic'"
        )).all())
    assert rates and all(rate is not None and float(rate) == 10.0 for rate in rates.values())


def test_fresh_database_upgrades_to_head(scratch_db) -> None:
    _, engine = scratch_db
    _alembic("upgrade", "head")
    assert _version(engine) == HEAD
    _assert_head_tables(engine)


def test_laptop_database_stamped_by_the_usage_branch_reaches_the_same_schema(scratch_db, upgraded_engine) -> None:
    _, engine = scratch_db
    _alembic("upgrade", BEFORE_SPLIT)
    _run(engine, _module(ROOT / "tests/fixtures/pr27_f8a9b0c1d2e3_ai_provider_usage.py", "pr27_usage"))
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE alembic_version SET version_num = 'f8a9b0c1d2e3'"))
        conn.execute(sa.text(
            "INSERT INTO ai_provider_calls (purpose, provider, model, status, input_tokens, output_tokens, cost_usd) "
            "VALUES ('solicitation_analysis', 'deepseek', 'deepseek-flash', 'succeeded', 50, 20, 0.0000195)"
        ))
        conn.execute(sa.text(
            "UPDATE ai_model_prices SET note = 'edited on the laptop' WHERE model = 'deepseek-flash'"
        ))
    inspector = sa.inspect(engine)
    assert not inspector.has_table("market_price_runs")
    assert "web_search_requests" not in {c["name"] for c in inspector.get_columns("ai_provider_calls")}

    _alembic("upgrade", "head")

    assert _version(engine) == HEAD
    _assert_head_tables(engine)
    with engine.connect() as conn:
        kept = conn.execute(sa.text(
            "SELECT input_tokens, output_tokens, web_search_requests FROM ai_provider_calls"
        )).all()
        note = conn.execute(sa.text(
            "SELECT note FROM ai_model_prices WHERE model = 'deepseek-flash'"
        )).scalar_one()
        price_rows = conn.execute(sa.text("SELECT count(*) FROM ai_model_prices")).scalar_one()
    assert [tuple(row) for row in kept] == [(50, 20, None)]  # recorded usage survives; no count is invented
    assert note == "edited on the laptop"  # an operator's price edit is not overwritten
    from govcon.ai.price_catalog import SEED_PRICES
    assert price_rows == len(SEED_PRICES)

    assert _schema(engine) == _schema(upgraded_engine)


def test_split_revisions_can_run_again_without_changes(scratch_db) -> None:
    _, engine = scratch_db
    _alembic("upgrade", "head")
    before = _schema(engine)
    versions = ROOT / "alembic/versions"
    for filename in ("f8a9b0c1d2e3_market_price_runs.py", "a9b0c1d2e3f4_sam_award_notice_status.py",
                     "b0c1d2e3f4a5_ai_provider_usage.py"):
        _run(engine, _module(versions / filename, filename.removesuffix(".py")))
    assert _schema(engine) == before
    with engine.connect() as conn:
        from govcon.ai.price_catalog import SEED_PRICES
        assert conn.execute(sa.text("SELECT count(*) FROM ai_model_prices")).scalar_one() == len(SEED_PRICES)
