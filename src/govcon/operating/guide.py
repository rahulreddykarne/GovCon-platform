"""Guided walk through one stored notice. No sample story is substituted."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.models import BotApproval, Match, Opportunity
from govcon.operating.trace import opportunity_trace

EXPLAIN = {
    "notice": "Ingest writes the notice here. The title and source below are the stored row, not an example.",
    "files": "After ingest, the chain downloads the SAM description body and the DIBBS RFQ PDF for notices just stored. A missing file stays missing.",
    "document": "The document bot reads the stored text and cites passages. Unread files leave this stage incomplete.",
    "matching": "Matching compares the notice with enabled watchlists. Rank is not a decision.",
    "compliance": "Compliance records eligibility, clauses, deadlines, and questions that are still unanswered.",
    "awards": "Awards shows historical USAspending rows already stored. History is not this notice's price.",
    "amendment": "Amendments list source events. An empty event list that was read means no stored change.",
    "bid_decision": "Bid or no-bid prepares a recommendation. It does not submit a bid.",
    "human": "You record pursue, no-bid, or a dismissal. That does not contact an agency or send email.",
    "analysis": "Solicitation analysis is a model call only when the gateway allows it. Sparse output stays incomplete.",
}


def guided_walk(session: Session, requested_id: int | None) -> dict[str, Any]:
    choices = [
        {"id": row.id, "title": row.title or row.source_id, "source": row.source}
        for row in session.scalars(select(Opportunity).order_by(Opportunity.updated_at.desc()).limit(8)).all()
    ]
    missing = False
    chosen: Opportunity | None = None
    if requested_id is not None:
        chosen = session.get(Opportunity, requested_id)
        missing = chosen is None
    if chosen is None:
        chosen = _default_opportunity(session)
    if chosen is None:
        return {"opportunity": None, "choices": choices, "stages": [], "missing": missing}
    stages = []
    for stage in opportunity_trace(session, chosen):
        stages.append({**stage, "explain": EXPLAIN.get(stage["id"], "")})
    return {
        "opportunity": {
            "id": chosen.id,
            "title": chosen.title or chosen.source_id,
            "source": chosen.source,
            "source_id": chosen.source_id,
        },
        "choices": choices,
        "stages": stages,
        "missing": missing,
    }


def _default_opportunity(session: Session) -> Opportunity | None:
    approval = session.scalar(
        select(BotApproval).where(
            BotApproval.status == "pending", BotApproval.opportunity_id.is_not(None),
        ).order_by(BotApproval.requested_at.desc()).limit(1)
    )
    if approval is not None and approval.opportunity_id is not None:
        row = session.get(Opportunity, approval.opportunity_id)
        if row is not None:
            return row
    match = session.scalar(
        select(Match).where(Match.status == "new", Match.active.is_(True))
        .order_by(Match.rank_score.desc().nullslast(), Match.id).limit(1)
    )
    if match is not None:
        row = session.get(Opportunity, match.opportunity_id)
        if row is not None:
            return row
    return session.scalar(select(Opportunity).order_by(Opportunity.updated_at.desc(), Opportunity.id.desc()).limit(1))
