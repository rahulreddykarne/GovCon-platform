"""Clause library (§15.8).

The library assists identification and routing. It does not replace legal
interpretation: a known clause loads verification questions and expected
evidence into a compliance requirement; an unknown clause, a date that
differs from the verified library date, or an alternate/deviation is surfaced
for review.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from govcon.compliance.matrix import (
    active_requirements,
    record_run,
    upsert_open_finding,
)
from govcon.compliance.records import Inventory
from govcon.compliance.text import ClauseReference, find_clause_references, normalize_ws
from govcon.models import ClauseLibraryEntry, Requirement

CLAUSE_CHECK_VERSION = "clause_validation.v1"
SEED_PATH = Path(__file__).parent / "data" / "clause_library_v1.json"
_CATEGORY_TO_TYPE = {
    "representations_certifications": "representation",
    "country_of_origin": "country_of_origin",
    "cybersecurity": "cybersecurity",
    "data_handling": "cybersecurity",
    "delivery": "delivery",
    "inspection": "technical",
    "packaging": "delivery",
    "submission": "submission",
    "administrative": "administrative",
    "dla_specific": "administrative",
}


def load_seed(path: Path = SEED_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def seed_clause_library(session: Session, path: Path = SEED_PATH) -> int:
    """Upsert the verified seed clauses on (family, number)."""
    data = load_seed(path)
    count = 0
    for clause in data["clauses"]:
        values = dict(clause)
        raw_date = values.get("source_version_date")
        values["source_version_date"] = date.fromisoformat(raw_date) if raw_date else None
        stmt = pg_insert(ClauseLibraryEntry).values(**values, active=True)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_clause_library_family_number",
            set_={k: stmt.excluded[k] for k in values if k not in {"clause_family", "clause_number"}} | {"updated_at": datetime.now()},
        )
        session.execute(stmt)
        count += 1
    session.flush()
    return count


@dataclass
class ClauseMatch:
    reference: ClauseReference
    entry: ClauseLibraryEntry | None
    issues: list[str]
    file_ids: list[int | None]


_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _library_date_text(value: date | None) -> str | None:
    return f"{_MONTHS[value.month - 1]} {value.year}" if value else None


def _norm_clause_date(text: str | None) -> str | None:
    import re

    match = re.match(r"\s*([A-Z]{3})[A-Z]*\.?\s+(\d{4})", (text or "").upper())
    return f"{match.group(1)} {match.group(2)}" if match else None


def match_references(references: list[tuple[ClauseReference, int | None]], library: dict[tuple[str, str], ClauseLibraryEntry]) -> list[ClauseMatch]:
    """Pure: map clause references to library entries and flag modifications."""
    merged: dict[tuple[str, str], ClauseMatch] = {}
    for ref, file_id in references:
        key = (ref.family, ref.number)
        entry = library.get(key)
        issues: list[str] = []
        if entry is None or not entry.active:
            issues.append("unknown_clause")
        else:
            library_date = _library_date_text(entry.source_version_date)
            if ref.date_text and library_date and _norm_clause_date(ref.date_text) != library_date:
                issues.append(f"clause_date_differs: solicitation {ref.date_text}, library {library_date}")
            if ref.modifier:
                issues.append(f"clause_modified: {ref.modifier}")
        existing = merged.get(key)
        if existing is None:
            merged[key] = ClauseMatch(ref, entry, issues, [file_id])
        else:
            existing.file_ids.append(file_id)
            for issue in issues:
                if issue not in existing.issues:
                    existing.issues.append(issue)
    return list(merged.values())


def run_clause_validation(session: Session, opportunity_id: int, inventory: Inventory) -> dict[str, Any]:
    """Extract clause references, link/create requirements, and surface unknown/modified clauses."""
    library = {
        (row.clause_family, row.clause_number): row
        for row in session.scalars(select(ClauseLibraryEntry)).all()
        if row.clause_family is not None and row.clause_number is not None
    }
    references: list[tuple[ClauseReference, int | None]] = []
    for doc in inventory.documents:
        for ref in find_clause_references(doc.text or ""):
            references.append((ref, doc.file_id))
    requirements = active_requirements(session, opportunity_id)
    for req in requirements:
        for ref in find_clause_references(f"{req.requirement_text} {req.source_quote or ''}"):
            references.append((ref, req.source_file_id))
    matches = match_references(references, library)

    run = record_run(
        session,
        opportunity_id=opportunity_id,
        run_type="clause_validation",
        run_version=CLAUSE_CHECK_VERSION,
        output={},
    )
    linked: list[int] = []
    created: list[int] = []
    flagged: list[str] = []
    for match in matches:
        label = f"{match.reference.family} {match.reference.number}"
        if match.entry is None:
            upsert_open_finding(
                session,
                opportunity_id=opportunity_id,
                finding_type="unknown_clause",
                severity="medium",
                description=f"{label} is referenced but not in the clause library; review its effect before relying on the matrix.",
                detected_by="clause_library",
                detector_version=CLAUSE_CHECK_VERSION,
                source_refs={"file_ids": match.file_ids, "raw": match.reference.raw},
                certainty="confirmed",
                compliance_run_id=run.id,
            )
            flagged.append(label)
            continue
        requirement = _find_or_create_clause_requirement(session, opportunity_id, match, requirements, run.id)
        (created if requirement.compliance_run_id == run.id else linked).append(requirement.id)
        for issue in match.issues:
            upsert_open_finding(
                session,
                opportunity_id=opportunity_id,
                requirement_id=requirement.id,
                finding_type="clause_modified" if issue.startswith("clause_modified") else "clause_version_mismatch",
                severity="medium",
                description=f"{label}: {issue}. Verify the clause text used in this solicitation.",
                detected_by="clause_library",
                detector_version=CLAUSE_CHECK_VERSION,
                source_refs={"file_ids": match.file_ids, "raw": match.reference.raw},
                certainty="confirmed",
                compliance_run_id=run.id,
            )
            _add_flag(requirement, "clause_needs_review")
            flagged.append(label)
    run.output_json = {
        "references": [
            {
                "clause": f"{m.reference.family} {m.reference.number}",
                "date": m.reference.date_text,
                "modifier": m.reference.modifier,
                "known": m.entry is not None,
                "clause_library_id": m.entry.id if m.entry else None,
                "issues": m.issues,
                "file_ids": sorted({f for f in m.file_ids if f is not None}),
            }
            for m in matches
        ],
        "linked_requirement_ids": sorted(set(linked)),
        "created_requirement_ids": sorted(set(created)),
        "flagged": sorted(set(flagged)),
    }
    session.flush()
    return run.output_json | {"run_id": run.id}


def _add_flag(requirement: Requirement, flag: str) -> None:
    reconciliation = dict(requirement.reconciliation or {})
    flags = list(reconciliation.get("flags") or [])
    if flag not in flags:
        flags.append(flag)
    reconciliation["flags"] = flags
    requirement.reconciliation = reconciliation


def _find_or_create_clause_requirement(
    session: Session,
    opportunity_id: int,
    match: ClauseMatch,
    requirements: list[Requirement],
    run_id: int,
) -> Requirement:
    entry = match.entry
    assert entry is not None
    number = match.reference.number
    for req in requirements:
        if req.clause_library_id == entry.id:
            return req
    for req in requirements:
        if number in f"{req.requirement_text} {req.source_quote or ''}":
            req.clause_library_id = entry.id
            _attach_clause_guidance(req, entry)
            return req
    file_id = next((f for f in match.file_ids if f is not None), None)
    req = Requirement(
        opportunity_id=opportunity_id,
        source_file_id=file_id,
        requirement_type=_CATEGORY_TO_TYPE.get(entry.category or "", "other"),
        requirement_text=f"Comply with {entry.clause_family} {entry.clause_number} {entry.title}.",
        mandatory=True,
        severity=entry.default_risk_level if entry.default_risk_level in {"critical", "high", "medium", "low"} else "medium",
        source_quote=normalize_ws(match.reference.raw)[:500],
        extraction_pass="clause_library",
        independently_confirmed=False,
        status="unreviewed",
        reconciliation={"found_by": ["clause_library"], "flags": ["clause_library_generated"]},
        key_values=None,
        clause_library_id=entry.id,
        compliance_run_id=run_id,
        created_by="clause_library",
        blocks_submission=True,
        source_refs=[{"pass": "clause_library", "source_file_id": file_id, "quote": match.reference.raw}],
    )
    _attach_clause_guidance(req, entry)
    session.add(req)
    session.flush()
    requirements.append(req)
    return req


def _attach_clause_guidance(req: Requirement, entry: ClauseLibraryEntry) -> None:
    validation = dict(req.validation or {})
    validation["clause"] = {
        "clause_library_id": entry.id,
        "clause": f"{entry.clause_family} {entry.clause_number}",
        "title": entry.title,
        "verification_questions": entry.verification_questions,
        "expected_evidence": entry.expected_evidence,
        "default_risk_level": entry.default_risk_level,
        "notice": "Library guidance routes review; it is not a legal interpretation.",
    }
    req.validation = validation
