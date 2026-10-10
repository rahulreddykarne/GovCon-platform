"""Render stored UTC timestamps in America/Los_Angeles, labeled PT."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


def as_pt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(PACIFIC)


def format_pt(value: datetime | None, *, seconds: bool = False) -> str:
    """``2026-10-10 14:05 PT`` (or with seconds). Empty when the instant is missing."""
    local = as_pt(value)
    if local is None:
        return ""
    pattern = "%Y-%m-%d %H:%M:%S PT" if seconds else "%Y-%m-%d %H:%M PT"
    return local.strftime(pattern)
