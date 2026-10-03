"""Phase 15 — Outcome learning & analytics tests.

Tests cover:
- Structured outcome recording (won / lost / no_bid / cancelled)
- Denormalization of agency/PSC/NAICS from opportunity
- Analytics: win rate by PSC, agency, size, margin averages
- No-bid and loss reason counts
- Competitors and suppliers
- Recommendation learning rules (< 3 wins → no win profile)
- Similar past outcomes for decision reports
- MCP op_record_outcome structured fields
- MCP op_learning_summary real analytics
- Web workspace_record_outcome structured capture
- outcome_analysis_v1 prompt is active
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.collaboration.users import invite_user
from govcon.learning import analytics as anl
from govcon.learning.outcomes import NO_BID_CATEGORIES
from govcon.learning.outcomes import record_outcome as _record_outcome_service
from govcon.mcp import operations as mcp_ops
from govcon.models import Opportunity, OutcomeFeedback, Pursuit, Submission


@pytest.fixture()
def session(upgraded_engine):
    with Session(upgraded_engine) as s:
        yield s
        s.rollback()


def _approver(session: Session):
    """One approver per test session; outcomes are recorded by an authorized human."""
    user = session.info.get("outcome_approver")
    if user is None:
        user = invite_user(
            session,
            email=f"ol-approver-{uuid4().hex[:8]}@example.test",
            display_name="OL Approver",
            password="correct horse battery",
            role="approver",
        )
        session.info["outcome_approver"] = user
    return user


def record_outcome(session: Session, **kwargs):
    kwargs.setdefault("actor", _approver(session))
    return _record_outcome_service(session, **kwargs)


@pytest.fixture()
def mcp_actor(owner, monkeypatch):
    from govcon.mcp.context import reset_server_actor

    reset_server_actor()
    monkeypatch.setenv("MCP_ACTOR_EMAIL", owner.email)
    yield owner
    reset_server_actor()


@pytest.fixture()
def owner(session: Session):
    return invite_user(
        session,
        email=f"ol-owner-{uuid4().hex[:8]}@example.test",
        display_name="OL Owner",
        password="correct horse battery",
        role="owner",
    )


def _opp(
    session: Session,
    *,
    psc: str = "5895",
    naics: str = "334290",
    agency: str | None = "DEPT OF DEFENSE > DLA",
    estimated_value: float | None = 50_000.0,
) -> Opportunity:
    row = Opportunity(
        source="sam",
        source_id=f"ol-{uuid4().hex}",
        title=f"Outcome-test opportunity {uuid4().hex[:6]}",
        description="Test fixture",
        status="open",
        psc_code=psc,
        naics_code=naics,
        agency_path=agency,
        estimated_value_min=Decimal(str(estimated_value)) if estimated_value else None,
        response_deadline=datetime.now(UTC) + timedelta(days=14),
        raw={"fixture": True},
        links={},
    )
    session.add(row)
    session.flush()
    return row


def _pursuit(session: Session, opp_id: int, stage: str = "submitted") -> Pursuit:
    p = Pursuit(opportunity_id=opp_id, stage=stage, submitted_at=datetime.now(UTC) if stage == "submitted" else None)
    session.add(p)
    session.flush()
    if stage == "submitted":
        session.add(Submission(opportunity_id=opp_id, pursuit_id=p.id, status="submitted", submitted_at=p.submitted_at))
        session.flush()
    return p


# ── Outcome recording ─────────────────────────────────────────────────────────


class TestRecordOutcome:
    def test_record_won_denorms_opportunity_fields(self, session: Session):
        opp = _opp(session, psc="6515", agency="DEPT OF DEFENSE > DLA", estimated_value=80_000.0)
        _pursuit(session, opp.id, "submitted")
        row = record_outcome(
            session,
            opportunity_id=opp.id,
            outcome="won",
            win_reason="Lowest price; strong past performance",
            win_margin_pct=22.5,
            win_supplier="Acme Supplies",
            award_amount=80_000.0,
        )
        assert row.outcome == "won"
        assert row.denorm_psc == "6515"
        assert row.denorm_agency == "DEPT OF DEFENSE > DLA"
        assert float(row.win_margin_pct) == pytest.approx(22.5)
        assert row.win_supplier == "Acme Supplies"
        assert float(row.award_amount) == pytest.approx(80_000.0)

    def test_record_lost_structured_fields(self, session: Session):
        opp = _opp(session, psc="5895")
        _pursuit(session, opp.id, "submitted")
        row = record_outcome(
            session,
            opportunity_id=opp.id,
            outcome="lost",
            loss_reason="pricing",
            known_winning_price=42_000.0,
            awarded_vendor_name="Superior Widgets LLC",
            government_feedback="Offeror's price was non-competitive.",
        )
        assert row.outcome == "lost"
        assert row.loss_reason == "pricing"
        assert float(row.known_winning_price) == pytest.approx(42_000.0)
        assert row.awarded_vendor_name == "Superior Widgets LLC"
        assert row.government_feedback is not None

    def test_record_no_bid_with_category(self, session: Session):
        opp = _opp(session, psc="5895")
        row = record_outcome(
            session,
            opportunity_id=opp.id,
            outcome="no_bid",
            no_bid_category="margin",
            no_bid_reason="Margin below threshold at current commodity pricing.",
        )
        assert row.outcome == "no_bid"
        assert row.no_bid_category == "margin"
        assert row.no_bid_reason is not None

    def test_record_updates_pursuit_stage(self, session: Session):
        opp = _opp(session)
        pursuit = _pursuit(session, opp.id, "submitted")
        record_outcome(session, opportunity_id=opp.id, outcome="won")
        session.expire(pursuit)
        assert pursuit.stage == "won"
        assert pursuit.outcome_at is not None

    def test_invalid_outcome_raises(self, session: Session):
        opp = _opp(session)
        with pytest.raises(ValueError, match="outcome must be"):
            record_outcome(session, opportunity_id=opp.id, outcome="maybe")

    def test_invalid_no_bid_category_raises(self, session: Session):
        opp = _opp(session)
        with pytest.raises(ValueError, match="no_bid_category must be"):
            record_outcome(
                session,
                opportunity_id=opp.id,
                outcome="no_bid",
                no_bid_category="wrong_category",
            )

    def test_no_bid_category_constants_complete(self):
        expected = {
            "missing_capability",
            "margin",
            "deadline",
            "supplier_availability",
            "eligibility",
            "compliance_issue",
            "competition",
            "strategic_choice",
            "other",
        }
        assert NO_BID_CATEGORIES == expected


# ── Analytics ─────────────────────────────────────────────────────────────────


class TestAnalytics:
    def _seed_outcomes(self, session: Session, *, prefix: str = "") -> list[OutcomeFeedback]:
        """Create a mix of outcomes for analytics testing."""
        rows = []
        # 3 wins on PSC 6515
        for i in range(3):
            opp = _opp(session, psc="6515", agency="DEPT OF DEFENSE > DLA", estimated_value=80_000.0)
            _pursuit(session, opp.id)
            row = record_outcome(
                session,
                opportunity_id=opp.id,
                outcome="won",
                win_margin_pct=15.0 + i,
                win_supplier=f"Supplier-{prefix}{i}",
                award_amount=80_000.0,
            )
            rows.append(row)
        # 2 losses on PSC 5895
        for i in range(2):
            opp = _opp(session, psc="5895", agency="DEPT OF THE AIR FORCE", estimated_value=200_000.0)
            _pursuit(session, opp.id)
            row = record_outcome(
                session,
                opportunity_id=opp.id,
                outcome="lost",
                loss_reason="pricing",
                awarded_vendor_name=f"Competitor-{prefix}{i}",
            )
            rows.append(row)
        # 1 no-bid
        opp = _opp(session, psc="9999")
        row = record_outcome(
            session,
            opportunity_id=opp.id,
            outcome="no_bid",
            no_bid_category="strategic_choice",
        )
        rows.append(row)
        return rows

    def test_win_count(self, session: Session):
        prefix = uuid4().hex[:4]
        self._seed_outcomes(session, prefix=prefix)
        count = anl.win_count(session)
        assert count >= 3

    def test_win_profile_not_available_below_minimum(self, session: Session):
        """Before 3 wins, win profile must be unavailable."""
        with Session(session.get_bind()) as clean:
            count = anl.win_count(clean)
        available, note = anl.win_profile_note(session)
        if count < anl.WIN_PROFILE_MINIMUM:
            assert not available
            assert "minimum" in note.lower()

    def test_win_profile_available_after_minimum(self, session: Session):
        self._seed_outcomes(session, prefix=uuid4().hex[:4])
        count = anl.win_count(session)
        available, note = anl.win_profile_note(session)
        if count >= anl.WIN_PROFILE_MINIMUM:
            assert available

    def test_avg_margin_on_wins(self, session: Session):
        self._seed_outcomes(session, prefix=uuid4().hex[:4])
        margin = anl.avg_margin_on_wins(session)
        assert margin is not None
        assert margin > 0

    def test_no_bid_reason_counts(self, session: Session):
        prefix = uuid4().hex[:4]
        self._seed_outcomes(session, prefix=prefix)
        reasons = anl.no_bid_reason_counts(session)
        found = {r.reason for r in reasons}
        assert "strategic_choice" in found

    def test_loss_reason_counts(self, session: Session):
        prefix = uuid4().hex[:4]
        self._seed_outcomes(session, prefix=prefix)
        reasons = anl.loss_reason_counts(session)
        found = {r.reason for r in reasons}
        assert "pricing" in found

    def test_common_competitors(self, session: Session):
        prefix = uuid4().hex[:4]
        self._seed_outcomes(session, prefix=prefix)
        competitors = anl.common_competitors(session)
        assert len(competitors) >= 1

    def test_reliable_suppliers(self, session: Session):
        prefix = uuid4().hex[:4]
        self._seed_outcomes(session, prefix=prefix)
        suppliers = anl.reliable_suppliers(session)
        assert len(suppliers) >= 1

    def test_outcome_analytics_full(self, session: Session):
        prefix = uuid4().hex[:4]
        self._seed_outcomes(session, prefix=prefix)
        analytics = anl.outcome_analytics(session)
        assert analytics.total_won >= 3
        assert analytics.total_lost >= 2
        assert analytics.total_no_bid >= 1
        assert analytics.win_profile_note
        assert isinstance(analytics.by_psc, list)
        assert isinstance(analytics.by_agency, list)
        assert isinstance(analytics.no_bid_reasons, list)
        assert isinstance(analytics.loss_reasons, list)
        assert isinstance(analytics.common_competitors, list)
        assert isinstance(analytics.reliable_suppliers, list)
        assert isinstance(analytics.recent_outcomes, list)

    def test_win_rate_by_psc_has_entries(self, session: Session):
        self._seed_outcomes(session, prefix=uuid4().hex[:4])
        by_psc = anl.win_rate_by_psc(session)
        assert len(by_psc) >= 1
        # Check that small_sample is flagged for small groups
        for row in by_psc:
            if row.submitted < anl.SMALL_SAMPLE_THRESHOLD:
                assert row.small_sample

    def test_win_rate_by_agency_has_entries(self, session: Session):
        self._seed_outcomes(session, prefix=uuid4().hex[:4])
        by_agency = anl.win_rate_by_agency(session)
        assert len(by_agency) >= 1

    def test_win_rate_by_size(self, session: Session):
        self._seed_outcomes(session, prefix=uuid4().hex[:4])
        by_size = anl.win_rate_by_size(session)
        assert len(by_size) >= 1

    def test_similar_past_outcomes(self, session: Session):
        prefix = uuid4().hex[:4]
        self._seed_outcomes(session, prefix=prefix)
        # Find a PSC-6515 won opp
        target_opp = session.scalars(
            select(Opportunity).where(Opportunity.psc_code == "6515").limit(1)
        ).first()
        if target_opp is None:
            return
        # Create new opp with same PSC but no outcome to be the query target
        query_opp = _opp(session, psc="6515", agency="DLA")
        similar = anl.similar_past_outcomes(session, query_opp.id)
        assert isinstance(similar, list)
        if similar:
            for item in similar:
                assert "outcome" in item
                assert "match_reason" in item

    def test_analytics_note_no_causal_claims(self, session: Session):
        """The analytics note must not claim causation."""
        self._seed_outcomes(session, prefix=uuid4().hex[:4])
        analytics = anl.outcome_analytics(session)
        note = analytics.win_profile_note.lower()
        assert "caus" not in note


# ── MCP operations ────────────────────────────────────────────────────────────


class TestMCPOutcome:
    def test_mcp_record_outcome_structured(self, session: Session, mcp_actor):
        opp = _opp(session, psc="6515", agency="DEPT OF DEFENSE > DLA")
        _pursuit(session, opp.id, "submitted")
        result = mcp_ops.op_record_outcome(
            session,
            opp.id,
            outcome="won",
            win_reason="Best price and delivery",
            win_margin_pct=18.0,
            win_supplier="Widgets Corp",
            award_amount=75_000.0,
        )
        assert result["ok"] is True
        assert result["data"]["outcome"] == "won"
        assert result["data"]["win_supplier"] == "Widgets Corp"
        assert result["data"]["denorm_psc"] == "6515"

    def test_mcp_record_outcome_loss_structured(self, session: Session, mcp_actor):
        opp = _opp(session, psc="5895")
        _pursuit(session, opp.id, "submitted")
        result = mcp_ops.op_record_outcome(
            session,
            opp.id,
            outcome="lost",
            loss_reason="pricing",
            known_winning_price=38_000.0,
            awarded_vendor_name="Other Widgets LLC",
            government_feedback="Government awarded to lower price.",
        )
        assert result["ok"] is True
        assert result["data"]["outcome"] == "lost"
        assert result["data"]["loss_reason"] == "pricing"
        assert result["data"]["known_winning_price"] == pytest.approx(38_000.0)

    def test_mcp_record_outcome_no_bid(self, session: Session, mcp_actor):
        opp = _opp(session)
        result = mcp_ops.op_record_outcome(
            session,
            opp.id,
            outcome="no_bid",
            no_bid_category="margin",
            no_bid_reason="Below threshold margin after commodity spike.",
        )
        assert result["ok"] is True
        assert result["data"]["no_bid_category"] == "margin"

    def test_mcp_learning_summary_global(self, session: Session):
        # Seed some data first
        prefix = uuid4().hex[:4]
        for _ in range(3):
            opp = _opp(session, psc="6515")
            _pursuit(session, opp.id)
            record_outcome(session, opportunity_id=opp.id, outcome="won", win_margin_pct=10.0)
        result = mcp_ops.op_learning_summary(session)
        assert result["ok"] is True
        d = result["data"]
        assert "total_won" in d
        assert "overall_win_rate_pct" in d
        assert "win_profile_note" in d
        assert "by_psc" in d
        assert "by_agency" in d
        assert "no_bid_reasons" in d
        assert "loss_reasons" in d
        assert "common_competitors" in d
        assert "analytics_note" in d
        assert "caus" not in d["analytics_note"].lower()  # no causal overclaims

    def test_mcp_learning_summary_per_opportunity(self, session: Session):
        opp = _opp(session, psc="6515")
        _pursuit(session, opp.id)
        record_outcome(session, opportunity_id=opp.id, outcome="won")
        # Create a similar opp for "similar past outcomes"
        similar_opp = _opp(session, psc="6515")
        result = mcp_ops.op_learning_summary(session, opportunity_id=similar_opp.id)
        assert result["ok"] is True
        d = result["data"]
        assert "similar_past_outcomes" in d
        assert "note" in d

    def test_mcp_invalid_outcome(self, session: Session):
        opp = _opp(session)
        # ValueError propagates from op (server wrapper converts to failure dict)
        with pytest.raises(ValueError, match="outcome must be"):
            mcp_ops.op_record_outcome(session, opp.id, outcome="maybe")

    def test_mcp_invalid_no_bid_category(self, session: Session):
        opp = _opp(session)
        with pytest.raises(ValueError, match="no_bid_category must be"):
            mcp_ops.op_record_outcome(
                session,
                opp.id,
                outcome="no_bid",
                no_bid_category="made_up_category",
            )


# ── Prompt activation ─────────────────────────────────────────────────────────


class TestOutcomeAnalysisPrompt:
    def _prompt_root(self):
        from pathlib import Path

        return Path(__file__).parent.parent / "src" / "govcon" / "prompts" / "deepseek"

    def test_outcome_analysis_prompt_is_active(self):
        from govcon.prompting.registry import load_prompt_from_disk

        prompt = load_prompt_from_disk(self._prompt_root(), "outcome_analysis")
        assert prompt is not None
        assert prompt.metadata.get("status") == "active"
        assert "UNKNOWN" in prompt.body

    def test_outcome_analysis_prompt_has_evidence_rules(self):
        from govcon.prompting.registry import load_prompt_from_disk

        prompt = load_prompt_from_disk(self._prompt_root(), "outcome_analysis")
        body = prompt.body.upper()
        # Must not claim price difference alone establishes pricing factor
        assert "MERELY" in body or "ALONE" in body or "NOT SUFFICIENT" in body

    def test_outcome_learning_jev_spec_exists(self):
        from pathlib import Path

        spec_path = (
            Path(__file__).parent.parent
            / "src"
            / "govcon"
            / "prompts"
            / "jev"
            / "outcome_learning_v1.yaml"
        )
        assert spec_path.exists()
        content = spec_path.read_text(encoding="utf-8")
        assert "outcome_learning" in content
        assert "questions" in content


# ── Recommendation learning rules ─────────────────────────────────────────────


class TestRecommendationLearningRules:
    def test_no_win_profile_before_3_wins_explicit(self, session: Session):
        """If < 3 wins in DB, win_profile_note must say it's unavailable."""
        # Use a fresh session to check count
        total_wins = anl.win_count(session)
        available, note = anl.win_profile_note(session)
        if total_wins < anl.WIN_PROFILE_MINIMUM:
            assert not available
            assert str(anl.WIN_PROFILE_MINIMUM) in note

    def test_win_profile_minimum_is_3(self):
        assert anl.WIN_PROFILE_MINIMUM == 3

    def test_small_sample_labeled(self, session: Session):
        """Win-rate rows with < SMALL_SAMPLE_THRESHOLD bids must be labeled."""
        opp = _opp(session, psc="XXXX-unique")
        _pursuit(session, opp.id)
        record_outcome(session, opportunity_id=opp.id, outcome="won")
        by_psc = anl.win_rate_by_psc(session)
        small_rows = [r for r in by_psc if r.submitted < anl.SMALL_SAMPLE_THRESHOLD]
        for row in small_rows:
            assert row.small_sample is True

    def test_no_causal_overstatement_in_analytics_note(self, session: Session):
        analytics = anl.outcome_analytics(session)
        note = analytics.win_profile_note
        # Note must not claim causation — check for direct causal language
        forbidden = ["therefore", "caused by", "because of", "due to", "as a result"]
        note_lower = note.lower()
        for phrase in forbidden:
            assert phrase not in note_lower, f"Causal phrase found: {phrase!r}"


# ── Decision report integration ───────────────────────────────────────────────


class TestDecisionReportIntegration:
    def test_similar_past_outcomes_descriptive_only(self, session: Session):
        """similar_past_outcomes returns descriptive data with match_reason, no causal claims."""
        opp1 = _opp(session, psc="6515", agency="DEPT OF DEFENSE > DLA")
        _pursuit(session, opp1.id)
        record_outcome(session, opportunity_id=opp1.id, outcome="won", win_reason="Best price")

        opp2 = _opp(session, psc="6515", agency="DEPT OF DEFENSE > DLA")
        results = anl.similar_past_outcomes(session, opp2.id)
        assert isinstance(results, list)
        if results:
            for r in results:
                assert "outcome" in r
                assert "match_reason" in r
                # No causal language in the match_reason
                assert "cause" not in r["match_reason"].lower()

    def test_similar_past_outcomes_empty_for_unknown_psc(self, session: Session):
        opp = _opp(session, psc="ZZZZ-unique-no-match", agency=None)
        # Explicitly clear agency so no matches are possible
        opp.agency_path = None
        session.flush()
        results = anl.similar_past_outcomes(session, opp.id)
        assert results == []
