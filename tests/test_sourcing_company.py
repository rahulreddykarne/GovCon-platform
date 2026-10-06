"""Roadmap stage 4: sourcing records (ADR-071) and company registration refresh (ADR-072)."""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_web_ui import _make_user
from web_client import CsrfTestClient

from govcon.db import session_scope
from govcon.models import (
    AISharingAuthorization,
    CompanyRegistration,
    Notification,
    Opportunity,
    Product,
    RfqDraft,
    StoredFile,
    SupplierProduct,
    SupplierQuote,
    SupplierQuoteLine,
    Task,
    User,
)
from govcon.tasks.testing import drain
from govcon.web.app import create_app


@pytest.fixture()
def db(upgraded_engine):
    with Session(upgraded_engine) as session:
        yield session


@pytest.fixture()
def client(upgraded_engine):
    with CsrfTestClient(create_app(), follow_redirects=False) as client:
        yield client


@pytest.fixture()
def synced_prompts(upgraded_engine):
    from govcon.prompting.registry import sync_prompts
    with Session(upgraded_engine) as session:
        sync_prompts(session, Path(__file__).parent.parent / "src" / "govcon" / "prompts")
        session.commit()


def opportunity(db, **kw):
    opp = Opportunity(source="sam", source_id=uuid4().hex, title="Nitrile gloves", status="open",
                      solicitation_number=f"SPE-{uuid4().hex[:6]}", nsn="6515-01-519-8818", quantity=Decimal("500"),
                      unit="PR", response_deadline=datetime.now(UTC) + timedelta(days=20), raw={}, links={}, **kw)
    db.add(opp)
    db.commit()
    return opp


def user(db, role="reviewer"):
    row, token = _make_user(db, f"src-{role}-{uuid4().hex}@example.test", role)
    return row, token


# ── catalogs ─────────────────────────────────────────────────────────────────

def test_catalog_import_validates_rows_and_upserts(db):
    from govcon.sourcing.records import get_or_create_supplier, import_catalog_csv
    actor, _ = user(db)
    name = f"Acme {uuid4().hex[:6]}"
    csv_bytes = (
        "part_number,manufacturer,nsn,description,unit,list_price,valid_until\n"
        f"NG-100-{name},Acme,6515015198818,Nitrile glove M,PR,2.10,2027-01-31\n"
        f",Acme,,missing part,PR,1.00,\n"
        f"NG-200-{name},Acme,,Nitrile glove L,PR,abc,\n"
    ).encode()
    with session_scope() as s:
        supplier, created = get_or_create_supplier(s, name=name, actor=s.get(User, actor.id), provenance="test")
        result = import_catalog_csv(s, supplier_id=supplier.id, data=csv_bytes, filename="cat.csv", actor=s.get(User, actor.id))
        supplier_id = supplier.id
    assert created and (result.rows_total, result.rows_imported) == (3, 1)
    assert [e["line"] for e in result.errors] == [3, 4]
    product = db.scalar(select(Product).where(Product.part_number == f"NG-100-{name}"))
    assert product.nsn == "6515-01-519-8818", "NSNs are stored in canonical dashed form"
    link = db.scalar(select(SupplierProduct).where(SupplierProduct.product_id == product.id))
    assert link.list_price == Decimal("2.10") and link.valid_until == date(2027, 1, 31)
    with session_scope() as s:  # re-import updates, never duplicates
        import_catalog_csv(s, supplier_id=supplier_id, data=csv_bytes.replace(b"2.10", b"1.95"), filename="cat.csv",
                           actor=s.get(User, actor.id))
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(SupplierProduct).where(SupplierProduct.supplier_id == supplier_id)) == 1
    assert db.get(SupplierProduct, link.id).list_price == Decimal("1.95")


# ── quotes feed the analyses ─────────────────────────────────────────────────

def test_csv_quote_from_the_web_feeds_supplier_and_pricing_inputs(db, client):
    from govcon.config import get_settings
    from govcon.intelligence.ai_analyses import _REQUESTS
    opp = opportunity(db)
    _, token = user(db)
    client.cookies.set("govcon_session", token)
    quote_csv = b"description,part_number,quantity,unit,unit_price,lead_time_days\nNitrile glove M,NG-100,500,PR,2.00,14\n"
    files = {"quote_file": ("acme_quote.csv", quote_csv, "text/csv")}
    response = client.post(f"/workspace/{opp.id}/quotes", data={"supplier_name": "Acme Gloves",
                                                                 "valid_until": "2099-12-31"}, files=files)
    assert "notice=" in response.headers["location"], response.headers["location"]
    quote = db.scalar(select(SupplierQuote).where(SupplierQuote.opportunity_id == opp.id))
    assert quote.extraction_method == "csv" and quote.total_price == Decimal("1000.00")
    assert quote.source_sha256 and quote.source_filename == "acme_quote.csv"
    assert db.scalar(select(StoredFile).where(StoredFile.opportunity_id == opp.id)) is None, \
        "a quote document never becomes a solicitation file"
    with session_scope() as s:
        supplier_req = _REQUESTS["supplier"](s, opp.id, get_settings())
        pricing_req = _REQUESTS["pricing"](s, opp.id, get_settings())
    record = supplier_req["variables"]["SUPPLIER_RECORDS_JSON"][0]
    assert record["supplier"] == "Acme Gloves" and record["lines"][0]["unit_price"] == 2.0
    inputs = pricing_req["variables"]["PRICING_INPUTS_JSON"]
    assert inputs["sourcing_cost_total"] == 1000.0 and inputs["unit_cost"] == 2.0
    assert inputs["sourcing_cost_source"].startswith("lowest current supplier quote")
    page = client.get(f"/workspace/{opp.id}?tab=products").text
    assert "Acme Gloves" in page and "$1,000.00" in page


def test_expired_quotes_are_not_used(db):
    from govcon.config import get_settings
    from govcon.intelligence.ai_analyses import _REQUESTS, AnalysisInputMissing
    from govcon.sourcing.records import (
        get_or_create_supplier,
        record_quote,
        sourcing_revision,
    )
    opp = opportunity(db)
    actor, _ = user(db)
    with session_scope() as s:
        before = sourcing_revision(s, opp.id)
        supplier, _ = get_or_create_supplier(s, name=f"Old {uuid4().hex[:6]}", actor=None, provenance="test")
        record_quote(s, opportunity_id=opp.id, supplier_id=supplier.id, actor=s.get(User, actor.id), method="manual",
                     total_price="900", valid_until=(date.today() - timedelta(days=1)).isoformat())
        assert sourcing_revision(s, opp.id) == before, "an expired quote is not current"
        with pytest.raises(AnalysisInputMissing):
            _REQUESTS["supplier"](s, opp.id, get_settings())


# ── PDF quotes and the explicit authorization ────────────────────────────────

def quote_pdf() -> bytes:
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(72, 720, "QUOTE Q-77  valid until 2099-06-30")
    pdf.drawString(72, 700, "Nitrile glove M  NG-100  500 PR  @ 2.05  = 1025.00  lead time 10 days")
    pdf.save()
    return buffer.getvalue()


def test_pdf_quote_waits_without_authorization_then_is_read(db, client, monkeypatch, allow_proprietary_ai, synced_prompts):
    from govcon.config import get_settings
    from govcon.sourcing.records import grant_authorization
    monkeypatch.setenv("AI_PRIMARY_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()
    opp = opportunity(db)
    owner, token = user(db, "owner")
    client.cookies.set("govcon_session", token)
    client.post(f"/workspace/{opp.id}/quotes", data={"supplier_name": "Scan Supply"},
                files={"quote_file": ("quote.pdf", quote_pdf(), "application/pdf")})
    task = db.scalar(select(Task).where(Task.opportunity_id == opp.id, Task.task_type == "quote_extraction"))
    assert task.status == "queued"
    calls = []

    class Fake:
        name = "anthropic"

        def complete(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content=json.dumps({
                "supplier_name": "Scan Supply", "valid_until": "2099-06-30", "total_price": 1025.0,
                "lines": [{"description": "Nitrile glove M", "part_number": "NG-100", "quantity": 500, "unit": "PR",
                           "unit_price": 2.05, "extended_price": 1025.0, "lead_time_days": 10,
                           "source_quote": "Nitrile glove M  NG-100  500 PR  @ 2.05"}],
                "missing_information": []}), usage={}, model="claude", provider="anthropic", latency_ms=1)

    monkeypatch.setattr("govcon.ai.structured.get_provider", lambda *a, **k: Fake())
    assert drain(opportunity_id=opp.id, task_types=["quote_extraction"])[0][1] == "waiting_for_input"
    db.expire_all()
    task = db.get(Task, task.id)
    assert task.blocker_owner_role == "owner" and "Settings" in task.blocker_next_action and not calls
    assert "Settings page" in client.get(f"/workspace/{opp.id}?tab=products").text

    with session_scope() as s:
        grant_authorization(s, provider="anthropic", days=30, reason="Quotes from our usual distributors", actor=s.get(User, owner.id))
        from govcon.tasks.queue import requeue
        requeue(s, s.get(Task, task.id), actor_user_id=owner.id, reason="authorized")
    assert drain(opportunity_id=opp.id, task_types=["quote_extraction"])[0][1] == "succeeded"
    assert len(calls) == 1 and "NG-100" in calls[0]["user_prompt"]
    quote = db.scalar(select(SupplierQuote).where(SupplierQuote.opportunity_id == opp.id))
    assert quote.extraction_method == "ai" and quote.total_price == Decimal("1025.0")
    assert quote.valid_until == date(2099, 6, 30) and "verify" in quote.notes
    line = db.scalar(select(SupplierQuoteLine).where(SupplierQuoteLine.quote_id == quote.id))
    assert (line.part_number, line.unit_price, line.lead_time_days) == ("NG-100", Decimal("2.05"), 10)


def test_only_an_owner_grants_and_revokes_ai_sharing(db, client):
    from govcon.collaboration.users import PermissionDenied
    from govcon.sourcing.records import (
        SourcingError,
        active_authorization,
        grant_authorization,
    )
    approver, _ = user(db, "approver")
    with pytest.raises(PermissionDenied), session_scope() as s:
        grant_authorization(s, provider="openai", days=30, reason="Not allowed for approvers", actor=s.get(User, approver.id))
    owner, token = user(db, "owner")
    with pytest.raises(SourcingError), session_scope() as s:
        grant_authorization(s, provider="openai", days=30, reason="short", actor=s.get(User, owner.id))
    client.cookies.set("govcon_session", token)
    granted = client.post("/settings/ai-sharing", data={"action": "grant", "provider": "openai", "days": "7",
                                                        "reason": "Pilot with a single distributor"})
    assert "notice=" in granted.headers["location"]
    row = db.scalar(select(AISharingAuthorization).where(AISharingAuthorization.provider == "openai")
                    .order_by(AISharingAuthorization.id.desc()))
    with session_scope() as s:
        assert active_authorization(s, provider="openai") is not None
    client.post("/settings/ai-sharing", data={"action": "revoke", "authorization_id": str(row.id)})
    db.expire_all()
    assert db.get(AISharingAuthorization, row.id).revoked_at is not None
    with session_scope() as s:
        assert active_authorization(s, provider="openai") is None or active_authorization(s, provider="openai").id != row.id


# ── RFQ drafts ───────────────────────────────────────────────────────────────

def test_rfq_draft_uses_the_opportunity_facts_and_is_never_sent(db, client, monkeypatch):
    sent = []
    monkeypatch.setattr("govcon.alerts.digest.send_smtp", lambda *a, **k: sent.append(k))
    opp = opportunity(db)
    _, token = user(db)
    client.cookies.set("govcon_session", token)
    assert "notice=" in client.post(f"/workspace/{opp.id}/rfq", data={}).headers["location"]
    draft = db.scalar(select(RfqDraft).where(RfqDraft.opportunity_id == opp.id))
    assert draft.status == "draft" and opp.solicitation_number in draft.subject
    assert "6515-01-519-8818" in draft.body and "500" in draft.body and "valid" in draft.body
    assert not sent
    assert "never sends it" in client.get(f"/workspace/{opp.id}?tab=products").text


# ── company registration ─────────────────────────────────────────────────────

def _vendor(expiration: date, status="Active"):
    return SimpleNamespace(legal_name="OUR COMPANY LLC", cage_code="7XYZ1", registration_status=status,
                           raw={"entityRegistration": {"registrationExpirationDate": expiration.isoformat(),
                                                       "registrationStatus": status}})


def test_registration_refresh_overlays_facts_and_alerts_before_expiry(db, monkeypatch):
    from govcon.company.registration import (
        overlay_registration,
        refresh_company_registration,
    )
    from govcon.config import Settings
    uei = f"T{uuid4().hex[:11].upper()}"
    approver, _ = user(db, "approver")
    now = datetime(2026, 10, 6, 0, 30, tzinfo=UTC)
    expires = now.date() + timedelta(days=45)
    monkeypatch.setattr("govcon.ingest.sam_entities.ensure_vendor", lambda *a, **k: (_vendor(expires), True))
    configured = Settings(_env_file=None, company_uei=uei, sam_api_key="test")
    with session_scope() as s:
        result = refresh_company_registration(s, settings=configured, now=now)
    assert result.status == "refreshed" and result.expiration_date == expires and result.alerted
    alerts = db.scalars(select(Notification).where(Notification.user_id == approver.id,
                                                   Notification.notification_type == "registration_expiring")).all()
    assert len(alerts) == 1 and alerts[0].payload["days_left"] == 45
    with session_scope() as s:  # no repeat within a week
        assert refresh_company_registration(s, settings=configured, now=now + timedelta(days=1)).alerted is False
    with session_scope() as s:
        facts = overlay_registration(s, {"uei": uei, "sam_registration_status": "Inactive", "naics_codes": ["339113"]},
                                     settings=configured, now=now)
    assert facts["sam_registration_status"] == "Active" and facts["sam_expiration_date"] == expires.isoformat()
    assert facts["naics_codes"] == ["339113"], "other company facts stay human-maintained"
    assert facts["_provenance"]["sam_registration"]["stale"] is False
    with session_scope() as s:
        stale = overlay_registration(s, {"uei": uei, "sam_registration_status": "Active"}, settings=configured,
                                     now=now + timedelta(days=10))
    assert "sam_registration_status" not in stale and stale["_provenance"]["sam_registration"]["stale"] is True


def test_registration_refresh_skips_without_a_uei():
    from govcon.company.registration import refresh_company_registration
    from govcon.config import Settings
    with session_scope() as s:
        result = refresh_company_registration(s, settings=Settings(_env_file=None, sam_api_key="test"))
    assert result.status == "skipped" and "UEI" in result.reason


def test_settings_page_shows_the_registration(db, monkeypatch):
    from govcon.config import get_settings
    uei = f"S{uuid4().hex[:11].upper()}"
    monkeypatch.setenv("COMPANY_UEI", uei)  # before the app reads its settings
    get_settings.cache_clear()
    with session_scope() as s:
        s.add(CompanyRegistration(uei=uei, legal_name="SHOWN ON SETTINGS LLC", registration_status="Active",
                                  expiration_date=date(2099, 1, 1), refreshed_at=datetime.now(UTC)))
    _, token = user(db, "owner")
    with CsrfTestClient(create_app(), follow_redirects=False) as client:
        client.cookies.set("govcon_session", token)
        page = client.get("/settings").text
    assert "SHOWN ON SETTINGS LLC" in page and "AI reading of supplier quotes" in page
