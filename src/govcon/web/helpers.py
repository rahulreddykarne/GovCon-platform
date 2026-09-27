"""Shared template context helpers for Phase 14 web UI."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal


def deadline_info(deadline: datetime | None) -> tuple[str | None, str]:
    """Return (label, css_class) for an opportunity deadline."""
    if deadline is None:
        return None, ""
    now = datetime.now(UTC)
    # normalise timezone
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    delta = deadline - now
    days = delta.total_seconds() / 86400
    if days < 0:
        return f"Closed {abs(int(days))}d ago", "deadline-urgent"
    if days < 3:
        return f"⚠ {int(days)}d left", "deadline-urgent"
    if days < 7:
        return f"{int(days)}d left", "deadline-warning"
    if days < 30:
        return f"{int(days)}d left", "deadline-ok"
    return deadline.strftime("%Y-%m-%d"), ""


def format_value(min_v: Decimal | None, max_v: Decimal | None, source: str | None = None) -> str | None:
    """Return a human-readable value string."""
    if min_v is None and max_v is None:
        return None
    if min_v == max_v or max_v is None:
        v = min_v
    else:
        v = (min_v or Decimal(0) + (max_v or Decimal(0))) / 2
    if v is None:
        return None
    f = float(v)
    if f >= 1_000_000:
        return f"~${f/1_000_000:.1f}M"
    if f >= 1_000:
        return f"~${f/1_000:.0f}K"
    return f"~${f:.0f}"
