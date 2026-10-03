"""Phase 17 — Scheduling & operations tests.

Tests cover:
- Chain definitions match the spec schedule (6 chains, correct step names)
- govcon status shows last success/failure and row counts
- govcon jobs list lists all defined chains
- govcon jobs run <job> runs a chain and records results
- Hard predecessor failure aborts dependent steps
- Unrelated chains (independent jobs) are unaffected by a sibling failure
- Every run is visible via SchedulerJobRun (i.e., /ops data layer)
- No silent scheduler failures (errors always recorded)
- Chain result persisted to scheduler_job_runs table
- Individual step results persist to ingestion_runs (ingest steps)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from govcon.cli import app
from govcon.db import session_scope
from govcon.models import IngestionRun, SchedulerJobRun
from govcon.scheduler.chains import (
    ALL_CHAIN_NAMES,
    CHAIN_DEFINITIONS,
    ChainDef,
    run_chain,
)
from govcon.scheduler.jobs import StepResult

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoke(*args: str, env: dict | None = None):
    import os

    # Honor the suite's DATABASE_URL so the CLI never touches another database.
    default_env = {"DATABASE_URL": os.environ.get("DATABASE_URL", "postgresql+psycopg://govcon:govcon@localhost:5432/govcon")}
    if env:
        default_env.update(env)
    return runner.invoke(app, list(args), env=default_env)


def _settings():
    import os
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://govcon:govcon@localhost:5432/govcon")
    from govcon.config import get_settings
    return get_settings()


# ---------------------------------------------------------------------------
# 1. Chain definitions match the spec
# ---------------------------------------------------------------------------


class TestChainDefinitions:
    def test_six_chains_defined(self):
        assert len(CHAIN_DEFINITIONS) == 6

    def test_chain_names_match_spec(self):
        assert set(ALL_CHAIN_NAMES) == {
            "morning_ingest",
            "usaspending",
            "embeddings",
            "midday_check",
            "evening_ingest",
            "sunday_sweep",
        }

    def test_morning_ingest_steps(self):
        chain = CHAIN_DEFINITIONS["morning_ingest"]
        # ADR-069: ranking and controlled auto-pursue run between matching and alerts.
        assert chain.steps == ["sam_ingest", "dibbs_ingest", "source_changes", "match", "rank", "auto_pursue", "alerts"]

    def test_usaspending_steps(self):
        chain = CHAIN_DEFINITIONS["usaspending"]
        assert "usaspending" in chain.steps

    def test_embeddings_steps(self):
        chain = CHAIN_DEFINITIONS["embeddings"]
        assert "embeddings" in chain.steps
        assert "semantic_match" in chain.steps

    def test_midday_check_steps(self):
        chain = CHAIN_DEFINITIONS["midday_check"]
        assert "midday_deadline_check" in chain.steps

    def test_evening_ingest_steps(self):
        chain = CHAIN_DEFINITIONS["evening_ingest"]
        assert chain.steps == ["sam_ingest", "dibbs_ingest", "source_changes", "match", "rank", "auto_pursue", "alerts"]

    def test_sunday_sweep_steps(self):
        chain = CHAIN_DEFINITIONS["sunday_sweep"]
        assert chain.steps == ["archive_sweep", "cache_refresh", "analytics_refresh", "vacuum_analyze"]

    def test_all_chains_have_cron_description(self):
        for name, chain in CHAIN_DEFINITIONS.items():
            assert chain.cron, f"chain {name} has no cron description"
            assert chain.description, f"chain {name} has no description"


# ---------------------------------------------------------------------------
# 2. govcon status command
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("upgraded_engine")  # migrated schema; skipped without a DB
class TestStatusCommand:
    def test_status_shows_connectivity(self):
        result = _invoke("status")
        assert result.exit_code == 0
        assert "database_connectivity: ok" in result.output

    def test_status_shows_schema_revision(self):
        result = _invoke("status")
        assert result.exit_code == 0
        assert "schema_revision:" in result.output
        from alembic.script import ScriptDirectory

        from govcon.cli import alembic_config

        head = ScriptDirectory.from_config(alembic_config()).get_current_head()
        assert head in result.output

    def test_status_shows_row_counts(self):
        result = _invoke("status")
        assert result.exit_code == 0
        assert "opportunities_total:" in result.output
        assert "opportunities_open:" in result.output

    def test_status_shows_job_run_section(self):
        result = _invoke("status")
        assert result.exit_code == 0
        assert "last job runs" in result.output.lower() or "never_run" in result.output

    def test_status_shows_last_run_after_a_chain_runs(self):
        settings = _settings()
        # Run midday_check chain directly (it uses archive_expired_sam_opportunities internally)
        run_chain("midday_check", settings, trigger="test_status_check")

        # Now status should show it
        result = _invoke("status")
        assert result.exit_code == 0
        # midday_check should have a last run now
        assert "midday_check" in result.output


# ---------------------------------------------------------------------------
# 3. govcon jobs list command
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("upgraded_engine")  # migrated schema; skipped without a DB
class TestJobsList:
    def test_lists_all_six_chains(self):
        result = _invoke("jobs", "list")
        assert result.exit_code == 0
        for chain_name in ALL_CHAIN_NAMES:
            assert chain_name in result.output

    def test_shows_schedule_for_each_chain(self):
        result = _invoke("jobs", "list")
        assert result.exit_code == 0
        assert "daily" in result.output
        assert "Sunday" in result.output

    def test_shows_steps_for_each_chain(self):
        result = _invoke("jobs", "list")
        assert result.exit_code == 0
        assert "sam_ingest" in result.output
        assert "archive_sweep" in result.output

    def test_shows_never_run_for_fresh_chains(self):
        # There may or may not be runs; it should show "never_run" or a timestamp
        result = _invoke("jobs", "list")
        assert result.exit_code == 0
        # At least one column should mention the schedule
        assert "06:30 daily" in result.output or "schedule" in result.output.lower()


# ---------------------------------------------------------------------------
# 4. govcon jobs run <job> command
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("upgraded_engine")  # migrated schema; skipped without a DB
class TestJobsRun:
    def test_run_midday_check_succeeds(self):
        result = _invoke("jobs", "run", "midday_check")
        assert result.exit_code == 0
        assert "status: succeeded" in result.output
        assert "run_id:" in result.output

    def test_run_unknown_chain_exits_2(self):
        result = _invoke("jobs", "run", "nonexistent_chain_xyz")
        assert result.exit_code == 2
        assert "unknown chain" in result.output.lower()

    def test_run_shows_steps_completed(self):
        result = _invoke("jobs", "run", "midday_check")
        assert result.exit_code == 0
        assert "steps_completed" in result.output
        assert "midday_deadline_check" in result.output

    def test_run_records_job_in_db(self):
        settings = _settings()
        result = _invoke("jobs", "run", "midday_check")
        assert result.exit_code == 0

        with session_scope(settings) as db:
            run = db.scalars(
                select(SchedulerJobRun)
                .where(SchedulerJobRun.chain_name == "midday_check")
                .order_by(SchedulerJobRun.started_at.desc())
                .limit(1)
            ).first()
        assert run is not None
        assert run.status == "succeeded"
        assert run.finished_at is not None
        assert run.steps_completed is not None

    def test_run_failed_chain_exits_1(self):
        _settings()
        from govcon.scheduler.jobs import StepResult

        def always_fail(session, settings=None):
            return StepResult(step="midday_deadline_check", status="failed", error="test failure")

        with patch(
            "govcon.scheduler.chains._STEP_FUNCTIONS",
            {"midday_deadline_check": always_fail},
        ):
            result = _invoke("jobs", "run", "midday_check")
        assert result.exit_code == 1
        assert "failed" in result.output.lower()


# ---------------------------------------------------------------------------
# 5. Hard predecessor failure aborts dependent steps
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("upgraded_engine")  # migrated schema; skipped without a DB
class TestChainAbortOnFailure:
    def test_first_step_failure_aborts_remaining_steps(self):
        settings = _settings()

        call_log: list[str] = []

        def step_fail(session, settings=None):
            call_log.append("step_fail")
            return StepResult(step="test_fail", status="failed", error="deliberate failure")

        def step_should_not_run(session, settings=None):
            call_log.append("step_should_not_run")
            return StepResult(step="test_ok", status="succeeded")

        custom_chain = ChainDef(
            name="morning_ingest",
            description="test chain",
            cron="test",
            steps=["sam_ingest", "dibbs_ingest"],
        )

        from govcon.scheduler.chains import _STEP_FUNCTIONS
        patched_fns = dict(_STEP_FUNCTIONS)
        patched_fns["sam_ingest"] = step_fail
        patched_fns["dibbs_ingest"] = step_should_not_run

        with (
            patch("govcon.scheduler.chains._STEP_FUNCTIONS", patched_fns),
            patch("govcon.scheduler.chains.CHAIN_DEFINITIONS", {"morning_ingest": custom_chain}),
        ):
            result = run_chain("morning_ingest", settings, trigger="test")

        assert result.failed
        assert result.failed_step == "sam_ingest"
        # dibbs_ingest should NOT have been called
        assert "step_should_not_run" not in call_log
        # The job_run in DB should reflect the failure
        with session_scope(settings) as db:
            run = db.scalars(
                select(SchedulerJobRun)
                .where(SchedulerJobRun.chain_name == "morning_ingest")
                .order_by(SchedulerJobRun.started_at.desc())
                .limit(1)
            ).first()
        assert run is not None
        assert run.status == "failed"
        assert run.failed_step == "sam_ingest"

    def test_steps_completed_excludes_failed_step(self):
        settings = _settings()

        def step_ok(session, settings=None):
            return StepResult(step="archive_sweep", status="succeeded")

        def step_fail(session, settings=None):
            return StepResult(step="cache_refresh", status="failed", error="fail")

        def step_never(session, settings=None):
            return StepResult(step="analytics_refresh", status="succeeded")

        custom_chain = ChainDef(
            name="sunday_sweep",
            description="test",
            cron="test",
            steps=["archive_sweep", "cache_refresh", "analytics_refresh"],
        )

        from govcon.scheduler.chains import _STEP_FUNCTIONS
        patched = dict(_STEP_FUNCTIONS)
        patched["archive_sweep"] = step_ok
        patched["cache_refresh"] = step_fail
        patched["analytics_refresh"] = step_never

        with patch("govcon.scheduler.chains._STEP_FUNCTIONS", patched), patch(
            "govcon.scheduler.chains.CHAIN_DEFINITIONS",
            {"sunday_sweep": custom_chain},
        ):
            result = run_chain("sunday_sweep", settings, trigger="test")

        assert result.failed
        assert "archive_sweep" in result.steps_completed
        assert "cache_refresh" not in result.steps_completed
        assert "analytics_refresh" not in result.steps_completed


# ---------------------------------------------------------------------------
# 6. Unrelated chains are independent
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("upgraded_engine")  # migrated schema; skipped without a DB
class TestIndependentChains:
    def test_failure_in_one_chain_does_not_affect_another(self):
        """Two chains run independently; one failing does not prevent the other."""
        settings = _settings()

        def step_fail(session, settings=None):
            return StepResult(step="midday_deadline_check", status="failed", error="fake failure")

        patched = {"midday_deadline_check": step_fail}

        with patch("govcon.scheduler.chains._STEP_FUNCTIONS", patched):
            result_midday = run_chain("midday_check", settings, trigger="test")

        assert result_midday.failed

        # Now run sunday_sweep, which is independent
        result_sunday = run_chain("sunday_sweep", settings, trigger="test")
        # sunday_sweep should succeed (all its steps are independent of midday)
        assert result_sunday.status == "succeeded", (
            f"sunday_sweep failed unexpectedly: {result_sunday.error}"
        )


# ---------------------------------------------------------------------------
# 7. Every run visible in scheduler_job_runs (/ops data layer)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("upgraded_engine")  # migrated schema; skipped without a DB
class TestRunVisibility:
    def test_completed_run_persisted_to_db(self):
        settings = _settings()
        run_chain("midday_check", settings, trigger="test_visibility")

        with session_scope(settings) as db:
            run = db.scalars(
                select(SchedulerJobRun)
                .where(SchedulerJobRun.chain_name == "midday_check")
                .where(SchedulerJobRun.trigger == "test_visibility")
                .order_by(SchedulerJobRun.started_at.desc())
                .limit(1)
            ).first()
        assert run is not None
        assert run.status in ("succeeded", "failed")
        assert run.started_at is not None
        assert run.finished_at is not None

    def test_run_has_row_counts(self):
        settings = _settings()
        run_chain("midday_check", settings, trigger="test_row_counts")

        with session_scope(settings) as db:
            run = db.scalars(
                select(SchedulerJobRun)
                .where(SchedulerJobRun.chain_name == "midday_check")
                .where(SchedulerJobRun.trigger == "test_row_counts")
                .order_by(SchedulerJobRun.started_at.desc())
                .limit(1)
            ).first()
        assert run is not None
        assert run.row_counts is not None
        assert isinstance(run.row_counts, dict)

    def test_failed_run_records_failed_step(self):
        settings = _settings()

        def step_fail(session, settings=None):
            return StepResult(step="midday_deadline_check", status="failed", error="deliberate")

        with patch("govcon.scheduler.chains._STEP_FUNCTIONS", {"midday_deadline_check": step_fail}):
            run_chain("midday_check", settings, trigger="test_failed_step")

        with session_scope(settings) as db:
            run = db.scalars(
                select(SchedulerJobRun)
                .where(SchedulerJobRun.chain_name == "midday_check")
                .where(SchedulerJobRun.trigger == "test_failed_step")
                .order_by(SchedulerJobRun.started_at.desc())
                .limit(1)
            ).first()
        assert run is not None
        assert run.status == "failed"
        assert run.failed_step == "midday_deadline_check"
        assert run.error is not None


# ---------------------------------------------------------------------------
# 8. No silent scheduler failures
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("upgraded_engine")  # migrated schema; skipped without a DB
class TestNoSilentFailures:
    def test_exception_in_step_is_recorded_not_swallowed(self):
        settings = _settings()

        def step_raises(session, settings=None):
            raise RuntimeError("unexpected step error")

        with patch(
            "govcon.scheduler.chains._STEP_FUNCTIONS",
            {"midday_deadline_check": step_raises},
        ):
            result = run_chain("midday_check", settings, trigger="test_no_silent")

        assert result.failed
        assert result.error is not None
        assert "unexpected step error" in result.error

        # Confirm it's in the DB as a failed run
        with session_scope(settings) as db:
            run = db.scalars(
                select(SchedulerJobRun)
                .where(SchedulerJobRun.chain_name == "midday_check")
                .where(SchedulerJobRun.trigger == "test_no_silent")
                .order_by(SchedulerJobRun.started_at.desc())
                .limit(1)
            ).first()
        assert run is not None
        assert run.status == "failed"

    def test_step_result_failure_propagates_to_chain(self):
        settings = _settings()

        def step_returns_fail(session, settings=None):
            return StepResult(step="midday_deadline_check", status="failed", error="inner error")

        with patch(
            "govcon.scheduler.chains._STEP_FUNCTIONS",
            {"midday_deadline_check": step_returns_fail},
        ):
            result = run_chain("midday_check", settings, trigger="test_propagate")

        assert result.failed
        assert result.error == "inner error"


# ---------------------------------------------------------------------------
# 9. StepResult row_counts helper
# ---------------------------------------------------------------------------


class TestStepResult:
    def test_row_counts_returns_dict(self):
        sr = StepResult(step="sam_ingest", status="succeeded", fetched=100, inserted=50, updated=5, unchanged=45)
        counts = sr.row_counts()
        assert counts["fetched"] == 100
        assert counts["inserted"] == 50
        assert counts["updated"] == 5
        assert counts["unchanged"] == 45

    def test_failed_property(self):
        assert StepResult(step="x", status="failed").failed
        assert not StepResult(step="x", status="succeeded").failed
        assert not StepResult(step="x", status="skipped").failed


# ---------------------------------------------------------------------------
# 10. Ingest steps write ingestion_runs (spot-check one)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("upgraded_engine")  # migrated schema; skipped without a DB
class TestIngestStepPersistence:
    def test_midday_step_writes_ingestion_run(self):
        settings = _settings()
        before_count = 0
        with session_scope(settings) as db:
            from sqlalchemy import func
            before_count = db.scalar(
                select(func.count()).select_from(IngestionRun).where(
                    IngestionRun.job == "sched:midday_check"
                )
            ) or 0

        run_chain("midday_check", settings, trigger="test_ingest_write")

        with session_scope(settings) as db:
            from sqlalchemy import func
            after_count = db.scalar(
                select(func.count()).select_from(IngestionRun).where(
                    IngestionRun.job == "sched:midday_check"
                )
            ) or 0

        assert after_count > before_count


# ---------------------------------------------------------------------------
# 11. APScheduler runner imports and basic structure
# ---------------------------------------------------------------------------


class TestSchedulerRunner:
    def test_runner_module_importable(self):
        from govcon.scheduler.runner import start_blocking_scheduler
        assert callable(start_blocking_scheduler)

    def test_scheduler_start_command_exists(self):
        """Check that the CLI command is registered."""
        result = runner.invoke(app, ["scheduler", "--help"])
        assert result.exit_code == 0
        assert "start" in result.output

    def test_jobs_commands_exist(self):
        result = runner.invoke(app, ["jobs", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "run" in result.output


# ---------------------------------------------------------------------------
# 12. Sunday sweep includes vacuum analyze step
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("upgraded_engine")  # migrated schema; skipped without a DB
class TestSundaySweep:
    def test_sunday_sweep_includes_vacuum(self):
        chain = CHAIN_DEFINITIONS["sunday_sweep"]
        assert "vacuum_analyze" in chain.steps

    def test_sunday_sweep_full_chain_runs(self):
        """End-to-end: sunday_sweep chain completes all 4 steps."""
        result = _invoke("jobs", "run", "sunday_sweep")
        assert result.exit_code == 0
        assert "status: succeeded" in result.output
        assert "archive_sweep" in result.output
        assert "cache_refresh" in result.output
        assert "analytics_refresh" in result.output
        assert "vacuum_analyze" in result.output


# ---------------------------------------------------------------------------
# H11. Ingest failures do not stop matching and alerts
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("upgraded_engine")
class TestSoftIngestSteps:
    def _patched(self, calls: list[str], **overrides):
        from govcon.scheduler.chains import _STEP_FUNCTIONS

        def make(name: str, status: str = "succeeded", error: str | None = None):
            def step(session, settings=None):
                calls.append(name)
                return StepResult(step=name, status=status, error=error)

            return step

        patched = dict(_STEP_FUNCTIONS)
        for name in ("sam_ingest", "dibbs_ingest", "source_changes", "match", "rank", "auto_pursue", "alerts"):
            patched[name] = overrides.get(name) or make(name)
        return patched, make

    def test_failed_sam_ingest_still_runs_match_and_alerts(self):
        calls: list[str] = []
        patched, make = self._patched(calls)
        patched["sam_ingest"] = make("sam_ingest", "failed", "SAM HTTP 503")
        with patch("govcon.scheduler.chains._STEP_FUNCTIONS", patched):
            result = run_chain("morning_ingest", _settings(), trigger="test_soft")
        assert calls == ["sam_ingest", "dibbs_ingest", "source_changes", "match", "rank", "auto_pursue", "alerts"]
        assert result.status == "completed_with_errors"
        assert not result.failed
        assert "match" in result.steps_completed and "alerts" in result.steps_completed
        assert "SAM HTTP 503" in (result.error or "")

    def test_partial_dibbs_errors_are_not_a_failure(self):
        calls: list[str] = []
        patched, make = self._patched(calls)
        patched["dibbs_ingest"] = make("dibbs_ingest", "completed_with_errors", "line 17: expected 140 characters")
        with patch("govcon.scheduler.chains._STEP_FUNCTIONS", patched):
            result = run_chain("morning_ingest", _settings(), trigger="test_soft")
        assert result.status == "completed_with_errors"
        assert "alerts" in result.steps_completed

    def test_match_failure_still_stops_alerts(self):
        calls: list[str] = []
        patched, make = self._patched(calls)
        patched["match"] = make("match", "failed", "matching crashed")
        with patch("govcon.scheduler.chains._STEP_FUNCTIONS", patched):
            result = run_chain("morning_ingest", _settings(), trigger="test_soft")
        assert result.failed and result.failed_step == "match"
        assert "alerts" not in calls

    def test_dibbs_step_reports_bad_lines_as_completed_with_errors(self):
        from govcon.ingest.dibbs import DibbsCoverage, DibbsIngestResult
        from govcon.ingest.runs import IngestStats
        from govcon.scheduler.jobs import step_dibbs_ingest

        fake = DibbsIngestResult(
            stats=IngestStats(fetched=10, inserted=9, errors=["line 4: expected 140 characters, got 12"]),
            coverage=DibbsCoverage(records=10, nsn=9, quantity=10),
            index_name="in260925.txt",
        )
        with session_scope(_settings()) as db, patch("govcon.ingest.dibbs.pull_dibbs_index", return_value=fake):
            result = step_dibbs_ingest(db, _settings())
        assert result.status == "completed_with_errors"
        assert not result.failed

    def test_cli_exits_zero_for_completed_with_errors(self):
        from govcon.scheduler.chains import _STEP_FUNCTIONS

        def partial(session, settings=None):
            return StepResult(step="midday_deadline_check", status="completed_with_errors", error="1 row skipped")

        with patch("govcon.scheduler.chains._STEP_FUNCTIONS", {**_STEP_FUNCTIONS, "midday_deadline_check": partial}):
            result = _invoke("jobs", "run", "midday_check")
        assert result.exit_code == 0
        assert "completed_with_errors" in result.output
