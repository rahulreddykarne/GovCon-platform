"""FastMCP server exposing GovCon capabilities over stdio (Phase 12)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy.orm import Session

from govcon.config import get_settings
from govcon.db import session_scope
from govcon.logging import configure_logging
from govcon.mcp import handlers

try:
    from fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
    raise ImportError(
        "fastmcp is required for Phase 12. Install with: pip install -e '.[dev]'"
    ) from exc


mcp = FastMCP(
    name="govcon",
    instructions=(
        "Local GovCon opportunity and bid assistant. "
        "Read tools return compact structured JSON; descriptions are truncated "
        "unless include_full_description=true. Write tools require actor_email "
        "and echo the changed record. Submission tools never auto-submit portals. "
        "Destructive updates require confirm=true. Secrets are never exposed."
    ),
)


@contextmanager
def _session() -> Iterator[Session]:
    with session_scope(get_settings()) as session:
        yield session


# ── Read tools ──


@mcp.tool
def search_opportunities(
    query: str | None = None,
    source: str | None = None,
    status: str | None = None,
    psc_prefix: str | None = None,
    naics_prefix: str | None = None,
    nsn: str | None = None,
    closing_within_days: int | None = None,
    limit: int = 25,
    include_full_description: bool = False,
) -> dict[str, Any]:
    """Search stored opportunities. Descriptions are truncated unless requested."""
    with _session() as session:
        return handlers.search_opportunities(
            session,
            query=query,
            source=source,
            status=status,
            psc_prefix=psc_prefix,
            naics_prefix=naics_prefix,
            nsn=nsn,
            closing_within_days=closing_within_days,
            limit=limit,
            include_full_description=include_full_description,
        )


@mcp.tool
def get_opportunity(
    opportunity_id: int,
    include_full_description: bool = False,
) -> dict[str, Any]:
    """Get one opportunity by id."""
    with _session() as session:
        return handlers.get_opportunity(
            session, opportunity_id, include_full_description=include_full_description
        )


@mcp.tool
def get_opportunity_history(opportunity_id: int, limit: int = 50) -> dict[str, Any]:
    """Return snapshot/event history for an opportunity."""
    with _session() as session:
        return handlers.get_opportunity_history(session, opportunity_id, limit=limit)


@mcp.tool
def price_history(
    nsn: str | None = None,
    psc: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return historical award price points for an NSN and/or PSC prefix."""
    with _session() as session:
        return handlers.price_history_tool(session, nsn=nsn, psc=psc, limit=limit)


@mcp.tool
def vendor_profile(uei: str, refresh: bool = False) -> dict[str, Any]:
    """Return a SAM vendor profile plus award stats for a UEI."""
    with _session() as session:
        return handlers.vendor_profile_tool(session, uei, refresh=refresh)


@mcp.tool
def competitor_summary(opportunity_id: int, limit: int = 5) -> dict[str, Any]:
    """Summarize likely competitors for an opportunity from award history."""
    with _session() as session:
        return handlers.competitor_summary_tool(session, opportunity_id, limit=limit)


@mcp.tool
def list_matches(
    status: str | None = None,
    watchlist_id: int | None = None,
    closing_within_days: int | None = None,
    limit: int = 50,
    include_full_description: bool = False,
) -> dict[str, Any]:
    """List watchlist matches, optionally filtered by status and deadline window."""
    with _session() as session:
        return handlers.list_matches(
            session,
            status=status,
            watchlist_id=watchlist_id,
            closing_within_days=closing_within_days,
            limit=limit,
            include_full_description=include_full_description,
        )


@mcp.tool
def pipeline_summary() -> dict[str, Any]:
    """Summarize pursuit pipeline stage counts and recent pursuits."""
    with _session() as session:
        return handlers.pipeline_summary(session)


@mcp.tool
def get_bid_analysis(opportunity_id: int) -> dict[str, Any]:
    """Return the latest stored bid decision / analysis for an opportunity."""
    with _session() as session:
        return handlers.get_bid_analysis(session, opportunity_id)


@mcp.tool
def get_compliance_matrix(
    opportunity_id: int,
    filters: list[str] | None = None,
) -> dict[str, Any]:
    """Return the compliance matrix rows for an opportunity."""
    with _session() as session:
        return handlers.get_compliance_matrix(session, opportunity_id, filters=filters)


@mcp.tool
def get_proposal(opportunity_id: int) -> dict[str, Any]:
    """Return the proposal workspace for an opportunity."""
    with _session() as session:
        return handlers.get_proposal(session, opportunity_id)


@mcp.tool
def submission_status(opportunity_id: int) -> dict[str, Any]:
    """Return submission package readiness/status for an opportunity."""
    with _session() as session:
        return handlers.submission_status(session, opportunity_id)


@mcp.tool
def learning_summary() -> dict[str, Any]:
    """Outcome learning summary (Phase 15)."""
    with _session() as session:
        return handlers.learning_summary(session)


@mcp.tool
def similar_opportunities(opportunity_id: int | None = None) -> dict[str, Any]:
    """Semantic similar opportunities (Phase 13)."""
    with _session() as session:
        return handlers.similar_opportunities(session, opportunity_id=opportunity_id)


# ── Write tools ──


@mcp.tool
def update_match(
    match_id: int,
    status: str,
    actor_email: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Update a match status. Dismissed requires confirm=true."""
    with _session() as session:
        return handlers.update_match(
            session,
            match_id=match_id,
            status=status,
            actor_email=actor_email,
            confirm=confirm,
        )


@mcp.tool
def add_pursuit(
    opportunity_id: int,
    actor_email: str,
    stage: str = "evaluating",
    notes: str | None = None,
    sourcing_cost: str | None = None,
    quote_price: str | None = None,
    supplier: str | None = None,
) -> dict[str, Any]:
    """Create a pursuit for an opportunity."""
    with _session() as session:
        return handlers.add_pursuit(
            session,
            opportunity_id=opportunity_id,
            actor_email=actor_email,
            stage=stage,
            notes=notes,
            sourcing_cost=sourcing_cost,
            quote_price=quote_price,
            supplier=supplier,
        )


@mcp.tool
def update_pursuit(
    pursuit_id: int,
    actor_email: str,
    expected_version: int,
    stage: str | None = None,
    notes: str | None = None,
    sourcing_cost: str | None = None,
    quote_price: str | None = None,
    supplier: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Update a pursuit. Terminal stages require confirm=true."""
    with _session() as session:
        return handlers.update_pursuit(
            session,
            pursuit_id=pursuit_id,
            actor_email=actor_email,
            expected_version=expected_version,
            stage=stage,
            notes=notes,
            sourcing_cost=sourcing_cost,
            quote_price=quote_price,
            supplier=supplier,
            confirm=confirm,
        )


@mcp.tool
def record_human_bid_decision(
    opportunity_id: int,
    actor_email: str,
    human_decision: str,
    human_comments: str | None = None,
    bid_decision_id: int | None = None,
) -> dict[str, Any]:
    """Record a human decision on the latest (or specified) bid decision."""
    with _session() as session:
        return handlers.record_human_bid_decision(
            session,
            opportunity_id=opportunity_id,
            actor_email=actor_email,
            human_decision=human_decision,
            human_comments=human_comments,
            bid_decision_id=bid_decision_id,
        )


@mcp.tool
def assign_reviewer(
    opportunity_id: int,
    user_id: int,
    actor_email: str | None = None,
    assignment_role: str = "reviewer",
) -> dict[str, Any]:
    """Assign a reviewer to an opportunity."""
    with _session() as session:
        return handlers.assign_reviewer_tool(
            session,
            opportunity_id=opportunity_id,
            user_id=user_id,
            actor_email=actor_email,
            assignment_role=assignment_role,
        )


@mcp.tool
def add_review_comment(
    opportunity_id: int,
    actor_email: str,
    body: str,
    topic: str | None = None,
    parent_comment_id: int | None = None,
    user_recommendation: str | None = None,
    validate_with_ai: bool = False,
) -> dict[str, Any]:
    """Add a review comment. AI validation is off by default for MCP calls."""
    with _session() as session:
        return handlers.add_review_comment(
            session,
            opportunity_id=opportunity_id,
            actor_email=actor_email,
            body=body,
            topic=topic,
            parent_comment_id=parent_comment_id,
            user_recommendation=user_recommendation,
            validate_with_ai=validate_with_ai,
        )


@mcp.tool
def complete_review(
    opportunity_id: int,
    actor_email: str,
    action: str,
    recommendation: str | None = None,
    agree_with_ai_assessment: bool | None = None,
    second_review_reason: str | None = None,
) -> dict[str, Any]:
    """Complete the actor's review assignment for an opportunity."""
    with _session() as session:
        return handlers.complete_review(
            session,
            opportunity_id=opportunity_id,
            actor_email=actor_email,
            action=action,
            recommendation=recommendation,
            agree_with_ai_assessment=agree_with_ai_assessment,
            second_review_reason=second_review_reason,
        )


@mcp.tool
def request_ai_comment_validation(
    comment_id: int,
    actor_email: str | None = None,
) -> dict[str, Any]:
    """Run AI validation on an existing review comment."""
    with _session() as session:
        return handlers.request_ai_comment_validation(
            session, comment_id=comment_id, actor_email=actor_email
        )


@mcp.tool
def approve_to_bid(
    opportunity_id: int,
    actor_email: str,
    expected_version: int | None = None,
    override_reason: str | None = None,
) -> dict[str, Any]:
    """Finalize review approval to bid for an opportunity."""
    with _session() as session:
        return handlers.approve_to_bid(
            session,
            opportunity_id=opportunity_id,
            actor_email=actor_email,
            expected_version=expected_version,
            override_reason=override_reason,
        )


@mcp.tool
def update_requirement_status(
    requirement_id: int,
    status: str,
    actor_email: str,
    reason: str,
    expected_version: int,
    acknowledge_deterministic_failure: bool = False,
) -> dict[str, Any]:
    """Authorized human override of a compliance requirement status."""
    with _session() as session:
        return handlers.update_requirement_status(
            session,
            requirement_id=requirement_id,
            status=status,
            actor_email=actor_email,
            reason=reason,
            expected_version=expected_version,
            acknowledge_deterministic_failure=acknowledge_deterministic_failure,
        )


@mcp.tool
def create_proposal_version(
    opportunity_id: int,
    actor_email: str | None = None,
    skip_ai: bool = True,
) -> dict[str, Any]:
    """Create a new proposal version for an approved-to-bid opportunity."""
    with _session() as session:
        return handlers.create_proposal_version(
            session,
            opportunity_id=opportunity_id,
            actor_email=actor_email,
            skip_ai=skip_ai,
        )


@mcp.tool
def set_submission_ready(
    opportunity_id: int,
    actor_email: str,
    override_reason: str | None = None,
) -> dict[str, Any]:
    """Move the pursuit to ready_to_submit when pre-flight allows it."""
    with _session() as session:
        return handlers.set_submission_ready(
            session,
            opportunity_id=opportunity_id,
            actor_email=actor_email,
            override_reason=override_reason,
        )


@mcp.tool
def record_submission_confirmation(
    opportunity_id: int,
    actor_email: str,
    confirmation_number: str | None = None,
    confirmation_notes: str | None = None,
) -> dict[str, Any]:
    """Record human portal submission confirmation. Never auto-submits."""
    with _session() as session:
        return handlers.record_submission_confirmation_tool(
            session,
            opportunity_id=opportunity_id,
            actor_email=actor_email,
            confirmation_number=confirmation_number,
            confirmation_notes=confirmation_notes,
        )


@mcp.tool
def record_outcome(
    opportunity_id: int | None = None,
    outcome: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Record win/loss/no-bid outcome (Phase 15)."""
    with _session() as session:
        return handlers.record_outcome(
            session, opportunity_id=opportunity_id, outcome=outcome, notes=notes
        )


def create_server() -> FastMCP:
    """Return the configured FastMCP instance."""
    return mcp


def run_stdio() -> None:
    """Run the MCP server on stdio (default FastMCP transport)."""
    configure_logging(get_settings())
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_stdio()
