"""Laptop check at b777de0: priced estimate, budget always visible, one rerun button."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_web_ui import _make_user
from web_client import CsrfTestClient

from govcon.config import Settings
from govcon.models import (
    AIAnalysis,
    AICallUsage,
    AIModelPrice,
    FilePage,
    Opportunity,
    Requirement,
    StoredFile,
    Task,
)
from govcon.web.app import create_app


def test_estimate_uses_routed_model_price_and_cached_count(db) -> None:
    from govcon.ai.price_catalog import SEED_PRICES
    from govcon.ai.spend_guard import estimate_review
    from govcon.ai.usage_log import ReportedTokens, cost_for, price_for

    opp = _opp(db)
    stored = StoredFile(
        opportunity_id=opp.id, filename="rfq.pdf", classification="PUBLIC",
        source_origin="dibbs", extracted_text="page", extraction_status="success",
        mime_type="application/pdf",
    )
    db.add(stored)
    db.flush()
    for page_no in range(1, 4):
        db.add(FilePage(file_id=stored.id, page_no=page_no, text=f"page {page_no}", text_source="native", char_count=6))
    db.add(AIAnalysis(
        opportunity_id=opp.id, analysis_type="solicitation_summary",
        output_json={"summary": "cached"}, generation_settings={"role": "part"},
    ))
    db.flush()
    settings = Settings(_env_file=None)
    estimate = estimate_review(db, opp.id, settings)
    assert estimate["cached_parts"] == 1
    assert estimate["usd"] is not None
    price = price_for(db, str(estimate["provider"]), str(estimate["model"]))
    if price is None:
        seed = next(item for item in SEED_PRICES if item["provider"] == "deepseek" and item["model"] == "deepseek-flash")
        price = AIModelPrice(**{k: seed[k] for k in (
            "provider", "model", "input_usd_per_million", "output_usd_per_million",
            "cached_usd_per_million", "cache_write_usd_per_million", "web_search_usd_per_thousand",
            "source_url", "effective_as_of", "note",
        )})
        db.add(price)
        db.flush()
        estimate = estimate_review(db, opp.id, settings)
        price = price_for(db, str(estimate["provider"]), str(estimate["model"]))
    assert price is not None
    expected = cost_for(
        ReportedTokens(estimate["input_tokens"], estimate["output_tokens"], estimate["cached_tokens"], None),
        price,
    )
    assert expected is not None
    assert abs(float(estimate["usd"]) - float(expected)) < 1e-9


def test_workspace_shows_dollar_estimate_and_always_shows_budget(db, client) -> None:
    user, token = _make_user(db, f"usd-{uuid4().hex[:8]}@example.test", "owner")
    client.cookies.set("govcon_session", token)
    opp = _opp(db)
    stored = StoredFile(
        opportunity_id=opp.id, filename="rfq.pdf", classification="PUBLIC",
        source_origin="dibbs", extracted_text="page", extraction_status="success",
        mime_type="application/pdf",
    )
    db.add(stored)
    db.flush()
    db.add(FilePage(file_id=stored.id, page_no=1, text="page 1", text_source="native", char_count=6))
    db.commit()
    page = client.get(f"/workspace/{opp.id}?tab=overview").text
    assert 'id="spend-estimate"' in page
    assert "$" in page.split('id="spend-estimate"', 1)[1].split("</div>", 1)[0]
    assert "1 cached part" in page or "0 cached part" in page
    assert "spendable" in page
    assert "Used" in page and "limit" in page
    assert page.count(f'action="/workspace/{opp.id}/analyze"') == 1


def test_estimate_over_spendable_blocks_and_offers_raise(db, client) -> None:
    user, token = _make_user(db, f"cap-{uuid4().hex[:8]}@example.test", "owner")
    client.cookies.set("govcon_session", token)
    opp = _opp(db)
    opp.ai_max_input_tokens = 20_000
    stored = StoredFile(
        opportunity_id=opp.id, filename="rfq.pdf", classification="PUBLIC",
        source_origin="dibbs", extracted_text="page", extraction_status="success",
        mime_type="application/pdf",
    )
    db.add(stored)
    db.flush()
    for page_no in range(1, 21):
        db.add(FilePage(file_id=stored.id, page_no=page_no, text=f"page {page_no}", text_source="native", char_count=8))
    db.add(AICallUsage(
        opportunity_id=opp.id, purpose="solicitation_analysis", provider="deepseek",
        status="succeeded", input_tokens=10_000, output_tokens=1,
    ))
    db.commit()
    page = client.get(f"/workspace/{opp.id}?tab=overview").text
    assert 'id="estimate-exceeds-budget"' in page
    assert "Raise budget for this opportunity" in page
    assert f'action="/workspace/{opp.id}/analyze"' not in page
    assert "spendable" in page
    denied = client.post(f"/workspace/{opp.id}/analyze")
    assert denied.status_code == 303
    db.expire_all()
    assert db.scalar(select(Task).where(Task.opportunity_id == opp.id, Task.task_type == "opportunity_review")) is None


def test_trace_document_from_stored_requirements(db) -> None:
    from govcon.operating.trace import opportunity_trace

    opp = _opp(db)
    db.add(Requirement(
        opportunity_id=opp.id, requirement_text="Offeror shall acknowledge amendments.",
        requirement_type="administrative", status="unreviewed",
    ))
    db.flush()
    stages = {stage["id"]: stage for stage in opportunity_trace(db, opp)}
    assert stages["document"]["status"] == "stored"
    assert "1 requirement" in stages["document"]["detail"]
    assert stages["document"]["status"] != "not run"


def test_prefix_part_disclaimers_stripped_at_render(db, client) -> None:
    from govcon.enrich.summarize import display_solicitation_output

    user, token = _make_user(db, f"sum-{uuid4().hex[:8]}@example.test", "owner")
    client.cookies.set("govcon_session", token)
    opp = _opp(db)
    db.add_all([
        AIAnalysis(
            opportunity_id=opp.id, analysis_type="solicitation_summary",
            output_json={"summary": "FOB origin (only part 1 of 13; later pages not read)."},
            generation_settings={"role": "part", "part_index": 1, "quality": "accepted"},
        ),
        AIAnalysis(
            opportunity_id=opp.id, analysis_type="solicitation_summary",
            output_json={"summary": "Delivery is 30 days (only part 2 of 13)."},
            generation_settings={"role": "part", "part_index": 2, "quality": "accepted"},
        ),
    ])
    db.commit()
    display = display_solicitation_output(db, opp.id)
    assert display is not None
    assert "only part" not in (display.get("summary") or "").lower()
    assert "FOB origin" in display["summary"]
    assert "Delivery is 30 days" in display["summary"]
    assert "Coverage:" in (display.get("coverage_note") or "")
    page = client.get(f"/workspace/{opp.id}?tab=overview").text
    assert "FOB origin" in page
    assert "only part 1 of 13" not in page
    assert "only part 2 of 13" not in page
    assert "Coverage:" in page


def _opp(db, **values) -> Opportunity:
    data = {
        "source": "dibbs",
        "source_id": f"b777de0-{uuid4().hex}",
        "title": f"DIBBS RFQ {uuid4().hex[:8]}",
        "status": "open",
        "raw": {},
        "links": {},
    }
    data.update(values)
    row = Opportunity(**data)
    db.add(row)
    db.flush()
    return row


@pytest.fixture()
def db(upgraded_engine):
    with Session(upgraded_engine) as session:
        yield session


@pytest.fixture()
def client(upgraded_engine):
    with CsrfTestClient(create_app(), follow_redirects=False) as client:
        yield client
