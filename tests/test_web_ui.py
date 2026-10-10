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

import secrets
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from web_client import CsrfTestClient as TestClient
from web_client import page_containing

from govcon.models import (
    Match,
    Opportunity,
    ReviewComment,
    ReviewSession,
    User,
    UserSession,
    Watchlist,
)
from govcon.tasks.testing import drain
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
    from govcon.collaboration.users import create_session, hash_password
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


def _fresh_opp(session: Session, tag: str) -> Opportunity:
    opp = Opportunity(
        source="test",
        source_id=f"WEB-UI-{tag}-{secrets.token_hex(6)}",
        title=f"Web gate test {tag}",
        psc_code="9999",
        agency_path="Test Agency",
        status="open",
        response_deadline=datetime.now(UTC) + timedelta(days=30),
        raw={},
    )
    session.add(opp)
    session.flush()
    session.commit()
    return opp


def _assign(session: Session, opp_id: int, user: User) -> None:
    from govcon.collaboration.assignments import assign_reviewer
    from govcon.collaboration.review_sessions import ensure_review_session

    ensure_review_session(session, opportunity_id=opp_id)
    assign_reviewer(session, opportunity_id=opp_id, user_id=user.id)
    session.commit()


def _complete_review(session: Session, opp_id: int, tag: str) -> User:
    """A real reviewer completes a real assignment through the quorum service."""
    from govcon.collaboration.comments import add_comment
    from govcon.collaboration.review_sessions import complete_assignment

    reviewer, _ = _make_user(session, f"rev_{tag}_{secrets.token_hex(4)}@example.com", "reviewer")
    _assign(session, opp_id, reviewer)
    add_comment(
        session,
        opportunity_id=opp_id,
        user_id=reviewer.id,
        body="Pricing and delivery reviewed against the solicitation; acceptable to proceed.",
        validate_with_ai=False,
    )
    complete_assignment(
        session,
        opportunity_id=opp_id,
        user_id=reviewer.id,
        action="approve_continue",
        recommendation="bid",
    )
    session.commit()
    return reviewer


def _review_session(session: Session, opp_id: int) -> ReviewSession:
    rs = session.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp_id))
    assert rs is not None
    session.refresh(rs)
    return rs


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
        _user, _ = _make_user(db_session, "web_login_test@example.com", "reviewer")
        resp = client.post("/login", data={"email": "web_login_test@example.com", "password": "TestPassword123!"})
        assert resp.status_code == 303
        assert "govcon_session" in resp.cookies

    def test_logout_clears_cookie(self, client, db_session):
        _, token = _make_user(db_session, "web_logout_test@example.com", "reviewer")
        c = client
        resp = c.post("/logout", cookies={"govcon_session": token})
        assert resp.status_code == 303


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
        assert resp.status_code == 303
        db_session.expire(self.match)
        assert self.match.status == "seen"

    def test_dismiss_action(self, client, db_session):
        self.match.status = "new"
        db_session.commit()
        resp = client.post("/inbox/action", data={"match_id": self.match.id, "action": "dismissed"}, cookies=self.cookies)
        assert resp.status_code == 303
        db_session.expire(self.match)
        assert self.match.status == "dismissed"

    def test_pursue_action(self, client, db_session):
        self.match.status = "new"
        db_session.commit()
        resp = client.post("/inbox/action", data={"match_id": self.match.id, "action": "pursuing"}, cookies=self.cookies)
        assert resp.status_code == 303
        db_session.expire(self.match)
        assert self.match.status == "pursuing"

    def test_invalid_action_rejected(self, client):
        resp = client.post("/inbox/action", data={"match_id": self.match.id, "action": "HACK"}, cookies=self.cookies)
        assert resp.status_code == 400


class TestConcurrentAccess:
    """AC: Two logged-in users can open the same opportunity simultaneously.
    Neither user silently overwrites the other's work."""

    def test_two_users_open_same_workspace(self, client, db_session):
        _user1, token1 = _make_user(db_session, "concurrent_u1@example.com", "reviewer")
        _user2, token2 = _make_user(db_session, "concurrent_u2@example.com", "reviewer")
        opp = _make_opp(db_session)

        resp1 = client.get(f"/workspace/{opp.id}?tab=overview", cookies={"govcon_session": token1})
        resp2 = client.get(f"/workspace/{opp.id}?tab=overview", cookies={"govcon_session": token2})
        assert resp1.status_code == 200
        assert resp2.status_code == 200

    def test_reviewer_identity_visible_on_comments(self, client, db_session):
        user1, token1 = _make_user(db_session, "ident_u1@example.com", "reviewer")
        opp = _make_opp(db_session)
        _assign(db_session, opp.id, user1)
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
        _reviewer, token = _make_user(db_session, "perm_reviewer@example.com", "reviewer")
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
        client.post(
            f"/workspace/{opp.id}/approve",
            data={"decision": "approve_to_bid", "expected_version": rs.version},
            cookies={"govcon_session": token},
        )
        # Either redirect or 303 — should not change status to approved
        db_session.expire(rs)
        # status stays as approval_pending or is redirected
        assert rs.status != "approved_to_bid"

    def test_approver_can_approve(self, client, db_session):
        approver, token = _make_user(db_session, "perm_approver@example.com", "approver")
        opp = _fresh_opp(db_session, "PERM")
        _complete_review(db_session, opp.id, "perm")
        rs = _review_session(db_session, opp.id)
        assert rs.status == "approval_pending"

        resp = client.post(
            f"/workspace/{opp.id}/approve",
            data={"decision": "approve_to_bid", "expected_version": rs.version},
            cookies={"govcon_session": token},
        )
        assert resp.status_code in (200, 303)
        db_session.expire(rs)
        assert rs.status == "approved_to_bid"
        assert rs.approved_by_user_id == approver.id



class TestApproveToGenerates:
    """AC: Approve to Bid auto-generates proposal and submission package.

    §20 requires the full workflow to complete in the UI. Approving to bid
    queues a durable generation task (ADR-062) that a worker runs; these
    tests run it in-process with ``drain`` before checking the tabs.
    """

    def _make_opp_for_generate(self, db_session, suffix: str):
        """Return (opp, pursuit, review_session, approver, token).
        Uses a random token to avoid unique constraint violations on re-runs."""
        from govcon.models import Pursuit
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
        db_session.commit()
        _complete_review(db_session, opp.id, f"gen{suffix}")
        rs = _review_session(db_session, opp.id)
        assert rs.status == "approval_pending"
        return opp, pursuit, rs, approver, token

    def test_approve_to_bid_creates_proposal(self, client, db_session):
        """Approving creates a Proposal row (skip_ai=True path)."""
        from govcon.models import Proposal
        opp, _pursuit, rs, _approver, token = self._make_opp_for_generate(db_session, "A01")

        resp = client.post(
            f"/workspace/{opp.id}/approve",
            data={"decision": "approve_to_bid", "expected_version": rs.version},
            cookies={"govcon_session": token},
        )
        drain(opportunity_id=opp.id)
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
        opp, _pursuit, rs, _approver, token = self._make_opp_for_generate(db_session, "A02")

        client.post(
            f"/workspace/{opp.id}/approve",
            data={"decision": "approve_to_bid", "expected_version": rs.version},
            cookies={"govcon_session": token},
        )
        drain(opportunity_id=opp.id)

        sub = db_session.scalar(
            select(Submission).where(Submission.opportunity_id == opp.id)
        )
        assert sub is not None, "Submission must be created on Approve to Bid"

    def test_proposal_tab_shows_real_state_after_approve(self, client, db_session):
        """Proposal tab renders real content (not forever-generating) after approve."""
        opp, _pursuit, rs, _approver, token = self._make_opp_for_generate(db_session, "A03")

        client.post(
            f"/workspace/{opp.id}/approve",
            data={"decision": "approve_to_bid", "expected_version": rs.version},
            cookies={"govcon_session": token},
        )
        drain(opportunity_id=opp.id)

        resp = client.get(
            f"/workspace/{opp.id}?tab=proposal",
            cookies={"govcon_session": token},
        )
        assert resp.status_code == 200
        assert b"generation in progress or not yet triggered" not in resp.content, \
            "Proposal tab should show real draft state after Approve to Bid"

    def test_submission_tab_shows_real_state_after_approve(self, client, db_session):
        """Submission tab renders real package (not locked) after approve."""
        opp, _pursuit, rs, _approver, token = self._make_opp_for_generate(db_session, "A04")

        client.post(
            f"/workspace/{opp.id}/approve",
            data={"decision": "approve_to_bid", "expected_version": rs.version},
            cookies={"govcon_session": token},
        )
        drain(opportunity_id=opp.id)

        resp = client.get(
            f"/workspace/{opp.id}?tab=submission",
            cookies={"govcon_session": token},
        )
        assert resp.status_code == 200
        assert b"Submission package available after proposal is approved" not in resp.content, \
            "Submission tab should show real package state after Approve to Bid"

    def test_full_workflow_approve_then_record_outcome(self, client, db_session, tmp_path):
        """Full workflow: approve_to_bid → proposal final-approve → authorize submission → record won."""
        from govcon.models import Proposal, Submission
        opp, pursuit, rs, approver, token = self._make_opp_for_generate(db_session, "A05")

        # Step 1: Approve to bid (auto-generates proposal + submission)
        client.post(
            f"/workspace/{opp.id}/approve",
            data={"decision": "approve_to_bid", "expected_version": rs.version},
            cookies={"govcon_session": token},
        )
        drain(opportunity_id=opp.id)

        proposal = db_session.scalar(select(Proposal).where(Proposal.opportunity_id == opp.id))
        assert proposal is not None
        db_session.refresh(proposal)
        assert proposal.status == "ai_generated"

        # Step 2: Final approval goes through the compliance gate. No pre-flight
        # has run, so it is refused unless the approver gives their own reason.
        resp = client.post(
            f"/workspace/{opp.id}/proposal/approve",
            data={"decision": "APPROVE_FOR_SUBMISSION", "expected_version": proposal.version},
            cookies={"govcon_session": token},
        )
        assert "error=" in resp.headers["location"]
        db_session.expire(proposal)
        assert proposal.status == "ai_generated"

        from govcon.compliance.deterministic import PackageFile, SubmissionPackage
        from govcon.compliance.submission_preflight import run_submission_preflight
        from govcon.proposals.export import export_proposal_docx
        from govcon.submissions.manifest import assemble_package
        sub = db_session.scalar(select(Submission).where(Submission.opportunity_id == opp.id))
        opp.response_deadline = sub.submission_deadline = datetime.now(UTC) + timedelta(days=7)
        db_session.flush()
        artifact = tmp_path / "proposal.docx"
        artifact.write_bytes(export_proposal_docx(db_session, proposal_version_id=proposal.current_version_id))
        package = SubmissionPackage(files=[PackageFile("proposal.docx", role="proposal", local_path=str(artifact))], proposal_version_id=proposal.current_version_id)
        assemble_package(db_session, opportunity_id=opp.id, package=package, actor=approver)
        run_submission_preflight(db_session, opp.id, package)
        db_session.commit()

        response = client.post(
            f"/workspace/{opp.id}/proposal/approve",
            data={
                "decision": "APPROVE_FOR_SUBMISSION",
                "expected_version": proposal.version,
                "override_reason": "Pre-flight reviewed manually; package verified by the approver.",
            },
            cookies={"govcon_session": token},
        )
        assert "error=" not in response.headers.get("location", ""), response.headers.get("location")
        db_session.expire(proposal)
        assert proposal.status == "final_approved"
        assert proposal.approved_version_id == proposal.current_version_id

        # Step 3: The submission is ready; the human records the manual submission.
        sub = db_session.scalar(select(Submission).where(Submission.opportunity_id == opp.id))
        assert sub is not None
        db_session.refresh(sub)
        assert sub.status == "ready"

        client.post(
            f"/workspace/{opp.id}/submission/approve",
            data={"expected_version": sub.version, "confirmation_number": "E2E-CONF-1"},
            cookies={"govcon_session": token},
        )
        db_session.expire(sub)
        assert sub.status == "submitted"
        db_session.expire(pursuit)
        assert pursuit.stage == "submitted"

        # Step 5: Record outcome (Phase 15 structured capture — use lessons_learned)
        client.post(
            f"/workspace/{opp.id}/record-outcome",
            data={"outcome": "won", "lessons_learned": "E2E test win"},
            cookies={"govcon_session": token},
        )
        db_session.expire(pursuit)
        assert pursuit.stage == "won"
        assert pursuit.outcome_notes == "E2E test win"


class TestWorkflowGates:
    """C1: every mutating route goes through its service gate."""

    def test_read_only_user_cannot_complete_review(self, client, db_session):
        _viewer, token = _make_user(db_session, f"ro_{secrets.token_hex(4)}@example.com", "read_only")
        opp = _fresh_opp(db_session, "RO")
        from govcon.collaboration.review_sessions import ensure_review_session

        ensure_review_session(db_session, opportunity_id=opp.id)
        db_session.commit()
        resp = client.post(
            f"/workspace/{opp.id}/complete-review",
            data={"recommendation": "bid", "agree_with_ai_assessment": "on"},
            cookies={"govcon_session": token},
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        rs = _review_session(db_session, opp.id)
        assert rs.completed_review_count == 0

    def test_unassigned_reviewer_cannot_complete_review(self, client, db_session):
        _reviewer, token = _make_user(db_session, f"na_{secrets.token_hex(4)}@example.com", "reviewer")
        opp = _fresh_opp(db_session, "NA")
        from govcon.collaboration.review_sessions import ensure_review_session

        ensure_review_session(db_session, opportunity_id=opp.id)
        db_session.commit()
        for _ in range(2):
            resp = client.post(
                f"/workspace/{opp.id}/complete-review",
                data={"recommendation": "bid", "agree_with_ai_assessment": "on"},
                cookies={"govcon_session": token},
            )
            assert "error=" in resp.headers["location"]
        rs = _review_session(db_session, opp.id)
        assert rs.completed_review_count == 0
        assert rs.status != "approval_pending"

    def test_repeated_complete_review_counts_once(self, client, db_session):
        from govcon.collaboration.review_sessions import set_review_policy

        approver, _ = _make_user(db_session, f"ap_{secrets.token_hex(4)}@example.com", "approver")
        reviewer, token = _make_user(db_session, f"rp_{secrets.token_hex(4)}@example.com", "reviewer")
        opp = _fresh_opp(db_session, "REP")
        _assign(db_session, opp.id, reviewer)
        set_review_policy(db_session, opportunity_id=opp.id, review_policy="dual", actor=approver)
        db_session.commit()
        for _ in range(3):
            client.post(
                f"/workspace/{opp.id}/complete-review",
                data={"recommendation": "bid", "agree_with_ai_assessment": "on"},
                cookies={"govcon_session": token},
            )
        rs = _review_session(db_session, opp.id)
        assert rs.completed_review_count == 1
        assert rs.required_review_count == 2
        assert rs.status not in {"approval_pending", "review_complete", "approved_to_bid"}

    def test_approval_with_unmet_quorum_is_refused(self, client, db_session):
        _approver, token = _make_user(db_session, f"uq_{secrets.token_hex(4)}@example.com", "approver")
        opp = _fresh_opp(db_session, "UQ")
        from govcon.collaboration.review_sessions import ensure_review_session

        rs = ensure_review_session(db_session, opportunity_id=opp.id)
        db_session.commit()
        resp = client.post(
            f"/workspace/{opp.id}/approve",
            data={"decision": "approve_to_bid", "expected_version": rs.version},
            cookies={"govcon_session": token},
        )
        assert "error=" in resp.headers["location"]
        rs = _review_session(db_session, opp.id)
        assert rs.status != "approved_to_bid"
        assert rs.final_approval_status is None

    def test_stale_version_is_refused(self, client, db_session):
        _approver, token = _make_user(db_session, f"sv_{secrets.token_hex(4)}@example.com", "approver")
        opp = _fresh_opp(db_session, "SV")
        _complete_review(db_session, opp.id, "sv")
        rs = _review_session(db_session, opp.id)
        resp = client.post(
            f"/workspace/{opp.id}/approve",
            data={"decision": "approve_to_bid", "expected_version": rs.version - 1},
            cookies={"govcon_session": token},
        )
        assert "error=" in resp.headers["location"]
        rs = _review_session(db_session, opp.id)
        assert rs.status == "approval_pending"

    def test_approval_is_audited(self, client, db_session):
        from govcon.models import AuditEvent

        approver, token = _make_user(db_session, f"au_{secrets.token_hex(4)}@example.com", "approver")
        opp = _fresh_opp(db_session, "AU")
        _complete_review(db_session, opp.id, "au")
        rs = _review_session(db_session, opp.id)
        client.post(
            f"/workspace/{opp.id}/approve",
            data={"decision": "approve_to_bid", "expected_version": rs.version},
            cookies={"govcon_session": token},
        )
        events = db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.opportunity_id == opp.id,
                AuditEvent.action_type == "review_final_approval_set",
            )
        ).all()
        assert events and events[-1].user_id == approver.id

    def test_submission_cannot_be_recorded_without_final_approval(self, client, db_session):
        from govcon.models import Pursuit, Submission

        _approver, token = _make_user(db_session, f"sb_{secrets.token_hex(4)}@example.com", "approver")
        opp = _fresh_opp(db_session, "SB")
        db_session.add(Pursuit(opportunity_id=opp.id, stage="evaluating"))
        db_session.commit()
        _complete_review(db_session, opp.id, "sb")
        rs = _review_session(db_session, opp.id)
        client.post(
            f"/workspace/{opp.id}/approve",
            data={"decision": "approve_to_bid", "expected_version": rs.version},
            cookies={"govcon_session": token},
        )
        drain(opportunity_id=opp.id)
        sub = db_session.scalar(select(Submission).where(Submission.opportunity_id == opp.id))
        assert sub is not None
        db_session.refresh(sub)
        resp = client.post(
            f"/workspace/{opp.id}/submission/approve",
            data={"expected_version": sub.version, "confirmation_number": "SHOULD-NOT-RECORD"},
            cookies={"govcon_session": token},
        )
        assert "error=" in resp.headers["location"]
        db_session.expire(sub)
        assert sub.status != "submitted"

    def test_read_only_user_cannot_record_outcome_or_triage(self, client, db_session):
        from govcon.models import OutcomeFeedback

        _viewer, token = _make_user(db_session, f"rv_{secrets.token_hex(4)}@example.com", "read_only")
        opp = _fresh_opp(db_session, "RV")
        resp = client.post(
            f"/workspace/{opp.id}/record-outcome",
            data={"outcome": "no_bid", "no_bid_category": "margin"},
            cookies={"govcon_session": token},
        )
        assert "error=" in resp.headers["location"]
        assert db_session.scalar(select(OutcomeFeedback).where(OutcomeFeedback.opportunity_id == opp.id)) is None

        wl = _make_watchlist(db_session)
        m = Match(opportunity_id=opp.id, watchlist_id=wl.id, status="new")
        db_session.add(m)
        db_session.commit()
        resp = client.post("/inbox/action", data={"match_id": m.id, "action": "dismissed"}, cookies={"govcon_session": token})
        assert resp.status_code == 403
        db_session.expire(m)
        assert m.status == "new"

        resp = client.post("/watchlists/new", data={"name": "RO watchlist"}, cookies={"govcon_session": token})
        assert "error=" in resp.headers["location"]
        assert db_session.scalar(select(Watchlist).where(Watchlist.name == "RO watchlist")) is None


class TestAuditTrail:
    """AC: Audit history shows who changed what and when."""

    def test_comment_stored_with_user_id(self, client, db_session):
        user, token = _make_user(db_session, "audit_user@example.com", "reviewer")
        opp = _make_opp(db_session)
        _assign(db_session, opp.id, user)
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


class TestSecurityRequirements:
    """AC: App defaults to localhost only. No secret values in rendered HTML."""

    def test_bind_host_defaults_to_loopback(self):
        from govcon.config import Settings
        from govcon.web.app import bind_host
        s = Settings(database_url="postgresql+psycopg://x:x@localhost/x")
        assert bind_host(s) == "127.0.0.1"

    def test_no_password_hash_in_rendered_page(self, client, db_session):
        _user, token = _make_user(db_session, "security_test@example.com", "read_only")
        resp = client.get("/ops", cookies={"govcon_session": token})
        assert resp.status_code == 200
        # Password hashes start with $argon2
        assert b"$argon2" not in resp.content

    def test_public_bind_requires_explicit_opt_in(self):
        import pytest

        from govcon.config import Settings
        with pytest.raises(Exception):
            Settings(
                database_url="postgresql+psycopg://x:x@localhost/x",
                web_bind_host="0.0.0.0",
            )


class TestRealSourceLinks:
    """H5: detail and workspace pages render real normalised SAM links."""

    def _sam_opp(self, db_session) -> Opportunity:
        import json
        from pathlib import Path

        from govcon.ingest.sam_opportunities import normalize_opportunity
        from govcon.ingest.snapshots import upsert_opportunity

        fixture = Path(__file__).parent / "fixtures" / "sam_opportunities_search.json"
        record = dict(json.loads(fixture.read_text(encoding="utf-8"))["opportunitiesData"][0])
        record["noticeId"] = f"web-links-{secrets.token_hex(6)}"
        record["resourceLinks"] = [
            "https://api.sam.gov/prod/opportunities/v3/resources/files/aaa/download",
            {"url": "https://api.sam.gov/prod/opportunities/v3/resources/files/bbb/download", "name": "SOW.docx"},
        ]
        upsert_opportunity(db_session, normalize_opportunity(record))
        db_session.commit()
        opp = db_session.scalar(select(Opportunity).where(Opportunity.source_id == record["noticeId"]))
        assert isinstance(opp.links.get("attachments"), list)  # list-valued links reach the template
        return opp

    def test_detail_page_renders_list_valued_links(self, client, db_session):
        _user, token = _make_user(db_session, "links_viewer@example.com", "read_only")
        opp = self._sam_opp(db_session)
        resp = client.get(f"/opp/{opp.id}", cookies={"govcon_session": token})
        assert resp.status_code == 200
        body = resp.text
        assert "Attachment 1" in body and "Attachment 2" in body
        assert "/resources/files/bbb/download" in body
        if opp.links.get("ui"):
            assert opp.links["ui"] in body  # the Source button uses the real notice link

    def test_workspace_renders_source_button(self, client, db_session):
        _user, token = _make_user(db_session, "links_viewer2@example.com", "read_only")
        opp = self._sam_opp(db_session)
        resp = client.get(f"/workspace/{opp.id}?tab=overview", cookies={"govcon_session": token})
        assert resp.status_code == 200
        if opp.links.get("ui"):
            assert opp.links["ui"] in resp.text


def test_source_links_helper_tolerates_every_shape():
    from govcon.web.helpers import primary_source_url, source_links

    links = {
        "self": [{"rel": "self", "href": "https://api.sam.gov/opp/1"}],
        "ui": "https://sam.gov/opp/1/view",
        "attachments": ["https://files.example.test/a.pdf", "not a url", None],
        "document_name": "PR-123.PDF",
        "package": "https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive/ca260925.zip",
        "weird": {"href": "javascript:alert(1)"},
    }
    entries = source_links(links)
    urls = [e["url"] for e in entries]
    assert urls[0] == "https://sam.gov/opp/1/view"
    assert "https://files.example.test/a.pdf" in urls
    assert all(u.startswith("https://") for u in urls)
    assert primary_source_url(links) == "https://sam.gov/opp/1/view"
    assert source_links(None) == [] and source_links(["x"]) == []
    assert primary_source_url({"attachments": ["https://files.example.test/a.pdf"]}) == "https://files.example.test/a.pdf"


def test_alerted_match_stays_in_inbox_with_marker(client, db_session, tmp_path):
    """H6: sending the digest must not empty the inbox."""
    from govcon.alerts.digest import run_digest
    from govcon.config import Settings

    _user, token = _make_user(db_session, f"inbox_alert_{secrets.token_hex(4)}@example.com", "reviewer")
    wl = Watchlist(name=f"Inbox alert WL {secrets.token_hex(4)}", enabled=True, psc_codes=["71"])
    db_session.add(wl)
    opp = _fresh_opp(db_session, "ALERT")
    opp.title = f"Alerted inbox item {secrets.token_hex(3)}"
    db_session.flush()
    match = Match(opportunity_id=opp.id, watchlist_id=wl.id, status="new")
    db_session.add(match)
    db_session.commit()

    settings = Settings(outbox_dir=tmp_path, smtp_host=None, alert_email_to=None)
    result = run_digest(db_session, settings=settings)
    db_session.commit()
    assert match.id in result.match_ids
    db_session.refresh(match)
    assert match.status == "new" and match.alerted_at is not None

    resp = page_containing(client, "/", opp.title, cookies={"govcon_session": token})
    assert resp.status_code == 200
    assert opp.title in resp.text
    assert "Alerted" in resp.text


def _engine_evidence(watchlist: Watchlist, opp: Opportunity) -> dict:
    from govcon.matching.engine import evaluate_match

    is_match, matched_on, _score = evaluate_match(watchlist, opp)
    assert is_match
    return matched_on


def test_match_summary_names_only_the_rules_that_passed():
    from govcon.web.helpers import match_summary

    opp = Opportunity(source="dibbs", source_id="X", title="Valve assembly", psc_code="4820",
                      status="open", response_deadline=datetime.now(UTC) + timedelta(days=20.5))
    wl = Watchlist(name="Valves", psc_codes=["48"], keywords=["valve"], sources=["dibbs"], min_deadline_days=5)
    summary = match_summary(_engine_evidence(wl, opp))
    assert summary == "PSC 4820 (prefix 48) · keywords: valve · source DIBBS · 20 days left (needs 5)"


def test_match_summary_of_a_sources_only_watchlist_has_no_raw_evidence():
    from govcon.web.helpers import match_summary

    opp = Opportunity(source="dibbs", source_id="X", title="Any item", psc_code="7310", status="open")
    wl = Watchlist(name="Demo", sources=["sam", "dibbs", "usaspending"])
    summary = match_summary(_engine_evidence(wl, opp))
    assert summary == "source DIBBS"
    assert "wildcard" not in summary and "{" not in summary


def test_match_summary_marks_unknown_facts_and_keeps_legacy_rows():
    from govcon.web.helpers import match_summary

    opp = Opportunity(source="sam", source_id="X", title="Unpriced", status="open")
    wl = Watchlist(name="Value", min_value=1000, min_deadline_days=3)
    assert match_summary(_engine_evidence(wl, opp)) == "value not stated · deadline not stated"
    assert match_summary({"psc": ["71"], "keywords": []}) == "psc: 71"
    assert match_summary(None) == ""


def test_inbox_shows_a_readable_match_summary(client, db_session):
    user, token = _make_user(db_session, f"inbox_summary_{secrets.token_hex(4)}@example.com", "reviewer")
    wl = Watchlist(name=f"Inbox summary WL {secrets.token_hex(4)}", enabled=True, psc_codes=["99"])
    db_session.add(wl)
    opp = _fresh_opp(db_session, "SUMMARY")
    db_session.add(Match(opportunity_id=opp.id, watchlist_id=wl.id, status="new",
                         matched_on=_engine_evidence(wl, opp)))
    db_session.commit()

    resp = page_containing(client, "/", opp.title, cookies={"govcon_session": token})
    assert resp.status_code == 200
    assert "Matched: PSC 9999 (prefix 99)" in resp.text
    assert "wildcard" not in resp.text and "score_basis" not in resp.text


def test_decision_tab_shows_package_lists_and_real_percentages(client, db_session):
    """Lists are stored as {"items": [...]} and scores as 0-1 fractions; the tab must unwrap and scale them."""
    import re

    from govcon.decision.engine import run_preliminary_decision_package

    user, token = _make_user(db_session, f"decision_tab_{secrets.token_hex(4)}@example.com", "reviewer")
    opp = _fresh_opp(db_session, "DECISION")
    package = run_preliminary_decision_package(db_session, opportunity_id=opp.id)
    db_session.commit()
    bid = package.bid_decision

    page = client.get(f"/workspace/{opp.id}?tab=ai_decision", cookies={"govcon_session": token}).text
    assert "<li>items</li>" not in page
    for line in bid.missing_information["items"]:
        assert f"<li>{line}</li>" in page
    shown = lambda label: re.search(label + r'</div>\s*<div class="value[^"]*">\s*(\d+)%', page).group(1)
    assert shown("Confidence") == f"{float(bid.recommendation_score) * 100:.0f}"
    assert shown("Compliance Risk") == f"{(1 - float(bid.compliance_risk_score)) * 100:.0f}"
    assert shown("Margin Score") == f"{float(bid.margin_score) * 100:.0f}"
    assert "Rules engine" in page  # no JEV key in tests: the rules provider produced it
    assert "source: opportunity" in page
