"""Roadmap stage 3: ranking, auto-pursue, recommendations, notifications and escalation (ADR-068..070)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_web_ui import _complete_review, _make_user
from web_client import CsrfTestClient, page_containing

from govcon.db import session_scope
from govcon.models import (
    AppSetting,
    AuditEvent,
    Match,
    Notification,
    NotificationDelivery,
    Opportunity,
    Pursuit,
    Recommendation,
    ReviewAssignment,
    ReviewSession,
    Task,
    Watchlist,
)
from govcon.tasks.testing import drain
from govcon.web.app import create_app

NOW = datetime.now(UTC)


@pytest.fixture()
def db(upgraded_engine):
    with Session(upgraded_engine) as session:
        yield session


@pytest.fixture()
def client(upgraded_engine):
    with CsrfTestClient(create_app(), follow_redirects=False) as client:
        yield client


@pytest.fixture(autouse=True)
def default_settings(upgraded_engine):
    with session_scope() as s:
        s.query(AppSetting).delete()
    yield
    with session_scope() as s:
        s.query(AppSetting).delete()


def opportunity(db, *, days=30, value=Decimal(250000), deadline=True):
    opp = Opportunity(source="sam", source_id=uuid4().hex, title=f"Rank {uuid4().hex[:6]}", status="open",
                      response_deadline=NOW + timedelta(days=days) if deadline else None,
                      estimated_value_min=value, estimated_value_max=value, raw={}, links={})
    db.add(opp)
    db.flush()
    return opp


def rule_match(db, opp, *, passing=2, active=2, status="new"):
    wl = Watchlist(name=f"WL {uuid4().hex[:6]}", enabled=True)
    db.add(wl)
    db.flush()
    groups = [f"g{i}" for i in range(active)]
    match = Match(opportunity_id=opp.id, watchlist_id=wl.id, status=status, active=True, score=Decimal(passing),
                  matched_on={"active_groups": groups, "passing_groups": groups[:passing]})
    db.add(match)
    db.flush()
    return match


def set_setting(db, key, value):
    owner, _ = _make_user(db, f"owner-{uuid4().hex}@example.test", "owner")
    from govcon.workflow.app_settings import set_setting as save
    with session_scope() as s:
        save(s, key, value, actor=s.get(type(owner), owner.id))


# ── explainable ranking ──────────────────────────────────────────────────────

def test_rank_is_explainable_and_unknown_factors_are_excluded(db):
    from govcon.matching.ranking import WEIGHTS, rank_match
    opp = opportunity(db, days=30, value=Decimal(1000000))
    match = rule_match(db, opp, passing=1, active=2)
    score, factors = rank_match(db, match, opp, now=NOW)
    by_name = {f["name"]: f for f in factors}
    assert set(by_name) == set(WEIGHTS) and sum(WEIGHTS.values()) == 100
    assert by_name["rule_match"]["raw"] == 0.5 and "1 of 2" in by_name["rule_match"]["evidence"]
    assert by_name["time_remaining"]["raw"] == 1.0
    assert 0.6 < by_name["value"]["raw"] < 0.7  # $1M on the $10K-$10M log scale
    for name in ("semantic_similarity", "similar_to_wins", "competition"):
        assert by_name[name]["available"] is False and by_name[name]["points"] is None
    available = WEIGHTS["rule_match"] + WEIGHTS["value"] + WEIGHTS["time_remaining"]
    expected = (30 * 0.5 + 15 * by_name["value"]["raw"] + 10 * 1.0) / available * 100
    assert float(score) == pytest.approx(expected, abs=0.1)
    assert sum(f["points"] for f in factors if f["available"]) == pytest.approx(float(score), abs=0.2)

    db.add(Recommendation(opportunity_id=opp.id, watchlist_id=match.watchlist_id, category="semantic_match",
                          similarity=Decimal("0.9"), evidence={}, active=True))
    db.flush()
    _, factors = rank_match(db, match, opp, now=NOW)
    assert next(f for f in factors if f["name"] == "semantic_similarity")["raw"] == 0.9


def test_rank_step_ranks_active_matches_and_the_inbox_explains_it(db, client):
    from govcon.matching.ranking import rank_active_matches
    opp = opportunity(db)
    match = rule_match(db, opp)
    db.commit()
    with session_scope() as s:
        assert rank_active_matches(s) >= 1
    db.expire_all()
    ranked = db.get(Match, match.id)
    assert ranked.rank_score is not None and ranked.ranked_at is not None and len(ranked.rank_factors) == 6
    _, token = _make_user(db, f"inbox-{uuid4().hex}@example.test", "reviewer")
    client.cookies.set("govcon_session", token)
    page = page_containing(client, "/", opp.title).text
    assert f"Rank {float(ranked.rank_score):.0f}" in page and "Why this rank" in page


# ── recommendations are persisted ────────────────────────────────────────────

def test_recommendations_are_persisted_with_revisions_and_deactivated(db, monkeypatch):
    from govcon.matching import recommendations as recs
    opp = opportunity(db)
    db.commit()
    item = {"id": opp.id, "title": opp.title, "cosine_distance": 0.2}
    monkeypatch.setattr(recs, "semantic_recommendations_for_watchlist", lambda *a, **k: {"matches": []})
    monkeypatch.setattr(recs, "win_profile_recommendations", lambda *a, **k: {"matches": []})
    monkeypatch.setattr(recs, "pursued_profile_recommendations", lambda *a, **k: {"matches": [item]})
    monkeypatch.setattr(recs, "recompete_radar", lambda *a, **k: {"matches": []})
    provider = SimpleNamespace(model_version="all-MiniLM-L6-v2@abc123")
    with session_scope() as s:
        stats = recs.refresh_recommendations(s, provider)
    assert stats.inserted >= 1
    row = db.scalar(select(Recommendation).where(Recommendation.opportunity_id == opp.id))
    assert row.category == "similar_to_pursued" and row.similarity == Decimal("0.8")
    assert row.embedding_model_version == "all-MiniLM-L6-v2@abc123" and row.evidence["id"] == opp.id
    monkeypatch.setattr(recs, "pursued_profile_recommendations", lambda *a, **k: {"matches": []})
    with session_scope() as s:
        recs.refresh_recommendations(s, provider)
    db.expire_all()
    assert db.get(Recommendation, row.id).active is False, "recommendations are deactivated, never deleted"


# ── auto-pursue guardrails ───────────────────────────────────────────────────

def ranked(db, opp, score, *, coverage_full=True, status="new"):
    from govcon.matching.ranking import WEIGHTS
    match = rule_match(db, opp, status=status)
    match.rank_score = Decimal(str(score))
    match.rank_factors = [{"name": n, "weight": w, "available": coverage_full or n == "rule_match"}
                          for n, w in WEIGHTS.items()]
    db.flush()
    return match


def test_auto_pursue_follows_every_guardrail(db):
    from govcon.matching.auto_pursue import run_auto_pursue
    set_setting(db, "auto_pursue", {"enabled": True, "min_score": 75, "min_days": 10, "max_per_day": 50})
    good = opportunity(db, days=30)
    low = opportunity(db, days=30)
    soon = opportunity(db, days=5)
    unknown = opportunity(db, deadline=False)
    thin = opportunity(db, days=30)
    dismissed = opportunity(db, days=30)
    ranked(db, good, 90)
    ranked(db, low, 60)
    ranked(db, soon, 95)
    unknown_match = ranked(db, unknown, 95)
    thin_match = ranked(db, thin, 99, coverage_full=False)
    ranked(db, dismissed, 99, status="dismissed")
    semantic_only = opportunity(db, days=30)
    db.add(Recommendation(opportunity_id=semantic_only.id, category="semantic_match", similarity=Decimal("0.99"),
                          evidence={}, active=True))
    db.commit()
    with session_scope() as s:
        result = run_auto_pursue(s)
    assert good.id in result.pursued
    for opp in (low, soon, unknown, thin, dismissed, semantic_only):
        assert opp.id not in result.pursued
    assert {unknown_match.id, thin_match.id} <= set(result.needs_eligibility_decision)
    db.expire_all()
    pursuit = db.scalar(select(Pursuit).where(Pursuit.opportunity_id == good.id))
    assert pursuit is not None and pursuit.stage == "evaluating"
    assert db.scalar(select(AuditEvent).where(AuditEvent.opportunity_id == good.id,
                                              AuditEvent.action_type == "auto_pursue_applied")) is not None
    assert db.scalar(select(Task).where(Task.opportunity_id == good.id, Task.task_type == "opportunity_preparation"))
    assert db.scalar(select(Notification).where(Notification.opportunity_id == good.id,
                                                Notification.notification_type == "auto_pursued")) is not None


def test_auto_pursue_daily_cap_and_off_switch(db):
    from govcon.matching.auto_pursue import pursued_today, run_auto_pursue
    with session_scope() as s:
        already = pursued_today(s, datetime.now(UTC))
    set_setting(db, "auto_pursue", {"enabled": True, "min_score": 75, "min_days": 10, "max_per_day": already + 1})
    opps = [opportunity(db, days=30) for _ in range(3)]
    for opp in opps:
        ranked(db, opp, 99.5)
    db.commit()
    with session_scope() as s:
        result = run_auto_pursue(s)
    mine = [o.id for o in opps if o.id in result.pursued]
    assert len(mine) <= 1 and result.skipped_daily_cap >= 2
    set_setting(db, "auto_pursue", {"enabled": False, "min_score": 75, "min_days": 10, "max_per_day": 50})
    with session_scope() as s:
        assert run_auto_pursue(s).enabled is False


# ── email delivery ───────────────────────────────────────────────────────────

def test_notification_email_is_queued_with_the_notification_and_retried(db, monkeypatch):
    from govcon.collaboration.notifications import notify
    from govcon.config import Settings
    user, _ = _make_user(db, f"mail-{uuid4().hex}@example.test", "reviewer")
    opp = opportunity(db)
    db.commit()
    configured = Settings(_env_file=None, notify_email_enabled=True, smtp_host="smtp.example.test")
    with session_scope() as s:
        row = notify(s, user_id=user.id, notification_type="review_assigned", opportunity_id=opp.id,
                     payload={"title": opp.title}, settings=configured)
        notification_id = row.id
    delivery = db.scalar(select(NotificationDelivery).where(NotificationDelivery.notification_id == notification_id))
    assert delivery.recipient == user.email and delivery.status == "queued"
    sent = []
    monkeypatch.setattr("govcon.alerts.digest.send_smtp", lambda settings, **k: sent.append(k))
    assert drain(opportunity_id=opp.id, task_types=["notification_email"])[0][1] == "succeeded"
    db.expire_all()
    assert db.get(NotificationDelivery, delivery.id).status == "sent"
    assert sent[0]["to"] == user.email and "assigned a review" in sent[0]["subject"]


def test_failed_email_is_marked_failed_after_retries(db, monkeypatch):
    from govcon.alerts.digest import DigestDeliveryError
    from govcon.collaboration.notifications import notify
    from govcon.config import Settings
    user, _ = _make_user(db, f"fail-{uuid4().hex}@example.test", "reviewer")
    opp = opportunity(db)
    db.commit()
    configured = Settings(_env_file=None, notify_email_enabled=True, smtp_host="smtp.example.test")
    with session_scope() as s:
        notify(s, user_id=user.id, notification_type="review_reminder", opportunity_id=opp.id, settings=configured)
    task = db.scalar(select(Task).where(Task.opportunity_id == opp.id, Task.task_type == "notification_email"))
    task.max_attempts = 1
    db.commit()

    def refuse(settings, **_):
        raise DigestDeliveryError("SMTP delivery failed: 550 mailbox unavailable")

    monkeypatch.setattr("govcon.alerts.digest.send_smtp", refuse)
    assert drain(opportunity_id=opp.id, task_types=["notification_email"])[0][1] == "failed"
    db.expire_all()
    delivery = db.scalar(select(NotificationDelivery).join(Notification).where(Notification.opportunity_id == opp.id))
    assert delivery.status == "failed" and "550" in delivery.error


def test_email_is_not_queued_unless_enabled(db):
    from govcon.collaboration.notifications import notify
    from govcon.config import Settings
    user, _ = _make_user(db, f"off-{uuid4().hex}@example.test", "reviewer")
    with session_scope() as s:
        row = notify(s, user_id=user.id, notification_type="review_assigned",
                     settings=Settings(_env_file=None, smtp_host="smtp.example.test"))
        notification_id = row.id
    assert db.scalar(select(NotificationDelivery).where(NotificationDelivery.notification_id == notification_id)) is None


def test_action_required_notifications_can_be_acknowledged(db, client):
    from govcon.collaboration.notifications import notify
    user, token = _make_user(db, f"ack-{uuid4().hex}@example.test", "reviewer")
    with session_scope() as s:
        notification_id = notify(s, user_id=user.id, notification_type="review_reminder").id
    client.cookies.set("govcon_session", token)
    page = client.get("/notifications").text
    assert "Action required" in page and f"/notifications/{notification_id}/acknowledge" in page
    assert client.post(f"/notifications/{notification_id}/acknowledge").status_code == 303
    db.expire_all()
    row = db.get(Notification, notification_id)
    assert row.acknowledged_at is not None and row.read_at is not None
    _other, other_token = _make_user(db, f"ack2-{uuid4().hex}@example.test", "reviewer")
    client.cookies.set("govcon_session", other_token)
    assert client.post(f"/notifications/{notification_id}/acknowledge").status_code == 404


# ── reminders and escalation ─────────────────────────────────────────────────

def test_reminders_and_escalations_fire_once(db):
    from govcon.collaboration.escalation import run_review_escalations
    from govcon.collaboration.review_sessions import ensure_review_session
    from govcon.config import Settings
    approver, _ = _make_user(db, f"esc-appr-{uuid4().hex}@example.test", "approver")
    reviewer, _ = _make_user(db, f"esc-rev-{uuid4().hex}@example.test", "reviewer")
    opp = opportunity(db, days=2)
    ensure_review_session(db, opportunity_id=opp.id)
    db.add(ReviewAssignment(opportunity_id=opp.id, user_id=reviewer.id, status="assigned",
                            assigned_at=NOW - timedelta(hours=120)))
    db.commit()
    configured = Settings(_env_file=None, review_reminder_hours=48, review_overdue_hours=96,
                          review_escalate_days_before_deadline=3)

    def kinds():
        db.expire_all()
        return sorted(n.notification_type for n in db.scalars(select(Notification).where(
            Notification.opportunity_id == opp.id, Notification.user_id.in_([approver.id, reviewer.id]))))

    with session_scope() as s:
        counts = run_review_escalations(s, settings=configured)
    assert counts.reminders >= 1 and counts.overdue >= 1 and counts.deadline >= 1
    assert kinds() == ["deadline_escalation", "review_overdue_escalation", "review_reminder"]
    with session_scope() as s:
        run_review_escalations(s, settings=configured)
    assert kinds() == ["deadline_escalation", "review_overdue_escalation", "review_reminder"], "no repeats within a day"


# ── single-reviewer deadline exception (roadmap Q5) ──────────────────────────

def dual_review(db, days):
    from govcon.collaboration.review_sessions import ensure_review_session
    opp = opportunity(db, days=days)
    db.add(Pursuit(opportunity_id=opp.id, stage="evaluating"))
    review = ensure_review_session(db, opportunity_id=opp.id)
    review.review_policy = "dual"
    db.commit()
    _complete_review(db, opp.id, uuid4().hex[:6])
    approver, _ = _make_user(db, f"exc-{uuid4().hex}@example.test", "approver")
    return opp, approver


@pytest.mark.parametrize("days,reason,enabled,approved", [
    (3, "Deadline is in three days; one review is enough.", True, True),
    (3, None, True, False),
    (20, "Deadline is far away, no exception applies.", True, False),
    (3, "Owner turned the exception off.", False, False),
])
def test_deadline_exception_allows_one_review_only_when_it_applies(db, monkeypatch, days, reason, enabled, approved):
    from govcon.collaboration.review_sessions import (
        ReviewWorkflowError,
        finalize_approval,
        recalculate_quorum,
    )
    from govcon.config import get_settings
    from govcon.models import User
    monkeypatch.setenv("REVIEW_OVERRIDE_ALLOWED", "false")
    get_settings.cache_clear()
    if not enabled:
        set_setting(db, "single_reviewer_deadline_exception", {"enabled": False})
    opp, approver = dual_review(db, days)
    with session_scope() as s:
        quorum = recalculate_quorum(s, opportunity_id=opp.id)
        assert quorum.completed_review_count == 1 and not quorum.quorum_satisfied
        version = s.scalar(select(ReviewSession.version).where(ReviewSession.opportunity_id == opp.id))
    try:
        with session_scope() as s:
            finalize_approval(s, opportunity_id=opp.id, actor=s.get(User, approver.id), action="approve_to_bid",
                              expected_version=version, override_reason=reason)
        outcome = True
    except ReviewWorkflowError:
        outcome = False
    assert outcome is approved
    db.expire_all()
    exception = db.scalar(select(AuditEvent).where(AuditEvent.opportunity_id == opp.id,
                                                   AuditEvent.action_type == "review_deadline_exception"))
    assert (exception is not None) is approved
    if approved:
        review = db.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp.id))
        assert review.status == "approved_to_bid" and review.override_reason.startswith("Deadline exception")


# ── Settings page ────────────────────────────────────────────────────────────

def test_owner_sets_auto_pursue_thresholds(db, client):
    _, token = _make_user(db, f"ap-{uuid4().hex}@example.test", "owner")
    client.cookies.set("govcon_session", token)
    page = client.get("/settings").text
    assert "Automatic pursuit" in page and "Deadline exception" in page
    response = client.post("/settings", data={
        "reviewer_mode": "manual", "auto_prepare": "on", "auto_pursue": "on",
        "auto_pursue_min_score": "80", "auto_pursue_min_days": "14", "auto_pursue_max_per_day": "2",
    })
    assert "notice=" in response.headers["location"]
    db.expire_all()
    assert db.get(AppSetting, "auto_pursue").value == {"enabled": True, "min_score": 80.0, "min_days": 14, "max_per_day": 2}
    assert db.get(AppSetting, "single_reviewer_deadline_exception").value == {"enabled": False}
    bad = client.post("/settings", data={"reviewer_mode": "manual", "auto_pursue_min_score": "150"})
    assert "error=" in bad.headers["location"]
