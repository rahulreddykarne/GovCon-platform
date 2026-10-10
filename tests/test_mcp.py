"""Phase 12 MCP server tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.collaboration.users import invite_user
from govcon.mcp import operations as mcp_ops
from govcon.mcp.serialize import compact_opportunity, failure, truncate_text
from govcon.mcp.server import mcp, update_match
from govcon.models import Award, BidDecision, Match, Opportunity, Pursuit, Watchlist


@pytest.fixture()
def session(upgraded_engine):
    with Session(upgraded_engine) as s:
        yield s
        s.rollback()


@pytest.fixture()
def owner(session: Session):
    return invite_user(
        session,
        email=f"owner-{uuid4().hex[:8]}@example.test",
        display_name="MCP Owner",
        password="correct horse battery",
        role="owner",
    )


@pytest.fixture()
def mcp_actor(owner, monkeypatch):
    """Write tools act as the single user in MCP_ACTOR_EMAIL."""
    from govcon.mcp.context import reset_server_actor

    reset_server_actor()
    monkeypatch.setenv("MCP_ACTOR_EMAIL", owner.email)
    yield owner
    reset_server_actor()


def _opp(
    session: Session,
    *,
    title: str = "MCP fixture opportunity",
    nsn: str = "6515-01-632-0167",
    days: int = 5,
) -> Opportunity:
    row = Opportunity(
        source="sam",
        source_id=f"mcp-{uuid4().hex}",
        title=title,
        description="A" * 800,
        status="open",
        psc_code="6515",
        naics_code="339113",
        nsn=nsn,
        response_deadline=datetime.now(UTC) + timedelta(days=days),
        raw={"fixture": True},
        links={},
    )
    session.add(row)
    session.flush()
    return row


def _watchlist(session: Session) -> Watchlist:
    row = Watchlist(name=f"mcp-{uuid4().hex[:8]}", enabled=True, sources=["sam"])
    session.add(row)
    session.flush()
    return row


def test_mcp_registers_all_phase12_tools():
    tool_names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    expected = {
        "search_opportunities",
        "get_opportunity",
        "get_opportunity_history",
        "price_history",
        "vendor_profile",
        "competitor_summary",
        "list_matches",
        "pipeline_summary",
        "get_bid_analysis",
        "get_compliance_matrix",
        "get_proposal",
        "submission_status",
        "learning_summary",
        "similar_opportunities",
        "update_match",
        "add_pursuit",
        "update_pursuit",
        "assign_reviewer",
        "add_review_comment",
        "request_ai_comment_validation",
        "create_proposal_version",
        "record_outcome",
    }
    assert expected <= tool_names
    # Human-authority decisions are not available to an MCP client.
    human_only = {
        "approve_to_bid",
        "complete_review",
        "record_human_bid_decision",
        "update_requirement_status",
        "set_submission_ready",
        "record_submission_confirmation",
    }
    assert not (human_only & tool_names)


def test_compact_opportunity_truncates_description_and_scrubs_secrets():
    class _Row:
        id = 1
        source = "sam"
        source_id = "abc"
        solicitation_number = None
        title = "t"
        description = "x" * 900
        opportunity_type = None
        psc_code = None
        naics_code = None
        set_aside_code = None
        agency_path = None
        nsn = None
        quantity = None
        unit = None
        estimated_value_min = None
        estimated_value_max = None
        posted_date = None
        response_deadline = None
        status = "open"
        links: ClassVar[dict[str, str]] = {"api_key": "secret-value"}

    payload = compact_opportunity(_Row(), include_full_description=False, description_limit=100)
    assert payload["description_truncated"] is True
    assert len(payload["description"]) == 100
    assert "api_key" not in (payload.get("links") or {})


def test_truncate_text_and_failure_shape():
    assert truncate_text("short", limit=10) == "short"
    assert truncate_text("abcdefghij", limit=7) == "abcd..."
    err = failure("validation_error", "bad input", field="status")
    assert err["ok"] is False
    assert err["error"]["code"] == "validation_error"


def test_dismiss_match_requires_confirm(session: Session, mcp_actor):
    opp = _opp(session)
    watchlist = _watchlist(session)
    match = Match(opportunity_id=opp.id, watchlist_id=watchlist.id, status="new", score=Decimal("1.0"))
    session.add(match)
    session.flush()

    with pytest.raises(ValueError, match="confirm=true"):
        mcp_ops.op_update_match(session, match.id, status="dismissed", confirm=False)

    session.commit()
    denied = update_match(match_id=match.id, status="dismissed", confirm=False)
    assert denied["ok"] is False

    ok = update_match(match_id=match.id, status="dismissed", confirm=True)
    assert ok["ok"] is True
    assert ok["data"]["status"] == "dismissed"


def test_record_outcome_persists_feedback(session: Session, mcp_actor):
    opp = _opp(session)
    mcp_ops.op_add_pursuit(session, opp.id)
    pursuit = session.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp.id))
    pursuit.stage = "submitted"  # fixture: a recorded submission exists
    pursuit.submitted_at = datetime.now(UTC)
    from govcon.models import Submission
    session.add(Submission(opportunity_id=opp.id, pursuit_id=pursuit.id, status="submitted", submitted_at=pursuit.submitted_at))
    session.flush()
    result = mcp_ops.op_record_outcome(
        session,
        opp.id,
        outcome="lost",
        loss_reason="Price not competitive",
        lessons_learned="Need earlier supplier quote",
    )
    assert result["ok"] is True
    assert result["data"]["outcome"] == "lost"
    assert result["data"]["pursuit_stage"] == "lost"


def test_acceptance_e2e_mcp_workflow(session: Session, mcp_actor):
    """Phase 12 acceptance: matches closing soon → prices → bid analysis → reviewing → gaps."""
    opp = _opp(session, title="Top bid candidate fixture", days=5)
    watchlist = _watchlist(session)
    match = Match(
        opportunity_id=opp.id,
        watchlist_id=watchlist.id,
        status="new",
        score=Decimal("9.5"),
        matched_on={"groups": {"nsn": {"status": "matched"}}},
    )
    session.add(match)
    session.add(
        Award(
            source="usaspending",
            award_id=f"mcp-award-{uuid4().hex[:8]}",
            nsn=opp.nsn,
            recipient_name="Fixture Vendor LLC",
            recipient_uei="ZJEUBM5FYLQ2",
            action_date=datetime.now(UTC).date(),
            total_obligation=Decimal(12000),
            unit_price=Decimal("24.50"),
            quantity=Decimal(500),
            raw={"fixture": True},
        )
    )
    session.add(
        BidDecision(
            opportunity_id=opp.id,
            recommendation="bid",
            recommendation_score=Decimal("0.82"),
            strengths={"items": ["Strong NSN history"]},
            risks={"items": ["Short deadline"]},
            missing_information={
                "items": [
                    "Supplier expedited lead-time confirmation",
                    "Country-of-origin certificate",
                ]
            },
            evidence={"items": []},
            rules_result={},
        )
    )
    session.flush()

    # Filter by watchlist_id to avoid hitting the limit(50) when many test matches exist in the DB
    matches = mcp_ops.op_list_matches(
        session, status="new", watchlist_id=watchlist.id, closing_within_days=7
    )
    assert matches["ok"] is True
    ours = [row for row in matches["data"]["matches"] if row["opportunity"]["id"] == opp.id]
    assert len(ours) == 1
    top = ours[0]
    assert top["match_id"] == match.id

    prices = mcp_ops.op_price_history(session, nsn=opp.nsn, limit=5)
    assert prices["ok"] is True
    assert prices["data"]["count"] >= 1
    assert Decimal(prices["data"]["points"][0]["unit_price"]) == Decimal("24.50")

    analysis = mcp_ops.op_get_bid_analysis(session, opp.id)
    assert analysis["ok"] is True
    assert analysis["data"]["bid_decision"]["recommendation"] == "bid"
    missing = analysis["data"]["bid_decision"]["missing_information"]["items"]
    assert "Supplier expedited lead-time confirmation" in missing

    moved = mcp_ops.op_update_match(session, match.id, status="reviewing", confirm=False)
    assert moved["ok"] is True
    assert moved["data"]["status"] == "reviewing"
    session.refresh(match)
    assert match.status == "reviewing"

    matrix = mcp_ops.op_get_compliance_matrix(session, opp.id)
    assert matrix["ok"] is True
    assert "summary" in matrix["data"]

    wrapped = mcp_ops.op_search_opportunities(
        session,
        query="Top bid candidate",
        closing_within_days=7,
        include_full_description=False,
    )
    assert wrapped["ok"] is True
    assert wrapped["data"]["opportunities"][0]["description_truncated"] is True

    pursuit = mcp_ops.op_add_pursuit(session, opp.id, stage="evaluating")
    assert pursuit["ok"] is True

    pipeline = mcp_ops.op_pipeline_summary(session)
    assert pipeline["ok"] is True
    assert pipeline["data"]["stage_counts"].get("evaluating", 0) >= 1

    similar = mcp_ops.op_similar_opportunities(session, opp.id, limit=5)
    assert similar["ok"] is True
    assert similar["data"]["method"] == "heuristic"

    learning = mcp_ops.op_learning_summary(session)
    assert learning["ok"] is True
    # Phase 15 analytics structure replaces legacy outcome_counts
    assert "total_won" in learning["data"] or "outcome_counts" in learning["data"]
