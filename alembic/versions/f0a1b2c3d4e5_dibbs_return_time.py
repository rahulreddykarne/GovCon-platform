"""DIBBS response deadlines: 3:00 PM Eastern on the business-day return date.

DIBBS rows were stored with a 23:59:59 UTC deadline. Quotes are actually due
at 3:00 PM Eastern (EST or EDT) on the return date, moved to the next business
day when that date is a weekend or federal holiday. This recomputes
``response_deadline`` for existing DIBBS rows from the stored ``raw.return_by``.
Rows whose deadline was already changed are recomputed the same way; content
hashes and snapshots are untouched.

The business-day rule is copied here (not imported from ``govcon``) so the
migration keeps its meaning if application code changes later.

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-10-02
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op

revision = "f0a1b2c3d4e5"
down_revision = "e9f0a1b2c3d4"
branch_labels = None
depends_on = None

_EASTERN = ZoneInfo("America/New_York")


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    following = date(year + (month == 12), month % 12 + 1, 1)
    last = following - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _holidays(year: int) -> set[date]:
    fixed = [date(year, 1, 1), date(year, 6, 19), date(year, 7, 4), date(year, 11, 11), date(year, 12, 25)]
    days = {_observed(day) for day in fixed}
    days |= {
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _nth_weekday(year, 5, 0, -1),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 10, 0, 2),
        _nth_weekday(year, 11, 3, 4),
    }
    days.add(_observed(date(year + 1, 1, 1)))
    return days


def _deadline(text: str) -> datetime | None:
    try:
        day = datetime.strptime(text.strip(), "%m/%d/%y").date()
    except ValueError:
        return None
    while day.weekday() >= 5 or day in _holidays(day.year):
        day += timedelta(days=1)
    return datetime.combine(day, time(15, 0), tzinfo=_EASTERN).astimezone(timezone.utc)


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, raw->>'return_by' AS return_by FROM opportunities "
            "WHERE source = 'dibbs' AND coalesce(raw->>'return_by', '') <> ''"
        )
    ).all()
    update = sa.text("UPDATE opportunities SET response_deadline = :deadline WHERE id = :id")
    for row in rows:
        deadline = _deadline(row.return_by)
        if deadline is not None:
            bind.execute(update, {"deadline": deadline, "id": row.id})


def downgrade() -> None:
    op.execute(
        """
        UPDATE opportunities
        SET response_deadline = (to_date(raw->>'return_by', 'MM/DD/YY') + time '23:59:59') AT TIME ZONE 'UTC'
        WHERE source = 'dibbs' AND coalesce(raw->>'return_by', '') ~ '^[0-9]{2}/[0-9]{2}/[0-9]{2}$'
        """
    )
