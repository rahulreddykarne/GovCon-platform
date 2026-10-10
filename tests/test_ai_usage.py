"""Provider usage is recorded from the response, never estimated."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import httpx2
import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from govcon.ai.gateway import AIGatewayBlocked
from govcon.ai.price_catalog import SEED_PRICES
from govcon.config import Settings
from govcon.models import AIModelPrice, AIProviderCall
from govcon.security.classification import DataClassification


def _settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


def _rows(db) -> list[AIProviderCall]:
    engine = db.get_bind()
    with Session(engine) as other:
        return list(other.scalars(select(AIProviderCall).order_by(AIProviderCall.id)).all())


def test_seeded_prices_match_the_cited_catalog(db) -> None:
    stored = {
        (row.provider, row.model): row
        for row in db.scalars(select(AIModelPrice)).all()
    }
    for item in SEED_PRICES:
        row = stored[(item["provider"], item["model"])]
        assert row.input_usd_per_million == item["input_usd_per_million"]
        assert row.output_usd_per_million == item["output_usd_per_million"]
        assert row.cached_usd_per_million == item["cached_usd_per_million"]
        assert row.web_search_usd_per_thousand == item["web_search_usd_per_thousand"]
        assert row.source_url == item["source_url"]
        assert row.effective_as_of == item["effective_as_of"]
        assert str(row.source_url).startswith("https://")
    anthropic = [item for item in SEED_PRICES if item["provider"] == "anthropic"]
    assert anthropic and all(item["web_search_usd_per_thousand"] == Decimal(10) for item in anthropic)


def test_a_capped_completion_is_recorded_as_truncated_not_succeeded(db, monkeypatch) -> None:
    from govcon.ai.budget import complete_with_budget

    class Provider:
        name = "deepseek"

        def complete(self, **kwargs):
            return SimpleNamespace(
                content='{"summary":',
                model="deepseek-flash",
                provider="deepseek",
                usage={"prompt_tokens": 40, "completion_tokens": 8192},
                latency_ms=20,
                finish_reason="length",
            )

    complete_with_budget(
        Provider(), db, opportunity_id=None, settings=_settings(),
        system_prompt="s", user_prompt="u", classification=DataClassification.PUBLIC, purpose="solicitation_analysis",
    )
    row = _rows(db)[-1]
    assert row.status == "truncated"
    assert row.finish_reason == "length"
    assert row.output_tokens == 8192


def test_discarded_json_is_recorded_as_output_rejected(db, monkeypatch) -> None:
    from pathlib import Path

    from govcon.ai.structured import (
        StructuredCallError,
        execute_prepared_call,
        prepare_structured_call,
    )
    from govcon.prompting.registry import sync_prompts

    sync_prompts(db, Path(__file__).parent.parent / "src" / "govcon" / "prompts", settings=_settings())
    db.commit()

    class Provider:
        name = "deepseek"

        def complete(self, **kwargs):
            return SimpleNamespace(
                content="not json",
                model="deepseek-flash",
                provider="deepseek",
                usage={"prompt_tokens": 10, "completion_tokens": 4},
                latency_ms=3,
                finish_reason="stop",
            )

    monkeypatch.setattr("govcon.ai.structured.get_provider", lambda *a, **k: Provider())
    before = len(_rows(db))
    prepared = prepare_structured_call(
        db, opportunity_id=None, prompt_name="solicitation_analysis",
        analysis_type="solicitation_summary",
        variables={"OPPORTUNITY_JSON": {}, "SOURCE_PACKAGE_JSON": "synthetic public source"},
        context_manifest={}, settings=_settings(), classification=DataClassification.PUBLIC,
    )
    with pytest.raises(StructuredCallError, match="invalid_output"):
        execute_prepared_call(prepared, settings=_settings(), session=db)
    rows = _rows(db)[before:]
    assert rows and all(row.status == "output_rejected" for row in rows)
    assert all(row.finish_reason == "stop" for row in rows)


def test_prompt_classification_gate_writes_a_blocked_ledger_row(db) -> None:
    from pathlib import Path

    from govcon.ai.structured import StructuredCallError, prepare_structured_call
    from govcon.prompting.registry import sync_prompts

    sync_prompts(db, Path(__file__).parent.parent / "src" / "govcon" / "prompts", settings=_settings())
    db.commit()
    before = len(_rows(db))
    with pytest.raises(StructuredCallError, match="does not allow UNKNOWN"):
        prepare_structured_call(
            db, opportunity_id=None, prompt_name="requirement_extraction_a",
            analysis_type="compliance_review",
            variables={"OPPORTUNITY_JSON": {}, "DOCUMENT_INVENTORY_JSON": [], "SOURCE_CHUNKS": "x",
                       "AMENDMENT_JSON": {}},
            context_manifest={}, settings=_settings(), classification=DataClassification.UNKNOWN,
        )
    added = _rows(db)[before:]
    assert len(added) == 1
    assert added[0].status == "blocked"
    assert added[0].purpose == "requirement_extraction_a"
    assert "UNKNOWN" in (added[0].finish_reason or "")


def test_usage_is_taken_from_the_provider_response(db, monkeypatch) -> None:
    from govcon.ai.budget import complete_with_budget

    class Provider:
        name = "deepseek"

        def complete(self, **kwargs):
            return SimpleNamespace(
                content="{}",
                model="deepseek-flash",
                provider="deepseek",
                usage={
                    "prompt_tokens": 150,
                    "completion_tokens": 20,
                    "prompt_cache_hit_tokens": 100,
                    "prompt_cache_miss_tokens": 50,
                },
                latency_ms=15,
            )

    monkeypatch.setattr("govcon.ai.budget.time.sleep", lambda _: None)
    complete_with_budget(
        Provider(), db, opportunity_id=None, settings=_settings(),
        system_prompt="s", user_prompt="u", classification=DataClassification.PUBLIC, purpose="solicitation_analysis",
    )
    row = _rows(db)[-1]
    assert row.status == "succeeded"
    assert row.input_tokens == 50
    assert row.output_tokens == 20
    assert row.cached_tokens == 100
    assert row.latency_ms == 15
    assert row.web_search_requests is None
    assert row.cost_usd == (Decimal(50) * Decimal("0.15") + Decimal(20) * Decimal("0.6") + Decimal(100) * Decimal("0.003")) / Decimal(1_000_000)


def test_missing_usage_stays_null(db) -> None:
    from govcon.ai.budget import complete_with_budget

    class Provider:
        name = "deepseek"

        def complete(self, **kwargs):
            return SimpleNamespace(content="{}", model="deepseek-flash", provider="deepseek", usage={}, latency_ms=4)

    complete_with_budget(
        Provider(), db, opportunity_id=None, settings=_settings(),
        system_prompt="s", user_prompt="u", classification=DataClassification.PUBLIC, purpose="solicitation_analysis",
    )
    row = _rows(db)[-1]
    assert row.input_tokens is None
    assert row.output_tokens is None
    assert row.cached_tokens is None
    assert row.cost_usd is None


def test_unpriced_model_has_no_cost(db) -> None:
    from govcon.ai.budget import complete_with_budget
    from govcon.ai.usage_log import usage_page

    class Provider:
        name = "openai"

        def complete(self, **kwargs):
            return SimpleNamespace(
                content="{}", model="gpt-5", provider="openai",
                usage={"prompt_tokens": 10, "completion_tokens": 2}, latency_ms=3,
            )

    complete_with_budget(
        Provider(), db, opportunity_id=None, settings=_settings(),
        system_prompt="s", user_prompt="u", classification=DataClassification.PUBLIC, purpose="draft",
    )
    row = _rows(db)[-1]
    assert row.input_tokens == 10 and row.output_tokens == 2
    assert row.cost_usd is None
    page = usage_page(Session(db.get_bind()))
    assert page["recent"][0]["cost"] == "price not set"


def test_web_search_requests_are_counted_and_priced(db) -> None:
    from govcon.ai.budget import complete_with_budget
    from govcon.ai.usage_log import usage_page

    class Provider:
        name = "anthropic"

        def complete(self, **kwargs):
            return SimpleNamespace(
                content="{}", model="claude-sonnet-5-5", provider="anthropic",
                usage={"input_tokens": 1000, "output_tokens": 100, "web_search_requests": 3, "web_fetch_requests": 1},
                latency_ms=9,
            )

    complete_with_budget(
        Provider(), db, opportunity_id=None, settings=_settings(),
        system_prompt="s", user_prompt="u", classification=DataClassification.PUBLIC, purpose="market_price_research",
        server_tools=[{"type": "web_search_20260209"}],
    )
    row = _rows(db)[-1]
    assert (row.web_search_requests, row.web_fetch_requests) == (3, 1)
    tokens = (Decimal(1000) * Decimal(2) + Decimal(100) * Decimal(10)) / Decimal(1_000_000)
    assert row.cost_usd == tokens + Decimal(3) * Decimal(10) / Decimal(1000)
    recent = usage_page(Session(db.get_bind()))["recent"][0]
    assert recent["server_tools"] == "3 search · 1 fetch"


def test_unset_web_search_fee_leaves_cost_unset(upgraded_engine) -> None:
    from govcon.ai.usage_log import record_call, usage_page

    model = f"test-{uuid4().hex[:8]}"
    with Session(upgraded_engine) as db, db.begin():
        db.add(AIModelPrice(provider="anthropic", model=model, input_usd_per_million=Decimal(1),
                            output_usd_per_million=Decimal(1), source_url="https://example.test/prices",
                            effective_as_of=date(2026, 10, 10)))
    try:
        call_id = record_call(None, engine=upgraded_engine, provider="anthropic", model=model,
                              purpose="market_price_research", status="succeeded",
                              usage={"input_tokens": 10, "output_tokens": 1, "web_search_requests": 2})
        with Session(upgraded_engine) as db:
            row = db.get(AIProviderCall, call_id)
            assert row is not None and row.price_id is not None and row.cost_usd is None
            recent = next(item for item in usage_page(db)["recent"] if item["model"] == model)
            assert recent["cost"] == "price not set (web search fee)"
    finally:
        with Session(upgraded_engine) as db, db.begin():
            db.execute(delete(AIProviderCall).where(AIProviderCall.model == model))
            db.execute(delete(AIModelPrice).where(AIModelPrice.model == model))


def test_retries_are_separate_rows(db, monkeypatch) -> None:
    from govcon.ai.budget import complete_with_budget
    from govcon.ai.providers.base import ProviderAPIError

    calls = []

    class Provider:
        name = "deepseek"

        def complete(self, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise ProviderAPIError(self.name, 503)
            return SimpleNamespace(
                content="{}", model="deepseek-flash", provider="deepseek",
                usage={"prompt_tokens": 8, "completion_tokens": 1}, latency_ms=2,
            )

    monkeypatch.setattr("govcon.ai.budget.time.sleep", lambda _: None)
    before = len(_rows(db))
    complete_with_budget(
        Provider(), db, opportunity_id=None, settings=_settings(ai_max_provider_retries=1),
        system_prompt="s", user_prompt="u", classification=DataClassification.PUBLIC, purpose="retry",
    )
    added = _rows(db)[before:]
    assert [row.status for row in added] == ["failed", "succeeded"]
    assert added[0].input_tokens is None
    assert added[1].input_tokens == 8


def test_every_retry_is_gated_with_the_current_classification(db, monkeypatch) -> None:
    """A file classified PROPRIETARY between attempts blocks the retry before it is sent."""
    from govcon.ai.budget import complete_with_budget
    from govcon.ai.providers.base import ProviderAPIError

    reads = []

    def classification_now(session, opportunity_id, declared):
        reads.append(opportunity_id)
        return DataClassification.PUBLIC if len(reads) < 3 else DataClassification.PROPRIETARY

    monkeypatch.setattr("govcon.security.classification.opportunity_classification", classification_now)
    monkeypatch.setattr("govcon.ai.budget.time.sleep", lambda _: None)
    sent = []

    class Provider:
        name = "deepseek"

        def complete(self, **kwargs):
            sent.append(kwargs["classification"])
            raise ProviderAPIError(self.name, 503)

    before = len(_rows(db))
    with pytest.raises(AIGatewayBlocked):
        complete_with_budget(
            Provider(), db, opportunity_id=987654321, settings=_settings(ai_max_provider_retries=2),
            system_prompt="s", user_prompt="u", classification=DataClassification.PUBLIC, purpose="retry_gate",
        )
    # Initial check, first attempt, then the retry that is refused.
    assert len(reads) == 3
    assert sent == [DataClassification.PUBLIC]
    added = _rows(db)[before:]
    assert [row.status for row in added] == ["failed", "blocked"]
    assert added[1].input_tokens == 0 and added[1].cost_usd == 0


def test_blocked_call_is_not_transmitted(db, monkeypatch) -> None:
    from govcon.ai.budget import complete_with_budget

    def refuse(*args, **kwargs):
        raise AssertionError("provider client was opened")

    monkeypatch.setattr("govcon.http.build_client", refuse)

    class Provider:
        name = "deepseek"

        def complete(self, **kwargs):
            raise AssertionError("complete ran")

    before = len(_rows(db))
    with pytest.raises(AIGatewayBlocked):
        complete_with_budget(
            Provider(), db, opportunity_id=None, settings=_settings(),
            system_prompt="secret", user_prompt="secret", classification=DataClassification.UNKNOWN,
            purpose="solicitation_analysis",
        )
    added = _rows(db)[before:]
    assert len(added) == 1
    assert added[0].status == "blocked"
    assert added[0].input_tokens == 0
    assert added[0].output_tokens == 0
    assert added[0].cost_usd == 0


def test_proprietary_stays_blocked_by_default() -> None:
    settings = _settings()
    assert settings.ai_external_allowed_for_proprietary is False


def _anthropic_message(content, *, stop_reason="end_turn", searches=1):
    return {"id": "msg", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 10, "server_tool_use": {"web_search_requests": searches}}}


def _paused_search():
    return [{"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "gloves"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
             "content": [{"type": "web_search_result", "url": "https://alpha.example.com", "title": "Alpha",
                          "encrypted_content": "abc", "page_age": None}]}]


def _anthropic(handler):
    from govcon.ai.providers.anthropic import AnthropicProvider

    settings = _settings(anthropic_api_key="sk-ant-test-key-0123456789", anthropic_model="claude-opus-5-5")
    return AnthropicProvider("sk-ant-test-key-0123456789", model="claude-opus-5-5", settings=settings,
                             http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), max_retries=0)


def test_anthropic_continuations_are_each_gated(monkeypatch) -> None:
    replies = [_anthropic_message(_paused_search(), stop_reason="pause_turn"),
               _anthropic_message([{"type": "text", "text": "{}"}], searches=2)]
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=replies[len(sent) - 1])

    checks = []
    real = __import__("govcon.ai.gateway", fromlist=["authorize_external_call"]).authorize_external_call

    def counting(**kwargs):
        checks.append(kwargs["classification"])
        return real(**kwargs)

    monkeypatch.setattr("govcon.ai.providers.anthropic.authorize_external_call", counting)
    result = _anthropic(handler).complete(system_prompt="s", user_prompt="u", classification=DataClassification.PUBLIC,
                                          purpose="market_price_research", server_tools=[{"type": "web_search_20260209",
                                                                                          "name": "web_search"}])
    assert len(sent) == 2 and len(checks) == 2
    assert result.usage["web_search_requests"] == 3


def test_anthropic_continuation_refused_by_the_gate_is_not_sent(monkeypatch) -> None:
    sent = []

    def handler(request):
        sent.append(1)
        return httpx2.Response(200, json=_anthropic_message(_paused_search(), stop_reason="pause_turn"))

    checks = []

    def second_refused(**kwargs):
        checks.append(1)
        if len(checks) > 1:
            raise AIGatewayBlocked(DataClassification.PROPRIETARY)

    monkeypatch.setattr("govcon.ai.providers.anthropic.authorize_external_call", second_refused)
    with pytest.raises(AIGatewayBlocked):
        _anthropic(handler).complete(system_prompt="s", user_prompt="u", classification=DataClassification.PUBLIC,
                                     purpose="market_price_research",
                                     server_tools=[{"type": "web_search_20260209", "name": "web_search"}])
    assert len(sent) == 1


def test_market_price_research_records_usage_with_server_tool_counts(upgraded_engine, monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta

    from govcon.ai.providers.base import CompletionResult
    from govcon.models import Opportunity
    from govcon.sourcing import market_prices as mp

    class FakeClaude:
        name = "anthropic"

        def complete(self, **kwargs):
            return CompletionResult(content=json.dumps({"product_searched": "x", "listings": [], "notes": None}),
                                    model="claude-opus-5-5", provider="anthropic",
                                    usage={"input_tokens": 1200, "output_tokens": 300, "web_search_requests": 2},
                                    latency_ms=40, finish_reason="end_turn")

    monkeypatch.setattr("govcon.ai.providers.get_provider", lambda settings, provider_name=None: FakeClaude())
    settings = _settings(anthropic_api_key="sk-ant-test-key-0123456789", anthropic_model="claude-opus-5-5",
                         ai_max_provider_retries=0)
    with Session(upgraded_engine) as session:
        opp = Opportunity(source="sam", source_id=f"usage-{uuid4().hex}", title="Gloves, nitrile", status="open",
                          psc_code="6515", quantity=Decimal(10), unit="BX",
                          response_deadline=datetime.now(UTC) + timedelta(days=20), raw={}, links={})
        session.add(opp)
        session.commit()
        opp_id = opp.id
        inp = mp.build_research_input(session, opp_id, settings)
        session.commit()
    mp.run_research(inp, settings, engine=upgraded_engine)
    with Session(upgraded_engine) as session:
        row = session.scalars(select(AIProviderCall).where(AIProviderCall.opportunity_id == opp_id)).one()
        assert row.purpose == "market_price_research" and row.status == "succeeded"
        assert (row.input_tokens, row.output_tokens, row.web_search_requests) == (1200, 300, 2)
        expected = (Decimal(1200) * Decimal(4) + Decimal(300) * Decimal(20)) / Decimal(1_000_000) + Decimal("0.02")
        assert row.cost_usd == expected


def test_jev_answer_and_refusal_are_recorded(upgraded_engine, monkeypatch) -> None:
    from govcon.decision.provider import DecisionProviderUnavailable
    from govcon.decision.providers.jev import JevDecisionProvider

    monkeypatch.setattr("govcon.http.build_client", lambda *a, **k: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"answers": {"q": {"value": 1}}, "model": "jev-latest",
                                                  "usage": {"input_tokens": 40, "output_tokens": 5}}))))
    settings = _settings(jev_enabled=True, jev_api_key="jev-test-key")
    opp_marker = 900_000_000 + int(uuid4().int % 1_000_000)
    with Session(upgraded_engine) as session:
        provider = JevDecisionProvider(api_key="jev-test-key", settings=settings, session=session)
        provider.decide(bundle_name="bid_decision", bundle_version="v1",
                        state={"data_classification": "PUBLIC", "budget_opportunity_id": opp_marker})
        with pytest.raises(DecisionProviderUnavailable):
            provider.decide(bundle_name="bid_decision", bundle_version="v1",
                            state={"data_classification": "UNKNOWN", "budget_opportunity_id": opp_marker})
        session.rollback()
    with Session(upgraded_engine) as session:
        rows = list(session.scalars(select(AIProviderCall).where(AIProviderCall.opportunity_id == opp_marker)
                                    .order_by(AIProviderCall.id)).all())
    assert [row.status for row in rows] == ["succeeded", "blocked"]
    assert (rows[0].input_tokens, rows[0].output_tokens) == (40, 5)
    assert rows[1].input_tokens == 0 and rows[1].cost_usd == 0


def test_local_call_is_zero_cost(db) -> None:
    from govcon.ai.usage_log import record_call, usage_page

    record_call(db, provider="local", purpose="embedding", status="local", model="all-MiniLM-L6-v2")
    row = _rows(db)[-1]
    assert row.status == "local" and row.cost_usd == 0 and row.input_tokens is None
    hidden = usage_page(Session(db.get_bind()))
    assert all(item["status"] != "local" for item in hidden["recent"])
    page = usage_page(Session(db.get_bind()), include_local=True)
    assert page["recent"][0]["cost"] == "$0"
    assert page["recent"][0]["input"] == "—"
    local = next(row for row in page["periods"][0]["by_model"] if row["label"].startswith("local ·"))
    assert local["input"] == "—"
    assert local["output"] == "—"
    assert local["cost"] == "$0"
    assert page["empty"] is False


def test_usage_page_renders_prices_and_empty_copy(db, client) -> None:
    from test_web_ui import _make_user

    _, token = _make_user(db, f"usage-{uuid4().hex[:8]}@example.test", "owner")
    db.commit()
    client.cookies.set("govcon_session", token)
    page = client.get("/operate/usage")
    assert page.status_code == 200
    body = page.text
    assert "AI usage and cost" in body
    assert "deepseek-flash" in body
    assert "https://api-docs.deepseek.com/quick_start/pricing" in body
    assert "Web search / 1k" in body
    assert "SAMPLE DATA" not in body
    ops = client.get("/ops")
    assert ops.status_code == 200
    assert "AI usage" in ops.text
    assert "Full view" in ops.text


def test_price_edit_needs_manage_users_and_saves_the_search_fee(db, client) -> None:
    from test_web_ui import _make_user

    model = f"edit-{uuid4().hex[:8]}"
    form = {"provider": "anthropic", "model": model, "input_usd_per_million": "1", "output_usd_per_million": "2",
            "web_search_usd_per_thousand": "10", "source_url": "https://example.test/prices",
            "effective_as_of": "2026-10-10"}
    _, viewer = _make_user(db, f"viewer-{uuid4().hex[:8]}@example.test", "read_only")
    _, owner = _make_user(db, f"owner-{uuid4().hex[:8]}@example.test", "owner")
    db.commit()
    try:
        client.cookies.set("govcon_session", viewer)
        denied = client.post("/operate/usage/prices", data=form)
        assert denied.status_code == 303 and "error=" in denied.headers["location"]
        assert db.scalar(select(AIModelPrice).where(AIModelPrice.model == model)) is None
        client.cookies.set("govcon_session", owner)
        saved = client.post("/operate/usage/prices", data=form)
        assert saved.status_code == 303 and "notice=" in saved.headers["location"]
        db.expire_all()
        row = db.scalar(select(AIModelPrice).where(AIModelPrice.model == model))
        assert row is not None and row.web_search_usd_per_thousand == Decimal(10)
        bad = client.post("/operate/usage/prices", data={**form, "web_search_usd_per_thousand": "NaN"})
        assert "error=" in bad.headers["location"]
    finally:
        db.execute(delete(AIModelPrice).where(AIModelPrice.model == model))
        db.commit()
