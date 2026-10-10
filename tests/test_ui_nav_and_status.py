"""Task 4: main-flow navigation, status strip from real rows, smoke over every GET."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from test_web_ui import _make_user

from govcon.models import (
    IngestionRun,
    Opportunity,
    ProcessHeartbeat,
    SchedulerJobRun,
    Task,
    Watchlist,
)
from govcon.web.status_strip import status_strip


def _login(db, client, role="owner"):
    user, token = _make_user(db, f"{role}-{uuid4().hex}@nav.test", role)
    client.cookies.set("govcon_session", token)
    return user


def _opp(db) -> Opportunity:
    opp = Opportunity(source="sam", source_id=uuid4().hex, title=f"Nav {uuid4().hex[:6]}",
                      status="open", response_deadline=datetime.now(UTC) + timedelta(days=20), raw={}, links={})
    db.add(opp)
    db.flush()
    return opp


def test_status_strip_reads_real_rows_and_marks_stuck_jobs(db):
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    empty = status_strip(db, now=now)
    assert [s["text"] for s in empty["sources"]] == ["no successful pull"] * 3
    assert empty["jobs"] == []
    assert all(h["text"] == "no heartbeat" for h in empty["heartbeats"])

    db.add(IngestionRun(job="sam_opportunities", started_at=now - timedelta(hours=2),
                        finished_at=now - timedelta(hours=1), status="succeeded", fetched=4))
    db.add(IngestionRun(job="dibbs_index", started_at=now - timedelta(hours=3),
                        finished_at=now - timedelta(hours=3), status="completed_with_errors"))
    db.add(ProcessHeartbeat(role="worker", instance_id="w1", beat_at=now - timedelta(seconds=20), detail={}))
    db.add(ProcessHeartbeat(role="scheduler", instance_id="s1", beat_at=now - timedelta(minutes=10), detail={}))
    db.add(Task(task_type="opportunity_preparation", status="running", dedup_key=f"stuck-{uuid4().hex}",
                payload={}, input_revision={}, checkpoint={},
                lease_expires_at=now - timedelta(minutes=5), next_attempt_at=now))
    db.add(SchedulerJobRun(chain_name="nightly", trigger="manual", status="running",
                           started_at=now - timedelta(hours=5)))
    db.flush()

    strip = status_strip(db, now=now)
    by_key = {item["key"]: item for item in strip["sources"] + strip["jobs"] + strip["heartbeats"]}
    assert by_key["sam"]["tone"] == "ok" and "ago" in by_key["sam"]["text"]
    assert by_key["dibbs"]["tone"] == "warn" and "errors" in by_key["dibbs"]["text"]
    assert by_key["usaspending"]["text"] == "no successful pull"
    assert by_key["worker"]["tone"] == "ok"
    assert by_key["scheduler"]["tone"] == "danger"
    stuck = [item for item in strip["jobs"] if item["stuck"]]
    assert len(stuck) == 2
    assert all(item["tone"] == "danger" and "STUCK" in item["text"] for item in stuck)
    assert strip["has_stuck"] is True
    db.rollback()


def test_header_shows_the_strip_and_the_main_flow(db, client):
    _login(db, client)
    opp = _opp(db)
    db.add(IngestionRun(job="usaspending_awards", started_at=datetime.now(UTC),
                        finished_at=datetime.now(UTC), status="succeeded"))
    db.commit()
    inbox = client.get("/")
    assert inbox.status_code == 200
    text = inbox.text
    assert 'aria-label="Main"' in text
    assert ">Inbox<" in text and ">Pipeline<" in text
    assert ">Ops / Admin / Settings<" in text
    assert ">Bots<" in text and ">Insights<" in text and ">Watchlists<" in text
    assert 'data-strip="sam"' in text and 'data-strip="usaspending"' in text
    assert "USAspending" in text

    page = client.get(f"/workspace/{opp.id}")
    assert page.status_code == 200
    assert ">Summary<" in page.text and ">Compliance<" in page.text and ">Decision<" in page.text
    assert "Pricing / Products" in page.text and ">Documents<" in page.text
    assert ">Opportunity<" in page.text


def test_old_workspace_tabs_still_resolve(db, client):
    _login(db, client, "reviewer")
    opp = _opp(db)
    db.commit()
    for tab, marker in (
        ("overview", "Status"),
        ("review", "Scorecard"),
        ("sourcing", "Supplier quotes"),
        ("documents", "Documents"),
        ("summary", "Status"),
        ("decision", "Human approval required"),
    ):
        page = client.get(f"/workspace/{opp.id}?tab={tab}")
        assert page.status_code == 200, tab
        assert marker in page.text, (tab, marker)


def test_every_get_route_fails_the_suite_on_a_500(db, client):
    """Hit every registered GET route. A 500 fails the test; 401/403/404/422 are allowed."""
    from govcon.web.app import create_app

    user = _login(db, client)
    opp = _opp(db)
    watchlist = Watchlist(name=f"Nav {uuid4().hex[:6]}", enabled=True)
    db.add(watchlist)
    db.flush()
    samples = {
        "opp_id": opp.id,
        "wl_id": watchlist.id,
        "req_id": 1,
        "task_id": 1,
        "user_id": user.id,
        "notification_id": 1,
        "quote_id": 1,
        "suggestion_id": 1,
        "approval_id": 1,
        "bot_name": "discovery",
        "kind": "summary",
        "view": "usage",
        "action": "retry",
    }
    app = create_app()
    failures: list[str] = []
    checked = 0
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", None)
        if "GET" not in methods or not path or path.startswith("/static"):
            continue
        try:
            url = path.format(**samples)
        except KeyError:
            continue
        response = client.get(url)
        checked += 1
        if response.status_code >= 500:
            failures.append(f"{url} → {response.status_code}")
    assert checked >= 20, f"too few GET routes exercised: {checked}"
    assert not failures, "GET routes returned 500:\n" + "\n".join(failures)
