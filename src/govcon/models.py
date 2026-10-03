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

    uei: Mapped[str] = mapped_column(Text, primary_key=True)
    cage_code: Mapped[str | None] = mapped_column(Text)
    legal_name: Mapped[str | None] = mapped_column(Text)
    dba_name: Mapped[str | None] = mapped_column(Text)
    registration_status: Mapped[str | None] = mapped_column(Text)
    physical_address: Mapped[dict | None] = mapped_column(JSONB)
    business_types: Mapped[dict | None] = mapped_column(JSONB)
    naics_codes: Mapped[dict | None] = mapped_column(JSONB)
    psc_codes: Mapped[dict | None] = mapped_column(JSONB)
    points_of_contact: Mapped[dict | None] = mapped_column(JSONB)
    raw: Mapped[dict | None] = mapped_column(JSONB)
    fetched_at: Mapped[datetime | None] = mapped_column(_ts())


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
            "'proposal_package_generated', 'submission_ready'"
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
    ai_supporting_evidence: Mapped[dict | None] = mapped_column(JSONB)
    ai_contradicting_evidence: Mapped[dict | None] = mapped_column(JSONB)
    ai_missing_information: Mapped[dict | None] = mapped_column(JSONB)
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
    steps_completed: Mapped[dict | None] = mapped_column(JSONB)
    failed_step: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    row_counts: Mapped[dict | None] = mapped_column(JSONB)


UPDATED_AT_TABLES: tuple[str, ...] = tuple(
    table.name for table in Base.metadata.tables.values() if "updated_at" in table.c
)
