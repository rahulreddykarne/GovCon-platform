"""End-to-end compliance run (§15.1).

inventory → extraction A / B / deterministic → reconciliation (+ AI hints) →
clause library → conflict scan → amendment revalidation → deterministic
validators → evidence/AI validation → red team → JEV routing → coverage
counts. Proposal coverage and submission pre-flight run later against a
selected proposal version and assembled package.

A run is ``incomplete`` (never silently complete) when source ingestion has
blocking warnings, an AI extraction pass failed, or AI passes were not run.
Extraction is cached only when complete: an incomplete extraction for an
unchanged inventory is retried on the next AI-enabled run.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.ai.structured import StructuredCallError, run_structured_prompt
from govcon.compliance.amendments import diff_inventory, inventory_files, run_amendment_revalidation
from govcon.compliance.clauses import run_clause_validation
from govcon.compliance.conflicts import run_conflict_scan
from govcon.compliance.deterministic import SubmissionPackage, build_context, run_deterministic_validation
from govcon.compliance.extractor import run_ai_pass, run_scanner_pass
from govcon.compliance.inventory import build_document_inventory, inventory_hash
from govcon.compliance.matrix import active_requirements, latest_run, validator_identity
from govcon.compliance.metrics import record_matrix_run
from govcon.compliance.reconciler import apply_ai_hints, persist_reconciliation, reconcile
from govcon.compliance.red_team import run_red_team
from govcon.compliance.validator import run_jev_routing, run_validation
from govcon.config import Settings, get_settings
from govcon.models import Opportunity
from govcon.security.classification import DataClassification, strictest_classification

logger = logging.getLogger("govcon.compliance.pipeline")


class CompanyFactsInvalid(ValueError):
    """``COMPANY_FACTS_PATH`` is not a JSON object; facts are never guessed from it."""


def load_company_facts(settings: Settings, session: Session | None = None) -> dict[str, Any]:
    """Approved company facts (§38.4); absent facts stay unknown.

    With a session, SAM registration fields refreshed daily (ADR-072) replace
    the file's values, or are removed when the refresh is stale.
    """
    facts = read_company_facts_file(settings)
    if session is None:
        return facts
    from govcon.company.registration import overlay_registration

    return overlay_registration(session, facts, settings=settings)


def read_company_facts_file(settings: Settings) -> dict[str, Any]:
    """The human-maintained facts file at ``COMPANY_FACTS_PATH``, as written."""
    path = settings.company_facts_path
    if path is None:
        return {}
    path = Path(path)
    if not path.is_file():
        logger.warning("COMPANY_FACTS_PATH %s does not exist; company facts are unknown", path)
        return {}
    try:
        facts = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CompanyFactsInvalid(f"company facts file {path.name} is not valid JSON: {exc}") from exc
    if not isinstance(facts, dict):
        raise CompanyFactsInvalid(f"company facts file {path.name} must hold a JSON object, not {type(facts).__name__}")
    return facts


def passes_independent(ai_outcomes: list[Any]) -> bool:
    """True when extraction passes A and B ran on different, known provider/model pairs."""
    identities = [(o.provider, o.model) for o in ai_outcomes if o.pass_label in {"A", "B"}]
    if len(identities) != 2 or any(provider is None or model is None for provider, model in identities):
        return False
    return validator_identity(*identities[0]) != validator_identity(*identities[1])


def _ai_reconciliation_hints(session, opportunity_id, candidates, canonicals, inventory, settings, warnings) -> dict[str, Any]:
    try:
        result = run_structured_prompt(
            session,
            classification=strictest_classification(*(d.classification for d in inventory.documents)),
            opportunity_id=opportunity_id,
            prompt_name="requirement_reconciliation",
            analysis_type=AnalysisType.COMPLIANCE_REVIEW,
            variables={
                "REQUIREMENTS_JSON": [
                    {"candidate_id": c.candidate_id, "pass": c.pass_label, "text": c.requirement_text, "mandatory": c.mandatory, "severity": c.severity, "source_file_id": c.source_file_id, "page": c.source_page, "quote": c.supporting_quote}
                    for c in candidates
                ],
                "DOCUMENT_INVENTORY_JSON": [d.manifest() for d in inventory.documents],
            },
            context_manifest={"candidate_count": len(candidates)},
            settings=settings,
        )
    except StructuredCallError as exc:
        warnings.append({"code": f"reconciliation_ai_{exc.reason}", "severity": "low", "message": exc.detail})
        return {"status": "failed", "reason": exc.reason}
    apply_ai_hints(canonicals, [g.model_dump() for g in result.output.groups])
    return {"status": "complete", "ai_analysis_id": result.analysis.id if result.analysis else None, "groups": len(result.output.groups)}


def run_compliance_pipeline(
    session: Session,
    opportunity_id: int,
    *,
    use_ai: bool = True,
    force: bool = False,
    company_facts: dict[str, Any] | None = None,
    package: SubmissionPackage | None = None,
    supplier: dict[str, Any] | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise ValueError(f"opportunity not found: {opportunity_id}")
    facts = company_facts if company_facts is not None else load_company_facts(settings, session)
    warnings: list[dict[str, Any]] = []

    inventory, inventory_run = build_document_inventory(session, opportunity_id)
    warnings += [w for w in (inventory_run.warnings or []) if w.get("blocking")]
    prior = latest_run(session, opportunity_id, "requirement_reconciliation")
    prior_files = (prior.output_json or {}).get("inventory_files", []) if prior else []
    prior_requirement_ids = {r.id for r in active_requirements(session, opportunity_id)}
    current_hash = inventory_hash(inventory)
    # Only a complete extraction is reused for an unchanged inventory; an
    # incomplete one (provider outage, missing key, policy block) is retried.
    extraction_needed = (
        prior is None
        or force
        or prior.input_hash != current_hash
        or (use_ai and prior.status != "complete")
    )
    diff = diff_inventory(prior_files, inventory) if prior is not None else None

    extraction: dict[str, Any] = {"status": "skipped", "reason": "inventory unchanged since last reconciliation"}
    ai_passes_ok = prior is not None and not extraction_needed and (prior.status == "complete")
    if extraction_needed:
        outcomes = [run_scanner_pass(session, opportunity_id, inventory)]
        if use_ai:
            outcomes += [run_ai_pass(session, opportunity, inventory, label, settings=settings) for label in ("A", "B")]
        for outcome in outcomes:
            warnings += outcome.warnings
        ai_outcomes = [o for o in outcomes if o.pass_label in {"A", "B"}]
        ai_passes_ok = bool(ai_outcomes) and all(o.status == "complete" for o in ai_outcomes)
        if not use_ai:
            warnings.append({"code": "ai_passes_not_run", "severity": "high", "message": "Independent AI extraction passes were not run; only the deterministic scanner extracted requirements."})
        candidates = [c for o in outcomes for c in o.candidates]
        canonicals = reconcile(
            candidates,
            merge_threshold=settings.compliance_merge_similarity,
            duplicate_threshold=settings.compliance_possible_duplicate_similarity,
            ab_independent=passes_independent(ai_outcomes),
        )
        hints = {"status": "not_run"}
        if use_ai and sum(1 for o in ai_outcomes if o.candidates) == 2:
            hints = _ai_reconciliation_hints(session, opportunity_id, candidates, canonicals, inventory, settings, warnings)
        _, extraction = persist_reconciliation(
            session,
            opportunity_id,
            canonicals,
            merge_threshold=settings.compliance_merge_similarity,
            run_label="amendment" if diff is not None and diff.new_amendments else "reextraction",
            source_snapshot_ids=sorted({d.snapshot_id for d in inventory.documents if d.snapshot_id}),
            pass_runs={o.pass_label: o.run_id for o in outcomes},
            warnings=warnings or None,
            input_hash=current_hash,
            inventory_files=inventory_files(inventory),
        )
        if not ai_passes_ok:
            latest = latest_run(session, opportunity_id, "requirement_reconciliation")
            latest.status = "incomplete"
        extraction["ai_reconciliation"] = hints
        extraction["passes"] = {o.pass_label: {"status": o.status, "candidates": len(o.candidates), "provider": o.provider, "model": o.model} for o in outcomes}

    clauses = run_clause_validation(session, opportunity_id, inventory)
    conflicts = run_conflict_scan(session, opportunity_id, inventory, use_ai=use_ai, settings=settings)
    amendment: dict[str, Any] | None = None
    if diff is not None and diff.changed and extraction_needed:
        amendment = run_amendment_revalidation(
            session,
            opportunity_id,
            inventory,
            diff,
            prior_requirement_ids=prior_requirement_ids,
            since=prior.created_at if prior else None,
            superseded_ids=conflicts["applied"]["superseded"],
            use_ai=use_ai,
            settings=settings,
        )
    ctx = build_context(opportunity, inventory, company_facts=facts, package=package, supplier=supplier, now=now)
    deterministic = run_deterministic_validation(session, opportunity_id, ctx)
    validation = run_validation(session, opportunity_id, use_ai=use_ai, settings=settings)
    warnings += validation["warnings"]
    red_team = run_red_team(session, opportunity_id, inventory, use_ai=use_ai, settings=settings)
    routing = run_jev_routing(session, opportunity_id, amendment=amendment, settings=settings)
    review_reopened = False
    if amendment and amendment["impact"]["review_reopen_required"]:
        # Consume the flag here so an amendment after approval always reopens
        # review and invalidates the proposal approval and submission readiness.
        from govcon.collaboration.review_sessions import apply_material_amendment_reopen

        review_reopened = apply_material_amendment_reopen(session, opportunity_id=opportunity_id)
    bid_rerun = None
    if amendment and amendment["impact"]["rerun_jev_bid_decision"]:
        from govcon.decision.engine import run_decision_bundle

        bid_rerun = run_decision_bundle(session, opportunity_id=opportunity_id, bundle_name="bid_decision", settings=settings).run.id

    complete = inventory.complete and ai_passes_ok
    counts, matrix_run_id = record_matrix_run(
        session,
        opportunity_id,
        status="complete" if complete else "incomplete",
        warnings=warnings or None,
        extra={"inventory_run_id": inventory_run.id, "routing_run_id": routing["run_id"]},
    )
    return {
        "opportunity_id": opportunity_id,
        "status": "complete" if complete else "incomplete",
        "matrix_run_id": matrix_run_id,
        "inventory": {"run_id": inventory_run.id, "complete": inventory.complete, "documents": len(inventory.documents)},
        "extraction": extraction,
        "clauses": {k: clauses[k] for k in ("run_id", "linked_requirement_ids", "created_requirement_ids", "flagged")},
        "conflicts": {"run_id": conflicts["run_id"], **conflicts["applied"]},
        "amendment": amendment,
        "deterministic": deterministic,
        "validation": {"run_id": validation["run_id"], "changed": validation["changed"]},
        "red_team": {"run_id": red_team["run_id"], "findings": len(red_team["finding_ids"]), "ai": red_team["ai"]},
        "jev_routing": {"run_id": routing["run_id"], "decision_run_id": routing["decision_run_id"], "provider": routing["provider"]},
        "bid_decision_rerun_id": bid_rerun,
        "review_reopened": review_reopened,
        "counts": counts,
        "warnings": warnings,
    }
