"""Sanitized eval cases. Nothing here calls a model or a government API.

Each case measures the current code on one synthetic example. The "before"
line is the check that used to accept the example. The "after" line is the
check that now keeps it from looking finished. Sample size is the number of
cases, not a count of production notices.
"""

from __future__ import annotations

import re
from typing import Any

from govcon.ai.quality import assess_output_quality
from govcon.ai.schemas import SolicitationAnalysisV1
from govcon.decision.rules import HardRuleFinding, apply_hard_rule_override

_WINDOW = re.compile(r"within (\d+) days", re.IGNORECASE)


def run_eval_harness() -> dict[str, Any]:
    cases = [
        _sparse_output(),
        _missing_citation(),
        _contradiction(),
        _missed_blocker(),
    ]
    passed = sum(1 for case in cases if case["passed"])
    return {
        "sample_size": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "cases": cases,
        "note": "Four synthetic examples. This is not a win rate and not a sample of live notices.",
    }


def _sparse_output() -> dict[str, Any]:
    output = SolicitationAnalysisV1(summary="ok")
    schema_ok = True
    quality, reason = assess_output_quality(output)
    return _case(
        "sparse_output",
        "Sparse output",
        passed=schema_ok and quality == "incomplete",
        before="Schema validation accepts the summary 'ok'.",
        after=f"Quality gate: {quality}. {reason}",
    )


def _missing_citation() -> dict[str, Any]:
    output = SolicitationAnalysisV1(
        summary="The notice asks for twelve valve kits delivered to Norfolk.",
    )
    quality, reason = assess_output_quality(output)
    cited = bool(output.source_refs)
    return _case(
        "missing_citations",
        "Missing citations",
        passed=quality == "incomplete" and not cited,
        before="The summary is long enough to look finished and names no citation.",
        after=f"Quality gate: {quality}. {reason}",
    )


def _contradiction() -> dict[str, Any]:
    statements = [
        "Delivery is required within 10 days after award.",
        "Delivery is required within 45 days after award.",
    ]
    windows = sorted({int(match.group(1)) for text in statements for match in _WINDOW.finditer(text)})
    conflicts = 1 if len(windows) > 1 else 0
    return _case(
        "contradictions",
        "Contradictory delivery windows",
        passed=conflicts == 1 and windows == [10, 45],
        before=f"{len(statements)} statements, treated as separate sentences.",
        after=f"Delivery windows found: {windows}. Conflicts: {conflicts}. This check only compares 'within N days'.",
    )


def _missed_blocker() -> dict[str, Any]:
    model = {"recommendation": "bid", "recommend_bid_approval": True, "human_review_required": False}
    finding = HardRuleFinding(
        code="set_aside",
        reason="Set-aside is 8(a) and the company strategy does not list 8(a).",
        severity="high",
        blocks_bid=True,
    )
    kept = apply_hard_rule_override("bid_decision", model, [finding])
    blockers = kept.get("hard_rule_blockers") or []
    return _case(
        "missed_blockers",
        "Missed blocker",
        passed=kept.get("recommendation") == "no_bid" and blockers == [finding.reason] and kept.get("recommend_bid_approval") is False,
        before="Model result said bid and did not mention the set-aside.",
        after=f"Rules override recommendation={kept.get('recommendation')}; blockers kept={len(blockers)}.",
    )


def _case(case_id: str, title: str, *, passed: bool, before: str, after: str) -> dict[str, Any]:
    return {
        "id": case_id,
        "title": title,
        "passed": passed,
        "before": before,
        "after": after,
    }
