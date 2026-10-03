"""Regressions for the 2026-10-03 production blockers.

SAM registration data must not look current when the fetch is stale, failed,
cancelled, or older than data already stored. A later registration period
must not keep the previous period's assertions.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker
from tenacity import wait_exponential
from test_web_ui import _make_user
from web_client import CsrfTestClient

from govcon.ai.replay import Recorder, active_recorder, run_recorded
from govcon.config import Settings
from govcon.ingest.freshness import evidence_status, registration_key, response_is_older
from govcon.ingest.sam_entities import (
    SamEntityError,
    cancel_vendor_refresh,
    ensure_vendor,
)
from govcon.models import (
    Award,
    CompanyRegistration,
    Match,
    Opportunity,
    Task,
    Vendor,
    Watchlist,
)
from govcon.prompting.loader import PromptAsset
from govcon.prompting.registry import PromptRegistryDenied, _verify_includes
from govcon.prompting.renderer import PromptRenderError, render_system_prompt
from govcon.web.app import create_app

NOW = datetime(2026, 9, 26, 20, 30, tzinfo=UTC)
FAST_WAIT = wait_exponential(multiplier=0.01, min=0.01, max=0.02)


@pytest.fixture
def session(upgraded_engine) -> Session:
    factory = sessionmaker(bind=upgraded_engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture
def fast_sam(monkeypatch):
    monkeypatch.setattr("govcon.ingest.sam_entities.SAM_ENTITY_RETRY_WAIT", FAST_WAIT)


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "database_url": os.environ["DATABASE_URL"],
        "sam_api_key": "phase6-test-key",
        "sam_vendor_cache_hours": 24,
        "company_facts_max_age_days": 3,
        "outbox_dir": tmp_path,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _uei() -> str:
    return uuid4().hex[:12].upper()


def _record(
    uei: str,
    *,
    legal: str,
    status: str = "Active",
    registration_date: str = "2020-01-15",
    last_update: str = "2026-08-01",
    expiration: str = "2027-06-30",
    activation: str | None = None,
    naics: bool = True,
) -> dict:
    registration = {
        "ueiSAM": uei,
        "cageCode": "1ABC2",
        "legalBusinessName": legal,
        "registrationStatus": status,
        "registrationDate": registration_date,
        "lastUpdateDate": last_update,
        "registrationExpirationDate": expiration,
        "ueiStatus": "Active",
    }
    if activation is not None:
        registration["activationDate"] = activation
    record: dict = {"entityRegistration": registration, "coreData": {}, "pointsOfContact": []}
    if naics:
        record["assertions"] = {
            "naicsList": [{"naicsCode": "423450", "naicsDescription": "Medical Equipment"}],
        }
    return record


def _client(record: dict, *, on_request=None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if on_request is not None:
            on_request(request)
        return httpx.Response(200, json={"entityData": [record]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _failing_client(status_code: int = 503, body: str = "unavailable") -> tuple[httpx.Client, dict[str, int]]:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(status_code, text=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def _vendor(session: Session, uei: str) -> Vendor:
    row = session.get(Vendor, uei)
    assert row is not None
    session.refresh(row)
    return row


def test_fresh_fetch_records_freshness_and_a_recent_expiry_stays_cached(session: Session, tmp_path, fast_sam) -> None:
    uei = _uei()
    settings = _settings(tmp_path)
    record = _record(uei, legal="FRESH LLC", expiration="2026-01-15", last_update="2025-06-01")
    row, applied = ensure_vendor(session, uei, client=_client(record), settings=settings, now=NOW)
    assert applied is True
    assert row.freshness_status == "expired"
    assert row.fetched_at == NOW
    assert row.source_updated_at == datetime(2025, 6, 1, tzinfo=UTC)
    assert row.expires_at.isoformat() == "2026-01-15"
    assert row.attempt_state == "applied"
    assert row.registration_status == "Active"
    session.commit()

    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json={"entityData": [record]})

    cached, fetched = ensure_vendor(
        session, uei, client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=settings, now=NOW + timedelta(hours=1),
    )
    assert fetched is False
    assert calls["count"] == 0
    assert cached.legal_name == "FRESH LLC"
    assert evidence_status(cached, now=NOW + timedelta(hours=1), max_age=timedelta(hours=24)) == "expired"


def test_failed_refresh_clears_registration_assertions_and_is_not_a_cache_hit(
    session: Session, tmp_path, fast_sam,
) -> None:
    uei = _uei()
    settings = _settings(tmp_path)
    ensure_vendor(
        session, uei, client=_client(_record(uei, legal="WAS ACTIVE LLC")), settings=settings, now=NOW,
    )
    session.commit()
    failing, calls = _failing_client()
    with pytest.raises(SamEntityError):
        ensure_vendor(
            session, uei, refresh=True, client=failing, settings=settings, now=NOW + timedelta(hours=2),
        )
    row = _vendor(session, uei)
    assert row.freshness_status == "failed"
    assert row.registration_status is None
    assert row.legal_name is None
    assert row.naics_codes is None
    assert row.raw is None
    assert row.registration_key is None
    assert row.expires_at is None
    assert row.attempt_state == "failed"
    assert "phase6-test-key" not in (row.last_refresh_error or "")
    assert calls["count"] == 5
    session.commit()

    other = sessionmaker(bind=session.get_bind(), expire_on_commit=False)()
    try:
        stored = other.get(Vendor, uei)
        assert stored is not None
        assert stored.freshness_status == "failed"
        assert stored.registration_status is None
        assert evidence_status(stored, now=NOW + timedelta(hours=3), max_age=timedelta(days=3)) == "failed"
    finally:
        other.close()


def test_outage_then_recovery_replaces_the_failed_registration(session: Session, tmp_path, fast_sam) -> None:
    uei = _uei()
    settings = _settings(tmp_path)
    ensure_vendor(session, uei, client=_client(_record(uei, legal="BEFORE OUTAGE")), settings=settings, now=NOW)
    session.commit()
    failing, _ = _failing_client()
    with pytest.raises(SamEntityError):
        ensure_vendor(session, uei, refresh=True, client=failing, settings=settings, now=NOW + timedelta(minutes=5))
    session.commit()
    recovered = _record(uei, legal="AFTER RECOVERY LLC", last_update="2026-09-01")
    row, applied = ensure_vendor(
        session, uei, refresh=True, client=_client(recovered), settings=settings, now=NOW + timedelta(hours=1),
    )
    assert applied is True
    assert row.legal_name == "AFTER RECOVERY LLC"
    assert row.freshness_status == "fresh"
    assert row.registration_status == "Active"
    assert row.last_refresh_error is None


def test_retries_then_success_keeps_one_registration(session: Session, tmp_path, fast_sam) -> None:
    uei = _uei()
    settings = _settings(tmp_path)
    record = _record(uei, legal="RETRIED LLC")
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"entityData": [record]})

    row, applied = ensure_vendor(
        session, uei, client=httpx.Client(transport=httpx.MockTransport(handler)), settings=settings, now=NOW,
    )
    assert applied is True
    assert calls["count"] == 3
    assert row.legal_name == "RETRIED LLC"
    assert row.freshness_status == "fresh"
    assert row.refresh_generation == 1


def test_older_response_after_a_newer_one_is_discarded(session: Session, tmp_path, fast_sam) -> None:
    uei = _uei()
    settings = _settings(tmp_path)
    newer = _record(uei, legal="NEWER LLC", last_update="2026-09-01", expiration="2027-12-31")
    older = _record(uei, legal="OLDER LLC", last_update="2024-01-01", expiration="2025-01-01", naics=False)
    ensure_vendor(session, uei, client=_client(newer), settings=settings, now=NOW)
    session.commit()
    row, applied = ensure_vendor(
        session, uei, refresh=True, client=_client(older), settings=settings, now=NOW + timedelta(minutes=10),
    )
    assert applied is False
    assert row.legal_name == "NEWER LLC"
    assert row.naics_codes is not None
    assert row.source_updated_at == datetime(2026, 9, 1, tzinfo=UTC)
    assert row.expires_at.isoformat() == "2027-12-31"
    assert row.freshness_status == "fresh"


def test_cancelled_refresh_does_not_apply_a_late_response(session: Session, tmp_path, upgraded_engine, fast_sam) -> None:
    uei = _uei()
    settings = _settings(tmp_path)
    first = _record(uei, legal="KEPT LLC", last_update="2026-07-01")
    ensure_vendor(session, uei, client=_client(first), settings=settings, now=NOW)
    session.commit()
    replacement = _record(uei, legal="LATE LLC", last_update="2026-09-20", naics=False)

    def cancel_during_fetch(request: httpx.Request) -> None:
        session.commit()
        other = Session(upgraded_engine)
        try:
            pending = other.get(Vendor, uei)
            assert pending is not None and pending.attempt_state == "in_progress"
            assert cancel_vendor_refresh(other, uei, pending.attempt_id)
            other.commit()
        finally:
            other.close()

    row, applied = ensure_vendor(
        session, uei, refresh=True, client=_client(replacement, on_request=cancel_during_fetch),
        settings=settings, now=NOW + timedelta(minutes=5),
    )
    assert applied is False
    assert row.legal_name == "KEPT LLC"
    assert row.naics_codes is not None
    assert row.attempt_state == "cancelled"
    session.commit()
    other = Session(upgraded_engine)
    try:
        stored = other.get(Vendor, uei)
        assert stored.legal_name == "KEPT LLC"
        assert stored.registration_status == "Active"
    finally:
        other.close()


def test_restarted_registration_does_not_reuse_the_previous_period(
    session: Session, tmp_path, upgraded_engine, fast_sam,
) -> None:
    uei = _uei()
    settings = _settings(tmp_path)
    original = _record(uei, legal="PERIOD ONE LLC", registration_date="2020-01-15", last_update="2026-01-01")
    first, _ = ensure_vendor(session, uei, client=_client(original), settings=settings, now=NOW)
    first_key = first.registration_key
    assert first.naics_codes
    session.commit()

    def supersede(request: httpx.Request) -> None:
        session.commit()
        other = Session(upgraded_engine)
        try:
            pending = other.get(Vendor, uei)
            restarted = _record(
                uei, legal="PERIOD TWO LLC", registration_date="2026-09-01",
                last_update="2026-09-20", activation="2026-09-01", naics=False, status="Active",
            )
            ensure_vendor(
                other, uei, refresh=True, client=_client(restarted), settings=settings,
                now=NOW + timedelta(minutes=1),
            )
            other.commit()
            assert pending is not None
        finally:
            other.close()

    late = _record(uei, legal="STALE PERIOD ONE", registration_date="2020-01-15", last_update="2026-08-01")
    row, applied = ensure_vendor(
        session, uei, refresh=True, client=_client(late, on_request=supersede),
        settings=settings, now=NOW + timedelta(minutes=2),
    )
    assert applied is False
    assert row.legal_name == "PERIOD TWO LLC"
    assert row.naics_codes is None
    assert row.registration_key != first_key
    assert "2026-09-01" in row.registration_key


def test_null_fields_in_a_new_payload_do_not_keep_old_assertions(session: Session, tmp_path, fast_sam) -> None:
    uei = _uei()
    settings = _settings(tmp_path)
    ensure_vendor(
        session, uei, client=_client(_record(uei, legal="HAS NAICS LLC")), settings=settings, now=NOW,
    )
    session.commit()
    cleared = _record(uei, legal="NO NAICS LLC", last_update="2026-09-15", naics=False, status="Inactive")
    row, applied = ensure_vendor(
        session, uei, refresh=True, client=_client(cleared), settings=settings, now=NOW + timedelta(minutes=5),
    )
    assert applied is True
    assert row.legal_name == "NO NAICS LLC"
    assert row.registration_status == "Inactive"
    assert row.naics_codes is None
    assert registration_key(uei, row.raw) == row.registration_key


def test_company_failed_refresh_drops_assertions_for_jobs_and_later_sessions(
    session: Session, tmp_path, fast_sam, monkeypatch,
) -> None:
    from govcon.company.registration import (
        overlay_registration,
        refresh_company_registration,
    )
    from govcon.scheduler.jobs import step_company_registration

    uei = _uei()
    facts_path = tmp_path / "facts.json"
    facts_path.write_text(json.dumps({
        "uei": uei, "legal_name": "FILE NAME LLC", "sam_registration_status": "Active",
        "sam_expiration_date": "2099-01-01",
    }), encoding="utf-8")
    settings = _settings(tmp_path, company_uei=uei, company_facts_path=facts_path)
    record = _record(uei, legal="COMPANY LLC", expiration="2027-01-15")
    result = refresh_company_registration(
        session, settings=settings, client=_client(record), now=NOW,
    )
    assert result.status == "refreshed"
    assert result.expiration_date.isoformat() == "2027-01-15"
    session.commit()

    failing, calls = _failing_client()
    failed = refresh_company_registration(
        session, settings=settings, client=failing, now=NOW + timedelta(days=1),
    )
    assert failed.status == "failed"
    assert calls["count"] == 5
    company = session.get(CompanyRegistration, uei)
    session.refresh(company)
    assert company.freshness_status == "failed"
    assert company.registration_status is None
    assert company.legal_name is None
    assert company.expiration_date is None
    assert "phase6-test-key" not in (company.last_error or "")
    vendor = _vendor(session, uei)
    assert vendor.freshness_status == "failed"
    assert vendor.registration_status is None
    overlaid = overlay_registration(session, json.loads(facts_path.read_text(encoding="utf-8")), settings=settings, now=NOW + timedelta(days=1))
    assert "sam_registration_status" not in overlaid
    assert "sam_expiration_date" not in overlaid
    assert overlaid["_provenance"]["sam_registration"]["freshness_status"] == "failed"
    assert overlaid["_provenance"]["sam_registration"]["stale"] is True
    session.commit()

    def fail_fetch(client, configured, requested_uei, *, wait=None):
        raise SamEntityError("SAM entity lookup failed with HTTP 503: unavailable")

    monkeypatch.setattr("govcon.ingest.sam_entities.fetch_entity_payload", fail_fetch)
    step = step_company_registration(session, settings)
    assert step.status == "failed"
    other = sessionmaker(bind=session.get_bind(), expire_on_commit=False)()
    try:
        stored = other.get(CompanyRegistration, uei)
        assert stored.registration_status is None
        assert stored.freshness_status == "failed"
    finally:
        other.close()


def test_stale_vendor_signal_is_not_treated_as_current(session: Session, tmp_path) -> None:
    from govcon.decision.signals import Signals, eligibility_signals

    uei = _uei()
    settings = _settings(tmp_path, sam_api_key=None, company_uei=uei)
    session.add(Vendor(
        uei=uei, legal_name="STALE SIGNAL LLC", registration_status="Active",
        fetched_at=NOW - timedelta(days=30), freshness_status="fresh",
        source_updated_at=NOW - timedelta(days=40), expires_at=NOW.date() + timedelta(days=200),
        attempt_state="applied",
    ))
    opp = Opportunity(source="sam", source_id=uuid4().hex, title="stale signal", status="open", raw={})
    session.add(opp)
    session.commit()
    signals = Signals()
    eligibility_signals(
        session, opp, {"uei": uei}, {}, has_summary=False, settings=settings, signals=signals,
    )
    assert signals.values["sam_active"] is None
    assert "not used as current" in signals.provenance["sam_active"]["detail"]
    other = sessionmaker(bind=session.get_bind(), expire_on_commit=False)()
    try:
        stored = other.get(Vendor, uei)
        assert stored.registration_status == "Active"
        assert evidence_status(stored, now=NOW, max_age=timedelta(days=3)) == "stale"
    finally:
        other.close()


def test_failed_vendor_refresh_does_not_revive_sam_active(session: Session, tmp_path, fast_sam) -> None:
    from govcon.decision.signals import Signals, eligibility_signals

    uei = _uei()
    settings = _settings(tmp_path, company_uei=uei)
    session.add(Vendor(
        uei=uei, legal_name="OLD ACTIVE LLC", registration_status="Active",
        fetched_at=NOW - timedelta(days=10), freshness_status="stale",
        naics_codes=[{"code": "423450"}], raw={"entityRegistration": {"registrationStatus": "Active"}},
    ))
    opp = Opportunity(source="sam", source_id=uuid4().hex, title="failed signal", status="open", raw={})
    session.add(opp)
    session.commit()
    _failing, _ = _failing_client()
    signals = Signals()
    # The eligibility path refreshes a stale own-vendor row. Patch the client
    # by failing the payload fetch the refresh actually calls.
    from govcon.ingest import sam_entities

    def fail_fetch(client, settings, uei, *, wait=None):
        raise SamEntityError("SAM entity lookup failed with HTTP 503: unavailable api_key=phase6-test-key")

    original = sam_entities.fetch_entity_payload
    sam_entities.fetch_entity_payload = fail_fetch
    try:
        eligibility_signals(
            session, opp, {"uei": uei}, {}, has_summary=False, settings=settings, signals=signals,
        )
    finally:
        sam_entities.fetch_entity_payload = original
    assert signals.values["sam_active"] is None
    row = _vendor(session, uei)
    assert row.freshness_status == "failed"
    assert row.registration_status is None
    assert row.naics_codes is None
    assert "phase6-test-key" not in (row.last_refresh_error or "")
    session.commit()
    other = sessionmaker(bind=session.get_bind(), expire_on_commit=False)()
    try:
        assert other.get(Vendor, uei).registration_status is None
    finally:
        other.close()


def test_response_older_than_the_stored_source_is_rejected() -> None:
    row = Vendor(uei="ABCDEFGHIJKL", source_updated_at=datetime(2026, 9, 1, tzinfo=UTC), fetched_at=NOW)
    assert response_is_older(
        row, incoming_source_updated_at=datetime(2026, 1, 1, tzinfo=UTC), incoming_fetched_at=NOW + timedelta(days=1),
    )
    assert response_is_older(row, incoming_source_updated_at=None, incoming_fetched_at=NOW + timedelta(days=1))
    assert not response_is_older(
        row, incoming_source_updated_at=datetime(2026, 9, 1, tzinfo=UTC), incoming_fetched_at=NOW,
    )


def test_watchlist_validation_keeps_submitted_values_and_the_saved_row(session: Session) -> None:
    owner, token = _make_user(session, f"wl-{uuid4().hex}@example.test", "owner")
    session.commit()
    existing = Watchlist(name=f"kept-{uuid4().hex[:8]}", enabled=True, min_value=Decimal(10), max_value=Decimal(20))
    session.add(existing)
    session.commit()
    with CsrfTestClient(create_app(), follow_redirects=False) as client:
        client.cookies.set("govcon_session", token)
        created = client.post(
            "/watchlists/new",
            data={"name": "Bad money", "min_value": "12.50", "max_value": "nope", "min_deadline_days": "3.5",
                  "notes": "<script>alert(1)</script>"},
            cookies={"govcon_session": token},
        )
        assert created.status_code == 400
        page = created.text
        assert "nope" in page and "3.5" in page and "12.50" in page
        assert "dollar amount" in page
        assert "whole number" in page
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page
        edited = client.post(
            f"/watchlists/{existing.id}/edit",
            data={"name": existing.name, "min_value": "500", "max_value": "10", "min_deadline_days": "soon"},
            cookies={"govcon_session": token},
        )
    assert edited.status_code == 400
    assert "500" in edited.text and "soon" in edited.text
    session.refresh(existing)
    assert existing.min_value == Decimal(10)
    assert existing.max_value == Decimal(20)
    assert session.scalar(select(Watchlist).where(Watchlist.name == "Bad money")) is None
    assert owner.is_active


def test_missing_prompt_include_is_refused(tmp_path: Path) -> None:
    asset = PromptAsset(
        path=tmp_path / "task.md", name="task_prompt", version="1",
        metadata={"includes": "shared/not_on_disk_v1"}, body="Task body.", content_hash="abc",
    )
    with pytest.raises(PromptRenderError, match="not_on_disk_v1"):
        render_system_prompt(asset, tmp_path)
    with pytest.raises(PromptRegistryDenied, match="not_on_disk_v1"):
        _verify_includes(None, asset, tmp_path)  # type: ignore[arg-type]
    include = tmp_path / "shared"
    include.mkdir()
    (include / "present_v1.md").write_text(
        "---\nname: shared/present_v1\nversion: 1\nstatus: active\n---\nShared rules.\n",
        encoding="utf-8",
    )
    present = PromptAsset(
        path=tmp_path / "task.md", name="task_prompt", version="1",
        metadata={"includes": "shared/present_v1"}, body="Task body.", content_hash="abc",
    )
    rendered = render_system_prompt(present, tmp_path)
    assert rendered.startswith("Shared rules.")
    assert "Task body." in rendered


def test_ranking_reuses_one_award_query_and_one_recommendation_query(session: Session) -> None:
    from govcon.matching.ranking import rank_active_matches

    psc = f"P{uuid4().hex[:6]}"
    for existing in session.scalars(select(Match).where(Match.active.is_(True))):
        existing.active = False
    session.flush()
    opportunities = []
    for _ in range(2):
        opp = Opportunity(
            source="sam", source_id=uuid4().hex, title="shared psc", status="open", psc_code=psc, raw={},
        )
        session.add(opp)
        session.flush()
        watchlist = Watchlist(name=f"rank-{uuid4().hex[:8]}", enabled=True)
        session.add(watchlist)
        session.flush()
        session.add(Match(
            opportunity_id=opp.id, watchlist_id=watchlist.id, status="new", active=True, score=Decimal(1),
            matched_on={"active_groups": ["g"], "passing_groups": ["g"]},
        ))
        opportunities.append(opp)
    session.add(Award(
        award_id=f"rank-{uuid4().hex}", piid="P", description="past", psc_code=psc,
        recipient_uei="AWARDEEUEI01", recipient_name="Awardee", total_obligation=Decimal(100),
        action_date=NOW.date(), raw={},
    ))
    session.commit()
    queries: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany) -> None:
        queries.append(statement)

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", before)
    try:
        ranked = rank_active_matches(session, now=NOW)
    finally:
        event.remove(bind, "before_cursor_execute", before)
    assert ranked >= 2
    award_groups = [query for query in queries if "awards" in query.lower() and "group by" in query.lower()]
    recommendation_reads = [
        query for query in queries
        if "recommendations" in query.lower() and query.lstrip().lower().startswith("select")
    ]
    assert len(award_groups) == 1
    assert len(recommendation_reads) == 1


def test_ai_replay_survives_a_new_recorder_after_a_crash() -> None:
    calls = {"count": 0}

    def perform() -> str:
        calls["count"] += 1
        return "answered"

    def run_pass() -> str:
        recorder = active_recorder()
        assert recorder is not None
        return recorder.call({"prompt": "same"}, perform)

    saved: dict = {}
    result = run_recorded(run_pass, persist=lambda snapshot: saved.update(snapshot=deepcopy(snapshot)))
    assert result == "answered"
    assert calls["count"] == 1
    restored = Recorder.restore(saved["snapshot"])
    assert restored.snapshot()["calls_made"] == 1
    calls["count"] = 0
    again = run_recorded(run_pass, restore=saved["snapshot"])
    assert again == "answered"
    assert calls["count"] == 0


def test_preparation_persists_replay_so_a_restart_does_not_repeat_the_call(session: Session, tmp_path) -> None:
    from govcon.tasks.registry import StepContext
    from govcon.workflow.preparation import _run_service
    from govcon.workflow.source_revision import current_source_revision

    settings = _settings(tmp_path)
    opp = Opportunity(source="sam", source_id=uuid4().hex, title="prep", status="open", raw={})
    session.add(opp)
    session.flush()
    task = Task(
        task_type="opportunity_preparation", status="running", dedup_key=uuid4().hex, payload={},
        input_revision={}, opportunity_id=opp.id, lease_owner="prep-worker", claim_token=1,
        lease_expires_at=datetime.now(UTC) + timedelta(hours=2),
    )
    session.add(task)
    session.commit()
    ctx = StepContext(
        settings=settings, task_id=task.id, opportunity_id=opp.id, payload={
            "_preparation_claim": {
                "task_id": task.id, "task_type": task.task_type, "opportunity_id": opp.id,
                "token": 1, "worker_id": "prep-worker",
            },
            "_preparation_source_revision": str(current_source_revision(session, opp.id)),
        }, input_revision={},
    )
    calls = {"count": 0}

    def service(db: Session) -> dict:
        recorder = active_recorder()
        assert recorder is not None
        value = recorder.call({"q": "prep"}, lambda: calls.__setitem__("count", calls["count"] + 1) or "ok")
        return {"value": value}

    output = _run_service(ctx, "summary", service)
    assert output["value"] == "ok"
    assert calls["count"] == 1
    session.refresh(task)
    assert task.checkpoint["ai_replay"]["summary"]["calls_made"] == 1
    calls["count"] = 0
    second = _run_service(ctx, "summary", service)
    assert second["value"] == "ok"
    assert calls["count"] == 0


def test_health_reports_database_connectivity_without_leaking_errors(session: Session, monkeypatch) -> None:
    with CsrfTestClient(create_app()) as client:
        healthy = client.get("/health")
    assert healthy.status_code == 200
    assert healthy.json() == {"status": "ok"}

    def explode(*args, **kwargs):
        raise RuntimeError("password=super-secret-db-password")

    monkeypatch.setattr("govcon.db.session_scope", explode)
    with CsrfTestClient(create_app()) as client:
        down = client.get("/health")
    assert down.status_code == 503
    assert down.json() == {"status": "unavailable"}
    assert "super-secret-db-password" not in down.text


def test_public_bind_requires_a_stable_csrf_secret() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="WEB_CSRF_SECRET"):
        Settings(_env_file=None, web_bind_host="0.0.0.0", web_bind_allow_public=True, database_url="postgresql+psycopg://govcon:govcon@localhost/govcon")
    accepted = Settings(
        _env_file=None, web_bind_host="0.0.0.0", web_bind_allow_public=True,
        web_csrf_secret="x" * 32, database_url="postgresql+psycopg://govcon:govcon@localhost/govcon",
    )
    assert accepted.web_bind_allow_public is True
