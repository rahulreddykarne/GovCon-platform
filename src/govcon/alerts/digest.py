"""Alert digests for unalerted new matches and optional deadline re-alerts.

Phase 3 sends one HTML message per run, grouped by watchlist. SMTP is used
when ``SMTP_HOST`` and ``ALERT_EMAIL_TO`` are both set. Otherwise the HTML
file is written under ``OUTBOX_DIR``. An empty run writes nothing and sends
nothing.

Later phases may add historical awards, competitors, and bid recommendations.
This renderer does not.
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.message import EmailMessage
from html import escape
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.config import Settings, get_settings
from govcon.logging import redact
from govcon.models import Match, Opportunity, OpportunityEvent, Watchlist

logger = logging.getLogger("govcon.alerts.digest")


class DigestDeliveryError(RuntimeError):
    """Raised when a digest was not delivered. Matches stay unalerted."""


@dataclass
class DigestResult:
    sent: bool
    channel: str
    path: str | None
    new_count: int
    amendment_count: int
    match_ids: list[int]


@dataclass
class _Item:
    match: Match
    kind: str
    watchlist_id: int
    watchlist_name: str
    title: str
    agency: str
    source: str
    psc: str
    naics: str
    set_aside: str
    deadline_text: str
    days_remaining_text: str
    estimated_value_text: str
    source_link: str | None
    previous_deadline_text: str | None
    event_detected_at: datetime | None
    match_status: str


def run_digest(
    session: Session,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> DigestResult:
    """Deliver one digest, then stamp ``alerted_at`` on the included matches.

    Delivery runs before the watermark is flushed. A successful commit does not
    send the same match again. A crash after delivery and before commit can
    produce one duplicate on the next run.
    """
    settings = settings or get_settings()
    now = _aware(now or datetime.now(UTC))
    new_items = _collect_new(session, now)
    amendment_items = _collect_amendments(
        session,
        enabled=settings.alert_on_material_deadline_change,
        now=now,
    )
    if not new_items and not amendment_items:
        logger.info("alert_digest sent=no channel=none new=0 amendments=0")
        return DigestResult(
            sent=False,
            channel="none",
            path=None,
            new_count=0,
            amendment_count=0,
            match_ids=[],
        )

    html = render_html(new_items, amendment_items, generated_at=now)
    plain = render_plain(new_items, amendment_items, generated_at=now)
    subject = _subject(len(new_items), len(amendment_items))
    if settings.email_configured:
        send_smtp(settings, subject=subject, html=html, plain=plain)
        channel = "smtp"
        path = None
    else:
        written = write_outbox(resolve_outbox(settings), html, now)
        channel = "outbox"
        path = str(written)

    for item in new_items + amendment_items:
        _mark_alerted(item, now)
    match_ids = [item.match.id for item in new_items + amendment_items]
    session.flush()
    record_audit(
        session,
        action_type="alert_digest_sent",
        entity_type="alert_digest",
        new_value={
            "channel": channel,
            "new_count": len(new_items),
            "amendment_count": len(amendment_items),
            "match_ids": match_ids,
        },
    )
    logger.info(
        "alert_digest sent=yes channel=%s new=%s amendments=%s",
        channel,
        len(new_items),
        len(amendment_items),
    )
    return DigestResult(
        sent=True,
        channel=channel,
        path=path,
        new_count=len(new_items),
        amendment_count=len(amendment_items),
        match_ids=match_ids,
    )


def render_html(new_items: list[_Item], amendment_items: list[_Item], *, generated_at: datetime) -> str:
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        "<title>GovCon alert digest</title>",
        "</head>",
        "<body>",
        "<h1>GovCon alert digest</h1>",
        f"<p>Generated {_esc(generated_at.astimezone(UTC).isoformat())}</p>",
    ]
    if new_items:
        parts.append("<h2>New matches</h2>")
        parts.extend(_render_groups(new_items, kind="new"))
    if amendment_items:
        parts.append("<h2>Amendment alerts</h2>")
        parts.append("<p>Material deadline change since the previous alert.</p>")
        parts.extend(_render_groups(amendment_items, kind="amendment"))
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def render_plain(new_items: list[_Item], amendment_items: list[_Item], *, generated_at: datetime) -> str:
    lines = [
        "GovCon alert digest",
        f"Generated {generated_at.astimezone(UTC).isoformat()}",
        "",
    ]
    if new_items:
        lines.append("New matches")
        lines.extend(_plain_groups(new_items))
    if amendment_items:
        lines.append("Amendment alerts")
        lines.append("Material deadline change since the previous alert.")
        lines.extend(_plain_groups(amendment_items))
    return "\n".join(lines) + "\n"


def resolve_outbox(settings: Settings) -> Path:
    path = settings.outbox_dir
    if path.is_absolute():
        return path
    return (Path.cwd() / path).resolve()


def write_outbox(directory: Path, html: str, now: datetime) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"digest-{stamp}.html"
    suffix = 2
    while path.exists():
        path = directory / f"digest-{stamp}-{suffix}.html"
        suffix += 1
    path.write_text(html, encoding="utf-8")
    return path


def send_smtp(settings: Settings, *, subject: str, html: str, plain: str) -> None:
    if not settings.smtp_host or not settings.alert_email_to:
        raise DigestDeliveryError("SMTP delivery requires SMTP_HOST and ALERT_EMAIL_TO")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = _from_address(settings)
    message["To"] = settings.alert_email_to
    message.set_content(plain)
    message.add_alternative(html, subtype="html")
    try:
        client_factory = smtplib.SMTP_SSL if settings.smtp_port == 465 else smtplib.SMTP
        with client_factory(settings.smtp_host, settings.smtp_port, timeout=30) as client:
            client.ehlo()
            if settings.smtp_port != 465 and client.has_extn("starttls"):
                client.starttls()
                client.ehlo()
            if settings.smtp_user and settings.smtp_pass:
                client.login(settings.smtp_user, settings.smtp_pass)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise DigestDeliveryError(f"SMTP delivery failed: {_safe_error(exc, settings)}") from exc


def source_link(links: dict | None) -> str | None:
    """Prefer the public opportunity page. Only http(s) URLs are returned."""
    if not isinstance(links, dict):
        return None
    candidates: list[str] = []
    ui = links.get("ui")
    if isinstance(ui, str):
        candidates.append(ui)
    additional = links.get("additional_info")
    if isinstance(additional, str):
        candidates.append(additional)
    self_links = links.get("self")
    if isinstance(self_links, list):
        for item in self_links:
            if isinstance(item, dict) and isinstance(item.get("href"), str):
                candidates.append(item["href"])
    description = links.get("description")
    if isinstance(description, str):
        candidates.append(description)
    for candidate in candidates:
        if candidate.startswith(("http://", "https://")):
            return candidate
    return None


def format_estimated_value(min_value: Decimal | None, max_value: Decimal | None) -> str | None:
    """Return the stated estimate. Unknown values stay unset."""
    if min_value is None and max_value is None:
        return None
    if min_value is not None and max_value is not None and min_value != max_value:
        return f"{_format_decimal(min_value)}–{_format_decimal(max_value)}"
    chosen = min_value if min_value is not None else max_value
    assert chosen is not None
    return _format_decimal(chosen)


def calendar_days_remaining(deadline: datetime | None, now: datetime) -> int | None:
    """Whole UTC calendar days from ``now`` until ``deadline``."""
    if deadline is None:
        return None
    deadline = _aware(deadline)
    now = _aware(now)
    return (deadline.astimezone(UTC).date() - now.astimezone(UTC).date()).days


def _collect_new(session: Session, now: datetime) -> list[_Item]:
    rows = session.execute(
        select(Match, Watchlist, Opportunity)
        .join(Watchlist, Match.watchlist_id == Watchlist.id)
        .join(Opportunity, Match.opportunity_id == Opportunity.id)
        .where(
            Match.status == "new",
            Match.alerted_at.is_(None),
            Watchlist.enabled.is_(True),
        )
        .order_by(Match.id)
        .with_for_update(of=Match)
    ).all()
    items = [
        _item_from_row(match, watchlist, opportunity, kind="new", now=now, previous=None, detected_at=None)
        for match, watchlist, opportunity in rows
        if match.status == "new" and match.alerted_at is None
    ]
    items.sort(key=lambda item: (item.watchlist_id, _deadline_sort(item), item.match.id))
    return items


def _collect_amendments(session: Session, *, enabled: bool, now: datetime) -> list[_Item]:
    if not enabled:
        return []
    ranked = (
        select(
            OpportunityEvent.opportunity_id.label("opportunity_id"),
            OpportunityEvent.old_value.label("old_value"),
            OpportunityEvent.detected_at.label("detected_at"),
            func.row_number()
            .over(
                partition_by=OpportunityEvent.opportunity_id,
                order_by=(OpportunityEvent.detected_at.desc(), OpportunityEvent.id.desc()),
            )
            .label("rn"),
        )
        .where(OpportunityEvent.event_type == "deadline_changed")
        .subquery()
    )
    rows = session.execute(
        select(Match, Watchlist, Opportunity, ranked.c.old_value, ranked.c.detected_at)
        .join(Watchlist, Match.watchlist_id == Watchlist.id)
        .join(Opportunity, Match.opportunity_id == Opportunity.id)
        .join(ranked, ranked.c.opportunity_id == Opportunity.id)
        .where(
            Match.alerted_at.is_not(None),
            Watchlist.enabled.is_(True),
            ranked.c.rn == 1,
            ranked.c.detected_at > Match.alerted_at,
        )
        .order_by(Match.id)
        .with_for_update(of=Match)
    ).all()
    items: list[_Item] = []
    for match, watchlist, opportunity, old_value, detected_at in rows:
        if match.alerted_at is None or detected_at <= match.alerted_at:
            continue
        items.append(
            _item_from_row(
                match,
                watchlist,
                opportunity,
                kind="amendment",
                now=now,
                previous=_event_scalar(old_value),
                detected_at=detected_at,
            )
        )
    items.sort(key=lambda item: (item.watchlist_id, item.match.id))
    return items


def _item_from_row(
    match: Match,
    watchlist: Watchlist,
    opportunity: Opportunity,
    *,
    kind: str,
    now: datetime,
    previous: str | None,
    detected_at: datetime | None,
) -> _Item:
    estimate = format_estimated_value(opportunity.estimated_value_min, opportunity.estimated_value_max)
    return _Item(
        match=match,
        kind=kind,
        watchlist_id=watchlist.id,
        watchlist_name=watchlist.name,
        title=_stated(opportunity.title),
        agency=_stated(opportunity.agency_path),
        source=_stated(opportunity.source),
        psc=_stated(opportunity.psc_code),
        naics=_stated(opportunity.naics_code),
        set_aside=_stated(opportunity.set_aside_code),
        deadline_text=_deadline_text(opportunity.response_deadline),
        days_remaining_text=_days_label(opportunity.response_deadline, now),
        estimated_value_text=estimate if estimate is not None else "not stated",
        source_link=source_link(opportunity.links),
        previous_deadline_text=previous,
        event_detected_at=_aware(detected_at) if detected_at is not None else None,
        match_status=match.status,
    )


def _mark_alerted(item: _Item, now: datetime) -> None:
    stamped = now
    if item.event_detected_at is not None and item.event_detected_at > stamped:
        stamped = item.event_detected_at
    item.match.alerted_at = stamped
    if item.kind == "new" and item.match.status == "new":
        item.match.status = "seen"


def _render_groups(items: list[_Item], *, kind: str) -> list[str]:
    html: list[str] = []
    for _watchlist_id, name, group in _groups(items):
        html.append("<section>")
        html.append(f"<h3>Watchlist: {_esc(name)}</h3>")
        for item in group:
            html.append(f'<article class="match" data-kind="{kind}" data-match-id="{item.match.id}">')
            html.append(f"<h4>{_esc(item.title)}</h4>")
            html.append("<dl>")
            html.extend(_row("Agency", item.agency))
            html.extend(_row("Source", item.source))
            html.extend(_row("PSC", item.psc))
            html.extend(_row("NAICS", item.naics))
            html.extend(_row("Set-aside", item.set_aside))
            html.extend(_row("Deadline", item.deadline_text))
            html.extend(_row("Days remaining", item.days_remaining_text))
            html.extend(_row("Estimated value", item.estimated_value_text))
            if item.source_link:
                href = escape(item.source_link, quote=True)
                html.append("<dt>Source link</dt>")
                html.append(f'<dd><a href="{href}">{_esc(item.source_link)}</a></dd>')
            else:
                html.extend(_row("Source link", "not stated"))
            if kind == "amendment":
                html.extend(_row("Previous deadline", item.previous_deadline_text or "not stated"))
                html.extend(_row("Change", "material deadline change"))
                html.extend(_row("Match status", item.match_status))
            html.append("</dl></article>")
        html.append("</section>")
    return html


def _plain_groups(items: list[_Item]) -> list[str]:
    lines: list[str] = []
    for _watchlist_id, name, group in _groups(items):
        lines.append(f"Watchlist: {name}")
        for item in group:
            lines.append(f"Title: {item.title}")
            lines.append(f"Agency: {item.agency}")
            lines.append(f"Source: {item.source}")
            lines.append(f"PSC: {item.psc}")
            lines.append(f"NAICS: {item.naics}")
            lines.append(f"Set-aside: {item.set_aside}")
            lines.append(f"Deadline: {item.deadline_text}")
            lines.append(f"Days remaining: {item.days_remaining_text}")
            lines.append(f"Estimated value: {item.estimated_value_text}")
            lines.append(f"Source link: {item.source_link or 'not stated'}")
            if item.kind == "amendment":
                lines.append(f"Previous deadline: {item.previous_deadline_text or 'not stated'}")
                lines.append("Change: material deadline change")
                lines.append(f"Match status: {item.match_status}")
            lines.append("")
    return lines


def _groups(items: list[_Item]) -> list[tuple[int, str, list[_Item]]]:
    grouped: list[tuple[int, str, list[_Item]]] = []
    for item in items:
        if not grouped or grouped[-1][0] != item.watchlist_id:
            grouped.append((item.watchlist_id, item.watchlist_name, [item]))
        else:
            grouped[-1][2].append(item)
    return grouped


def _row(label: str, value: str) -> list[str]:
    return [f"<dt>{_esc(label)}</dt>", f"<dd>{_esc(value)}</dd>"]


def _subject(new_count: int, amendment_count: int) -> str:
    parts: list[str] = []
    if new_count:
        parts.append(f"{new_count} new")
    if amendment_count:
        parts.append(f"{amendment_count} amendment")
    return f"GovCon alert digest ({', '.join(parts)})"


def _from_address(settings: Settings) -> str:
    user = settings.smtp_user or ""
    if "@" in user:
        return user
    return "govcon-alerts@localhost"


def _safe_error(exc: Exception, settings: Settings) -> str:
    return redact(str(exc), settings.secret_values())


def _stated(value: str | None) -> str:
    if value is None or value.strip() == "":
        return "not stated"
    return value


def _deadline_text(deadline: datetime | None) -> str:
    if deadline is None:
        return "not stated"
    return _aware(deadline).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _days_label(deadline: datetime | None, now: datetime) -> str:
    remaining = calendar_days_remaining(deadline, now)
    if remaining is None:
        return "not stated"
    if remaining < 0:
        return f"{abs(remaining)} days overdue"
    return f"{remaining} days remaining"


def _event_scalar(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return "not stated"
    value = payload.get("value")
    if value in (None, ""):
        return "not stated"
    return str(value)


def _format_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _deadline_sort(item: _Item) -> tuple[int, str]:
    if item.deadline_text == "not stated":
        return (1, "")
    return (0, item.deadline_text)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _esc(value: str) -> str:
    return escape(value, quote=True)
