"""Shared listing eligibility; unknown deadlines require human confirmation."""

from datetime import UTC, datetime

from govcon.models import Opportunity

ELIGIBLE_STATUSES = frozenset({"open"})


def pursuit_eligibility(opportunity: Opportunity, now: datetime | None = None) -> tuple[str, str | None]:
    if opportunity.status not in ELIGIBLE_STATUSES:
        return "ineligible", f"listing status is {opportunity.status!r}"
    deadline = opportunity.response_deadline
    if deadline is None:
        return "unknown", "response deadline is unknown; confirm before pursuing"
    now = now or datetime.now(UTC)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if deadline <= now:
        return "ineligible", "response deadline has passed"
    return "eligible", None
