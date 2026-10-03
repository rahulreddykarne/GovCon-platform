"""Regression tests for outcome, package, review and scheduler integrity."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4
import zipfile

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, DBAPIError
from sqlalchemy.orm import Session

from govcon.models import (
    AIAnalysis, Match, Opportunity, OutcomeCorrection, OutcomeFeedback,
    PackageManifest, Proposal, ProposalVersion, Pursuit, Requirement,
    ReviewAssignment, SchedulerJobRun, Submission, Watchlist,
)


@pytest.fixture
def db(upgraded_engine):
    with Session(upgraded_engine) as session:
        yield session
        session.rollback()


def opportunity(db):
    opp = Opportunity(source="sam", source_id=uuid4().hex, title="Supplies", status="open", raw={}, links={}, posted_date=datetime.now(UTC).date(), response_deadline=datetime.now(UTC) + timedelta(days=7))
    db.add(opp)
    db.flush()
    return opp


def actor(db):
    from govcon.collaboration.users import invite_user
    return invite_user(db, email=f"m5-{uuid4().hex}@example.test", display_name="Approver", password="correct horse battery", role="approver")


def submitted(db, opp, supplier="Acme"):
    pursuit = Pursuit(opportunity_id=opp.id, stage="submitted", submitted_at=datetime.now(UTC), supplier=supplier)
    db.add(pursuit)
    db.flush()
    sub = Submission(opportunity_id=opp.id, pursuit_id=pursuit.id, status="submitted", submitted_at=pursuit.submitted_at)
    db.add(sub)
    db.flush()
    return sub


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("schema,field", [("won", "award_amount"), ("lost", "known_winning_price"), ("won", "win_margin_pct")])
def test_invalid_amounts_rejected(value, schema, field):
    from govcon.learning.schemas import OUTCOME_SCHEMAS
    with pytest.raises(ValidationError):
        OUTCOME_SCHEMAS[schema].model_validate({"outcome": schema, field: value})


def test_outcome_specific_fields_and_dates():
    from govcon.learning.schemas import OUTCOME_SCHEMAS
    for payload in ({"outcome": "won", "win_margin_pct": 101}, {"outcome": "won", "award_date": "2026-02-30"}, {"outcome": "won", "award_date": "2026-01-01junk"}, {"outcome": "won", "loss_reason": "Wrong outcome"}):
        with pytest.raises(ValidationError):
            OUTCOME_SCHEMAS[payload["outcome"]].model_validate(payload)


def test_outcome_upsert_keeps_full_immutable_actor_history(db):
    from govcon.learning.outcomes import record_outcome
    from govcon.learning.analytics import outcome_analytics
    opp = opportunity(db)
    submitted(db, opp)
    user = actor(db)
    baseline_wins = outcome_analytics(db).total_won
    first = record_outcome(db, opportunity_id=opp.id, outcome="won", actor=user, award_amount=10, award_date=datetime.now(UTC).date())
    second = record_outcome(db, opportunity_id=opp.id, outcome="won", actor=user, award_amount=20, win_margin_pct=25)
    assert first.id == second.id
    history = db.scalars(select(OutcomeCorrection).where(OutcomeCorrection.opportunity_id == opp.id).order_by(OutcomeCorrection.id)).all()
    assert len(history) == 2 and all(h.actor_id == user.id for h in history)
    assert Decimal(history[1].old_value["award_amount"]) == 10
    assert Decimal(history[1].new_value["award_amount"]) == 20
    assert outcome_analytics(db).total_won == baseline_wins + 1
    with pytest.raises(DBAPIError), db.begin_nested():
        db.execute(text("UPDATE outcome_corrections SET old_value = '{}' WHERE id = :id"), {"id": history[0].id})
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(OutcomeFeedback(opportunity_id=opp.id, outcome="lost"))
        db.flush()


def test_won_requires_real_submission_and_cannot_change_terminal_outcome(db):
    from govcon.learning.outcomes import record_outcome
    opp = opportunity(db)
    user = actor(db)
    db.add(Pursuit(opportunity_id=opp.id, stage="submitted"))
    db.flush()
    with pytest.raises(ValueError, match="recorded submission"):
        record_outcome(db, opportunity_id=opp.id, outcome="won", actor=user)
    pursuit = db.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp.id))
    db.add(Submission(opportunity_id=opp.id, pursuit_id=pursuit.id, status="submitted", submitted_at=datetime.now(UTC)))
    db.flush()
    record_outcome(db, opportunity_id=opp.id, outcome="won", actor=user)
    with pytest.raises(ValueError, match="terminal outcome cannot change"):
        record_outcome(db, opportunity_id=opp.id, outcome="lost", actor=user)


def test_supplier_denominator_and_amendment_count(db, monkeypatch):
    from govcon.learning.outcomes import record_outcome
    from govcon.learning.analytics import reliable_suppliers, avg_cycle_times
    user = actor(db)
    for outcome in ("won", "lost"):
        opp = opportunity(db)
        submitted(db, opp)
        record_outcome(db, opportunity_id=opp.id, outcome=outcome, actor=user)
    supplier = reliable_suppliers(db)[0]
    assert (supplier.wins, supplier.total, supplier.win_rate_pct) == (1, 2, 50)
    docs = [SimpleNamespace(document_type="amendment", amendment_number=n) for n in [1, 1, 2]]
    monkeypatch.setattr("govcon.compliance.inventory.load_inventory", lambda *a: SimpleNamespace(documents=docs))
    assert avg_cycle_times(db)["avg_amendment_count"] == 2


@pytest.mark.parametrize("key", ["recipient_email", "submission_method", "submission_portal", "portal_url"])
def test_conflicting_destinations_are_not_guessed(key):
    from govcon.submissions.service import _extract_submission_info
    info = _extract_submission_info([Requirement(key_values={key: "one"}), Requirement(key_values={key: "two"})])
    assert info["destination_conflicts"]
    assert all(info[k] is None for k in ("submission_method", "submission_destination", "recipient_email", "portal_url", "portal_name"))


def test_conflict_finding_and_unique_submission(db):
    from govcon.submissions.service import generate_submission_package
    from govcon.compliance.matrix import open_findings
    opp = opportunity(db)
    pursuit = Pursuit(opportunity_id=opp.id, stage="drafting")
    db.add(pursuit)
    db.add_all([Requirement(opportunity_id=opp.id, requirement_text="Send here", key_values={"recipient_email": email}, status="satisfied") for email in ("one@example.test", "two@example.test")])
    db.flush()
    result = generate_submission_package(db, opportunity_id=opp.id)
    assert result["recipient_email"] is None
    assert any(f.finding_type == "submission_destination_conflict" and f.blocks_submission for f in open_findings(db, opp.id))
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(Submission(opportunity_id=opp.id, pursuit_id=pursuit.id))
        db.flush()


def test_manifest_checklist_completed_actions_and_changed_content(db, tmp_path):
    from govcon.compliance.deterministic import PackageFile, SubmissionPackage
    from govcon.submissions.manifest import assemble_package, current_package, verify_package
    from govcon.submissions.checklist import generate_final_checklist
    opp = opportunity(db)
    sub = submitted(db, opp)
    sub.required_files = {"files": ["SF30.pdf"]}
    sub.status = "preparing"
    sub.required_actions = {"amendment_acknowledgments": ["0001"]}
    file = tmp_path / "SF30.pdf"
    file.write_bytes(b"signed amendment")
    package = SubmissionPackage(files=[PackageFile("SF30.pdf", local_path=str(file), role="acknowledgment", signed=True)], amendments_acknowledged=["0001"])
    manifest = assemble_package(db, opportunity_id=opp.id, package=package, actor=actor(db))
    assert manifest.manifest["files"][0]["sha256"] == hashlib.sha256(file.read_bytes()).hexdigest()
    checklist = generate_final_checklist(db, opportunity_id=opp.id)
    statuses = {i["label"]: i["status"] for i in checklist["items"]}
    assert statuses["Required files"] == statuses["Amendment acknowledgments"] == "ready"
    package.files[0].name = "mutated"
    assert current_package(db, sub).files[0].name == "SF30.pdf"
    with pytest.raises(DBAPIError), db.begin_nested():
        db.execute(text("DELETE FROM submission_package_manifests WHERE id = :id"), {"id": manifest.id})
    file.write_bytes(b"changed content")
    assert verify_package(current_package(db, sub))
    assert generate_final_checklist(db, opportunity_id=opp.id)["overall"] == "blocked"


@pytest.mark.parametrize("value", ["=HYPERLINK(1)", "+cmd", "-1+2", "@SUM(1)", "  \t=SUM(1)"])
def test_spreadsheet_formula_prefixes_escaped(value):
    from govcon.proposals.export import spreadsheet_text
    assert spreadsheet_text(value) == "'" + value


def test_zip_contains_supporting_files_hashes_pdf_and_fails_on_generation_error(db, tmp_path, monkeypatch):
    from govcon.proposals.export import export_submission_zip
    from govcon.compliance.deterministic import PackageFile, SubmissionPackage
    from govcon.submissions.manifest import assemble_package
    opp = opportunity(db)
    sub = submitted(db, opp)
    sub.required_files = {"files": ["certificate.pdf"]}
    sub.status = "preparing"
    proposal = Proposal(opportunity_id=opp.id, pursuit_id=sub.pursuit_id, status="draft")
    db.add(proposal)
    db.flush()
    version = ProposalVersion(proposal_id=proposal.id, version_number=1, created_by="human")
    db.add(version)
    db.flush()
    proposal.current_version_id = version.id
    file = tmp_path / "certificate.pdf"
    file.write_bytes(b"supporting evidence")
    assemble_package(db, opportunity_id=opp.id, package=SubmissionPackage(files=[PackageFile("certificate.pdf", local_path=str(file))]), actor=actor(db))
    archive = export_submission_zip(db, opportunity_id=opp.id)
    with zipfile.ZipFile(BytesIO(archive)) as zf:
        assert zf.read("certificate.pdf") == b"supporting evidence"
        assert zf.read("proposal_v1.pdf").startswith(b"%PDF-")
        for entry in json.loads(zf.read("file_manifest.json"))["files"]:
            assert entry["sha256"] == hashlib.sha256(zf.read(entry["filename"])).hexdigest()
    def fail(*args, **kwargs):
        raise RuntimeError("coverage failed")
    monkeypatch.setattr("govcon.proposals.export.export_coverage_xlsx", fail)
    with pytest.raises(RuntimeError, match="coverage failed"):
        export_submission_zip(db, opportunity_id=opp.id)
    file.unlink()
    with pytest.raises(ValueError, match="unavailable"):
        export_submission_zip(db, opportunity_id=opp.id)


def test_email_does_not_invent_missing_proposal(db):
    from govcon.submissions.email_adapter import draft_submission_email
    result = draft_submission_email(db, opportunity_id=opportunity(db).id)
    assert result["attachments"] == []
    assert result["notes"]


def test_jev_block_clears_when_satisfied_and_on_rerouting(db, monkeypatch):
    from govcon.compliance.matrix import StatusDecision, apply_decision
    from govcon.compliance.validator import run_jev_routing
    opp = opportunity(db)
    req = Requirement(opportunity_id=opp.id, requirement_text="Optional requirement", mandatory=False, severity="low", status="missing", blocks_submission=True, validation={"jev_blocks_submission": True})
    db.add(req)
    db.flush()
    monkeypatch.setattr("govcon.decision.engine.build_decision_state", lambda *a: {})
    monkeypatch.setattr("govcon.decision.engine.run_decision_bundle", lambda *a, **k: SimpleNamespace(result={}, run=SimpleNamespace(id=1), provider="rules", model="rules"))
    run_jev_routing(db, opp.id)
    assert not req.blocks_submission and not req.validation.get("jev_blocks_submission")
    req.validation = {"jev_blocks_submission": True}
    req.blocks_submission = True
    apply_decision(req, StatusDecision("satisfied", "verified", False, ["human"], False))
    assert not req.blocks_submission and not req.validation.get("jev_blocks_submission")


@pytest.mark.parametrize("low,high,expected", [(100, 300, "~$200"), (None, 300, "~$300"), (0, 300, "~$150"), (300, None, "~$300")])
def test_displayed_midpoint(low, high, expected):
    from govcon.web.helpers import format_value
    assert format_value(Decimal(low) if low is not None else None, Decimal(high) if high is not None else None) == expected


def test_embedding_rebuilds_changed_source_and_model_and_uses_active_matches(db):
    from govcon.enrich.embeddings import run_embedding_job, build_watchlist_profiles
    class Provider:
        model_version = "model-v1"
        def embed(self, text):
            return [1.0 if "Supplies" in text else 0.0] * 384
    provider = Provider()
    opp = opportunity(db)
    assert run_embedding_job(db, provider)["embedded"] >= 1
    source = opp.embedding_source_hash
    assert run_embedding_job(db, provider)["embedded"] == 0
    opp.description = "new description"
    assert run_embedding_job(db, provider)["embedded"] == 1
    assert opp.embedding_source_hash != source
    provider.model_version = "model-v2"
    assert run_embedding_job(db, provider)["embedded"] >= 1
    assert opp.embedding_model == "model-v2" and opp.embedding_dimension == 384
    wl = Watchlist(name="criteria", enabled=True)
    db.add(wl)
    db.flush()
    db.add(Match(opportunity_id=opp.id, watchlist_id=wl.id, active=True))
    db.flush()
    build_watchlist_profiles(db, provider)
    assert list(wl.embedding) == [0.5] * 384


def test_embedding_model_identity_tracks_resolved_commit():
    from govcon.enrich.embeddings import SentenceTransformerProvider
    def provider(commit):
        result = SentenceTransformerProvider("same-model-alias")
        result._model = [SimpleNamespace(auto_model=SimpleNamespace(config=SimpleNamespace(_commit_hash=commit)))]
        return result
    assert provider("revision-one").model_version != provider("revision-two").model_version


def test_scheduler_lock_excludes_another_worker_and_recovers_stale_runs(upgraded_engine, monkeypatch):
    from govcon.config import get_settings
    from govcon.scheduler.chains import ChainDef, run_chain, _chain_lock_key
    settings = get_settings()
    lock_key = _chain_lock_key("midday_check")
    with upgraded_engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(742901, :key)"), {"key": lock_key})
        connection.commit()
        try:
            assert run_chain("midday_check", settings).status == "skipped"
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(742901, :key)"), {"key": lock_key})
            connection.commit()
    with Session(upgraded_engine) as db:
        stale = SchedulerJobRun(chain_name="midday_check", trigger="test", status="running", started_at=datetime.now(UTC))
        db.add(stale)
        db.commit()
        stale_id = stale.id
    monkeypatch.setattr("govcon.scheduler.chains.CHAIN_DEFINITIONS", {"midday_check": ChainDef("midday_check", "test", "test", [])})
    assert run_chain("midday_check", settings).status == "succeeded"
    with Session(upgraded_engine) as db:
        stale = db.get(SchedulerJobRun, stale_id)
        assert stale.status == "failed" and stale.finished_at is not None


def test_persistent_scheduler_jobs_are_serializable_and_preserve_due_times(upgraded_engine):
    from govcon.config import get_settings
    from govcon.scheduler.runner import _configured_scheduler
    scheduler = _configured_scheduler(get_settings())
    jobs = scheduler.get_jobs()
    assert len(jobs) == 6
    for job in jobs:
        scheduler._real_add_job(job, "default", replace_existing=True)
    expected = {job.id: job.next_run_time for job in jobs}
    scheduler._jobstores["default"].shutdown()
    restarted = _configured_scheduler(get_settings())
    try:
        for job in restarted.get_jobs():
            assert job.next_run_time == expected[job.id]
            assert job.args == (job.id,)
    finally:
        restarted._jobstores["default"].remove_all_jobs()
        restarted._jobstores["default"].shutdown()


def test_single_policy_honors_explicit_second_review_without_configured_triggers(db, monkeypatch):
    from govcon.collaboration.review_sessions import ensure_review_session, recalculate_quorum
    monkeypatch.setenv("REVIEW_CONDITIONAL_TRIGGERS", "")
    opp = opportunity(db)
    review = ensure_review_session(db, opportunity_id=opp.id)
    review.review_policy = "single"
    db.add(ReviewAssignment(opportunity_id=opp.id, user_id=actor(db).id, status="complete", second_review_requested=True, completed_at=datetime.now(UTC)))
    db.flush()
    result = recalculate_quorum(db, opportunity_id=opp.id)
    assert result.required_review_count == 2 and not result.quorum_satisfied


def test_dual_reviewer_cannot_self_approve(db):
    from govcon.collaboration.review_sessions import ensure_review_session, finalize_approval, ReviewWorkflowError
    opp = opportunity(db)
    user = actor(db)
    review = ensure_review_session(db, opportunity_id=opp.id)
    review.review_policy = "dual"
    db.add(ReviewAssignment(opportunity_id=opp.id, user_id=user.id, status="complete", completed_at=datetime.now(UTC)))
    db.flush()
    with pytest.raises(ReviewWorkflowError, match="did not complete a review"):
        finalize_approval(db, opportunity_id=opp.id, actor=user, action="approve_to_bid", expected_version=review.version, override_reason="Cannot bypass separation of review and approval.")


def test_multiple_summary_rows_return_latest_after_rerun(db):
    from govcon.ai.analysis_types import AnalysisType
    from govcon.enrich.summarize import run_solicitation_analysis, SCHEMA_VERSION
    from govcon.workflow.source_revision import SOURCE_REVISION_KEY, current_source_revision
    opp = opportunity(db)
    for value in ("first", "latest"):
        row = AIAnalysis(opportunity_id=opp.id, analysis_type=AnalysisType.SOLICITATION_SUMMARY, schema_version=SCHEMA_VERSION, context_manifest={SOURCE_REVISION_KEY: current_source_revision(db, opp.id)}, output_json={"value": value})
        db.add(row)
        db.flush()
    assert run_solicitation_analysis(db, opp).id == row.id


def test_migration_preserves_legacy_duplicates_and_roundtrips(upgraded_engine):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).parents[1] / "alembic/versions/01a2b3c4d5e6_outcomes_packages_embeddings.py"
    spec = importlib.util.spec_from_file_location("outcome_package_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with upgraded_engine.connect() as conn:
        transaction = conn.begin()
        try:
            schema = f"m5_migration_{uuid4().hex}"
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            conn.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            for sql in (
                "CREATE TABLE opportunities (id bigint PRIMARY KEY)",
                "CREATE TABLE users (id bigint PRIMARY KEY)",
                "CREATE TABLE watchlists (id bigint PRIMARY KEY)",
                "CREATE TABLE outcome_feedback (id bigint PRIMARY KEY, opportunity_id bigint NOT NULL, outcome text, award_amount numeric, known_winning_price numeric, win_margin_pct numeric, created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now())",
                "CREATE TABLE submissions (id bigint PRIMARY KEY, opportunity_id bigint NOT NULL, status text)",
                "CREATE TABLE proposals (id bigint PRIMARY KEY, submission_id bigint REFERENCES submissions(id))",
                "INSERT INTO opportunities VALUES (1)",
                "INSERT INTO outcome_feedback (id, opportunity_id, outcome, award_amount) VALUES (1, 1, 'won', 10), (2, 1, 'won', 20)",
                "UPDATE outcome_feedback SET known_winning_price = 'NaN', win_margin_pct = 101 WHERE id = 2",
                "INSERT INTO submissions VALUES (1, 1, 'confirmed'), (2, 1, 'preparing')",
                "INSERT INTO proposals VALUES (1, 2)",
            ):
                conn.execute(text(sql))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                assert conn.scalar(text("SELECT count(*) FROM outcome_corrections")) == 2
                assert conn.scalar(text("SELECT count(*) FROM submission_legacy_history")) == 2
                assert conn.scalar(text("SELECT id FROM submissions")) == 1
                assert conn.scalar(text("SELECT submission_id FROM proposals")) == 1
                assert conn.scalar(text("SELECT snapshot->'referencing_proposal_ids' FROM submission_legacy_history WHERE snapshot->>'id' = '2'")) == [1]
                assert conn.execute(text("SELECT id, award_amount, known_winning_price, win_margin_pct FROM outcome_feedback")).one() == (2, Decimal(20), None, None)
                with pytest.raises(DBAPIError), conn.begin_nested():
                    conn.execute(text("UPDATE outcome_corrections SET new_value = '{}'"))
                migration.downgrade()
                migration.upgrade()
                assert conn.scalar(text("SELECT count(*) FROM outcome_feedback")) == 1
        finally:
            transaction.rollback()


def test_concurrent_upserts_keep_one_outcome_and_submission(upgraded_engine):
    from concurrent.futures import ThreadPoolExecutor
    from govcon.learning.outcomes import record_outcome
    from govcon.submissions.service import generate_submission_package
    with Session(upgraded_engine, expire_on_commit=False) as db:
        opp = opportunity(db)
        user = actor(db)
        db.add(Pursuit(opportunity_id=opp.id, stage="evaluating"))
        db.commit()
        opp_id, user_id = opp.id, user.id
    def update(index):
        from govcon.models import User
        with Session(upgraded_engine) as db:
            user = db.get(User, user_id)
            result = generate_submission_package(db, opportunity_id=opp_id, actor=user)
            record_outcome(db, opportunity_id=opp_id, outcome="no_bid", actor=user, no_bid_reason=f"revision {index}")
            db.commit()
            return result["submission_id"]
    with ThreadPoolExecutor(max_workers=2) as workers:
        ids = list(workers.map(update, [1, 2]))
    assert ids[0] == ids[1]
    with Session(upgraded_engine) as db:
        assert len(db.scalars(select(OutcomeFeedback).where(OutcomeFeedback.opportunity_id == opp_id)).all()) == 1
        assert len(db.scalars(select(OutcomeCorrection).where(OutcomeCorrection.opportunity_id == opp_id)).all()) == 2
