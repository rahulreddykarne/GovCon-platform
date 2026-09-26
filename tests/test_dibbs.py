"""Phase 4 DIBBS index ingestion.

The fixture is the live index file in260925.txt downloaded on 2026-09-26
from https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive/in260925.txt after the
DoD consent banner. That was the newest file on the recent RFQ page. DIBBS
posts a day's file on the following day, and 2026-09-26 had no in260926.txt.
Normal tests do not call the network.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from govcon.cli import app
from govcon.config import Settings
from govcon.ingest.dibbs import (
    ARCHIVE_ROOT,
    DibbsError,
    fetch_consented,
    index_file_url,
    index_links,
    ingest_index_bytes,
    ingest_index_file,
    parse_index,
    pull_dibbs_index,
)
from govcon.ingest.snapshots import canonical_content_hash, upsert_opportunity
from govcon.matching.engine import run_matching
from govcon.matching.watchlists import create_watchlist
from govcon.models import Contact, Match, Opportunity, OpportunityEvent, OpportunitySnapshot

runner = CliRunner()
FIXTURE = Path(__file__).parent / "fixtures" / "dibbs" / "in260925.txt"
FIRST_SOLICITATION = "SPE1C126T1698"
FIRST_NSN = "8415-01-392-3228"
FIRST_SOURCE_ID = "SPE1C126T1698:7018200348"
NOW = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)

WARNING_HTML = """
<html><title>Department of Defense (DoD) Warning and Consent</title>
<body>
<p>Department of Defense (DoD) Notice and Consent Banner</p>
<form method="post" action="dodwarning.aspx?goto=%2fdownloads%2frfq%2farchive%2fin260925.txt">
<input type="hidden" name="__VIEWSTATE" value="abc" />
<input type="submit" name="butAgree" value="OK" />
</form>
</body></html>
"""

LISTING_HTML = """
<table>
<tr>
<td><a href='https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive/ca260925.zip'>ca260925.zip</a></td>
<td><a href='https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive/in260925.txt'>in260925.txt</a></td>
<td><a href='https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive/bq260925.zip'>bq260925.zip</a></td>
</tr>
<tr>
<td><a href='https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive/in260924.txt'>in260924.txt</a></td>
</tr>
</table>
"""


@pytest.fixture
def session(upgraded_engine) -> Session:
    factory = sessionmaker(bind=upgraded_engine, autoflush=False, expire_on_commit=False)
    db = factory()
    _purge_dibbs(db)
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _purge_dibbs(db: Session) -> None:
    db.rollback()
    opportunity_ids = select(Opportunity.id).where(Opportunity.source == "dibbs")
    db.execute(delete(Match).where(Match.opportunity_id.in_(opportunity_ids)))
    db.execute(delete(Contact).where(Contact.first_seen_opportunity_id.in_(opportunity_ids)))
    db.execute(delete(OpportunityEvent).where(OpportunityEvent.opportunity_id.in_(opportunity_ids)))
    db.execute(delete(OpportunitySnapshot).where(OpportunitySnapshot.opportunity_id.in_(opportunity_ids)))
    db.execute(delete(Opportunity).where(Opportunity.source == "dibbs"))
    db.commit()


def _opportunity(db: Session, source_id: str) -> Opportunity:
    row = db.scalar(select(Opportunity).where(Opportunity.source == "dibbs", Opportunity.source_id == source_id))
    assert row is not None
    return row


def _snapshot_count(db: Session, opportunity_id: int) -> int:
    count = db.scalar(
        select(func.count()).select_from(OpportunitySnapshot).where(OpportunitySnapshot.opportunity_id == opportunity_id)
    )
    return int(count or 0)


def test_fixture_is_the_live_daily_index() -> None:
    payload = FIXTURE.read_bytes()
    assert payload.startswith(b"SPE1C126T1698")
    assert b"BAG,FLYER'S HELMET" in payload
    assert payload.count(b"\n") == 523
    text = payload.decode("latin-1")
    assert all(len(line) == 140 for line in text.splitlines())


def test_index_parses_solicitation_nsn_quantity_and_buyer() -> None:
    items, errors = parse_index(FIXTURE.read_text(encoding="latin-1"), index_name="in260925.txt")
    assert errors == []
    assert len(items) == 523
    first = items[0]
    assert first.source == "dibbs"
    assert first.source_id == FIRST_SOURCE_ID
    assert first.solicitation_number == FIRST_SOLICITATION
    assert first.nsn == FIRST_NSN
    assert first.nsn_candidates == (FIRST_NSN,)
    assert first.psc_code == "8415"
    assert first.quantity == Decimal("200")
    assert first.unit == "EA"
    assert first.title == "BAG,FLYER'S HELMET"
    assert first.set_aside_code == "N"
    assert first.response_deadline == datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)
    assert first.posted_date == date(2026, 9, 25)
    assert first.status == "open"
    assert first.agency_path == "Defense Logistics Agency"
    assert first.poc == [{"contact_type": "buyer", "buyer_code": "DSC01", "amsc": "D"}]
    assert first.links["ui"] == f"https://www.dibbs.bsm.dla.mil/RFQ/RfqRec.aspx?sn={FIRST_SOLICITATION}"
    assert first.links["index"] == f"{ARCHIVE_ROOT}/in260925.txt"
    assert first.links["package"].endswith("/ca260925.zip")
    assert first.links["batch_quote"].endswith("/bq260925.zip")
    assert first.raw["record"].startswith(FIRST_SOLICITATION)
    assert len(first.raw["record"]) == 140
    local = next(item for item in items if item.solicitation_number == "SPEFA526Q0078")
    assert local.nsn is None
    assert local.quantity == Decimal("24")
    assert local.unit == "EA"
    assert sum(1 for item in items if item.nsn) == 521
    assert sum(1 for item in items if item.quantity is not None) == 523


def test_short_line_is_reported_and_skipped() -> None:
    items, errors = parse_index("too-short\n", index_name="in260925.txt")
    assert items == []
    assert errors == ["line 1: expected 140 characters, got 9"]


def test_index_links_keep_newest_file_first() -> None:
    links = index_links(LISTING_HTML + LISTING_HTML.lower())
    assert links == [
        "https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive/in260925.txt",
        "https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive/in260924.txt",
    ]
    assert index_file_url(date(2026, 9, 25)).endswith("/in260925.txt")


def test_consent_post_returns_the_index_bytes() -> None:
    payload = FIXTURE.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=WARNING_HTML)
        assert request.method == "POST"
        assert b"butAgree=OK" in request.content
        assert b"__VIEWSTATE=abc" in request.content
        return httpx.Response(200, content=payload, headers={"content-type": "text/plain; charset=ISO-8859-1"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    response = fetch_consented(client, index_file_url(date(2026, 9, 25)), interval=0)
    assert response.content == payload


def test_missing_index_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><h1>File was not found</h1></html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(DibbsError, match="not available"):
        pull_dibbs_index(
            None,  # type: ignore[arg-type]
            settings=Settings(data_dir=Path("/tmp")),
            posted_date=date(2026, 9, 25),
            client=client,
            interval=0,
        )


def test_todays_fixture_ingests_and_logs_coverage(session: Session, caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    caplog.set_level(logging.INFO, logger="govcon.ingest.dibbs")
    result = ingest_index_file(session, FIXTURE, data_dir=tmp_path)
    assert result.stats.fetched == 523
    assert result.stats.inserted == 523
    assert result.stats.updated == 0
    assert result.stats.errors == []
    assert result.coverage.records == 523
    assert result.coverage.nsn == 521
    assert result.coverage.quantity == 523
    assert "dibbs_coverage index=in260925.txt records=523 nsn=521 quantity=523" in caplog.text
    assert "nsn_coverage=0.9962" in caplog.text
    assert "quantity_coverage=1.0000" in caplog.text
    retained = Path(result.retained_path or "")
    assert retained.read_bytes() == FIXTURE.read_bytes()
    row = _opportunity(session, FIRST_SOURCE_ID)
    assert row.raw["solicitation_number"] == FIRST_SOLICITATION
    assert row.raw_hash
    snapshot = session.scalar(select(OpportunitySnapshot).where(OpportunitySnapshot.opportunity_id == row.id))
    assert snapshot is not None
    assert snapshot.raw["record"] == row.raw["record"]
    assert snapshot.normalized["nsn"] == FIRST_NSN
    assert snapshot.normalized["quantity"] == "200"
    contacts = session.scalars(select(Contact).where(Contact.first_seen_opportunity_id == row.id)).all()
    assert contacts == []


def test_rerun_is_idempotent(session: Session, tmp_path: Path) -> None:
    first = ingest_index_file(session, FIXTURE, data_dir=tmp_path)
    row = _opportunity(session, FIRST_SOURCE_ID)
    snapshots = _snapshot_count(session, row.id)
    second = ingest_index_file(session, FIXTURE, data_dir=tmp_path)
    assert first.stats.inserted == 523
    assert second.stats.inserted == 0
    assert second.stats.updated == 0
    assert second.stats.unchanged == 523
    assert _snapshot_count(session, row.id) == snapshots
    assert session.scalar(select(func.count()).select_from(Opportunity).where(Opportunity.source == "dibbs")) == 523


def test_amendment_writes_snapshot_and_field_events(session: Session) -> None:
    line = FIXTURE.read_text(encoding="latin-1").splitlines()[0]
    amended = line[:72] + "10/15/26" + line[80:]
    amended = amended[:99] + "0000201" + amended[106:]
    assert len(amended) == 140
    original, errors = parse_index(line + "\n", index_name="in260925.txt")
    assert errors == []
    changed, changed_errors = parse_index(amended + "\n", index_name="in260925.txt")
    assert changed_errors == []
    assert upsert_opportunity(session, original[0]) == "inserted"
    assert upsert_opportunity(session, changed[0]) == "updated"
    row = _opportunity(session, FIRST_SOURCE_ID)
    assert row.quantity == Decimal("201")
    assert row.response_deadline == datetime(2026, 10, 15, 23, 59, 59, tzinfo=UTC)
    assert _snapshot_count(session, row.id) == 2
    events = session.scalars(select(OpportunityEvent).where(OpportunityEvent.opportunity_id == row.id)).all()
    event_types = {event.event_type for event in events}
    assert "quantity_changed" in event_types
    assert "deadline_changed" in event_types
    assert all(event.snapshot_id is not None for event in events if event.event_type != "created")


def test_cancellation_uses_upsert_snapshot(session: Session) -> None:
    items, errors = parse_index(FIXTURE.read_text(encoding="latin-1").splitlines()[0] + "\n", index_name="in260925.txt")
    assert errors == []
    item = items[0]
    assert upsert_opportunity(session, item) == "inserted"
    cancelled_raw = dict(item.raw)
    cancelled_raw["status"] = "cancelled"
    cancelled = replace(
        item,
        status="cancelled",
        raw=cancelled_raw,
        content_hash=canonical_content_hash(cancelled_raw),
    )
    assert upsert_opportunity(session, cancelled) == "updated"
    row = _opportunity(session, FIRST_SOURCE_ID)
    assert row.status == "cancelled"
    events = session.scalars(select(OpportunityEvent).where(OpportunityEvent.opportunity_id == row.id)).all()
    assert "cancelled" in {event.event_type for event in events}
    assert _snapshot_count(session, row.id) == 2


def test_dibbs_rows_use_the_same_watchlist_engine(session: Session, tmp_path: Path) -> None:
    ingest_index_file(session, FIXTURE, data_dir=tmp_path)
    dibbs_row = _opportunity(session, FIRST_SOURCE_ID)
    session.add(
        Opportunity(
            source="sam",
            source_id="sam-same-nsn",
            nsn=FIRST_NSN,
            title="SAM helmet",
            status="open",
            raw={"fixture": True},
        )
    )
    session.flush()
    dibbs_only = create_watchlist(
        session,
        name="dibbs helmet nsn",
        sources=["dibbs"],
        nsn_list=[FIRST_NSN],
    )
    stats = run_matching(session, watchlist_id=dibbs_only.id, now=NOW)
    assert stats.matched == 1
    match = session.scalar(select(Match).where(Match.watchlist_id == dibbs_only.id))
    assert match is not None
    assert match.opportunity_id == dibbs_row.id

    both = create_watchlist(
        session,
        name="sam and dibbs nsn",
        sources=["sam", "dibbs"],
        nsn_list=[FIRST_NSN],
    )
    both_stats = run_matching(session, watchlist_id=both.id, now=NOW)
    assert both_stats.matched == 2

    set_aside = create_watchlist(session, name="dibbs small business", sources=["dibbs"], set_asides=["Y"])
    set_aside_stats = run_matching(session, watchlist_id=set_aside.id, now=NOW)
    assert set_aside_stats.matched == 106

    keyword = create_watchlist(session, name="dibbs helmet word", sources=["dibbs"], keywords=["helmet"])
    keyword_stats = run_matching(session, watchlist_id=keyword.id, now=NOW)
    assert keyword_stats.matched >= 1
    keyword_match = session.scalar(
        select(Match).where(Match.watchlist_id == keyword.id, Match.opportunity_id == dibbs_row.id)
    )
    assert keyword_match is not None


def test_pull_uses_the_newest_index_and_not_the_zip(session: Session, tmp_path: Path) -> None:
    payload = FIXTURE.read_bytes()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "RFQDates.aspx" in str(request.url):
            return httpx.Response(200, text=LISTING_HTML)
        if str(request.url).endswith("/in260925.txt"):
            return httpx.Response(200, content=payload)
        raise AssertionError(f"unexpected URL {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = pull_dibbs_index(
        session,
        settings=Settings(data_dir=tmp_path, dibbs_request_interval_seconds=0),
        client=client,
        interval=0,
    )
    assert seen == [
        "https://www.dibbs.bsm.dla.mil/RFQ/RFQDates.aspx?category=recent",
        "https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive/in260925.txt",
    ]
    assert result.stats.inserted == 523
    assert not any("ca260925" in url or "bq260925" in url for url in seen)


def test_cli_file_ingest_is_idempotent(session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    first = runner.invoke(app, ["ingest", "dibbs", "--file", str(FIXTURE)])
    assert first.exit_code == 0, first.output
    assert "records: 523" in first.output
    assert "nsn_coverage: 521/523" in first.output
    assert "quantity_coverage: 523/523" in first.output
    assert "inserted: 523" in first.output
    assert "unchanged: 0" in first.output
    second = runner.invoke(app, ["ingest", "dibbs", "--file", str(FIXTURE)])
    assert second.exit_code == 0, second.output
    assert "inserted: 0" in second.output
    assert "unchanged: 523" in second.output
    stored = session.scalar(select(func.count()).select_from(Opportunity).where(Opportunity.source == "dibbs"))
    assert stored == 523
    rejected = runner.invoke(app, ["ingest", "dibbs", "--file", str(FIXTURE), "--date", "2026-09-25"])
    assert rejected.exit_code == 2
    help_text = runner.invoke(app, ["ingest", "dibbs", "--help"])
    assert help_text.exit_code == 0
    # CI terminals can force Rich styling; compare the help text without SGR codes.
    plain_help = re.sub(r"\x1b\[[0-9;]*m", "", help_text.output)
    assert "--file" in plain_help
    assert "--date" in plain_help
    _purge_dibbs(session)


def test_ingest_bytes_counts_parse_errors(session: Session) -> None:
    payload = b"short\n" + FIXTURE.read_text(encoding="latin-1").splitlines()[0].encode("latin-1") + b"\n"
    result = ingest_index_bytes(session, payload, index_name="in260925.txt")
    assert result.stats.fetched == 1
    assert result.stats.inserted == 1
    assert len(result.stats.errors) == 1
