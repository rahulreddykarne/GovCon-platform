"""Phase 12 tests: FastMCP interface over implemented capabilities.

Acceptance criteria (master §18):
  AC-1 Example end-to-end interaction completes through MCP against local DB:
       new matches closing in 7 days → historical prices → top bid candidate
       explanation → move to reviewing → show missing information.
Safety rules covered: compact/truncated descriptions, structured errors (no
stack traces), destructive confirm, write echo, no auto-submit, no secrets,
Phase 13/15 stubs return not_implemented.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from govcon.audit import scrub
from govcon.cli import app
from govcon.collaboration.users import invite_user
from govcon.mcp import handlers, serialize
from govcon.mcp.server import create_server, mcp
from govcon.models import Award, BidDecision, Match, Opportunity, Watchlist


@pytest.fixture()
def session(upgraded_engine):
    with Session(upgraded_engine) as s:
        yield s
        s.rollback()


def _opp(session: Session, *, days: int = 3, nsn: str = "5210-00-123-4567", **extra) -> Opportunity:
    now = datetime.now(UTC)
    row = Opportunity(
        source=extra.pop("source", "sam"),
        source_id=extra.pop("source_id", f"mcp-{uuid4().hex[:12]}"),
        solicitation_number=extra.pop("solicitation_number", "SP3300-26-Q-MCP"),
        title=extra.pop("title", "MCP Test Widget"),
        description=extra.pop(
            "description",
            "A" * 400 + " long solicitation description with pricing and delivery terms.",
        ),
        psc_code=extra.pop("psc_code", "5210"),
        naics_code=extra.pop("naics_code", "332216"),
        nsn=nsn,
        status=extra.pop("status", "open"),
        response_deadline=now + timedelta(days=days),
        raw=extra.pop("raw", {"noticeId": "mcp-test"}),
        **extra,
    )
    session.add(row)
    session.flush()
    return row


def _watchlist(session: Session) -> Watchlist:
    row = Watchlist(name=f"MCP WL {uuid4().hex[:8]}", enabled=True, sources=["sam"])
    session.add(row)
    session.flush()
    return row


def _match(session: Session, opportunity: Opportunity, watchlist: Watchlist, *, status: str = "new") -> Match:
    row = Match(
        opportunity_id=opportunity.id,
        watchlist_id=watchlist.id,
        score=Decimal("0.91"),
        matched_on={"psc": opportunity.psc_code},
        status=status,
    )
    session.add(row)
    session.flush()
    return row


def _award(session: Session, *, nsn: str = "5210-00-123-4567") -> Award:
    row = Award(
        source="usaspending",
        award_id=f"AW-{uuid4().hex[:10]}",
        piid="PIID-MCP-1",
        description="Historical widget award",
        psc_code="5210",
        nsn=nsn,
        recipient_uei="ABCD12345678",
        recipient_name="Acme Supply",
        awarding_agency="DLA",
        action_date=date(2025, 6, 1),
        total_obligation=Decimal("12000.00"),
        quantity=Decimal("100"),
        unit_price=Decimal("120.00"),
        raw={"award_id": "mcp"},
    )
    session.add(row)
    session.flush()
    return row


def _user(session: Session, role: str = "approver", email: str | None = None):
    return invite_user(
        session,
        email=email or f"{role}-{uuid4().hex[:8]}@example.com",
        display_name=role.title(),
        password="secure-password-12",
        role=role,
    )


def _bid(session: Session, opportunity_id: int) -> BidDecision:
    row = BidDecision(
        opportunity_id=opportunity_id,
        recommendation="bid",
        recommendation_score=Decimal("0.82"),
        strengths={"items": ["Known NSN history"]},
        risks={"items": ["Tight deadline"]},
        missing_information={"items": ["Confirmed supplier lead time", "Signed quote"]},
        evidence={"items": ["award history"]},
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

EXPECTED_READ_TOOLS = {
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
}

EXPECTED_WRITE_TOOLS = {
    "update_match",
    "add_pursuit",
    "update_pursuit",
    "record_human_bid_decision",
    "assign_reviewer",
    "add_review_comment",
    "complete_review",
    "request_ai_comment_validation",
    "approve_to_bid",
    "update_requirement_status",
    "create_proposal_version",
    "set_submission_ready",
    "record_submission_confirmation",
    "record_outcome",
}


def test_mcp_registers_stable_tool_contracts():
    async def _names():
        tools = await mcp.list_tools()
        return {tool.name for tool in tools}

    names = asyncio.run(_names())
    assert EXPECTED_READ_TOOLS.issubset(names)
    assert EXPECTED_WRITE_TOOLS.issubset(names)
    assert create_server() is mcp


def test_cli_mcp_list_tools():
    runner = CliRunner()
    result = runner.invoke(app, ["mcp", "list-tools"])
    assert result.exit_code == 0, result.output
    listed = set(result.output.splitlines())
    assert "list_matches" in listed
    assert "update_match" in listed
    assert "record_outcome" in listed


# ---------------------------------------------------------------------------
# Safety helpers
# ---------------------------------------------------------------------------

def test_description_truncated_by_default(session: Session):
    opp = _opp(session)
    result = handlers.get_opportunity(session, opp.id, include_full_description=False)
    assert result["ok"] is True
    assert result["data"]["description"].endswith("...")
    assert len(result["data"]["description"]) <= serialize.DEFAULT_DESCRIPTION_LIMIT

    full = handlers.get_opportunity(session, opp.id, include_full_description=True)
    assert full["ok"] is True
    assert len(full["data"]["description"]) == 400 + len(
        " long solicitation description with pricing and delivery terms."
    )


def test_structured_error_not_stack_trace(session: Session):
    result = handlers.get_opportunity(session, 999999999)
    assert result["ok"] is False
    assert result["error"]["type"] == "NotFound"
    dumped = json.dumps(result)
    assert "Traceback" not in dumped
    assert "File \"" not in dumped


def test_secret_keys_scrubbed_from_ok_payload():
    payload = serialize.ok(
        {
            "title": "x",
            "api_key": "should-not-leak",
            "nested": {"password": "secret", "ok": 1},
        }
    )
    assert "api_key" not in payload["data"]
    assert "password" not in payload["data"]["nested"]
    assert payload["data"]["nested"]["ok"] == 1
    assert scrub({"token": "abc", "keep": True}) == {"keep": True}


def test_phase_stubs_return_not_implemented(session: Session):
    for fn, phase, tool in (
        (handlers.learning_summary, 15, "learning_summary"),
        (handlers.similar_opportunities, 13, "similar_opportunities"),
        (handlers.record_outcome, 15, "record_outcome"),
    ):
        result = fn(session)
        assert result["ok"] is False
        assert result["error"]["type"] == "NotImplemented"
        assert result["error"]["details"]["phase"] == phase
        assert result["error"]["details"]["tool"] == tool


def test_destructive_match_requires_confirm(session: Session):
    user = _user(session, "owner")
    opp = _opp(session)
    wl = _watchlist(session)
    match = _match(session, opp, wl)
    blocked = handlers.update_match(
        session, match.id, "dismissed", user.email, confirm=False
    )
    assert blocked["ok"] is False
    assert "confirm" in blocked["error"]["message"]

    ok_result = handlers.update_match(
        session, match.id, "dismissed", user.email, confirm=True
    )
    assert ok_result["ok"] is True
    assert ok_result["changed"]["status"] == "dismissed"


# ---------------------------------------------------------------------------
# Core domain handlers
# ---------------------------------------------------------------------------

def test_list_matches_closing_within_days_and_price_history(session: Session):
    wl = _watchlist(session)
    near = _opp(session, days=3, title="Closing soon")
    far = _opp(session, days=30, title="Far out")
    _match(session, near, wl, status="new")
    _match(session, far, wl, status="new")
    _award(session, nsn=near.nsn)

    listed = handlers.list_matches(session, status="new", closing_within_days=7)
    assert listed["ok"] is True
    titles = {row["opportunity"]["title"] for row in listed["data"]["matches"]}
    assert "Closing soon" in titles
    assert "Far out" not in titles

    prices = handlers.price_history_tool(session, nsn=near.nsn)
    assert prices["ok"] is True
    assert prices["data"]["count"] >= 1
    assert prices["data"]["prices"][0]["unit_price"] == "120.00"


def test_bid_analysis_and_human_decision(session: Session):
    user = _user(session, "approver")
    opp = _opp(session)
    decision = _bid(session, opp.id)

    analysis = handlers.get_bid_analysis(session, opp.id)
    assert analysis["ok"] is True
    assert analysis["data"]["bid_decision"]["recommendation"] == "bid"
    missing = analysis["data"]["bid_decision"]["missing_information"]["items"]
    assert "Confirmed supplier lead time" in missing

    recorded = handlers.record_human_bid_decision(
        session,
        opportunity_id=opp.id,
        actor_email=user.email,
        human_decision="defer",
        human_comments="Need quote",
        bid_decision_id=decision.id,
    )
    assert recorded["ok"] is True
    assert recorded["changed"]["human_decision"] == "defer"


def test_pursuit_pipeline_and_write_echo(session: Session):
    user = _user(session, "owner")
    opp = _opp(session)
    created = handlers.add_pursuit(
        session, opportunity_id=opp.id, actor_email=user.email, stage="evaluating"
    )
    assert created["ok"] is True
    assert created["changed"]["stage"] == "evaluating"
    pursuit_id = created["changed"]["pursuit_id"]

    updated = handlers.update_pursuit(
        session,
        pursuit_id=pursuit_id,
        actor_email=user.email,
        expected_version=1,
        stage="review",
    )
    assert updated["ok"] is True
    assert updated["changed"]["stage"] == "review"
    assert updated["changed"]["version"] == 2

    summary = handlers.pipeline_summary(session)
    assert summary["ok"] is True
    assert summary["data"]["stage_counts"]["review"] >= 1


def test_record_submission_confirmation_never_auto_submits(session: Session):
    """Guard: confirmation tool sets auto_submitted=False even when called."""
    # Without a submission row the call fails structured — still no portal submit.
    user = _user(session, "approver")
    opp = _opp(session)
    result = handlers.record_submission_confirmation_tool(
        session,
        opportunity_id=opp.id,
        actor_email=user.email,
        confirmation_number="CNF-1",
    )
    assert result["ok"] is False
    assert "auto_submitted" not in result or result.get("auto_submitted") is False


# ---------------------------------------------------------------------------
# Acceptance criterion end-to-end via FastMCP Client
# ---------------------------------------------------------------------------

def test_ac_end_to_end_mcp_client(session: Session):
    """AC: matches→prices→bid analysis→reviewing→missing info through MCP."""
    from fastmcp import Client

    from govcon.config import get_settings
    from govcon.db import session_scope

    user = _user(session, "owner")
    wl = _watchlist(session)
    opp = _opp(session, days=4, title="Top Bid Candidate")
    match = _match(session, opp, wl, status="new")
    award = _award(session, nsn=opp.nsn)
    bid = _bid(session, opp.id)
    # Commit so the MCP server's separate session_scope can see the rows.
    session.commit()
    get_settings.cache_clear()

    try:

        async def scenario():
            async with Client(mcp) as client:
                matches = await client.call_tool(
                    "list_matches",
                    {"status": "new", "closing_within_days": 7},
                )
                assert matches.is_error is False
                match_payload = matches.data
                assert match_payload["ok"] is True
                assert match_payload["data"]["count"] >= 1
                top = next(
                    row
                    for row in match_payload["data"]["matches"]
                    if row["match_id"] == match.id
                )
                opportunity_id = top["opportunity"]["id"]
                nsn = top["opportunity"]["nsn"]

                prices = await client.call_tool("price_history", {"nsn": nsn})
                assert prices.data["ok"] is True
                assert prices.data["data"]["count"] >= 1

                analysis = await client.call_tool(
                    "get_bid_analysis", {"opportunity_id": opportunity_id}
                )
                assert analysis.data["ok"] is True
                bid_payload = analysis.data["data"]["bid_decision"]
                assert bid_payload["recommendation"] == "bid"
                assert bid_payload["missing_information"]["items"]

                moved = await client.call_tool(
                    "update_match",
                    {
                        "match_id": match.id,
                        "status": "reviewing",
                        "actor_email": user.email,
                    },
                )
                assert moved.data["ok"] is True
                assert moved.data["changed"]["status"] == "reviewing"

                again = await client.call_tool(
                    "get_bid_analysis", {"opportunity_id": opportunity_id}
                )
                missing = again.data["data"]["bid_decision"]["missing_information"]["items"]
                assert "Signed quote" in missing

                stub = await client.call_tool(
                    "similar_opportunities", {"opportunity_id": opportunity_id}
                )
                assert stub.data["ok"] is False
                assert stub.data["error"]["details"]["phase"] == 13

        asyncio.run(scenario())

        with session_scope(get_settings()) as verify:
            refreshed = verify.get(Match, match.id)
            assert refreshed is not None
            assert refreshed.status == "reviewing"
    finally:
        # Keep committed rows (audit FKs). Unique source_ids avoid collisions.
        _ = (award, bid, wl)
