"""The one list of ``ai_analyses.analysis_type`` values.

Producers write these and consumers (web, MCP, decision engine) read them, so
a renamed or misspelled type fails at import instead of silently showing
nothing. Values are plain strings in the database.
"""

from __future__ import annotations

from enum import StrEnum


class AnalysisType(StrEnum):
    SOLICITATION_SUMMARY = "solicitation_summary"
    DECISION_PACKAGE = "decision_package"
    MARKET = "market_analysis"
    SOURCING = "sourcing_analysis"
    PRICING = "pricing_analysis"
    COMPLIANCE_REVIEW = "compliance_review"
    AMENDMENT = "amendment_analysis"
    RED_TEAM_REVIEW = "red_team_review"
    PROPOSAL_COVERAGE = "proposal_coverage"
    PROPOSAL_DRAFTING = "proposal_drafting"
    PROPOSAL_RED_TEAM = "proposal_red_team"
    SUBMISSION_PREFLIGHT = "submission_preflight"
    REVIEWER_COMMENT_VALIDATION = "reviewer_comment_validation"
    CONSOLIDATED_REVIEW = "consolidated_review"
    OUTCOME = "outcome_analysis"
