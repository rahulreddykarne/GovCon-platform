"""Competitor intelligence from stored award history (Phase 6)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from govcon.intelligence.awards import AwardeeTotal, top_awardees
from govcon.matching.pricing import canonical_nsn
from govcon.models import Award, Opportunity


@dataclass(frozen=True)
class CompetitorBucket:
    dimension: str
    label: str
    winners: tuple[AwardeeTotal, ...]


@dataclass(frozen=True)
class CompetitorSummary:
    opportunity_id: int
    buckets: tuple[CompetitorBucket, ...]


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    text = str(value).strip()
    return text or None


def _contains(column, value: str):
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return column.ilike(f"%{escaped}%", escape="\\")


def opportunity_office(opportunity: Opportunity) -> str | None:
    raw = opportunity.raw if isinstance(opportunity.raw, dict) else {}
    office = _text(raw.get("office"))
    if office:
        return office
    path = _text(opportunity.agency_path)
    if path and "." in path:
        return path.rsplit(".", 1)[-1]
    return path


def top_awardees_for_agency(
    session: Session,
    agency: str,
    *,
    limit: int = 5,
) -> list[AwardeeTotal]:
    needle = agency.strip()
    if not needle or limit < 1:
        return []
    group_key = func.coalesce(Award.recipient_uei, Award.recipient_name)
    statement = (
        select(
            func.max(Award.recipient_uei),
            func.max(Award.recipient_name),
            func.count().label("award_count"),
            func.sum(Award.total_obligation).label("total_obligation"),
        )
        .where(_contains(Award.awarding_agency, needle))
        .group_by(group_key)
        .order_by(
            func.count().desc(),
            func.sum(Award.total_obligation).desc().nulls_last(),
        )
        .limit(limit)
    )
    totals: list[AwardeeTotal] = []
    for recipient_uei, recipient_name, award_count, total_obligation in session.execute(statement):
        totals.append(
            AwardeeTotal(
                recipient_name=recipient_name,
                recipient_uei=recipient_uei,
                award_count=int(award_count),
                total_obligation=total_obligation,
            )
        )
    return totals


def top_awardees_for_office(
    session: Session,
    office: str,
    *,
    limit: int = 5,
) -> list[AwardeeTotal]:
    needle = office.strip()
    if not needle or limit < 1:
        return []
    sub_agency = Award.raw["Awarding Sub Agency"].astext
    group_key = func.coalesce(Award.recipient_uei, Award.recipient_name)
    statement = (
        select(
            func.max(Award.recipient_uei),
            func.max(Award.recipient_name),
            func.count().label("award_count"),
            func.sum(Award.total_obligation).label("total_obligation"),
        )
        .where(
            or_(
                _contains(sub_agency, needle),
                _contains(Award.awarding_agency, needle),
            )
        )
        .group_by(group_key)
        .order_by(
            func.count().desc(),
            func.sum(Award.total_obligation).desc().nulls_last(),
        )
        .limit(limit)
    )
    totals: list[AwardeeTotal] = []
    for recipient_uei, recipient_name, award_count, total_obligation in session.execute(statement):
        totals.append(
            AwardeeTotal(
                recipient_name=recipient_name,
                recipient_uei=recipient_uei,
                award_count=int(award_count),
                total_obligation=total_obligation,
            )
        )
    return totals


def competitor_summary(
    session: Session,
    opportunity_id: int,
    *,
    limit: int = 5,
) -> CompetitorSummary | None:
    """Likely historical competitors for one opportunity."""
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        return None

    buckets: list[CompetitorBucket] = []
    if opportunity.nsn:
        normalized = canonical_nsn(opportunity.nsn)
        if normalized is not None:
            winners = tuple(top_awardees(session, nsn=normalized, limit=limit))
            if winners:
                buckets.append(
                    CompetitorBucket(dimension="nsn", label=normalized, winners=winners)
                )
    if opportunity.psc_code:
        winners = tuple(top_awardees(session, psc=opportunity.psc_code, limit=limit))
        if winners:
            buckets.append(
                CompetitorBucket(
                    dimension="psc",
                    label=opportunity.psc_code,
                    winners=winners,
                )
            )
    if opportunity.agency_path:
        agency_winners: list[AwardeeTotal] = []
        seen: set[tuple[str | None, str | None]] = set()
        segments = [part.strip() for part in opportunity.agency_path.split(".") if part.strip()]
        for segment in reversed(segments):
            for winner in top_awardees_for_agency(session, segment, limit=limit):
                key = (winner.recipient_uei, winner.recipient_name)
                if key in seen:
                    continue
                seen.add(key)
                agency_winners.append(winner)
                if len(agency_winners) >= limit:
                    break
            if len(agency_winners) >= limit:
                break
        if agency_winners:
            buckets.append(
                CompetitorBucket(
                    dimension="agency",
                    label=opportunity.agency_path,
                    winners=tuple(agency_winners[:limit]),
                )
            )
    office = opportunity_office(opportunity)
    if office:
        winners = tuple(top_awardees_for_office(session, office, limit=limit))
        if winners:
            buckets.append(
                CompetitorBucket(dimension="office", label=office, winners=winners)
            )

    return CompetitorSummary(opportunity_id=opportunity_id, buckets=tuple(buckets))
