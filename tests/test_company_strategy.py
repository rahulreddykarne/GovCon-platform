"""Company strategy stays missing until an owner saves a value."""

from __future__ import annotations

from uuid import uuid4

from test_web_ui import _make_user

from govcon.company.strategy import missing_labels
from govcon.compliance.pipeline import load_company_facts
from govcon.config import Settings
from govcon.workflow.app_settings import COMPANY_STRATEGY, get_setting


def test_blank_strategy_is_missing_and_not_zero(db) -> None:
    facts = load_company_facts(Settings(_env_file=None), db)
    assert facts.get("minimum_margin_pct") is None
    assert "NAICS" in facts["_strategy_missing"]
    assert "Minimum margin" in facts["_strategy_missing"]
    assert missing_labels(get_setting(db, COMPANY_STRATEGY)) == facts["_strategy_missing"]


def test_settings_saves_naics_and_leaves_a_blank_margin_missing(db, client) -> None:
    from sqlalchemy import delete

    from govcon.models import AppSetting

    _user, token = _make_user(db, f"strategy-{uuid4().hex[:8]}@example.test", "owner")
    client.cookies.set("govcon_session", token)
    try:
        _assert_saved_strategy(db, client)
    finally:
        db.execute(delete(AppSetting).where(AppSetting.key == COMPANY_STRATEGY))
        db.commit()


def _assert_saved_strategy(db, client) -> None:
    page = client.get("/settings").text
    assert "Company strategy" in page and "Missing:" in page and "NAICS" in page
    saved = client.post("/settings", data={
        "reviewer_mode": "manual",
        "strategy_form": "on",
        "strategy_naics_codes": "336413",
        "strategy_minimum_margin_pct": "",
    }, follow_redirects=True)
    assert saved.status_code == 200
    assert "336413" in saved.text
    facts = load_company_facts(Settings(_env_file=None), db)
    assert facts["naics_codes"] == ["336413"]
    assert "NAICS" not in facts["_strategy_missing"]
    assert "Minimum margin" in facts["_strategy_missing"]
    overview = client.get("/operate").text
    assert "Missing, and not guessed" in overview
    assert "Minimum margin" in overview
    assert "NAICS" not in overview.split("Missing, and not guessed", 1)[1].split(".", 1)[0]
