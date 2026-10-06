"""Pacific schedules stay on the same local hour across daylight saving."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.util import undefined

from govcon.scheduler.schedule import (
    DAILY_MISFIRE_GRACE_SECONDS,
    JOBS,
    SCHEDULE_VERSION,
    cron_trigger,
    preserved_next_run,
)

LA = ZoneInfo("America/Los_Angeles")


def test_daily_jobs_keep_the_same_local_hour_in_summer_and_winter() -> None:
    summer_now = datetime(2026, 7, 1, 12, tzinfo=LA)
    winter_now = datetime(2026, 1, 2, 12, tzinfo=LA)
    expected = {
        "morning_ingest": 6,
        "usaspending": 7,
        "embeddings": 8,
        "midday_check": 12,
        "evening_ingest": 17,
    }
    for job_id, hour in expected.items():
        trigger = cron_trigger(job_id)
        summer = trigger.get_next_fire_time(None, summer_now)
        winter = trigger.get_next_fire_time(None, winter_now)
        assert summer is not None and winter is not None
        assert summer.astimezone(LA).hour == hour
        assert winter.astimezone(LA).hour == hour
        assert summer.utcoffset() != winter.utcoffset()


def test_sunday_sweep_is_sunday_at_9am_pacific() -> None:
    trigger = cron_trigger("sunday_sweep")
    nxt = trigger.get_next_fire_time(None, datetime(2026, 1, 5, 12, tzinfo=LA))  # a Monday
    assert nxt is not None
    local = nxt.astimezone(LA)
    assert local.weekday() == 6
    assert local.hour == 9 and local.minute == 0


def test_misfire_grace_covers_an_overnight_laptop_restart() -> None:
    assert DAILY_MISFIRE_GRACE_SECONDS >= 18 * 60 * 60
    assert JOBS["sunday_sweep"]["grace"] >= 36 * 60 * 60


def test_old_utc_due_time_is_not_preserved() -> None:
    old = datetime(2026, 10, 6, 6, 30, tzinfo=ZoneInfo("UTC"))
    assert preserved_next_run({"schedule_version": "utc-v0"}, old) is undefined
    assert preserved_next_run(None, old) is undefined


def test_current_schedule_keeps_a_missed_due_time() -> None:
    missed = datetime(2026, 10, 6, 6, 30, tzinfo=ZoneInfo("UTC"))
    assert preserved_next_run({"schedule_version": SCHEDULE_VERSION}, missed) == missed
