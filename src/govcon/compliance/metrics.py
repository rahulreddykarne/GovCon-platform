"""Compliance coverage counts and reliability metrics (§15.13, §15.19).

Coverage is reported as counts. Any percentage is derived from those counts
and carries its definition. ``false_satisfied_detected`` is a runtime
self-check: a satisfied row without a validation method, with an
unacknowledged deterministic failure, or still stale is counted (it should
always be zero).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy.orm import Session

from govcon.compliance.matrix import active_requirements, open_findings, record_run
from govcon.compliance.records import RESOLVED_STATUSES
from govcon.diagnostics import trace_phase
from govcon.models import ComplianceFinding, Requirement

METRICS_VERSION = "compliance_matrix.v1"
CATEGORY_OF_TYPE = {
    "administrative": "Administrative", "signature": "Administrative", "amendment_acknowledgment": "Administrative", "other": "Administrative",
    "technical": "Technical", "past_performance": "Technical", "cybersecurity": "Technical", "country_of_origin": "Technical",
    "pricing": "Pricing",
    "delivery": "Delivery",
    "certification": "Certifications", "representation": "Certifications", "set_aside": "Certifications",
    "submission": "Submission", "formatting": "Submission", "page_limit": "Submission",
}
CATEGORIES = ("Administrative", "Technical", "Pricing", "Delivery", "Certifications", "Submission")
PERCENT_DEFINITION = "satisfied ÷ total for the named group; not_applicable rows count as neither; superseded rows are excluded"


def _pct(numerator: int, denominator: int) -> float | None:
    return round(100.0 * numerator / denominator, 1) if denominator else None


def false_satisfied(req: Requirement) -> bool:
    if req.status != "satisfied":
        return False
    validation = req.validation or {}
    if not validation.get("methods") and not validation.get("override"):
        return True
    if validation.get("deterministic_fail") and not (validation.get("override") or {}).get("acknowledged_deterministic_failures"):
        return True
    return bool(req.stale_due_to_amendment)


def coverage_counts(requirements: Iterable[Requirement], findings: Iterable[ComplianceFinding]) -> dict[str, Any]:
    rows = [r for r in requirements if r.status != "superseded"]
    mandatory = [r for r in rows if r.mandatory is not False and r.status != "not_applicable"]
    critical = [r for r in rows if r.severity == "critical" and r.status != "not_applicable"]
    blockers_req = [r for r in rows if r.blocks_submission]
    blocking_findings = [f for f in findings if f.blocks_submission and f.status == "open"]

    def status_count(items, status):
        return sum(1 for r in items if r.status == status)

    by_category: dict[str, dict[str, Any]] = {}
    for name in CATEGORIES:
        items = [r for r in mandatory if CATEGORY_OF_TYPE.get(r.requirement_type or "other", "Administrative") == name]
        satisfied = status_count(items, "satisfied")
        by_category[name] = {"satisfied": satisfied, "total": len(items), "display": f"{satisfied}/{len(items)}", "percent": _pct(satisfied, len(items))}

    counts = {
        "mandatory_total": len(mandatory),
        "mandatory_satisfied": status_count(mandatory, "satisfied"),
        "mandatory_missing": status_count(mandatory, "missing"),
        "mandatory_unknown": status_count(mandatory, "unknown"),
        "mandatory_needs_review": status_count(mandatory, "needs_review"),
        "mandatory_stale": status_count(mandatory, "stale"),
        "mandatory_unreviewed": status_count(mandatory, "unreviewed"),
        "mandatory_uncertain_flag": sum(1 for r in mandatory if r.mandatory is None),
        "critical_total": len(critical),
        "critical_satisfied": status_count(critical, "satisfied"),
        "critical_unresolved": sum(1 for r in critical if r.status not in RESOLVED_STATUSES),
        "not_independently_confirmed": sum(1 for r in rows if not r.independently_confirmed),
        "requirement_blockers": len(blockers_req),
        "finding_blockers": len(blocking_findings),
        "submission_blockers": len(blockers_req) + len(blocking_findings),
        "false_satisfied_detected": sum(1 for r in rows if false_satisfied(r)),
        "by_category": by_category,
    }
    counts["percentages"] = {
        "definition": PERCENT_DEFINITION,
        "mandatory_satisfied_pct": _pct(counts["mandatory_satisfied"], counts["mandatory_total"]),
        "critical_satisfied_pct": _pct(counts["critical_satisfied"], counts["critical_total"]),
    }
    return counts


def coverage_summary(requirements: list[Requirement]) -> dict[str, Any]:
    """Return a flat summary dict suitable for workspace/checklist display.

    Accepts a list of Requirement ORM objects directly (no Session needed).
    Returns the same keys as coverage_counts without the by_category breakdown.
    """
    counts = coverage_counts(requirements, [])
    return {k: v for k, v in counts.items() if k not in ("by_category", "percentages")}


@trace_phase("compliance.metrics.record_matrix_run")
def record_matrix_run(session: Session, opportunity_id: int, *, status: str = "complete", warnings: list | None = None, extra: dict[str, Any] | None = None) -> tuple[dict[str, Any], int]:
    requirements = active_requirements(session, opportunity_id)
    findings = open_findings(session, opportunity_id)
    counts = coverage_counts(requirements, findings)
    columns = {k: counts[k] for k in (
        "mandatory_total", "mandatory_satisfied", "mandatory_missing", "mandatory_unknown", "mandatory_needs_review",
        "critical_total", "critical_satisfied", "critical_unresolved", "false_satisfied_detected",
    )}
    run = record_run(
        session,
        opportunity_id=opportunity_id,
        run_type="compliance_matrix",
        run_version=METRICS_VERSION,
        output={"counts": counts, "blocking_requirement_ids": [r.id for r in requirements if r.blocks_submission], **(extra or {})},
        status=status,
        warnings=warnings,
        counts=columns,
    )
    return counts, run.id


def format_coverage(counts: dict[str, Any]) -> list[str]:
    lines = [
        f"Mandatory requirements:  {counts['mandatory_total']}",
        f"Satisfied:               {counts['mandatory_satisfied']}",
        f"Missing:                 {counts['mandatory_missing']}",
        f"Unknown:                 {counts['mandatory_unknown']}",
        f"Needs review:            {counts['mandatory_needs_review']}",
        f"Stale:                   {counts['mandatory_stale']}",
        f"Unreviewed:              {counts['mandatory_unreviewed']}",
        "",
        f"Critical requirements:   {counts['critical_total']}",
        f"Critical satisfied:      {counts['critical_satisfied']}",
        f"Critical unresolved:     {counts['critical_unresolved']}",
        "",
        f"Submission blockers:     {counts['submission_blockers']}",
        f"Not independently confirmed: {counts['not_independently_confirmed']}",
        "",
    ]
    for name, item in counts["by_category"].items():
        lines.append(f"{name:<16}{item['display']:>8}")
    lines.append(f"(percentages: {counts['percentages']['definition']})")
    return lines


# ── benchmark metrics ──


def recall(expected: list[str], matched: set[str]) -> float | None:
    if not expected:
        return None
    return round(len([e for e in expected if e in matched]) / len(expected), 4)


def rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None
