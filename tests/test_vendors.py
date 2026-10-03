"""Phase 6 vendor profiles, competitor intelligence, and contact search."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker
from tenacity import wait_exponential
from typer.testing import CliRunner

from govcon.cli import app
from govcon.config import Settings
from govcon.ingest.sam_entities import (
    SAM_ENTITY_URL,
    SamEntityError,
    ensure_vendor,
    fetch_entity_payload,
    parse_entity_record,
)
from govcon.intelligence.competitors import competitor_summary, opportunity_office, top_awardees_for_office
from govcon.intelligence.contacts import search_contacts
from govcon.intelligence.vendors import award_stats_for_uei, vendor_profile
from govcon.models import Award, Contact, Opportunity, Vendor
from govcon.paths import repo_root

runner = CliRunner()
FAST_WAIT = wait_exponential(multiplier=0.01, min=0.01, max=0.05)
FIXTURE = repo_root() / "tests" / "fixtures" / "sam_entity_v3.json"
UEI = "ZJEUBM5FYLQ2"
NOW = datetime(2026, 9, 26, 20, 30, tzinfo=UTC)


@pytest.fixture
def session(upgraded_engine) -> Session:
    factory = sessionmaker(bind=upgraded_engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _settings(tmp_path) -> Settings:
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        sam_api_key="phase6-test-key",
        sam_vendor_cache_hours=24,
        outbox_dir=tmp_path,
    )


def _entity_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["entityData"][0]


def _mock_entity_client() -> httpx.Client:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.sam.gov"
        assert request.url.path.endswith("/entity-information/v3/entities")
        assert request.url.params.get("ueiSAM") == UEI
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def _award(session: Session, **overrides) -> Award:
    award_id = overrides.pop("award_id", f"phase6-{uuid4().hex}")
    row = Award(
        award_id=award_id,
        piid=overrides.pop("piid", award_id),
        description=overrides.pop("description", "phase 6 award"),
        psc_code=overrides.pop("psc_code", "6515"),
        naics_code=overrides.pop("naics_code", "423450"),
        nsn=overrides.pop("nsn", "6545-01-632-0167"),
        recipient_uei=overrides.pop("recipient_uei", UEI),
        recipient_name=overrides.pop("recipient_name", "CARDINAL HEALTH 200, LLC"),
        awarding_agency=overrides.pop(
            "awarding_agency", "Department of Defense / Defense Logistics Agency"
        ),
        action_date=overrides.pop("action_date", datetime(2026, 6, 11).date()),
        total_obligation=overrides.pop("total_obligation", Decimal("96870.96")),
        raw=overrides.pop(
            "raw",
            {
                "Awarding Agency": "Department of Defense",
                "Awarding Sub Agency": "Defense Logistics Agency",
            },
        ),
    )
    session.add(row)
    session.flush()
    return row


def _opportunity(session: Session, **overrides) -> Opportunity:
    source_id = overrides.pop("source_id", f"phase6-{uuid4().hex}")
    row = Opportunity(
        source=overrides.pop("source", "sam"),
        source_id=source_id,
        title=overrides.pop("title", "Medical supplies"),
        psc_code=overrides.pop("psc_code", "6515"),
        naics_code=overrides.pop("naics_code", "423450"),
        nsn=overrides.pop("nsn", "6545-01-632-0167"),
        agency_path=overrides.pop(
            "agency_path",
            "GENERAL SERVICES ADMINISTRATION.PUBLIC BUILDINGS SERVICE.PBS R5",
        ),
        raw=overrides.pop(
            "raw",
            {
                "office": "PBS R5",
                "department": "GENERAL SERVICES ADMINISTRATION",
                "subTier": "PUBLIC BUILDINGS SERVICE",
            },
        ),
        status="open",
    )
    session.add(row)
    session.flush()
    return row


def test_parse_entity_record_maps_public_fields() -> None:
    parsed = parse_entity_record(_entity_payload())
    assert parsed is not None
    assert parsed.uei == UEI
    assert parsed.cage_code == "1ABC2"
    assert parsed.registration_status == "Active"
    assert parsed.legal_name == "CARDINAL HEALTH 200, LLC"
    assert parsed.business_types is not None
    assert parsed.naics_codes == [{"code": "423450", "description": "Medical Equipment Merchant Wholesalers"}]
    assert parsed.psc_codes == [{"code": "6515", "description": "Medical and Surgical Instruments"}]


def test_vendor_profile_combines_sam_data_and_award_stats(session: Session, tmp_path) -> None:
    _award(session, total_obligation=Decimal("100"))
    _award(
        session,
        awarding_agency="Department of Defense / Defense Logistics Agency",
        psc_code="6515",
        total_obligation=Decimal("200"),
    )
    _award(
        session,
        recipient_uei="OTHERUEI1234",
        recipient_name="Other Vendor",
        awarding_agency="Department of Veterans Affairs",
        psc_code="R425",
        total_obligation=Decimal("50"),
    )
    session.commit()

    settings = _settings(tmp_path)
    client = _mock_entity_client()
    profile = vendor_profile(session, UEI, client=client, settings=settings, now=NOW)
    assert profile.registration_status == "Active"
    assert profile.from_cache is False
    assert profile.award_stats.award_count == 2
    assert profile.award_stats.total_obligation == Decimal("300")
    assert profile.award_stats.top_agencies[0].label.endswith("Defense Logistics Agency")
    assert profile.award_stats.top_pscs[0].label == "6515"


def test_cache_prevents_unnecessary_sam_calls(session: Session, tmp_path) -> None:
    settings = _settings(tmp_path)
    client = _mock_entity_client()
    ensure_vendor(session, UEI, client=client, settings=settings, now=NOW)
    session.commit()

    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json=json.loads(FIXTURE.read_text(encoding="utf-8")))

    transport = httpx.MockTransport(handler)
    second_client = httpx.Client(transport=transport)
    row, fetched = ensure_vendor(
        session,
        UEI,
        client=second_client,
        settings=settings,
        now=NOW + timedelta(hours=1),
    )
    assert fetched is False
    assert calls["count"] == 0
    assert row.legal_name == "CARDINAL HEALTH 200, LLC"


def test_refresh_bypasses_cache(session: Session, tmp_path) -> None:
    settings = _settings(tmp_path)
    client = _mock_entity_client()
    ensure_vendor(session, UEI, client=client, settings=settings, now=NOW - timedelta(days=2))
    session.commit()

    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json=json.loads(FIXTURE.read_text(encoding="utf-8")))

    transport = httpx.MockTransport(handler)
    second_client = httpx.Client(transport=transport)
    _, fetched = ensure_vendor(
        session,
        UEI,
        refresh=True,
        client=second_client,
        settings=settings,
        now=NOW,
    )
    assert fetched is True
    assert calls["count"] == 1


def test_competitor_summary_groups_by_nsn_psc_agency_and_office(session: Session) -> None:
    opportunity = _opportunity(session)
    _award(session, recipient_uei=UEI, recipient_name="Cardinal", total_obligation=Decimal("900"))
    _award(
        session,
        recipient_uei="OTHERUEI1234",
        recipient_name="Other Vendor",
        total_obligation=Decimal("100"),
    )
    _award(
        session,
        recipient_uei="THIRDUEI12345",
        recipient_name="Third Vendor",
        awarding_agency="Department of Veterans Affairs / PBS R5",
        raw={"Awarding Agency": "Department of Veterans Affairs", "Awarding Sub Agency": "PBS R5"},
        total_obligation=Decimal("75"),
    )
    _award(
        session,
        recipient_uei="AGENCYWIN1234",
        recipient_name="Agency Winner",
        awarding_agency="General Services Administration / Public Buildings Service",
        raw={
            "Awarding Agency": "General Services Administration",
            "Awarding Sub Agency": "Public Buildings Service",
        },
        total_obligation=Decimal("40"),
    )
    session.commit()

    summary = competitor_summary(session, opportunity.id, limit=3)
    assert summary is not None
    dimensions = {bucket.dimension: bucket for bucket in summary.buckets}
    assert "nsn" in dimensions
    assert dimensions["nsn"].winners[0].recipient_uei == UEI
    assert "psc" in dimensions
    assert "agency" in dimensions
    assert "office" in dimensions
    assert opportunity_office(opportunity) == "PBS R5"
    office_winners = top_awardees_for_office(session, "PBS R5", limit=3)
    assert office_winners
    assert office_winners[0].recipient_uei in {UEI, "THIRDUEI12345"}


def test_search_contacts_by_name_agency_and_email(session: Session) -> None:
    opportunity = _opportunity(session)
    session.add(
        Contact(
            name="Jesse L. Jones",
            email="jesse.jones@gsa.gov",
            phone="2174941263",
            title="Contracting Officer",
            agency_path="GENERAL SERVICES ADMINISTRATION.PUBLIC BUILDINGS SERVICE.PBS R5",
            contact_type="primary",
            first_seen_opportunity_id=opportunity.id,
        )
    )
    session.add(
        Contact(
            name="Other Buyer",
            email="buyer@example.mil",
            agency_path="Department of Defense",
            contact_type="primary",
        )
    )
    session.commit()

    by_name = search_contacts(session, name="Jesse")
    assert len(by_name) == 1
    assert by_name[0].email == "jesse.jones@gsa.gov"

    by_agency = search_contacts(session, agency="PUBLIC BUILDINGS")
    assert len(by_agency) == 1

    by_email = search_contacts(session, email="gsa.gov")
    assert len(by_email) == 1


def test_fetch_entity_payload_requires_api_key(tmp_path) -> None:
    settings = Settings(database_url=os.environ["DATABASE_URL"], sam_api_key=None, outbox_dir=tmp_path)
    client = httpx.Client()
    with pytest.raises(Exception, match="SAM_API_KEY"):
        fetch_entity_payload(client, settings, UEI, wait=FAST_WAIT)


def test_cli_vendors_show_and_competitors(session: Session, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    monkeypatch.setenv("SAM_API_KEY", "phase6-test-key")
    monkeypatch.setenv("OUTBOX_DIR", str(tmp_path))

    _award(session)
    opportunity = _opportunity(session)
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(SAM_ENTITY_URL):
            return httpx.Response(200, json=json.loads(FIXTURE.read_text(encoding="utf-8")))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    cache_now = datetime.now(UTC)
    with httpx.Client(transport=transport) as client:
        first = vendor_profile(session, UEI, client=client, settings=_settings(tmp_path), now=cache_now)
        second = vendor_profile(
            session,
            UEI,
            client=client,
            settings=_settings(tmp_path),
            now=cache_now + timedelta(hours=1),
        )
    assert first.from_cache is False
    assert second.from_cache is True
    session.commit()

    show = runner.invoke(app, ["vendors", "show", "--uei", UEI])
    assert show.exit_code == 0, show.stdout
    assert "registration_status: Active" in show.stdout
    assert "award_count: 1" in show.stdout

    competitors = runner.invoke(app, ["vendors", "competitors", "--opportunity-id", str(opportunity.id)])
    assert competitors.exit_code == 0, competitors.stdout
    assert "dimension: nsn" in competitors.stdout
    assert "dimension: psc" in competitors.stdout


def test_cli_contacts_search(session: Session, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    monkeypatch.setenv("OUTBOX_DIR", str(tmp_path))
    opportunity = _opportunity(session)
    session.add(
        Contact(
            name="Jesse L. Jones",
            email="jesse.jones@gsa.gov",
            agency_path=opportunity.agency_path,
            first_seen_opportunity_id=opportunity.id,
        )
    )
    session.commit()

    result = runner.invoke(app, ["contacts", "search", "--email", "jesse.jones"])
    assert result.exit_code == 0, result.stdout
    assert "count: 1" in result.stdout
    assert "jesse.jones@gsa.gov" in result.stdout


@pytest.fixture(autouse=True)
def _cleanup_phase6_rows(session: Session):
    yield
    session.rollback()
    session.execute(
        delete(Vendor).where(
            Vendor.uei.in_([UEI, "OTHERUEI1234", "THIRDUEI12345", "AGENCYWIN1234"])
        )
    )
    session.execute(delete(Award).where(Award.award_id.like("phase6-%")))
    session.execute(delete(Contact).where(Contact.email.in_(["jesse.jones@gsa.gov", "buyer@example.mil"])))
    session.execute(delete(Opportunity).where(Opportunity.source_id.like("phase6-%")))
    session.commit()
