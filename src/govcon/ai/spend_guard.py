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
    """Pages still needing a model call, with a conservative dollar/token estimate."""
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
    tokens_per = 4_000 + int(settings.ai_max_output_tokens_per_call)
    tokens = calls * tokens_per
    rate = None
    if settings.ai_budget_usd_per_million_tokens is not None:
        rate = float(settings.ai_budget_usd_per_million_tokens)
    usd = None if rate is None else tokens * rate / 1_000_000
    return {
        "pages": int(pages),
        "cached_parts": int(cached),
        "calls": calls,
        "tokens": tokens,
        "usd": usd,
        "cap_usd": None if usd is None else usd * 2,
        "cap_tokens": tokens * 2,
        "note": (
            f"About {calls} model call(s) for unread pages; cached parts are reused and not re-billed. "
            "The run stops if spend exceeds twice this estimate."
        ),
    }
