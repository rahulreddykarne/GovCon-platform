"""Compliance benchmark and release gate (§15.20, §25 compliance release gate).

Each case under ``tests/fixtures/compliance/<case>/case.json`` lists source
files, recorded AI extraction outputs, and expectations (mandatory/critical
requirements, conflicts, amendment changes, submission files, statuses that
must not be satisfied). The harness runs the production pure stages —
extraction mapping, deterministic scanner, reconciliation, conflict
precedence, amendment impact, deterministic validators, and the status gate
— and measures:

- mandatory and critical requirement recall
- source-citation accuracy of AI pass citations
- false-satisfied rate
- amendment-change detection and conflict recall
- submission-file completeness and expected-status accuracy

Replay mode (default, used in CI) uses recorded pass outputs. ``live=True``
re-runs passes A/B against the configured provider. The gate fails when any
aggregate metric falls below ``baseline_metrics.json`` or any case misses a
critical requirement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from govcon.compliance.amendments import affected_requirement_reasons, change_sentences
from govcon.compliance.conflicts import RequirementFacts, detect_conflicts
from govcon.compliance.deterministic import SubmissionPackage, ValidationContext, validators_for
from govcon.compliance.extractor import build_context, candidates_from_output, scan_requirements, structural_candidates
from govcon.compliance.inventory import analyze_inventory, classify_document
from govcon.compliance.matrix import ValidationInputs, decide_status
from govcon.compliance.metrics import rate, recall
from govcon.compliance.reconciler import reconcile
from govcon.compliance.records import CanonicalRequirement, Candidate, Inventory, SourceDocument
from govcon.compliance.schemas import RequirementExtractionV1
from govcon.compliance.text import quote_in_text

BENCHMARK_VERSION = "compliance_benchmark.v1"
HIGHER_IS_BETTER = ("mandatory_recall", "critical_recall", "citation_accuracy", "canonical_citation_accuracy", "amendment_change_detection", "conflict_recall", "submission_file_completeness", "expected_status_accuracy")
LOWER_IS_BETTER = ("false_satisfied_rate",)


def default_fixture_root() -> Path:
    from govcon.paths import repo_root

    return repo_root() / "tests" / "fixtures" / "compliance"


@dataclass
class GateOutcome:
    passed: bool
    failures: list[str]


@dataclass
class CaseResult:
    case_id: str
    metrics: dict[str, float | None]
    counts: dict[str, int]
    missed_mandatory: list[str]
    missed_critical: list[str]
    false_satisfied: list[str]
    statuses: dict[str, str | None]
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class SuiteResult:
    cases: list[CaseResult]
    aggregate: dict[str, float | None]
    gate: GateOutcome
    baseline: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "benchmark_version": BENCHMARK_VERSION,
            "aggregate": self.aggregate,
            "gate": {"passed": self.gate.passed, "failures": self.gate.failures},
            "baseline": self.baseline,
            "cases": [c.__dict__ for c in self.cases],
        }


def _documents(case_dir: Path, case: dict[str, Any], stage: str | None) -> list[SourceDocument]:
    docs: list[SourceDocument] = []
    order = case.get("stages", ["base"])
    allowed = order[: order.index(stage) + 1] if stage in order else order
    for spec in case["source_files"]:
        if spec.get("stage", "base") not in allowed:
            continue
        text = (case_dir / spec["path"]).read_text(encoding="utf-8")
        doc_type, amendment_number = classify_document(spec["filename"], text)
        doc_type = spec.get("document_type", doc_type)
        amendment_number = spec.get("amendment_number", amendment_number)
        docs.append(
            SourceDocument(
                file_id=spec["file_id"],
                filename=spec["filename"],
                url=spec.get("url"),
                sha256=spec.get("sha256", f"fixture-{spec['file_id']}"),
                downloaded_at=None,
                snapshot_id=None,
                document_type=doc_type,
                mime_type=spec.get("mime_type", "text/plain"),
                text=text,
                text_extraction_status=spec.get("extraction_status", "success"),
                table_extraction_status="success" if "[Table " in text or "[Sheet:" in text else "not_applicable",
                amendment_number=amendment_number,
                precedence_rank=(10 + (amendment_number or 0)) if doc_type == "amendment" else (1 if doc_type == "qa" else 0),
            )
        )
    return docs


def _recorded_candidates(case_dir: Path, case: dict[str, Any], stage: str, inventory: Inventory) -> list[Candidate]:
    candidates: list[Candidate] = []
    for stage_name in case.get("stages", ["base"]):
        for label, filename in (case.get("recorded_passes", {}).get(stage_name) or {}).items():
            data = json.loads((case_dir / filename).read_text(encoding="utf-8"))
            output = RequirementExtractionV1.model_validate(data)
            mapped = candidates_from_output(label, output.requirements, inventory)
            for c in mapped:
                c.candidate_id = f"{stage_name}-{c.candidate_id}"
            candidates += mapped
        if stage_name == stage:
            break
    return candidates


def _live_candidates(inventory: Inventory, settings) -> list[Candidate]:
    from govcon.ai.structured import run_structured_prompt
    from govcon.compliance.extractor import PASS_PROMPTS, _amendment_json, _inventory_json

    out: list[Candidate] = []
    for label, prompt in PASS_PROMPTS.items():
        chunks = build_context(inventory, label, char_budget=10**7)
        variables = {"DOCUMENT_INVENTORY_JSON": _inventory_json(inventory), "SOURCE_CHUNKS": "\n\n".join(c["text"] for c in chunks), "AMENDMENT_JSON": _amendment_json(inventory)}
        if label == "A":
            variables["OPPORTUNITY_JSON"] = {"benchmark": True}
        result = run_structured_prompt(None, opportunity_id=None, prompt_name=prompt, analysis_type="compliance_review", variables=variables, context_manifest={"benchmark": True}, settings=settings)
        out += candidates_from_output(label, result.output.requirements, inventory)
    return out


def _haystack(canonical: CanonicalRequirement) -> str:
    quotes = " ".join(c.supporting_quote or "" for c in canonical.candidates)
    return f"{canonical.requirement_text} {canonical.source_quote or ''} {quotes}".lower()


def _match(expectations: list[dict[str, Any]], canonicals: list[CanonicalRequirement]) -> dict[str, CanonicalRequirement]:
    matched: dict[str, CanonicalRequirement] = {}
    for item in expectations:
        terms = [t.lower() for t in item["match"]]
        hits = [c for c in canonicals if all(t in _haystack(c) for t in terms)]
        if hits:
            hits.sort(key=lambda c: (0 if set(c.found_by) & {"A", "B"} else 1, -len(c.found_by)))
            matched[item["id"]] = hits[0]
    return matched


def run_case(case_dir: Path, *, live: bool = False, settings=None) -> CaseResult:
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    stages = case.get("stages", ["base"])
    final_stage = stages[-1]
    thresholds = case.get("thresholds", {"merge": 0.72, "duplicate": 0.4})

    def extract(stage: str) -> tuple[Inventory, list[Candidate], list[CanonicalRequirement]]:
        inventory = analyze_inventory(_documents(case_dir, case, stage))
        ai = _live_candidates(inventory, settings) if live else _recorded_candidates(case_dir, case, stage, inventory)
        deterministic = scan_requirements(inventory) + structural_candidates(inventory)
        return inventory, ai, reconcile(ai + deterministic, merge_threshold=thresholds["merge"], duplicate_threshold=thresholds["duplicate"])

    base_inventory, _, base_canonicals = extract(stages[0])
    inventory, ai_candidates, canonicals = extract(final_stage)
    docs = inventory.by_file_id()

    facts = [RequirementFacts(ref=c.key, key_values=c.key_values, text=c.requirement_text, file_id=c.source_file_id, severity=c.severity, mandatory=c.mandatory, quote=c.source_quote) for c in canonicals]
    conflicts = detect_conflicts(facts, docs)
    superseded = {key for conflict in conflicts if conflict.resolution == "superseded" for key in conflict.superseded}
    ambiguous = {v["requirement"] for conflict in conflicts if conflict.resolution == "ambiguous" for v in conflict.values}
    for canonical in canonicals:
        if canonical.key in ambiguous and "conflict_ambiguous" not in canonical.flags:
            canonical.flags.append("conflict_ambiguous")

    stale: set[str] = set()
    if len(stages) > 1:
        new_ids = {d.file_id for d in inventory.documents} - {d.file_id for d in base_inventory.documents}
        base_keys = {c.key for c in base_canonicals}

        @dataclass
        class _View:
            ref: str
            requirement_type: str | None
            text: str
            source_section: str | None
            source_file_id: int | None

        reasons = affected_requirement_reasons(
            [_View(c.key, c.requirement_type, f"{c.requirement_text} {c.source_quote or ''}", c.source_section, c.source_file_id) for c in canonicals if c.key in base_keys and c.key not in superseded],
            change_sentences([docs[i] for i in new_ids if i in docs]),
            changed_source_file_ids=set(),
            event_topics=set(),
            filenames={d.file_id: d.filename for d in inventory.documents},
            exclude_file_ids=new_ids,
        )
        stale = set(reasons)

    evidence = case.get("evidence", {})
    ctx = ValidationContext(
        now=datetime.fromisoformat(case["now"]),
        response_deadline=datetime.fromisoformat(case["opportunity"]["response_deadline"]) if case.get("opportunity", {}).get("response_deadline") else None,
        set_aside_code=case.get("opportunity", {}).get("set_aside_code"),
        company_facts=evidence.get("company_facts", {}),
        package=SubmissionPackage.from_dict(evidence["package"]) if evidence.get("package") else None,
        supplier=evidence.get("supplier", {}),
        known_amendments=[f"{d.amendment_number:04d}" for d in inventory.amendments() if d.amendment_number],
    )
    all_expected = case["expected_mandatory_requirements"]
    matched = _match(all_expected, canonicals)
    recorded_ai = {matched[k].key: v for k, v in case.get("recorded_ai_validation", {}).items() if k in matched}
    statuses_by_key: dict[str, str] = {}
    for canonical in canonicals:
        if canonical.key in superseded:
            statuses_by_key[canonical.key] = "superseded"
            continue
        results = [r.as_dict() for r in validators_for(canonical.requirement_type, canonical.key_values, f"{canonical.requirement_text} {canonical.source_quote or ''}", ctx)]
        decision = decide_status(
            ValidationInputs(
                requirement_type=canonical.requirement_type,
                mandatory=canonical.mandatory,
                severity=canonical.severity,
                has_source_location=canonical.has_source_location,
                current_status=canonical.status,
                stale=canonical.key in stale,
                flags=canonical.flags,
                deterministic=results,
                ai_primary=recorded_ai.get(canonical.key),
                high_confidence_threshold=None,
            )
        )
        statuses_by_key[canonical.key] = decision.status

    critical_ids = set(case.get("expected_critical_requirements", [])) | {e["id"] for e in all_expected if e.get("critical")}
    mandatory_ids = [e["id"] for e in all_expected]
    missed_mandatory = [i for i in mandatory_ids if i not in matched]
    missed_critical = sorted(i for i in critical_ids if i not in matched)

    ai_quoted = [c for c in ai_candidates if c.supporting_quote]
    located = [c for c in canonicals if c.has_source_location]
    canonical_verified = sum(1 for c in located if quote_in_text(c.source_quote, docs[c.source_file_id].page_text(c.source_page)))

    expected_changes = case.get("expected_amendment_changes", [])
    detected_changes = []
    for change in expected_changes:
        target = matched.get(change["requirement"])
        if target is not None and (target.key in superseded or target.key in stale):
            detected_changes.append(change["requirement"])

    expected_conflicts = case.get("expected_conflicts", [])
    found_conflicts = [e for e in expected_conflicts if any(c.topic.split("@")[0] == e["topic"] and c.resolution == e.get("resolution", c.resolution) for c in conflicts)]

    forms_found = {f.upper() for c in canonicals for f in c.key_values.get("forms", [])}
    texts = " ".join(_haystack(c) for c in canonicals)
    expected_files = case.get("expected_submission_files", [])
    files_found = [f for f in expected_files if f.upper() in forms_found or f.lower() in texts]

    not_satisfied = case.get("expected_not_satisfied", [])
    false_sat = [i for i in not_satisfied if i in matched and statuses_by_key.get(matched[i].key) == "satisfied"]
    expected_status = case.get("expected_status", {})
    status_hits = [i for i, s in expected_status.items() if i in matched and statuses_by_key.get(matched[i].key) == s]

    metrics = {
        "mandatory_recall": recall(mandatory_ids, set(matched)),
        "critical_recall": recall(sorted(critical_ids), set(matched)),
        "citation_accuracy": rate(sum(1 for c in ai_quoted if c.citation_verified), len(ai_quoted)),
        "canonical_citation_accuracy": rate(canonical_verified, len(located)),
        "false_satisfied_rate": rate(len(false_sat), len(not_satisfied)),
        "amendment_change_detection": rate(len(detected_changes), len(expected_changes)),
        "conflict_recall": rate(len(found_conflicts), len(expected_conflicts)),
        "submission_file_completeness": rate(len(files_found), len(expected_files)),
        "expected_status_accuracy": rate(len(status_hits), len(expected_status)),
    }
    counts = {
        "expected_mandatory": len(mandatory_ids), "matched_mandatory": len(mandatory_ids) - len(missed_mandatory),
        "expected_critical": len(critical_ids), "matched_critical": len(critical_ids) - len(missed_critical),
        "ai_quoted": len(ai_quoted), "ai_quoted_verified": sum(1 for c in ai_quoted if c.citation_verified),
        "located": len(located), "located_verified": canonical_verified,
        "not_satisfied_expected": len(not_satisfied), "false_satisfied": len(false_sat),
        "expected_changes": len(expected_changes), "detected_changes": len(detected_changes),
        "expected_conflicts": len(expected_conflicts), "found_conflicts": len(found_conflicts),
        "expected_files": len(expected_files), "found_files": len(files_found),
        "expected_statuses": len(expected_status), "status_hits": len(status_hits),
        "canonical_requirements": len(canonicals),
    }
    return CaseResult(
        case_id=case["case_id"],
        metrics=metrics,
        counts=counts,
        missed_mandatory=missed_mandatory,
        missed_critical=missed_critical,
        false_satisfied=false_sat,
        statuses={i: statuses_by_key.get(c.key) for i, c in matched.items()},
        details={
            "found_by": {i: c.found_by for i, c in matched.items()},
            "conflicts": [c.as_dict() for c in conflicts],
            "stale_expected_ids": [i for i, c in matched.items() if c.key in stale],
            "superseded_expected_ids": [i for i, c in matched.items() if c.key in superseded],
            "inventory_warnings": [w.as_dict() for w in inventory.warnings],
        },
    )


def aggregate(cases: list[CaseResult]) -> dict[str, float | None]:
    def total(num: str, den: str) -> float | None:
        return rate(sum(c.counts[num] for c in cases), sum(c.counts[den] for c in cases))

    return {
        "mandatory_recall": total("matched_mandatory", "expected_mandatory"),
        "critical_recall": total("matched_critical", "expected_critical"),
        "citation_accuracy": total("ai_quoted_verified", "ai_quoted"),
        "canonical_citation_accuracy": total("located_verified", "located"),
        "false_satisfied_rate": total("false_satisfied", "not_satisfied_expected"),
        "amendment_change_detection": total("detected_changes", "expected_changes"),
        "conflict_recall": total("found_conflicts", "expected_conflicts"),
        "submission_file_completeness": total("found_files", "expected_files"),
        "expected_status_accuracy": total("status_hits", "expected_statuses"),
    }


def evaluate_gate(cases: list[CaseResult], agg: dict[str, float | None], baseline: dict[str, Any]) -> GateOutcome:
    failures: list[str] = []
    for case in cases:
        if case.missed_critical:
            failures.append(f"{case.case_id}: critical requirement recall regression (missed {', '.join(case.missed_critical)})")
        if case.false_satisfied:
            failures.append(f"{case.case_id}: false SATISFIED for {', '.join(case.false_satisfied)}")
    for name in HIGHER_IS_BETTER:
        floor = baseline.get("min", {}).get(name)
        value = agg.get(name)
        if floor is not None and value is not None and value < floor:
            failures.append(f"{name} {value} below baseline {floor}")
    for name in LOWER_IS_BETTER:
        ceiling = baseline.get("max", {}).get(name)
        value = agg.get(name)
        if ceiling is not None and value is not None and value > ceiling:
            failures.append(f"{name} {value} above baseline {ceiling}")
    return GateOutcome(not failures, failures)


def case_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if (p / "case.json").is_file())


def run_benchmark_suite(root: Path, *, live: bool = False, settings=None, baseline: dict[str, Any] | None = None) -> SuiteResult:
    cases = [run_case(path, live=live, settings=settings) for path in case_dirs(root)]
    if baseline is None:
        baseline_path = root / "baseline_metrics.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.is_file() else {}
    agg = aggregate(cases)
    gate = evaluate_gate(cases, agg, baseline)
    if not cases:
        gate = GateOutcome(False, ["no compliance benchmark cases found"])
    return SuiteResult(cases, agg, gate, baseline)
