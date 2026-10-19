"""Per-run SAM.gov request budget shared by search ingest and source documents.

One process (a scheduler chain, a CLI command) gets one budget. After the
first ``api.sam.gov`` HTTP 429, further SAM calls in that run are skipped so
a quota hit does not fan out into dozens of retries. Non-SAM URLs are not
charged and are still fetched.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit

from govcon.config import Settings, get_settings

SAM_API_HOSTS = frozenset({"api.sam.gov"})

_budget: ContextVar[SamRequestBudget | None] = ContextVar("sam_request_budget", default=None)


def is_sam_api_url(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in SAM_API_HOSTS


@dataclass
class SamRequestBudget:
    max_requests: int
    used: int = 0
    rate_limited: bool = False
    retry_after: float | None = None

    def allow(self, url: str) -> bool:
        if not is_sam_api_url(url):
            return True
        if self.rate_limited:
            return False
        return self.used < self.max_requests

    def consume(self, url: str) -> bool:
        """Count a SAM request. Return False when the URL must not be fetched."""
        if not is_sam_api_url(url):
            return True
        if not self.allow(url):
            return False
        self.used += 1
        return True

    def mark_rate_limited(self, retry_after: float | None = None) -> None:
        self.rate_limited = True
        if retry_after is None:
            return
        current = self.retry_after
        self.retry_after = retry_after if current is None else max(current, retry_after)


def current_sam_budget() -> SamRequestBudget | None:
    return _budget.get()


def begin_sam_budget(max_requests: int) -> SamRequestBudget:
    budget = SamRequestBudget(max_requests=max(1, int(max_requests)))
    _budget.set(budget)
    return budget


def reset_sam_budget() -> None:
    _budget.set(None)


def ensure_sam_budget(settings: Settings | None = None) -> SamRequestBudget:
    existing = _budget.get()
    if existing is not None:
        return existing
    settings = settings or get_settings()
    return begin_sam_budget(settings.sam_request_budget_per_run)


def note_sam_rate_limited(retry_after: float | None = None, settings: Settings | None = None) -> SamRequestBudget:
    budget = ensure_sam_budget(settings)
    budget.mark_rate_limited(retry_after)
    return budget
