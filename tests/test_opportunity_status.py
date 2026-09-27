"""Bid eligibility must follow the notice type and current deadline."""

from datetime import UTC, date, datetime, timedelta

from govcon.ingest.status import opportunity_status


NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)


def _classify(source="sam", notice_type="Solicitation", deadline=None, archive=None, active="Yes"):
    return opportunity_status(
        source=source, notice_type=notice_type, active=active,
        response_deadline=deadline, archive_date=archive, now=NOW,
    )


def test_award_notice_is_never_open_even_when_active() -> None:
    assert _classify(notice_type="Award Notice", archive=date(2020, 1, 1)) == "awarded"


def test_informational_notice_is_not_a_bid() -> None:
    assert _classify(notice_type="Sources Sought") == "informational"


def test_expired_sam_and_dibbs_deadlines_close() -> None:
    past = NOW - timedelta(seconds=1)
    assert _classify(deadline=past) == "closed"
    assert _classify(source="dibbs", notice_type="RFQ", deadline=past) == "closed"


def test_future_bid_stays_open_and_cancelled_notice_does_not() -> None:
    assert _classify(deadline=NOW + timedelta(days=2)) == "open"
    assert _classify(notice_type="Cancelled Solicitation") == "cancelled"
