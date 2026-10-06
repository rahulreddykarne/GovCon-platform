"""Deterministic hard rules and confidence escalation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

HARD_RULE_CONFIG = {
    "max_delivery_slip_days": 0,
    "critical_deadline_days": 2,
}


@dataclass(frozen=True)
class HardRuleFinding:
    code: str
    reason: str
    severity: str
    blocks_bid: bool = True


def evaluate_hard_rules(state: dict[str, Any]) -> list[HardRuleFinding]:
    """Evaluate deterministic hard rules before any JEV decision call."""
    findings: list[HardRuleFinding] = []
    opportunity = state.get("opportunity") or {}
    eligibility = state.get("eligibility") or {}
    sourcing = state.get("sourcing") or {}
    compliance = state.get("compliance") or {}

    status = str(opportunity.get("status") or "").strip().lower()
    if status in {"closed", "cancelled", "archived"}:
        findings.append(
            HardRuleFinding(
                code="solicitation_closed",
                reason="Solicitation is already closed/cancelled/archived.",
                severity="critical",
            )
        )

    days_remaining = opportunity.get("days_remaining")
    if isinstance(days_remaining, (int, float)) and days_remaining < 0:
        findings.append(
            HardRuleFinding(
                code="deadline_passed",
                reason="Response deadline has already passed.",
                severity="critical",
            )
        )

    set_aside_required = bool(opportunity.get("set_aside"))
    set_aside_match = eligibility.get("set_aside_match")
    if set_aside_required and set_aside_match is False:
        findings.append(
            HardRuleFinding(
                code="set_aside_mismatch",
                reason="Mandatory set-aside requirement is not satisfied.",
                severity="critical",
            )
        )

    sam_active = eligibility.get("sam_active")
    if sam_active is False:
        findings.append(
            HardRuleFinding(
                code="sam_inactive",
                reason="Mandatory registration/status indicates inactive SAM profile.",
                severity="critical",
            )
        )

    if eligibility.get("mandatory_certifications_met") is False:
        findings.append(
            HardRuleFinding(
                code="certification_missing",
                reason="Mandatory certification appears absent.",
                severity="high",
            )
        )

    if sourcing.get("product_found") is False:
        findings.append(
            HardRuleFinding(
                code="product_unavailable",
                reason="Required product cannot currently be sourced.",
                severity="high",
            )
        )

    if compliance.get("country_of_origin_conflict") is True:
        findings.append(
            HardRuleFinding(
                code="country_of_origin_conflict",
                reason="Prohibited country-of-origin conflict was identified.",
                severity="critical",
            )
        )

    lead_time_days = sourcing.get("lead_time_days")
    required_delivery_days = opportunity.get("required_delivery_days")
    if isinstance(lead_time_days, (int, float)) and isinstance(required_delivery_days, (int, float)):
        allowed_slip = HARD_RULE_CONFIG["max_delivery_slip_days"]
        if lead_time_days > (required_delivery_days + allowed_slip):
            findings.append(
                HardRuleFinding(
                    code="delivery_infeasible",
                    reason="Supplier lead-time exceeds required delivery schedule.",
                    severity="high",
                )
            )

    if compliance.get("mandatory_missing", 0) not in (None, 0):
        findings.append(
            HardRuleFinding(
                code="mandatory_submission_missing",
                reason="One or more mandatory compliance items are unresolved.",
                severity="high",
                blocks_bid=False,
            )
        )

    return findings


def apply_hard_rule_override(
    bundle_name: str,
    result: dict[str, Any],
    findings: list[HardRuleFinding],
) -> dict[str, Any]:
    """Override bundle output when deterministic hard blockers are present."""
    if not findings:
        return result

    out = dict(result)
    blockers = [item.reason for item in findings if item.blocks_bid]
    if bundle_name == "bid_decision":
        if blockers:
            out["recommendation"] = "no_bid"
            out["recommend_bid_approval"] = False
            out["unresolved_no_bid_issue"] = True
        elif out.get("recommendation") == "bid":
            out["recommendation"] = "review"
            out["recommend_bid_approval"] = False
        out["human_review_required"] = True
        out["hard_rule_blockers"] = blockers
        return out

    if "human_review_required" in out:
        out["human_review_required"] = True
    if bundle_name == "submission_readiness":
        out["status"] = "review"
        out["blocking_issue_exists"] = True
        out["human_verification_required"] = True
    return out


def confidence_to_score(level: str | None) -> float:
    if level == "high":
        return 0.85
    if level == "medium":
        return 0.62
    if level == "low":
        return 0.35
    return 0.5


def enforce_low_confidence_escalation(
    bundle_name: str,
    result: dict[str, Any],
    *,
    confidence: float | None,
    threshold: float,
) -> dict[str, Any]:
    """Route low-confidence/high-risk results to review when required."""
    out = dict(result)
    low_confidence = confidence is not None and confidence < threshold
    if not low_confidence:
        return out

    if "human_review_required" in out:
        out["human_review_required"] = True
    if bundle_name == "bid_decision":
        if out.get("recommendation") not in {"no_bid", "insufficient_information"}:
            out["recommendation"] = "review"
        out["recommend_bid_approval"] = False
        out["evidence_confidence"] = "low"
    if bundle_name == "submission_readiness":
        out["status"] = "review"
        out["human_verification_required"] = True
    return out
