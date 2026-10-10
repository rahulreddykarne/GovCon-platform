"""Contradiction detection (§15.9).

Deterministic detection compares machine-parsed values (delivery days, page
limits, deadlines, timezones, recipients, quantities, file limits, counts)
stated by different requirements. Precedence comes only from source version
rules: a numbered amendment outranks the base package and lower-numbered
amendments. Anything else (same document, Q&A vs base, SOW vs solicitation,
two amendments with the same number) is ambiguous and becomes
``needs_review``.

The optional AI contradiction pass may surface semantic conflicts; the same
version rules decide recency, and the AI never chooses a controlling
instruction.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.ai.structured import (
    StructuredCallError,
    checked_output,
    run_structured_prompt,
)
from govcon.compliance.matrix import (
    active_requirements,
    close_undetected_findings,
    record_run,
    upsert_open_finding,
)
from govcon.compliance.records import Inventory, SourceDocument
from govcon.compliance.schemas import ContradictionDetectionV1
from govcon.config import Settings, get_settings
from govcon.diagnostics import trace_phase
from govcon.models import Requirement
from govcon.security.classification import DataClassification

CONFLICT_VERSION = "conflict_scan.v1"
SCALAR_TOPICS = (
    "delivery_days", "page_limit", "response_deadline_date", "response_deadline_time", "deadline_timezone",
    "recipient_email", "submission_portal", "max_file_size_mb", "allowed_file_types",
)
_CLIN = re.compile(r"\bCLIN\s*(\d{4}[A-Z]{0,2})\b", re.IGNORECASE)


@dataclass
class RequirementFacts:
    ref: Any
    key_values: dict[str, Any]
    text: str
    file_id: int | None
    severity: str | None
    mandatory: bool | None
    quote: str | None = None


@dataclass
class Conflict:
    topic: str
    values: list[dict[str, Any]]
    resolution: str
    controlling: list[Any] = field(default_factory=list)
    superseded: list[Any] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "values": self.values,
            "resolution": self.resolution,
            "controlling": self.controlling,
            "superseded": self.superseded,
            "reason": self.reason,
        }


def _scope(facts: RequirementFacts) -> str:
    clins = sorted({m.upper() for m in _CLIN.findall(f"{facts.text} {facts.quote or ''}")})
    return ",".join(clins)


def _topic_values(facts: RequirementFacts) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    scope = _scope(facts)
    for topic in SCALAR_TOPICS:
        if topic in facts.key_values and facts.key_values[topic] not in (None, "", []):
            value = facts.key_values[topic]
            normalized = ",".join(sorted(str(v).upper() for v in value)) if isinstance(value, list) else str(value).strip().lower()
            out.append((f"{topic}@{scope}" if scope and topic == "delivery_days" else topic, normalized))
    for clin, qty in (facts.key_values.get("clin_quantities") or {}).items():
        out.append((f"clin_quantity@{clin}", str(qty)))
    if "required_count" in facts.key_values and facts.key_values.get("count_noun"):
        out.append((f"required_count@{facts.key_values['count_noun']}", str(facts.key_values["required_count"])))
    return out


def resolve_precedence(entries: list[tuple[RequirementFacts, str]], docs: dict[int | None, SourceDocument]) -> tuple[str, list[Any], list[Any], str]:
    """Return (resolution, controlling refs, superseded refs, reason) from version rules only."""
    def rank(facts: RequirementFacts) -> tuple[int, str]:
        doc = docs.get(facts.file_id)
        if doc is None:
            return -1, "unknown source"
        return doc.precedence_rank, f"{doc.document_type}{f' {doc.amendment_number:04d}' if doc.amendment_number else ''}"

    ranked = sorted(entries, key=lambda e: rank(e[0])[0], reverse=True)
    top_rank = rank(ranked[0][0])[0]
    top = [e for e in ranked if rank(e[0])[0] == top_rank]
    top_values = {v for _, v in top}
    others = [e for e in ranked if rank(e[0])[0] < top_rank]
    if top_rank >= 10 and len(top_values) == 1 and others and all(rank(e[0])[0] >= 0 for e in entries):
        label = rank(top[0][0])[1]
        return (
            "superseded",
            [e[0].ref for e in top],
            [e[0].ref for e in others if e[1] not in top_values],
            f"{label} is the latest numbered amendment among the conflicting sources",
        )
    if top_rank < 0:
        return "ambiguous", [], [], "source document for at least one statement is unknown"
    if len(top_values) > 1:
        return "ambiguous", [], [], "conflicting statements come from sources with the same precedence"
    return "ambiguous", [], [], "no amendment/version evidence establishes which instruction controls"


def detect_conflicts(facts: list[RequirementFacts], docs: dict[int | None, SourceDocument]) -> list[Conflict]:
    """Pure: group requirements by parsed topic and report differing values."""
    by_topic: dict[str, list[tuple[RequirementFacts, str]]] = {}
    for item in facts:
        for topic, value in _topic_values(item):
            by_topic.setdefault(topic, []).append((item, value))
    conflicts: list[Conflict] = []
    for topic, entries in sorted(by_topic.items()):
        if len({v for _, v in entries}) < 2:
            continue
        resolution, controlling, superseded, reason = resolve_precedence(entries, docs)
        values = []
        for item, value in entries:
            doc = docs.get(item.file_id)
            values.append({
                "requirement": item.ref,
                "value": value,
                "file_id": item.file_id,
                "document_type": doc.document_type if doc else None,
                "amendment_number": doc.amendment_number if doc else None,
                "quote": item.quote,
            })
        conflicts.append(Conflict(topic, values, resolution, controlling, superseded, reason))
    return conflicts


def _facts_for(req: Requirement) -> RequirementFacts:
    return RequirementFacts(
        ref=req.id,
        key_values=req.key_values or {},
        text=req.requirement_text,
        file_id=req.source_file_id,
        severity=req.severity,
        mandatory=req.mandatory,
        quote=req.source_quote,
    )


def _flag(req: Requirement, flag: str, detail: dict[str, Any] | None = None) -> None:
    reconciliation = dict(req.reconciliation or {})
    flags = list(reconciliation.get("flags") or [])
    if flag not in flags:
        flags.append(flag)
    reconciliation["flags"] = flags
    if detail:
        reconciliation.setdefault("conflicts", [])
        reconciliation["conflicts"] = [*reconciliation["conflicts"], detail]
    req.reconciliation = reconciliation


def apply_conflicts(session: Session, opportunity_id: int, conflicts: list[Conflict], *, run_id: int, detected_by: str) -> dict[str, list[int]]:
    requirements = {r.id: r for r in active_requirements(session, opportunity_id)}
    superseded_ids: list[int] = []
    ambiguous_ids: list[int] = []
    finding_ids: list[int] = []
    now = datetime.now(UTC)
    for conflict in conflicts:
        involved = [requirements[v["requirement"]] for v in conflict.values if v["requirement"] in requirements]
        if not involved:
            continue
        severe = any(r.severity == "critical" for r in involved)
        potentially_mandatory = any(r.mandatory is not False for r in involved)
        summary = "; ".join(f"req {v['requirement']} ({v['document_type']}{' ' + format(v['amendment_number'], '04d') if v['amendment_number'] else ''}) = {v['value']}" for v in conflict.values)
        if conflict.resolution == "superseded":
            controlling = conflict.controlling[0]
            for old_id in conflict.superseded:
                old = requirements.get(old_id)
                if old is None or old.status == "superseded":
                    continue
                old.status = "superseded"
                old.superseded_by_requirement_id = controlling
                old.status_reason = f"superseded on {conflict.topic}: {conflict.reason}"
                old.blocks_submission = False
                old.version = (old.version or 1) + 1
                superseded_ids.append(old.id)
            for new_id in conflict.controlling:
                new = requirements.get(new_id)
                if new is not None:
                    _flag(new, "supersedes", {"topic": conflict.topic, "superseded": conflict.superseded})
                    new.amendment_changed_at = new.amendment_changed_at or now
            finding = upsert_open_finding(
                session,
                opportunity_id=opportunity_id,
                requirement_id=controlling,
                finding_type="conflict_superseded",
                severity="high" if severe else "medium",
                description=f"CONFLICT / SUPERSESSION DETECTED on {conflict.topic}: {summary}. Latest controlling candidate: requirement {controlling} ({conflict.reason}).",
                detected_by=detected_by,
                detector_version=CONFLICT_VERSION,
                source_refs=conflict.as_dict(),
                blocks_submission=False,
                compliance_run_id=run_id,
            )
            finding_ids.append(finding.id)
        else:
            for req in involved:
                _flag(req, "conflict_ambiguous", {"topic": conflict.topic, "values": [v["value"] for v in conflict.values]})
                ambiguous_ids.append(req.id)
            finding = upsert_open_finding(
                session,
                opportunity_id=opportunity_id,
                requirement_id=involved[0].id,
                finding_type="conflict_ambiguous",
                severity="critical" if severe else "high",
                description=f"CONFLICTING INSTRUCTIONS on {conflict.topic}: {summary}. Precedence is ambiguous ({conflict.reason}); needs review.",
                detected_by=detected_by,
                detector_version=CONFLICT_VERSION,
                source_refs=conflict.as_dict(),
                blocks_submission=potentially_mandatory or severe,
                compliance_run_id=run_id,
            )
            finding_ids.append(finding.id)
    session.flush()
    return {"superseded": sorted(set(superseded_ids)), "ambiguous": sorted(set(ambiguous_ids)), "finding_ids": finding_ids}


@trace_phase("compliance.conflicts.run_conflict_scan")
def run_conflict_scan(
    session: Session,
    opportunity_id: int,
    inventory: Inventory,
    *,
    use_ai: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    docs = inventory.by_file_id()
    requirements = active_requirements(session, opportunity_id)
    conflicts = detect_conflicts([_facts_for(r) for r in requirements], docs)
    run = record_run(session, opportunity_id=opportunity_id, run_type="conflict_scan", run_version=CONFLICT_VERSION, output={})
    applied = apply_conflicts(session, opportunity_id, conflicts, run_id=run.id, detected_by="deterministic_conflict_scan")
    applied["closed_finding_ids"] = close_undetected_findings(
        session, opportunity_id, detected_by="deterministic_conflict_scan", keep_ids=set(applied["finding_ids"]), run_id=run.id,
        finding_types={"conflict_ambiguous"},
    )
    ai_summary: dict[str, Any] = {"status": "not_run"}
    warnings: list[dict[str, Any]] = []
    if use_ai and requirements:
        ai_summary = _ai_conflicts(session, opportunity_id, inventory, requirements, docs, run.id, settings, warnings)
    run.output_json = {"conflicts": [c.as_dict() for c in conflicts], "applied": applied, "ai": ai_summary}
    run.warnings = warnings or None
    session.flush()
    return run.output_json | {"run_id": run.id}


# Part failures confined to one answer; the scan moves on to the next part.
_PART_LOCAL_FAILURES = frozenset({"invalid_output", "output_truncated", "provider_error"})


def _requirement_payload(r) -> dict[str, Any]:
    return {"requirement_id": r.id, "text": r.requirement_text, "type": r.requirement_type, "source_file_id": r.source_file_id,
            "page": r.source_page, "quote": r.source_quote, "values": r.key_values}


def contradiction_parts(requirements: list, max_bytes: int) -> list[list]:
    """Requirements in parts that each fit one call.

    Contradictions are between statements on the same topic, so requirements
    are grouped by type and whole groups are packed together; only a type
    larger than one part is split, and its halves are not compared with each
    other. The deterministic scan still compares key values across all of them.
    """
    groups: dict[str, list] = {}
    for r in requirements:
        groups.setdefault(r.requirement_type or "other", []).append(r)
    sized = {key: [(r, len(json.dumps(_requirement_payload(r), default=str))) for r in rows] for key, rows in groups.items()}
    parts: list[list] = []
    current: list = []
    used = 0
    for key in sorted(sized, key=lambda k: -sum(n for _, n in sized[k])):
        rows = sized[key]
        total = sum(n for _, n in rows)
        if total > max_bytes:  # one type too large for a call: split it on its own
            if current:
                parts.append(current)
                current, used = [], 0
            chunk: list = []
            chunk_used = 0
            for r, n in rows:
                if chunk and chunk_used + n > max_bytes:
                    parts.append(chunk)
                    chunk, chunk_used = [], 0
                chunk.append(r)
                chunk_used += n
            parts.append(chunk)
            continue
        if current and used + total > max_bytes:
            parts.append(current)
            current, used = [], 0
        current.extend(r for r, _ in rows)
        used += total
    if current:
        parts.append(current)
    return parts


def _ai_conflicts(session, opportunity_id, inventory, requirements, docs, run_id, settings, warnings) -> dict[str, Any]:
    by_id = {r.id: r for r in requirements}
    inventory_json = [d.manifest() for d in inventory.documents]
    # Half the call's input limit for requirements; the rest covers the prompt,
    # schema and document inventory.
    max_bytes = max(
        min(
            settings.ai_max_input_tokens_per_call // 2,
            settings.ai_max_input_tokens_per_opportunity // 2,
            settings.ai_source_batch_bytes,
        ) - len(json.dumps(inventory_json, default=str)),
        2_000,
    )
    parts = contradiction_parts(requirements, max_bytes)
    analysis_ids: list[int] = []
    raised = 0
    failed_parts = 0
    pending = list(parts)
    index = 0
    while index < len(pending):
        part = pending[index]
        try:
            result = run_structured_prompt(
                session,
                classification=DataClassification.PUBLIC,
                opportunity_id=opportunity_id,
                prompt_name="contradiction_detection",
                analysis_type=AnalysisType.COMPLIANCE_REVIEW,
                variables={
                    "REQUIREMENTS_JSON": [_requirement_payload(r) for r in part],
                    "DOCUMENT_INVENTORY_JSON": inventory_json,
                },
                context_manifest={"requirement_ids": sorted(r.id for r in part), "part": index + 1, "parts": len(pending),
                                  "files": [d.file_id for d in inventory.documents]},
                settings=settings,
            )
        except StructuredCallError as exc:
            if exc.reason == "output_truncated":
                halves = _split_requirement_part(part)
                if halves:
                    pending[index:index + 1] = halves
                    continue
            failed_parts += 1
            warnings.append({"code": f"contradiction_ai_{exc.reason}", "severity": "medium",
                             "message": f"part {index + 1} of {len(pending)}: {exc.detail}"})
            if exc.reason in _PART_LOCAL_FAILURES:
                index += 1
                continue
            break  # budget, policy or prompt problems fail every later part the same way
        analysis_ids.append(result.analysis.id)
        for item in checked_output(result.output, ContradictionDetectionV1).conflicts:
            _apply_ai_conflict(session, opportunity_id, item, by_id, docs, run_id, result.prompt)
            raised += 1
        index += 1
    if not analysis_ids:
        return {"status": "failed", "reason": warnings[-1]["code"].removeprefix("contradiction_ai_"), "parts": len(pending)}
    status = "complete" if failed_parts == 0 else "partial"
    return {"status": status, "ai_analysis_id": analysis_ids[0], "ai_analysis_ids": analysis_ids,
            "conflicts_raised": raised, "parts": len(pending), "parts_failed": failed_parts}


def _split_requirement_part(part: list) -> list[list]:
    """Halve a truncated contradiction part so it can be retried as smaller calls."""
    if len(part) < 2:
        return []
    mid = len(part) // 2
    return [part[:mid], part[mid:]]


def _apply_ai_conflict(session, opportunity_id, item, by_id, docs, run_id, prompt) -> None:
    ids = [i for i in item.requirement_ids if i in by_id]
    if len(ids) >= 2:
        entries = [(_facts_for(by_id[i]), str(i)) for i in ids]
        resolution, controlling, superseded, reason = resolve_precedence(entries, docs)
        conflict = Conflict(
            f"ai:{item.topic}",
            [{"requirement": i, "value": next((s.value for s in item.statements if s.source_file_id == by_id[i].source_file_id), None), "file_id": by_id[i].source_file_id, "document_type": docs.get(by_id[i].source_file_id).document_type if docs.get(by_id[i].source_file_id) else None, "amendment_number": docs.get(by_id[i].source_file_id).amendment_number if docs.get(by_id[i].source_file_id) else None, "quote": by_id[i].source_quote} for i in ids],
            resolution if resolution == "superseded" and item.precedence == "resolved_by_version" else "ambiguous",
            controlling,
            superseded,
            reason,
        )
        apply_conflicts(session, opportunity_id, [conflict], run_id=run_id, detected_by="contradiction_detection_ai")
        return
    upsert_open_finding(
        session,
        opportunity_id=opportunity_id,
        requirement_id=ids[0] if ids else None,
        finding_type="possible_conflict",
        severity=item.severity,
        description=f"Possible conflict on {item.topic}: {item.description}",
        detected_by="contradiction_detection_ai",
        detector_version=f"{prompt.name}@{prompt.version}:{prompt.content_hash[:12]}",
        source_refs={"statements": [s.model_dump() for s in item.statements]},
        blocks_submission=False,
        certainty="possible",
        compliance_run_id=run_id,
    )
