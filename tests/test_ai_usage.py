"""Provider usage is recorded from the response, never estimated."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.gateway import AIGatewayBlocked
from govcon.ai.price_catalog import SEED_PRICES
from govcon.config import Settings
from govcon.models import AIModelPrice, AIProviderCall
from govcon.security.classification import DataClassification


def _settings() -> Settings:
    return Settings(_env_file=None)


def _rows(db) -> list[AIProviderCall]:
    engine = db.get_bind()
    with Session(engine) as other:
        return list(other.scalars(select(AIProviderCall).order_by(AIProviderCall.id)).all())


def test_seeded_prices_match_the_cited_catalog(db) -> None:
    stored = {
        (row.provider, row.model): row
        for row in db.scalars(select(AIModelPrice)).all()
    }
    assert len(stored) == len(SEED_PRICES)
    for item in SEED_PRICES:
        row = stored[(item["provider"], item["model"])]
        assert row.input_usd_per_million == item["input_usd_per_million"]
        assert row.output_usd_per_million == item["output_usd_per_million"]
        assert row.cached_usd_per_million == item["cached_usd_per_million"]
        assert row.source_url == item["source_url"]
        assert row.effective_as_of == item["effective_as_of"]
        assert str(row.source_url).startswith("https://")


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
        Provider(), db, opportunity_id=None, settings=_settings().model_copy(update={"ai_max_provider_retries": 1}),
        system_prompt="s", user_prompt="u", classification=DataClassification.PUBLIC, purpose="retry",
    )
    added = _rows(db)[before:]
    assert [row.status for row in added] == ["failed", "succeeded"]
    assert added[0].input_tokens is None
    assert added[1].input_tokens == 8


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


def test_local_call_is_zero_cost(db) -> None:
    from govcon.ai.usage_log import record_call, usage_page

    record_call(db, provider="local", purpose="embedding", status="local", model="all-MiniLM-L6-v2")
    row = _rows(db)[-1]
    assert row.status == "local" and row.cost_usd == 0 and row.input_tokens is None
    page = usage_page(Session(db.get_bind()))
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
    assert "SAMPLE DATA" not in body
    ops = client.get("/ops")
    assert ops.status_code == 200
    assert "AI usage" in ops.text
    assert "Full view" in ops.text
