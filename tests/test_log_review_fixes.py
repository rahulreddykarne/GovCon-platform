"""Fixes from the 2026-10-10 log review: pursuits on non-open notices, JSON parsing, JEV accounting."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.providers.base import CompletionResult
from govcon.ai.providers.deepseek import parse_json_response
from govcon.config import Settings
from govcon.models import Opportunity, Pursuit


def _result(content: str) -> CompletionResult:
    return CompletionResult(content=content, model="deepseek-flash", provider="deepseek")


# ── 6. tolerant JSON parsing and unknown summary keys ────────────────────────

@pytest.mark.parametrize("content", [
    '{"summary": "line one\nline two\twith a tab"}',            # raw control characters inside a string
    'Here is the analysis:\n{"summary": "ok"}\nThanks.',        # prose around the object
    'Result:\n```json\n{"summary": "ok"}\n```',                 # a fence that is not at the start
    '{"summary": "ok"}\n{"summary": "second"}',                 # trailing extra data
])
def test_common_json_mode_slips_still_parse(content):
    assert parse_json_response(_result(content))["summary"].startswith(("line one", "ok"))


@pytest.mark.parametrize("content", ["", "   ", "no json here", "[1, 2"])
def test_empty_or_non_json_answers_still_raise(content):
    with pytest.raises(json.JSONDecodeError):
        parse_json_response(_result(content))


def test_unknown_top_level_summary_keys_are_dropped_not_rejected():
    from pydantic import ValidationError

    from govcon.ai.schemas import SolicitationAnalysisV1
    from govcon.ai.structured import _drop_unknown_root_keys

    prepared = SimpleNamespace(schema_cls=SolicitationAnalysisV1, schema_version="solicitation_analysis.v1")
    answer = {"summary": "Nitrile gloves, 500 boxes.", "confidence_note": "invented by the model"}
    with pytest.raises(ValidationError):
        SolicitationAnalysisV1.model_validate(answer)
    cleaned = _drop_unknown_root_keys(prepared, answer)
    assert "confidence_note" not in cleaned
    assert SolicitationAnalysisV1.model_validate(cleaned).summary == "Nitrile gloves, 500 boxes."
    # Content only under invented keys is still an empty, rejected summary.
    with pytest.raises(ValidationError):
        SolicitationAnalysisV1.model_validate(_drop_unknown_root_keys(prepared, {"analysis": "text"}))


def test_lenient_schemas_are_left_untouched():
    from govcon.ai.schemas import MarketAnalysisV1
    from govcon.ai.structured import _drop_unknown_root_keys

    prepared = SimpleNamespace(schema_cls=MarketAnalysisV1, schema_version="market_analysis.v1")
    data = {"anything": 1}
    assert _drop_unknown_root_keys(prepared, data) is data


# ── 1. only open notices can be pursued ──────────────────────────────────────

def _opportunity(session, status):
    opp = Opportunity(source="sam", source_id=f"lr-{uuid4().hex}", title="Gloves, surgeons'", status=status,
                      response_deadline=datetime.now(UTC) + timedelta(days=10), raw={}, links={})
    session.add(opp)
    session.commit()
    return opp.id


@pytest.mark.parametrize("status", ["awarded", "notice_only", "closed", "cancelled", "archived"])
def test_non_open_notices_cannot_be_pursued(upgraded_engine, status):
    from govcon.workflow.pursuits import create_or_get_pursuit

    with Session(upgraded_engine) as session:
        opp_id = _opportunity(session, status)
        with pytest.raises(ValueError, match="only open solicitations can be pursued"):
            create_or_get_pursuit(session, opportunity_id=opp_id, actor=None, origin="web_workspace", prepare=False)
        session.rollback()
        assert session.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp_id)) is None


def test_open_notices_and_recorded_decisions_still_create_pursuits(upgraded_engine):
    from govcon.workflow.pursuits import create_or_get_pursuit

    with Session(upgraded_engine) as session:
        open_id = _opportunity(session, "open")
        _, created = create_or_get_pursuit(session, opportunity_id=open_id, actor=None, origin="cli", prepare=False)
        assert created
        # A reviewer's recorded decision on a closed notice (for example no-bid) is not a new bid.
        closed_id = _opportunity(session, "closed")
        _, created = create_or_get_pursuit(session, opportunity_id=closed_id, actor=None, origin="approval", prepare=False)
        assert created
        session.rollback()


def test_workspace_shows_the_reason_instead_of_a_pursue_button(upgraded_engine):
    from test_web_ui import _make_user
    from web_client import CsrfTestClient

    from govcon.web.app import create_app

    with Session(upgraded_engine) as session:
        opp_id = _opportunity(session, "awarded")
        _, token = _make_user(session, f"lr-{uuid4().hex}@example.test", "reviewer")
        session.commit()
    with CsrfTestClient(create_app(), follow_redirects=False) as client:
        client.cookies.set("govcon_session", token)
        page = client.get(f"/workspace/{opp_id}").text
        assert "not open for offers" in page and f'action="/opp/{opp_id}/start-workspace"' not in page
        response = client.post(f"/opp/{opp_id}/start-workspace", data={})
        assert response.status_code == 303 and "error=" in response.headers["location"]


# ── 5. JEV calls that answered are recorded as succeeded ─────────────────────

@pytest.mark.parametrize("status_code,expected", [(200, "succeeded"), (503, "failed")])
def test_jev_reservation_status_follows_the_http_answer(monkeypatch, status_code, expected):
    from govcon.decision.providers.jev import JevDecisionProvider

    finished = []

    class _Reservation:
        def finish(self, result=None):
            finished.append("succeeded" if result is not None else "failed")
            assert result is None or result.usage == {}  # no usage: the full reservation is kept

    monkeypatch.setattr("govcon.decision.providers.jev.reserve", lambda *a, **k: _Reservation())
    monkeypatch.setattr("govcon.http.build_client", lambda *a, **k: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status_code, json={"answers": {}}))))
    provider = JevDecisionProvider(api_key="test-key", settings=Settings(_env_file=None))
    response, _latency = provider._post("https://jev.example.test/v1/systemone", {"state": {}}, {}, "bid_decision", None)
    assert response.status_code == status_code and finished == [expected]


# ── 4. five web searches by default ──────────────────────────────────────────

def test_market_price_search_allows_five_searches_by_default():
    from govcon.sourcing.market_prices import server_tools

    settings = Settings(_env_file=None)
    assert settings.market_price_max_searches == 5
    assert server_tools(settings)[0]["max_uses"] == 5
