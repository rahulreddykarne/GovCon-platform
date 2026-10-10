"""CLI, migrations, auth baseline, and schema coverage."""

from __future__ import annotations

import os
import subprocess
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from alembic import command
from govcon.audit import record_audit
from govcon.cli import alembic_config, app
from govcon.collaboration.notifications import notify
from govcon.collaboration.users import (
    PermissionDenied,
    authenticate,
    create_session,
    deactivate_user,
    invite_user,
    logout,
    require_permission,
    user_for_token,
)
from govcon.concurrency import StaleRecordError, apply_versioned_update
from govcon.db import Base
from govcon.models import AuditEvent, Opportunity, Pursuit, ReviewComment, ReviewSession
from govcon.paths import repo_root
from govcon.seed import DEMO_WATCHLIST_NAME

runner = CliRunner()


def test_help_shows_command_groups() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    output = result.stdout
    assert "db" in output
    assert "users" in output
    assert "match" in output
    assert "watchlist" in output
    assert "status" in output
    db_help = runner.invoke(app, ["db", "--help"])
    assert db_help.exit_code == 0
    assert "upgrade" in db_help.stdout
    assert "seed-demo-watchlist" in db_help.stdout


def test_status_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "")
    from govcon.config import get_settings

    get_settings.cache_clear()
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 2
    assert "DATABASE_URL" in result.output


def test_env_file_is_ignored() -> None:
    root = repo_root()
    ignored = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=root, check=False)
    assert ignored.returncode == 0
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"], cwd=root, check=False)
    assert tracked.returncode != 0
    example = root / ".env.example"
    assert example.is_file()
    assert "SAM_API_KEY=" in example.read_text(encoding="utf-8")
    assert "live-" not in example.read_text(encoding="utf-8")


def test_upgrade_from_empty_database(upgraded_engine) -> None:
    from sqlalchemy.engine import make_url

    # Same server as the suite's DATABASE_URL; never a hardcoded one.
    base = make_url(os.environ.get("DATABASE_URL", "postgresql+psycopg://govcon:govcon@localhost:5432/govcon"))
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = "govcon_phase0_empty"
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        connection.execute(text(f"CREATE DATABASE {name}"))
    url = base.set(database=name).render_as_string(hide_password=False)
    previous = os.environ.get("GOVCON_ALEMBIC_URL")
    os.environ["GOVCON_ALEMBIC_URL"] = url
    try:
        command.upgrade(alembic_config(), "head")
        engine = create_engine(url)
        _assert_schema(engine)
        with engine.connect() as connection:
            extension = connection.execute(
                text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            ).scalar_one()
            assert extension == "vector"
        engine.dispose()
    finally:
        if previous is None:
            os.environ.pop("GOVCON_ALEMBIC_URL", None)
        else:
            os.environ["GOVCON_ALEMBIC_URL"] = previous
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    admin.dispose()
    assert upgraded_engine is not None


def test_status_prints_connectivity_and_revision(upgraded_engine) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "database_connectivity: ok" in result.stdout
    assert "schema_revision: " in result.stdout
    assert "schema_revision: None" not in result.stdout
    assert "schema_revision: unavailable" not in result.stdout


def test_seed_demo_watchlist_is_idempotent(upgraded_engine) -> None:
    first = runner.invoke(app, ["db", "seed-demo-watchlist"])
    second = runner.invoke(app, ["db", "seed-demo-watchlist"])
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "demo_watchlist_count: 1" in first.stdout
    assert "demo_watchlist_count: 1" in second.stdout
    assert "demo_watchlist_created: no" in second.stdout
    factory = sessionmaker(bind=upgraded_engine, expire_on_commit=False)
    with factory() as session:
        from govcon.models import Watchlist

        row = session.scalar(select(Watchlist).where(Watchlist.name == DEMO_WATCHLIST_NAME))
        assert row is not None
        assert row.psc_codes == []
        assert row.keywords == []


def test_auth_sessions_roles_and_audit(upgraded_engine) -> None:
    factory = sessionmaker(bind=upgraded_engine, autoflush=False, expire_on_commit=False)
    email = f"phase0-{uuid4().hex}@example.com"
    other_email = f"phase0-{uuid4().hex}@example.com"
    password = "correct-horse-battery"
    with factory() as session:
        owner = invite_user(
            session,
            email=email,
            display_name="Owner One",
            password=password,
            role="owner",
        )
        reviewer = invite_user(
            session,
            email=other_email,
            display_name="Reviewer Two",
            password=password,
            role="reviewer",
        )
        session.commit()
        owner_id = owner.id
        reviewer_id = reviewer.id
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.entity_id == owner_id, AuditEvent.action_type == "user_invited")
        )
        assert audit is not None
        assert audit.new_value == {"email": email, "role": "owner"}
        assert "password" not in audit.new_value
        assert "password_hash" not in audit.new_value
        rendered = str(audit.new_value)
        assert password not in rendered
        assert "argon2" not in rendered.lower()

        probed = record_audit(
            session,
            action_type="secret_probe",
            user_id=owner_id,
            entity_type="user",
            entity_id=owner_id,
            old_value={"password": password, "note": "before"},
            new_value={
                "email": email,
                "role": "owner",
                "user_id": owner_id,
                "password": password,
                "password_hash": owner.password_hash,
                "token": "raw-session-token",
                "nested": {"api_key": "sk-test-secret", "id": owner_id},
            },
        )
        stored = str(probed.old_value) + str(probed.new_value)
        assert probed.old_value == {"note": "before"}
        assert probed.new_value == {
            "email": email,
            "role": "owner",
            "user_id": owner_id,
            "nested": {"id": owner_id},
        }
        assert password not in stored
        assert owner.password_hash not in stored
        assert "raw-session-token" not in stored
        assert "sk-test-secret" not in stored

        assert authenticate(session, email, password) is not None
        assert authenticate(session, email, "wrong-password-value") is None
        token_a = create_session(session, owner)
        token_b = create_session(session, reviewer)
        session.commit()
        assert user_for_token(session, token_a).id == owner_id
        assert user_for_token(session, token_b).id == reviewer_id
        require_permission(owner, "approve")
        with pytest.raises(PermissionDenied):
            require_permission(reviewer, "approve")
        assert logout(session, token_a) is True
        session.commit()
        assert user_for_token(session, token_a) is None
        assert user_for_token(session, token_b) is not None

        deactivate_user(session, reviewer, actor_user_id=owner_id)
        session.commit()
        assert authenticate(session, other_email, password) is None
        assert user_for_token(session, token_b) is None

        opportunity = Opportunity(source="test", source_id=uuid4().hex, raw={"fixture": True}, title="Fixture")
        session.add(opportunity)
        session.flush()
        comment = ReviewComment(
            opportunity_id=opportunity.id,
            user_id=owner_id,
            body="First comment",
        )
        session.add(comment)
        session.flush()
        record_audit(
            session,
            action_type="review_comment_added",
            user_id=owner_id,
            opportunity_id=opportunity.id,
            entity_type="review_comment",
            entity_id=comment.id,
            new_value={"body": "First comment"},
        )
        notify(session, user_id=owner_id, notification_type="new_reviewer_comment", opportunity_id=opportunity.id)
        review = ReviewSession(opportunity_id=opportunity.id)
        pursuit = Pursuit(opportunity_id=opportunity.id, sourcing_cost=Decimal(100), quote_price=Decimal(150))
        session.add(review)
        session.add(pursuit)
        session.flush()
        session.commit()
        session.refresh(review)
        session.refresh(pursuit)
        assert pursuit.margin_pct == Decimal("50.0")
        review_id = review.id

        apply_versioned_update(session, review, review.version, {"status": "under_review"})
        session.commit()

    with factory() as stale, factory() as current:
        current_row = current.get(ReviewSession, review_id)
        stale_row = stale.get(ReviewSession, review_id)
        assert current_row is not None and stale_row is not None
        apply_versioned_update(
            current,
            current_row,
            current_row.version,
            {"final_approval_status": "pending"},
        )
        current.commit()
        with pytest.raises(StaleRecordError):
            apply_versioned_update(
                stale,
                stale_row,
                stale_row.version,
                {"final_approval_status": "approved"},
            )

    listed = runner.invoke(app, ["users", "list"])
    assert listed.exit_code == 0
    assert email in listed.stdout
    assert "argon2" not in listed.stdout.lower()
    assert password not in listed.stdout


def test_margin_stays_null_when_cost_missing(upgraded_engine) -> None:
    factory = sessionmaker(bind=upgraded_engine, expire_on_commit=False)
    with factory() as session:
        opportunity = Opportunity(source="test", source_id=uuid4().hex, raw={"fixture": True})
        session.add(opportunity)
        session.flush()
        pursuit = Pursuit(opportunity_id=opportunity.id, quote_price=Decimal(10))
        session.add(pursuit)
        session.commit()
        session.refresh(pursuit)
        assert pursuit.margin_pct is None


def _assert_schema(engine) -> None:
    inspector = inspect(engine)
    db_tables = set(inspector.get_table_names()) - {"alembic_version"}
    model_tables = set(Base.metadata.tables)
    assert db_tables == model_tables
    for name, table in Base.metadata.tables.items():
        db_columns = {column["name"] for column in inspector.get_columns(name)}
        model_columns = {column.name for column in table.columns}
        assert db_columns == model_columns, name
    opportunity_indexes = {index["name"] for index in inspector.get_indexes("opportunities")}
    assert "opportunities_fts" in opportunity_indexes
    prompt_indexes = {index["name"] for index in inspector.get_indexes("prompt_registry")}
    assert "prompt_registry_one_active" in prompt_indexes
    proposal_fks = inspector.get_foreign_keys("proposals")
    assert any(fk["referred_table"] == "proposal_versions" for fk in proposal_fks)


def test_repository_layout_files_exist() -> None:
    root = repo_root()
    expected = [
        "pyproject.toml",
        "docker-compose.yml",
        "alembic.ini",
        "scheduler.py",
        "scripts/smoke.sh",
        "src/govcon/config.py",
        "src/govcon/db.py",
        "src/govcon/models.py",
        "src/govcon/logging.py",
        "src/govcon/http.py",
        "src/govcon/cli.py",
        "src/govcon/ingest/sam_opportunities.py",
        "src/govcon/decision/providers/jev.py",
        "src/govcon/prompts/jev/bid_decision_v1.yaml",
        "src/govcon/security/classification.py",
        "src/govcon/web/app.py",
    ]
    for relative in expected:
        assert (root / relative).is_file(), relative
    assert not (root / ".env").exists() or ".env" in Path(root / ".gitignore").read_text(encoding="utf-8")
