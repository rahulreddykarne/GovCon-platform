"""Regression coverage for the workflow and UI review, using disposable data."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_preparation import client as client
from test_preparation import db as db
from test_preparation import new_opportunity
from test_web_ui import _make_user
from web_client import _NextPage, page_containing

from govcon.models import OutcomeFeedback, Pursuit, Submission, Task
from govcon.tasks.queue import enqueue
from govcon.workflow.preparation import PREPARATION_TASK


def login(db, client, role="owner"):
    user, token = _make_user(db, f"cleanup-{uuid4().hex}@example.test", role)
    client.cookies.set("govcon_session", token)
    return user


def test_cancel_available_before_submission_and_stops_preparation(db, client):
    owner = login(db, client)
    opp = new_opportunity(db)
    db.add(Pursuit(opportunity_id=opp.id, stage="evaluating"))
    task, _ = enqueue(db, task_type=PREPARATION_TASK, opportunity_id=opp.id,
                      input_revision={"test": "cancel"}, actor_user_id=owner.id)
    db.commit()
    page = client.get(f"/workspace/{opp.id}?tab=submission")
    assert 'id="outcome-form"' in page.text
    assert '<option value="cancelled">' in page.text
    assert '<option value="won">' not in page.text
    assert '<option value="no_bid">' not in page.text
    response = client.post(f"/workspace/{opp.id}/record-outcome", data={"outcome": "cancelled"})
    assert response.status_code == 303
    db.expire_all()
    assert db.get(Task, task.id).status == "cancelled"
    assert db.scalar(select(OutcomeFeedback).where(OutcomeFeedback.opportunity_id == opp.id)).outcome == "cancelled"
    assert "error=" in client.post(f"/workspace/{opp.id}/prepare").headers["location"]


@pytest.mark.parametrize("cost", [81.5, 20])
def test_submitted_outcome_options_follow_transition_table(db, client, cost):
    login(db, client)
    opp = new_opportunity(db)
    pursuit = Pursuit(opportunity_id=opp.id, stage="submitted", quote_price=100, sourcing_cost=cost, supplier="Known supplier")
    db.add(pursuit)
    db.flush()
    db.add(Submission(opportunity_id=opp.id, pursuit_id=pursuit.id, status="submitted", submitted_at=datetime.now(UTC)))
    db.commit()
    page = client.get(f"/workspace/{opp.id}?tab=submission")
    assert 'id="outcome-form"' in page.text
    assert '<option value="won">' in page.text
    assert '<option value="lost">' in page.text
    assert '<option value="no_bid">' not in page.text
    margin = pursuit.margin_pct if 0 <= pursuit.margin_pct <= 100 else ""
    assert f'name="win_margin_pct" value="{margin}"' in page.text
    assert 'value="Known supplier"' in page.text


def test_read_only_has_no_outcome_form(db, client):
    login(db, client, "read_only")
    opp = new_opportunity(db)
    db.add(Pursuit(opportunity_id=opp.id, stage="evaluating"))
    db.commit()
    assert 'id="outcome-form"' not in client.get(f"/workspace/{opp.id}?tab=submission").text


def test_comment_form_requires_assignment(db, client):
    from govcon.collaboration.assignments import assign_reviewer
    from govcon.collaboration.review_sessions import ensure_review_session

    owner = login(db, client)
    opp = new_opportunity(db)
    ensure_review_session(db, opportunity_id=opp.id)
    db.commit()
    target = f"/workspace/{opp.id}?tab=review"
    assert f'action="/workspace/{opp.id}/comment"' not in client.get(target).text
    assign_reviewer(db, opportunity_id=opp.id, user_id=owner.id)
    db.commit()
    assert f'action="/workspace/{opp.id}/comment"' in client.get(target).text


def test_web_comment_queues_validation_without_calling_ai(db, client, monkeypatch):
    from govcon.collaboration.assignments import assign_reviewer
    from govcon.models import ReviewComment

    user = login(db, client, "reviewer")
    opp = new_opportunity(db)
    assign_reviewer(db, opportunity_id=opp.id, user_id=user.id)
    db.commit()

    def no_inline_validation(*args, **kwargs):
        raise AssertionError("The web request must not make an AI call")

    monkeypatch.setattr("govcon.collaboration.comments.validate_comment_with_ai", no_inline_validation)
    response = client.post(f"/workspace/{opp.id}/comment", data={"body": "Supplier evidence confirms the required delivery schedule."})
    assert response.status_code == 303
    db.expire_all()
    comment = db.scalar(select(ReviewComment).where(ReviewComment.opportunity_id == opp.id))
    assert comment and comment.ai_position is None
    task = db.scalar(select(Task).where(Task.opportunity_id == opp.id))
    assert task and task.status == "queued" and task.payload["comment_id"] == comment.id


def test_comment_worker_publishes_side_opinion_and_rechecks_evidence(allow_proprietary_ai, db, client, monkeypatch):
    from pathlib import Path

    from test_collaborative_review import _FakeProvider

    from govcon.collaboration.assignments import assign_reviewer
    from govcon.db import session_scope
    from govcon.models import ReviewComment
    from govcon.prompting.registry import sync_prompts
    from govcon.tasks.testing import drain

    sync_prompts(db, Path(__file__).parent.parent / "src/govcon/prompts")
    user = login(db, client, "reviewer")
    opp = new_opportunity(db)
    pursuit = Pursuit(opportunity_id=opp.id, stage="evaluating", supplier="Initial supplier")
    db.add(pursuit)
    assign_reviewer(db, opportunity_id=opp.id, user_id=user.id)
    db.commit()
    body = "The supplier lead time is not supported by the provided evidence."
    client.post(f"/workspace/{opp.id}/comment", data={"body": body})
    task = db.scalar(select(Task).where(Task.opportunity_id == opp.id))
    provider = _FakeProvider()
    original = provider.complete
    calls = []

    def changed_evidence(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            with session_scope() as writer:
                writer.get(Pursuit, pursuit.id).supplier = "Updated supplier"
        return original(**kwargs)

    monkeypatch.setattr(provider, "complete", changed_evidence)
    monkeypatch.setattr("govcon.ai.structured.get_provider", lambda *args, **kwargs: provider)
    results = drain(opportunity_id=opp.id)
    assert [status for _, status in results] == ["cancelled", "succeeded"]
    db.expire_all()
    assert db.get(Task, task.id).superseded_by_task_id == results[1][0]
    comment = db.scalar(select(ReviewComment).where(ReviewComment.opportunity_id == opp.id))
    assert comment.ai_position == "disagree" and comment.body == body
    assert len(calls) == 2


def test_review_rejects_contradictory_no_bid_recommendation(db, client):
    from govcon.collaboration.assignments import assign_reviewer
    from govcon.collaboration.review_sessions import complete_assignment

    user = login(db, client, "reviewer")
    opp = new_opportunity(db)
    assign_reviewer(db, opportunity_id=opp.id, user_id=user.id)
    db.commit()
    with pytest.raises(ValueError, match="no_bid"):
        complete_assignment(db, opportunity_id=opp.id, user_id=user.id, action="no_bid",
                            recommendation="bid", agree_with_ai_assessment=True)


def test_search_ranks_relevance_then_deadline_with_undated_last(db, client):
    login(db, client)
    unique = "turbo" + uuid4().hex
    near, far, undated = [new_opportunity(db) for _ in range(3)]
    for opp in (near, far, undated):
        opp.title = unique
        opp.description = ""
    near.response_deadline = datetime.now(UTC) + timedelta(days=1)
    far.response_deadline = datetime.now(UTC) + timedelta(days=90)
    undated.response_deadline = None
    db.commit()
    page = client.get("/search", params={"q": unique}).text
    paths = [f'/opp/{opp.id}"' for opp in (near, far, undated)]
    assert page.index(paths[0]) < page.index(paths[1]) < page.index(paths[2])
    far.description = (unique + " ") * 20
    db.commit()
    ranked = client.get("/search", params={"q": unique}).text
    assert ranked.index(paths[1]) < ranked.index(paths[0])


def test_url_cannot_spoof_flash_and_real_messages_are_consumed_once(db, client):
    login(db, client)
    spoof = "FAKE SECURITY MESSAGE " + uuid4().hex
    assert spoof not in client.get("/", params={"error": spoof, "notice": spoof}).text
    opp = new_opportunity(db)
    response = client.post(f"/workspace/{opp.id}/record-outcome", data={"outcome": "invalid"})
    target = response.headers["location"]
    assert "Unknown outcome" in client.get(target).text
    assert "Unknown outcome" not in client.get(target).text


def duplicate_matches(db, opp):
    from govcon.models import Match, Watchlist

    matches = []
    for _ in range(2):
        wl = Watchlist(name="Cleanup " + uuid4().hex, enabled=True)
        db.add(wl)
        db.flush()
        match = Match(opportunity_id=opp.id, watchlist_id=wl.id, status="new", active=True)
        db.add(match)
        matches.append(match)
    db.commit()
    return matches


def test_inbox_unique_opportunities_and_pursue_updates_all_matches(db, client):
    login(db, client)
    opp = new_opportunity(db)
    matches = duplicate_matches(db, opp)
    page = page_containing(client, "/", f'href="/opp/{opp.id}"').text
    assert page.count(f'href="/opp/{opp.id}"') == 2  # title + Review, once per opportunity
    response = client.post("/inbox/action", data={"match_id": matches[0].id, "action": "pursuing"},
                           headers={"HX-Request": "true"})
    assert response.headers["HX-Redirect"] == f"/workspace/{opp.id}"
    db.expire_all()
    assert [db.get(type(m), m.id).status for m in matches] == ["pursuing", "pursuing"]


def test_read_only_inbox_hides_mutations_and_returns_honest_error(db, client):
    login(db, client, "read_only")
    opp = new_opportunity(db)
    matches = duplicate_matches(db, opp)
    page = client.get("/").text
    assert 'value="pursuing"' not in page and 'value="dismissed"' not in page
    response = client.post("/inbox/action", data={"match_id": matches[0].id, "action": "pursuing"},
                           headers={"HX-Request": "true"})
    assert response.status_code == 403 and 'role="alert"' in response.text


def test_pipeline_has_six_columns_unique_matches_and_separate_closed_count(db, client):
    login(db, client)
    opp = new_opportunity(db)
    duplicate_matches(db, opp)
    closed = new_opportunity(db)
    db.add(Pursuit(opportunity_id=closed.id, stage="cancelled"))
    db.commit()
    page = page_containing(client, "/pipeline", f'href="/workspace/{opp.id}"').text
    assert page.count('class="pipeline-column"') == 6
    assert page.count(f'href="/workspace/{opp.id}"') == 1
    assert "closed opportunities" in page
    filtered = client.get("/pipeline?closed=won").text
    assert f'href="/workspace/{closed.id}"' not in filtered


def test_merged_workspace_keeps_legacy_links_and_one_commercial_form(db, client):
    import re

    login(db, client)
    opp = new_opportunity(db)
    opp.description = "The original solicitation details remain available."
    db.add(Pursuit(opportunity_id=opp.id, stage="evaluating"))
    db.commit()
    overview = client.get(f"/opp/{opp.id}").text
    nav = re.search(r'<nav class="tab-bar".*?>(.*?)</nav>', overview, re.S).group(1)
    assert nav.count('<a href="/workspace/') >= 5
    assert opp.description in overview
    for alias in ("products", "pricing", "sourcing"):
        page = client.get(f"/workspace/{opp.id}?tab={alias}").text
        assert page.count(f'action="/workspace/{opp.id}/pursuit-facts"') == 1
        assert "Supplier Analysis" in page or "product/supplier analysis" in page
        assert "Pricing Analysis" in page or "pricing analysis" in page


@pytest.mark.parametrize("role", ["owner", "reviewer", "read_only"])
def test_merged_workspace_keeps_explicit_pursue_entry_point(db, client, role):
    from sqlalchemy import func

    login(db, client, role)
    opp = new_opportunity(db)
    target = f"/opp/{opp.id}/start-workspace"
    for url in (f"/opp/{opp.id}", f"/workspace/{opp.id}?tab=compliance"):
        page = client.get(url)
        assert page.status_code == 200
        assert f'href="/opp/{opp.id}"' not in page.text
        assert (f'action="{target}"' in page.text) == (role != "read_only")
        assert db.scalar(select(Pursuit.id).where(Pursuit.opportunity_id == opp.id)) is None
    response = client.post(target)
    if role == "read_only":
        assert "error=" in response.headers["location"]
        assert db.scalar(select(Pursuit.id).where(Pursuit.opportunity_id == opp.id)) is None
    else:
        assert response.status_code == 303 and response.headers["location"] == f"/workspace/{opp.id}"
        assert client.post(target).status_code == 303
        db.expire_all()
        assert db.scalar(select(func.count(Pursuit.id)).where(Pursuit.opportunity_id == opp.id)) == 1
        assert db.scalar(select(func.count(Task.id)).where(Task.opportunity_id == opp.id,
                                                         Task.task_type == PREPARATION_TASK)) == 1
        assert f'action="{target}"' not in client.get(response.headers["location"]).text


def test_unique_matches_remain_reachable_beyond_first_page(db, client):
    from govcon.models import Match, Opportunity, Watchlist

    login(db, client)
    watchlists = [Watchlist(name="Pager " + uuid4().hex, enabled=True) for _ in range(2)]
    opportunities = [Opportunity(source="sam", source_id=uuid4().hex, title="Pager " + uuid4().hex,
                                 status="open", raw={}) for _ in range(105)]
    db.add_all(watchlists + opportunities)
    db.flush()
    db.add_all([Match(opportunity_id=opp.id, watchlist_id=wl.id, status="new", active=True)
                for opp in opportunities for wl in watchlists])
    db.commit()
    for url, link in (("/", "/opp/"), ("/pipeline", "/workspace/")):
        expected = {f'href="{link}{opp.id}"' for opp in opportunities}
        remaining = expected.copy()
        seen = set()
        visited = set()
        while url:
            assert url not in visited
            visited.add(url)
            page = client.get(url)
            assert page.status_code == 200
            found = {marker for marker in expected if marker in page.text}
            assert not (found & seen), "An opportunity cannot repeat across pages"
            seen |= found
            for marker in found:
                assert page.text.count(marker) == (2 if link == "/opp/" else 1)
            remaining -= found
            url = _NextPage(page.text).url
        assert not remaining, "Unique matches after the first 100 must stay reachable"
        assert len(visited) >= 2
    assert client.get("/?page=0").status_code == 422
    assert client.get("/pipeline?matched_page=0").status_code == 422
    assert client.get("/?page=1000000").status_code == 200
    assert client.get("/pipeline?matched_page=1000000").status_code == 200


def test_inbox_error_handlers_are_in_body_and_title_stays_plain(db, client):
    import re

    login(db, client)
    page = client.get("/").text
    title = re.search(r"<title>(.*?)</title>", page, re.S).group(1)
    assert "<" not in title and "document." not in title
    assert page.count("document.body.addEventListener") == 2


@pytest.mark.parametrize("raw_poc,legacy,expected", [
    (["Plain contact"], False, "Plain contact"),
    ([None, {"fullName": "Primary contact", "email": "synthetic@example.test"}], False, "Primary contact"),
    ([{"name": "Primary contact"}, "Plain contact", None], False, "Plain contact"),
    ({"fullName": "Legacy contact", "email": "synthetic@example.test"}, True, "Legacy contact"),
    ([{"fullName": "<script>unsafe()</script>"}], False, "&lt;script&gt;unsafe()&lt;/script&gt;"),
])
def test_workspace_source_contacts_handle_feed_variations(db, client, raw_poc, legacy, expected):
    from govcon.ingest.sam_opportunities import normalize_opportunity

    login(db, client)
    opp = new_opportunity(db)
    normalized = normalize_opportunity({"noticeId": opp.source_id, "title": opp.title,
                                        "pointOfContact": raw_poc})
    opp.poc = raw_poc if legacy else normalized.poc
    db.commit()
    for url in (f"/opp/{opp.id}", f"/workspace/{opp.id}"):
        response = client.get(url)
        assert response.status_code == 200
        assert expected in response.text
        assert "<script>unsafe()</script>" not in response.text
        if "synthetic@example.test" in repr(raw_poc):
            assert 'href="mailto:synthetic@example.test"' in response.text


def test_watchlist_save_matches_checkbox_filters_and_delete_preserves_pursuit(db, client):
    from govcon.models import Match, Watchlist

    login(db, client)
    opp = new_opportunity(db)
    keyword = uuid4().hex
    opp.title = keyword
    opp.set_aside_code = "SBA"
    db.commit()
    response = client.post("/watchlists/new", data={"name": keyword, "keywords": keyword,
                                                   "sources": ["sam", "dibbs"], "set_asides": ["SBA", "HZC"]})
    assert response.status_code == 303
    db.expire_all()
    wl = db.scalar(select(Watchlist).where(Watchlist.name == keyword))
    assert wl.sources == ["sam", "dibbs"] and wl.set_asides == ["SBA", "HZC"]
    assert db.scalar(select(Match.id).where(Match.watchlist_id == wl.id, Match.opportunity_id == opp.id))
    db.add(Pursuit(opportunity_id=opp.id, stage="evaluating"))
    db.commit()
    wl_id = wl.id
    assert client.post(f"/watchlists/{wl_id}/delete").status_code == 303
    db.expire_all()
    assert db.get(Watchlist, wl_id) is None
    assert db.scalar(select(Pursuit.id).where(Pursuit.opportunity_id == opp.id))


def test_use_quote_updates_cost_and_supplier_and_rejects_stale_or_foreign_quotes(db, client):
    from decimal import Decimal

    from govcon.sourcing.records import get_or_create_supplier, record_quote

    owner = login(db, client)
    opp = new_opportunity(db)
    pursuit = Pursuit(opportunity_id=opp.id, stage="evaluating", quote_price=200)
    db.add(pursuit)
    supplier, _ = get_or_create_supplier(db, name="Quote supplier " + uuid4().hex, actor=owner, provenance="audit fixture")
    quote = record_quote(db, opportunity_id=opp.id, supplier_id=supplier.id, actor=owner,
                         method="manual", total_price=125)
    db.commit()
    target = f"/workspace/{opp.id}/quotes/{quote.id}/use"
    response = client.post(target, data={"expected_version": pursuit.version})
    assert response.status_code == 303 and "error=" not in response.headers["location"]
    db.expire_all()
    assert pursuit.sourcing_cost == Decimal(125) and pursuit.supplier == supplier.name
    assert pursuit.quote_price == Decimal(200)
    stale = client.post(target, data={"expected_version": 1})
    assert "error=" in stale.headers["location"]
    other = new_opportunity(db)
    foreign = client.post(f"/workspace/{other.id}/quotes/{quote.id}/use", data={"expected_version": 1})
    assert "error=" in foreign.headers["location"]
    quote.valid_until = datetime.now(UTC).date() - timedelta(days=1)
    db.commit()
    expired = client.post(target, data={"expected_version": pursuit.version})
    assert "error=" in expired.headers["location"]


def test_admin_changes_revoke_sessions_and_protect_last_owner(db, client):
    from govcon.collaboration.user_admin import manage_user
    from govcon.collaboration.users import verify_password
    from govcon.models import User, UserSession

    owner = login(db, client)
    target, _ = _make_user(db, f"target-{uuid4().hex}@example.test", "reviewer")
    response = client.post(f"/admin/users/{target.id}/reset_password", data={"password": "ReplacementPassword123!"})
    assert response.status_code == 303 and "error=" not in response.headers["location"]
    db.expire_all()
    assert verify_password("ReplacementPassword123!", target.password_hash)
    assert db.scalar(select(UserSession).where(UserSession.user_id == target.id)).revoked_at is not None
    client.post(f"/admin/users/{target.id}/role", data={"role": "approver"})
    db.expire_all()
    assert target.role == "approver"
    client.post(f"/admin/users/{target.id}/deactivate")
    db.expire_all()
    assert not target.is_active
    db.query(User).filter(User.role == "owner", User.id != owner.id).update({"is_active": False})
    with pytest.raises(ValueError, match="last active owner"):
        manage_user(db, actor_id=owner.id, user_id=owner.id, action="deactivate")
    db.rollback()
    login(db, client, "read_only")
    assert client.get("/admin?section=users").status_code == 403
    denied = client.post(f"/admin/users/{target.id}/activate")
    assert "error=" in denied.headers["location"]


def test_progress_is_authenticated_read_only_and_changes_with_checkpoints(db, client):
    owner = login(db, client)
    opp = new_opportunity(db)
    task, _ = enqueue(db, task_type=PREPARATION_TASK, opportunity_id=opp.id,
                      input_revision={"test": "progress"}, actor_user_id=owner.id)
    db.commit()
    url = f"/workspace/{opp.id}/progress"
    initial = client.get(url).json()
    assert initial["active"] is True
    task.checkpoint = {"completed_steps": ["download"]}
    db.commit()
    changed = client.get(url).json()
    assert initial["revision"] != changed["revision"]
    assert client.get(url).json() == changed
    client.cookies.clear()
    assert client.get(url).status_code == 401


def test_all_notifications_have_sentences_and_links_without_raw_payload(db, client):
    from govcon.collaboration.notifications import NOTIFICATION_TYPES, notify

    owner = login(db, client)
    opp = new_opportunity(db)
    marker = "RAW_INTERNAL_PAYLOAD_" + uuid4().hex
    for kind in NOTIFICATION_TYPES:
        notify(db, user_id=owner.id, notification_type=kind, opportunity_id=opp.id, payload={"internal": marker})
    db.commit()
    page = client.get("/notifications").text
    assert marker not in page and "?tab=review" in page and "?tab=submission" in page
    assert "/admin?section=settings" in page
    assert '&#128276;' in page
    assert client.get("/static/vendor/htmx.min.js").status_code == 200
    assert "unpkg.com" not in page
