"""Operator clocks for the six scheduled chains.

Times are America/Los_Angeles wall clocks. APScheduler applies Pacific
Daylight Time and Pacific Standard Time, so the same local hour stays put
when daylight saving starts or ends.
"""

from __future__ import annotations

from typing import Any

from apscheduler.triggers.cron import CronTrigger
from apscheduler.util import undefined

OPERATOR_TZ_NAME = "America/Los_Angeles"
# Bump when the default clocks change so a stored due time from the previous
# version is dropped and the new trigger takes over.
SCHEDULE_VERSION = "la-operator-v2"

# A laptop that slept through the night should still run the missed chain once.
DAILY_MISFIRE_GRACE_SECONDS = 18 * 60 * 60
WEEKLY_MISFIRE_GRACE_SECONDS = 36 * 60 * 60

# hour and minute are local. Sunday is the only weekly job.
# Embeddings sit after the morning ingest so the new notices exist first.
JOBS: dict[str, dict[str, Any]] = {
    "morning_ingest": {
        "hour": 6, "minute": 30,
        "cron": "6:30 AM America/Los_Angeles",
        "grace": DAILY_MISFIRE_GRACE_SECONDS,
    },
    "usaspending": {
        "hour": 7, "minute": 0,
        "cron": "7:00 AM America/Los_Angeles",
        "grace": DAILY_MISFIRE_GRACE_SECONDS,
    },
    "embeddings": {
        "hour": 8, "minute": 0,
        "cron": "8:00 AM America/Los_Angeles",
        "grace": DAILY_MISFIRE_GRACE_SECONDS,
    },
    "midday_check": {
        "hour": 12, "minute": 0,
        "cron": "12:00 PM America/Los_Angeles",
        "grace": DAILY_MISFIRE_GRACE_SECONDS,
    },
    "evening_ingest": {
        "hour": 17, "minute": 0,
        "cron": "5:00 PM America/Los_Angeles",
        "grace": DAILY_MISFIRE_GRACE_SECONDS,
    },
    "sunday_sweep": {
        "hour": 9, "minute": 0, "day_of_week": "sun",
        "cron": "Sunday 9:00 AM America/Los_Angeles",
        "grace": WEEKLY_MISFIRE_GRACE_SECONDS,
    },
}


def clock_label(hour: int, minute: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {suffix}"


def _with_cron(spec: dict[str, Any], *, sunday: bool) -> dict[str, Any]:
    label = clock_label(int(spec["hour"]), int(spec["minute"]))
    prefix = "Sunday " if sunday else ""
    copied = dict(spec)
    copied["cron"] = f"{prefix}{label} America/Los_Angeles"
    return copied


def effective_jobs(overrides: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Defaults, with owner hour/minute overrides applied when they are present."""
    jobs: dict[str, dict[str, Any]] = {}
    saved = overrides or {}
    for name, spec in JOBS.items():
        incoming = saved.get(name) if isinstance(saved.get(name), dict) else None
        merged = dict(spec)
        if incoming and "hour" in incoming and "minute" in incoming:
            merged["hour"] = int(incoming["hour"])
            merged["minute"] = int(incoming["minute"])
        jobs[name] = _with_cron(merged, sunday="day_of_week" in spec)
    return jobs


def describe(job_id: str, jobs: dict[str, dict[str, Any]] | None = None) -> str:
    table = jobs or JOBS
    return str(table[job_id]["cron"])


def cron_trigger(job_id: str, jobs: dict[str, dict[str, Any]] | None = None) -> CronTrigger:
    spec = (jobs or JOBS)[job_id]
    kwargs: dict[str, Any] = {
        "hour": spec["hour"],
        "minute": spec["minute"],
        "timezone": OPERATOR_TZ_NAME,
    }
    if "day_of_week" in spec:
        kwargs["day_of_week"] = spec["day_of_week"]
    return CronTrigger(**kwargs)


def preserved_next_run(job_kwargs: dict[str, Any] | None, next_run_time: object) -> object:
    """Keep a stored due time only when it was computed from this schedule.

    A due time left over from the old UTC clocks is dropped so the new
    Pacific trigger can take over. A due time that is already in the past
    is kept: the misfire grace window then runs that missed job once.
    """
    if not job_kwargs or job_kwargs.get("schedule_version") != SCHEDULE_VERSION:
        return undefined
    if next_run_time is None:
        return undefined
    return next_run_time
