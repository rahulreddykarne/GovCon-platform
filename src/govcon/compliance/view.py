"""Read model for the workspace compliance section.

Every extracted requirement is shown once, grouped by what it asks for, with
its source citation, the company fact that answers it (or the fact that is
missing), the deterministic checks and the AI validation side by side. Only
a ``satisfied`` row reads as Met; anything unresolved reads as Needs review
with the stored reason, so no unknown is presented as compliant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.compliance.deterministic import (
    SET_ASIDE_STATUS,
    UNRESTRICTED_SET_ASIDE_CODES,
)
from govcon.compliance.matrix import (
    _matrix_row,
    active_requirements,
    evidence_for,
    latest_run,
)
from govcon.models import ComplianceRun, Opportunity, Requirement, StoredFile

CATEGORIES: tuple[tuple[str, str], ...] = (
    ("registration", "Registration (SAM, UEI, CAGE)"),
    ("set_aside", "Set-aside and socioeconomic status"),
    ("clauses", "FAR / DFARS clauses"),
    ("certifications", "Certifications and representations"),
    ("packaging", "Packaging and marking"),
    ("delivery", "Delivery"),
    ("section_m", "Section M: evaluation"),
    ("section_l", "Section L: instructions and submission"),
    ("technical", "Technical and product"),
    ("pricing", "Pricing"),
    ("other", "Other"),
)
CATEGORY_LABELS = dict(CATEGORIES)
_CATEGORY_ORDER = {key: index for index, (key, _label) in enumerate(CATEGORIES)}
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

STATUS_DISPLAY: dict[str, tuple[str, str]] = {
    "satisfied": ("Met", "success"),
    "missing": ("Missing", "danger"),
    "not_applicable": ("Not applicable", "secondary"),
}
NEEDS_REVIEW = ("Needs review", "warning")
_DEFAULT_REVIEW_REASON = {
    "unknown": "Not established by any check or evidence",
    "unreviewed": "Not yet validated",
    "needs_review": "Flagged for human review",
    "stale": "Changed by an amendment since it was last validated",
}

_REGISTRATION = re.compile(r"system for award management|\bsam\b|\buei\b|\bcage\b|52\.204-7\b|52\.204-13\b", re.I)
_CLAUSE = re.compile(r"\b(FAR|DFARS)\s*\d|\b52\.2\d\d-\d+|\b252\.2\d\d-\d{4}", re.I)
_PACKAGING = re.compile(r"packag|marking|mil-std-2073|mil-std-129|\bastm d3951\b|preservation", re.I)
_SECTION_M = re.compile(r"^\s*(section\s+m\b|evaluation)", re.I)
_SECTION_L = re.compile(r"^\s*(section\s+l\b|instructions)", re.I)
_SECTION_L_TYPES = {"submission", "formatting", "page_limit", "signature", "amendment_acknowledgment", "administrative"}


def category_for(req: Requirement) -> str:
    """One category per requirement, most specific first."""
    rtype = (req.requirement_type or "").strip().lower()
    text = f"{req.requirement_text} {req.source_quote or ''}"
    section = req.source_section or ""
    validators = {d.get("validator") for d in (req.validation or {}).get("deterministic") or []}
    if rtype == "set_aside" or "set_aside_matches" in validators:
        return "set_aside"
    if "sam_registration_known" in validators or (rtype in {"administrative", "representation", "submission", "other", ""} and _REGISTRATION.search(text)):
        return "registration"
    if (req.validation or {}).get("clause") or (rtype not in {"certification", "representation"} and _CLAUSE.search(text)):
        return "clauses"
    if rtype in {"certification", "representation", "country_of_origin", "cybersecurity"}:
        return "certifications"
    if _PACKAGING.search(text):
        return "packaging"
    if rtype == "delivery":
        return "delivery"
    if rtype == "past_performance" or _SECTION_M.search(section):
        return "section_m"
    if rtype in _SECTION_L_TYPES or _SECTION_L.search(section):
        return "section_l"
    if rtype == "technical":
        return "technical"
    if rtype == "pricing":
        return "pricing"
    return "other"


@dataclass
class FactEvidence:
    """What the approved company facts say for one requirement."""

    state: str  # "present", "contradicts", "missing", "not_applicable"
    text: str

    @property
    def missing(self) -> bool:
        return self.state == "missing"


def _listed(facts: dict[str, Any], key: str) -> list[str]:
    value = facts.get(key)
    return [str(v).strip() for v in value if str(v).strip()] if isinstance(value, list) else []


def company_fact_evidence(category: str, req: Requirement, opportunity: Opportunity, facts: dict[str, Any]) -> FactEvidence:
    """The company fact that answers this requirement, or the fact that is missing."""
    text = f"{req.requirement_text} {req.source_quote or ''}".lower()
    if category == "registration":
        status = str(facts.get("sam_registration_status") or "").strip()
        if not status:
            return FactEvidence("missing", "missing company fact: SAM registration status")
        expires = facts.get("sam_expiration_date")
        uei = facts.get("uei")
        detail = f"SAM registration {status}" + (f", expires {str(expires)[:10]}" if expires else ", expiration date not on file")
        detail += f"; UEI {uei}" if uei else "; UEI not on file"
        return FactEvidence("present" if status.lower() == "active" else "contradicts", detail)
    if category == "set_aside":
        code = (opportunity.set_aside_code or "").strip().upper()
        if not code or code in UNRESTRICTED_SET_ASIDE_CODES:
            return FactEvidence("not_applicable", "No set-aside on this notice")
        required = SET_ASIDE_STATUS.get(code)
        if required is None:
            return FactEvidence("missing", f"missing company fact: set-aside code {code} is not mapped to a business status")
        value = (facts.get("socioeconomic") or {}).get(required)
        if value is True:
            return FactEvidence("present", f"Company facts assert {required} (set-aside {code})")
        if value is False:
            return FactEvidence("contradicts", f"Company facts state the company is not {required} (set-aside {code})")
        return FactEvidence("missing", f"missing company fact: {required} status (set-aside {code})")
    if category == "certifications":
        held = _listed(facts, "certifications")
        if not held:
            return FactEvidence("missing", "missing company fact: certifications")
        matched = [c for c in held if c.lower() in text]
        if matched:
            return FactEvidence("present", "Certification on file: " + ", ".join(matched))
        return FactEvidence("missing", "missing company fact: no certification on file matches this requirement (on file: " + ", ".join(held) + ")")
    if category == "section_m" and (req.requirement_type == "past_performance" or "past performance" in text):
        records = _listed(facts, "past_performance")
        if not records:
            return FactEvidence("missing", "missing company fact: past performance")
        return FactEvidence("present", f"{len(records)} past performance record(s) on file: " + "; ".join(records[:3]) + ("…" if len(records) > 3 else ""))
    if category == "technical":
        products = _listed(facts, "products")
        if not products:
            return FactEvidence("missing", "missing company fact: products")
        return FactEvidence("present", "Products on file: " + ", ".join(products[:5]) + ("…" if len(products) > 5 else ""))
    if category == "pricing":
        margin = facts.get("minimum_margin_pct")
        if margin is None:
            return FactEvidence("missing", "missing company fact: minimum margin")
        return FactEvidence("present", f"Minimum margin {margin}%")
    return FactEvidence("not_applicable", "No company fact applies")


def _ai_view(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if not entry:
        return None
    confidence = entry.get("confidence")
    return {
        "status": entry.get("status"),
        "reason": entry.get("reason"),
        "confidence": round(float(confidence) * 100) if isinstance(confidence, (int, float)) else None,
        "provider": entry.get("provider"),
        "model": entry.get("model"),
        "analysis_id": entry.get("analysis_id"),
    }


@dataclass
class ComplianceView:
    rows: list[dict[str, Any]]
    groups: list[dict[str, Any]]
    counts: dict[str, int]
    mandatory: dict[str, int]
    superseded: int
    missing_facts: int
    run: ComplianceRun | None
    run_complete: bool
    run_warnings: list[dict[str, Any]] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    ai_run: bool = False


def _row(req: Requirement, base: dict[str, Any], opportunity: Opportunity, facts: dict[str, Any]) -> dict[str, Any]:
    validation = req.validation or {}
    category = category_for(req)
    label, tone = STATUS_DISPLAY.get(req.status, NEEDS_REVIEW)
    reason = req.status_reason or ("" if req.status in STATUS_DISPLAY else _DEFAULT_REVIEW_REASON.get(req.status, ""))
    fact = company_fact_evidence(category, req, opportunity, facts)
    override = validation.get("override")
    return {
        **base,
        "category": category,
        "category_label": CATEGORY_LABELS[category],
        "status_label": label,
        "status_tone": tone,
        "status_reason_text": reason,
        "company_fact": fact,
        "verified_evidence": [e for e in base["evidence"] if e["verification_status"] == "verified"],
        "deterministic": base["validator_output"],
        "ai": _ai_view(validation.get("ai")),
        "ai_secondary": _ai_view(validation.get("ai_secondary")),
        "blocked_ai_claims": validation.get("blocked_ai_claims") or [],
        "override": override,
        "has_deterministic_failure": any(d.get("status") == "fail" for d in base["validator_output"]),
    }


def _sort_key(row: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        _CATEGORY_ORDER[row["category"]],
        0 if row["status_label"] in {"Missing", "Needs review"} else 1,
        _SEVERITY_ORDER.get(row["severity"] or "", 4),
        row["requirement_id"],
    )


def compliance_view(session: Session, opportunity_id: int, *, facts: dict[str, Any]) -> ComplianceView:
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise ValueError(f"opportunity not found: {opportunity_id}")
    requirements = active_requirements(session, opportunity_id, include_superseded=True)
    current = [r for r in requirements if r.status != "superseded"]
    evidence = evidence_for(session, [r.id for r in current])
    files = {f.id: f for f in session.scalars(select(StoredFile).where(StoredFile.opportunity_id == opportunity_id)).all()}
    rows = sorted(
        (_row(r, _matrix_row(r, evidence.get(r.id, []), files.get(r.source_file_id) if r.source_file_id else None), opportunity, facts)
         for r in current),
        key=_sort_key,
    )
    from govcon.compliance.metrics import coverage_counts, is_mandatory, normalize_mandatory

    for req in current:
        normalize_mandatory(req)
        row_for = next((row for row in rows if row["requirement_id"] == req.id), None)
        if row_for is not None:
            row_for["mandatory"] = req.mandatory
    counted = coverage_counts(current, [])
    counts = {label: 0 for label in ("Met", "Missing", "Needs review", "Not applicable")}
    mandatory = {"total": counted["mandatory_total"], **{label: 0 for label in counts}}
    for row in rows:
        counts[row["status_label"]] += 1
        if is_mandatory(row["mandatory"]) and row["status"] != "not_applicable":
            mandatory[row["status_label"]] += 1
    groups: list[dict[str, Any]] = []
    for key, label in CATEGORIES:
        members = [row for row in rows if row["category"] == key]
        if members:
            groups.append({"key": key, "label": label, "count": len(members),
                           "open": sum(1 for m in members if m["status_label"] in {"Missing", "Needs review"})})
    run = latest_run(session, opportunity_id, "compliance_matrix")
    ai_run = any(row["ai"] or row["ai_secondary"] for row in rows)
    methods = sorted({m for row in rows for m in (row["validation_methods"] or [])})
    return ComplianceView(
        rows=rows,
        groups=groups,
        counts=counts,
        mandatory=mandatory,
        superseded=len(requirements) - len(current),
        missing_facts=sum(1 for row in rows if row["company_fact"].missing),
        run=run,
        run_complete=run is not None and run.status == "complete",
        run_warnings=list(run.warnings or []) if run is not None else [],
        methods=methods,
        ai_run=ai_run,
    )
