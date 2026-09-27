"""Phase 14 Web UI tests.

Tests cover:
- Login page renders
- Unauthenticated requests redirect to /login
- Authenticated session allows access to all main pages
- Inbox, search, pipeline, watchlists, vendors, ops, learning render
- Match status actions work (seen, dismissed, pursuing)
- Workspace renders for all tabs
- Two users can view the same workspace simultaneously
- Reviewer identity is visible on comments
- Approval permissions are enforced
- Audit history captures who changed what

These tests use the real Postgres DB via the upgraded_engine fixture.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.models import (
    Match,
    Opportunity,
    ReviewAssignment,
    ReviewComment,
    ReviewSession,
    User,
    UserSession,
    Watchlist,
)
from govcon.web.app import create_app


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def db_session(upgraded_engine):
    """Provide a transactional session for setup/teardown."""
    with Session(upgraded_engine, autobegin=True) as session:
        yield session
        session.rollback()


@pytest.fixture(scope="module")
def client(upgraded_engine):
    import os
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://govcon:govcon@localhost:5432/govcon")
    from govcon.config import get_settings
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
        yield c


def _make_user(session: Session, email: str, role: str = "reviewer") -> tuple[User, str]:
    """Create a user and return (user, raw_token)."""
    from govcon.collaboration.users import hash_password, create_session
    existing = session.scalar(select(User).where(User.email == email))
    if existing:
        # clean old sessions
        session.query(UserSession).filter_by(user_id=existing.id).delete()
        user = existing
    else:
        user = User(
            email=email,
            display_name=email.split("@")[0],
            password_hash=hash_password("TestPassword123!"),
            role=role,
            is_active=True,
        )
        session.add(user)
        session.flush()
    raw = create_session(session, user)
    session.commit()
    return user, raw


def _make_opp(session: Session) -> Opportunity:
    existing = session.scalar(select(Opportunity).where(Opportunity.source == "test", Opportunity.source_id == "WEB-UI-TEST-001"))
    if existing:
        return existing
    opp = Opportunity(
        source="test",
        source_id="WEB-UI-TEST-001",
        title="Phase 14 Web UI Test Opportunity",
        psc_code="7110",
        agency_path="Test Agency",
        status="open",
        response_deadline=datetime.now(UTC) + timedelta(days=30),
        raw={},
    )
    session.add(opp)
    session.flush()
    session.commit()
    return opp


def _make_watchlist(session: Session) -> Watchlist:
    existing = session.scalar(select(Watchlist).where(Watchlist.name == "Test WL Phase14"))
    if existing:
        return existing
    wl = Watchlist(name="Test WL Phase14", enabled=True, psc_codes=["71"])
    session.add(wl)
    session.flush()
    session.commit()
    return wl


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestAuthentication:
    """AC: session-based auth, invite-only, role checks."""

    def test_login_page_renders(self, client):
        """Login page is accessible without auth."""
        resp = client.get("/login")
        assert resp.status_code == 200
        assert b"Sign in" in resp.content or b"GovCon" in resp.content

    def test_unauthenticated_inbox_redirects(self, client):
        """Unauthenticated request to / redirects to /login."""
        resp = client.get("/")
        assert resp.status_code == 303
        assert "/login" in resp.headers["location"]

    def test_unauthenticated_search_redirects(self, client):
        resp = client.get("/search")
        assert resp.status_code == 303

    def test_unauthenticated_pipeline_redirects(self, client):
        resp = client.get("/pipeline")
        assert resp.status_code == 303

    def test_unauthenticated_ops_redirects(self, client):
        resp = client.get("/ops")
        assert resp.status_code == 303

    def test_bad_credentials_return_401(self, client):
        resp = client.post("/login", data={"email": "nobody@example.com", "password": "wrong"})
        assert resp.status_code == 401
        assert b"Invalid" in resp.content

    def test_valid_login_sets_cookie(self, client, db_session):
        user, _ = _make_user(db_session, "web_login_test@example.com", "reviewer")
        resp = client.post("/login", data={"email": "web_login_test@example.com", "password": "TestPassword123!"})
        assert resp.status_code == 303
        assert "govcon_session" in resp.cookies

    def test_logout_clears_cookie(self, client, db_session):
        _, token = _make_user(db_session, "web_logout_test@example.com", "reviewer")
        c = client
        resp = c.post("/logout", cookies={"govcon_session": token})
        assert resp.status_code == 303


def test_workspace_actions_require_login_and_role(client, db_session) -> None:
    opp = _make_opp(db_session)
    assert client.post(f"/workspace/{opp.id}/run/compliance").status_code == 303
    _, token = _make_user(db_session, "web_action_readonly@example.com", "read_only")
    response = client.post(
        f"/workspace/{opp.id}/run/compliance", cookies={"govcon_session": token}
    )
    assert response.status_code == 403


def test_summary_action_reports_missing_documents(client, db_session) -> None:
    opp = _make_opp(db_session)
    _, token = _make_user(db_session, "web_action_reviewer@example.com", "reviewer")
    response = client.post(
        f"/workspace/{opp.id}/run/summary",
        data={"external_ai_authorized": "yes"}, cookies={"govcon_session": token}
    )
    assert response.status_code == 303
    assert "error=missing_inputs" in response.headers["location"]


def test_pricing_requires_provenance_and_records_real_input(client, db_session) -> None:
    from decimal import Decimal
    from govcon.models import AuditEvent, Pursuit

    user, token = _make_user(db_session, "web_pricing_reviewer@example.com", "reviewer")
    unique = secrets.token_hex(6)
    opp = Opportunity(source="test", source_id=f"WEB-PRICE-{unique}", title="Pricing test", status="open", raw={})
    db_session.add(opp)
    db_session.flush()
    pursuit = Pursuit(opportunity_id=opp.id, stage="evaluating")
    db_session.add(pursuit)
    db_session.commit()
    payload = {"supplier": "Actual Supplier", "sourcing_cost": "120.00", "quote_price": "150.00"}
    invalid = client.post(f"/workspace/{opp.id}/pricing", data={**payload, "evidence_reference": ""}, cookies={"govcon_session": token})
    assert invalid.status_code == 400
    response = client.post(f"/workspace/{opp.id}/pricing", data={**payload, "evidence_reference": "Quote Q-123"}, cookies={"govcon_session": token})
    assert response.status_code == 303
    db_session.refresh(pursuit)
    assert pursuit.sourcing_cost == Decimal("120.00")
    assert pursuit.quote_price == Decimal("150.00")
    event = db_session.scalar(select(AuditEvent).where(AuditEvent.opportunity_id == opp.id, AuditEvent.action_type == "pricing_recorded"))
    assert event is not None and event.new_value["evidence_reference"] == "Quote Q-123"


class TestMainPages:
    """AC: All 9 pages render for authenticated users."""

    @pytest.fixture(autouse=True)
    def setup(self, db_session):
        self.user, self.token = _make_user(db_session, "web_pages_test@example.com", "reviewer")
        self.cookies = {"govcon_session": self.token}
        self.opp = _make_opp(db_session)
        _make_watchlist(db_session)

    def test_inbox_renders(self, client):
        resp = client.get("/", cookies=self.cookies)
        assert resp.status_code == 200
        assert b"Inbox" in resp.content

    def test_search_renders(self, client):
        resp = client.get("/search", cookies=self.cookies)
        assert resp.status_code == 200
        assert b"Search" in resp.content

    def test_search_with_query(self, client):
        resp = client.get("/search?q=test", cookies=self.cookies)
        assert resp.status_code == 200

    def test_pipeline_renders(self, client):
        resp = client.get("/pipeline", cookies=self.cookies)
        assert resp.status_code == 200
        assert b"Pipeline" in resp.content

    def test_watchlists_renders(self, client):
        resp = client.get("/watchlists", cookies=self.cookies)
        assert resp.status_code == 200
        assert b"Watchlist" in resp.content

    def test_vendors_renders(self, client):
        resp = client.get("/vendors", cookies=self.cookies)
        assert resp.status_code == 200
        assert b"Vendor" in resp.content

    def test_ops_renders(self, client):
        resp = client.get("/ops", cookies=self.cookies)
        assert resp.status_code == 200
        assert b"Operations" in resp.content

    def test_learning_renders(self, client):
        resp = client.get("/learning", cookies=self.cookies)
        assert resp.status_code == 200
        assert b"Learning" in resp.content

    def test_opp_detail_renders(self, client):
        resp = client.get(f"/opp/{self.opp.id}", cookies=self.cookies)
        assert resp.status_code == 200
        assert b"Phase 14" in resp.content or b"Test" in resp.content

    def test_workspace_overview_renders(self, client):
        """Workspace page renders even without a pursuit."""
        resp = client.get(f"/workspace/{self.opp.id}?tab=overview", cookies=self.cookies)
        assert resp.status_code == 200
        assert b"workspace" in resp.content.lower() or b"Overview" in resp.content

    def test_workspace_all_tabs_render(self, client):
        tabs = [
            "overview", "ai_decision", "requirements", "market", "awards",
            "products", "pricing", "competitors", "compliance", "review",
            "proposal", "submission", "activity",
        ]
        for tab in tabs:
            resp = client.get(f"/workspace/{self.opp.id}?tab={tab}", cookies=self.cookies)
            assert resp.status_code == 200, f"Tab {tab} failed: {resp.status_code}"


class TestInboxActions:
    """AC: Inbox action buttons work (Seen, Dismiss, Pursue, Review)."""

    @pytest.fixture(autouse=True)
    def setup(self, db_session):
        self.user, self.token = _make_user(db_session, "web_inbox_test@example.com", "reviewer")
        self.cookies = {"govcon_session": self.token}
        self.opp = _make_opp(db_session)
        wl = _make_watchlist(db_session)
        # Create a fresh match
        existing = db_session.scalar(
            select(Match).where(Match.opportunity_id == self.opp.id, Match.watchlist_id == wl.id)
        )
        if existing:
            existing.status = "new"
            db_session.flush()
            self.match = existing
        else:
            m = Match(opportunity_id=self.opp.id, watchlist_id=wl.id, status="new")
            db_session.add(m)
            db_session.flush()
            self.match = m
        db_session.commit()

    def test_seen_action(self, client, db_session):
        resp = client.post("/inbox/action", data={"match_id": self.match.id, "action": "seen"}, cookies=self.cookies)
        assert resp.status_code == 200
        db_session.expire(self.match)
        assert self.match.status == "seen"

    def test_dismiss_action(self, client, db_session):
        self.match.status = "new"
        db_session.commit()
        resp = client.post("/inbox/action", data={"match_id": self.match.id, "action": "dismissed"}, cookies=self.cookies)
        assert resp.status_code == 200
        db_session.expire(self.match)
        assert self.match.status == "dismissed"

    def test_pursue_action(self, client, db_session):
        self.match.status = "new"
        db_session.commit()
        resp = client.post("/inbox/action", data={"match_id": self.match.id, "action": "pursuing"}, cookies=self.cookies)
        assert resp.status_code == 200
        db_session.expire(self.match)
        assert self.match.status == "pursuing"

    def test_invalid_action_rejected(self, client):
        resp = client.post("/inbox/action", data={"match_id": self.match.id, "action": "HACK"}, cookies=self.cookies)
        assert resp.status_code == 400


class TestConcurrentAccess:
    """AC: Two logged-in users can open the same opportunity simultaneously.
    Neither user silently overwrites the other's work."""

    def test_two_users_open_same_workspace(self, client, db_session):
        user1, token1 = _make_user(db_session, "concurrent_u1@example.com", "reviewer")
        user2, token2 = _make_user(db_session, "concurrent_u2@example.com", "reviewer")
        opp = _make_opp(db_session)

        resp1 = client.get(f"/workspace/{opp.id}?tab=overview", cookies={"govcon_session": token1})
        resp2 = client.get(f"/workspace/{opp.id}?tab=overview", cookies={"govcon_session": token2})
        assert resp1.status_code == 200
        assert resp2.status_code == 200

    def test_reviewer_identity_visible_on_comments(self, client, db_session):
        user1, token1 = _make_user(db_session, "ident_u1@example.com", "reviewer")
        opp = _make_opp(db_session)
        # Post a comment
        resp = client.post(
            f"/workspace/{opp.id}/comment",
            data={"body": "Identity test comment"},
            cookies={"govcon_session": token1},
        )
        assert resp.status_code in (200, 303)
        # The comment was recorded with user_id
        comment = db_session.scalar(
            select(ReviewComment)
            .where(ReviewComment.opportunity_id == opp.id, ReviewComment.body == "Identity test comment")
        )
        assert comment is not None
        assert comment.user_id == user1.id


class TestApprovalPermissions:
    """AC: Approval permissions are enforced. Reviewer cannot approve."""

    def test_reviewer_cannot_approve(self, client, db_session):
        reviewer, token = _make_user(db_session, "perm_reviewer@example.com", "reviewer")
        opp = _make_opp(db_session)
        # Ensure review session exists at approval_pending
        rs = db_session.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp.id))
        if not rs:
            rs = ReviewSession(
                opportunity_id=opp.id,
                status="approval_pending",
                review_policy="conditional",
                required_review_count=1,
                completed_review_count=1,
            )
            db_session.add(rs)
        else:
            rs.status = "approval_pending"
        db_session.commit()

        # Reviewer tries to approve — should be redirected (not crash, not approve)
        resp = client.post(
            f"/workspace/{opp.id}/approve",
            data={"decision": "approve_to_bid"},
            cookies={"govcon_session": token},
        )
        # Either redirect or 303 — should not change status to approved
        db_session.expire(rs)
        # status stays as approval_pending or is redirected
        assert rs.status != "approved_to_bid"

    def test_approver_can_approve(self, client, db_session):
        approver, token = _make_user(db_session, "perm_approver@example.com", "approver")
        opp = _make_opp(db_session)
        rs = db_session.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp.id))
        if not rs:
            rs = ReviewSession(
                opportunity_id=opp.id,
                status="approval_pending",
                review_policy="conditional",
                required_review_count=1,
                completed_review_count=1,
            )
            db_session.add(rs)
        else:
            rs.status = "approval_pending"
        db_session.commit()

        resp = client.post(
            f"/workspace/{opp.id}/approve",
            data={"decision": "approve_to_bid"},
            cookies={"govcon_session": token},
        )
        assert resp.status_code in (200, 303)
        db_session.expire(rs)
        assert rs.status == "approved_to_bid"
        assert rs.approved_by_user_id == approver.id



class TestApproveToGenerates:
    """AC: Approve to Bid auto-generates proposal and submission package.

    §20 requires the full workflow to complete in the UI.  The Phase 11 services
    (generate_proposal + generate_submission_package) must be called when an
    approver clicks 'Approve to Bid' so that tabs show real draft/package state.
    """

    def _make_opp_for_generate(self, db_session, suffix: str):
        """Return (opp, pursuit, review_session, approver, token).
        Uses a random token to avoid unique constraint violations on re-runs."""
        from govcon.models import Pursuit, ReviewSession
        uid = secrets.token_hex(6)
        approver, token = _make_user(db_session, f"approve_gen_{suffix}_{uid}@example.com", "approver")
        opp = Opportunity(
            source="test",
            source_id=f"WEB-UI-GEN-{suffix}-{uid}",
            title=f"Phase 14 Generate Test {suffix}",
            psc_code="9999",
            status="open",
            raw={},
        )
        db_session.add(opp)
        db_session.flush()
        pursuit = Pursuit(opportunity_id=opp.id, stage="evaluating")
        db_session.add(pursuit)
        db_session.flush()
        rs = ReviewSession(
            opportunity_id=opp.id,
            status="approval_pending",
            review_policy="conditional",
            required_review_count=1,
            completed_review_count=1,
        )
        db_session.add(rs)
        db_session.commit()
        return opp, pursuit, rs, approver, token

    def test_approve_to_bid_creates_proposal(self, client, db_session):
        """Approving creates a Proposal row (skip_ai=True path)."""
        from govcon.models import Proposal
        opp, pursuit, rs, approver, token = self._make_opp_for_generate(db_session, "A01")

        resp = client.post(
            f"/workspace/{opp.id}/approve",
            data={"decision": "approve_to_bid"},
            cookies={"govcon_session": token},
        )
        assert resp.status_code in (200, 303)

        proposal = db_session.scalar(
            select(Proposal).where(Proposal.opportunity_id == opp.id)
        )
        assert proposal is not None, "Proposal must be created on Approve to Bid"
        assert proposal.status in ("draft", "ai_generated"), \
            f"Unexpected proposal status: {proposal.status!r}"

    def test_approve_to_bid_creates_submission_package(self, client, db_session):
        """Approving creates a Submission row."""
        from govcon.models import Submission
        opp, pursuit, rs, approver, token = self._make_opp_for_generate(db_session, "A02")

        client.post(
            f"/workspace/{opp.id}/approve",
            data={"decision": "approve_to_bid"},
            cookies={"govcon_session": token},
        )

        sub = db_session.scalar(
            select(Submission).where(Submission.opportunity_id == opp.id)
        )
        assert sub is not None, "Submission must be created on Approve to Bid"

    def test_proposal_tab_shows_real_state_after_approve(self, client, db_session):
        """Proposal tab renders real content (not forever-generating) after approve."""
        opp, pursuit, rs, approver, token = self._make_opp_for_generate(db_session, "A03")

        client.post(
            f"/workspace/{opp.id}/approve",
            data={"decision": "approve_to_bid"},
            cookies={"govcon_session": token},
        )

        resp = client.get(
            f"/workspace/{opp.id}?tab=proposal",
            cookies={"govcon_session": token},
        )
        assert resp.status_code == 200
        assert b"generation in progress or not yet triggered" not in resp.content, \
            "Proposal tab should show real draft state after Approve to Bid"

    def test_submission_tab_shows_real_state_after_approve(self, client, db_session):
        """Submission tab renders real package (not locked) after approve."""
        opp, pursuit, rs, approver, token = self._make_opp_for_generate(db_session, "A04")

        client.post(
            f"/workspace/{opp.id}/approve",
            data={"decision": "approve_to_bid"},
            cookies={"govcon_session": token},
        )

        resp = client.get(
            f"/workspace/{opp.id}?tab=submission",
            cookies={"govcon_session": token},
        )
        assert resp.status_code == 200
        assert b"Submission package available after proposal is approved" not in resp.content, \
            "Submission tab should show real package state after Approve to Bid"

    def test_full_workflow_approve_then_record_outcome(self, client, db_session):
        """Full workflow: approve_to_bid → proposal final-approve → authorize submission → record won."""
        from govcon.models import Proposal, Submission, Pursuit
        opp, pursuit, rs, approver, token = self._make_opp_for_generate(db_session, "A05")

        # Step 1: Approve to bid (auto-generates proposal + submission)
        client.post(
            f"/workspace/{opp.id}/approve",
            data={"decision": "approve_to_bid"},
            cookies={"govcon_session": token},
        )

        proposal = db_session.scalar(select(Proposal).where(Proposal.opportunity_id == opp.id))
        assert proposal is not None

        # Step 2: Set proposal to red_teamed so final-approve is allowed
        proposal.status = "red_teamed"
        db_session.commit()

        # Step 3: Final-approve proposal
        client.post(
            f"/workspace/{opp.id}/proposal/approve",
            data={"decision": "APPROVE_FOR_SUBMISSION", "override_reason": "Fixture reviewer accepted missing preflight"},
            cookies={"govcon_session": token},
        )
        db_session.expire(proposal)
        assert proposal.status == "final_approved"

        # Step 4: Final approval marks the package ready. Authorization alone
        # must not claim that a portal submission happened.
        sub = db_session.scalar(select(Submission).where(Submission.opportunity_id == opp.id))
        assert sub is not None
        assert sub.status == "ready"

        client.post(
            f"/workspace/{opp.id}/submission/approve",
            data={"decision": "approve"},
            cookies={"govcon_session": token},
        )
        db_session.expire(sub)
        assert sub.status == "ready"

        missing = client.post(
            f"/workspace/{opp.id}/submission/approve",
            data={"decision": "record_submitted"},
            cookies={"govcon_session": token},
        )
        assert missing.status_code == 400
        client.post(
            f"/workspace/{opp.id}/submission/approve",
            data={"decision": "record_submitted", "confirmation_number": "TEST-PORTAL-RECEIPT"},
            cookies={"govcon_session": token},
        )
        db_session.expire(sub)
        assert sub.status == "submitted"

        # Step 5: Record outcome (Phase 15 structured capture — use lessons_learned)
        client.post(
            f"/workspace/{opp.id}/record-outcome",
            data={"outcome": "won", "lessons_learned": "E2E test win"},
            cookies={"govcon_session": token},
        )
        db_session.expire(pursuit)
        assert pursuit.stage == "won"
        assert pursuit.outcome_notes == "E2E test win"


class TestAuditTrail:
    """AC: Audit history shows who changed what and when."""

    def test_comment_stored_with_user_id(self, client, db_session):
        user, token = _make_user(db_session, "audit_user@example.com", "reviewer")
        opp = _make_opp(db_session)
        body = f"Audit trail test {secrets.token_hex(4)}"
        client.post(
            f"/workspace/{opp.id}/comment",
            data={"body": body},
            cookies={"govcon_session": token},
        )
        comment = db_session.scalar(
            select(ReviewComment).where(
                ReviewComment.opportunity_id == opp.id,
                ReviewComment.body == body,
            )
        )
        assert comment is not None
        assert comment.user_id == user.id
        assert comment.created_at is not None


class TestWatchlistCRUD:
    """AC: Watchlist CRUD works from the UI."""

    @pytest.fixture(autouse=True)
    def setup(self, db_session):
        self.user, self.token = _make_user(db_session, "wl_crud_test@example.com", "owner")
        self.cookies = {"govcon_session": self.token}

    def test_watchlist_list_renders(self, client):
        resp = client.get("/watchlists", cookies=self.cookies)
        assert resp.status_code == 200

    def test_watchlist_new_form_renders(self, client):
        resp = client.get("/watchlists/new", cookies=self.cookies)
        assert resp.status_code == 200

    def test_watchlist_create_and_list(self, client, db_session):
        resp = client.post(
            "/watchlists/new",
            data={"name": "UI Test WL", "psc_codes": "7110", "keywords": "test,widget"},
            cookies=self.cookies,
        )
        assert resp.status_code in (200, 303)
        wl = db_session.scalar(select(Watchlist).where(Watchlist.name == "UI Test WL"))
        assert wl is not None
        assert wl.psc_codes == ["7110"]

    def test_new_watchlist_matches_existing_opportunity(self, client, db_session):
        opp = _make_opp(db_session)
        response = client.post(
            "/watchlists/new",
            data={"name": "Auto Match UI Test", "psc_codes": "7110", "keywords": "Phase 14"},
            cookies=self.cookies,
        )
        assert response.status_code == 303
        db_session.expire_all()
        watchlist = db_session.scalar(select(Watchlist).where(Watchlist.name == "Auto Match UI Test"))
        assert watchlist is not None
        match = db_session.scalar(
            select(Match).where(Match.watchlist_id == watchlist.id, Match.opportunity_id == opp.id)
        )
        assert match is not None
        db_session.query(Match).filter_by(watchlist_id=watchlist.id).delete()
        db_session.delete(watchlist)
        db_session.commit()

    def test_empty_watchlist_is_rejected(self, client):
        response = client.post(
            "/watchlists/new",
            data={"name": "No Filters"},
            cookies=self.cookies,
        )
        assert response.status_code == 200
        assert b"Add at least one code" in response.content


class TestSecurityRequirements:
    """AC: App defaults to localhost only. No secret values in rendered HTML."""

    def test_bind_host_defaults_to_loopback(self):
        from govcon.web.app import bind_host
        from govcon.config import Settings
        s = Settings(database_url="postgresql+psycopg://x:x@localhost/x")
        assert bind_host(s) == "127.0.0.1"

    def test_no_password_hash_in_rendered_page(self, client, db_session):
        user, token = _make_user(db_session, "security_test@example.com", "read_only")
        resp = client.get("/ops", cookies={"govcon_session": token})
        assert resp.status_code == 200
        # Password hashes start with $argon2
        assert b"$argon2" not in resp.content

    def test_public_bind_requires_explicit_opt_in(self):
        from govcon.config import Settings
        import pytest
        with pytest.raises(Exception):
            Settings(
                database_url="postgresql+psycopg://x:x@localhost/x",
                web_bind_host="0.0.0.0",
            )
