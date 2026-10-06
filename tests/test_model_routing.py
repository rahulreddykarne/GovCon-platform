"""Model routing is visible, falls back when a key is missing, and can roll back."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import delete
from test_web_ui import _make_user

from govcon.ai.routing import MODEL_ROUTING, describe_route
from govcon.config import Settings
from govcon.models import AppSetting


def test_unconfigured_provider_names_the_known_good_fallback(db) -> None:
    from govcon.ai.routing import save_analysis_route

    user, _token = _make_user(db, f"route-{uuid4().hex[:8]}@example.test", "owner")
    settings = Settings(_env_file=None, deepseek_api_key="present-not-printed", ai_primary_provider="deepseek")
    try:
        save_analysis_route(db, settings, provider="anthropic", model="claude-test", actor=user)
        described = describe_route(db, settings)
        assert described["analysis_provider"] == "deepseek"
        assert described["analysis_model"] == "deepseek-flash"
        assert described["fallback_reason"]
        assert "not configured" in described["fallback_reason"]
        assert "present-not-printed" not in described["fallback_reason"]
        assert described["decision_fallback"] == "rules"
    finally:
        db.execute(delete(AppSetting).where(AppSetting.key == MODEL_ROUTING))
        db.commit()


def test_owner_can_save_and_roll_back_without_calling_a_provider(db, client) -> None:
    _, token = _make_user(db, f"route-ui-{uuid4().hex[:8]}@example.test", "owner")
    client.cookies.set("govcon_session", token)
    try:
        first = client.post("/settings/model-routing", data={
            "action": "save", "provider": "deepseek", "model": "deepseek-flash",
        }, follow_redirects=True)
        assert first.status_code == 200
        assert "deepseek-flash" in first.text
        assert "Known-good fallback" in first.text
        second = client.post("/settings/model-routing", data={
            "action": "save", "provider": "deepseek", "model": "deepseek-v4-pro",
        }, follow_redirects=True)
        assert "deepseek-v4-pro" in second.text
        version = describe_route(db, Settings(_env_file=None))["history"][-1]["version"]
        rolled = client.post("/settings/model-routing", data={"action": "rollback", "version": version}, follow_redirects=True)
        assert "deepseek-flash" in rolled.text
        active = describe_route(db, Settings(_env_file=None))
        assert active["analysis_model"] == "deepseek-flash"
    finally:
        db.execute(delete(AppSetting).where(AppSetting.key == MODEL_ROUTING))
        db.commit()
