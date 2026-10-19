"""SAM 429 handling: Retry-After forms, a shared request budget, one deferred retry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from govcon.config import Settings
from govcon.enrich.attachments import download_attachments
from govcon.enrich.safe_fetch import (
    MAX_RETRY_AFTER_SECONDS,
    FetchRateLimited,
    retry_after_seconds,
    safe_fetch,
)
from govcon.ingest.sam_quota import begin_sam_budget, current_sam_budget, is_sam_api_url
from govcon.models import Opportunity, Task
from govcon.scheduler.jobs import step_source_documents

PUBLIC_IP = "93.184.216.34"


def _opp(session, *, links: dict | None = None, source: str = "sam") -> Opportunity:
    row = Opportunity(
        source=source,
        source_id=f"sam429-{uuid4().hex}",
        title="SAM 429 fixture",
        status="open",
        raw={},
        links=links or {},
    )
    session.add(row)
    session.flush()
    return row


def test_retry_after_numeric_and_http_date() -> None:
    numeric = httpx.Response(429, headers={"Retry-After": "12"})
    assert retry_after_seconds(numeric) == 12.0
    past = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    assert retry_after_seconds(past) == 0.0
    future = datetime.now(UTC) + timedelta(seconds=8)
    header = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    dated = httpx.Response(429, headers={"Retry-After": header})
    delay = retry_after_seconds(dated)
    assert delay is not None and 0 <= delay <= 8 + 1
    capped = httpx.Response(429, headers={"Retry-After": "9999"})
    assert retry_after_seconds(capped) == MAX_RETRY_AFTER_SECONDS
    missing = httpx.Response(429)
    assert retry_after_seconds(missing) is None


def test_safe_fetch_http_date_retry_after_then_rate_limited(monkeypatch) -> None:
    monkeypatch.setattr("govcon.enrich.safe_fetch.time.sleep", lambda _s: None)
    future = datetime.now(UTC) + timedelta(seconds=4)
    header = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": header})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(FetchRateLimited) as caught:
        safe_fetch(
            client,
            "https://api.sam.gov/prod/opportunities/v2/noticedesc",
            max_bytes=1000,
            resolver=lambda _host: [PUBLIC_IP],
            attempts=2,
        )
    assert calls["n"] == 2
    assert caught.value.retry_after is not None
    assert 0 <= caught.value.retry_after <= MAX_RETRY_AFTER_SECONDS


def test_safe_fetch_without_retry_after_uses_capped_backoff(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("govcon.enrich.safe_fetch.time.sleep", lambda s: slept.append(s))
    monkeypatch.setattr("govcon.enrich.safe_fetch.random.random", lambda: 0.0)  # jitter 0.5x
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(FetchRateLimited) as caught:
        safe_fetch(
            client,
            "https://example.test/file.pdf",
            max_bytes=1000,
            resolver=lambda _host: [PUBLIC_IP],
            attempts=3,
            backoff_seconds=0.4,
        )
    assert calls["n"] == 3
    assert len(slept) == 2
    assert all(0 <= pause <= 60 for pause in slept)
    assert caught.value.retry_after is None


def test_sam_budget_stops_further_sam_calls_and_keeps_non_sam(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("govcon.enrich.safe_fetch.time.sleep", lambda _s: None)
    begin_sam_budget(80)
    sam_calls: list[str] = []
    other_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if host == "api.sam.gov":
            sam_calls.append(str(request.url))
            return httpx.Response(429, headers={"Retry-After": "5"})
        other_calls.append(str(request.url))
        return httpx.Response(200, content=b"plain text body", headers={"content-type": "text/plain"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = Settings(data_dir=tmp_path, sam_api_key="sam-test-key-123456")
    first = _opp(
        db,
        links={
            "description": "https://api.sam.gov/prod/opportunities/v2/noticedesc?noticeid=one",
            "attachments": ["https://api.sam.gov/prod/opportunities/v3/resources/files/a1/download"],
        },
    )
    second = _opp(
        db,
        links={
            "description": "https://api.sam.gov/prod/opportunities/v2/noticedesc?noticeid=two",
            "attachments": ["https://files.example.test/rfq.txt"],
        },
    )
    files1 = download_attachments(
        db, first, settings=settings, client=client, resolver=lambda _h: [PUBLIC_IP],
    )
    files2 = download_attachments(
        db, second, settings=settings, client=client, resolver=lambda _h: [PUBLIC_IP],
    )
    budget = current_sam_budget()
    assert budget is not None and budget.rate_limited
    # First SAM URL retries, later SAM URLs are skipped entirely.
    assert 1 <= len(sam_calls) <= 3
    assert other_calls and all("files.example.test" in url for url in other_calls)
    assert all(row.extraction_status == "download_failed" for row in files1)
    stored = [row for row in files2 if row.sha256]
    failed_sam = [row for row in files2 if row.extraction_status == "download_failed"]
    assert stored and failed_sam
    assert "next_retry=" in (failed_sam[0].extraction_error or "")
    tasks = list(db.scalars(select(Task).where(Task.task_type == "attachment_download")))
    assert len(tasks) == 1
    assert set(tasks[0].payload.get("opportunity_ids") or []) == {first.id, second.id}
    assert tasks[0].opportunity_id is None
    assert tasks[0].next_attempt_at is not None


def test_source_documents_counts_stored_files_and_queues_one_retry(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("govcon.enrich.safe_fetch.time.sleep", lambda _s: None)
    monkeypatch.setattr("govcon.enrich.safe_fetch.system_resolver", lambda _h: [PUBLIC_IP])
    begin_sam_budget(80)
    settings = Settings(_env_file=None, data_dir=tmp_path, sam_api_key="sam-test-key-123456")
    opps = [
        _opp(db, links={"description": f"https://api.sam.gov/prod/opportunities/v2/noticedesc?noticeid={uuid4().hex}"})
        for _ in range(3)
    ]
    db.flush()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "govcon.enrich.attachments.build_client",
        lambda *a, **k: client,
    )
    monkeypatch.setattr(
        "govcon.scheduler.jobs._source_document_page",
        lambda session, after, upper: [row for row in opps if after < row.id <= upper],
    )
    result = step_source_documents(db, settings)
    assert result.status == "completed_with_errors"
    assert result.inserted == 0
    assert result.extra["stored"] == 0
    tasks = list(db.scalars(select(Task).where(Task.task_type == "attachment_download")))
    assert len(tasks) == 1
    queued = set(tasks[0].payload.get("opportunity_ids") or [])
    assert {opp.id for opp in opps} <= queued


def test_sam_ingest_persists_pages_before_a_later_429(db, monkeypatch) -> None:
    from datetime import date

    from govcon.ingest.sam_opportunities import pull_sam_opportunities
    from govcon.models import Opportunity as Opp

    begin_sam_budget(80)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "totalRecords": 2,
                    "opportunitiesData": [
                        {"noticeId": "kept-after-429", "title": "Kept", "active": "Yes"},
                    ],
                },
            )
        return httpx.Response(429, headers={"Retry-After": "1"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = pull_sam_opportunities(
        db,
        api_key="test-key",
        posted_from=date(2026, 9, 24),
        posted_to=date(2026, 9, 26),
        limit=1,
        client=client,
        attempts=1,
        settings=Settings(_env_file=None),
    )
    assert stats.inserted == 1
    assert any("rate limited" in err.lower() for err in stats.errors)
    assert db.scalar(select(Opp).where(Opp.source_id == "kept-after-429")) is not None
    assert is_sam_api_url("https://api.sam.gov/opportunities/v2/search")


def test_start_govcon_script_starts_portable_postgres_on_5433() -> None:
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "windows" / "Start-GovCon.ps1"
    text = script.read_text(encoding="utf-8")
    assert "[string]$PgRoot" in text
    assert "pg_ctl" in text
    assert "-t 600" in text
    assert "SELECT 1" in text
    assert "wait_postgres.py" in text
    assert "5432" in text
    assert "never starts or touches a Postgres service on port 5432" in text
    assert "PostgresPort = 5433" in text
