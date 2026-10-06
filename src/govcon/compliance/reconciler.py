"""Requirement reconciliation (§15.5–15.6).

Conservative by construction:

- candidates from different passes merge only when similarity clears
  ``COMPLIANCE_MERGE_SIMILARITY``, their stated numbers/values agree, and
  exactly one existing group qualifies;
- borderline or ambiguous matches stay separate and are flagged as possible
  duplicates;
- a requirement found by only one pass is always kept and flagged;
- A/B disagreement on mandatory or severity becomes ``needs_review``;
- persistence merges into existing rows and never deletes one: an existing
  requirement that a later extraction does not find is kept and flagged.

The optional AI reconciliation prompt only adds flags; it cannot merge or
drop candidates.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from govcon.compliance.matrix import active_requirements, record_run
from govcon.compliance.records import Candidate, CanonicalRequirement, max_severity
from govcon.compliance.text import containment, jaccard, normalize_ws, numbers, tokens
from govcon.models import Requirement

RECONCILER_VERSION = "requirement_reconciliation.v1"
_PASS_PRIORITY = {"A": 0, "B": 1, "D": 2}
_COMPARABLE_KEYS = ("delivery_days", "page_limit", "response_deadline_date", "response_deadline_time", "deadline_timezone", "recipient_email", "required_count", "max_file_size_mb")


def _text_of(c: Candidate) -> str:
    return f"{c.requirement_text} {c.supporting_quote or ''}"


def values_compatible(a_values: dict, b_values: dict, a_text: str, b_text: str) -> bool:
    for key in _COMPARABLE_KEYS:
        if key in a_values and key in b_values and str(a_values[key]).lower() != str(b_values[key]).lower():
            return False
    a_clins = a_values.get("clin_quantities") or {}
    b_clins = b_values.get("clin_quantities") or {}
    for clin in set(a_clins) & set(b_clins):
        if a_clins[clin] != b_clins[clin]:
            return False
    a_nums, b_nums = numbers(a_text), numbers(b_text)
    if a_nums and b_nums and a_nums != b_nums and not (a_nums <= b_nums or b_nums <= a_nums):
        return False
    return True


def similarity(a_text: str, a_quote: str | None, b_text: str, b_quote: str | None) -> float:
    ta, tb = tokens(a_text), tokens(b_text)
    qa, qb = tokens(a_quote or a_text), tokens(b_quote or b_text)
    scores = [jaccard(ta, tb), jaccard(qa, qb)]
    if len(qa) >= 5:
        scores.append(containment(qa, tb | qb))
    if len(qb) >= 5:
        scores.append(containment(qb, ta | qa))
    return max(scores)


def candidate_similarity(a: Candidate, b: Candidate) -> float:
    if not values_compatible(a.key_values, b.key_values, _text_of(a), _text_of(b)):
        return -1.0
    return similarity(a.requirement_text, a.supporting_quote, b.requirement_text, b.supporting_quote)


def reconcile(
    candidates: list[Candidate],
    *,
    merge_threshold: float,
    duplicate_threshold: float,
    ab_independent: bool = True,
) -> list[CanonicalRequirement]:
    """Pure: cluster candidates into canonical requirements without losing any.

    ``ab_independent`` is False when passes A and B ran on the same provider
    and model: their agreement is then one extraction, not two independent
    confirmations.
    """
    groups: list[list[Candidate]] = []
    possible: dict[int, set[int]] = {}
    ambiguous: set[int] = set()
    ordered = sorted(candidates, key=lambda c: (_PASS_PRIORITY.get(c.pass_label, 9), c.source_file_id or 0, c.source_page or 0))
    for cand in ordered:
        above: list[int] = []
        near: list[int] = []
        for index, group in enumerate(groups):
            same_pass = any(m.pass_label == cand.pass_label for m in group)
            best = max(candidate_similarity(cand, m) for m in group)
            if best < 0:
                continue
            if same_pass:
                if best >= 0.95 and all(normalize_ws(m.requirement_text).lower() == normalize_ws(cand.requirement_text).lower() for m in group if m.pass_label == cand.pass_label):
                    above.append(index)
                elif best >= merge_threshold:
                    near.append(index)
                continue
            if best >= merge_threshold:
                above.append(index)
            elif best >= duplicate_threshold:
                near.append(index)
        if len(above) == 1:
            groups[above[0]].append(cand)
            continue
        groups.append([cand])
        new_index = len(groups) - 1
        linked = above + near
        if len(above) > 1:
            ambiguous.add(new_index)
        for other in linked:
            possible.setdefault(new_index, set()).add(other)
            possible.setdefault(other, set()).add(new_index)

    canonicals = [_canonical(group, ab_independent=ab_independent) for group in groups]
    for index, canonical in enumerate(canonicals):
        for other in sorted(possible.get(index, ())):
            canonical.possible_duplicate_of.append(canonicals[other].key)
        if canonical.possible_duplicate_of:
            canonical.flags.append("possible_duplicate")
        if index in ambiguous:
            canonical.flags.append("ambiguous_merge")
    return canonicals


def _key(primary: Candidate) -> str:
    basis = f"{normalize_ws(primary.supporting_quote or primary.requirement_text).lower()}|{primary.source_file_id}|{primary.source_page}"
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def _canonical(group: list[Candidate], *, ab_independent: bool = True) -> CanonicalRequirement:
    ai = [c for c in group if c.pass_label in {"A", "B"}]
    ranked = sorted(group, key=lambda c: (0 if c.citation_verified else 1, _PASS_PRIORITY.get(c.pass_label, 9)))
    primary = sorted(ai or group, key=lambda c: (0 if c.citation_verified else 1, _PASS_PRIORITY.get(c.pass_label, 9)))[0]
    located = next((c for c in ranked if c.citation_verified and c.source_file_id is not None), None)
    found_by = sorted({c.pass_label for c in group}, key=lambda p: _PASS_PRIORITY.get(p, 9))
    flags: list[str] = []
    disagreements: dict[str, Any] = {}

    mandatory_votes = {c.pass_label: c.mandatory for c in ai}
    known = {v for v in mandatory_votes.values() if v is not None}
    if len(known) > 1:
        mandatory: bool | None = True
        disagreements["mandatory"] = mandatory_votes
        flags.append("ab_disagreement")
    elif known:
        mandatory = known.pop()
        if None in mandatory_votes.values():
            disagreements["mandatory_partial"] = mandatory_votes
    else:
        scanner = [c.mandatory for c in group if c.pass_label == "D" and c.mandatory is not None]
        mandatory = True if scanner else None
    if mandatory is None:
        flags.append("mandatory_uncertain")

    severity_votes = {c.pass_label: c.severity for c in ai if c.severity}
    if len(set(severity_votes.values())) > 1:
        disagreements["severity"] = severity_votes
        if "ab_disagreement" not in flags:
            flags.append("ab_disagreement")
    severity = max_severity(list(severity_votes.values())) or max_severity([c.severity for c in group])

    type_votes = {c.pass_label: c.requirement_type for c in ai if c.requirement_type}
    if len(set(type_votes.values())) > 1:
        disagreements["requirement_type"] = type_votes
    requirement_type = primary.requirement_type or next((c.requirement_type for c in group if c.requirement_type), "other")

    response_votes = [c.response_required for c in ai if c.response_required is not None]
    response_required = True if True in response_votes else (False if response_votes else None)

    key_values: dict[str, Any] = {}
    for cand in sorted(group, key=lambda c: 0 if c.pass_label == "D" else 1):
        for k, v in cand.key_values.items():
            key_values.setdefault(k, v)
    confidences = [c.confidence for c in ai if c.confidence is not None]

    both_passes = {"A", "B"} <= set(found_by)
    independently_confirmed = both_passes and ab_independent
    if not both_passes:
        flags.append("single_pass")
        flags.append(f"found_only_by_{'_'.join(found_by)}")
    elif not ab_independent:
        flags.append("ab_same_model")
    if any(c.supporting_quote for c in group) and located is None:
        flags.append("citation_unverified")

    source = located or primary
    canonical = CanonicalRequirement(
        key=_key(source),
        requirement_text=primary.requirement_text,
        requirement_type=requirement_type,
        mandatory=mandatory,
        severity=severity,
        response_required=response_required,
        source_file_id=source.source_file_id if located else None,
        source_page=source.source_page if located else primary.source_page,
        source_section=source.source_section or primary.source_section,
        source_quote=source.supporting_quote if located else None,
        source_snapshot_id=source.source_snapshot_id,
        confidence=min(confidences) if confidences else None,
        found_by=found_by,
        candidates=list(group),
        independently_confirmed=independently_confirmed,
        flags=flags,
        disagreements=disagreements,
        key_values=key_values,
        clause_refs=sorted({r for c in group for r in c.clause_refs}),
    )
    if not canonical.has_source_location:
        canonical.status = "needs_review"
        canonical.status_reason = "source location could not be established"
    elif "ab_disagreement" in flags:
        canonical.status = "needs_review"
        canonical.status_reason = f"extraction passes disagree on {', '.join(k for k in disagreements if k in {'mandatory', 'severity'})}"
    elif "mandatory_uncertain" in flags:
        canonical.status = "needs_review"
        canonical.status_reason = "mandatory status is ambiguous"
    return canonical


def apply_ai_hints(canonicals: list[CanonicalRequirement], groups: list[dict[str, Any]]) -> None:
    """Record AI reconciliation relationships as flags only (never merge or drop)."""
    by_candidate = {c.candidate_id: canonical for canonical in canonicals for c in canonical.candidates}
    for group in groups:
        members = {by_candidate[cid].key: by_candidate[cid] for cid in group.get("candidate_ids", []) if cid in by_candidate}
        if len(members) < 2:
            continue
        relationship = group.get("relationship")
        if not isinstance(relationship, str):
            continue
        flag = {"duplicate": "ai_possible_duplicate", "conflicting": "ai_possible_conflict", "possible_supersession": "ai_possible_supersession", "uncertain": "ai_uncertain_relationship"}.get(relationship)
        if not flag:
            continue
        for key, canonical in members.items():
            if flag not in canonical.flags:
                canonical.flags.append(flag)
            for other in members:
                if other != key and other not in canonical.possible_duplicate_of:
                    canonical.possible_duplicate_of.append(other)


# ── persistence ──


def _requirement_similarity(req: Requirement, canonical: CanonicalRequirement) -> float:
    if not values_compatible(req.key_values or {}, canonical.key_values, f"{req.requirement_text} {req.source_quote or ''}", f"{canonical.requirement_text} {canonical.source_quote or ''}"):
        return -1.0
    return similarity(req.requirement_text, req.source_quote, canonical.requirement_text, canonical.source_quote)


def persist_reconciliation(
    session: Session,
    opportunity_id: int,
    canonicals: list[CanonicalRequirement],
    *,
    merge_threshold: float,
    run_label: str = "extraction",
    source_snapshot_ids: list[int] | None = None,
    pass_runs: dict[str, int | None] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    input_hash: str | None = None,
    inventory_files: list[dict[str, Any]] | None = None,
) -> tuple[list[Requirement], dict[str, Any]]:
    existing = active_requirements(session, opportunity_id)
    matched_existing: set[int] = set()
    inserted: list[Requirement] = []
    updated: list[Requirement] = []
    run = record_run(
        session,
        opportunity_id=opportunity_id,
        run_type="requirement_reconciliation",
        run_version=RECONCILER_VERSION,
        output={},
        status="complete",
        warnings=warnings,
        source_snapshot_ids=source_snapshot_ids,
        input_hash=input_hash,
    )
    for canonical in canonicals:
        best: Requirement | None = None
        best_score = 0.0
        for req in existing:
            if req.id in matched_existing:
                continue
            score = _requirement_similarity(req, canonical)
            if score >= merge_threshold and score > best_score and req.source_file_id == canonical.source_file_id:
                best, best_score = req, score
        if best is not None:
            matched_existing.add(best.id)
            _merge_into(best, canonical)
            canonical.requirement_id = best.id
            updated.append(best)
            continue
        row = _new_requirement(opportunity_id, canonical, run.id, introduced_by=run_label if existing else None)
        session.add(row)
        session.flush()
        canonical.requirement_id = row.id
        inserted.append(row)
    kept: list[int] = []
    for req in existing:
        if req.id in matched_existing:
            continue
        reconciliation = dict(req.reconciliation or {})
        flags = list(reconciliation.get("flags") or [])
        if "not_found_in_latest_extraction" not in flags:
            flags.append("not_found_in_latest_extraction")
        reconciliation["flags"] = flags
        req.reconciliation = reconciliation
        kept.append(req.id)
    independently = sum(1 for c in canonicals if c.independently_confirmed)
    run.output_json = {
        "canonical_count": len(canonicals),
        "inserted_requirement_ids": [r.id for r in inserted],
        "updated_requirement_ids": [r.id for r in updated],
        "retained_unmatched_requirement_ids": kept,
        "independently_confirmed": independently,
        "single_pass": len(canonicals) - independently,
        "needs_review_on_reconciliation": [c.requirement_id for c in canonicals if c.status == "needs_review"],
        "pass_runs": pass_runs or {},
        "inventory_files": inventory_files or [],
        "canonicals": [
            {"requirement_id": c.requirement_id, **c.reconciliation_json(), "source_refs": c.source_refs}
            for c in canonicals
        ],
    }
    session.flush()
    return inserted + updated, {"run_id": run.id, **{k: v for k, v in run.output_json.items() if k not in {"canonicals", "inventory_files"}}}


def _new_requirement(opportunity_id: int, canonical: CanonicalRequirement, run_id: int, *, introduced_by: str | None) -> Requirement:
    reconciliation = canonical.reconciliation_json()
    if introduced_by:
        reconciliation["flags"] = [*reconciliation["flags"], f"introduced_by_{introduced_by}"]
    return Requirement(
        opportunity_id=opportunity_id,
        source_file_id=canonical.source_file_id,
        source_snapshot_id=canonical.source_snapshot_id,
        requirement_type=canonical.requirement_type,
        requirement_text=canonical.requirement_text,
        mandatory=canonical.mandatory,
        severity=canonical.severity,
        source_section=canonical.source_section,
        source_page=canonical.source_page,
        source_quote=canonical.source_quote,
        source_text_hash=hashlib.sha256((canonical.source_quote or "").encode()).hexdigest() if canonical.source_quote else None,
        extraction_pass="+".join(canonical.found_by),
        extraction_confidence=canonical.confidence,
        independently_confirmed=canonical.independently_confirmed,
        response_required=canonical.response_required,
        status=canonical.status,
        status_reason=canonical.status_reason,
        source_refs=canonical.source_refs,
        reconciliation=reconciliation,
        key_values=canonical.key_values or None,
        validation={"clause_refs": canonical.clause_refs} if canonical.clause_refs else None,
        compliance_run_id=run_id,
        created_by="ai" if set(canonical.found_by) & {"A", "B"} else "deterministic",
        blocks_submission=canonical.potentially_mandatory or canonical.is_critical,
    )


def _merge_into(req: Requirement, canonical: CanonicalRequirement) -> None:
    reconciliation = dict(req.reconciliation or {})
    found_by = sorted(set(reconciliation.get("found_by") or []) | set(canonical.found_by), key=lambda p: _PASS_PRIORITY.get(p, 9))
    flags = [f for f in (reconciliation.get("flags") or []) if f != "not_found_in_latest_extraction"]
    for flag in canonical.flags:
        if flag not in flags:
            flags.append(flag)
    # Passes A and B confirm independently only when they ran on different models.
    independent = bool(req.independently_confirmed or canonical.independently_confirmed)
    if {"A", "B"} <= set(found_by):
        flags = [f for f in flags if f != "single_pass" and not f.startswith("found_only_by_")]
    if independent:
        flags = [f for f in flags if f != "ab_same_model"]
    reconciliation.update({"found_by": found_by, "flags": flags, "disagreements": {**(reconciliation.get("disagreements") or {}), **canonical.disagreements}, "last_matched_at": datetime.now(UTC).isoformat()})
    req.reconciliation = reconciliation
    refs = list(req.source_refs or [])
    known = {(r.get("candidate_id"), r.get("pass"), r.get("quote")) for r in refs}
    for ref in canonical.source_refs:
        if (ref.get("candidate_id"), ref.get("pass"), ref.get("quote")) not in known:
            refs.append(ref)
    req.source_refs = refs
    req.independently_confirmed = independent
    req.extraction_pass = "+".join(found_by)
    key_values = dict(req.key_values or {})
    for k, v in canonical.key_values.items():
        key_values.setdefault(k, v)
    req.key_values = key_values or None
    if req.source_quote is None and canonical.source_quote:
        req.source_file_id = canonical.source_file_id
        req.source_page = canonical.source_page
        req.source_quote = canonical.source_quote
    if canonical.severity and (req.severity is None or _sev(canonical.severity) > _sev(req.severity)):
        req.severity = canonical.severity
    if req.mandatory is None and canonical.mandatory is not None:
        req.mandatory = canonical.mandatory


def _sev(value: str | None) -> int:
    from govcon.compliance.records import SEVERITY_ORDER

    return SEVERITY_ORDER.get(value or "", -1)
