"""Deterministic hard rules and confidence escalation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

HARD_RULE_CONFIG = {
    "max_delivery_slip_days": 0,
    "critical_deadline_days": 2,
}


OPEN_STATUS = "open"
CLOSED_STATUSES = frozenset({"closed", "cancelled", "archived"})


@dataclass(frozen=True)
class HardRuleFinding:
    """A deterministic finding. ``forces`` is what it does to a bid recommendation.

    ``no_bid``: the bid cannot proceed (not open, ineligible, unmet mandatory).
    ``needs_info``: a required input is unknown, so no bid until a person supplies it.
    ``review``: a bid recommendation is downgraded to human review.
    """

    code: str
    reason: str
    severity: str
    blocks_bid: bool = True
    forces: str | None = None

    @property
    def effect(self) -> str:
        return self.forces or ("no_bid" if self.blocks_bid else "review")


def evaluate_hard_rules(state: dict[str, Any]) -> list[HardRuleFinding]:
    """Evaluate deterministic hard rules before any JEV decision call."""
    findings: list[HardRuleFinding] = []
    opportunity = state.get("opportunity") or {}
    eligibility = state.get("eligibility") or {}
    sourcing = state.get("sourcing") or {}
    compliance = state.get("compliance") or {}

    status = str(opportunity.get("status") or "").strip().lower()
    if status in CLOSED_STATUSES:
        findings.append(
            HardRuleFinding(
                code="solicitation_closed",
                reason="Solicitation is already closed/cancelled/archived.",
                severity="critical",
            )
        )
    elif status and status != OPEN_STATUS:
        findings.append(
            HardRuleFinding(
                code="notice_not_open",
                reason=f"Notice is {status.replace('_', ' ')}, not open for offers.",
                severity="critical",
            )
        )
    elif not status:
        findings.append(
            HardRuleFinding(
                code="notice_status_unknown",
                reason="Notice status is not recorded, so it is not known to be open for offers.",
                severity="high",
                blocks_bid=False,
                forces="needs_info",
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
    elif set_aside_required and set_aside_match is None:
        findings.append(
            HardRuleFinding(
                code="set_aside_unverified",
                reason=f"Set-aside {opportunity.get('set_aside')} applies and company facts do not establish eligibility.",
                severity="high",
                blocks_bid=False,
                forces="needs_info",
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

    unmet = int(compliance.get("mandatory_unmet") or 0)
    unresolved = int(compliance.get("mandatory_missing") or 0)
    if unmet:
        findings.append(
            HardRuleFinding(
                code="mandatory_requirement_unmet",
                reason=f"{unmet} mandatory requirement(s) are not met.",
                severity="critical",
            )
        )
    if unresolved > unmet:
        findings.append(
            HardRuleFinding(
                code="mandatory_submission_missing",
                reason=f"{unresolved - unmet} mandatory compliance item(s) are unresolved (unknown or awaiting review).",
                severity="high",
                blocks_bid=False,
                forces="needs_info",
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
    blockers = [item.reason for item in findings if item.effect == "no_bid"]
    needs = [item.reason for item in findings if item.effect == "needs_info"]
    if bundle_name == "bid_decision":
        if blockers:
            out["recommendation"] = "no_bid"
            out["unresolved_no_bid_issue"] = True
        elif needs:
            if out.get("recommendation") != "no_bid":
                out["recommendation"] = "insufficient_information"
            out["sufficient_information"] = False
        elif out.get("recommendation") == "bid":
            out["recommendation"] = "review"
        out["recommend_bid_approval"] = False
        out["human_review_required"] = True
        out["hard_rule_blockers"] = blockers
        out["needs_information"] = needs
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


# How far down missing inputs pull a decision's confidence: with no input
# known the confidence is halved; with every input known it is unchanged.
MISSING_INPUT_CONFIDENCE_FLOOR = 0.5


def coverage_adjusted_confidence(confidence: float | None, coverage: float) -> float | None:
    """Scale a provider's confidence by how much of the scorecard's input weight is known."""
    if confidence is None:
        return None
    coverage = min(max(coverage, 0.0), 1.0)
    return round(confidence * (MISSING_INPUT_CONFIDENCE_FLOOR + (1 - MISSING_INPUT_CONFIDENCE_FLOOR) * coverage), 4)


def arbitrate_bid(
    rules_result: dict[str, Any],
    rules_confidence: float | None,
    *,
    model_provider: str | None,
    model_result: dict[str, Any] | None,
    model_confidence: float | None,
    model_note: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reconcile the rules recommendation with a JEV/LLM one before the hard rules apply.

    Agreement keeps the shared answer. On disagreement neither side wins on
    its own: a missing-input answer from either side stands, otherwise the
    bid goes to human review. The returned record says which side won and why.
    """
    rules_rec = rules_result.get("recommendation")
    record: dict[str, Any] = {
        "rules": {"recommendation": rules_rec, "confidence": rules_confidence},
        "model": None,
    }
    if model_provider in (None, "rules") or model_result is None:
        record.update(winner="rules", why=[model_note or "No JEV or LLM recommendation was produced; the rules result stands."])
        return dict(rules_result), record
    model_rec = model_result.get("recommendation")
    record["model"] = {"provider": model_provider, "recommendation": model_rec, "confidence": model_confidence}
    if model_rec == rules_rec:
        record.update(winner="agree", why=[f"Rules and {model_provider} agree on {str(rules_rec).replace('_', ' ')}."])
        return dict(model_result), record
    out = dict(model_result)
    if "insufficient_information" in (rules_rec, model_rec):
        out["recommendation"] = "insufficient_information"
        out["sufficient_information"] = False
        source = "rules" if rules_rec == "insufficient_information" else model_provider
        record.update(winner=source, why=[
            f"Rules said {str(rules_rec).replace('_', ' ')}, {model_provider} said {str(model_rec).replace('_', ' ')}; "
            f"{source} found required information missing, which stands until it is supplied."])
    else:
        out["recommendation"] = "review"
        record.update(winner="human_review", why=[
            f"Rules said {str(rules_rec).replace('_', ' ')}, {model_provider} said {str(model_rec).replace('_', ' ')}; "
            "they disagree, so a person decides."])
    out["recommend_bid_approval"] = False
    out["human_review_required"] = True
    return out, record


def finalize_arbitration(
    record: dict[str, Any],
    before: dict[str, Any],
    after_rules: dict[str, Any],
    final: dict[str, Any],
    findings: list[HardRuleFinding],
    *,
    raw_confidence: float | None,
    confidence: float | None,
    coverage: float,
    threshold: float,
) -> dict[str, Any]:
    """Name what decided the final recommendation: a hard rule, the confidence gate, or the arbitration."""
    out = dict(record)
    why = list(out.get("why") or [])
    blockers = [f.reason for f in findings if f.effect == "no_bid"]
    needs = [f.reason for f in findings if f.effect == "needs_info"]
    if after_rules.get("recommendation") != before.get("recommendation"):
        if blockers:
            out["winner"] = "rules"
            why.append("Hard blocker(s) force NO-BID: " + "; ".join(blockers))
        elif needs:
            out["winner"] = "rules"
            why.append("Missing required input(s) force NEEDS-INFO: " + "; ".join(needs))
        else:
            out["winner"] = "rules"
            why.append("A hard rule finding downgrades the bid to human review.")
    elif blockers or needs:
        why.append("Hard rule findings agree with this result: " + "; ".join(blockers + needs))
    if final.get("recommendation") != after_rules.get("recommendation"):
        out["winner"] = "confidence_gate"
        why.append(
            f"Confidence {round((confidence or 0) * 100)}% is below the review threshold {round(threshold * 100)}%, "
            "so the recommendation goes to human review.")
    out["why"] = why
    out["final"] = final.get("recommendation")
    out["confidence"] = {"raw": raw_confidence, "adjusted": confidence, "input_coverage": round(coverage, 4),
                         "threshold": threshold}
    return out
