"""Alert digests for unalerted new matches and optional deadline re-alerts.

Phase 3 sends one HTML message per run, grouped by watchlist. SMTP is used
when ``SMTP_HOST`` and ``ALERT_EMAIL_TO`` are both set. Otherwise the HTML
file is written under ``OUTBOX_DIR``. An empty run writes nothing and sends
nothing.

Phase 5 adds recent award comps when a stored award matches the opportunity
NSN, or the PSC when no NSN history exists. Unit price is shown only when the
award row has one. Competitors and bid recommendations stay in later phases.

New-match alerts cover only active matches on open opportunities whose
deadline has not passed. Amendment alerts go to matches already alerted and
not dismissed, for the changes in ``AMENDMENT_EVENT_LABELS`` detected since
the last alert: a deadline change (gated by
``ALERT_ON_MATERIAL_DEADLINE_CHANGE``), a cancellation, newly posted files, or
a set-aside change. A cancellation is reported even though it closes the
opportunity; other changes to a closed opportunity are not.
"""

from __future__ import annotations

import ipaddress
import logging
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.message import EmailMessage
from html import escape
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.config import Settings, get_settings
from govcon.logging import redact
from govcon.matching.pricing import PricePoint, recent_award_comps
from govcon.models import Match, Opportunity, OpportunityEvent, Watchlist

logger = logging.getLogger("govcon.alerts.digest")

DEADLINE_CHANGED = "deadline_changed"
CANCELLED = "cancelled"
# Source events that re-alert an already-alerted match, with their digest label.
AMENDMENT_EVENT_LABELS = {
    DEADLINE_CHANGED: "material deadline change",
    CANCELLED: "opportunity cancelled",
    "files_added": "new files posted",
    "set_aside_changed": "set-aside changed",
}


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
    award_comps: list[PricePoint]
    changes: tuple[str, ...] = ()


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


def send_smtp(settings: Settings, *, subject: str, html: str, plain: str, to: str | None = None) -> None:
    """Send one message; ``to`` defaults to ``ALERT_EMAIL_TO`` (the digest recipient)."""
    recipient = to or settings.alert_email_to
    if not settings.smtp_host or not recipient:
        raise DigestDeliveryError("SMTP delivery requires SMTP_HOST and a recipient (ALERT_EMAIL_TO for digests)")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = _from_address(settings)
    message["To"] = recipient
    message.set_content(plain)
    message.add_alternative(html, subtype="html")
    plaintext_allowed = settings.smtp_allow_plaintext_local_relay and is_loopback_host(settings.smtp_host)
    try:
        # Certificate and host name are verified for both implicit TLS and STARTTLS.
        context = ssl.create_default_context()
        if settings.smtp_port == 465:
            client_cm = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30, context=context)
        else:
            client_cm = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
        with client_cm as client:
            client.ehlo()
            if settings.smtp_port != 465:
                if client.has_extn("starttls"):
                    client.starttls(context=context)
                    client.ehlo()
                elif not plaintext_allowed:
                    # Nothing (credentials or bid data) is sent over an unencrypted session.
                    raise DigestDeliveryError(
                        "SMTP server did not offer STARTTLS; refusing to authenticate or send without TLS "
                        "(use port 465, enable STARTTLS on the server, or set SMTP_ALLOW_PLAINTEXT_LOCAL_RELAY "
                        "for a relay on localhost)"
                    )
            if settings.smtp_user and settings.smtp_pass:
                client.login(settings.smtp_user, settings.smtp_pass)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise DigestDeliveryError(f"SMTP delivery failed: {_safe_error(exc, settings)}") from exc


def is_loopback_host(host: str | None) -> bool:
    """True only for this machine: ``localhost`` or a loopback IP literal."""
    value = (host or "").strip().strip("[]").lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


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
            Match.active.is_(True),
            Watchlist.enabled.is_(True),
            # Never alert on a closed or expired opportunity.
            Opportunity.status == "open",
            or_(Opportunity.response_deadline.is_(None), Opportunity.response_deadline >= now),
        )
        .order_by(Match.id)
        .with_for_update(of=Match)
    ).all()
    items = [
        _item_from_row(
            session,
            match,
            watchlist,
            opportunity,
            kind="new",
            now=now,
            previous=None,
            detected_at=None,
        )
        for match, watchlist, opportunity in rows
        if match.status == "new" and match.alerted_at is None
    ]
    items.sort(key=lambda item: (item.watchlist_id, _deadline_sort(item), item.match.id))
    return items


def _opportunity_open(opportunity: Opportunity, now: datetime) -> bool:
    if opportunity.status != "open":
        return False
    return opportunity.response_deadline is None or _aware(opportunity.response_deadline) >= now


def _collect_amendments(session: Session, *, enabled: bool, now: datetime) -> list[_Item]:
    """Already-alerted matches with an alertable source change since that alert.

    ``enabled`` gates deadline-change re-alerts only (ALERT_ON_MATERIAL_DEADLINE_CHANGE).
    """
    event_types = [t for t in AMENDMENT_EVENT_LABELS if enabled or t != DEADLINE_CHANGED]
    rows = session.execute(
        select(Match, Watchlist, Opportunity, OpportunityEvent)
        .join(Watchlist, Match.watchlist_id == Watchlist.id)
        .join(Opportunity, Match.opportunity_id == Opportunity.id)
        .join(OpportunityEvent, OpportunityEvent.opportunity_id == Opportunity.id)
        .where(
            Match.alerted_at.is_not(None),
            Match.status != "dismissed",
            Watchlist.enabled.is_(True),
            # A match that stopped matching is not followed; one closed by a
            # cancellation still hears about the cancellation.
            or_(Match.active.is_(True), Match.inactive_reason == "opportunity_closed"),
            OpportunityEvent.event_type.in_(event_types),
            OpportunityEvent.detected_at > Match.alerted_at,
        )
        .order_by(Match.id, OpportunityEvent.detected_at, OpportunityEvent.id)
        .with_for_update(of=Match)
    ).all()

    grouped: dict[int, tuple[Match, Watchlist, Opportunity, list[OpportunityEvent]]] = {}
    for match, watchlist, opportunity, event in rows:
        if match.alerted_at is None or event.detected_at <= match.alerted_at:
            continue
        grouped.setdefault(match.id, (match, watchlist, opportunity, []))[3].append(event)

    items: list[_Item] = []
    for match, watchlist, opportunity, events in grouped.values():
        is_open = _opportunity_open(opportunity, now)
        relevant = [e for e in events if e.event_type == CANCELLED or is_open]
        if not relevant:
            continue
        changes = tuple(dict.fromkeys(AMENDMENT_EVENT_LABELS[e.event_type] for e in relevant))
        deadline_events = [e for e in relevant if e.event_type == DEADLINE_CHANGED]
        item = _item_from_row(
            session,
            match,
            watchlist,
            opportunity,
            kind="amendment",
            now=now,
            previous=_event_scalar(deadline_events[-1].old_value) if deadline_events else None,
            detected_at=max(e.detected_at for e in relevant),
        )
        item.changes = changes
        items.append(item)
    items.sort(key=lambda item: (item.watchlist_id, item.match.id))
    return items


def _item_from_row(
    session: Session,
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
        award_comps=recent_award_comps(session, nsn=opportunity.nsn, psc_code=opportunity.psc_code),
    )


def _mark_alerted(item: _Item, now: datetime) -> None:
    stamped = now
    if item.event_detected_at is not None and item.event_detected_at > stamped:
        stamped = item.event_detected_at
    # Alerting is not triage: ``status`` stays ``new`` until a person acts on
    # the match, so it remains in the inbox. ``alerted_at`` alone prevents
    # a repeat alert.
    item.match.alerted_at = stamped


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
                if AMENDMENT_EVENT_LABELS[DEADLINE_CHANGED] in item.changes:
                    html.extend(_row("Previous deadline", item.previous_deadline_text or "not stated"))
                html.extend(_row("Change", "; ".join(item.changes) or "not stated"))
                html.extend(_row("Match status", item.match_status))
            html.append("</dl>")
            html.extend(_award_comp_html(item.award_comps))
            html.append("</article>")
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
                if AMENDMENT_EVENT_LABELS[DEADLINE_CHANGED] in item.changes:
                    lines.append(f"Previous deadline: {item.previous_deadline_text or 'not stated'}")
                lines.append(f"Change: {'; '.join(item.changes) or 'not stated'}")
                lines.append(f"Match status: {item.match_status}")
            lines.extend(_award_comp_plain(item.award_comps))
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


def _comp_text(comp: PricePoint) -> str:
    vendor = comp.vendor_name or "not stated"
    when = comp.action_date.isoformat() if comp.action_date else "not stated"
    amount = _format_decimal(comp.amount) if comp.amount is not None else "not stated"
    text = f"Vendor: {vendor}; Date: {when}; Amount: {amount}"
    if comp.unit_price is not None:
        text += f"; Unit price: {_format_decimal(comp.unit_price)}"
    return text


def _award_comp_html(comps: list[PricePoint]) -> list[str]:
    if not comps:
        return []
    html = ['<section class="award-comps">', "<h5>Recent award comps</h5>", "<ul>"]
    for comp in comps:
        html.append(f'<li data-award-id="{_esc(comp.award_id)}">{_esc(_comp_text(comp))}</li>')
    html.append("</ul></section>")
    return html


def _award_comp_plain(comps: list[PricePoint]) -> list[str]:
    if not comps:
        return []
    lines = ["Recent award comps:"]
    lines.extend(_comp_text(comp) for comp in comps)
    return lines


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
