"""Shared template context helpers for Phase 14 web UI."""

from __future__ import annotations

from datetime import UTC, datetime
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
        v = ((min_v if min_v is not None else max_v) + (max_v if max_v is not None else min_v)) / 2
    if v is None:
        return None
    f = float(v)
    if f >= 1_000_000:
        return f"~${f/1_000_000:.1f}M"
    if f >= 1_000:
        return f"~${f/1_000:.0f}K"
    return f"~${f:.0f}"


_LINK_LABELS = {
    "ui": "SAM.gov notice",
    "additional_info": "Additional information",
    "description": "Full description",
    "index": "DIBBS daily index",
    "package": "DIBBS solicitation package",
    "batch_quote": "DIBBS batch quote template",
    "posted_date_search": "DIBBS posted-date search",
}
_PRIMARY_KEYS = ("ui", "additional_info")


def _http(value: object) -> str | None:
    if isinstance(value, str) and value.strip().lower().startswith(("https://", "http://")):
        return value.strip()
    return None


def source_links(links: object) -> list[dict[str, str]]:
    """Normalise an opportunity's ``links`` JSON into ``[{label, url}]`` for templates.

    Values may be strings, lists of strings, or lists of ``{href|url, rel|name}``
    objects; anything that is not an http(s) URL is dropped.
    """
    if not isinstance(links, dict):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(label: str, url: str | None) -> None:
        if url and url not in seen:
            seen.add(url)
            out.append({"label": label, "url": url})

    ordered = [k for k in _PRIMARY_KEYS if k in links] + [k for k in links if k not in _PRIMARY_KEYS]
    for key in ordered:
        value = links[key]
        base = _LINK_LABELS.get(key, str(key).replace("_", " ").title())
        if isinstance(value, list):
            for index, item in enumerate(value, start=1):
                if isinstance(item, dict):
                    url = _http(item.get("href") or item.get("url") or item.get("uri"))
                    name = item.get("name") or item.get("rel")
                    label = f"{base}: {name}" if isinstance(name, str) and name else f"{base} {index}"
                else:
                    url = _http(item)
                    label = f"Attachment {index}" if key == "attachments" else f"{base} {index}"
                add(label, url)
        elif isinstance(value, dict):
            add(base, _http(value.get("href") or value.get("url")))
        else:
            add(base, _http(value))
    return out


def primary_source_url(links: object) -> str | None:
    """The public notice page when known, else the first usable link."""
    if isinstance(links, dict):
        for key in _PRIMARY_KEYS:
            url = _http(links.get(key))
            if url:
                return url
    entries = source_links(links)
    return entries[0]["url"] if entries else None
