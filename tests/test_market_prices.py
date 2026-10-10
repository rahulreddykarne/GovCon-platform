"""Web market price research: Claude web search for a pursued product's prices, feeding margin math."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.providers.base import CompletionResult
from govcon.config import Settings
from govcon.models import MarketPriceRun, Opportunity, Pursuit, Task
from govcon.sourcing import market_prices as mp


def _settings(**values) -> Settings:
    base = {"anthropic_api_key": "sk-ant-test-key-0123456789", "anthropic_model": "claude-opus-5-5",
            "ai_max_provider_retries": 0}
    base.update(values)
    return Settings(_env_file=None, **base)


def _listing(seller, price, units, match="match", *, url=None, currency="USD", **extra):
    return {"seller": seller, "product_title": f"{seller} nitrile exam gloves", "url": url or f"https://{seller.lower()}.example.com/p",
            "price": price, "currency": currency, "pack_description": f"{units} boxes" if units else None,
            "units_per_listing": units, "spec_match": match, "spec_gaps": [], **extra}


# Seven listings: one government site, one past the five-listing limit.
LISTINGS = [
    _listing("Alpha", 80.00, 10),                         # $8.00 / BX, match
    _listing("Bravo", 9.50, 1),                           # $9.50 / BX, match
    _listing("Federal", 7.00, 1, url="https://www.gsaadvantage.gov/item/1"),
    _listing("Charlie", 85.00, 10, "partial"),            # $8.50 / BX, partial
    _listing("Delta", 5.00, 1, "mismatch"),               # not used: contradicts the spec
    _listing("Echo", 12.00, None, "unknown"),             # pack size unclear
    _listing("Foxtrot", 9.00, 1),                         # sixth valid listing: over the limit
]
ANSWER = json.dumps({"product_searched": "Nitrile exam gloves, box of 100", "listings": LISTINGS, "notes": None})


def _inp(**kw) -> mp.ResearchInput:
    values = {"opportunity_id": 1, "task_id": None, "source_revision": "r1", "unit": "BX", "quantity": Decimal(10)}
    values.update(kw)
    return mp.ResearchInput(**values)


# ── answer cleaning and the estimate ─────────────────────────────────────────

def test_listings_drop_government_sites_and_stop_at_five():
    kept, excluded = mp.clean_listings(LISTINGS, _inp(), limit=5)
    assert [item["seller"] for item in kept] == ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    reasons = {item["domain"]: item["reason"] for item in excluded}
    assert reasons["www.gsaadvantage.gov"] == "government site"
    assert reasons["foxtrot.example.com"] == "over the 5-listing limit"
    assert kept[0]["unit_price"] == "8.0000" and kept[4]["unit_price"] is None


@pytest.mark.parametrize("url,reason", [
    ("https://supply.army.mil/x", "government site"),
    ("https://shop.state.fed.us/x", "government site"),
    ("javascript:alert(1)", "not an http(s) link"),
])
def test_unsafe_or_government_links_are_excluded(url, reason):
    kept, excluded = mp.clean_listings([_listing("X", 10, 1, url=url)], _inp(), limit=5)
    assert not kept and excluded[0]["reason"] == reason


def test_other_currencies_duplicates_and_invalid_listings_are_excluded():
    rows = [_listing("A", 10, 1), _listing("A", 10, 1), _listing("B", 10, 1, currency="EUR"), {"seller": "C", "price": -1}]
    kept, excluded = mp.clean_listings(rows, _inp(), limit=5)
    assert len(kept) == 1
    assert [e["reason"].split(" (")[0] for e in excluded] == ["duplicate link", "priced in EUR", "invalid listing"]


def test_estimate_is_the_median_of_listings_that_fit_the_specification():
    kept, _ = mp.clean_listings(LISTINGS, _inp(), limit=5)
    est = mp.estimate(kept, Decimal(10))
    assert est.unit_cost == Decimal("8.5000") and (est.low, est.high) == (Decimal("8.0000"), Decimal("9.5000"))
    assert est.total == Decimal("85.00") and est.confidence == "medium"
    assert [item["used_in_estimate"] for item in kept] == [True, True, True, False, False]
    assert "not a supplier quote" in est.basis


def test_unconfirmed_listings_give_a_low_confidence_estimate_and_mismatches_none():
    unknown, _ = mp.clean_listings([_listing("A", 10, 1, "unknown")], _inp(), limit=5)
    assert mp.estimate(unknown, None).confidence == "low" and mp.estimate(unknown, None).total is None
    mismatch, _ = mp.clean_listings([_listing("A", 10, 1, "mismatch")], _inp(), limit=5)
    assert mp.estimate(mismatch, None).unit_cost is None


def test_answer_is_found_among_prose():
    text = 'I searched three sites. {"draft": true} Final: ' + ANSWER + " Done."
    assert mp.extract_answer(text)["product_searched"] == "Nitrile exam gloves, box of 100"
    assert mp.extract_answer("no json here") is None


def test_search_tools_block_government_sites_and_cap_usage():
    tools = mp.server_tools(_settings(market_price_max_searches=2, market_price_max_fetches=0))
    assert [t["type"] for t in tools] == ["web_search_20260209"]
    assert tools[0]["max_uses"] == 2 and "sam.gov" in tools[0]["blocked_domains"]
    assert "at most 5 listings" in mp.system_prompt(_settings())


@pytest.mark.parametrize("fields,expected", [
    ({"psc_code": "6515"}, True),
    ({"psc_code": "R499"}, False),
    ({"psc_code": None, "nsn": "6515-01-519-8818"}, True),
    ({"psc_code": "R499", "source": "dibbs"}, True),
    ({"psc_code": None}, False),
])
def test_product_opportunities_are_recognized(fields, expected):
    opp = SimpleNamespace(source=fields.get("source", "sam"), nsn=fields.get("nsn"), psc_code=fields.get("psc_code"))
    assert (mp.product_reason(opp, []) is not None) is expected


# ── provider: server tools and paused turns ──────────────────────────────────

def _message(content, *, stop_reason="end_turn", searches=1):
    return {"id": "msg", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 100, "server_tool_use": {"web_search_requests": searches}}}


def test_anthropic_server_tools_resume_a_paused_turn_and_sum_usage():
    from govcon.ai.providers.anthropic import AnthropicProvider
    from govcon.security.classification import DataClassification

    search = [{"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "nitrile gloves"}},
              {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
               "content": [{"type": "web_search_result", "url": "https://alpha.example.com", "title": "Alpha",
                            "encrypted_content": "abc", "page_age": None}]}]
    replies = [_message(search, stop_reason="pause_turn"), _message([{"type": "text", "text": ANSWER}], searches=2)]
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=replies[len(seen) - 1])

    settings = _settings()
    provider = AnthropicProvider("sk-ant-test-key-0123456789", model="claude-opus-5-5", settings=settings,
                                 http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), max_retries=0)
    result = provider.complete(system_prompt="s", user_prompt="u", json_mode=True, classification=DataClassification.PUBLIC,
                               purpose="market_price_research", server_tools=mp.server_tools(settings))
    assert result.content == ANSWER and result.finish_reason == "end_turn"
    assert result.usage["web_search_requests"] == 3 and result.usage["input_tokens"] == 2000
    assert seen[0]["tools"][0]["type"] == "web_search_20260209"
    assert seen[1]["messages"][1]["role"] == "assistant"
    assert seen[1]["messages"][1]["content"][0]["type"] == "server_tool_use"


# ── end to end against the database ──────────────────────────────────────────

class _FakeClaude:
    name = "anthropic"

    def __init__(self, content=ANSWER):
        self.calls = []
        self.content = content

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return CompletionResult(content=self.content, model="claude-opus-5-5", provider="anthropic",
                                usage={"input_tokens": 1200, "output_tokens": 300, "web_search_requests": 2},
                                latency_ms=40, finish_reason="end_turn")


def _opportunity(session, **kw):
    values = {"source": "sam", "source_id": f"mp-{uuid4().hex}", "title": "Gloves, patient examination, nitrile",
              "status": "open", "psc_code": "6515", "nsn": "6515-01-519-8818", "quantity": Decimal(10), "unit": "BX",
              "response_deadline": datetime.now(UTC) + timedelta(days=20), "raw": {}, "links": {}}
    values.update(kw)
    opp = Opportunity(**values)
    session.add(opp)
    session.commit()
    session.refresh(opp)
    session.expunge(opp)  # a detached copy whose loaded columns stay readable after later commits
    return opp


@pytest.fixture()
def fake_claude(monkeypatch):
    fake = _FakeClaude()
    monkeypatch.setattr("govcon.ai.providers.get_provider", lambda settings, provider_name=None: fake)
    return fake


def test_search_feeds_the_estimate_into_margin_math(upgraded_engine, fake_claude, caplog):
    from govcon.decision.engine import build_decision_state

    settings = _settings()
    with Session(upgraded_engine) as session:
        opp = _opportunity(session)
        session.add(Pursuit(opportunity_id=opp.id, quote_price=Decimal(120)))
        session.commit()
        inp = mp.build_research_input(session, opp.id, settings)
        session.commit()
    assert inp.status == "ready" and inp.product["nsn"] == "6515-01-519-8818" and inp.unit == "BX"

    with caplog.at_level(logging.INFO, logger="govcon.sourcing.market_prices"):
        outcome = mp.run_research(inp, settings, engine=upgraded_engine)
    call = fake_claude.calls[0]
    assert call["purpose"] == "market_price_research" and call["server_tools"][0]["type"] == "web_search_20260209"
    assert json.loads(call["user_prompt"].split("\n", 1)[1])["unit"] == "BX"
    assert "domain=www.gsaadvantage.gov reason=government site" in caplog.text
    assert "PRODUCT_JSON" not in caplog.text and "ROLE" not in caplog.text  # the prompt is never logged

    with Session(upgraded_engine) as session:
        run = mp.store_run(session, outcome, settings)
        session.commit()
        assert run.status == "completed" and len(run.listings) == 5
        assert run.estimate_unit_cost == Decimal("8.5000") and run.estimated_total_cost == Decimal("85.00")
        assert run.prompt_version == mp.PROMPT_VERSION and run.usage["web_search_requests"] == 2
        assert "8.50 per BX" in mp.run_summary(run)

        opportunity = session.get(Opportunity, opp.id)
        pursuit = session.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp.id))
        basis = mp.effective_cost_basis(session, opportunity, pursuit, Decimal(10))
        assert (basis.basis, basis.total, basis.market_price_run_id) == ("web_estimate", Decimal("85.00"), run.id)

        pricing = build_decision_state(session, opp.id)["pricing"]
        assert pricing["cost_basis"] == "web_estimate" and pricing["supplier_cost"] == 85.0
        assert pricing["margin_pct"] == 41.2  # (120 - 85) / 85
        assert pricing["margin_basis"] == "proposed price vs web_estimate cost"
        assert pricing["unit_cost"] == 8.5

        # A recorded sourcing cost takes precedence over the web estimate.
        pursuit.sourcing_cost = Decimal(100)
        session.commit()
        pricing = build_decision_state(session, opp.id)["pricing"]
        assert pricing["cost_basis"] == "pursuit" and pricing["margin_pct"] == 20.0
        session.rollback()


def test_service_opportunities_are_skipped_without_a_call(upgraded_engine, fake_claude):
    settings = _settings()
    with Session(upgraded_engine) as session:
        opp = _opportunity(session, psc_code="R499", nsn=None)
        inp = mp.build_research_input(session, opp.id, settings)
        outcome = mp.run_research(inp, settings, engine=upgraded_engine)
        run = mp.store_run(session, outcome, settings)
        session.commit()
        assert run.status == "skipped" and "not a product opportunity" in run.note
        assert mp.effective_cost_basis(session, opp, None, None).basis is None
    assert not fake_claude.calls


def test_missing_key_and_bad_answers_are_recorded_not_raised(upgraded_engine, fake_claude):
    with Session(upgraded_engine) as session:
        opp = _opportunity(session)
        no_key = mp.build_research_input(session, opp.id, _settings(anthropic_api_key=None))
        assert no_key.status == "skipped" and "ANTHROPIC_API_KEY" in no_key.note
        fake_claude.content = "I could not find prices."
        inp = mp.build_research_input(session, opp.id, _settings())
        session.commit()
    outcome = mp.run_research(inp, _settings(), engine=upgraded_engine)
    assert outcome.status == "failed" and "not the expected JSON" in outcome.note


def test_estimate_is_ignored_once_the_solicitation_changes(upgraded_engine, fake_claude):
    settings = _settings()
    with Session(upgraded_engine) as session:
        opp = _opportunity(session)
        inp = mp.build_research_input(session, opp.id, settings)
        session.commit()
    outcome = mp.run_research(inp, settings, engine=upgraded_engine)
    with Session(upgraded_engine) as session:
        mp.store_run(session, outcome, settings)
        session.commit()
        assert mp.latest_estimate(session, opp.id) is not None
        session.get(Opportunity, opp.id).raw_hash = "amended-notice"
        session.commit()
        assert mp.latest_estimate(session, opp.id) is None


def test_workspace_queues_a_search_and_the_task_stores_a_run(upgraded_engine, fake_claude):
    from test_web_ui import _make_user
    from web_client import CsrfTestClient

    from govcon.tasks.testing import drain
    from govcon.web.app import create_app

    with Session(upgraded_engine) as session:
        opp = _opportunity(session)
        _, token = _make_user(session, f"mp-{uuid4().hex}@example.test", "reviewer")
        session.commit()
    with CsrfTestClient(create_app(), follow_redirects=False) as client:
        client.cookies.set("govcon_session", token)
        response = client.post(f"/workspace/{opp.id}/market-prices", data={})
        assert "notice=" in response.headers["location"]
        drain(_settings(), opportunity_id=opp.id)
        page = client.get(f"/workspace/{opp.id}?tab=sourcing")
    with Session(upgraded_engine) as session:
        task = session.scalar(select(Task).where(Task.opportunity_id == opp.id, Task.task_type == mp.MARKET_PRICE_TASK))
        run = session.scalar(select(MarketPriceRun).where(MarketPriceRun.opportunity_id == opp.id))
        assert task.status == "succeeded" and run.task_id == task.id and run.status == "completed"
        assert task.result["summary"].startswith("5 web price(s) found")
    assert "Web market prices" in page.text and "Alpha nitrile exam gloves" in page.text
    assert "gsaadvantage.gov: government site" in page.text


def test_preparation_searches_after_compliance_and_before_research():
    from govcon.workflow.preparation import STEPS

    assert STEPS.index("compliance") < STEPS.index("market_prices") < STEPS.index("research") < STEPS.index("decision")


# ── Operate: the orchestration dashboard shows the step ──────────────────────

def test_operate_dashboard_shows_the_market_price_step(upgraded_engine, fake_claude, monkeypatch):
    from test_web_ui import _make_user
    from web_client import CsrfTestClient

    from govcon.config import get_settings
    from govcon.operating.board import architecture_board
    from govcon.operating.integrations import MARKET_PRICE_CARD, market_price_card
    from govcon.operating.trace import opportunity_trace
    from govcon.web.app import create_app

    settings = _settings()
    with Session(upgraded_engine) as session:
        opp = _opportunity(session)
        trace = opportunity_trace(session, opp)
        ids = [stage["id"] for stage in trace]
        assert ids.index("compliance") + 1 == ids.index("market_prices")
        assert trace[ids.index("market_prices")]["status"] == "not run"
        assert market_price_card(session, _settings(anthropic_api_key=None))["status"] == "Not configured"
        assert market_price_card(session, _settings(market_price_research_enabled=False))["status"] == "Disabled"
        inp = mp.build_research_input(session, opp.id, settings)
        session.commit()
    outcome = mp.run_research(inp, settings, engine=upgraded_engine)
    with Session(upgraded_engine) as session:
        mp.store_run(session, outcome, settings)
        session.commit()
        stage = next(s for s in opportunity_trace(session, session.get(Opportunity, opp.id)) if s["id"] == "market_prices")
        assert stage["status"] == "completed" and stage["tag"] == "good" and "estimated cost $8.50" in stage["detail"]
        card = market_price_card(session, settings)
        assert card["name"] == MARKET_PRICE_CARD and card["status"] == "Healthy"
        assert any("from 5 listing(s)" in row["value"] for row in card["rows"])

        board = architecture_board(session, settings, "market_prices")
        agents = next(band for band in board["rows"] if band["label"].startswith("03"))
        order = [node["id"] for node in agents["nodes"]]
        assert order.index("compliance") + 1 == order.index("market_prices") == order.index("bid_decision") - 1
        node = next(node for node in agents["nodes"] if node["id"] == "market_prices")
        assert (node["status"], node["tag"]) == ("completed", "good")
        assert board["selected"]["title"] == "Market prices" and board["selected"]["run"]["status"] == "completed"
        models = next(band for band in board["rows"] if band["label"].startswith("04"))
        assert next(n for n in models["nodes"] if n["id"] == "claude_web")["status"] == "Healthy"
        _, token = _make_user(session, f"mp-ops-{uuid4().hex}@example.test", "owner")
        session.commit()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-0123456789")
    get_settings.cache_clear()
    with CsrfTestClient(create_app(), follow_redirects=False) as client:
        client.cookies.set("govcon_session", token)
        page = client.get("/operate/architecture?node=market_prices").text
        assert "Market prices" in page and "Claude web search" in page and "Government sites are excluded" in page
        assert "Claude web search" in client.get("/operate/integrations").text
        assert "Market prices" in client.get(f"/operate/how?opp={opp.id}").text
    get_settings.cache_clear()
