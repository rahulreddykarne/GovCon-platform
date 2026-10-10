"""The operator shell matches the light redesign and only shows stored data."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from jinja2 import Environment, FileSystemLoader, select_autoescape
from test_web_ui import _make_opp, _make_user

_ROOT = "src/govcon/web/templates"
_SAMPLE = ("SAMPLE DATA", "INTERACTIVE CONCEPT", "DLA aircraft parts", "7 of 8 services healthy")


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(_ROOT), autoescape=select_autoescape(["html"]))
    env.globals["csrf_token"] = lambda request: "test-token"
    env.globals["can"] = lambda user, perm: True
    return env


def _user() -> SimpleNamespace:
    return SimpleNamespace(display_name="Ada Owner", email="ada@example.test", role="owner")


def _render(name: str, **ctx: object) -> str:
    base = {
        "request": None,
        "current_user": _user(),
        "active_page": "inbox",
        "unread_notification_count": 0,
        "flash_error": None,
        "flash_notice": None,
    }
    base.update(ctx)
    return _env().get_template(name).render(**base)


def _assert_shell(html: str, section: str | None = None) -> None:
    assert "brandmark" in html
    assert "app-tabs" in html
    assert "GovCon" in html
    for phrase in _SAMPLE:
        assert phrase not in html
    if section:
        assert section in html


def test_login_uses_the_light_mark() -> None:
    html = _env().get_template("login.html").render(request=None, error=None, csrf_token=lambda request: "t")
    assert "brandmark" in html
    assert "Sign in" in html
    assert "GovCon" in html
    for phrase in _SAMPLE:
        assert phrase not in html


def test_inbox_empty_state_has_no_sample_rows() -> None:
    html = _render(
        "inbox.html",
        pending_approvals=0,
        orchestrator=None,
        unhealthy=[],
        promising=[],
        groups=[],
        total_count=0,
        page=1,
        pages=1,
    )
    _assert_shell(html, "Inbox")
    assert "No new matches." in html
    assert "not run" in html


def test_inbox_labels_a_stored_estimate() -> None:
    html = _render(
        "inbox.html",
        pending_approvals=0,
        orchestrator=None,
        unhealthy=[],
        promising=[],
        total_count=1,
        page=1,
        pages=1,
        groups=[{
            "watchlist_name": "Stored watchlist",
            "matches": [{
                "match_id": 1,
                "opp_id": 9,
                "title": "Stored notice title",
                "source": "sam",
                "psc": "7110",
                "agency": "Test Agency",
                "deadline_label": "30d left",
                "deadline_class": "deadline-ok",
                "estimated_value": "~$200",
                "matched_on": "PSC 71",
                "watchlists": ["Stored watchlist"],
                "alerted": False,
                "rank_score": 40,
                "rank_factors": [],
                "needs_eligibility_decision": False,
                "analysis_label": "not run",
                "analysis_detail": "No document run is stored.",
            }],
        }],
    )
    assert "Stored notice title" in html
    assert "estimated ~$200" in html
    assert "guaranteed" not in html.lower()


def test_bots_empty_state_names_a_missing_run() -> None:
    spec = SimpleNamespace(
        title="Orchestrator", trigger="manual", inputs="stored notices",
        outputs="a run row", permissions="owner", failure="the run stays incomplete",
    )
    html = _render(
        "bots.html",
        active_page="bots",
        can_decide=True,
        pending=[],
        cards=[{"spec": spec, "run": None, "why": "No run is stored.", "evidence": []}],
    )
    _assert_shell(html, "Approvals")
    assert "Nothing is waiting for a decision." in html
    assert "No run yet" in html


def test_learning_empty_state_does_not_invent_a_win_rate() -> None:
    stats = SimpleNamespace(
        submitted=0, won=0, lost=0, no_bid=0, win_rate=0, avg_margin=None,
        avg_days_cycle=None, win_profile_available=False,
        win_profile_note="Not enough recorded outcomes.", win_profile_minimum=3,
    )
    html = _render(
        "learning.html",
        active_page="learning",
        capture_counts={"suggested": 0, "confirmed": 0, "dismissed": 0},
        capture_rows=[],
        evals={"note": "No eval case is stored.", "sample_size": 0, "passed": 0, "failed": 0, "cases": []},
        calculations={"submitted": 0, "won": 0, "lost": 0, "no_bid": 0, "win_rate": None, "margin": None, "margin_sample": 0},
        stats=stats,
        last_refresh=None,
        by_psc=[],
        by_agency=[],
        by_size=[],
        no_bid_reasons=[],
        loss_reasons=[],
        common_competitors=[],
        reliable_suppliers=[],
        recent_outcomes=[],
    )
    _assert_shell(html, "Insights")
    assert "No outcome suggestion is stored." in html
    assert "Win rate is not computed" in html
    assert "—" in html


def test_operate_overview_empty_counts_stay_zero() -> None:
    html = _render(
        "operate.html",
        active_page="operate",
        view="overview",
        overview={
            "greeting": "No greeting is stored.",
            "strategy_missing": ["naics"],
            "pending_count": 0,
            "running_count": 0,
            "new_match_count": 0,
            "healthy_count": 0,
            "check_count": 0,
            "pending": [],
            "activity": [],
            "current": None,
            "attention": [],
        },
        agents=None,
        architecture=None,
        integrations=None,
        guide=None,
        routing=None,
    )
    _assert_shell(html, "Mission control")
    assert "No bot approval is pending." in html
    assert "No bot run is stored yet." in html
    assert ">0<" in html


def test_operator_pages_render_from_the_database(client, db) -> None:
    _, token = _make_user(db, f"ui-{uuid4().hex[:8]}@example.test", "owner")
    opp = _make_opp(db)
    cookies = {"govcon_session": token}
    pages = {
        "/": b"Inbox",
        "/operate": b"Mission control",
        "/operate/agents": b"Orchestrator",
        "/operate/architecture": b"SAM.gov",
        "/operate/integrations": b"Not configured",
        "/operate/how": b"How GovCon works",
        "/bots": b"Bots",
        "/ops": b"Operations",
        "/learning": b"Learning",
        "/pipeline": b"Pipeline",
        "/search": b"Search",
        "/watchlists": b"Watchlist",
        "/vendors": b"Vendor",
        "/suppliers": b"Supplier",
        "/settings": b"Settings",
        f"/workspace/{opp.id}": opp.title.encode(),
        f"/opp/{opp.id}": opp.title.encode(),
    }
    for path, marker in pages.items():
        resp = client.get(path, cookies=cookies)
        assert resp.status_code == 200, path
        body = resp.content
        assert b"brandmark" in body, path
        assert b"app-tabs" in body, path
        assert marker in body, path
        for phrase in _SAMPLE:
            assert phrase.encode() not in body, path
    login = client.get("/login")
    assert login.status_code == 200
    assert b"Sign in" in login.content
    assert b"brandmark" in login.content
