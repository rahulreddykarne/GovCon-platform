"""Record provider-reported token use and price it from ``ai_model_prices``.

Token counts come only from the response usage object. A missing field stays
NULL. Cost stays NULL when the model has no price row, except blocked and local
calls, which are explicitly $0 because nothing was billed.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Connection, Engine, select
from sqlalchemy.orm import Session

from govcon.models import AIModelPrice, AIProviderCall

_MILLION = Decimal(1_000_000)
_link_ids: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar("ai_usage_link_ids", default=None)


@dataclass(frozen=True)
class ReportedTokens:
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    cache_write_tokens: int | None


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

    if billable is None and output is None and cached is None and cache_write is None:
        return None
    if billable is not None and billable < 0:
        return None
    return ReportedTokens(billable, output, cached, cache_write)


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
    return total / _MILLION


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
            price_id = None
        else:
            cost = cost_for(tokens, price)
            input_tokens = None if tokens is None else tokens.input_tokens
            output_tokens = None if tokens is None else tokens.output_tokens
            cached = None if tokens is None else tokens.cached_tokens
            cache_write = None if tokens is None else tokens.cache_write_tokens
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
            latency_ms=latency_ms,
            analysis_id=analysis_id,
            decision_run_id=decision_run_id,
            cost_usd=cost,
            price_id=price_id,
        )
        db.add(row)
        db.flush()
        row_id = row.id
    bucket = _link_ids.get()
    if bucket is not None:
        bucket.append(row_id)
    return row_id


def collect_call_ids() -> tuple[contextvars.Token[list[int] | None], list[int]]:
    bucket: list[int] = []
    return _link_ids.set(bucket), bucket


def stop_collecting(token: contextvars.Token[list[int] | None]) -> None:
    _link_ids.reset(token)


def attach_call_ids(
    session: Session | None,
    ids: list[int],
    *,
    analysis_id: int | None = None,
    decision_run_id: int | None = None,
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


def _cost_label(rows: list[AIProviderCall]) -> str:
    if not rows:
        return "No calls in this period."
    known = [row.cost_usd for row in rows if row.cost_usd is not None]
    unpriced = [
        row for row in rows
        if row.status in {"succeeded", "failed"} and row.cost_usd is None
        and (row.input_tokens is not None or row.output_tokens is not None)
    ]
    unreported = [
        row for row in rows
        if row.status in {"succeeded", "failed"} and row.input_tokens is None and row.output_tokens is None
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
        return "price not set"
    return _money(row.cost_usd)


def _window(rows: list[AIProviderCall], start: datetime | None) -> list[AIProviderCall]:
    if start is None:
        return rows
    return [row for row in rows if row.created_at >= start]


def usage_page(session: Session) -> dict[str, Any]:
    """View model for /operate/usage and the /ops summary. No placeholder numbers."""
    rows = list(session.scalars(select(AIProviderCall).order_by(AIProviderCall.id.desc())).all())
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
        periods.append({
            "title": title,
            "empty": not chosen,
            "calls": len(chosen),
            "by_model": _group(chosen, lambda row: f"{row.provider} · {row.model or 'model not reported'}"),
            "by_purpose": _group(chosen, lambda row: row.purpose),
            "by_opportunity": _group(
                [row for row in chosen if row.opportunity_id is not None],
                lambda row: f"#{row.opportunity_id}",
            ),
        })
    recent = []
    for row in rows[:50]:
        recent.append({
            "when": row.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "provider": row.provider,
            "model": row.model or "—",
            "purpose": row.purpose,
            "opportunity_id": row.opportunity_id,
            "status": row.status,
            "input": _call_tokens(row, "input_tokens"),
            "output": _call_tokens(row, "output_tokens"),
            "cached": _call_tokens(row, "cached_tokens"),
            "cost": _call_cost(row),
            "latency": f"{row.latency_ms} ms" if row.latency_ms is not None else "—",
            "analysis_id": row.analysis_id,
            "decision_run_id": row.decision_run_id,
        })
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
    }


def parse_price_amount(raw: str, *, required: bool) -> Decimal | None:
    text = (raw or "").strip()
    if not text:
        if required:
            raise ValueError("a price is required")
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("price must be a number of USD per 1M tokens") from exc
    if amount < 0:
        raise ValueError("price cannot be negative")
    return amount
