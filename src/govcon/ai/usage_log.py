"""Record provider-reported token use and price it from ``ai_model_prices``.

Token counts come only from the response usage object. A missing field stays
NULL. Cost stays NULL when the model has no price row, except blocked and local
calls, which are explicitly $0 because nothing was billed. Server-tool counts
(``web_search_requests``/``web_fetch_requests``) are stored as reported; a call
that ran searches is only priced when the model's search fee is set.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Connection, Engine, select
from sqlalchemy.orm import Session

from govcon.display_time import format_pt
from govcon.models import AIModelPrice, AIProviderCall

USAGE_TABLE_LIMIT = 25
USAGE_STATUSES = ("succeeded", "truncated", "output_rejected", "blocked", "failed")
_JEV_PURPOSES = ("jev_decision_package", "jev_routing")

_MILLION = Decimal(1_000_000)
_THOUSAND = Decimal(1_000)
_link_stack: contextvars.ContextVar[tuple[list[int], ...] | None] = contextvars.ContextVar(
    "ai_usage_link_stack", default=None
)


@dataclass(frozen=True)
class ReportedTokens:
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    cache_write_tokens: int | None
    web_search_requests: int | None = None
    web_fetch_requests: int | None = None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def reported_tokens(usage: object) -> ReportedTokens | None:
    """Read token fields the provider actually returned. Never fill gaps."""
    if not isinstance(usage, dict) or not usage:
        return None
    hit = _count(usage.get("prompt_cache_hit_tokens"))
    miss = _count(usage.get("prompt_cache_miss_tokens"))
    cache_read = _count(usage.get("cache_read_input_tokens"))
    cache_write = _count(usage.get("cache_creation_input_tokens"))
    details = usage.get("prompt_tokens_details")
    openai_cached = _count(details.get("cached_tokens")) if isinstance(details, dict) else None
    prompt = _count(usage.get("prompt_tokens"))
    input_tokens = _count(usage.get("input_tokens"))
    output = _count(usage.get("completion_tokens"))
    if output is None:
        output = _count(usage.get("output_tokens"))

    cached: int | None
    billable: int | None
    if hit is not None or miss is not None:
        cached = hit if hit is not None else 0
        if miss is not None:
            billable = miss
        elif prompt is not None and hit is not None:
            billable = prompt - hit
        else:
            billable = prompt if prompt is not None else input_tokens
    elif openai_cached is not None and prompt is not None:
        cached = openai_cached
        billable = prompt - openai_cached
    elif cache_read is not None or cache_write is not None or input_tokens is not None:
        cached = cache_read
        billable = input_tokens
    elif prompt is not None:
        cached = None
        billable = prompt
    else:
        cached = None
        billable = None

    searches = _count(usage.get("web_search_requests"))
    fetches = _count(usage.get("web_fetch_requests"))
    if (billable is None and output is None and cached is None and cache_write is None
            and searches is None and fetches is None):
        return None
    if billable is not None and billable < 0:
        return None
    return ReportedTokens(billable, output, cached, cache_write, searches, fetches)


def _engine_for(session: Session | None, engine: Engine | Connection | None) -> Engine | Connection | None:
    if engine is not None:
        return engine
    if session is None:
        return None
    bind = session.get_bind()
    return getattr(bind, "engine", bind)


def price_for(db: Session, provider: str, model: str | None) -> AIModelPrice | None:
    if not model:
        return None
    return db.scalar(
        select(AIModelPrice).where(AIModelPrice.provider == provider, AIModelPrice.model == model)
    )


def cost_for(tokens: ReportedTokens | None, price: AIModelPrice | None) -> Decimal | None:
    """Price a fully reported call. Missing usage or a missing rate stays NULL."""
    if tokens is None or price is None:
        return None
    if tokens.input_tokens is None or tokens.output_tokens is None:
        return None
    if tokens.cached_tokens and price.cached_usd_per_million is None:
        return None
    if tokens.cache_write_tokens and price.cache_write_usd_per_million is None:
        return None
    if tokens.web_search_requests and price.web_search_usd_per_thousand is None:
        return None
    total = (
        Decimal(tokens.input_tokens) * price.input_usd_per_million
        + Decimal(tokens.output_tokens) * price.output_usd_per_million
    )
    if tokens.cached_tokens:
        assert price.cached_usd_per_million is not None
        total += Decimal(tokens.cached_tokens) * price.cached_usd_per_million
    if tokens.cache_write_tokens:
        assert price.cache_write_usd_per_million is not None
        total += Decimal(tokens.cache_write_tokens) * price.cache_write_usd_per_million
    total = total / _MILLION
    if tokens.web_search_requests:
        assert price.web_search_usd_per_thousand is not None
        total += Decimal(tokens.web_search_requests) * price.web_search_usd_per_thousand / _THOUSAND
    return total


def record_call(
    session: Session | None,
    *,
    provider: str,
    purpose: str,
    status: str,
    model: str | None = None,
    opportunity_id: int | None = None,
    usage: object = None,
    latency_ms: int | None = None,
    analysis_id: int | None = None,
    decision_run_id: int | None = None,
    compliance_run_id: int | None = None,
    finish_reason: str | None = None,
    engine: Engine | Connection | None = None,
) -> int | None:
    """Persist one attempt on its own connection. Returns the row id, or None if there is no database."""
    bind = _engine_for(session, engine)
    if bind is None:
        return None
    tokens = reported_tokens(usage)
    explicit_zero = status in {"blocked", "local"}
    with Session(bind) as db, db.begin():
        price = None if explicit_zero else price_for(db, provider, model)
        if explicit_zero:
            cost: Decimal | None = Decimal(0)
            input_tokens: int | None = 0 if status == "blocked" else None
            output_tokens: int | None = 0 if status == "blocked" else None
            cached = 0 if status == "blocked" else None
            cache_write = None
            searches: int | None = 0 if status == "blocked" else None
            fetches: int | None = 0 if status == "blocked" else None
            price_id = None
        else:
            cost = cost_for(tokens, price)
            input_tokens = None if tokens is None else tokens.input_tokens
            output_tokens = None if tokens is None else tokens.output_tokens
            cached = None if tokens is None else tokens.cached_tokens
            cache_write = None if tokens is None else tokens.cache_write_tokens
            searches = None if tokens is None else tokens.web_search_requests
            fetches = None if tokens is None else tokens.web_fetch_requests
            price_id = None if price is None else price.id
        row = AIProviderCall(
            opportunity_id=opportunity_id,
            purpose=purpose,
            provider=provider,
            model=model,
            status=status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached,
            cache_write_tokens=cache_write,
            web_search_requests=searches,
            web_fetch_requests=fetches,
            latency_ms=latency_ms,
            analysis_id=analysis_id,
            decision_run_id=decision_run_id,
            compliance_run_id=compliance_run_id,
            finish_reason=finish_reason,
            cost_usd=cost,
            price_id=price_id,
        )
        db.add(row)
        db.flush()
        row_id = row.id
    stack = _link_stack.get()
    if stack:
        for bucket in stack:
            bucket.append(row_id)
    return row_id


def collect_call_ids() -> tuple[contextvars.Token[tuple[list[int], ...] | None], list[int]]:
    """Start collecting call ids. Nested collectors each receive every new id."""
    bucket: list[int] = []
    stack = _link_stack.get() or ()
    return _link_stack.set((*stack, bucket)), bucket


def stop_collecting(token: contextvars.Token[tuple[list[int], ...] | None]) -> None:
    _link_stack.reset(token)


def update_call_status(
    session: Session | None,
    call_id: int | None,
    *,
    status: str,
    finish_reason: str | None = None,
    engine: Engine | Connection | None = None,
) -> None:
    """Reclassify a persisted attempt (invalid JSON is not a plain success)."""
    if call_id is None:
        return
    bind = _engine_for(session, engine)
    if bind is None:
        return
    with Session(bind) as db, db.begin():
        row = db.get(AIProviderCall, call_id)
        if row is None:
            return
        if row.status == "truncated" and status == "output_rejected":
            return
        row.status = status
        if finish_reason is not None:
            row.finish_reason = finish_reason


def attach_call_ids(
    session: Session | None,
    ids: list[int],
    *,
    analysis_id: int | None = None,
    decision_run_id: int | None = None,
    compliance_run_id: int | None = None,
    engine: Engine | Connection | None = None,
) -> None:
    if not ids:
        return
    bind = _engine_for(session, engine)
    if bind is None:
        return
    with Session(bind) as db, db.begin():
        rows = db.scalars(select(AIProviderCall).where(AIProviderCall.id.in_(ids))).all()
        for row in rows:
            if analysis_id is not None:
                row.analysis_id = analysis_id
            if decision_run_id is not None:
                row.decision_run_id = decision_run_id
            if compliance_run_id is not None:
                row.compliance_run_id = compliance_run_id


def _money(value: Decimal) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return f"${text}"


def _token_label(rows: list[AIProviderCall], attr: str) -> str:
    # Local rows store NULL because no provider was called. That is not a missing usage report.
    reportable = [row for row in rows if row.status != "local"]
    if not reportable:
        return "—"
    values = [getattr(row, attr) for row in reportable]
    known = [value for value in values if value is not None]
    missing = len(values) - len(known)
    if not known and missing:
        return "usage not reported"
    if not known:
        return "0"
    label = f"{sum(known):,}"
    if missing:
        label += f" ({missing} not reported)"
    return label


def _search_label(rows: list[AIProviderCall]) -> str:
    known = [row.web_search_requests for row in rows if row.web_search_requests is not None]
    return f"{sum(known):,}" if known else "—"


def _cost_label(rows: list[AIProviderCall]) -> str:
    if not rows:
        return "No calls in this period."
    known = [row.cost_usd for row in rows if row.cost_usd is not None]
    unpriced = [
        row for row in rows
        if row.status in {"succeeded", "failed", "truncated", "output_rejected"} and row.cost_usd is None
        and (row.input_tokens is not None or row.output_tokens is not None)
    ]
    unreported = [
        row for row in rows
        if row.status in {"succeeded", "failed", "truncated", "output_rejected"}
        and row.input_tokens is None and row.output_tokens is None
    ]
    if not known and unpriced and not unreported:
        return "price not set"
    if not known and unreported and not unpriced:
        return "usage not reported"
    parts: list[str] = []
    if known:
        parts.append(_money(sum(known, Decimal(0))))
    if unpriced:
        parts.append("price not set" if not known else f"price not set on {len(unpriced)}")
    if unreported:
        parts.append("usage not reported" if not known and not unpriced else f"usage not reported on {len(unreported)}")
    return " · ".join(parts) if parts else "price not set"


def _group(rows: list[AIProviderCall], key) -> list[dict[str, Any]]:
    buckets: dict[Any, list[AIProviderCall]] = {}
    for row in rows:
        buckets.setdefault(key(row), []).append(row)
    out = []
    for label, grouped in buckets.items():
        out.append({
            "label": label,
            "calls": len(grouped),
            "input": _token_label(grouped, "input_tokens"),
            "output": _token_label(grouped, "output_tokens"),
            "searches": _search_label(grouped),
            "cost": _cost_label(grouped),
        })
    out.sort(key=lambda item: (-item["calls"], str(item["label"])))
    return out


def _call_tokens(row: AIProviderCall, attr: str) -> str:
    value = getattr(row, attr)
    if row.status == "blocked":
        return "0"
    if row.status == "local":
        return "—"
    if value is None:
        return "usage not reported"
    return f"{value:,}"


def _call_cost(row: AIProviderCall) -> str:
    if row.status in {"blocked", "local"}:
        return "$0"
    if row.input_tokens is None and row.output_tokens is None:
        return "usage not reported"
    if row.cost_usd is None:
        if row.price_id is not None and row.web_search_requests:
            return "price not set (web search fee)"
        return "price not set"
    return _money(row.cost_usd)


def _server_tools(row: AIProviderCall) -> str:
    if row.status == "local":
        return "—"
    parts = []
    if row.web_search_requests is not None:
        parts.append(f"{row.web_search_requests:,} search")
    if row.web_fetch_requests is not None:
        parts.append(f"{row.web_fetch_requests:,} fetch")
    return " · ".join(parts) if parts else "—"


def _window(rows: list[AIProviderCall], start: datetime | None) -> list[AIProviderCall]:
    if start is None:
        return rows
    return [row for row in rows if row.created_at >= start]


def _is_blocked_jev(row: AIProviderCall) -> bool:
    purpose = (row.purpose or "")
    return row.status == "blocked" and (
        row.provider == "jev"
        or purpose.startswith("decision_bundle:")
        or purpose in _JEV_PURPOSES
        or purpose.startswith("jev_")
    )


def _collapse_blocked_jev(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse consecutive blocked JEV rows that share purpose/provider/model."""
    collapsed: list[dict[str, Any]] = []
    for item in items:
        prev = collapsed[-1] if collapsed else None
        if (
            prev
            and item.get("grouped_jev")
            and prev.get("grouped_jev")
            and prev["purpose"] == item["purpose"]
            and prev["provider"] == item["provider"]
            and prev["model"] == item["model"]
            and prev["status"] == "blocked"
        ):
            prev["count"] = int(prev.get("count") or 1) + 1
            continue
        collapsed.append(item)
    return collapsed


def usage_page(
    session: Session,
    *,
    include_local: bool = False,
    show_more: bool = False,
    status: str | None = None,
) -> dict[str, Any]:
    """View model for /operate/usage and the /ops summary. No placeholder numbers."""
    query = select(AIProviderCall).order_by(AIProviderCall.id.desc())
    if not include_local:
        query = query.where(AIProviderCall.status != "local")
    rows = list(session.scalars(query).all())
    status_counts = {name: 0 for name in USAGE_STATUSES}
    for row in rows:
        if row.status in status_counts:
            status_counts[row.status] += 1
    status_filter = status if status in USAGE_STATUSES else None
    if status_filter:
        rows = [row for row in rows if row.status == status_filter]
    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    windows = (
        ("Today", today),
        ("Last 7 days", now - timedelta(days=7)),
        ("Last 30 days", now - timedelta(days=30)),
        ("All time", None),
    )
    periods = []
    for title, start in windows:
        chosen = _window(rows, start)
        opp_groups = _group(
            [row for row in chosen if row.opportunity_id is not None],
            lambda row: f"#{row.opportunity_id}",
        )
        periods.append({
            "title": title,
            "empty": not chosen,
            "calls": len(chosen),
            "by_model": _group(chosen, lambda row: f"{row.provider} · {row.model or 'model not reported'}"),
            "by_purpose": _group(chosen, lambda row: row.purpose),
            "by_opportunity": _limit_rows(opp_groups, show_more),
            "opportunity_total": len(opp_groups),
        })
    recent = []
    recent_source = rows if show_more else rows[:USAGE_TABLE_LIMIT]
    for row in recent_source:
        recent.append({
            "when": format_pt(row.created_at, seconds=True),
            "provider": row.provider,
            "model": row.model or "—",
            "purpose": row.purpose,
            "opportunity_id": row.opportunity_id,
            "status": row.status,
            "input": _call_tokens(row, "input_tokens"),
            "output": _call_tokens(row, "output_tokens"),
            "cached": _call_tokens(row, "cached_tokens"),
            "server_tools": _server_tools(row),
            "cost": _call_cost(row),
            "latency": f"{row.latency_ms} ms" if row.latency_ms is not None else "—",
            "analysis_id": row.analysis_id,
            "decision_run_id": row.decision_run_id,
            "count": 1,
            "grouped_jev": _is_blocked_jev(row),
        })
    recent = _collapse_blocked_jev(recent)
    prices = list(session.scalars(select(AIModelPrice).order_by(AIModelPrice.provider, AIModelPrice.model)).all())
    summary_rows = _window(rows, today)
    month_rows = _window(rows, now - timedelta(days=30))
    return {
        "empty": not rows,
        "periods": periods,
        "recent": recent,
        "prices": prices,
        "today_calls": len(summary_rows),
        "today_cost": _cost_label(summary_rows) if summary_rows else None,
        "month_calls": len(month_rows),
        "month_cost": _cost_label(month_rows) if month_rows else None,
        "include_local": include_local,
        "show_more": show_more,
        "table_limit": USAGE_TABLE_LIMIT,
        "recent_total": len(rows),
        "status_counts": status_counts,
        "status_filter": status_filter,
        "statuses": USAGE_STATUSES,
    }


def _limit_rows(rows: list[dict[str, Any]], show_more: bool) -> list[dict[str, Any]]:
    if show_more or len(rows) <= USAGE_TABLE_LIMIT:
        return rows
    return rows[:USAGE_TABLE_LIMIT]


def parse_price_amount(raw: str, *, required: bool, unit: str = "USD per 1M tokens") -> Decimal | None:
    text = (raw or "").strip()
    if not text:
        if required:
            raise ValueError("a price is required")
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"price must be a number of {unit}") from exc
    if not amount.is_finite():
        raise ValueError(f"price must be a number of {unit}")
    if amount < 0:
        raise ValueError("price cannot be negative")
    return amount
