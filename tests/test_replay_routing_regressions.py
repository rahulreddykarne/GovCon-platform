"""Regressions from the 2026-10-10 review: replay checkpoints, saved routes and JEV fallback."""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from govcon.config import Settings
from govcon.security.classification import DataClassification


def _deepseek_settings(**overrides) -> Settings:
    values = {"deepseek_api_key": "synthetic-test-key", "ai_primary_provider": "deepseek",
              "ai_budget_usd_per_million_tokens": 2.0, "ai_max_provider_retries": 0}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _serve_deepseek(monkeypatch, calls: list[int]) -> None:
    """The real DeepSeek provider, answered by a local transport instead of the network."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"summary": "ok"}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        })

    monkeypatch.setattr("govcon.http.build_client",
                        lambda *a, **k: httpx.Client(transport=httpx.MockTransport(handler)))


# ── F1, F2: a DeepSeek response and its reservation survive a checkpoint ─────

def test_deepseek_response_and_reservation_survive_a_worker_restart(upgraded_engine, monkeypatch):
    from govcon.ai.budget import complete_with_budget
    from govcon.ai.providers import get_provider
    from govcon.ai.providers.base import CompletionResult
    from govcon.ai.replay import run_recorded

    settings = _deepseek_settings()
    provider = get_provider(settings)
    calls: list[int] = []
    _serve_deepseek(monkeypatch, calls)

    def service():
        return complete_with_budget(provider, None, opportunity_id=None, settings=settings, engine=upgraded_engine,
                                    system_prompt="system", user_prompt="user", temperature=0.0, json_mode=True,
                                    classification=DataClassification.PUBLIC, purpose="solicitation_summary")

    snapshots: list[dict] = []
    result, reservation = run_recorded(service, persist=snapshots.append)
    assert calls == [1]
    assert isinstance(result, CompletionResult), "DeepSeek returns the shared result type"
    assert reservation.cost is not None

    # The checkpoint is stored as JSON; a new worker resumes from it.
    saved = json.loads(json.dumps(snapshots[-1]))
    restored_result, restored_reservation = run_recorded(service, restore=saved)
    assert calls == [1], "the paid call is not repeated after a restart"
    assert (restored_result.content, restored_result.provider, restored_result.usage) == (
        result.content, "deepseek", result.usage)
    assert restored_reservation.id == reservation.id
    assert restored_reservation.cost == reservation.cost and isinstance(restored_reservation.cost, Decimal)
    assert restored_reservation.rate == reservation.rate
    restored_reservation.finish(restored_result)  # already settled: a no-op


def test_recorded_deepseek_error_keeps_its_status_code():
    from govcon.ai.providers.deepseek import DeepSeekAPIError
    from govcon.ai.replay import Recorder, active_recorder, run_recorded

    def failing():
        raise DeepSeekAPIError(503)

    def service():
        try:
            active_recorder().call({"prompt": "x"}, failing)
        except DeepSeekAPIError as exc:
            return exc.status_code
        return None

    snapshots: list[dict] = []
    assert run_recorded(service, persist=snapshots.append) == 503
    restored = Recorder.restore(json.loads(json.dumps(snapshots[-1])))
    [outcome] = restored._outcomes.values()
    assert isinstance(outcome.error, DeepSeekAPIError) and outcome.error.status_code == 503


def test_checkpoints_written_before_reservations_kept_metadata_still_restore():
    from govcon.ai.replay import _thaw

    legacy = _thaw({"__replay__": "reservation"})
    assert legacy.cost is None and legacy.id is None
    legacy.finish()


# ── F3: availability follows the saved route ─────────────────────────────────

@pytest.fixture()
def anthropic_route(db):
    from test_web_ui import _make_user

    from govcon.ai.routing import MODEL_ROUTING, save_analysis_route
    from govcon.models import AppSetting

    settings = Settings(_env_file=None, ai_primary_provider="deepseek", deepseek_api_key=None,
                        anthropic_api_key="synthetic-test-key")
    user, _token = _make_user(db, f"route-avail-{uuid4().hex[:8]}@example.test", "owner")
    save_analysis_route(db, settings, provider="anthropic", model="claude-test", actor=user)
    db.flush()
    try:
        yield settings
    finally:
        db.execute(delete(AppSetting).where(AppSetting.key == MODEL_ROUTING))
        db.commit()


def test_saved_route_with_a_key_counts_as_available_when_the_default_has_none(db, anthropic_route):
    from govcon.ai.providers import provider_available
    from govcon.ai.routing import analysis_available, analysis_selection

    assert not provider_available(anthropic_route), "the settings-only check sees just the missing DeepSeek key"
    assert analysis_available(db, anthropic_route)
    assert analysis_selection(db, anthropic_route, provider_name=None, model=None)[:2] == ("anthropic", "claude-test")


def test_no_key_on_the_route_or_the_known_good_fallback_is_unavailable(db):
    from govcon.ai.routing import analysis_available

    assert not analysis_available(db, Settings(_env_file=None, ai_primary_provider="deepseek", deepseek_api_key=None))
    assert analysis_available(None, Settings(_env_file=None, deepseek_api_key="synthetic-test-key"))


def test_llm_decision_fallback_calls_the_routed_provider(db, anthropic_route, monkeypatch):
    from govcon.ai.providers import NoProviderConfigured
    from govcon.decision.provider import DecisionProviderUnavailable
    from govcon.decision.providers.llm_fallback import LLMDecisionProvider

    asked: list[str | None] = []

    def fake_get_provider(settings, *, provider_name=None):
        asked.append(provider_name)
        raise NoProviderConfigured("stop before any call")

    monkeypatch.setattr("govcon.decision.providers.llm_fallback.get_provider", fake_get_provider)
    with pytest.raises(DecisionProviderUnavailable):
        LLMDecisionProvider(anthropic_route, session=db).decide(bundle_name="bid_decision", bundle_version="v1",
                                                                state={})
    assert asked == ["anthropic"]


# ── F4: an invalid JEV answer value falls back to the rules result ───────────

def test_invalid_jev_answer_value_falls_back_to_rules(upgraded_engine, monkeypatch):
    from test_decision_engine import _base_state, _create_opportunity

    from govcon.decision.engine import run_decision_bundle
    from govcon.decision.providers.jev import JevDecisionProvider

    def post(self, endpoint, body, headers, bundle_name, budget_opportunity_id, *, session=None, engine=None):
        # "BID" is not one of the bundle's recommendation values.
        return httpx.Response(200, json={"answers": {"recommendation": {"choice": "BID", "confidence": 0.9}}}), 5

    monkeypatch.setattr(JevDecisionProvider, "_post", post)
    settings = Settings(_env_file=None, jev_enabled=True, jev_api_key="synthetic-test-key",
                        decision_primary_provider="jev", decision_fallback_provider="rules",
                        ai_external_allowed_for_proprietary=True)
    with Session(upgraded_engine) as session:
        opp = _create_opportunity(session)
        execution = run_decision_bundle(session, opportunity_id=opp.id, bundle_name="bid_decision",
                                        state=_base_state(opp), settings=settings)
        session.rollback()

    assert execution.provider == "rules"
    assert execution.result["recommendation"] in {"bid", "no_bid", "review", "insufficient_information"}
    assert execution.fallback_reason and "DecisionProviderInvalidResponse" in execution.fallback_reason


def test_valid_jev_answer_is_still_used(upgraded_engine, monkeypatch):
    from test_decision_engine import _base_state, _create_opportunity

    from govcon.decision.engine import run_decision_bundle
    from govcon.decision.providers.jev import JevDecisionProvider

    def post(self, endpoint, body, headers, bundle_name, budget_opportunity_id, *, session=None, engine=None):
        return httpx.Response(200, json={"answers": {"recommendation": {"choice": "review", "confidence": 0.9}}}), 5

    monkeypatch.setattr(JevDecisionProvider, "_post", post)
    settings = Settings(_env_file=None, jev_enabled=True, jev_api_key="synthetic-test-key",
                        decision_primary_provider="jev", ai_external_allowed_for_proprietary=True)
    with Session(upgraded_engine) as session:
        opp = _create_opportunity(session)
        execution = run_decision_bundle(session, opportunity_id=opp.id, bundle_name="bid_decision",
                                        state=_base_state(opp), settings=settings)
        session.rollback()

    assert execution.provider == "jev" and execution.fallback_reason is None
    assert execution.result["recommendation"] == "review"
