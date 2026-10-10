"""Per-run spend estimate and a hard stop at twice that estimate.

A human sees the estimate before starting a rerun. Each paid call is charged
against it. Crossing 2× stops the run; cached parts stay committed so the next
run can continue without paying again.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from govcon.config import Settings
from govcon.models import AIAnalysis, FilePage, StoredFile


class SpendGuardExceeded(RuntimeError):
    """This run spent more than twice its pre-run estimate."""

    def __init__(self, message: str, *, spent: Decimal, estimate: Decimal, cap: Decimal) -> None:
        super().__init__(message)
        self.spent = spent
        self.estimate = estimate
        self.cap = cap


@dataclass
class SpendGuard:
    estimate_usd: Decimal
    estimate_tokens: int
    spent_usd: Decimal = field(default_factory=lambda: Decimal(0))
    spent_tokens: int = 0

    @property
    def cap_usd(self) -> Decimal:
        return self.estimate_usd * 2

    @property
    def cap_tokens(self) -> int:
        return self.estimate_tokens * 2

    def charge(self, *, cost_usd: Decimal | None, tokens: int | None) -> None:
        if cost_usd is not None:
            self.spent_usd += cost_usd
        if tokens:
            self.spent_tokens += int(tokens)
        if self.estimate_usd > 0 and self.spent_usd > self.cap_usd:
            raise SpendGuardExceeded(
                f"This run spent ${self.spent_usd:.4f} against a pre-run estimate of "
                f"${self.estimate_usd:.4f} (hard stop at ${self.cap_usd:.4f} = 2×). "
                "Committed parts are kept. Start a new run after reviewing the estimate.",
                spent=self.spent_usd, estimate=self.estimate_usd, cap=self.cap_usd,
            )
        if self.estimate_usd <= 0 and self.estimate_tokens > 0 and self.spent_tokens > self.cap_tokens:
            raise SpendGuardExceeded(
                f"This run used {self.spent_tokens:,} tokens against a pre-run estimate of "
                f"{self.estimate_tokens:,} (hard stop at {self.cap_tokens:,} = 2×). "
                "Committed parts are kept. Start a new run after reviewing the estimate.",
                spent=Decimal(self.spent_tokens), estimate=Decimal(self.estimate_tokens),
                cap=Decimal(self.cap_tokens),
            )


_current: contextvars.ContextVar[SpendGuard | None] = contextvars.ContextVar("govcon_spend_guard", default=None)


@contextmanager
def hold_spend_guard(estimate_usd: Decimal | float | int, estimate_tokens: int) -> Iterator[SpendGuard]:
    guard = SpendGuard(estimate_usd=Decimal(str(estimate_usd)), estimate_tokens=int(estimate_tokens))
    token = _current.set(guard)
    try:
        yield guard
    finally:
        _current.reset(token)


def note_spend(*, cost_usd: Decimal | None = None, tokens: int | None = None) -> None:
    guard = _current.get()
    if guard is not None:
        guard.charge(cost_usd=cost_usd, tokens=tokens)


def estimate_review(session: Session, opportunity_id: int, settings: Settings) -> dict[str, int | float | str | None]:
    """Pages still needing a model call, priced from the routed model's row."""
    from govcon.ai.routing import describe_route
    from govcon.ai.usage_log import ReportedTokens, cost_for, price_for

    pages = session.scalar(
        select(func.count()).select_from(FilePage).join(StoredFile, FilePage.file_id == StoredFile.id).where(
            StoredFile.opportunity_id == opportunity_id, StoredFile.active.is_(True),
        )
    ) or 0
    if pages == 0:
        files = session.scalar(
            select(func.count()).select_from(StoredFile).where(
                StoredFile.opportunity_id == opportunity_id, StoredFile.active.is_(True),
                StoredFile.extraction_status.in_(["success", "partial"]),
            )
        ) or 0
        pages = int(files)
    cached = sum(
        1 for row in session.scalars(
            select(AIAnalysis).where(AIAnalysis.opportunity_id == opportunity_id)
        )
        if (row.generation_settings or {}).get("role") == "part"
    )
    remaining = max(int(pages) - int(cached), 0)
    # Extraction + summary each read the pages; cached parts are not re-sent.
    calls = max(remaining, 1) if pages else 0
    input_per = 4_000
    output_per = int(settings.ai_max_output_tokens_per_call)
    input_tokens = calls * input_per
    output_tokens = calls * output_per
    cached_tokens = int(cached) * input_per
    tokens = input_tokens + output_tokens
    route = describe_route(session, settings)
    provider = str(route["analysis_provider"])
    model = str(route["analysis_model"])
    price = price_for(session, provider, model)
    cached_for_price = cached_tokens if cached_tokens and price is not None and price.cached_usd_per_million is not None else None
    priced = cost_for(
        ReportedTokens(input_tokens or None, output_tokens or None, cached_for_price, None),
        price,
    )
    usd: float | None
    if priced is not None:
        usd = float(priced)
    elif settings.ai_budget_usd_per_million_tokens is not None and tokens:
        usd = tokens * float(settings.ai_budget_usd_per_million_tokens) / 1_000_000
    elif price is not None and tokens == 0:
        usd = 0.0
    else:
        usd = None
    return {
        "pages": int(pages),
        "cached_parts": int(cached),
        "calls": calls,
        "tokens": tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "provider": provider,
        "model": model,
        "usd": usd,
        "cap_usd": None if usd is None else usd * 2,
        "cap_tokens": tokens * 2,
        "note": (
            f"About {calls} model call(s) for unread pages; {int(cached)} cached part(s) "
            f"reused and not re-billed at full input rate ({provider} {model}). "
            "The run stops if spend exceeds twice this estimate."
        ),
    }
