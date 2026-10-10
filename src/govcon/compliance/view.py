"""Compliance section view model: every extracted requirement, grouped and explained.

Each row carries its plain status (Met / Missing / Needs review / Not
applicable), the source quote with file and page, evidence from company facts
(or the fact that is missing), the deterministic check results and, beside
them, the AI result. Nothing here changes a status; overrides go through
``matrix.override_requirement``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.compliance.matrix import (
    OVERRIDE_STATUSES,
    evidence_for,
    latest_run,
    open_findings,
)
from govcon.compliance.records import RESOLVED_STATUSES
from govcon.models import AuditEvent, ComplianceRun, Requirement, StoredFile, User

CATEGORIES: tuple[tuple[str, str], ...] = (
    ("section_l", "Section L: instructions to offerors"),
    ("section_m", "Section M: evaluation"),
    ("clauses", "Clauses (FAR / DFARS)"),
    ("certifications", "Certifications and representations"),
    ("registration", "Registration (SAM, UEI, CAGE)"),
    ("set_aside", "Set-aside and eligibility"),
    ("delivery", "Delivery"),
    ("packaging", "Packaging and marking"),
    ("technical", "Technical"),
    ("pricing", "Pricing"),
    ("past_performance", "Past performance"),
    ("other", "Other"),
)
CATEGORY_LABELS = dict(CATEGORIES)

STATUS_LABELS = {
    "satisfied": "Met",
    "missing": "Missing",
    "not_applicable": "Not applicable",
    "unknown": "Needs review",
    "needs_review": "Needs review",
    "unreviewed": "Needs review",
    "stale": "Needs review",
    "superseded": "Superseded",
}
_NEEDS_REVIEW_WHY = {
    "unknown": "not enough evidence to decide",
    "needs_review": "a reviewer must decide",
    "unreviewed": "not reviewed yet",
    "stale": "changed by an amendment; re-check",
}
_OVERRIDE_CHOICES = (
    ("satisfied", "Met"),
    ("missing", "Missing"),
    ("needs_review", "Needs review"),
    ("unknown", "Needs review: not enough evidence"),
    ("not_applicable", "Not applicable"),
)
STATUS_TONE = {"Met": "ok", "Missing": "bad", "Needs review": "warn", "Not applicable": "muted", "Superseded": "muted"}

VALIDATOR_LABELS = {
    "deadline_not_passed": "Deadline not passed",
    "deadline_timezone_consistent": "Deadline time zone",
    "submission_before_deadline": "Planned submission before deadline",
    "page_count_within_limit": "Page limit",
    "required_forms_present": "Required forms present",
    "required_files_present": "Required files present",
    "signatures_confirmed": "Signatures confirmed",
    "amendments_acknowledged": "Amendments acknowledged",
    "pricing_rows_populated": "Pricing rows filled",
    "clins_accounted": "CLINs accounted for",
    "quantities_accounted": "Quantities accounted for",
    "file_types_allowed": "File types allowed",
    "filenames_match": "File names match",
    "file_size_within_limit": "File size limit",
    "recipient_matches": "Recipient matches",
    "portal_matches": "Portal matches",
    "sam_registration_known": "SAM registration",
    "set_aside_matches": "Set-aside eligibility",
    "delivery_date_arithmetic": "Delivery days arithmetic",
    "margin_arithmetic": "Margin arithmetic",
}

_CLAUSE = re.compile(r"\b(?:FAR|DFARS)\b|\b(?:52|252)\.\d{3}-\d{1,4}\b", re.IGNORECASE)
_REGISTRATION = re.compile(r"system for award management|\bSAM\b|\bUEI\b|\bCAGE\b|unique entity", re.IGNORECASE)
_SET_ASIDE = re.compile(r"set[- ]aside|small business|8\(a\)|hubzone|sdvosb|service[- ]disabled|women[- ]owned|wosb",
                        re.IGNORECASE)
_PACKAGING = re.compile(r"packag|marking|\blabel|MIL-STD-129|MIL-STD-2073|bar ?code|palletiz", re.IGNORECASE)
_DELIVERY = re.compile(r"\bdeliver|\bFOB\b|ship(?:ment|ping)?\b|lead time|days (?:after|ARO)", re.IGNORECASE)
_SECTION = re.compile(r"^\s*(?:section\s+)?([LM])(?:[.\-:\s]|\d|$)", re.IGNORECASE)
_EVALUATION = re.compile(r"\bwill be evaluated\b|\bevaluation (?:factor|criteria)\b|\bbasis (?:of|for) award\b",
                         re.IGNORECASE)
_INSTRUCTION_TYPES = frozenset({"formatting", "page_limit", "submission", "signature", "amendment_acknowledgment",
                                "administrative"})


def category_for(req: Requirement) -> str:
    """One display group per requirement. Earlier rules win; the order is the list above, not alphabetical."""
    text = f"{req.requirement_text or ''} {req.source_quote or ''}"
    validation = req.validation or {}
    kind = req.requirement_type or "other"
    section = _SECTION.match(req.source_section or "")
    if validation.get("clause") or req.clause_library_id is not None or (_CLAUSE.search(text) and kind in {
            "other", "administrative", "representation", "certification", "cybersecurity", "country_of_origin"}):
        return "clauses"
    if kind in {"certification", "representation", "country_of_origin", "cybersecurity"}:
        return "certifications"
    if _REGISTRATION.search(text) and kind not in {"technical", "pricing"}:
        return "registration"
    if kind == "set_aside" or (_SET_ASIDE.search(text) and kind in {"other", "administrative"}):
        return "set_aside"
    if _PACKAGING.search(text) and kind in {"delivery", "technical", "other", "administrative"}:
        return "packaging"
    if kind == "delivery" or (_DELIVERY.search(text) and kind == "other"):
        return "delivery"
    if (section and section.group(1).upper() == "M") or _EVALUATION.search(text):
        return "section_m"
    if (section and section.group(1).upper() == "L") or kind in _INSTRUCTION_TYPES:
        return "section_l"
    if kind in {"technical", "pricing", "past_performance"}:
        return kind
    return "other"


@dataclass(frozen=True)
class Fact:
    state: str  # "present" | "missing"
    text: str


def _listed(facts: dict[str, Any], name: str) -> list[str]:
    value = facts.get(name)
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def company_fact_evidence(req: Requirement, category: str, facts: dict[str, Any]) -> list[Fact]:
    """What the approved company facts say about this requirement, or which fact is missing."""
    out: list[Fact] = []
    seen_validators = set()
    for result in (req.validation or {}).get("deterministic") or []:
        name = result.get("validator")
        seen_validators.add(name)
        status = result.get("status")
        reason = str(result.get("reason") or "")
        evidence = result.get("evidence") or {}
        if name == "sam_registration_known":
            if status == "unknown":
                out.append(Fact("missing", "missing company fact: SAM registration status"))
            else:
                out.append(Fact("present", reason))
        elif name == "set_aside_matches":
            needed = evidence.get("status")
            if status == "unknown" and needed:
                out.append(Fact("missing", f"missing company fact: {needed} status"))
            elif status == "unknown":
                out.append(Fact("missing", f"missing mapping: {reason}"))
            else:
                out.append(Fact("present", reason))
        elif name == "delivery_date_arithmetic" and status == "unknown" and "supplier" in reason:
            out.append(Fact("missing", f"missing supplier fact: {reason}"))
    if category == "certifications":
        held = _listed(facts, "certifications")
        if not held:
            out.append(Fact("missing", "missing company fact: certifications held"))
        else:
            text = (req.requirement_text or "").lower()
            named = [cert for cert in held if cert.lower() in text]
            out.append(Fact("present", f"company holds {', '.join(named)}" if named
                            else f"company facts list {', '.join(held)}; none is named in this requirement"))
    if category == "registration" and "sam_registration_known" not in seen_validators:
        status = str(facts.get("sam_registration_status") or "").strip()
        out.append(Fact("present", f"SAM registration status: {status}") if status
                   else Fact("missing", "missing company fact: SAM registration status"))
        uei = str(facts.get("uei") or "").strip()
        out.append(Fact("present", f"UEI on file: {uei}") if uei else Fact("missing", "missing company fact: UEI"))
    if category == "set_aside" and "set_aside_matches" not in seen_validators:
        socio = facts.get("socioeconomic")
        if isinstance(socio, dict) and socio:
            held = sorted(name for name, value in socio.items() if value is True)
            out.append(Fact("present", "company facts assert " + (", ".join(held) if held else "no socioeconomic status")))
        else:
            out.append(Fact("missing", "missing company fact: socioeconomic status"))
    if category == "past_performance":
        refs = _listed(facts, "past_performance")
        out.append(Fact("present", f"{len(refs)} past performance reference(s) on file") if refs
                   else Fact("missing", "missing company fact: past performance references"))
    return out


def _deterministic(req: Requirement) -> list[dict[str, str]]:
    return [
        {
            "label": VALIDATOR_LABELS.get(str(item.get("validator")), str(item.get("validator"))),
            "status": str(item.get("status")),
            "reason": str(item.get("reason") or ""),
        }
        for item in (req.validation or {}).get("deterministic") or []
    ]


def _ai(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict) or not result.get("status"):
        return None
    confidence = result.get("confidence")
    return {
        "status": str(result["status"]),
        "label": STATUS_LABELS.get(str(result["status"]), str(result["status"])),
        "reason": result.get("reason"),
        "confidence": f"{float(confidence):.2f}" if isinstance(confidence, (int, float)) else None,
        "model": " · ".join(str(part) for part in (result.get("provider"), result.get("model")) if part) or None,
    }


def _source(req: Requirement, files: dict[int, StoredFile]) -> dict[str, Any]:
    stored = files.get(req.source_file_id) if req.source_file_id is not None else None
    refs = [ref for ref in req.source_refs or [] if isinstance(ref, dict)]
    return {
        "quote": req.source_quote,
        "page": req.source_page,
        "section": req.source_section,
        "filename": stored.filename if stored else None,
        "file_id": req.source_file_id,
        "other_refs": len(refs) - 1 if len(refs) > 1 else 0,
    }


def _row(req: Requirement, category: str, facts: dict[str, Any], files: dict[int, StoredFile],
         evidence_rows: list[Any]) -> dict[str, Any]:
    label = STATUS_LABELS.get(req.status, req.status)
    validation = req.validation or {}
    deterministic = _deterministic(req)
    override = validation.get("override") if isinstance(validation.get("override"), dict) else None
    return {
        "id": req.id,
        "version": req.version,
        "text": req.requirement_text,
        "type": req.requirement_type or "other",
        "mandatory": req.mandatory,
        "severity": req.severity,
        "status": req.status,
        "label": label,
        "tone": STATUS_TONE.get(label, "warn"),
        "why": req.status_reason or _NEEDS_REVIEW_WHY.get(req.status),
        "blocks_submission": req.blocks_submission,
        "source": _source(req, files),
        "facts": company_fact_evidence(req, category, facts),
        "evidence": [
            {"type": row.evidence_type, "status": row.verification_status, "description": row.description,
             "page": row.source_page}
            for row in evidence_rows
        ],
        "deterministic": deterministic,
        "deterministic_failed": any(item["status"] == "fail" for item in deterministic),
        "ai": _ai(validation.get("ai")),
        "ai_secondary": _ai(validation.get("ai_secondary")),
        "found_by": (req.reconciliation or {}).get("found_by") or ([req.extraction_pass] if req.extraction_pass else []),
        "flags": (req.reconciliation or {}).get("flags") or [],
        "independently_confirmed": req.independently_confirmed,
        "override": override,
    }


def analysis_state(matrix_run: ComplianceRun | None, reconciliation: ComplianceRun | None,
                   requirement_count: int) -> dict[str, Any]:
    """Whether the extraction can be trusted as complete. Partial or sparse output is never 'complete'."""
    if matrix_run is None:
        return {"state": "not_run", "label": "Not run",
                "reasons": ["No compliance run yet. Requirements have not been extracted for this opportunity."]}
    reasons: list[str] = []
    warnings = [w for w in matrix_run.warnings or [] if isinstance(w, dict)]
    for warning in warnings:
        if warning.get("severity") in {"high", "critical"} or warning.get("blocking"):
            reasons.append(str(warning.get("message") or warning.get("code")))
    passes = ((reconciliation.output_json or {}).get("passes") or {}) if reconciliation is not None else {}
    for name, info in sorted(passes.items()):
        if not isinstance(info, dict) or name == "D":
            continue
        if info.get("status") != "complete":
            reasons.append(f"Extraction pass {name} is {info.get('status') or 'missing'}.")
        elif not info.get("candidates"):
            reasons.append(f"Extraction pass {name} returned no requirements.")
    if requirement_count == 0:
        reasons.append("No requirements were extracted. A solicitation with no requirements is not a finished analysis.")
    incomplete = matrix_run.status != "complete" or requirement_count == 0 or bool(
        [r for r in reasons if r.startswith("Extraction pass")])
    if incomplete:
        return {"state": "incomplete", "label": "Incomplete", "reasons": list(dict.fromkeys(reasons))
                or ["The last compliance run did not finish."]}
    return {"state": "complete", "label": "Extraction complete", "reasons": list(dict.fromkeys(reasons))}


def _override_log(session: Session, opportunity_id: int) -> list[dict[str, Any]]:
    events = list(session.scalars(
        select(AuditEvent).where(AuditEvent.opportunity_id == opportunity_id,
                                 AuditEvent.action_type == "compliance_requirement_override")
        .order_by(AuditEvent.created_at.desc()).limit(50)
    ).all())
    names = {user.id: user.display_name for user in session.scalars(
        select(User).where(User.id.in_({event.user_id for event in events if event.user_id is not None}))).all()}
    return [
        {
            "at": event.created_at,
            "requirement_id": event.entity_id,
            "user": names.get(event.user_id, "unknown user") if event.user_id else "unknown user",
            "from": STATUS_LABELS.get(str((event.old_value or {}).get("status")), (event.old_value or {}).get("status")),
            "to": STATUS_LABELS.get(str((event.new_value or {}).get("status")), (event.new_value or {}).get("status")),
            "reason": (event.new_value or {}).get("reason"),
            "acknowledged": (event.new_value or {}).get("deterministic_failures_overridden") or [],
        }
        for event in events
    ]


PAGE_SIZE = 200


def compliance_view(session: Session, opportunity_id: int, facts: dict[str, Any], *, page: int = 1,
                    page_size: int = PAGE_SIZE) -> dict[str, Any]:
    requirements = list(session.scalars(
        select(Requirement).where(Requirement.opportunity_id == opportunity_id).order_by(Requirement.id)
    ).all())
    active = [req for req in requirements if req.status != "superseded"]
    files = {row.id: row for row in session.scalars(
        select(StoredFile).where(StoredFile.opportunity_id == opportunity_id)).all()}
    evidence = evidence_for(session, [req.id for req in active])
    rank = {key: index for index, (key, _) in enumerate(CATEGORIES)}
    order = {"Missing": 0, "Needs review": 1, "Met": 2, "Not applicable": 3}
    rows_all = []
    for req in active:
        category = category_for(req)
        rows_all.append((category, _row(req, category, facts, files, evidence.get(req.id, []))))
    rows_all.sort(key=lambda item: (rank[item[0]], order.get(item[1]["label"], 4), item[1]["mandatory"] is not True,
                                    item[1]["id"]))
    pages = max(1, (len(rows_all) + page_size - 1) // page_size)
    page = min(max(page, 1), pages)
    shown = rows_all[(page - 1) * page_size:page * page_size]
    groups: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in CATEGORIES}
    for category, row in shown:
        groups[category].append(row)
    missing_facts = sorted({fact.text for _, row in rows_all for fact in row["facts"] if fact.state == "missing"})

    counts = {label: 0 for label in ("Met", "Missing", "Needs review", "Not applicable")}
    mandatory = dict(counts)
    for req in active:
        label = STATUS_LABELS.get(req.status, "Needs review")
        counts[label] = counts.get(label, 0) + 1
        if req.mandatory is not False:
            mandatory[label] = mandatory.get(label, 0) + 1
    matrix_run = latest_run(session, opportunity_id, "compliance_matrix")
    reconciliation = latest_run(session, opportunity_id, "requirement_reconciliation")
    return {
        "analysis": analysis_state(matrix_run, reconciliation, len(active)),
        "run": matrix_run,
        "groups": [{"key": key, "label": label, "rows": groups[key]} for key, label in CATEGORIES if groups[key]],
        "total": len(active),
        "page": page,
        "pages": pages,
        "start": (page - 1) * page_size + 1 if shown else 0,
        "end": (page - 1) * page_size + len(shown),
        "superseded": len(requirements) - len(active),
        "counts": counts,
        "mandatory_counts": mandatory,
        "unresolved_mandatory": sum(1 for req in active if req.mandatory is not False and req.status not in RESOLVED_STATUSES),
        "blocking": sum(1 for req in active if req.blocks_submission),
        "missing_facts": missing_facts,
        "findings": [
            {"id": f.id, "severity": f.severity, "type": f.finding_type.replace("_", " "), "description": f.description,
             "blocks_submission": f.blocks_submission, "requirement_id": f.requirement_id}
            for f in open_findings(session, opportunity_id)
        ],
        "override_statuses": [(status, label) for status, label in _OVERRIDE_CHOICES if status in OVERRIDE_STATUSES],
        "override_log": _override_log(session, opportunity_id),
    }
