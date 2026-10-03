"""Suggest outcomes for submitted bids from award records (roadmap gap 9, ADR-073).

Runs daily after the USAspending pull, for every submitted pursuit whose
outcome is not yet recorded. Two sources are matched on several identifiers:

- **SAM award notices** with the same solicitation number are a *strong*
  match; the notice names the awardee and the contract number (PIID).
- **USAspending awards** are *strong* when their PIID is one a matching award
  notice named; otherwise they are *possible* when NSN or PSC, awarding agency
  and an action date after submission all agree.

A suggestion is only a suggestion. ``won`` is suggested only when a strong
match names our UEI as awardee, and ``lost`` only when a strong match names
another awardee and no strong match names more than one awardee (a multi-award
solicitation, where another firm's award says nothing about ours). Possible
matches suggest no outcome. Missing award data never suggests a loss. A person
confirms through ``record_outcome``.

Possible matches are ranked (same NSN before same PSC only, then the award
closest after submission) and at most ``MAX_POSSIBLE_PER_BID`` are kept per
bid. Each run sends approvers one notification per bid with new suggestions,
not one per award record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from govcon.config import Settings, get_settings
from govcon.models import Award, Opportunity, OutcomeFeedback, OutcomeSuggestion, Pursuit, User

POSSIBLE_WINDOW = (timedelta(days=-30), timedelta(days=365))
# Possible (non-identifying) matches kept per bid, over all runs.
MAX_POSSIBLE_PER_BID = 5
# Rows read before the agency check and ranking narrow them down.
_POSSIBLE_SCAN_LIMIT = 500


@dataclass
class SuggestionCounts:
    pursuits_checked: int = 0
    created: int = 0
    strong: int = 0


def _norm(value: str | None) -> str:
    return "".join((value or "").upper().split()).replace("-", "")


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        return None


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _agency_segments(path: str | None) -> set[str]:
    """Comparable agency names from a SAM path (dotted) or a USAspending name (slashed)."""
    import re

    segments = set()
    for part in re.split(r"[./]", path or ""):
        words = part.upper().replace("DEPT ", "DEPARTMENT ").replace(",", " ").split()
        name = " ".join(w for w in words if w not in ("OF", "THE"))
        if name:
            segments.add(name)
    return segments


def _same_agency(award_agency: str | None, opportunity_agency: str | None) -> bool:
    return bool(_agency_segments(award_agency) & _agency_segments(opportunity_agency))


def _outcome(strength: str, awardee_uei: str | None, our_uei: str | None) -> str | None:
    """Only a strong match that names an awardee UEI, compared with ours, suggests an outcome."""
    if strength != "strong" or not awardee_uei or not our_uei:
        return None
    return "won" if awardee_uei.strip().upper() == our_uei else "lost"


def _candidates(session: Session, opp: Opportunity, submitted_at: datetime | None) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    piids: set[str] = set()
    if opp.solicitation_number and opp.solicitation_number.strip():
        notices = session.scalars(select(Opportunity).where(
            Opportunity.id != opp.id,
            Opportunity.source == "sam",
            func.upper(func.trim(Opportunity.solicitation_number)) == opp.solicitation_number.strip().upper(),
        )).all()
        for notice in notices:
            award = (notice.raw or {}).get("award")
            if not isinstance(award, dict):
                continue
            if notice.agency_path and opp.agency_path and not _same_agency(notice.agency_path, opp.agency_path):
                continue
            award_date = _day(award.get("date"))
            if submitted_at is not None and award_date is not None and not (
                submitted_at.date() + POSSIBLE_WINDOW[0] <= award_date <= submitted_at.date() + POSSIBLE_WINDOW[1]
            ):
                continue
            awardee = award.get("awardee") or {}
            piid = (award.get("number") or "").strip() or None
            if piid:
                piids.add(_norm(piid))
            found.append({
                "source": "sam_award_notice", "source_ref": notice.source_id, "strength": "strong",
                "identifiers": {"solicitation_number": opp.solicitation_number.strip(), "piid": piid},
                "evidence": {"notice_id": notice.source_id, "title": notice.title, "notice_type": notice.opportunity_type,
                             "agency": notice.agency_path, "award": award},
                "awardee_name": awardee.get("name"), "awardee_uei": awardee.get("ueiSAM"),
                "award_amount": _decimal(award.get("amount")), "award_date": _day(award.get("date")),
            })
    nsns = {n for n in (opp.nsn_candidates or []) if n} | ({opp.nsn} if opp.nsn else set())
    strong_ids: set[str] = set()
    if piids:
        normalized_piid = func.upper(func.replace(func.replace(Award.piid, "-", ""), " ", ""))
        for award in session.scalars(select(Award).where(normalized_piid.in_(piids)).order_by(Award.id)):
            strong_ids.add(award.award_id)
            found.append(_usaspending_match(award, "strong", {"piid": award.piid}))
    if submitted_at is not None and (nsns or opp.psc_code) and opp.agency_path:
        found += _possible_awards(session, opp, submitted_at, nsns, exclude=strong_ids)
    return found


def _possible_awards(session: Session, opp: Opportunity, submitted_at: datetime, nsns: set[str], *,
                     exclude: set[str]) -> list[dict[str, Any]]:
    """The best few same-product, same-agency awards near the submission, best first."""
    submitted = submitted_at.date()
    start, end = submitted + POSSIBLE_WINDOW[0], submitted + POSSIBLE_WINDOW[1]
    product = Award.nsn.in_(nsns) if nsns else Award.psc_code == opp.psc_code
    # Awards after submission first, nearest first; then those shortly before.
    after = Award.action_date >= submitted
    distance = func.abs(Award.action_date - submitted)
    rows = session.scalars(
        select(Award)
        .where(product, Award.action_date >= start, Award.action_date <= end)
        .order_by(after.desc(), distance, Award.id)
        .limit(_POSSIBLE_SCAN_LIMIT)
    )
    matches = []
    for award in rows:
        if award.award_id in exclude or not _same_agency(award.awarding_agency, opp.agency_path):
            continue
        matches.append(_usaspending_match(award, "possible", {
            "nsn": award.nsn if award.nsn in nsns else None,
            "psc_code": award.psc_code if award.psc_code == opp.psc_code else None,
            "agency": award.awarding_agency, "action_date": award.action_date.isoformat() if award.action_date else None,
        }))
        if len(matches) >= MAX_POSSIBLE_PER_BID:
            break
    return matches


def _usaspending_match(award: Award, strength: str, identifiers: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "usaspending", "source_ref": award.award_id, "strength": strength,
        "identifiers": identifiers,
        "evidence": {"award_id": award.award_id, "piid": award.piid, "agency": award.awarding_agency,
                     "quantity": str(award.quantity) if award.quantity is not None else None,
                     "unit_price": str(award.unit_price) if award.unit_price is not None else None},
        "awardee_name": award.recipient_name, "awardee_uei": award.recipient_uei,
        "award_amount": award.total_obligation, "award_date": award.action_date,
    }


def _multi_award(session: Session, opp: Opportunity, matches: list[dict[str, Any]]) -> bool:
    """True when strong records, new or already suggested, name more than one awardee."""
    awardees = {m["awardee_uei"].strip().upper() for m in matches if m["strength"] == "strong" and m["awardee_uei"]}
    awardees |= {uei.strip().upper() for uei in session.scalars(select(OutcomeSuggestion.awardee_uei).where(
        OutcomeSuggestion.opportunity_id == opp.id, OutcomeSuggestion.strength == "strong",
        OutcomeSuggestion.awardee_uei.is_not(None))) if uei and uei.strip()}
    return len(awardees) > 1


def suggest_outcomes(session: Session, *, settings: Settings | None = None) -> SuggestionCounts:
    from govcon.company.registration import company_uei
    from govcon.compliance.pipeline import read_company_facts_file

    settings = settings or get_settings()
    our_uei = company_uei(settings, read_company_facts_file(settings))
    approvers = list(session.scalars(select(User.id).where(User.is_active.is_(True), User.role.in_(("owner", "approver")))))
    decided = select(OutcomeFeedback.opportunity_id).where(OutcomeFeedback.outcome.in_(("won", "lost")))
    counts = SuggestionCounts()
    for pursuit, opp in session.execute(
        select(Pursuit, Opportunity).join(Opportunity, Opportunity.id == Pursuit.opportunity_id)
        .where(Pursuit.stage == "submitted", Pursuit.opportunity_id.not_in(decided))
    ):
        counts.pursuits_checked += 1
        matches = _candidates(session, opp, pursuit.submitted_at)
        multi_award = _multi_award(session, opp, matches)
        if multi_award:
            _withdraw_lost_suggestions(session, opp)
        possible_left = MAX_POSSIBLE_PER_BID - (session.scalar(select(func.count()).select_from(OutcomeSuggestion).where(
            OutcomeSuggestion.opportunity_id == opp.id, OutcomeSuggestion.strength == "possible")) or 0)
        created: list[OutcomeSuggestion] = []
        for match in matches:
            exists = session.scalar(select(OutcomeSuggestion.id).where(
                OutcomeSuggestion.opportunity_id == opp.id, OutcomeSuggestion.source == match["source"],
                OutcomeSuggestion.source_ref == match["source_ref"]))
            if exists is not None:
                continue
            if match["strength"] == "possible":
                if possible_left <= 0:
                    continue
                possible_left -= 1
            outcome = _outcome(match["strength"], match["awardee_uei"], our_uei)
            evidence = _jsonable(match["evidence"])
            if outcome == "lost" and multi_award:
                outcome = None
                evidence["note"] = MULTI_AWARD_NOTE
            suggestion = OutcomeSuggestion(
                opportunity_id=opp.id, source=match["source"], source_ref=match["source_ref"],
                strength=match["strength"], suggested_outcome=outcome,
                matched_identifiers=match["identifiers"], evidence=evidence,
                awardee_name=match["awardee_name"], awardee_uei=match["awardee_uei"],
                award_amount=match["award_amount"], award_date=match["award_date"],
            )
            session.add(suggestion)
            session.flush()
            created.append(suggestion)
            counts.created += 1
            counts.strong += match["strength"] == "strong"
        if created:
            _notify_approvers(session, opp, created, approvers, settings)
    session.flush()
    return counts


MULTI_AWARD_NOTE = ("Award records for this solicitation name more than one awardee (multi-award), "
                    "so another firm's award does not establish a loss.")


def _withdraw_lost_suggestions(session: Session, opp: Opportunity) -> None:
    """A loss suggested before a second awardee appeared no longer follows from the record."""
    for suggestion in session.scalars(select(OutcomeSuggestion).where(
            OutcomeSuggestion.opportunity_id == opp.id, OutcomeSuggestion.status == "suggested",
            OutcomeSuggestion.suggested_outcome == "lost").with_for_update()):
        suggestion.suggested_outcome = None
        suggestion.evidence = {**(suggestion.evidence or {}), "note": MULTI_AWARD_NOTE}


def _notify_approvers(session: Session, opp: Opportunity, created: list[OutcomeSuggestion],
                      approvers: list[int], settings: Settings) -> None:
    """One notification per bid per run, however many records matched."""
    from govcon.collaboration.notifications import notify

    strong = [s for s in created if s.strength == "strong"]
    outcomes = sorted({s.suggested_outcome for s in strong if s.suggested_outcome})
    lead = strong[0] if strong else created[0]
    payload = {"title": opp.title, "new_suggestions": len(created), "strong": len(strong),
               "strength": lead.strength, "suggested_outcome": "/".join(outcomes) or "none",
               "awardee": lead.awardee_name or "unknown"}
    for user_id in approvers:
        notify(session, user_id=user_id, notification_type="outcome_suggested", opportunity_id=opp.id,
               settings=settings, payload=payload)


def _jsonable(value: Any) -> Any:
    import json

    return json.loads(json.dumps(value, default=str))
