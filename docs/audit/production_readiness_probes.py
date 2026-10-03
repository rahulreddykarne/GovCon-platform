"""Isolated counterexamples for the 2026-10-02 audit.

Run: .venv/Scripts/python.exe docs/audit/production_readiness_probes.py
No database, real provider, SMTP, or production files are used. PASS means the
reported defect was reproduced, not that the application behaves correctly.
Mocks supply persistence/transport boundaries; production decision code runs.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from govcon.config import Settings
from govcon.models import Opportunity, Proposal, Pursuit, Requirement, StoredFile, Submission, User

logging.disable(logging.CRITICAL)
RESULTS = []


def probe(name):
    def decorate(fn):
        try:
            detail = fn()
            RESULTS.append({"id": name, "reproduced": True, "detail": detail})
        except Exception as exc:
            RESULTS.append({"id": name, "reproduced": False, "detail": f"{type(exc).__name__}: {exc}"})
        return fn
    return decorate


@probe("F01")
def failed_source_event_is_consumed():
    from govcon.workflow import invalidation as m
    event = NS(id=7, opportunity_id=1, snapshot_id=3, new_value={"value": {"level": "material"}})
    db = MagicMock()
    db.begin_nested.return_value = nullcontext()
    with patch.object(m, "pending_source_change_events", return_value=[event]), patch.object(m, "_has_workflow_state", return_value=True), patch.object(m, "_revalidate_sources", side_effect=RuntimeError("temporary outage")), patch.object(m, "apply_source_change") as invalidate:
        result = m.process_pending_source_changes(db, settings=Settings(_env_file=None))
    marker = db.add.call_args.args[0]
    assert result["errors"] and marker.old_value == {"event_id": 7}
    assert marker.event_type == m.MATERIAL_SOURCE_CHANGE_HANDLED_EVENT
    assert not invalidate.called
    return "Failure produced a handled marker; approval invalidation was never called."


@probe("F02")
def reverted_attachment_is_not_current():
    from govcon.enrich.attachments import reconcile_attachment_versions
    from govcon.enrich.attachment_refs import AttachmentRef
    a = StoredFile(id=10, opportunity_id=1, url="https://example.org/a.pdf", sha256="a"*64, active=True)
    b = StoredFile(id=11, opportunity_id=1, url=a.url, sha256="b"*64, active=False)
    db = MagicMock()
    db.scalars.return_value.all.return_value = [a, b]
    reconcile_attachment_versions(db, Opportunity(id=1), [AttachmentRef(url=a.url)])
    assert b.active and not a.active
    return "When current fetch returns existing A, reconciliation still activates higher-ID B."


@probe("F03")
def failed_refresh_hides_missing_new_content():
    from govcon.enrich.attachments import _record_failure
    from govcon.enrich.attachment_refs import AttachmentRef
    old = StoredFile(id=1, sha256="a"*64, extraction_status="success", snapshot_id=1)
    db = MagicMock()
    db.scalars.return_value.first.return_value = old
    row = _record_failure(db, Opportunity(id=1), AttachmentRef(url="https://example.org/a.pdf"), snapshot_id=2, message="HTTP 503")
    assert row is old and row.extraction_status == "success" and row.snapshot_id == 1
    assert not db.add.called
    return "A failed download for new snapshot 2 returned successful snapshot-1 data without a failure row."


@probe("F04")
def fictitious_package_passes_integrity():
    from govcon.compliance.deterministic import PackageFile, SubmissionPackage
    from govcon.submissions.manifest import verify_package
    pkg = SubmissionPackage(files=[PackageFile(name="proposal.pdf", role="proposal", sha256="0"*64, size_bytes=123, local_path=None)])
    assert verify_package(pkg) == []
    return "A made-up hash/size with no file path passes the same verifier used by pre-flight and confirmation."


@probe("F05")
def headings_and_negations_are_covered():
    from govcon.compliance.proposal_coverage import SectionView, scan_coverage
    text = "The offeror shall provide ISO 9001 certification."
    heading = scan_coverage(1, text, {}, [SectionView(1, "cert", "ISO 9001 certification", "", [])], full=.6, partial=.3)
    negative = scan_coverage(1, text, {}, [SectionView(1, "cert", None, "We do not have ISO 9001 certification.", [])], full=.6, partial=.3)
    assert heading.coverage_status == negative.coverage_status == "COVERED"
    return "An empty section with matching heading and an explicit denial both produce COVERED."


@probe("F06")
def obsolete_version_evidence_satisfies_new_version():
    from govcon.compliance.matrix import inputs_for, decide_status
    req = Requirement(id=1, mandatory=True, severity="high", requirement_type="technical", source_file_id=1, source_quote="source", status="satisfied", stale_due_to_amendment=False, validation={"proposal_coverage": {"proposal_version_id": 2}, "proposal_coverage_checks": [{"validator": "proposal_coverage", "status": "unknown", "reason": "version 2 has insufficient content"}]})
    old = NS(id=9, proposal_version_id=1, verification_status="verified", verification_method="proposal_scan", created_at=datetime.now(UTC))
    result = decide_status(inputs_for(req, [old]))
    assert result.status == "satisfied"
    return "Verified evidence for proposal v1 still yields SATISFIED when v2 coverage is unknown."


@probe("F07")
def identical_ai_counts_as_independent_validation():
    from govcon.compliance.matrix import ValidationInputs, decide_status
    same = {"status": "SATISFIED", "evidence_ids": [1], "confidence": .9, "provider": "deepseek", "model": "same-model"}
    result = decide_status(ValidationInputs(requirement_type="technical", mandatory=True, severity="critical", has_source_location=True, current_status="unknown", stale=False, verified_evidence_ids={1}, ai_primary=same, ai_secondary=same))
    assert result.status == "satisfied" and len(result.methods) == 2
    return "The same provider/model/evidence counted twice and satisfied a critical requirement."


@probe("F08")
def incomplete_extraction_never_retries():
    from govcon.compliance import pipeline as m
    inventory = NS(complete=True, documents=[])
    previous = NS(input_hash="same", status="incomplete", output_json={}, created_at=datetime.now(UTC))
    db = MagicMock()
    db.get.return_value = Opportunity(id=1)
    patches = {
        "build_document_inventory": (inventory, NS(id=1, warnings=[])),
        "latest_run": previous, "active_requirements": [], "inventory_hash": "same",
        "diff_inventory": NS(changed=False),
        "run_clause_validation": {"run_id": 1, "linked_requirement_ids": [], "created_requirement_ids": [], "flagged": []},
        "run_conflict_scan": {"run_id": 1, "applied": {"superseded": []}},
        "build_context": None, "run_deterministic_validation": {},
        "run_validation": {"run_id": 1, "changed": 0, "warnings": []},
        "run_red_team": {"run_id": 1, "finding_ids": [], "ai": {}},
        "run_jev_routing": {"run_id": 1, "decision_run_id": 1, "provider": "rules"},
        "record_matrix_run": ({}, 1),
    }
    from contextlib import ExitStack
    with ExitStack() as stack:
        for name, value in patches.items():
            stack.enter_context(patch.object(m, name, return_value=value))
        ai = stack.enter_context(patch.object(m, "run_ai_pass"))
        result = m.run_compliance_pipeline(db, 1, use_ai=True, company_facts={}, settings=Settings(_env_file=None))
    assert result["status"] == "incomplete" and not ai.called
    return "A prior incomplete extraction with unchanged inventory skipped both AI passes despite use_ai=True."


@probe("F09")
def submission_regeneration_retains_old_destination():
    from govcon.submissions import service as m
    old_deadline = datetime.now(UTC) + timedelta(days=2)
    new_deadline = old_deadline + timedelta(days=5)
    old = Submission(id=1, opportunity_id=1, submission_method="email", recipient_email="old@example.org", submission_deadline=old_deadline, required_files={"files": ["obsolete.pdf"]}, status="preparing")
    db = MagicMock()
    db.scalars.return_value.first.return_value = old
    req = Requirement(key_values={"submission_method": "email", "recipient_email": "new@example.org", "required_files": ["new.pdf"]}, requirement_type="submission", mandatory=True)
    with patch.object(m, "lock_one", side_effect=[Opportunity(id=1, response_deadline=new_deadline), Pursuit(id=1)]), patch.object(m, "active_requirements", return_value=[req]), patch.object(m, "record_run", return_value=NS(id=1)), patch.object(m, "close_undetected_findings"), patch.object(m, "record_audit"):
        m.generate_submission_package(db, opportunity_id=1, settings=Settings(_env_file=None))
    assert old.recipient_email == "old@example.org" and old.submission_deadline == old_deadline
    assert old.required_files["files"] == ["new.pdf", "obsolete.pdf"]
    return "New extracted recipient/deadline did not replace prior inferred values; removed file remained required."


@probe("F10")
def disjoint_file_types_disable_validator():
    from govcon.compliance.submission_preflight import collect_instructions, preflight_items
    from govcon.compliance.deterministic import PackageFile, SubmissionPackage
    reqs = [Requirement(id=i, key_values={"allowed_file_types": [t]}, requirement_type="formatting", status="satisfied", blocks_submission=False) for i, t in [(1, "PDF"), (2, "XLSX")]]
    ins = collect_instructions(Opportunity(response_deadline=datetime.now(UTC)+timedelta(days=2)), reqs, None, [])
    items = preflight_items(ins, SubmissionPackage(files=[PackageFile("x.exe", size_bytes=1, sha256="0"*64)]), reqs, [], inventory_complete=True, coverage_ok=True, now=datetime.now(UTC))
    result = next(i for i in items if i["check"] == "file_types")
    assert ins.allowed_file_types == [] and result["status"] == "not_applicable"
    return "PDF and XLSX constraints intersect to empty, causing file_types=NOT_APPLICABLE even for .exe."


@probe("F11")
def expired_opportunity_is_eligible():
    from govcon.matching.semantic import is_eligible_for_pursuit
    assert is_eligible_for_pursuit(Opportunity(status="open", response_deadline=datetime.now(UTC)-timedelta(days=30)))
    return "An open listing whose deadline passed 30 days ago is eligible_for_pursuit=True."


@probe("F12")
def pipeline_overrides_terminal_stage():
    from govcon.web import routes as m
    db = MagicMock()
    db.execute.side_effect = [NS(all=lambda: [(Pursuit(opportunity_id=1, stage="won"), Opportunity(id=1, title="Won"))]), NS(all=lambda: [])]
    db.scalar.return_value = NS(status="approved_to_bid")
    user = User(id=1, role="owner")
    with patch.object(m, "_require_login", return_value=user), patch.object(m, "session_scope", return_value=nullcontext(db)), patch.object(m, "_render", side_effect=lambda request, template, ctx, user: ctx):
        result = m.pipeline(NS())
    # Inspect returned columns without depending on the template.
    serialized = str(result)
    assert "Won" in serialized
    columns = result["columns"]
    approved = next(c for c in columns if c["key"] == "bid_approved")
    assert approved["cards"][0]["title"] == "Won"
    return "A pursuit at won is rendered in Approved to Bid because the old review remains approved_to_bid."


@probe("F13")
def hidden_outcome_field_discards_award_value():
    from govcon.web import routes as m
    from govcon.web.app import create_app
    user = User(id=1, role="owner", is_active=True)
    with patch.object(m, "_require_login", return_value=user), patch.object(m, "session_scope", return_value=nullcontext(MagicMock())), patch.object(m, "_actor", return_value=user), patch.object(m, "record_outcome") as record:
        response = TestClient(create_app(Settings(_env_file=None))).post("/workspace/1/record-outcome", content="outcome=won&award_amount=95000&award_amount=", headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert response.status_code == 303 and record.call_args.kwargs["award_amount"] is None
    return "The actual outcome route discarded a won award value when the later hidden lost input was empty."


@probe("F14")
def no_active_registry_prompt_falls_back_to_disk():
    from govcon.ai import structured as m
    with patch.object(m, "load_prompt", side_effect=ValueError("no active version")), patch.object(m, "load_prompt_from_disk", return_value="disk-active"):
        result = m.resolve_prompt(MagicMock(), "compliance_validator", Settings(_env_file=None))
    assert result == "disk-active"
    return "Removing all active registry versions does not disable use: disk-active prompt is returned."


@probe("F15")
def alternate_provider_ui_is_skipped():
    from govcon.web import routes as m
    from govcon.proposals import service
    from govcon.submissions import service as submission
    db = MagicMock()
    db.begin_nested.return_value = nullcontext()
    settings = Settings(_env_file=None, ai_primary_provider="anthropic", anthropic_api_key="fake-key", deepseek_api_key=None)
    with patch("govcon.config.get_settings", return_value=settings), patch.object(service, "generate_proposal") as generate, patch.object(submission, "generate_submission_package"):
        m._trigger_proposal_generation(db, opp_id=1, actor=NS())
    assert generate.call_args.kwargs["skip_ai"] is True
    return "A configured Anthropic primary provider still received skip_ai=True in approve-to-bid generation."


@probe("F16")
def malformed_deadline_crashes_parser():
    from govcon.compliance.deterministic import parse_source_deadline
    try:
        parse_source_deadline("2026-10-02", "25:99", "UTC")
    except ValueError:
        return "Source time 25:99 raises ValueError instead of producing an unknown validation result."
    raise AssertionError("invalid time did not throw")


@probe("F17")
def post_approval_commercial_fields_remain_mutable():
    from govcon.mcp import operations as m
    pursuit = Pursuit(id=1, opportunity_id=1, stage="submitted", version=1, quote_price=100, sourcing_cost=80)
    db = MagicMock()
    db.scalar.return_value = pursuit
    def update(db, row, expected, changes):
        old = {key: getattr(row, key) for key in changes}
        for key, value in changes.items():
            setattr(row, key, value)
        row.version += 1
        return old
    with patch.object(m, "current_actor", return_value=User(id=2, role="reviewer")), patch.object(m, "apply_versioned_update", side_effect=update), patch.object(m, "record_audit"):
        result = m.op_update_pursuit(db, 1, expected_version=1, quote_price=999)
    assert result["ok"] and pursuit.quote_price == 999 and pursuit.stage == "submitted"
    return "A reviewer changed a submitted bid's quote_price to 999 without a correction or invalidation workflow."


@probe("F18")
def login_accepts_foreign_origin():
    from govcon.web import routes as m
    from govcon.web.app import create_app
    with patch.object(m, "session_scope", return_value=nullcontext(MagicMock())), patch.object(m, "authenticate", return_value=User(id=1)), patch.object(m, "create_session", return_value="fake-session"):
        response = TestClient(create_app(Settings(_env_file=None))).post("/login", data={"email": "attacker@example.org", "password": "example-password"}, headers={"Origin": "https://attacker.example"}, follow_redirects=False)
    cookie = response.headers["set-cookie"]
    assert response.status_code == 303 and "fake-session" in cookie and "Secure" not in cookie
    return "Cross-origin login POST sets an HttpOnly/Lax session cookie without CSRF rejection or Secure."


@probe("F19")
def built_wheel_cannot_start_web_app():
    import os
    import subprocess
    import tempfile
    import zipfile
    wheel = Path(os.environ["TEMP"]) / "govcon-production-audit-wheel" / "govcon-0.1.0-py3-none-any.whl"
    assert wheel.exists(), "Build the wheel using the report's command first."
    with tempfile.TemporaryDirectory(prefix="govcon-audit-wheel-") as folder:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            assert not any("/templates/" in n or "/static/" in n for n in names)
            archive.extractall(folder)
        code = "import sys; sys.path.insert(0,sys.argv[1]); from govcon.web.app import create_app; create_app()"
        result = subprocess.run([sys.executable, "-I", "-c", code, folder], capture_output=True, text=True)
    assert result.returncode != 0 and "static" in result.stderr and "does not exist" in result.stderr
    return "Actual built wheel contains no web assets; isolated create_app() fails with missing static directory."


@probe("F20")
def semantic_postfilter_starves_results():
    from govcon.matching.semantic import semantic_recommendations_for_watchlist
    from govcon.models import Watchlist
    rows = [(Opportunity(id=i, status="open"), float(i)/100) for i in range(1, 52)]
    db = MagicMock()
    db.get.return_value = Watchlist(id=1, embedding=[.1]*384)
    db.scalars.return_value.all.return_value = list(range(1, 51))
    limits = []
    def execute(statement):
        limit = statement._limit_clause.value
        limits.append(limit)
        return NS(all=lambda: rows[:limit])
    db.execute.side_effect = execute
    result = semantic_recommendations_for_watchlist(db, 1, limit=10)
    assert result["matches"] == [] and limits == [50]
    return "With top 50 already rule-matched, candidate 51 exists but no semantic recommendation is returned."


@probe("F21")
def smtp_logs_in_without_tls():
    from govcon.alerts.digest import send_smtp
    client = MagicMock()
    client.has_extn.return_value = False
    factory = MagicMock()
    factory.return_value.__enter__.return_value = client
    with patch("govcon.alerts.digest.smtplib.SMTP", factory):
        send_smtp(Settings(_env_file=None, smtp_host="mail.example", smtp_port=587, smtp_user="fake-user", smtp_pass="fake-password", alert_email_to="recipient@example.org"), subject="audit", html="audit", plain="audit")
    assert client.login.called and client.send_message.called and not client.starttls.called
    return "SMTP login and digest delivery proceed when the server does not advertise STARTTLS."


@probe("F22")
def sync_activates_without_activation_gate():
    from govcon.prompting import registry as m
    from govcon.prompting.loader import load_markdown_prompt
    root = Path(__file__).resolve().parents[2] / "src/govcon/prompts"
    asset = load_markdown_prompt(root / "compliance/compliance_validator_v1.md")
    with patch.object(m, "activate_version") as activate, patch("govcon.prompting.evaluation.run_activation_gate") as gate:
        m._upsert_prompt(MagicMock(), asset)
    assert activate.called and not gate.called
    return "Registry sync upsert activates safety-critical disk-active prompt without running its activation gate."


@probe("F23")
def malformed_jev_output_is_not_unavailable():
    from govcon.decision.providers.jev import JevDecisionProvider
    from govcon.decision.provider import DecisionProviderUnavailable
    client = MagicMock()
    response = client.__enter__.return_value.post.return_value
    response.status_code = 200
    response.json.side_effect = json.JSONDecodeError("bad JSON", "<html>", 0)
    provider = JevDecisionProvider(api_key="fake", settings=Settings(_env_file=None, ai_external_allowed_for_proprietary=True))
    with patch("govcon.decision.providers.jev.httpx.Client", return_value=client):
        try:
            provider.decide(bundle_name="bid_decision", bundle_version="v1", state={})
        except DecisionProviderUnavailable:
            raise AssertionError("malformed response correctly converted to unavailable")
        except json.JSONDecodeError:
            return "HTTP-200 non-JSON response escapes as JSONDecodeError; engine catches only DecisionProviderUnavailable."
    raise AssertionError("no error raised")


@probe("F24")
def inactive_attachment_is_sent_to_summary_provider():
    from govcon.enrich import summarize as m
    db = MagicMock()
    historic = StoredFile(id=1, opportunity_id=1, filename="removed.txt", active=False, extracted_text="REMOVED-OLD-REQUIREMENT", extraction_status="success")
    db.execute.return_value.scalars.return_value.all.return_value = [historic]
    provider = MagicMock()
    provider.complete.side_effect = RuntimeError("stop before actual provider")
    with patch.object(m, "current_source_revision", return_value="revision"), patch.object(m, "get_provider", return_value=provider):
        m.run_solicitation_analysis(db, Opportunity(id=1, title="Audit"), force=True, settings=Settings(_env_file=None))
    statement = db.execute.call_args.args[0]
    assert "files.active" not in str(statement.whereclause.compile())
    assert "REMOVED-OLD-REQUIREMENT" in provider.complete.call_args.kwargs["user_prompt"]
    return "Summary query has no active filter and old removed attachment text reached the provider prompt."


@probe("F25")
def buyer_delivery_target_becomes_supplier_leadtime():
    from govcon.decision import engine as m
    from govcon.decision.providers.rule_fallback import RuleDecisionProvider
    db = MagicMock()
    db.get.return_value = Opportunity(id=1, response_deadline=datetime.now(UTC)+timedelta(days=15))
    summary = NS(context_manifest={"source_revision": "revision"}, output_json={"delivery": {"delivery_days": 30}}, id=1, schema_version="test")
    db.scalar.side_effect = [summary, None, None, 0, 0, 0, 0, 0, 0]
    db.execute.return_value.all.return_value = []
    def eligibility(session, opportunity, profile, summary, **kwargs):
        for key in ("sam_active", "set_aside_match", "mandatory_certifications_met"):
            kwargs["signals"].set(key, None, source="none", confidence="unknown")
    def capability(session, opportunity, profile, signals):
        signals.set("capability_fit_score", None, source="none", confidence="unknown")
    with patch.object(m, "current_source_revision", return_value="revision"), patch.object(m, "recent_award_comps", return_value=[]), patch.object(m, "competitor_summary", return_value=None), patch.object(m, "load_company_profile", return_value={}), patch.object(m, "eligibility_signals", side_effect=eligibility), patch.object(m, "capability_signal", side_effect=capability), patch.object(m, "amendment_signal", side_effect=lambda s, o, c, sig: sig.set("amendment_material", None, source="none", confidence="unknown")):
        state = m.build_decision_state(db, 1)
    assert state["sourcing"]["lead_time_days"] == state["opportunity"]["required_delivery_days"] == 30
    result, _ = RuleDecisionProvider()._bundle_eligibility_and_execution(state)
    assert result["delivery_feasibility"] == "yes"
    return "With no supplier/pursuit evidence, buyer's 30-day requirement became supplier lead time and feasibility=yes."


@probe("F27")
def foreign_proposal_version_is_accepted_for_coverage():
    from govcon.compliance import proposal_coverage as m
    from govcon.models import ProposalVersion
    db = MagicMock()
    db.get.return_value = ProposalVersion(id=99, proposal_id=200, version_number=1)
    db.scalars.return_value.all.return_value = []
    with patch.object(m, "_sections", return_value=[]), patch.object(m, "active_requirements", return_value=[]), patch.object(m, "record_run", return_value=NS(id=1)), patch.object(m, "close_undetected_findings"):
        result = m.check_proposal_coverage(db, opportunity_id=1, proposal_version_id=99, settings=Settings(_env_file=None))
    assert result["proposal_version_id"] == 99
    assert not any(call.args[0] is Proposal for call in db.get.call_args_list)
    return "Coverage for opportunity 1 accepted version 99 of another proposal without loading/verifying its owner."


if __name__ == "__main__":
    print(json.dumps(RESULTS, indent=2))
    sys.exit(0 if all(row["reproduced"] for row in RESULTS) else 1)
