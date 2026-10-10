"""Cited model prices. Rows are inserted by migration and stay editable in the database.

Amounts are USD per 1,000,000 tokens; the web search fee is USD per 1,000
searches. A model that is not listed here has no configured price; callers
must show "price not set" rather than zero.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

# Read from the provider pages on 2026-10-10. DeepSeek off-peak is the stored
# rate; the page says peak (Mon–Fri 01:00–04:00 and 06:00–10:00 UTC, excluding
# Chinese public holidays) is double, and this catalog does not apply that schedule.
_DEEPSEEK = "https://api-docs.deepseek.com/quick_start/pricing"
_DEEPSEEK_AS_OF = date(2026, 10, 10)
_DEEPSEEK_NOTE = (
    "Off-peak USD per 1M tokens from the official Models & Pricing page. "
    "Peak hours are double these rates and are not applied by this row. "
    "deepseek-v4-flash is billed at the deepseek-flash price."
)

# Anthropic list prices. Cache-write is the 5-minute write rate. The 1-hour
# write rate is different and is not this column. Effective date is the
# 2026-10-07 list card; the same figures, and the web search fee, were on the
# docs page on 2026-10-10.
_ANTHROPIC = "https://platform.claude.com/docs/en/about-claude/pricing"
_ANTHROPIC_AS_OF = date(2026, 10, 7)
_ANTHROPIC_NOTE = (
    "Base input, output, cache-hit, and 5-minute cache-write rates. "
    "1-hour cache writes are a different published rate and are not this column. "
    "Web search is $10 per 1,000 searches on top of tokens; web fetch has no extra charge."
)
_ANTHROPIC_WEB_SEARCH = "10"


def _row(
    provider: str,
    model: str,
    input_price: str,
    output_price: str,
    cached: str | None,
    cache_write: str | None,
    source_url: str,
    effective_as_of: date,
    note: str,
    web_search: str | None = None,
) -> dict[str, object]:
    return {
        "provider": provider,
        "model": model,
        "input_usd_per_million": Decimal(input_price),
        "output_usd_per_million": Decimal(output_price),
        "cached_usd_per_million": Decimal(cached) if cached is not None else None,
        "cache_write_usd_per_million": Decimal(cache_write) if cache_write is not None else None,
        "web_search_usd_per_thousand": Decimal(web_search) if web_search is not None else None,
        "source_url": source_url,
        "effective_as_of": effective_as_of,
        "note": note,
    }


def _anthropic(model: str, input_price: str, output_price: str, cached: str, cache_write: str) -> dict[str, object]:
    return _row("anthropic", model, input_price, output_price, cached, cache_write, _ANTHROPIC, _ANTHROPIC_AS_OF,
                _ANTHROPIC_NOTE, _ANTHROPIC_WEB_SEARCH)


SEED_PRICES: tuple[dict[str, object], ...] = (
    _row("deepseek", "deepseek-flash", "0.15", "0.6", "0.003", None, _DEEPSEEK, _DEEPSEEK_AS_OF, _DEEPSEEK_NOTE),
    _row("deepseek", "deepseek-v4-flash", "0.15", "0.6", "0.003", None, _DEEPSEEK, _DEEPSEEK_AS_OF, _DEEPSEEK_NOTE),
    _row("deepseek", "deepseek-v4-pro", "0.66", "1.98", "0.022", None, _DEEPSEEK, _DEEPSEEK_AS_OF, _DEEPSEEK_NOTE),
    _anthropic("claude-sonnet-5-5", "2", "10", "0.10", "2.50"),
    _anthropic("claude-opus-5-5", "4", "20", "0.20", "5"),
    _anthropic("claude-opus-5", "5", "25", "0.50", "6.25"),
    _anthropic("claude-fable-5", "10", "50", "1", "12.50"),
    _anthropic("claude-fable-5-1", "10", "50", "0.25", "12.50"),
)
