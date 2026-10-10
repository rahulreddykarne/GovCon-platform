"""Laptop rerun bugs 3–10: usage limits, prompt eval, mandatory/PT/missing-info, risk, awards, 429."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.usage_log import USAGE_TABLE_LIMIT, record_call, usage_page
from govcon.compliance.metrics import coverage_counts, is_mandatory, normalize_mandatory
from govcon.compliance.view import compliance_view
from govcon.decision.missing import normalize_missing_information
from govcon.display_time import format_pt
from govcon.enrich.attachments import download_attachments
from govcon.enrich.embeddings import run_embedding_job
from govcon.enrich.safe_fetch import FetchRateLimited, RATE_LIMITED_MESSAGE, safe_fetch
from govcon.matching.pricing import agencies_match, comparable_relevance, recent_award_comps
from govcon.models import (
    AIProviderCall,
    Award,
    Opportunity,
    PromptRegistryEntry,
    Requirement,
    ReviewSession,
    Task,
)
from govcon.prompting.registry import activation_next_step


def _opp(db, **values) -> Opportunity:
    data = {
        "source": "sam",
        "source_id": f"laptop-{uuid4().hex}",
        "title": f"Laptop bug {uuid4().hex[:8]}",
        "status": "open",
        "agency_path": "Defense Logistics Agency / SPE7L",
        "psc_code": "9988",
        "naics_code": "999999",
        "raw": {},
        "links": {},
    }
    data.update(values)
    row = Opportunity(**data)
    db.add(row)
    db.flush()
    return row


def _award(db, **overrides) -> Award:
    award_id = overrides.get("award_id", f"laptop-award-{uuid4().hex}")
    values = {
        "source": "usaspending",
        "award_id": award_id,
        "psc_code": "9988",
        "naics_code": "999999",
        "awarding_agency": "Defense Logistics Agency",
        "action_date": date(2025, 6, 1),
        "total_obligation": Decimal(1000),
        "raw": {"generated_internal_id": award_id},
    }
    values.update(overrides)
    row = Award(**values)
    db.add(row)
    db.flush()
    return row


class _Embed:
    DIM = 384

    def embed(self, text: str) -> list[float]:
        return [0.01] * self.DIM


def test_usage_page_hides_local_embeddings_and_paginates(db) -> None:
    before = {row.id for row in db.scalars(select(AIProviderCall)).all()}
    for index in range(USAGE_TABLE_LIMIT + 4):
        record_call(
            db,
            provider="deepseek",
            purpose="solicitation_analysis",
            status="succeeded",
            model=f"cap-{index}",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
        )
    record_call(db, provider="local", purpose="embedding", status="local", model="all-MiniLM-L6-v2")
    page = usage_page(Session(db.get_bind()))
    assert page["include_local"] is False
    assert all(item["status"] != "local" for item in page["recent"])
    assert len(page["recent"]) == USAGE_TABLE_LIMIT
    assert page["recent_total"] >= USAGE_TABLE_LIMIT + 4
    more = usage_page(Session(db.get_bind()), show_more=True)
    assert len(more["recent"]) >= USAGE_TABLE_LIMIT + 4
    local = usage_page(Session(db.get_bind()), include_local=True)
    assert any(item["status"] == "local" for item in local["recent"])
    leftover = [row for row in _usage_rows(db) if row.id not in before]
    assert leftover


def _usage_rows(db) -> list[AIProviderCall]:
    return list(Session(db.get_bind()).scalars(select(AIProviderCall)).all())


def test_embedding_job_writes_one_local_row_per_batch_without_opportunity(db) -> None:
    marker = f"embed-{uuid4().hex[:8]}"

    class Named(_Embed):
        model_name = marker

    for _ in range(5):
        _opp(db, title=f"Embed {uuid4().hex[:6]}")
    result = run_embedding_job(db, Named(), batch_size=2)
    expected = result["embedded"] // 2 + (1 if result["embedded"] % 2 else 0)
    rows = list(Session(db.get_bind()).scalars(select(AIProviderCall).where(AIProviderCall.model == marker)).all())
    assert result["embedded"] >= 5
    assert len(rows) == expected
    assert all(row.status == "local" and row.opportunity_id is None for row in rows)


def test_usage_timestamps_are_labeled_pt(db) -> None:
    record_call(db, provider="deepseek", purpose="draft", status="succeeded", model="pt-check",
                usage={"prompt_tokens": 1, "completion_tokens": 1})
    page = usage_page(Session(db.get_bind()))
    when = next(item["when"] for item in page["recent"] if item["model"] == "pt-check")
    assert when.endswith(" PT")
    assert format_pt(datetime(2026, 10, 10, 21, 5, tzinfo=UTC)) == "2026-10-10 14:05 PT"
    assert format_pt(datetime(2026, 10, 10, 21, 5, 7, tzinfo=UTC), seconds=True) == "2026-10-10 14:05:07 PT"


def test_inactive_prompt_names_eval_live_not_sync_alone(db) -> None:
    name = f"prompt_{uuid4().hex[:8]}"
    db.add(PromptRegistryEntry(
        prompt_name=name,
        prompt_version="v3",
        task_type="analysis",
        source_path=f"prompts/{name}.v3.md",
        prompt_hash="a" * 64,
        active=False,
    ))
    db.flush()
    step = activation_next_step(db, name)
    assert f"govcon prompts eval {name}@v3 --live" in step
    assert f"govcon prompts activate {name}@v3" in step
    assert "govcon prompts sync" not in step
    assert "eval" in activation_next_step(None, "solicitation_analysis")
    assert "sync" in activation_next_step(None, "solicitation_analysis")


def test_null_mandatory_counts_as_mandatory(db) -> None:
    opp = _opp(db)
    row = Requirement(
        opportunity_id=opp.id,
        requirement_text="Offeror shall acknowledge amendments",
        mandatory=None,
        severity="high",
        status="needs_review",
    )
    db.add(row)
    db.flush()
    assert is_mandatory(None) is True
    assert normalize_mandatory(row) is True
    assert row.mandatory is True
    counted = coverage_counts([row], [])
    assert counted["mandatory_total"] == 1
    assert counted["mandatory_needs_review"] == 1
    view = compliance_view(db, opp.id, facts={})
    assert view.mandatory["total"] == 1
    assert view.mandatory["Needs review"] == 1


def test_missing_information_dedupes_and_drops_part_notes() -> None:
    items = [
        {"field": "amendment_status", "reason": "not stated on this part"},
        {"field": "amendment_status", "reason": "still not stated"},
        {"field": "source_package", "reason": "remaining pages (1-13 and 18-20)"},
        "part 2 of 4 remaining pages (14-17)",
        {"field": "delivery", "reason": "FOB not stated"},
    ]
    assert normalize_missing_information(items) == [
        "Amendment status",
        "Unread source pages",
        "Delivery terms",
    ]


def test_usda_forest_awards_are_not_dla_comparables(db) -> None:
    dla = _award(db, award_id=f"dla-{uuid4().hex[:8]}", awarding_agency="Defense Logistics Agency")
    usda = _award(
        db,
        award_id=f"usda-{uuid4().hex[:8]}",
        awarding_agency="USDA Forest Service (12C2)",
    )
    assert agencies_match(dla.awarding_agency, "DLA / SPE7L")
    assert not agencies_match(usda.awarding_agency, "DLA / SPE7L")
    comps = recent_award_comps(
        db, nsn=None, psc_code="9988", naics_code="999999",
        awarding_agency="Defense Logistics Agency / SPE7L",
    )
    ids = {point.award_id for point in comps}
    assert dla.award_id in ids
    assert usda.award_id not in ids
    from govcon.matching.pricing import PricePoint

    usda_point = PricePoint(
        award_id=usda.award_id, piid=None, vendor_name=None, vendor_uei=None,
        action_date=usda.action_date, amount=usda.total_obligation, unit_price=None,
        quantity=None, nsn=usda.nsn, psc_code=usda.psc_code, naics_code=usda.naics_code,
        awarding_agency=usda.awarding_agency, description=None,
    )
    assert comparable_relevance(
        usda_point, nsn=None, psc_code="9988", naics_code="999999",
        awarding_agency="Defense Logistics Agency",
    ) == "low"
    included = recent_award_comps(
        db, nsn=None, psc_code="9988", awarding_agency="Defense Logistics Agency",
        include_low_relevance=True, limit=10,
    )
    assert usda.award_id in {point.award_id for point in included}


def test_safe_fetch_honors_retry_after_then_raises_rate_limited() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "0"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(FetchRateLimited, match=RATE_LIMITED_MESSAGE) as caught:
        safe_fetch(
            client,
            "https://api.sam.gov/prod/opportunities/v2/noticedesc",
            max_bytes=1000,
            resolver=lambda _host: ["93.184.216.34"],
            attempts=2,
        )
    assert calls["n"] == 2
    assert caught.value.retry_after == 0.0
    assert str(caught.value) == "rate limited, retry scheduled"


def test_rate_limited_download_records_gap_and_queues_retry(db, tmp_path) -> None:
    from govcon.config import Settings

    opp = _opp(
        db,
        links={"attachments": ["https://api.sam.gov/prod/opportunities/v3/resources/files/a1/download"]},
    )
    settings = Settings(data_dir=tmp_path, sam_api_key="sam-test-key-123456")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    files = download_attachments(
        db, opp, settings=settings, client=client,
        resolver=lambda _host: ["93.184.216.34"],
    )
    assert len(files) == 1
    assert files[0].extraction_status == "download_failed"
    assert files[0].extraction_error == "rate limited, retry scheduled"
    task = db.scalar(
        select(Task).where(
            Task.opportunity_id == opp.id,
            Task.task_type == "opportunity_preparation",
            Task.payload["retry_reason"].astext == "rate_limited",
        )
    )
    assert task is not None
    assert task.next_attempt_at is not None
    assert task.next_attempt_at > datetime.now(UTC) - timedelta(seconds=1)


def test_decision_header_uses_scorecard_compliance_not_inverted_risk(client, db) -> None:
    from test_web_ui import _make_user
    from govcon.decision.engine import run_preliminary_decision_package

    user, token = _make_user(db, f"risk-{uuid4().hex[:8]}@example.test", "owner")
    opp = _opp(db)
    run_preliminary_decision_package(db, opportunity_id=opp.id)
    db.commit()
    page = client.get(f"/workspace/{opp.id}?tab=ai_decision", cookies={"govcon_session": token}).text
    assert "Compliance Risk" not in page
    assert "Start pursuit (requires approval)" in page


def test_approve_to_bid_hidden_until_pursuit_exists(client, db) -> None:
    from test_web_ui import _make_user

    user, token = _make_user(db, f"pursuit-{uuid4().hex[:8]}@example.test", "owner")
    opp = _opp(db)
    db.add(ReviewSession(
        opportunity_id=opp.id,
        status="ready_for_review",
        review_policy="conditional",
        required_review_count=1,
        completed_review_count=0,
    ))
    db.commit()
    page = client.get(f"/workspace/{opp.id}?tab=review", cookies={"govcon_session": token}).text
    assert "Approve to Bid" not in page
    assert "Start pursuit (requires approval)" in page
    assert "ready-for-review actions stay disabled until a pursuit exists" in page
