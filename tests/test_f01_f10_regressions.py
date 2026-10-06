"""Regression coverage for production-readiness findings F01--F10.

Unit tests exercise the production decision code with persistence mocked at
the session boundary; the ``session`` tests run the same flows against
PostgreSQL.
"""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.compliance.deterministic import PackageFile, SubmissionPackage
from govcon.config import Settings
from govcon.enrich.attachment_refs import AttachmentRef
from govcon.models import (
    Opportunity,
    OpportunityEvent,
    Pursuit,
    Requirement,
    StoredFile,
    Submission,
)

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
URL = "https://files.example.test/rfq.pdf"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# ── F01: a failed source-change attempt is retried, approvals invalidated first ──


def _source_event(event_id: int, opportunity_id: int = 1, level: str = "material") -> NS:
    return NS(id=event_id, opportunity_id=opportunity_id, snapshot_id=3, new_value={"value": {"level": level}})


def _workflow_session() -> MagicMock:
    db = MagicMock()
    db.begin_nested.return_value = nullcontext()
    return db


def _added(db: MagicMock, event_type: str) -> list[OpportunityEvent]:
    return [c.args[0] for c in db.add.call_args_list if getattr(c.args[0], "event_type", None) == event_type]


def test_f01_failed_revalidation_invalidates_first_and_is_not_marked_handled() -> None:
    from govcon.workflow import invalidation as m

    db = _workflow_session()
    applied = {"review_reopened": True, "proposal_invalidated": True, "readiness_invalidated": False, "pursuit_rolled_back": True}
    with patch.object(m, "failed_attempts", return_value={}), \
         patch.object(m, "pending_source_change_events", return_value=[_source_event(7)]), \
         patch.object(m, "_has_workflow_state", return_value=True), \
         patch.object(m, "apply_source_change", return_value=applied) as invalidate, \
         patch.object(m, "_revalidate_sources", side_effect=RuntimeError("temporary outage")):
        summary = m.process_pending_source_changes(db, settings=_settings(), now=NOW)

    invalidate.assert_called_once()
    assert summary["invalidated"] == 1 and summary["errors"] and summary["opportunities"] == 0
    assert _added(db, m.MATERIAL_SOURCE_CHANGE_HANDLED_EVENT) == []
    [failure] = _added(db, m.MATERIAL_SOURCE_CHANGE_FAILED_EVENT)
    assert failure.old_value == {"event_id": 7}
    value = failure.new_value["value"]
    assert value["step"] == "revalidate" and value["attempt"] == 1 and "temporary outage" in value["error"]
    assert value["retry_after"] == (NOW + timedelta(seconds=m.RETRY_BASE_SECONDS)).isoformat()


def test_f01_event_waiting_for_backoff_is_skipped() -> None:
    from govcon.workflow import invalidation as m

    db = _workflow_session()
    with patch.object(m, "failed_attempts", return_value={7: (1, NOW - timedelta(minutes=1))}), \
         patch.object(m, "pending_source_change_events", return_value=[]) as pending, \
         patch.object(m, "apply_source_change") as invalidate:
        summary = m.process_pending_source_changes(db, settings=_settings(), now=NOW)
    assert pending.call_args.kwargs["exclude_ids"] == {7}
    assert summary["deferred"] == 1 and summary["events"] == 0
    invalidate.assert_not_called()


def test_f01_retry_after_backoff_succeeds_and_marks_handled() -> None:
    from govcon.workflow import invalidation as m

    db = _workflow_session()
    with patch.object(m, "failed_attempts", return_value={7: (1, NOW - timedelta(minutes=6))}), \
         patch.object(m, "pending_source_change_events", return_value=[_source_event(7)]) as pending, \
         patch.object(m, "_has_workflow_state", return_value=True), \
         patch.object(m, "apply_source_change", return_value={"review_reopened": False}), \
         patch.object(m, "_revalidate_sources", return_value={"compliance_status": "complete"}):
        summary = m.process_pending_source_changes(db, settings=_settings(), now=NOW)
    assert pending.call_args.kwargs["exclude_ids"] == set()
    assert summary["errors"] == [] and summary["opportunities"] == 1
    [handled] = _added(db, m.MATERIAL_SOURCE_CHANGE_HANDLED_EVENT)
    assert handled.old_value == {"event_id": 7}
    assert handled.new_value["value"]["compliance_status"] == "complete"


def test_f01_waiting_event_is_handled_with_a_newer_event_for_the_same_opportunity() -> None:
    from govcon.workflow import invalidation as m

    db = _workflow_session()
    db.scalars.return_value.all.return_value = [_source_event(7)]
    with patch.object(m, "failed_attempts", return_value={7: (1, NOW - timedelta(minutes=1))}), \
         patch.object(m, "pending_source_change_events", return_value=[_source_event(8)]), \
         patch.object(m, "_has_workflow_state", return_value=True), \
         patch.object(m, "apply_source_change", return_value={"review_reopened": True}), \
         patch.object(m, "_revalidate_sources", return_value={}):
        summary = m.process_pending_source_changes(db, settings=_settings(), now=NOW)
    assert summary["events"] == 2 and summary["deferred"] == 0
    assert sorted(e.old_value["event_id"] for e in _added(db, m.MATERIAL_SOURCE_CHANGE_HANDLED_EVENT)) == [7, 8]


def test_f01_retry_backoff_doubles_and_is_capped() -> None:
    from govcon.workflow.invalidation import (
        RETRY_MAX_SECONDS,
        retry_delay,
        retry_not_due,
    )

    assert retry_delay(0) == timedelta(0)
    assert retry_delay(1) == timedelta(minutes=5)
    assert retry_delay(2) == timedelta(minutes=10)
    assert retry_delay(50) == timedelta(seconds=RETRY_MAX_SECONDS)
    naive_last = (NOW - timedelta(minutes=7)).replace(tzinfo=None)
    assert retry_not_due({1: (2, naive_last), 2: (1, naive_last), 3: (1, None)}, NOW) == {1}


# ── F02 / F03: attachment versions follow the current fetch ──


def _file(file_id: int, sha: str | None, *, active: bool, url: str = URL) -> StoredFile:
    return StoredFile(classification="PUBLIC", source_origin="synthetic_test_fixture", id=file_id, opportunity_id=1, url=url, sha256=sha, active=active,
                      extraction_status="success" if sha else "download_failed")


def _reconcile(rows: list[StoredFile], refs: list[AttachmentRef], current: dict[str, int] | None) -> None:
    from govcon.enrich.attachments import reconcile_attachment_versions

    db = MagicMock()
    db.scalars.return_value.all.return_value = rows
    reconcile_attachment_versions(db, Opportunity(id=1), refs, current=current)


def test_f02_reverted_version_becomes_the_active_one() -> None:
    a, b = _file(10, "a" * 64, active=False), _file(11, "b" * 64, active=True)
    _reconcile([a, b], [AttachmentRef(url=URL)], {URL: a.id})
    assert a.active and a.removed_at is None
    assert not b.active and b.removed_at is not None


def test_f02_repeated_fetch_removal_and_readdition() -> None:
    a = _file(10, "a" * 64, active=True)
    _reconcile([a], [AttachmentRef(url=URL)], {URL: a.id})
    assert a.active
    _reconcile([a], [], {})
    assert not a.active and a.removed_at is not None
    _reconcile([a], [AttachmentRef(url=URL)], {URL: a.id})
    assert a.active and a.removed_at is None


def test_f03_failed_fetch_activates_the_failure_and_keeps_the_last_good_version() -> None:
    a, b = _file(10, "a" * 64, active=False), _file(11, "b" * 64, active=True)
    failure = _file(12, None, active=False)
    _reconcile([a, b, failure], [AttachmentRef(url=URL)], {URL: failure.id})
    assert failure.active and b.active and not a.active

    # Recovery: the fetched version is again the only active row.
    _reconcile([a, b, failure], [AttachmentRef(url=URL)], {URL: b.id})
    assert b.active and not failure.active and not a.active


def test_f03_failed_download_is_not_an_inventory_replacement() -> None:
    from govcon.compliance.amendments import diff_inventory
    from govcon.compliance.inventory import analyze_inventory, document_from_file

    good = StoredFile(classification="PUBLIC", source_origin="synthetic_test_fixture", id=10, url=URL, filename="rfq.txt", sha256="a" * 64, extraction_status="success", extracted_text="Offerors shall deliver.", snapshot_id=1)
    failure = StoredFile(classification="PUBLIC", source_origin="synthetic_test_fixture", id=12, url=URL, filename="rfq.txt", sha256=None, extraction_status="download_failed", extraction_error="HTTP 503", snapshot_id=2)
    prior = [{"file_id": 10, "sha256": "a" * 64, "filename": "rfq.txt", "document_type": "attachment"}]
    inventory = analyze_inventory([document_from_file(good), document_from_file(failure)], expected_urls=[(URL, "")])
    assert not inventory.complete and any(w.code == "download_failed" and w.blocking for w in inventory.warnings)
    diff = diff_inventory(prior, inventory)
    assert diff.replaced == [] and diff.removed_file_ids == []


def test_f02_without_a_fetch_no_other_version_is_promoted() -> None:
    a, b = _file(10, "a" * 64, active=True), _file(11, "b" * 64, active=False)
    _reconcile([a, b], [AttachmentRef(url=URL)], None)
    assert a.active and not b.active


def test_f02_refetched_existing_bytes_refresh_the_download_time(tmp_path) -> None:
    from govcon.enrich import attachments as m

    old = datetime(2026, 1, 1, tzinfo=UTC)
    existing = StoredFile(classification="PUBLIC", source_origin="synthetic_test_fixture", id=10, opportunity_id=1, url=URL, sha256=hashlib.sha256(b"A").hexdigest(), downloaded_at=old)
    fetched = NS(content=b"A", content_type="application/pdf", content_disposition=None)
    with patch.object(m, "safe_fetch", return_value=fetched), patch.object(m, "_existing", return_value=existing):
        row = m._download_one(MagicMock(), Opportunity(id=1), AttachmentRef(url=URL), settings=_settings(data_dir=tmp_path),
                              client=MagicMock(), resolver=None, snapshot_id=3)
    assert row is existing and row.downloaded_at > old


def test_f03_failed_refresh_records_a_current_failure_not_the_old_version() -> None:
    from govcon.enrich.attachments import DOWNLOAD_FAILED, _record_failure

    old = StoredFile(classification="PUBLIC", source_origin="synthetic_test_fixture", id=1, url=URL, sha256="a" * 64, extraction_status="success", snapshot_id=1, active=True)
    db = MagicMock()
    db.scalars.return_value.first.return_value = old  # only a stored version exists
    row = _record_failure(db, Opportunity(id=1), AttachmentRef(url=URL), snapshot_id=2, message="HTTP 503")
    assert row is not old and db.add.call_args.args[0] is row
    assert row.sha256 is None and row.extraction_status == DOWNLOAD_FAILED and row.snapshot_id == 2 and row.active
    assert old.extraction_status == "success" and old.snapshot_id == 1

    previous_failure = StoredFile(classification="PUBLIC", source_origin="synthetic_test_fixture", id=5, url=URL, sha256=None, extraction_status=DOWNLOAD_FAILED, snapshot_id=1)
    db = MagicMock()
    db.scalars.return_value.first.return_value = previous_failure
    assert _record_failure(db, Opportunity(id=1), AttachmentRef(url=URL), snapshot_id=2, message="HTTP 503") is previous_failure
    assert previous_failure.snapshot_id == 2 and not db.add.called


def test_f03_current_failure_blocks_the_inventory() -> None:
    from govcon.compliance.inventory import analyze_inventory, document_from_file

    failure = StoredFile(classification="PUBLIC", source_origin="synthetic_test_fixture", id=12, url=URL, filename="rfq.pdf", sha256=None, extraction_status="download_failed",
                         extraction_error="download failed: HTTP 503", snapshot_id=2)
    inventory = analyze_inventory([document_from_file(failure)], expected_urls=[(URL, "rfq.pdf")])
    codes = {w.code: w for w in inventory.warnings}
    assert codes["download_failed"].blocking and codes["missing_expected_attachment"].blocking
    assert not inventory.complete


# ── F04: package integrity needs the real assembled bytes ──


def test_f04_described_file_without_content_fails_integrity(tmp_path) -> None:
    from govcon.submissions.manifest import verify_package

    described = SubmissionPackage(files=[PackageFile("proposal.pdf", role="proposal", sha256="0" * 64, size_bytes=123)])
    assert verify_package(described) == ["assembled file has no retrievable content: proposal.pdf"]

    real = tmp_path / "proposal.pdf"
    real.write_bytes(b"%PDF-1.4 proposal")
    digest = hashlib.sha256(real.read_bytes()).hexdigest()
    assembled = SubmissionPackage(files=[PackageFile("proposal.pdf", role="proposal", sha256=digest, size_bytes=real.stat().st_size, local_path=str(real))])
    assert verify_package(assembled) == []
    real.write_bytes(b"%PDF-1.4 changed")
    assert verify_package(assembled) == ["assembled file changed: proposal.pdf"]
    real.unlink()
    assert verify_package(assembled) == ["assembled file is unavailable: proposal.pdf"]


def test_f04_preflight_fails_a_planning_manifest() -> None:
    from govcon.compliance.submission_preflight import (
        collect_instructions,
        preflight_items,
    )

    ins = collect_instructions(Opportunity(response_deadline=NOW + timedelta(days=2)), [], None, [])
    package = SubmissionPackage(files=[PackageFile("proposal.pdf", role="proposal", sha256="0" * 64, size_bytes=123)])
    items = {i["check"]: i for i in preflight_items(ins, package, [], [], inventory_complete=True, coverage_ok=True, now=NOW)}
    assert items["package_integrity"]["status"] == "fail"
    assert "no retrievable content" in items["package_integrity"]["reason"]


# ── F05: coverage needs substantive, affirmative content ──


def _scan(text: str, heading: str | None, content: str, *, key_values: dict | None = None, mapped: bool = False):
    from govcon.compliance.proposal_coverage import SectionView, scan_coverage

    section = SectionView(1, "s1", heading, content, [1] if mapped else [])
    return scan_coverage(1, text, key_values or {}, [section], full=0.6, partial=0.3)


CERT = "The offeror shall provide ISO 9001 certification."
DELIVERY = "Delivery shall be made within 30 calendar days after receipt of order."


@pytest.mark.parametrize(
    ("text", "heading", "content"),
    [
        (CERT, "ISO 9001 certification", ""),
        (CERT, "ISO 9001 certification", "See attached."),
        (CERT, None, "We do not have ISO 9001 certification."),
        (CERT, None, "We don't currently hold ISO 9001 certification."),
        (CERT, None, "Acme is unable to provide ISO 9001 certification."),
        (CERT, None, "Acme holds ISO certification for its plant."),
        (DELIVERY, None, "Delivery will be made within 45 calendar days after receipt of order."),
        (DELIVERY, None, "We take exception to delivery within 30 calendar days after receipt of order."),
    ],
)
def test_f05_heading_only_negated_or_wrong_figure_is_not_covered(text, heading, content) -> None:
    assert _scan(text, heading, content).coverage_status != "COVERED"


@pytest.mark.parametrize(
    ("text", "content"),
    [
        (CERT, "Acme holds current ISO 9001 certification; the certificate is attached."),
        (DELIVERY, "Delivery will be made no later than 30 calendar days after receipt of order."),
        (DELIVERY, "We take no exception: delivery within thirty calendar days after receipt of order."),
    ],
)
def test_f05_affirmative_content_is_covered(text, content) -> None:
    assert _scan(text, None, content).coverage_status == "COVERED"


def test_f05_counted_items_still_use_the_count_check() -> None:
    text = "Offerors shall provide three past performance references."
    content = "Past performance references:\nReference No. 1: DLA\nReference No. 2: VA\nReference No. 3: USACE"
    result = _scan(text, None, content, key_values={"required_count": 3, "count_noun": "reference"})
    assert result.coverage_status == "COVERED" and result.detected_count == 3


def test_f05_heading_match_without_content_is_a_gap_or_review() -> None:
    assert _scan(CERT, "ISO 9001 certification", "").coverage_status == "PARTIAL"
    assert _scan(CERT, "ISO 9001 certification", "", mapped=True).coverage_status == "NEEDS_REVIEW"


# ── F06: evidence belongs to the proposal version it was found in ──


def _coverage_requirement(coverage_status: str, version: int = 2, severity: str = "high") -> Requirement:
    checks = [] if coverage_status == "COVERED" else [{"validator": "proposal_coverage", "status": "unknown", "reason": f"version {version} unclear"}]
    return Requirement(id=1, mandatory=True, severity=severity, requirement_type="technical", source_file_id=1, source_quote="source",
                       status="satisfied", stale_due_to_amendment=False,
                       validation={"proposal_coverage": {"proposal_version_id": version, "coverage_status": coverage_status}, "proposal_coverage_checks": checks})


def _evidence(evidence_id: int, version: int | None, method: str = "proposal_scan") -> NS:
    return NS(id=evidence_id, proposal_version_id=version, verification_status="verified", verification_method=method, created_at=NOW)


def test_f06_old_version_evidence_does_not_satisfy_the_new_version() -> None:
    from govcon.compliance.matrix import decide_status, fresh_evidence, inputs_for

    req = _coverage_requirement("NEEDS_REVIEW")
    old = _evidence(9, 1)
    assert fresh_evidence(req, [old]) == []
    decision = decide_status(inputs_for(req, [old]))
    assert decision.status == "needs_review" and decision.blocks_submission


def test_f06_superseded_draft_evidence_does_not_count_even_when_coverage_is_clear() -> None:
    from govcon.compliance.matrix import decide_status, inputs_for

    decision = decide_status(inputs_for(_coverage_requirement("COVERED", version=3), [_evidence(9, 1)]))
    assert decision.status != "satisfied"


def test_f06_current_version_evidence_still_satisfies() -> None:
    from govcon.compliance.matrix import decide_status, inputs_for

    decision = decide_status(inputs_for(_coverage_requirement("COVERED"), [_evidence(9, 1), _evidence(10, 2)]))
    assert decision.status == "satisfied" and decision.methods == ["proposal_scan"]


def test_f06_unclear_coverage_blocks_other_automatic_methods_but_not_a_human() -> None:
    from govcon.compliance.matrix import decide_status, inputs_for

    req = _coverage_requirement("NEEDS_REVIEW")
    record = _evidence(11, None, method="company_record")
    ai = {"status": "SATISFIED", "evidence_ids": [11], "confidence": 0.99, "provider": "deepseek", "model": "m"}
    assert decide_status(inputs_for(req, [record], ai_primary=ai)).status == "needs_review"
    human = _evidence(12, None, method="human")
    decision = decide_status(inputs_for(req, [human]))
    assert decision.status == "satisfied" and decision.methods == ["human"]


# ── F07: the same model twice is one validation method ──


def _critical_inputs(primary: dict, secondary: dict):
    from govcon.compliance.matrix import ValidationInputs

    return ValidationInputs(requirement_type="technical", mandatory=True, severity="critical", has_source_location=True,
                            current_status="unknown", stale=False, verified_evidence_ids={1}, ai_primary=primary, ai_secondary=secondary)


def _ai(provider: str | None, model: str | None) -> dict:
    return {"status": "SATISFIED", "evidence_ids": [1], "confidence": 0.9, "provider": provider, "model": model}


@pytest.mark.parametrize(
    ("primary", "secondary"),
    [
        (_ai("deepseek", "same-model"), _ai("deepseek", "same-model")),
        (_ai("DeepSeek", "Same-Model "), _ai("deepseek", "same-model")),
        (_ai("deepseek", "m1"), _ai(None, None)),
        (_ai(None, None), _ai(None, None)),
    ],
)
def test_f07_dependent_or_unidentified_validators_count_once(primary, secondary) -> None:
    from govcon.compliance.matrix import decide_status

    decision = decide_status(_critical_inputs(primary, secondary))
    assert decision.status == "needs_review" and decision.methods == ["ai_validation"]
    assert "not independent" in decision.reason


def test_f07_different_models_are_two_methods() -> None:
    from govcon.compliance.matrix import decide_status

    decision = decide_status(_critical_inputs(_ai("deepseek", "deepseek-flash"), _ai("anthropic", "claude-sonnet-5")))
    assert decision.status == "satisfied" and decision.methods == ["ai_validation", "ai_validation_secondary"]


def test_f07_same_model_extraction_passes_are_not_independent_confirmation() -> None:
    from govcon.compliance.pipeline import passes_independent
    from govcon.compliance.reconciler import reconcile
    from govcon.compliance.records import Candidate

    def candidates():
        quote = "Delivery shall be made within 30 days."
        return [Candidate(f"{p}-1", p, "Deliver within 30 days", supporting_quote=quote, source_file_id=1, citation_verified=True, mandatory=True)
                for p in ("A", "B")]

    [same] = reconcile(candidates(), merge_threshold=0.72, duplicate_threshold=0.4, ab_independent=False)
    assert set(same.found_by) == {"A", "B"} and not same.independently_confirmed and "ab_same_model" in same.flags
    [independent] = reconcile(candidates(), merge_threshold=0.72, duplicate_threshold=0.4)
    assert independent.independently_confirmed and "ab_same_model" not in independent.flags

    outcome = lambda label, provider, model: NS(pass_label=label, provider=provider, model=model)  # noqa: E731
    assert passes_independent([outcome("A", "deepseek", "flash"), outcome("B", "deepseek", "pro")])
    assert not passes_independent([outcome("A", "deepseek", "flash"), outcome("B", "DeepSeek", "flash")])
    assert not passes_independent([outcome("A", "deepseek", "flash"), outcome("B", None, None)])
    assert not passes_independent([outcome("A", "deepseek", "flash")])


def test_f07_merge_keeps_flags_consistent_with_independence() -> None:
    from govcon.compliance.reconciler import _merge_into, reconcile
    from govcon.compliance.records import Candidate

    quote = "Delivery shall be made within 30 days."
    [canonical] = reconcile([Candidate(f"{p}-1", p, "Deliver within 30 days", supporting_quote=quote, source_file_id=1, citation_verified=True)
                             for p in ("A", "B")], merge_threshold=0.72, duplicate_threshold=0.4, ab_independent=False)
    req = Requirement(reconciliation={"found_by": ["B"], "flags": ["single_pass", "found_only_by_B"]}, independently_confirmed=False, source_refs=[])
    _merge_into(req, canonical)
    assert req.independently_confirmed is False
    assert "ab_same_model" in req.reconciliation["flags"] and "single_pass" not in req.reconciliation["flags"]


# ── F08: an incomplete extraction is retried ──


def _pipeline_run(prior_status: str, *, use_ai: bool):
    from govcon.compliance import pipeline as m
    from govcon.compliance.extractor import PassOutcome

    db = MagicMock()
    db.get.return_value = Opportunity(id=1)
    prior = NS(input_hash="same", status=prior_status, output_json={"inventory_files": []}, created_at=NOW)
    reconciliation = NS(status="complete")
    patches = {
        "build_document_inventory": (NS(complete=True, documents=[]), NS(id=1, warnings=[])),
        "active_requirements": [], "inventory_hash": "same",
        "diff_inventory": NS(changed=False, new_amendments=[]), "inventory_files": [],
        "run_scanner_pass": PassOutcome("D", [], "complete", 1),
        "reconcile": [], "persist_reconciliation": ([], {"run_id": 2}),
        "run_clause_validation": {"run_id": 1, "linked_requirement_ids": [], "created_requirement_ids": [], "flagged": []},
        "run_conflict_scan": {"run_id": 1, "applied": {"superseded": []}},
        "build_context": None, "run_deterministic_validation": {},
        "run_validation": {"run_id": 1, "changed": 0, "warnings": []},
        "run_red_team": {"run_id": 1, "finding_ids": [], "ai": {}},
        "run_jev_routing": {"run_id": 1, "decision_run_id": 1, "provider": "rules"},
        "record_matrix_run": ({}, 1),
    }
    with patch.multiple(m, **{name: MagicMock(return_value=value) for name, value in patches.items()}), \
         patch.object(m, "latest_run", side_effect=lambda _s, _o, run_type: prior if run_type == "requirement_reconciliation" else reconciliation), \
         patch.object(m, "run_ai_pass", side_effect=lambda *_a, **_k: PassOutcome(_a[3], [], "complete", 3, None, "deepseek", f"model-{_a[3]}")) as ai:
        result = m.run_compliance_pipeline(db, 1, use_ai=use_ai, company_facts={}, settings=_settings())
    return result, ai


def test_f08_incomplete_prior_extraction_is_retried_when_ai_is_available() -> None:
    result, ai = _pipeline_run("incomplete", use_ai=True)
    assert [c.args[3] for c in ai.call_args_list] == ["A", "B"]
    assert result["extraction"]["passes"]["B"]["status"] == "complete" and result["status"] == "complete"


def test_f08_complete_prior_extraction_is_reused() -> None:
    result, ai = _pipeline_run("complete", use_ai=True)
    assert not ai.called and result["extraction"]["status"] == "skipped"


def test_f08_without_ai_an_incomplete_extraction_stays_incomplete() -> None:
    result, ai = _pipeline_run("incomplete", use_ai=False)
    assert not ai.called and result["status"] == "incomplete"


# ── F09: regenerated instructions follow the current solicitation ──


def _generate(submission: Submission, requirements: list[Requirement], deadline: datetime):
    from govcon.submissions import service as m

    db = MagicMock()
    db.scalars.return_value.first.return_value = submission
    with patch.object(m, "lock_opportunity", return_value=Opportunity(id=1, response_deadline=deadline)), \
         patch.object(m, "lock_one", return_value=Pursuit(id=1)), \
         patch.object(m, "active_requirements", return_value=requirements), \
         patch.object(m, "record_run", return_value=NS(id=1)), \
         patch.object(m, "upsert_open_finding", return_value=NS(id=5)) as finding, \
         patch.object(m, "close_undetected_findings"), patch.object(m, "record_audit") as audit, \
         patch.object(m, "invalidate_submission_readiness") as invalidate:
        result = m.generate_submission_package(db, opportunity_id=1, settings=_settings())
    return result, finding, audit, invalidate


def _submission_req(**key_values) -> Requirement:
    return Requirement(key_values=key_values, requirement_type="submission", mandatory=True)


def test_f09_regeneration_replaces_superseded_instructions() -> None:
    old_deadline = NOW + timedelta(days=2)
    new_deadline = old_deadline + timedelta(days=5)
    submission = Submission(id=1, opportunity_id=1, submission_method="email", recipient_email="old@example.org", deadline_timezone="ET",
                            submission_deadline=old_deadline, required_files={"files": ["obsolete.pdf", "new.pdf"]}, status="ready", version=4)
    req = _submission_req(submission_method="email", recipient_email="new@example.org", required_files=["new.pdf"], deadline_timezone="ET")
    result, finding, audit, invalidate = _generate(submission, [req], new_deadline)
    assert submission.recipient_email == "new@example.org" and submission.submission_deadline == new_deadline
    assert submission.required_files == {"files": ["new.pdf"]} and result["required_files"] == ["new.pdf"]
    assert submission.version == 5
    invalidate.assert_called_once()
    changed = audit.call_args.kwargs["new_value"]["changed"]
    assert set(changed) == {"recipient_email", "submission_deadline", "required_files"}
    assert changed["recipient_email"] == {"old": "old@example.org", "new": "new@example.org"}
    finding.assert_not_called()


def test_f09_unchanged_instructions_do_not_invalidate_readiness() -> None:
    deadline = NOW + timedelta(days=3)
    submission = Submission(id=1, opportunity_id=1, submission_method="email", recipient_email="buyer@example.org",
                            submission_deadline=deadline, required_files={"files": []}, status="ready", readiness_status="ready", version=2)
    _, _, _, invalidate = _generate(submission, [_submission_req(submission_method="email", recipient_email="buyer@example.org")], deadline)
    invalidate.assert_not_called()
    assert submission.version == 2 and submission.readiness_status == "ready"


def test_f09_instruction_no_longer_stated_is_cleared() -> None:
    submission = Submission(id=1, opportunity_id=1, recipient_email="old@example.org", status="preparing", version=1)
    _generate(submission, [], NOW + timedelta(days=3))
    assert submission.recipient_email is None


def test_f09_conflicting_deadlines_and_time_zones_block_instead_of_choosing() -> None:
    submission = Submission(id=1, opportunity_id=1, status="preparing", version=1)
    reqs = [_submission_req(deadline="October 20, 2026", deadline_timezone="ET"),
            _submission_req(deadline="October 27, 2026", deadline_timezone="PT")]
    result, finding, _, _ = _generate(submission, reqs, datetime(2026, 10, 20, 21, 0, tzinfo=UTC))
    assert submission.deadline_timezone is None and submission.readiness_status == "not_ready"
    kwargs = finding.call_args.kwargs
    assert kwargs["finding_type"] == "submission_deadline_conflict" and kwargs["blocks_submission"]
    assert set(kwargs["source_refs"]) == {"deadline", "deadline_timezone", "opportunity_deadline"}
    assert result["deadline_conflicts"] == kwargs["source_refs"]


def test_f09_same_zone_spellings_and_matching_dates_are_not_conflicts() -> None:
    from govcon.submissions.service import (
        _deadline_disagrees,
        _extract_submission_info,
        _parse_date,
    )

    info = _extract_submission_info([_submission_req(deadline_timezone="ET", deadline="October 20, 2026"),
                                     _submission_req(deadline_timezone="EST", deadline="2026-10-20T17:00:00-04:00"),
                                     # A question due date is not the response deadline.
                                     Requirement(key_values={"response_deadline_date": "October 6, 2026"}, requirement_type="other")])
    assert info["deadline_conflicts"] == {} and info["deadline_timezone"] == "ET"
    assert info["extracted_deadline_dates"] == ["2026-10-20"]
    # 11:30 PM Eastern on Oct 20 is Oct 21 in UTC: still the same deadline.
    assert not _deadline_disagrees(datetime(2026, 10, 21, 3, 30, tzinfo=UTC), info["extracted_deadline_dates"])
    assert _deadline_disagrees(datetime(2026, 10, 27, 21, 0, tzinfo=UTC), info["extracted_deadline_dates"])
    assert _parse_date("Oct. 20, 2026") == _parse_date("10/20/2026") == _parse_date("2026-10-20 17:00") == datetime(2026, 10, 20).date()
    assert _parse_date("upon award") is None


def test_f09_submitted_record_is_not_rewritten() -> None:
    submission = Submission(id=1, opportunity_id=1, recipient_email="old@example.org", required_files={"files": ["a.pdf"]}, status="submitted", version=3)
    _, _, audit, invalidate = _generate(submission, [_submission_req(recipient_email="new@example.org")], NOW + timedelta(days=3))
    assert submission.recipient_email == "old@example.org" and submission.required_files == {"files": ["a.pdf"]}
    assert submission.version == 3 and not audit.called and not invalidate.called


def test_f09_preflight_rejects_stale_recorded_instructions() -> None:
    from govcon.compliance.submission_preflight import (
        collect_instructions,
        preflight_items,
    )

    deadline = NOW + timedelta(days=4)
    submission = Submission(recipient_email="old@example.org", submission_deadline=deadline - timedelta(days=3),
                            required_files={"files": ["obsolete.pdf"]}, status="preparing")
    reqs = [Requirement(id=1, key_values={"recipient_email": "new@example.org"}, requirement_type="submission", status="satisfied", blocks_submission=False)]
    ins = collect_instructions(Opportunity(response_deadline=deadline), reqs, submission, [])
    assert set(ins.stale_fields) == {"recipient_email", "submission_deadline", "required_files"}
    items = {i["check"]: i for i in preflight_items(ins, SubmissionPackage(), reqs, [], inventory_complete=True, coverage_ok=True, now=NOW)}
    assert items["instructions_current"]["status"] == "fail"

    fresh = Submission(recipient_email="new@example.org", submission_deadline=deadline, required_files={"files": []}, status="preparing")
    clean = collect_instructions(Opportunity(response_deadline=deadline), reqs, fresh, [])
    assert clean.stale_fields == [] and "stale_fields" not in clean.as_dict() and "deadline_conflicts" not in clean.as_dict()


# ── F10: file-type rules are scoped, and contradictions block ──


def _type_rule(rid: int, types: list[str], text: str = "", rtype: str = "formatting") -> Requirement:
    return Requirement(id=rid, key_values={"allowed_file_types": types}, requirement_type=rtype, requirement_text=text, status="satisfied", blocks_submission=False)


def _file_types(reqs: list[Requirement], files: list[PackageFile]) -> dict:
    from govcon.compliance.submission_preflight import (
        collect_instructions,
        preflight_items,
    )

    ins = collect_instructions(Opportunity(response_deadline=NOW + timedelta(days=2)), reqs, None, [])
    items = preflight_items(ins, SubmissionPackage(files=files), reqs, [], inventory_complete=True, coverage_ok=True, now=NOW)
    return next(i for i in items if i["check"] == "file_types")


def test_f10_disjoint_global_restrictions_fail() -> None:
    item = _file_types([_type_rule(1, ["PDF"]), _type_rule(2, ["XLSX"])], [PackageFile("x.exe", size_bytes=1, sha256="0" * 64)])
    assert item["status"] == "fail" and "contradictory" in item["reason"]
    assert item["evidence"]["conflicts"]["all"]


def test_f10_artifact_scoped_rules_check_each_matching_file() -> None:
    reqs = [_type_rule(1, ["PDF"], "The technical proposal shall be submitted in PDF format."),
            _type_rule(2, ["XLSX"], "The price schedule shall be submitted in Excel (XLSX) format.")]
    good = [PackageFile("technical.pdf", role="proposal"), PackageFile("prices.xlsx", role="pricing")]
    assert _file_types(reqs, good)["status"] == "pass"
    wrong = [PackageFile("technical.docx", role="proposal"), PackageFile("prices.xlsx", role="pricing")]
    item = _file_types(reqs, wrong)
    assert item["status"] == "fail" and item["evidence"]["bad"] == ["technical.docx"]
    swapped = [PackageFile("technical.pdf", role="proposal"), PackageFile("prices.pdf", role="pricing")]
    assert _file_types(reqs, swapped)["evidence"]["bad"] == ["prices.pdf"]


def test_f10_rule_for_all_documents_conflicting_with_a_scoped_rule_fails() -> None:
    reqs = [_type_rule(1, ["PDF"], "All documents shall be submitted in PDF format."),
            _type_rule(2, ["XLSX"], "The price schedule shall be submitted in XLSX format.")]
    item = _file_types(reqs, [PackageFile("technical.pdf", role="proposal"), PackageFile("prices.xlsx", role="pricing")])
    assert item["status"] == "fail" and "pricing" in item["evidence"]["conflicts"]


def test_f10_global_rule_still_applies_to_files_without_a_scoped_rule() -> None:
    reqs = [_type_rule(1, ["PDF", "XLSX"], "Proposals shall be submitted in PDF or XLSX format."),
            _type_rule(2, ["XLSX"], "The price schedule shall be submitted in XLSX format.")]
    item = _file_types(reqs, [PackageFile("cover.exe", role="other"), PackageFile("prices.xlsx", role="pricing")])
    assert item["status"] == "fail" and item["evidence"]["bad"] == ["cover.exe"]


def test_f10_absent_and_consistent_rules_keep_their_previous_results() -> None:
    assert _file_types([], [PackageFile("a.exe")])["status"] == "not_applicable"
    item = _file_types([_type_rule(1, ["PDF", "DOCX"]), _type_rule(2, ["PDF"])], [PackageFile("a.pdf"), PackageFile("b.docx")])
    assert item["status"] == "fail" and item["validator"] == "file_types_allowed"


# ── database-backed flows ──


@pytest.fixture()
def session(upgraded_engine):
    with Session(upgraded_engine) as s:
        yield s
        s.rollback()


def _db_opportunity(session: Session, links: dict | None = None) -> Opportunity:
    row = Opportunity(source="sam", source_id=f"f01-10-{uuid4().hex}", title="F01-F10 fixture", status="open", raw={}, links=links or {},
                      response_deadline=datetime.now(UTC) + timedelta(days=20))
    session.add(row)
    session.flush()
    return row


def _client(body: bytes | None) -> httpx.Client:
    def handler(_request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(503)
        return httpx.Response(200, content=body, headers={"content-type": "text/plain"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _download(session: Session, opp: Opportunity, tmp_path: Path, body: bytes | None) -> StoredFile:
    from govcon.enrich.attachments import download_attachments

    [row] = download_attachments(session, opp, settings=Settings(data_dir=tmp_path), client=_client(body), resolver=lambda _h: ["93.184.216.34"])
    return row


def _active(session: Session, opp: Opportunity) -> list[StoredFile]:
    return list(session.scalars(select(StoredFile).where(StoredFile.opportunity_id == opp.id, StoredFile.active.is_(True))).all())


def test_f02_f03_attachment_versions_track_the_current_fetch(session, tmp_path) -> None:
    from govcon.compliance.inventory import build_document_inventory

    url = "https://files.example.test/terms.txt"
    opp = _db_opportunity(session, {"attachments": [url]})
    a = _download(session, opp, tmp_path, b"Version A: offerors shall deliver within 30 days.")
    b = _download(session, opp, tmp_path, b"Version B: offerors shall deliver within 20 days.")
    assert b.id != a.id and [r.id for r in _active(session, opp)] == [b.id]

    reverted = _download(session, opp, tmp_path, b"Version A: offerors shall deliver within 30 days.")
    assert reverted.id == a.id and [r.id for r in _active(session, opp)] == [a.id]

    failed = _download(session, opp, tmp_path, None)
    assert failed.sha256 is None and failed.extraction_status == "download_failed"
    assert {r.id for r in _active(session, opp)} == {a.id, failed.id}
    inventory, _ = build_document_inventory(session, opp.id)
    assert not inventory.complete and any(w.code == "download_failed" and w.blocking for w in inventory.warnings)

    recovered = _download(session, opp, tmp_path, b"Version A: offerors shall deliver within 30 days.")
    assert recovered.id == a.id and [r.id for r in _active(session, opp)] == [a.id]
    inventory, _ = build_document_inventory(session, opp.id)
    assert not any(w.blocking for w in inventory.warnings), [w.code for w in inventory.warnings if w.blocking]


def test_f01_failure_is_retried_and_the_approval_never_stays_usable(session) -> None:
    from govcon.models import ReviewSession
    from govcon.workflow import invalidation as m

    opp = _db_opportunity(session)
    session.add(ReviewSession(opportunity_id=opp.id, status="approved_to_bid", final_approval_status="approved_to_bid"))
    session.add(OpportunityEvent(opportunity_id=opp.id, event_type=m.MATERIAL_SOURCE_CHANGE_EVENT, new_value={"value": {"level": "material"}}))
    session.flush()

    with patch.object(m, "_revalidate_sources", side_effect=RuntimeError("provider outage")):
        first = m.process_pending_source_changes(session, settings=_settings(), opportunity_ids={opp.id})
    assert first["errors"] and first["invalidated"] == 1
    review = session.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp.id))
    assert review.final_approval_status is None and review.status != "approved_to_bid"
    assert len(m.pending_source_change_events(session, opportunity_ids={opp.id})) == 1

    with patch.object(m, "_revalidate_sources", return_value={}) as revalidate:
        waiting = m.process_pending_source_changes(session, settings=_settings(), opportunity_ids={opp.id})
        assert waiting["deferred"] == 1 and not revalidate.called
        later = datetime.now(UTC) + timedelta(hours=1)
        retried = m.process_pending_source_changes(session, settings=_settings(), opportunity_ids={opp.id}, now=later)
    assert retried["errors"] == [] and retried["opportunities"] == 1
    assert m.pending_source_change_events(session, opportunity_ids={opp.id}) == []
