"""Inbox labels taken from stored bot runs. Missing runs stay 'not run'."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.models import BotRun

_TRACKED = ("document", "matching", "compliance", "bid_decision")


def analysis_labels(session: Session, opportunity_ids: list[int]) -> dict[int, dict[str, str]]:
    """One label per opportunity from the latest stored run of each tracked bot."""
    if not opportunity_ids:
        return {}
    rows = session.scalars(
        select(BotRun).where(
            BotRun.opportunity_id.in_(opportunity_ids),
            BotRun.bot_name.in_(_TRACKED),
        ).order_by(BotRun.started_at.desc(), BotRun.id.desc())
    ).all()
    latest: dict[tuple[int, str], BotRun] = {}
    for row in rows:
        if row.opportunity_id is None:
            continue
        latest.setdefault((row.opportunity_id, row.bot_name), row)
    return {opp_id: _label(opp_id, latest) for opp_id in opportunity_ids}


def _label(opportunity_id: int, latest: dict[tuple[int, str], BotRun]) -> dict[str, str]:
    present = [latest[(opportunity_id, name)] for name in _TRACKED if (opportunity_id, name) in latest]
    if not present:
        return {
            "label": "not run",
            "detail": "No document, match, compliance, or decision run is stored for this notice.",
        }
    failed = [
        row for row in present
        if row.status in {"failed", "blocked"} or (row.outputs or {}).get("state") == "incomplete"
    ]
    if failed:
        names = ", ".join(row.bot_name.replace("_", " ") for row in failed)
        return {
            "label": "incomplete",
            "detail": f"{names} did not finish. This notice is not a completed analysis.",
        }
    bid = latest.get((opportunity_id, "bid_decision"))
    if bid is not None and bid.status == "waiting_approval":
        return {
            "label": "waiting for you",
            "detail": "A recommendation is stored and needs a human decision.",
        }
    if bid is not None and bid.status in {"succeeded", "completed_with_errors"}:
        return {
            "label": "recommendation ready",
            "detail": "A decision package is stored. A person still decides pursuit and submission.",
        }
    return {
        "label": "in progress",
        "detail": "Some agents have run. A bid recommendation is not stored yet.",
    }
