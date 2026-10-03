"""Controlled automatic pursuit of top-ranked matches (roadmap gap 5, ADR-069).

Runs after ranking in the scheduler chains, under the owner's Settings-page
policy (roadmap Q4 = B). A match is pursued automatically only when every
guardrail holds:

- it is an active **rule** match (a semantic recommendation alone never counts);
- the opportunity is eligible (open, deadline known and not passed);
- its rank is at least ``min_score`` and at least ``min_days`` remain;
- factors with data carry at least half the evidence weight, so a score
  built on one or two known factors is left to a person; and its rank is
  still at least ``min_score`` without ``rule_match`` (1.0 for every match);
- it has no pursuit and was not dismissed; and
- fewer than ``max_per_day`` automatic pursuits were made today (UTC).

A top-ranked match whose eligibility is uncertain is never pursued; the inbox
marks it as needing a human eligibility decision. Bid approval stays human.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.matching.eligibility import pursuit_eligibility
from govcon.models import AuditEvent, Match, Opportunity, Pursuit, User
from govcon.workflow.app_settings import AUTO_PURSUE, get_setting

ELIGIBLE_MATCH_STATUSES = ("new", "seen", "reviewing")
MIN_DATA_COVERAGE = 50  # percent of ranking weight backed by data


def data_coverage(factors: list[dict[str, Any]] | None) -> int:
    """Percent of the evidence weight backed by data.

    ``rule_match`` is not evidence here: a match exists only when every rule
    group passed, so it is 1.0 for every match.
    """
    evidence = [f for f in factors or [] if f["name"] != "rule_match"]
    total = sum(f["weight"] for f in evidence)
    return round(100 * sum(f["weight"] for f in evidence if f.get("available")) / total) if total else 0


def guardrail_score(match: Match) -> float:
    """The rank rescaled over the available factors other than ``rule_match``."""
    evidence = [f for f in match.rank_factors or [] if f["name"] != "rule_match" and f.get("available")]
    if not evidence or any(f.get("raw") is None for f in evidence):
        return float(match.rank_score or 0)  # nothing to rescale; coverage decides
    return sum(f["weight"] * f["raw"] for f in evidence) / sum(f["weight"] for f in evidence) * 100


@dataclass
class AutoPursueResult:
    enabled: bool
    pursued: list[int] = field(default_factory=list)
    needs_eligibility_decision: list[int] = field(default_factory=list)
    skipped_daily_cap: int = 0


def pursued_today(session: Session, now: datetime) -> int:
    start = datetime.combine(now.astimezone(UTC).date(), time.min, tzinfo=UTC)
    return session.scalar(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action_type == "pursuit_created",
            AuditEvent.created_at >= start,
            AuditEvent.new_value["via"].astext == "auto_policy",
        )
    ) or 0


def run_auto_pursue(session: Session, *, now: datetime | None = None) -> AutoPursueResult:
    from govcon.collaboration.notifications import notify
    from govcon.workflow.pursuits import create_or_get_pursuit

    now = now or datetime.now(UTC)
    policy = get_setting(session, AUTO_PURSUE)
    result = AutoPursueResult(enabled=bool(policy["enabled"]))
    if not result.enabled:
        return result
    has_pursuit = select(Pursuit.id).where(Pursuit.opportunity_id == Match.opportunity_id).exists()
    candidates = session.execute(
        select(Match, Opportunity)
        .join(Opportunity, Opportunity.id == Match.opportunity_id)
        .where(Match.active.is_(True), Match.status.in_(ELIGIBLE_MATCH_STATUSES),
               Match.rank_score >= policy["min_score"], ~has_pursuit)
        .order_by(Match.rank_score.desc(), Match.id)
    ).all()
    remaining = max(0, int(policy["max_per_day"]) - pursued_today(session, now))
    approvers = list(session.scalars(select(User.id).where(User.is_active.is_(True), User.role.in_(("owner", "approver")))))
    done: set[int] = set()
    for match, opportunity in candidates:
        if opportunity.id in done:
            continue
        status, _reason = pursuit_eligibility(opportunity, now)
        if status == "unknown" or data_coverage(match.rank_factors) < MIN_DATA_COVERAGE:
            result.needs_eligibility_decision.append(match.id)
            continue
        if status != "eligible" or guardrail_score(match) < policy["min_score"]:
            continue
        if opportunity.response_deadline is None or opportunity.response_deadline - now < timedelta(days=policy["min_days"]):
            continue
        if remaining <= 0:
            result.skipped_daily_cap += 1
            continue
        pursuit, created = create_or_get_pursuit(session, opportunity_id=opportunity.id, actor=None, origin="auto_policy")
        if not created:
            continue
        match.status = "pursuing"
        record_audit(
            session, action_type="auto_pursue_applied", opportunity_id=opportunity.id,
            entity_type="matches", entity_id=match.id,
            new_value={"rank_score": float(match.rank_score), "factors": match.rank_factors, "policy": policy},
        )
        for user_id in approvers:
            notify(session, user_id=user_id, notification_type="auto_pursued", opportunity_id=opportunity.id,
                   payload={"title": opportunity.title, "rank_score": float(match.rank_score)})
        result.pursued.append(opportunity.id)
        done.add(opportunity.id)
        remaining -= 1
    session.flush()
    return result


def needs_eligibility_decision(match: Match, opportunity: Opportunity, policy: dict[str, Any], now: datetime) -> bool:
    """For the inbox: top-ranked, but eligibility or ranking data is too uncertain to pursue automatically."""
    return (
        match.rank_score is not None and float(match.rank_score) >= policy["min_score"]
        and (pursuit_eligibility(opportunity, now)[0] == "unknown"
             or data_coverage(match.rank_factors) < MIN_DATA_COVERAGE)
    )
