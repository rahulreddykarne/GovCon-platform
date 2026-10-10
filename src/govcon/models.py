"""SQLAlchemy models for the master-spec schema.

Illustrative DDL in MASTER_SPEC_v2.5 §5 is the column source. Mutable tables also
carry ``created_at`` and ``updated_at``. Shared records that must not be
last-write-wins carry ``version`` for optimistic concurrency. Authentication,
audit, in-app notifications, and JEV ``decision_runs`` are included so later
phases extend one schema.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from govcon.db import Base


def _ts() -> DateTime:
    return DateTime(timezone=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=text("now()"))


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=text("now()"))


class VersionMixin:
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class Opportunity(TimestampMixin, Base):
    __tablename__ = "opportunities"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_opportunities_source_source_id"),
        Index("ix_opportunities_psc_code", "psc_code"),
        Index("ix_opportunities_naics_code", "naics_code"),
        Index("ix_opportunities_response_deadline", "response_deadline"),
        Index("ix_opportunities_status", "status"),
        Index("ix_opportunities_nsn", "nsn"),
        Index(
            "opportunities_fts",
            text("to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, ''))"),
            postgresql_using="gin",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    solicitation_number: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    opportunity_type: Mapped[str | None] = mapped_column(Text)
    psc_code: Mapped[str | None] = mapped_column(Text)
    naics_code: Mapped[str | None] = mapped_column(Text)
    set_aside_code: Mapped[str | None] = mapped_column(Text)
    agency_path: Mapped[str | None] = mapped_column(Text)
    place_of_performance: Mapped[dict | None] = mapped_column(JSONB)
    nsn: Mapped[str | None] = mapped_column(Text)
    # Every NSN parsed from the source; ``nsn`` is the first of them.
    nsn_candidates: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric)
    unit: Mapped[str | None] = mapped_column(Text)
    estimated_value_min: Mapped[Decimal | None] = mapped_column(Numeric)
    estimated_value_max: Mapped[Decimal | None] = mapped_column(Numeric)
    estimated_value_source: Mapped[str | None] = mapped_column(Text)
    posted_date: Mapped[date | None] = mapped_column(Date)
    response_deadline: Mapped[datetime | None] = mapped_column(_ts())
    archive_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    poc: Mapped[dict | list | None] = mapped_column(JSONB)
    links: Mapped[dict | None] = mapped_column(JSONB)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    raw_hash: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384))
    embedding_model: Mapped[str | None] = mapped_column(Text)
    embedding_dimension: Mapped[int | None] = mapped_column(Integer)
    embedding_source_hash: Mapped[str | None] = mapped_column(Text)
    # Per-opportunity override of AI_MAX_INPUT_TOKENS_PER_OPPORTUNITY. NULL uses the env default.
    ai_max_input_tokens: Mapped[int | None] = mapped_column(Integer)


class OpportunitySnapshot(Base):
    __tablename__ = "opportunity_snapshots"
    __table_args__ = (
        UniqueConstraint("opportunity_id", "content_hash", name="uq_opportunity_snapshots_opportunity_hash"),
        Index("ix_opportunity_snapshots_opportunity_fetched", "opportunity_id", "fetched_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    source_version: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=text("now()"))
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    normalized: Mapped[dict | None] = mapped_column(JSONB)


class OpportunityEvent(Base):
    __tablename__ = "opportunity_events"
    __table_args__ = (Index("ix_opportunity_events_opportunity_detected", "opportunity_id", "detected_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    field_name: Mapped[str | None] = mapped_column(Text)
    old_value: Mapped[dict | None] = mapped_column(JSONB)
    new_value: Mapped[dict | None] = mapped_column(JSONB)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("opportunity_snapshots.id"))
    detected_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=text("now()"))


class Award(TimestampMixin, Base):
    __tablename__ = "awards"
    __table_args__ = (
        UniqueConstraint("source", "award_id", name="uq_awards_source_award_id"),
        Index("ix_awards_psc_code", "psc_code"),
        Index("ix_awards_nsn", "nsn"),
        Index("ix_awards_recipient_uei", "recipient_uei"),
        Index("ix_awards_action_date", "action_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'usaspending'"))
    award_id: Mapped[str] = mapped_column(Text, nullable=False)
    piid: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    psc_code: Mapped[str | None] = mapped_column(Text)
    naics_code: Mapped[str | None] = mapped_column(Text)
    nsn: Mapped[str | None] = mapped_column(Text)
    recipient_uei: Mapped[str | None] = mapped_column(Text)
    recipient_name: Mapped[str | None] = mapped_column(Text)
    awarding_agency: Mapped[str | None] = mapped_column(Text)
    action_date: Mapped[date | None] = mapped_column(Date)
    total_obligation: Mapped[Decimal | None] = mapped_column(Numeric)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric)
    set_aside_code: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)


class Vendor(TimestampMixin, Base):
    __tablename__ = "vendors"
    __table_args__ = (
        CheckConstraint(
            "freshness_status IN ('fresh', 'stale', 'expired', 'failed', 'unknown')",
            name="ck_vendors_freshness_status",
        ),
        CheckConstraint(
            "attempt_state IS NULL OR attempt_state IN ('in_progress', 'cancelled', 'applied', 'failed')",
            name="ck_vendors_attempt_state",
        ),
    )

    uei: Mapped[str] = mapped_column(Text, primary_key=True)
    cage_code: Mapped[str | None] = mapped_column(Text)
    legal_name: Mapped[str | None] = mapped_column(Text)
    dba_name: Mapped[str | None] = mapped_column(Text)
    registration_status: Mapped[str | None] = mapped_column(Text)
    physical_address: Mapped[dict | None] = mapped_column(JSONB)
    business_types: Mapped[dict | None] = mapped_column(JSONB)
    naics_codes: Mapped[list[dict] | None] = mapped_column(JSONB)
    psc_codes: Mapped[list[dict] | None] = mapped_column(JSONB)
    points_of_contact: Mapped[dict | list | None] = mapped_column(JSONB)
    raw: Mapped[dict | None] = mapped_column(JSONB)
    fetched_at: Mapped[datetime | None] = mapped_column(_ts())
    source_updated_at: Mapped[datetime | None] = mapped_column(_ts())
    expires_at: Mapped[date | None] = mapped_column(Date)
    freshness_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'unknown'"), default="unknown"
    )
    last_refresh_error: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(_ts())
    refresh_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    attempt_id: Mapped[str | None] = mapped_column(Text)
    attempt_state: Mapped[str | None] = mapped_column(Text)
    registration_key: Mapped[str | None] = mapped_column(Text)


class Contact(TimestampMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("email", "agency_path", name="uq_contacts_email_agency_path"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    agency_path: Mapped[str | None] = mapped_column(Text)
    contact_type: Mapped[str | None] = mapped_column(Text)
    first_seen_opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id"))


class Watchlist(TimestampMixin, Base):
    __tablename__ = "watchlists"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    psc_codes: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    naics_codes: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    keywords: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    exclude_keywords: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    nsn_list: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    set_asides: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    max_value: Mapped[Decimal | None] = mapped_column(Numeric)
    min_value: Mapped[Decimal | None] = mapped_column(Numeric)
    min_deadline_days: Mapped[int | None] = mapped_column(Integer)
    sources: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    notes: Mapped[str | None] = mapped_column(Text)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(_ts())
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384))
    embedding_updated_at: Mapped[datetime | None] = mapped_column(_ts())
    embedding_model: Mapped[str | None] = mapped_column(Text)
    embedding_dimension: Mapped[int | None] = mapped_column(Integer)
    embedding_source_hash: Mapped[str | None] = mapped_column(Text)


class Match(TimestampMixin, Base):
    __tablename__ = "matches"
    __table_args__ = (
        UniqueConstraint("opportunity_id", "watchlist_id", name="uq_matches_opportunity_watchlist"),
        CheckConstraint(
            "status IN ('new', 'seen', 'dismissed', 'reviewing', 'pursuing')",
            name="ck_matches_status",
        ),
        Index("ix_matches_watchlist_active", "watchlist_id", "active"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    watchlist_id: Mapped[int] = mapped_column(ForeignKey("watchlists.id"), nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric)
    matched_on: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'new'"))
    alerted_at: Mapped[datetime | None] = mapped_column(_ts())
    # False once the watchlist no longer produces this match (criteria changed,
    # opportunity closed, watchlist disabled). Kept for history, never deleted.
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), default=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(_ts())
    inactive_reason: Mapped[str | None] = mapped_column(Text)
    # Explainable 0-100 rank (ADR-069); ``score`` stays the rule-hit count.
    rank_score: Mapped[Decimal | None] = mapped_column(Numeric)
    rank_factors: Mapped[list | None] = mapped_column(JSONB)
    ranked_at: Mapped[datetime | None] = mapped_column(_ts())


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        CheckConstraint(
            "role IN ('owner', 'approver', 'reviewer', 'read_only')",
            name="ck_users_role",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'reviewer'"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class UserSession(TimestampMixin, Base):
    __tablename__ = "user_sessions"
    __table_args__ = (Index("ix_user_sessions_user_id", "user_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(_ts(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(_ts())
    last_seen_at: Mapped[datetime | None] = mapped_column(_ts())


class AuditEvent(CreatedAtMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_opportunity_created", "opportunity_id", "created_at"),
        Index("ix_audit_events_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id"))
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    old_value: Mapped[dict | None] = mapped_column(JSONB)
    new_value: Mapped[dict | None] = mapped_column(JSONB)


class Notification(TimestampMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_created", "user_id", "created_at"),
        CheckConstraint(
            "notification_type IN ("
            "'review_assigned', 'reviewer_completed', 'new_reviewer_comment', "
            "'ai_flagged_comment_needs_evidence', 'review_quorum_satisfied', "
            "'second_review_required', 'second_review_requested', 'review_reassigned', "
            "'approval_pending', 'material_amendment_after_review', "
            "'proposal_package_generated', 'submission_ready', "
            "'review_reminder', 'review_overdue_escalation', 'deadline_escalation', 'auto_pursued', "
            "'registration_expiring', 'outcome_suggested'"
            ")",
            name="ck_notifications_type",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id"))
    notification_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    read_at: Mapped[datetime | None] = mapped_column(_ts())
    # Explicit acknowledgement of an action-required notification (ADR-070).
    acknowledged_at: Mapped[datetime | None] = mapped_column(_ts())


class ReviewAssignment(TimestampMixin, Base):
    __tablename__ = "review_assignments"
    __table_args__ = (
        UniqueConstraint("opportunity_id", "user_id", name="uq_review_assignments_opportunity_user"),
        CheckConstraint(
            "status IN ('assigned', 'in_progress', 'complete', 'reopened')",
            name="ck_review_assignments_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    assignment_role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'reviewer'"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'assigned'"))
    recommendation: Mapped[str | None] = mapped_column(Text)
    agree_with_ai_assessment: Mapped[bool | None] = mapped_column(Boolean)
    second_review_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    second_review_request_reason: Mapped[str | None] = mapped_column(Text)
    assigned_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=text("now()"))
    started_at: Mapped[datetime | None] = mapped_column(_ts())
    completed_at: Mapped[datetime | None] = mapped_column(_ts())
    reopened_at: Mapped[datetime | None] = mapped_column(_ts())
    last_reminded_at: Mapped[datetime | None] = mapped_column(_ts())
    escalated_at: Mapped[datetime | None] = mapped_column(_ts())


class ReviewComment(TimestampMixin, Base):
    __tablename__ = "review_comments"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    parent_comment_id: Mapped[int | None] = mapped_column(ForeignKey("review_comments.id"))
    topic: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    source_refs: Mapped[dict | None] = mapped_column(JSONB)
    user_recommendation: Mapped[str | None] = mapped_column(Text)
    ai_position: Mapped[str | None] = mapped_column(Text)
    ai_confidence: Mapped[str | None] = mapped_column(Text)
    ai_reason: Mapped[str | None] = mapped_column(Text)
    ai_supporting_evidence: Mapped[list[dict] | None] = mapped_column(JSONB)
    ai_contradicting_evidence: Mapped[list[dict] | None] = mapped_column(JSONB)
    ai_missing_information: Mapped[list[str] | None] = mapped_column(JSONB)
    ai_suggested_action: Mapped[str | None] = mapped_column(Text)


class AIAnalysis(CreatedAtMixin, Base):
    __tablename__ = "ai_analyses"
    __table_args__ = (
        Index("ix_ai_analyses_opportunity_type_created", "opportunity_id", "analysis_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    analysis_type: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_name: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    prompt_hash: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[str | None] = mapped_column(Text)
    generation_settings: Mapped[dict | None] = mapped_column(JSONB)
    input_snapshot_hash: Mapped[str | None] = mapped_column(Text)
    context_manifest: Mapped[dict | None] = mapped_column(JSONB)
    output_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_refs: Mapped[dict | None] = mapped_column(JSONB)
    token_usage: Mapped[dict | None] = mapped_column(JSONB)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric)
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class ReviewSession(TimestampMixin, VersionMixin, Base):
    __tablename__ = "review_sessions"
    __table_args__ = (
        UniqueConstraint("opportunity_id", name="uq_review_sessions_opportunity"),
        CheckConstraint(
            "review_policy IN ('single', 'dual', 'conditional')",
            name="ck_review_sessions_policy",
        ),
        CheckConstraint(
            "status IN ("
            "'pending', 'ready_for_review', 'under_review', 'review_complete', "
            "'approval_pending', 'approved_to_bid', 'returned_for_review', 'no_bid'"
            ")",
            name="ck_review_sessions_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    ai_decision_package_id: Mapped[int | None] = mapped_column(ForeignKey("ai_analyses.id"))
    review_policy: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'conditional'"))
    required_review_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    completed_review_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    second_review_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    second_review_reason: Mapped[str | None] = mapped_column(Text)
    reviewer_summary: Mapped[dict | None] = mapped_column(JSONB)
    ai_consolidated_review: Mapped[dict | None] = mapped_column(JSONB)
    final_approval_status: Mapped[str | None] = mapped_column(Text)
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(_ts())
    override_used: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    override_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    override_reason: Mapped[str | None] = mapped_column(Text)
    override_at: Mapped[datetime | None] = mapped_column(_ts())


class Pursuit(TimestampMixin, VersionMixin, Base):
    __tablename__ = "pursuits"
    __table_args__ = (
        UniqueConstraint("opportunity_id", name="uq_pursuits_opportunity"),
        CheckConstraint(
            "stage IN ("
            "'evaluating', 'bid_approved', 'sourcing', 'drafting', 'review', "
            "'ready_to_submit', 'submitted', 'won', 'lost', 'cancelled', 'no_bid'"
            ")",
            name="ck_pursuits_stage",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'evaluating'"))
    sourcing_cost: Mapped[Decimal | None] = mapped_column(Numeric)
    quote_price: Mapped[Decimal | None] = mapped_column(Numeric)
    margin_pct: Mapped[Decimal | None] = mapped_column(
        Numeric,
        Computed(
            "CASE WHEN sourcing_cost > 0 "
            "THEN round((quote_price - sourcing_cost) / sourcing_cost * 100, 1) "
            "ELSE NULL END",
            persisted=True,
        ),
    )
    supplier: Mapped[str | None] = mapped_column(Text)
    approved_to_bid_at: Mapped[datetime | None] = mapped_column(_ts())
    submitted_at: Mapped[datetime | None] = mapped_column(_ts())
    outcome_at: Mapped[datetime | None] = mapped_column(_ts())
    outcome_notes: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)


class StoredFile(TimestampMixin, Base):
    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("opportunity_id", "url", "sha256", name="uq_files_opportunity_url_sha256"),
        Index("ix_files_opportunity_active", "opportunity_id", "active"),
        CheckConstraint("classification IN ('PUBLIC','PROPRIETARY','FCI','CUI','UNKNOWN','SECRET_CREDENTIAL')", name="ck_files_classification"),
        CheckConstraint("length(trim(source_origin)) BETWEEN 1 AND 200", name="ck_files_source_origin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("opportunity_snapshots.id"))
    filename: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[str] = mapped_column(Text, nullable=False, default="UNKNOWN", server_default=text("'UNKNOWN'"))
    source_origin: Mapped[str] = mapped_column(Text, nullable=False, default="legacy_unknown", server_default=text("'legacy_unknown'"))
    sha256: Mapped[str | None] = mapped_column(Text)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    extraction_status: Mapped[str | None] = mapped_column(Text)
    extraction_error: Mapped[str | None] = mapped_column(Text)
    downloaded_at: Mapped[datetime | None] = mapped_column(_ts())
    # Each row is an immutable attachment version (``downloaded_at`` = latest fetch that
    # returned these bytes); ``active`` = what the current source's latest fetch produced
    # (while a refresh fails: the failure row plus the last good version).
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    removed_at: Mapped[datetime | None] = mapped_column(_ts())
    # Page-level extraction (ADR-065): pages read by OCR, and pages that stayed
    # unreadable (no text layer and OCR unavailable or empty).
    page_count: Mapped[int | None] = mapped_column(Integer)
    ocr_pages: Mapped[list | None] = mapped_column(JSONB)
    ocr_failed_pages: Mapped[list | None] = mapped_column(JSONB)


class FilePage(CreatedAtMixin, Base):
    """Text of one page (PDF) or one part (sheet, document) of a stored file (ADR-065).

    ``text_source`` is ``native`` for the file's own text layer and ``ocr``
    when Tesseract read a page image; ``ocr_confidence`` is its mean word
    confidence (0–100).
    """

    __tablename__ = "file_pages"
    __table_args__ = (
        UniqueConstraint("file_id", "page_no", name="uq_file_pages_file_page"),
        CheckConstraint("text_source IN ('native','ocr','none')", name="ck_file_pages_text_source"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    file_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    # ``text`` shadows sqlalchemy.text in this class body, so defaults are plain strings.
    text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    text_source: Mapped[str] = mapped_column(Text, nullable=False)
    ocr_confidence: Mapped[Decimal | None] = mapped_column(Numeric)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class AICallUsage(CreatedAtMixin, Base):
    """Durable reservations independent of the business transaction.

    No opportunity FK: calls may precede its commit, and rollback must never
    erase incurred usage. IDs remain unique because sequences do not roll back.
    """
    __tablename__ = "ai_call_usage"
    __table_args__ = (
        CheckConstraint("input_tokens >= 0 AND output_tokens >= 0 AND (cost_usd IS NULL OR cost_usd >= 0)", name="ck_ai_call_usage_nonnegative"),
        CheckConstraint("status IN ('reserved','succeeded','failed')", name="ck_ai_call_usage_status"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric)
    usage: Mapped[dict | None] = mapped_column(JSONB)


class AIModelPrice(TimestampMixin, Base):
    """Editable USD price per 1,000,000 tokens. Missing rows mean the price is not set.

    ``web_search_usd_per_thousand`` is the server-side web search fee per 1,000
    searches; NULL means that fee is not set for the model.
    """

    __tablename__ = "ai_model_prices"
    __table_args__ = (
        UniqueConstraint("provider", "model", name="uq_ai_model_prices_provider_model"),
        CheckConstraint(
            "input_usd_per_million >= 0 AND output_usd_per_million >= 0 "
            "AND (cached_usd_per_million IS NULL OR cached_usd_per_million >= 0) "
            "AND (cache_write_usd_per_million IS NULL OR cache_write_usd_per_million >= 0) "
            "AND (web_search_usd_per_thousand IS NULL OR web_search_usd_per_thousand >= 0)",
            name="ck_ai_model_prices_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_usd_per_million: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    output_usd_per_million: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    cached_usd_per_million: Mapped[Decimal | None] = mapped_column(Numeric)
    cache_write_usd_per_million: Mapped[Decimal | None] = mapped_column(Numeric)
    web_search_usd_per_thousand: Mapped[Decimal | None] = mapped_column(Numeric)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    effective_as_of: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)


class AIProviderCall(CreatedAtMixin, Base):
    """One external or local model attempt, with tokens only when the provider reported them.

    ``input_tokens`` is NULL when the response had no usage fields. Blocked calls
    store zero tokens and a zero cost because nothing was sent. Local work stores
    a zero cost. A NULL cost on a succeeded, failed, truncated or discarded call
    means the price is not set or the usage was not reported.
    ``web_search_requests``/``web_fetch_requests`` are the server-tool counts the
    provider reported; NULL means not reported. ``truncated`` is a call whose
    ``finish_reason`` was ``length``/``max_tokens``; ``output_rejected`` is a
    completed HTTP call whose JSON was discarded.
    """

    __tablename__ = "ai_provider_calls"
    __table_args__ = (
        CheckConstraint(
            "status IN ('succeeded', 'failed', 'blocked', 'local', 'truncated', 'output_rejected')",
            name="ck_ai_provider_calls_status",
        ),
        CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) "
            "AND (output_tokens IS NULL OR output_tokens >= 0) "
            "AND (cached_tokens IS NULL OR cached_tokens >= 0) "
            "AND (cache_write_tokens IS NULL OR cache_write_tokens >= 0) "
            "AND (web_search_requests IS NULL OR web_search_requests >= 0) "
            "AND (web_fetch_requests IS NULL OR web_fetch_requests >= 0) "
            "AND (cost_usd IS NULL OR cost_usd >= 0) "
            "AND (latency_ms IS NULL OR latency_ms >= 0)",
            name="ck_ai_provider_calls_nonnegative",
        ),
        Index("ix_ai_provider_calls_created_at", "created_at"),
        Index("ix_ai_provider_calls_opportunity_id", "opportunity_id"),
        Index("ix_ai_provider_calls_provider_model", "provider", "model"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int | None] = mapped_column(BigInteger)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cached_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger)
    web_search_requests: Mapped[int | None] = mapped_column(BigInteger)
    web_fetch_requests: Mapped[int | None] = mapped_column(BigInteger)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    analysis_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ai_analyses.id", ondelete="SET NULL", name="fk_ai_provider_calls_analysis_id")
    )
    decision_run_id: Mapped[int | None] = mapped_column(BigInteger)
    compliance_run_id: Mapped[int | None] = mapped_column(BigInteger)
    finish_reason: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric)
    price_id: Mapped[int | None] = mapped_column(ForeignKey("ai_model_prices.id"))


class PromptRegistryEntry(TimestampMixin, Base):
    __tablename__ = "prompt_registry"
    __table_args__ = (
        UniqueConstraint("prompt_name", "prompt_version", name="uq_prompt_registry_name_version"),
        Index(
            "prompt_registry_one_active",
            "prompt_name",
            unique=True,
            postgresql_where=text("active = true"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    prompt_name: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    provider_family: Mapped[str | None] = mapped_column(Text)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_hash: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str | None] = mapped_column(Text)
    output_schema: Mapped[dict | None] = mapped_column(JSONB)
    default_settings: Mapped[dict | None] = mapped_column(JSONB)
    allowed_data_classes: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class BidDecision(CreatedAtMixin, Base):
    __tablename__ = "bid_decisions"
    __table_args__ = (
        Index("ix_bid_decisions_opportunity_created", "opportunity_id", "created_at"),
        CheckConstraint(
            "recommendation IN ('bid', 'no_bid', 'review', 'insufficient_information')",
            name="ck_bid_decisions_recommendation",
        ),
        CheckConstraint(
            "human_decision IS NULL OR human_decision IN ('approve_bid', 'no_bid', 'defer')",
            name="ck_bid_decisions_human_decision",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    recommendation: Mapped[str] = mapped_column(Text, nullable=False)
    recommendation_score: Mapped[Decimal | None] = mapped_column(Numeric)
    capability_score: Mapped[Decimal | None] = mapped_column(Numeric)
    pricing_score: Mapped[Decimal | None] = mapped_column(Numeric)
    past_performance_score: Mapped[Decimal | None] = mapped_column(Numeric)
    deadline_score: Mapped[Decimal | None] = mapped_column(Numeric)
    competition_score: Mapped[Decimal | None] = mapped_column(Numeric)
    margin_score: Mapped[Decimal | None] = mapped_column(Numeric)
    compliance_risk_score: Mapped[Decimal | None] = mapped_column(Numeric)
    strengths: Mapped[dict | None] = mapped_column(JSONB)
    risks: Mapped[dict | None] = mapped_column(JSONB)
    missing_information: Mapped[dict | None] = mapped_column(JSONB)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    rules_result: Mapped[dict | None] = mapped_column(JSONB)
    jev_result: Mapped[dict | None] = mapped_column(JSONB)
    llm_result: Mapped[dict | None] = mapped_column(JSONB)
    human_decision: Mapped[str | None] = mapped_column(Text)
    human_comments: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(_ts())


class DecisionRun(CreatedAtMixin, Base):
    __tablename__ = "decision_runs"
    __table_args__ = (
        Index("ix_decision_runs_opportunity_bundle_created", "opportunity_id", "bundle_name", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id"))
    bundle_name: Mapped[str] = mapped_column(Text, nullable=False)
    bundle_version: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'jev'"))
    model: Mapped[str | None] = mapped_column(Text)
    decision_spec_name: Mapped[str | None] = mapped_column(Text)
    decision_spec_hash: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[str | None] = mapped_column(Text)
    input_state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_state_hash: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric)
    cost: Mapped[Decimal | None] = mapped_column(Numeric)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    source_snapshot_ids: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    supersedes_run_id: Mapped[int | None] = mapped_column(ForeignKey("decision_runs.id"))


class Requirement(TimestampMixin, VersionMixin, Base):
    __tablename__ = "requirements"
    __table_args__ = (
        Index("ix_requirements_opportunity_status", "opportunity_id", "status"),
        CheckConstraint(
            "status IN ("
            "'unreviewed', 'satisfied', 'missing', 'unknown', 'needs_review', "
            "'not_applicable', 'stale', 'superseded'"
            ")",
            name="ck_requirements_status",
        ),
        CheckConstraint(
            "severity IS NULL OR severity IN ('critical', 'high', 'medium', 'low')",
            name="ck_requirements_severity",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    source_file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id"))
    source_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("opportunity_snapshots.id"))
    requirement_type: Mapped[str | None] = mapped_column(Text)
    requirement_text: Mapped[str] = mapped_column(Text, nullable=False)
    mandatory: Mapped[bool | None] = mapped_column(Boolean)
    severity: Mapped[str | None] = mapped_column(Text)
    source_section: Mapped[str | None] = mapped_column(Text)
    source_page: Mapped[int | None] = mapped_column(Integer)
    source_quote: Mapped[str | None] = mapped_column(Text)
    source_text_hash: Mapped[str | None] = mapped_column(Text)
    extraction_pass: Mapped[str | None] = mapped_column(Text)
    extraction_confidence: Mapped[Decimal | None] = mapped_column(Numeric)
    independently_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    response_required: Mapped[bool | None] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'unreviewed'"))
    assigned_proposal_section: Mapped[str | None] = mapped_column(Text)
    response_notes: Mapped[str | None] = mapped_column(Text)
    stale_due_to_amendment: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    superseded_by_requirement_id: Mapped[int | None] = mapped_column(ForeignKey("requirements.id"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ai'"))
    verified_by_human: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # Phase 9: every pass's citation, reconciliation flags, parsed facts, and validation trail.
    source_refs: Mapped[list | None] = mapped_column(JSONB)
    reconciliation: Mapped[dict | None] = mapped_column(JSONB)
    key_values: Mapped[dict | None] = mapped_column(JSONB)
    validation: Mapped[dict | None] = mapped_column(JSONB)
    status_reason: Mapped[str | None] = mapped_column(Text)
    blocks_submission: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    clause_library_id: Mapped[int | None] = mapped_column(ForeignKey("clause_library.id"))
    compliance_run_id: Mapped[int | None] = mapped_column(ForeignKey("compliance_runs.id"))
    amendment_changed_at: Mapped[datetime | None] = mapped_column(_ts())


class Proposal(TimestampMixin, VersionMixin, Base):
    __tablename__ = "proposals"
    __table_args__ = (
        UniqueConstraint("opportunity_id", name="uq_proposals_opportunity"),
        CheckConstraint(
            "status IN ('draft', 'ai_generated', 'red_teamed', 'final_approved', 'returned_for_fix', 'cancelled')",
            name="ck_proposals_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    pursuit_id: Mapped[int] = mapped_column(ForeignKey("pursuits.id"), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    current_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("proposal_versions.id", use_alter=True, name="fk_proposals_current_version_id"),
    )
    final_approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    final_approved_at: Mapped[datetime | None] = mapped_column(_ts())
    # Final approval applies to exactly this immutable version.
    approved_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("proposal_versions.id", use_alter=True, name="fk_proposals_approved_version_id"),
    )
    red_team_analysis_id: Mapped[int | None] = mapped_column(ForeignKey("ai_analyses.id"))
    submission_id: Mapped[int | None] = mapped_column(ForeignKey("submissions.id"))


class ProposalVersion(CreatedAtMixin, Base):
    __tablename__ = "proposal_versions"
    __table_args__ = (
        UniqueConstraint("proposal_id", "version_number", name="uq_proposal_versions_proposal_number"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposals.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    change_summary: Mapped[str | None] = mapped_column(Text)
    full_text: Mapped[str | None] = mapped_column(Text)
    version_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB)


class RequirementEvidence(TimestampMixin, Base):
    __tablename__ = "requirement_evidence"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    requirement_id: Mapped[int] = mapped_column(ForeignKey("requirements.id"), nullable=False)
    evidence_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id"))
    proposal_version_id: Mapped[int | None] = mapped_column(ForeignKey("proposal_versions.id"))
    description: Mapped[str | None] = mapped_column(Text)
    source_page: Mapped[int | None] = mapped_column(Integer)
    source_section: Mapped[str | None] = mapped_column(Text)
    source_quote: Mapped[str | None] = mapped_column(Text)
    evidence_value: Mapped[dict | None] = mapped_column(JSONB)
    verification_method: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'unverified'")
    )


class ComplianceRun(CreatedAtMixin, Base):
    __tablename__ = "compliance_runs"
    __table_args__ = (
        Index("ix_compliance_runs_opportunity_type_created", "opportunity_id", "run_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    run_type: Mapped[str] = mapped_column(Text, nullable=False)
    run_version: Mapped[str] = mapped_column(Text, nullable=False)
    source_snapshot_ids: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    input_hash: Mapped[str | None] = mapped_column(Text)
    output_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    mandatory_total: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    mandatory_satisfied: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    mandatory_missing: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    mandatory_unknown: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    mandatory_needs_review: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    critical_total: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    critical_satisfied: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    critical_unresolved: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    false_satisfied_detected: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'complete'"))
    warnings: Mapped[list | None] = mapped_column(JSONB)


class ClauseLibraryEntry(TimestampMixin, Base):
    __tablename__ = "clause_library"
    __table_args__ = (
        UniqueConstraint("clause_family", "clause_number", name="uq_clause_library_family_number"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    clause_family: Mapped[str | None] = mapped_column(Text)
    clause_number: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    typical_effects: Mapped[dict | None] = mapped_column(JSONB)
    verification_questions: Mapped[dict | None] = mapped_column(JSONB)
    expected_evidence: Mapped[dict | None] = mapped_column(JSONB)
    default_risk_level: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    source_version_date: Mapped[date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class ComplianceFinding(TimestampMixin, Base):
    __tablename__ = "compliance_findings"
    __table_args__ = (Index("ix_compliance_findings_opportunity_status", "opportunity_id", "status"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    requirement_id: Mapped[int | None] = mapped_column(ForeignKey("requirements.id"))
    finding_type: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source_refs: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    detected_by: Mapped[str | None] = mapped_column(Text)
    detector_version: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(_ts())
    resolution_notes: Mapped[str | None] = mapped_column(Text)
    compliance_run_id: Mapped[int | None] = mapped_column(ForeignKey("compliance_runs.id"))
    blocks_submission: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    certainty: Mapped[str | None] = mapped_column(Text)


class ProposalSection(TimestampMixin, Base):
    __tablename__ = "proposal_sections"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    proposal_version_id: Mapped[int] = mapped_column(ForeignKey("proposal_versions.id"), nullable=False)
    section_key: Mapped[str | None] = mapped_column(Text)
    heading: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str | None] = mapped_column(Text)
    requirement_ids: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    source_refs: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))


class Submission(TimestampMixin, VersionMixin, Base):
    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint("opportunity_id", name="uq_submissions_opportunity"),
        CheckConstraint(
            "status IN ('preparing', 'ready', 'submitted', 'confirmed', 'failed', 'withdrawn')",
            name="ck_submissions_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    pursuit_id: Mapped[int] = mapped_column(ForeignKey("pursuits.id"), nullable=False)
    submission_method: Mapped[str | None] = mapped_column(Text)
    submission_destination: Mapped[str | None] = mapped_column(Text)
    portal_name: Mapped[str | None] = mapped_column(Text)
    portal_url: Mapped[str | None] = mapped_column(Text)
    recipient_email: Mapped[str | None] = mapped_column(Text)
    submission_deadline: Mapped[datetime | None] = mapped_column(_ts())
    deadline_timezone: Mapped[str | None] = mapped_column(Text)
    required_files: Mapped[dict | None] = mapped_column(JSONB)
    submitted_files: Mapped[dict | None] = mapped_column(JSONB)
    assembled_files: Mapped[dict | None] = mapped_column(JSONB)
    completed_actions: Mapped[dict | None] = mapped_column(JSONB)
    package_manifest_hash: Mapped[str | None] = mapped_column(Text)
    required_actions: Mapped[dict | None] = mapped_column(JSONB)
    readiness_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'not_ready'"))
    submitted_at: Mapped[datetime | None] = mapped_column(_ts())
    confirmation_number: Mapped[str | None] = mapped_column(Text)
    confirmation_file: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'preparing'"))
    notes: Mapped[str | None] = mapped_column(Text)


class OutcomeFeedback(TimestampMixin, Base):
    __tablename__ = "outcome_feedback"
    __table_args__ = (
        UniqueConstraint("opportunity_id", name="uq_outcome_feedback_opportunity"),
        CheckConstraint("outcome IN ('won', 'lost', 'no_bid', 'cancelled')", name="ck_outcome_feedback_outcome"),
        CheckConstraint("award_amount IS NULL OR (award_amount >= 0 AND award_amount < 'Infinity'::numeric)", name="ck_outcome_award_amount"),
        CheckConstraint("known_winning_price IS NULL OR (known_winning_price >= 0 AND known_winning_price < 'Infinity'::numeric)", name="ck_outcome_winning_price"),
        CheckConstraint("win_margin_pct IS NULL OR (win_margin_pct >= 0 AND win_margin_pct <= 100)", name="ck_outcome_margin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    pursuit_id: Mapped[int | None] = mapped_column(ForeignKey("pursuits.id"))

    outcome: Mapped[str | None] = mapped_column(Text)

    # No-bid structured capture
    no_bid_reason: Mapped[str | None] = mapped_column(Text)
    no_bid_category: Mapped[str | None] = mapped_column(Text)

    # Loss structured capture
    loss_reason: Mapped[str | None] = mapped_column(Text)
    known_winning_price: Mapped[Decimal | None] = mapped_column(Numeric)

    # Win structured capture
    win_reason: Mapped[str | None] = mapped_column(Text)
    win_margin_pct: Mapped[Decimal | None] = mapped_column(Numeric)
    win_supplier: Mapped[str | None] = mapped_column(Text)
    win_delivery_terms: Mapped[str | None] = mapped_column(Text)
    win_proposal_version: Mapped[str | None] = mapped_column(Text)

    # Award details (applicable to won/lost)
    awarded_vendor_uei: Mapped[str | None] = mapped_column(Text)
    awarded_vendor_name: Mapped[str | None] = mapped_column(Text)
    award_amount: Mapped[Decimal | None] = mapped_column(Numeric)
    award_date: Mapped[date | None] = mapped_column(Date)

    # Government feedback
    government_feedback: Mapped[str | None] = mapped_column(Text)
    debrief_notes: Mapped[str | None] = mapped_column(Text)
    lessons_learned: Mapped[str | None] = mapped_column(Text)

    user_tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text))

    # Denormalized opportunity fields for analytics (captured at recording time)
    denorm_agency: Mapped[str | None] = mapped_column(Text)
    denorm_psc: Mapped[str | None] = mapped_column(Text)
    denorm_naics: Mapped[str | None] = mapped_column(Text)
    denorm_estimated_value: Mapped[Decimal | None] = mapped_column(Numeric)

    # FK to AI outcome_analysis result (optional)
    outcome_analysis_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ai_analyses.id", ondelete="SET NULL"), nullable=True
    )


class OutcomeCorrection(CreatedAtMixin, Base):
    __tablename__ = "outcome_corrections"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    old_value: Mapped[dict | None] = mapped_column(JSONB)
    new_value: Mapped[dict] = mapped_column(JSONB, nullable=False)


class PackageManifest(CreatedAtMixin, Base):
    __tablename__ = "submission_package_manifests"
    __table_args__ = (UniqueConstraint("submission_id", "sha256", name="uq_submission_manifest_hash"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class SubmissionLegacyHistory(Base):
    """Read-only archive of duplicate submissions retained during migration."""
    __tablename__ = "submission_legacy_history"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)


class ReviewNote(CreatedAtMixin, Base):
    __tablename__ = "review_notes"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id"), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    note_type: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, nullable=False)


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    job: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(_ts(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(_ts())
    status: Mapped[str | None] = mapped_column(Text)
    fetched: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    inserted: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    updated: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    unchanged: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    errors: Mapped[dict | None] = mapped_column(JSONB)


class SchedulerJobRun(Base):
    """Chain-level scheduler execution record written by Phase 17 job chains."""

    __tablename__ = "scheduler_job_runs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    chain_name: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'manual'"))
    started_at: Mapped[datetime] = mapped_column(_ts(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(_ts())
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
    steps_completed: Mapped[list[str] | None] = mapped_column(JSONB)
    failed_step: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    row_counts: Mapped[dict | None] = mapped_column(JSONB)


class ProcessHeartbeat(Base):
    """Latest liveness beat for a local web, worker, or scheduler process."""

    __tablename__ = "process_heartbeats"
    __table_args__ = (
        UniqueConstraint("role", "instance_id", name="uq_process_heartbeats_role_instance"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    instance_id: Mapped[str] = mapped_column(Text, nullable=False)
    beat_at: Mapped[datetime] = mapped_column(_ts(), nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


BOT_NAMES = (
    "orchestrator", "discovery", "document", "matching", "bid_decision",
    "compliance", "amendment", "awards", "alert", "operations",
)
BOT_STATUSES = (
    "queued", "running", "succeeded", "completed_with_errors", "failed",
    "skipped", "blocked", "waiting_approval",
)
BOT_APPROVAL_STATUSES = ("pending", "approved", "rejected")


class BotRun(Base):
    """One execution of an in-app bot, including retries of the same inputs."""

    __tablename__ = "bot_runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_bot_runs_idempotency_key"),
        CheckConstraint(f"bot_name IN {BOT_NAMES!r}", name="ck_bot_runs_bot_name"),
        CheckConstraint(f"status IN {BOT_STATUSES!r}", name="ck_bot_runs_status"),
        Index("ix_bot_runs_bot_started", "bot_name", "started_at"),
        Index("ix_bot_runs_opportunity", "opportunity_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    bot_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
    trigger: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'manual'"))
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id", ondelete="CASCADE"))
    source_revision: Mapped[str | None] = mapped_column(Text)
    parent_run_id: Mapped[int | None] = mapped_column(ForeignKey("bot_runs.id", ondelete="SET NULL"))
    started_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=text("now()"))
    finished_at: Mapped[datetime | None] = mapped_column(_ts())
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    inputs: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    outputs: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=text("now()"))


class BotApproval(Base):
    """A human decision the bots are not allowed to make themselves."""

    __tablename__ = "bot_approvals"
    __table_args__ = (
        CheckConstraint(f"status IN {BOT_APPROVAL_STATUSES!r}", name="ck_bot_approvals_status"),
        Index("ix_bot_approvals_status", "status", "requested_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    bot_run_id: Mapped[int] = mapped_column(ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    requested_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=text("now()"))
    decided_at: Mapped[datetime | None] = mapped_column(_ts())
    decided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decision_note: Mapped[str | None] = mapped_column(Text)


class AlertDelivery(Base):
    """A digest claim committed before SMTP so a crash cannot send the same body twice."""

    __tablename__ = "alert_deliveries"
    __table_args__ = (
        UniqueConstraint("claim_key", name="uq_alert_deliveries_claim_key"),
        CheckConstraint("status IN ('sending', 'sent', 'failed')", name="ck_alert_deliveries_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    claim_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'sending'"))
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    match_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    claimed_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=text("now()"))
    finished_at: Mapped[datetime | None] = mapped_column(_ts())


TASK_TYPES = (
    "proposal_generation", "ai_analysis", "solicitation_summary", "scheduler_chain", "opportunity_preparation",
    "notification_email", "quote_extraction", "bot_run", "market_price_research", "attachment_download",
    "opportunity_review",
)
TASK_STATUSES = (
    "queued", "running", "waiting_for_input", "waiting_for_budget", "retrying",
    "succeeded", "failed", "cancelled",
)
TERMINAL_TASK_STATUSES = ("succeeded", "failed", "cancelled")


class Task(TimestampMixin, Base):
    """Durable unit of background work claimed by workers (ADR-061).

    ``dedup_key`` is unique among non-terminal tasks, so the same work for the
    same inputs is queued once. ``claim_token`` is the lease fencing token: a
    worker may publish only while it still holds the lease it claimed.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(f"task_type IN {TASK_TYPES!r}", name="ck_tasks_task_type"),
        CheckConstraint(f"status IN {TASK_STATUSES!r}", name="ck_tasks_status"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="ck_tasks_attempts"),
        Index(
            "uq_tasks_dedup_key_active", "dedup_key", unique=True,
            postgresql_where=text(f"status NOT IN {TERMINAL_TASK_STATUSES!r}"),
        ),
        Index("ix_tasks_status_next_attempt_at", "status", "next_attempt_at"),
        Index("ix_tasks_opportunity_type_created", "opportunity_id", "task_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    opportunity_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("opportunities.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    dedup_key: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    input_revision: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    checkpoint: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    result: Mapped[dict | None] = mapped_column(JSONB)
    current_step: Mapped[str | None] = mapped_column(Text)
    blocker_owner_role: Mapped[str | None] = mapped_column(Text)
    blocker_owner_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    blocker_next_action: Mapped[str | None] = mapped_column(Text)
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(_ts())
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Monotonic fencing token: incremented by every claim, never decremented.
    claim_token: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    next_attempt_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=text("now()"))
    last_error_type: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    superseded_by_task_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("tasks.id"))
    scheduler_job_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("scheduler_job_runs.id", ondelete="SET NULL")
    )
    created_by_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    started_at: Mapped[datetime | None] = mapped_column(_ts())
    finished_at: Mapped[datetime | None] = mapped_column(_ts())


class Recommendation(TimestampMixin, Base):
    """A persisted semantic recommendation with the input revisions it came from (ADR-069)."""

    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False)
    watchlist_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("watchlists.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(Text, nullable=False)
    similarity: Mapped[Decimal | None] = mapped_column(Numeric)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    embedding_model_version: Mapped[str | None] = mapped_column(Text)
    watchlist_profile_hash: Mapped[str | None] = mapped_column(Text)
    opportunity_source_hash: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_seen_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=text("now()"))


class NotificationDelivery(TimestampMixin, Base):
    """One attempt series to deliver a notification by email (ADR-070)."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint("status IN ('queued','sent','failed')", name="ck_notification_deliveries_status"),
        CheckConstraint("channel IN ('email')", name="ck_notification_deliveries_channel"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    notification_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'email'"))
    recipient: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    sent_at: Mapped[datetime | None] = mapped_column(_ts())
    error: Mapped[str | None] = mapped_column(Text)


class Supplier(TimestampMixin, Base):
    """A supplier we buy from (ADR-071); ``provenance`` says who or what recorded it."""

    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    uei: Mapped[str | None] = mapped_column(Text)
    cage_code: Mapped[str | None] = mapped_column(Text)
    contact_name: Mapped[str | None] = mapped_column(Text)
    contact_email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))


class Product(TimestampMixin, Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    part_number: Mapped[str] = mapped_column(Text, nullable=False)
    manufacturer: Mapped[str | None] = mapped_column(Text)
    nsn: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(Text)


class CatalogImport(CreatedAtMixin, Base):
    __tablename__ = "catalog_imports"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    supplier_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False)
    filename: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    rows_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rows_imported: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    errors: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    imported_by_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))


class SupplierProduct(TimestampMixin, Base):
    __tablename__ = "supplier_products"
    __table_args__ = (UniqueConstraint("supplier_id", "product_id", name="uq_supplier_products_supplier_product"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    supplier_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    list_price: Mapped[Decimal | None] = mapped_column(Numeric)
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'USD'"))
    valid_until: Mapped[date | None] = mapped_column(Date)
    catalog_import_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("catalog_imports.id", ondelete="SET NULL"))


class SupplierQuote(TimestampMixin, Base):
    """A supplier's quote for one opportunity; PROPRIETARY (ADR-071).

    The quote document's bytes live in the attachment store (``source_key``)
    and never become a solicitation file of the opportunity.
    """

    __tablename__ = "supplier_quotes"
    __table_args__ = (
        CheckConstraint("status IN ('active','withdrawn')", name="ck_supplier_quotes_status"),
        CheckConstraint("extraction_method IN ('manual','csv','xlsx','ai')", name="ck_supplier_quotes_extraction_method"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False)
    supplier_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    received_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=text("now()"))
    valid_until: Mapped[date | None] = mapped_column(Date)
    total_price: Mapped[Decimal | None] = mapped_column(Numeric)
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'USD'"))
    extraction_method: Mapped[str] = mapped_column(Text, nullable=False)
    source_filename: Mapped[str | None] = mapped_column(Text)
    source_sha256: Mapped[str | None] = mapped_column(Text)
    source_key: Mapped[str | None] = mapped_column(Text)
    entered_by_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    notes: Mapped[str | None] = mapped_column(Text)


class SupplierQuoteLine(Base):
    __tablename__ = "supplier_quote_lines"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    quote_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("supplier_quotes.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="SET NULL"))
    description: Mapped[str | None] = mapped_column(Text)
    part_number: Mapped[str | None] = mapped_column(Text)
    nsn: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric)
    unit: Mapped[str | None] = mapped_column(Text)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric)
    extended_price: Mapped[Decimal | None] = mapped_column(Numeric)
    lead_time_days: Mapped[int | None] = mapped_column(Integer)


class RfqDraft(TimestampMixin, Base):
    """A request for quote drafted for a person to send; never sent by GovCon."""

    __tablename__ = "rfq_drafts"
    __table_args__ = (CheckConstraint("status IN ('draft')", name="ck_rfq_drafts_status"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False)
    supplier_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("suppliers.id", ondelete="SET NULL"))
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    created_by_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))


class MarketPriceRun(CreatedAtMixin, Base):
    """One web price search for an opportunity's product.

    Listings are commercial web prices, not supplier quotes: their median is an
    estimated cost for margin math until a supplier quote is recorded.
    """

    __tablename__ = "market_price_runs"
    __table_args__ = (
        CheckConstraint("status IN ('completed','no_results','skipped','blocked','failed')",
                        name="ck_market_price_runs_status"),
        Index("ix_market_price_runs_opportunity_created", "opportunity_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False)
    task_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("tasks.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    source_revision: Mapped[str | None] = mapped_column(Text)
    # The product description that was searched, as sent.
    product: Mapped[dict | None] = mapped_column(JSONB)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    prompt_sha256: Mapped[str | None] = mapped_column(Text)
    listings: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # Listings dropped after the search (government sites, invalid prices) with the reason.
    excluded: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    estimate_unit_cost: Mapped[Decimal | None] = mapped_column(Numeric)
    estimate_low: Mapped[Decimal | None] = mapped_column(Numeric)
    estimate_high: Mapped[Decimal | None] = mapped_column(Numeric)
    estimate_confidence: Mapped[str | None] = mapped_column(Text)
    estimate_basis: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric)
    unit: Mapped[str | None] = mapped_column(Text)
    estimated_total_cost: Mapped[Decimal | None] = mapped_column(Numeric)
    usage: Mapped[dict | None] = mapped_column(JSONB)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)


class AISharingAuthorization(Base):
    """An owner's explicit, expiring permission to send a data scope to one AI provider (ADR-071)."""

    __tablename__ = "ai_sharing_authorizations"
    __table_args__ = (CheckConstraint("scope IN ('supplier_quotes')", name="ck_ai_sharing_authorizations_scope"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    granted_by_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    granted_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=text("now()"))
    expires_at: Mapped[datetime] = mapped_column(_ts(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(_ts())
    revoked_by_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))


class CompanyRegistration(TimestampMixin, Base):
    """Our own SAM registration, refreshed daily from the SAM entity API (ADR-072)."""

    __tablename__ = "company_registration"
    __table_args__ = (
        CheckConstraint(
            "freshness_status IN ('fresh', 'stale', 'expired', 'failed', 'unknown')",
            name="ck_company_registration_freshness_status",
        ),
        CheckConstraint(
            "attempt_state IS NULL OR attempt_state IN ('in_progress', 'cancelled', 'applied', 'failed')",
            name="ck_company_registration_attempt_state",
        ),
    )

    uei: Mapped[str] = mapped_column(Text, primary_key=True)
    legal_name: Mapped[str | None] = mapped_column(Text)
    cage_code: Mapped[str | None] = mapped_column(Text)
    registration_status: Mapped[str | None] = mapped_column(Text)
    expiration_date: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'sam_entity_api'"))
    refreshed_at: Mapped[datetime] = mapped_column(_ts(), nullable=False)
    fetched_at: Mapped[datetime | None] = mapped_column(_ts())
    source_updated_at: Mapped[datetime | None] = mapped_column(_ts())
    expires_at: Mapped[date | None] = mapped_column(Date)
    freshness_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'unknown'"), default="unknown"
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(_ts())
    refresh_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    attempt_id: Mapped[str | None] = mapped_column(Text)
    attempt_state: Mapped[str | None] = mapped_column(Text)
    registration_key: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict | None] = mapped_column(JSONB)
    last_expiry_alert_at: Mapped[datetime | None] = mapped_column(_ts())


class OutcomeSuggestion(TimestampMixin, Base):
    """An award record that may settle a submitted bid; a person confirms it (ADR-073)."""

    __tablename__ = "outcome_suggestions"
    __table_args__ = (
        UniqueConstraint("opportunity_id", "source", "source_ref", name="uq_outcome_suggestions_source"),
        CheckConstraint("source IN ('sam_award_notice','usaspending')", name="ck_outcome_suggestions_source"),
        CheckConstraint("strength IN ('strong','possible')", name="ck_outcome_suggestions_strength"),
        CheckConstraint("suggested_outcome IS NULL OR suggested_outcome IN ('won','lost')",
                        name="ck_outcome_suggestions_outcome"),
        CheckConstraint("status IN ('suggested','confirmed','dismissed')", name="ck_outcome_suggestions_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    strength: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_outcome: Mapped[str | None] = mapped_column(Text)
    matched_identifiers: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    awardee_name: Mapped[str | None] = mapped_column(Text)
    awardee_uei: Mapped[str | None] = mapped_column(Text)
    award_amount: Mapped[Decimal | None] = mapped_column(Numeric)
    award_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'suggested'"))
    decided_by_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(_ts())


class AnalyticsSnapshot(CreatedAtMixin, Base):
    """A scheduled refresh of outcome analytics, with what it counted (ADR-074)."""

    __tablename__ = "analytics_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    outcome_count: Mapped[int] = mapped_column(Integer, nullable=False)
    won: Mapped[int] = mapped_column(Integer, nullable=False)
    lost: Mapped[int] = mapped_column(Integer, nullable=False)
    no_bid: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)


class AppSetting(TimestampMixin, Base):
    """An owner-editable workflow setting, changed on the web Settings page (ADR-067)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_by_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))


UPDATED_AT_TABLES: tuple[str, ...] = tuple(
    table.name for table in Base.metadata.tables.values() if "updated_at" in table.c
)
