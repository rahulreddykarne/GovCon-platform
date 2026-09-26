"""Amendment-driven invalidation (§15.10).

A material amendment is never just appended to the file list:

new amendment → inventory diff → which requirements changed? → mark affected
conclusions STALE → mark affected proposal sections STALE → flag sourcing /
pricing re-runs → re-run compliance validation → re-run JEV (and the bid
decision when material) → flag review reopen when policy requires.

Affected requirements come from version-rule supersession (conflict scan),
replaced/removed source files, amendment change sentences that name a
requirement's section, CLIN, source file, or topic, Phase 1 opportunity
events (deadline, quantity, set-aside), and optionally the
``amendment_analysis`` prompt. The prompt can add stale marks; it cannot
remove them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.structured import StructuredCallError, run_structured_prompt
from govcon.compliance.matrix import active_requirements, record_run, upsert_open_finding
from govcon.compliance.records import Inventory, SourceDocument
from govcon.compliance.text import split_sentences
from govcon.config import Settings, get_settings
from govcon.models import OpportunityEvent, ProposalSection, Requirement, ReviewSession

AMENDMENT_VERSION = "amendment_revalidation.v1"
_CHANGE_VERB = re.compile(r"\b(revised|changed|replaced|deleted|amended|extended|updated|is now|are now|hereby|superseded|modified|added|removed|reduced|increased)\b", re.I)
TOPIC_PATTERNS = {
    "delivery": re.compile(r"\bdeliver|\bship|f\.?o\.?b|lead time|packag|marking", re.I),
    "pricing": re.compile(r"\bpric|\bclin\b|price schedule|bid schedule|quantit", re.I),
    "page_limit": re.compile(r"\bpage", re.I),
    "submission": re.compile(r"due date|deadline|closing|\bsubmi|received by|offers? (are )?due|quotes? (are )?due", re.I),
    "signature": re.compile(r"\bsign", re.I),
    "set_aside": re.compile(r"set-?aside|small business", re.I),
    "country_of_origin": re.compile(r"country of origin|buy american|trade agreements|specialty metals", re.I),
    "technical": re.compile(r"specification|drawing|\bspec\b|purchase description", re.I),
    "formatting": re.compile(r"\bformat|file (name|type|size)", re.I),
    "past_performance": re.compile(r"past performance|references?", re.I),
}
_EVENT_TOPICS = {
    "deadline_changed": ("submission",),
    "quantity_changed": ("pricing",),
    "set_aside_changed": ("set_aside",),
    "files_added": (),
    "links_changed": (),
    "description_changed": (),
}
_REOPEN_REVIEW_STATES = {"review_complete", "approval_pending", "approved_to_bid"}


@dataclass
class InventoryDiff:
    new_file_ids: list[int] = field(default_factory=list)
    replaced: list[dict[str, Any]] = field(default_factory=list)
    removed_file_ids: list[int] = field(default_factory=list)
    new_amendments: list[SourceDocument] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.new_file_ids or self.replaced or self.removed_file_ids)

    @property
    def label(self) -> str:
        numbers = [f"{d.amendment_number:04d}" for d in self.new_amendments if d.amendment_number]
        return f"Amendment {', '.join(numbers)}" if numbers else "a source change"

    def as_dict(self) -> dict[str, Any]:
        return {
            "new_file_ids": self.new_file_ids,
            "replaced": self.replaced,
            "removed_file_ids": self.removed_file_ids,
            "new_amendments": [d.manifest() for d in self.new_amendments],
        }


def inventory_files(inventory: Inventory) -> list[dict[str, Any]]:
    return [{"file_id": d.file_id, "sha256": d.sha256, "filename": d.filename, "document_type": d.document_type} for d in inventory.documents]


def diff_inventory(prior_files: list[dict[str, Any]], inventory: Inventory) -> InventoryDiff:
    prior_ids = {f["file_id"] for f in prior_files}
    prior_by_name = {(f.get("filename") or "").lower(): f for f in prior_files}
    current_ids = {d.file_id for d in inventory.documents}
    diff = InventoryDiff()
    for doc in inventory.documents:
        if doc.file_id in prior_ids:
            continue
        diff.new_file_ids.append(doc.file_id)
        previous = prior_by_name.get((doc.filename or "").lower())
        if previous is not None and previous.get("sha256") != doc.sha256:
            diff.replaced.append({"filename": doc.filename, "old_file_id": previous["file_id"], "new_file_id": doc.file_id})
        if doc.document_type == "amendment":
            diff.new_amendments.append(doc)
    diff.removed_file_ids = sorted(i for i in prior_ids - current_ids if i is not None)
    return diff


def change_sentences(docs: list[SourceDocument]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for doc in docs:
        for page, text in doc.pages():
            for sentence in split_sentences(text or ""):
                if _CHANGE_VERB.search(sentence):
                    out.append({"file_id": doc.file_id, "page": page, "text": sentence})
    return out


def affected_requirement_reasons(
    requirements: list[Any],
    sentences: list[dict[str, Any]],
    *,
    changed_source_file_ids: set[int],
    event_topics: set[str],
    filenames: dict[int | None, str | None],
    exclude_file_ids: set[int],
) -> dict[Any, list[str]]:
    """Pure: map requirement refs to the reasons an amendment affects them.

    ``requirements`` items expose ``ref``, ``requirement_type``, ``text``,
    ``source_section``, ``source_file_id``.
    """
    reasons: dict[Any, list[str]] = {}
    for req in requirements:
        if req.source_file_id in exclude_file_ids:
            continue
        found: list[str] = []
        if req.source_file_id in changed_source_file_ids:
            found.append("source file replaced or removed")
        pattern = TOPIC_PATTERNS.get(req.requirement_type or "")
        section = (req.source_section or "").strip()
        section_id = re.match(r"(?:section\s+)?([A-Z]?\d+(?:\.\d+)*|[A-Z])\b", section, re.I)
        clins = set(re.findall(r"\bCLIN\s*(\d{4}[A-Z]{0,2})", req.text or "", re.I))
        fname = (filenames.get(req.source_file_id) or "").rsplit(".", 1)[0].lower()
        for sentence in sentences:
            text = sentence["text"]
            lowered = text.lower()
            if pattern is not None and pattern.search(text):
                found.append(f"amendment changes {req.requirement_type}: {text[:160]}")
            elif section_id and re.search(rf"\b(section\s+)?{re.escape(section_id.group(1))}\b", text, re.I) and len(section_id.group(1)) > 1:
                found.append(f"amendment revises section {section_id.group(1)}")
            elif clins and any(re.search(rf"\bCLIN\s*{c}\b", text, re.I) for c in clins):
                found.append(f"amendment revises CLIN {', '.join(sorted(clins))}")
            elif fname and len(fname) > 4 and fname in lowered:
                found.append(f"amendment references {fname}")
        if req.requirement_type in event_topics:
            found.append(f"opportunity listing changed ({req.requirement_type})")
        if found:
            reasons[req.ref] = sorted(set(found))
    return reasons


@dataclass
class _ReqView:
    ref: int
    requirement_type: str | None
    text: str
    source_section: str | None
    source_file_id: int | None


def mark_stale(session: Session, requirements: dict[int, Requirement], reasons: dict[int, list[str]], *, label: str) -> dict[str, Any]:
    now = datetime.now(UTC)
    previously_satisfied: list[int] = []
    staled: list[int] = []
    for rid, why in reasons.items():
        req = requirements.get(rid)
        if req is None or req.status == "superseded":
            continue
        if req.status == "satisfied":
            previously_satisfied.append(rid)
        req.status = "stale"
        req.stale_due_to_amendment = True
        req.amendment_changed_at = now
        req.status_reason = f"{label} changed relevant source material: {why[0]}"
        req.blocks_submission = req.mandatory is not False or req.severity == "critical"
        req.version = (req.version or 1) + 1
        staled.append(rid)
    sections: list[int] = []
    if staled:
        rows = session.scalars(select(ProposalSection).where(ProposalSection.requirement_ids.overlap(staled))).all()
        for section in rows:
            section.status = "stale"
            sections.append(section.id)
    session.flush()
    return {"stale_requirement_ids": sorted(staled), "previously_satisfied_ids": sorted(previously_satisfied), "stale_proposal_section_ids": sorted(sections)}


def run_amendment_revalidation(
    session: Session,
    opportunity_id: int,
    inventory: Inventory,
    diff: InventoryDiff,
    *,
    prior_requirement_ids: set[int],
    since: datetime | None,
    superseded_ids: list[int],
    use_ai: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    requirements = {r.id: r for r in active_requirements(session, opportunity_id, include_superseded=True)}
    prior = {rid: r for rid, r in requirements.items() if rid in prior_requirement_ids and r.status != "superseded"}
    docs = inventory.by_file_id()
    new_docs = [docs[i] for i in diff.new_file_ids if i in docs]
    events: list[OpportunityEvent] = []
    if since is not None:
        events = list(session.scalars(select(OpportunityEvent).where(OpportunityEvent.opportunity_id == opportunity_id, OpportunityEvent.detected_at > since)).all())
    event_topics = {t for e in events for t in _EVENT_TOPICS.get(e.event_type, ())}
    changed_files = {int(x["old_file_id"]) for x in diff.replaced} | set(diff.removed_file_ids)
    reasons = affected_requirement_reasons(
        [_ReqView(r.id, r.requirement_type, f"{r.requirement_text} {r.source_quote or ''}", r.source_section, r.source_file_id) for r in prior.values()],
        change_sentences(new_docs),
        changed_source_file_ids=changed_files,
        event_topics=event_topics,
        filenames={d.file_id: d.filename for d in inventory.documents},
        exclude_file_ids=set(diff.new_file_ids),
    )
    warnings: list[dict[str, Any]] = []
    ai_summary: dict[str, Any] = {"status": "not_run"}
    if use_ai and new_docs and prior:
        try:
            result = run_structured_prompt(
                session,
                opportunity_id=opportunity_id,
                prompt_name="amendment_analysis",
                analysis_type="amendment_analysis",
                variables={
                    "REQUIREMENTS_JSON": [{"requirement_id": r.id, "text": r.requirement_text, "type": r.requirement_type, "status": r.status, "section": r.source_section, "values": r.key_values} for r in prior.values()],
                    "AMENDMENT_JSON": [{"file_id": d.file_id, "amendment_number": d.amendment_number, "text": d.text} for d in new_docs],
                    "DOCUMENT_INVENTORY_JSON": [d.manifest() for d in inventory.documents],
                },
                context_manifest={"new_file_ids": diff.new_file_ids, "prior_requirement_ids": sorted(prior)},
                settings=settings,
            )
            added = 0
            for change in result.output.changes:
                for rid in change.affected_requirement_ids:
                    if rid in prior:
                        reasons.setdefault(rid, []).append(f"amendment analysis: {change.change_type}")
                        added += 1
            ai_summary = {"status": "complete", "ai_analysis_id": result.analysis.id, "material": result.output.material, "stale_marks_added": added}
        except StructuredCallError as exc:
            warnings.append({"code": f"amendment_ai_{exc.reason}", "severity": "medium", "message": exc.detail})
            ai_summary = {"status": "failed", "reason": exc.reason}

    marked = mark_stale(session, prior, reasons, label=diff.label)
    affected_types = {prior[rid].requirement_type for rid in marked["stale_requirement_ids"] if rid in prior}
    affected_types |= {requirements[rid].requirement_type for rid in superseded_ids if rid in requirements}
    material = bool(marked["stale_requirement_ids"] or superseded_ids or diff.new_amendments)
    impact = {
        "material": material,
        "rerun_sourcing": bool(affected_types & {"delivery", "technical", "country_of_origin"}),
        "rerun_pricing": bool(affected_types & {"pricing", "delivery"}),
        "rerun_compliance_validation": True,
        "rerun_jev_bid_decision": material,
        "review_reopen_required": False,
    }
    review = session.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opportunity_id))
    if material and review is not None and review.status in _REOPEN_REVIEW_STATES:
        impact["review_reopen_required"] = True

    run = record_run(session, opportunity_id=opportunity_id, run_type="amendment_revalidation", run_version=AMENDMENT_VERSION, output={}, warnings=warnings)
    count = len(marked["previously_satisfied_ids"])
    if material:
        upsert_open_finding(
            session,
            opportunity_id=opportunity_id,
            finding_type="compliance_status_changed",
            severity="high",
            description=(
                f"COMPLIANCE STATUS CHANGED: {count} previously satisfied requirement{'s' if count != 1 else ''} "
                f"require{'s' if count == 1 else ''} revalidation because {diff.label} changed relevant source material "
                f"({len(marked['stale_requirement_ids'])} stale, {len(superseded_ids)} superseded)."
            ),
            detected_by="amendment_revalidation",
            detector_version=AMENDMENT_VERSION,
            source_refs={"diff": diff.as_dict(), **marked, "superseded_ids": superseded_ids, "impact": impact},
            blocks_submission=bool(marked["stale_requirement_ids"]),
            compliance_run_id=run.id,
        )
    for flag, text in (("rerun_sourcing", "Sourcing must be re-checked"), ("rerun_pricing", "Pricing must be re-checked")):
        if impact[flag]:
            upsert_open_finding(
                session,
                opportunity_id=opportunity_id,
                finding_type=flag,
                severity="high",
                description=f"{text}: {diff.label} changed {', '.join(sorted(t for t in affected_types if t))} requirements.",
                detected_by="amendment_revalidation",
                detector_version=AMENDMENT_VERSION,
                blocks_submission=False,
                compliance_run_id=run.id,
            )
    if impact["review_reopen_required"]:
        upsert_open_finding(
            session,
            opportunity_id=opportunity_id,
            finding_type="review_reopen_required",
            severity="high",
            description=f"{diff.label} is material and arrived after review ({review.status}); the review workflow must reopen the affected review.",
            detected_by="amendment_revalidation",
            detector_version=AMENDMENT_VERSION,
            blocks_submission=True,
            compliance_run_id=run.id,
        )
    run.output_json = {
        "diff": diff.as_dict(),
        "reasons": {str(k): v for k, v in reasons.items()},
        "events": [{"event_type": e.event_type, "detected_at": e.detected_at.isoformat()} for e in events],
        **marked,
        "superseded_ids": superseded_ids,
        "impact": impact,
        "ai": ai_summary,
    }
    session.flush()
    return {"run_id": run.id, **run.output_json, "amendment_count": len(inventory.amendments())}
