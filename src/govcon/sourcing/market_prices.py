"""Web market price research for pursued product opportunities.

Claude searches the web (Anthropic's ``web_search`` and ``web_fetch`` server
tools) for commercial listings of the solicitation's product and returns at
most ``MARKET_PRICE_MAX_RESULTS`` (5) priced listings. The median unit price
of the listings that match the specification is an **estimated cost**: margin
math uses it until a supplier quote or a recorded sourcing cost exists
(:func:`effective_cost_basis`). A web listing is never treated as a quote.

It runs as a task step (ADR-061 pattern), in preparation after compliance and
before research, and as the standalone ``market_price_research`` task the
Products tab queues:

1. :func:`prepare_step` (in a transaction): decide whether the opportunity is
   a product, build the product description from the solicitation summary and
   its sourcing requirements, and check policy and configuration.
2. :func:`execute_step` (no transaction open): one budgeted Claude call.
3. :func:`publish_step`: store a :class:`MarketPriceRun`, whatever the outcome.

Research never stops preparation: a skipped, blocked or failed search is
stored with its reason and preparation continues.

Government sites (``.gov``, ``.mil``, ``.fed.us``) are excluded twice: the
main procurement sites are blocked in the search tools, and any listing on a
government host is dropped from the answer before it is stored.

Logging (``govcon.sourcing.market_prices``): each run logs why it was
skipped, the search it sent (model, limits, product, never the prompt), the
response (latency, searches, tokens, listings), every excluded listing with
its domain and reason, and the estimate. Provider failures log the status
only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from statistics import median
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.config import Settings
from govcon.models import AIAnalysis, MarketPriceRun, Opportunity, Pursuit, Task
from govcon.security.classification import DataClassification

logger = logging.getLogger("govcon.sourcing.market_prices")

MARKET_PRICE_TASK = "market_price_research"
PURPOSE = "market_price_research"
PROMPT_VERSION = "market_price_research.v1"

SYSTEM_PROMPT = """ROLE
You are a commercial price researcher for a government contractor.

OBJECTIVE
Find current prices that commercial sellers publicly list on the web for the
product described in PRODUCT_JSON, so the contractor can estimate its cost.

TOOLS
Use web_search to find listings and web_fetch to read a listing page when the
search result does not show the price or the pack size.

RULES
- Return at most {max_results} listings. Stop searching once you have them.
- Use commercial sellers only: distributors, manufacturers and retailers.
  Never use a government website (.gov, .mil, .fed.us), including GSA
  Advantage, DLA, DIBBS, FedMall or SAM.gov, and never use award or contract
  records as prices.
- Prefer listings that match the specification exactly: NSN or part number,
  manufacturer, material, size, standards (for example ASTM) and pack size.
- Only report a price shown on the listing. Never estimate, average or invent
  a price, a pack size or a seller.
- Prices are in US dollars. Skip listings priced in another currency.
- units_per_listing is how many of the solicitation's UNIT one listing price
  buys. Example: UNIT is BX (box of 100 gloves) and the listing is a case of
  10 boxes for $80.00, so units_per_listing is 10. Leave it null when the
  pack size or the solicitation unit cannot be matched from the page.
- spec_match: "match" when every stated specification is met, "partial" when
  some are met and none contradicted, "mismatch" when any is contradicted,
  "unknown" when the page does not say. List unmet or unverified
  specifications in spec_gaps.
- Do not state stock, delivery time or country of origin unless the page
  states it.

OUTPUT
Return one JSON object:
{{"product_searched": "<short description>",
  "listings": [{{"seller": "", "product_title": "", "url": "https://...",
                "price": 0.0, "currency": "USD", "pack_description": "",
                "units_per_listing": null, "spec_match": "unknown",
                "spec_gaps": [], "availability": null,
                "country_of_origin": null}}],
  "notes": "<anything the contractor should know, or null>"}}
"""

# Hostnames blocked in the search tools (plain hostnames; subdomains are
# covered). Bare TLDs are rejected by the API, so .gov/.mil as a whole are
# filtered again after the search.
BLOCKED_GOVERNMENT_DOMAINS = [
    "sam.gov", "gsa.gov", "gsaadvantage.gov", "usaspending.gov", "fpds.gov", "acquisition.gov",
    "dla.mil", "fedmall.mil", "defense.gov", "army.mil", "navy.mil", "af.mil", "va.gov",
    "unicor.gov", "abilityone.gov", "federalregister.gov", "usa.gov", "grants.gov",
]
GOVERNMENT_SUFFIXES = (".gov", ".mil", ".fed.us")
SPEC_MATCHES = ("match", "partial", "mismatch", "unknown")
_MONEY = Decimal("0.0001")
_MAX_REQUIREMENTS = 12
_MAX_REQUIREMENT_CHARS = 300
# Requirement types that describe the product itself (delivery is not searched).
_PRODUCT_REQUIREMENT_TYPES = frozenset(
    {"technical", "item", "packaging", "quality", "certification", "country_of_origin", "marking"}
)


def system_prompt(settings: Settings) -> str:
    return SYSTEM_PROMPT.format(max_results=settings.market_price_max_results)


def prompt_sha256(settings: Settings) -> str:
    return hashlib.sha256(system_prompt(settings).encode("utf-8")).hexdigest()


# ── answer schema ─────────────────────────────────────────────────────────────

class _Listing(BaseModel):
    model_config = ConfigDict(extra="ignore")

    seller: str = Field(min_length=1, max_length=200)
    product_title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=8, max_length=2000)
    price: float = Field(gt=0, allow_inf_nan=False)
    currency: str = "USD"
    pack_description: str | None = Field(default=None, max_length=500)
    units_per_listing: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    spec_match: Literal["match", "partial", "mismatch", "unknown"] = "unknown"
    spec_gaps: list[str] = Field(default_factory=list)
    availability: str | None = Field(default=None, max_length=200)
    country_of_origin: str | None = Field(default=None, max_length=100)


# ── step data ─────────────────────────────────────────────────────────────────

@dataclass
class ResearchInput:
    opportunity_id: int
    task_id: int | None
    source_revision: str | None
    status: str = "ready"  # ready | skipped | blocked
    note: str | None = None
    product: dict[str, Any] = field(default_factory=dict)
    quantity: Decimal | None = None
    unit: str | None = None
    classification: str = DataClassification.PUBLIC.value
    model: str | None = None


@dataclass
class ResearchOutcome:
    input: ResearchInput
    status: str
    note: str | None = None
    listings: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int | None = None
    product_searched: str | None = None


# ── 1. prepare ────────────────────────────────────────────────────────────────

def _current_summary(session: Session, opportunity: Opportunity) -> AIAnalysis | None:
    from govcon.ai.analysis_types import AnalysisType
    from govcon.workflow.source_revision import (
        current_source_revision,
        is_stale,
        stamp_of,
    )

    analysis = session.scalar(select(AIAnalysis).where(
        AIAnalysis.opportunity_id == opportunity.id,
        AIAnalysis.analysis_type == AnalysisType.SOLICITATION_SUMMARY,
        AIAnalysis.schema_version == "solicitation_analysis.v1",
    ).order_by(AIAnalysis.created_at.desc(), AIAnalysis.id.desc()).limit(1))
    if analysis is None or is_stale(stamp_of(analysis.context_manifest), current_source_revision(session, opportunity.id)):
        return None
    return analysis


def _summary_items(analysis: AIAnalysis | None) -> tuple[list[Any], dict[str, Any]]:
    from govcon.ai.schemas import SolicitationAnalysisV1

    if analysis is None:
        return [], {}
    try:
        summary = SolicitationAnalysisV1.model_validate(analysis.output_json)
    except ValueError:
        return [], {}
    items = [item for item in summary.items if item.description or item.part_number or item.nsn]
    return items, {
        "country_of_origin": summary.country_of_origin_references[:3],
        "certifications": summary.certifications[:5],
    }


def product_reason(opportunity: Opportunity, items: list[Any]) -> str | None:
    """Why the opportunity is for a product, or None when it is not."""
    from govcon.matching.pricing import canonical_nsn

    if (opportunity.source or "").lower() == "dibbs":
        return "DIBBS request for quotation"
    if canonical_nsn(opportunity.nsn or ""):
        return "national stock number"
    psc = (opportunity.psc_code or "").strip()
    if psc[:1].isdigit():
        return f"product service code {psc}"
    if psc[:1].isalpha():
        return None  # service PSCs start with a letter
    if any(item.nsn or item.part_number or item.manufacturer for item in items):
        return "solicitation line items identify a product"
    return None


def _primary_item(items: list[Any], nsn: str | None) -> Any | None:
    from govcon.matching.pricing import canonical_nsn

    if nsn:
        for item in items:
            if canonical_nsn(item.nsn or "") == nsn:
                return item
    return items[0] if items else None


def build_research_input(session: Session, opportunity_id: int, settings: Settings, *,
                         task_id: int | None = None) -> ResearchInput:
    """Everything the search needs, read in one transaction."""
    from govcon.ai.gateway import external_call_allowed
    from govcon.compliance.matrix import active_requirements
    from govcon.security.classification import opportunity_classification
    from govcon.sourcing.product_facts import product_facts_from_summary
    from govcon.workflow.source_revision import current_source_revision

    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise ValueError(f"opportunity {opportunity_id} not found")
    revision = current_source_revision(session, opportunity_id)
    inp = ResearchInput(opportunity_id=opportunity_id, task_id=task_id,
                        source_revision=str(revision) if revision is not None else None)

    if not settings.market_price_research_enabled:
        return _skip(inp, "market price research is turned off (MARKET_PRICE_RESEARCH_ENABLED=false)")
    analysis = _current_summary(session, opportunity)
    items, extras = _summary_items(analysis)
    reason = product_reason(opportunity, items)
    if reason is None:
        psc = opportunity.psc_code or "none"
        return _skip(inp, f"not a product opportunity (PSC {psc}); no web price search")
    if not settings.anthropic_api_key:
        return _skip(inp, "ANTHROPIC_API_KEY is not set; web price search needs Claude web search")

    facts = product_facts_from_summary(opportunity, analysis)
    item = _primary_item(items, facts.nsn)
    requirements = [
        r.requirement_text.strip()[:_MAX_REQUIREMENT_CHARS]
        for r in active_requirements(session, opportunity_id)
        if (r.requirement_type or "") in _PRODUCT_REQUIREMENT_TYPES and (r.requirement_text or "").strip()
    ][:_MAX_REQUIREMENTS]
    inp.quantity = facts.quantity if facts.quantity is not None and facts.quantity > 0 else None
    inp.unit = facts.unit
    inp.product = {
        "title": opportunity.title,
        "description": (item.description if item is not None else None) or opportunity.title,
        "nsn": facts.nsn,
        "manufacturer": item.manufacturer if item is not None else None,
        "part_number": item.part_number if item is not None else None,
        "psc": opportunity.psc_code,
        "quantity": str(inp.quantity) if inp.quantity is not None else None,
        "unit": inp.unit,
        "specifications": requirements,
        **extras,
    }
    if len(items) > 1:
        inp.product["other_line_items_not_searched"] = len(items) - 1
    inp.product = {key: value for key, value in inp.product.items() if value not in (None, [], "")}

    classification = opportunity_classification(session, opportunity_id, DataClassification.PUBLIC)
    inp.classification = classification.value
    if not external_call_allowed(classification, settings):
        inp.status, inp.note = "blocked", (
            f"the opportunity's documents are {classification.value}; the data-sharing policy does not allow "
            "sending its product description to an external AI provider")
        logger.info("market price research blocked opportunity=%s classification=%s",
                    opportunity_id, classification.value)
        return inp
    inp.model = settings.market_price_model or settings.anthropic_model
    logger.info("market price research ready opportunity=%s reason=%r product=%r quantity=%s unit=%s specs=%d",
                opportunity_id, reason, inp.product.get("description"), inp.quantity, inp.unit, len(requirements))
    return inp


def _skip(inp: ResearchInput, note: str) -> ResearchInput:
    inp.status, inp.note = "skipped", note
    logger.info("market price research skipped opportunity=%s: %s", inp.opportunity_id, note)
    return inp


# ── 2. execute ────────────────────────────────────────────────────────────────

def server_tools(settings: Settings) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = [{
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": settings.market_price_max_searches,
        "blocked_domains": list(BLOCKED_GOVERNMENT_DOMAINS),
        "user_location": {"type": "approximate", "country": "US"},
    }]
    if settings.market_price_max_fetches:
        tools.append({
            "type": "web_fetch_20260209",
            "name": "web_fetch",
            "max_uses": settings.market_price_max_fetches,
            "blocked_domains": list(BLOCKED_GOVERNMENT_DOMAINS),
            "max_content_tokens": settings.market_price_fetch_max_tokens,
        })
    return tools


def run_research(inp: ResearchInput, settings: Settings, *, engine=None) -> ResearchOutcome:
    """Make the search with no transaction open. Failures become an outcome, never an exception."""
    from govcon.ai.budget import AIBudgetExceeded, complete_with_budget
    from govcon.ai.gateway import AIGatewayBlocked
    from govcon.ai.providers import NoProviderConfigured, get_provider
    from govcon.ai.providers.base import ProviderAPIError, ProviderRefusal

    if inp.status != "ready":
        return ResearchOutcome(input=inp, status=inp.status, note=inp.note)
    started = time.monotonic()
    logger.info("market price search sending opportunity=%s model=%s max_results=%d max_searches=%d max_fetches=%d",
                inp.opportunity_id, inp.model or "provider default", settings.market_price_max_results,
                settings.market_price_max_searches, settings.market_price_max_fetches)
    try:
        provider = get_provider(settings, provider_name="anthropic")
        result, _reservation = complete_with_budget(
            provider, None, opportunity_id=inp.opportunity_id, settings=settings, engine=engine,
            system_prompt=system_prompt(settings),
            user_prompt="PRODUCT_JSON:\n" + json.dumps(inp.product, sort_keys=True, default=str),
            model=inp.model, temperature=0.0, json_mode=True,
            classification=DataClassification(inp.classification), purpose=PURPOSE,
            server_tools=server_tools(settings),
        )
    except NoProviderConfigured as exc:
        return _failed(inp, "skipped", str(exc))
    except (AIGatewayBlocked, AIBudgetExceeded) as exc:
        return _failed(inp, "blocked", f"{type(exc).__name__}: {exc}")
    except ProviderRefusal as exc:
        return _failed(inp, "failed", f"the model declined the search ({exc.category or 'no category'})")
    except ProviderAPIError as exc:
        return _failed(inp, "failed", f"the Anthropic API failed ({exc.status_code or 'connection error'}); "
                                      "re-run the price search from the Products tab")
    except Exception as exc:  # noqa: BLE001  research must never stop preparation
        logger.exception("market price search crashed opportunity=%s", inp.opportunity_id)
        return _failed(inp, "failed", f"unexpected error ({type(exc).__name__})")

    usage = dict(result.usage or {})
    latency_ms = result.latency_ms or int((time.monotonic() - started) * 1000)
    logger.info("market price search answered opportunity=%s model=%s latency_ms=%s searches=%s fetches=%s "
                "input_tokens=%s output_tokens=%s finish=%s",
                inp.opportunity_id, result.model, latency_ms, usage.get("web_search_requests", 0),
                usage.get("web_fetch_requests", 0), usage.get("input_tokens"), usage.get("output_tokens"),
                result.finish_reason)
    parsed = extract_answer(result.content)
    if parsed is None:
        logger.warning("market price search returned no JSON answer opportunity=%s finish=%s chars=%d",
                       inp.opportunity_id, result.finish_reason, len(result.content or ""))
        outcome = ResearchOutcome(input=inp, status="failed", model=result.model, usage=usage, latency_ms=latency_ms,
                                  note="the search answer was not the expected JSON; re-run the price search")
        return outcome
    listings, excluded = clean_listings(parsed.get("listings"), inp, settings.market_price_max_results)
    for item in excluded:
        logger.info("market price listing excluded opportunity=%s domain=%s reason=%s",
                    inp.opportunity_id, item.get("domain"), item.get("reason"))
    status = "completed" if listings else "no_results"
    notes = parsed.get("notes") if isinstance(parsed.get("notes"), str) else None
    searched = parsed.get("product_searched") if isinstance(parsed.get("product_searched"), str) else None
    logger.info("market price search kept opportunity=%s listings=%d excluded=%d", inp.opportunity_id,
                len(listings), len(excluded))
    return ResearchOutcome(input=inp, status=status, note=(notes or "")[:1000] or None, listings=listings,
                           excluded=excluded, model=result.model, usage=usage, latency_ms=latency_ms,
                           product_searched=(searched or "")[:300] or None)


def _failed(inp: ResearchInput, status: str, note: str) -> ResearchOutcome:
    log = logger.warning if status == "failed" else logger.info
    log("market price search %s opportunity=%s: %s", status, inp.opportunity_id, note)
    return ResearchOutcome(input=inp, status=status, note=note)


def extract_answer(content: str | None) -> dict[str, Any] | None:
    """The last JSON object with a ``listings`` key; text around it is ignored."""
    text = content or ""
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict) and "listings" in value:
            found = value
        index = text.find("{", end)
    return found


def is_government_host(host: str | None) -> bool:
    name = (host or "").lower().rstrip(".")
    return name in {"gov", "mil"} or name.endswith(GOVERNMENT_SUFFIXES)


def _decimal(value: float) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number > 0 else None


def clean_listings(raw: Any, inp: ResearchInput, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate each listing; drop government sites, non-USD, duplicates and anything past ``limit``."""
    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        try:
            listing = _Listing.model_validate(entry)
        except ValidationError as exc:
            url = entry.get("url") if isinstance(entry, dict) else None
            excluded.append({"domain": _domain(url), "url": url if isinstance(url, str) else None,
                             "reason": f"invalid listing ({exc.error_count()} field error(s))"})
            continue
        parts = urlsplit(listing.url.strip())
        domain = (parts.hostname or "").lower()
        reason = None
        if parts.scheme not in {"http", "https"} or not domain:
            reason = "not an http(s) link"
        elif is_government_host(domain):
            reason = "government site"
        elif listing.currency.strip().upper() != "USD":
            reason = f"priced in {listing.currency.strip().upper() or 'an unknown currency'}"
        elif listing.url.strip() in seen:
            reason = "duplicate link"
        elif len(kept) >= limit:
            reason = f"over the {limit}-listing limit"
        if reason is not None:
            excluded.append({"domain": domain or None, "url": listing.url.strip(), "seller": listing.seller,
                             "reason": reason})
            continue
        seen.add(listing.url.strip())
        price = _decimal(listing.price)
        units = _decimal(listing.units_per_listing) if listing.units_per_listing is not None else None
        unit_price = (price / units).quantize(_MONEY, ROUND_HALF_UP) if price is not None and units is not None else None
        kept.append({
            "rank": len(kept) + 1,
            "seller": listing.seller.strip(),
            "product_title": listing.product_title.strip(),
            "url": listing.url.strip(),
            "domain": domain,
            "price": str(price) if price is not None else None,
            "currency": "USD",
            "pack_description": listing.pack_description,
            "units_per_listing": str(units) if units is not None else None,
            "unit": inp.unit,
            "unit_price": str(unit_price) if unit_price is not None else None,
            "spec_match": listing.spec_match,
            "spec_gaps": [str(gap)[:200] for gap in listing.spec_gaps[:10]],
            "availability": listing.availability,
            "country_of_origin": listing.country_of_origin,
            "used_in_estimate": False,
        })
    return kept, excluded


def _domain(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    return (urlsplit(url.strip()).hostname or "").lower() or None


# ── estimate ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Estimate:
    unit_cost: Decimal | None
    low: Decimal | None
    high: Decimal | None
    confidence: str | None
    basis: str | None
    total: Decimal | None


def estimate(listings: list[dict[str, Any]], quantity: Decimal | None) -> Estimate:
    """Median unit price of the listings that fit the specification; marks the ones used."""
    priced = [item for item in listings if item.get("unit_price")]
    used = [item for item in priced if item["spec_match"] in ("match", "partial")]
    fallback = False
    if not used:
        used = [item for item in priced if item["spec_match"] == "unknown"]
        fallback = bool(used)
    if not used:
        return Estimate(None, None, None, None, None, None)
    for item in used:
        item["used_in_estimate"] = True
    prices = [Decimal(item["unit_price"]) for item in used]
    unit_cost = Decimal(median(prices)).quantize(_MONEY, ROUND_HALF_UP)
    exact = sum(1 for item in used if item["spec_match"] == "match")
    confidence = "medium" if exact >= 2 else "low"
    basis = (f"median of {len(used)} web listing(s), {exact} matching every stated specification"
             + ("; no listing confirmed the specification" if fallback else "")
             + "; list prices before shipping, tax and volume discounts, not a supplier quote")
    total = (unit_cost * quantity).quantize(Decimal("0.01"), ROUND_HALF_UP) if quantity is not None else None
    return Estimate(unit_cost, min(prices), max(prices), confidence, basis, total)


# ── 3. publish ────────────────────────────────────────────────────────────────

def store_run(session: Session, outcome: ResearchOutcome, settings: Settings) -> MarketPriceRun:
    inp = outcome.input
    est = estimate(outcome.listings, inp.quantity) if outcome.status == "completed" else None
    product = dict(inp.product)
    if outcome.product_searched:
        product["searched_as"] = outcome.product_searched
    run = MarketPriceRun(
        opportunity_id=inp.opportunity_id,
        task_id=inp.task_id,
        status=outcome.status,
        source_revision=inp.source_revision,
        product=product or None,
        provider="anthropic" if outcome.model else None,
        model=outcome.model,
        prompt_version=PROMPT_VERSION if outcome.model else None,
        prompt_sha256=prompt_sha256(settings) if outcome.model else None,
        listings=outcome.listings,
        excluded=outcome.excluded,
        quantity=inp.quantity,
        unit=inp.unit,
        usage=outcome.usage or None,
        latency_ms=outcome.latency_ms,
        note=outcome.note,
    )
    if est is not None and est.unit_cost is not None:
        run.estimate_unit_cost, run.estimate_low, run.estimate_high = est.unit_cost, est.low, est.high
        run.estimate_confidence, run.estimate_basis, run.estimated_total_cost = est.confidence, est.basis, est.total
    session.add(run)
    session.flush()
    logger.info("market price run stored id=%s opportunity=%s status=%s listings=%d estimate_unit_cost=%s "
                "estimated_total_cost=%s confidence=%s", run.id, run.opportunity_id, run.status, len(run.listings),
                run.estimate_unit_cost, run.estimated_total_cost, run.estimate_confidence)
    return run


def run_summary(run: MarketPriceRun) -> str:
    """One line for the preparation panel and the task result."""
    if run.status == "completed":
        if run.estimate_unit_cost is not None:
            per = f" per {run.unit}" if run.unit else " per unit"
            total = f", about ${run.estimated_total_cost:,.2f} in total" if run.estimated_total_cost is not None else ""
            return (f"{len(run.listings)} web price(s) found; estimated cost ${run.estimate_unit_cost:,.2f}{per}"
                    f"{total} ({run.estimate_confidence} confidence)")
        return f"{len(run.listings)} web price(s) found, but none could be converted to the solicitation's unit"
    if run.status == "no_results":
        return "no commercial web prices found" + (f": {run.note}" if run.note else "")
    return f"web price search {run.status}: {run.note or 'no detail'}"


# ── task steps (shared by preparation and the standalone task) ────────────────

def prepare_step(session: Session, task: Task, ctx) -> ResearchInput:
    from govcon.tasks.registry import required_opportunity_id

    return build_research_input(session, required_opportunity_id(task.opportunity_id), ctx.settings, task_id=task.id)


def execute_step(inp: ResearchInput, ctx) -> ResearchOutcome:
    from govcon.db import shared_session_factory

    engine = shared_session_factory(ctx.settings).kw["bind"] if inp.status == "ready" else None
    return run_research(inp, ctx.settings, engine=engine)


def publish_step(session: Session, task: Task, outcome: ResearchOutcome, ctx) -> None:
    run = store_run(session, outcome, ctx.settings)
    summary = run_summary(run)
    ctx.checkpoint_data = {**(ctx.checkpoint_data or {}), "market_price_run_id": run.id, "note": summary}
    ctx.result = {"market_price_run_id": run.id, "status": run.status, "summary": summary}


def queue_market_price_research(session: Session, *, opportunity_id: int, actor_user_id: int | None,
                                settings: Settings | None = None) -> tuple[Task, bool]:
    """Queue a fresh search (the Products tab's re-run)."""
    from govcon.tasks import queue
    from govcon.workflow.source_revision import current_source_revision

    revision = current_source_revision(session, opportunity_id)
    return queue.enqueue(
        session, task_type=MARKET_PRICE_TASK, opportunity_id=opportunity_id,
        input_revision={"source_revision": str(revision), "requested_at": datetime.now(UTC).isoformat()},
        actor_user_id=actor_user_id, settings=settings,
    )


# ── reading the estimate ──────────────────────────────────────────────────────

def latest_run(session: Session, opportunity_id: int) -> MarketPriceRun | None:
    return session.scalar(select(MarketPriceRun).where(MarketPriceRun.opportunity_id == opportunity_id)
                          .order_by(MarketPriceRun.created_at.desc(), MarketPriceRun.id.desc()).limit(1))


def latest_estimate(session: Session, opportunity_id: int) -> MarketPriceRun | None:
    """The newest run with an estimate, while the solicitation it searched is still current."""
    from govcon.workflow.source_revision import current_source_revision

    run = session.scalar(select(MarketPriceRun).where(
        MarketPriceRun.opportunity_id == opportunity_id, MarketPriceRun.status == "completed",
        MarketPriceRun.estimate_unit_cost.is_not(None),
    ).order_by(MarketPriceRun.created_at.desc(), MarketPriceRun.id.desc()).limit(1))
    if run is None:
        return None
    revision = current_source_revision(session, opportunity_id)
    if run.source_revision != (str(revision) if revision is not None else None):
        return None
    return run


@dataclass(frozen=True)
class CostBasis:
    """The cost margin math uses, with where it came from."""

    total: Decimal | None
    unit: Decimal | None
    basis: str | None  # pursuit | supplier_quote | web_estimate | None
    detail: str | None
    market_price_run_id: int | None = None

    @property
    def is_estimate(self) -> bool:
        return self.basis == "web_estimate"


def effective_cost_basis(session: Session, opportunity: Opportunity, pursuit: Pursuit | None,
                         quantity: Decimal | None) -> CostBasis:
    """Recorded cost first, then the lowest current supplier quote (ADR-071), then the web estimate."""
    from govcon.sourcing.records import lowest_current_total

    qty = quantity if quantity is not None and quantity > 0 else None
    if pursuit is not None and pursuit.sourcing_cost is not None:
        cost = Decimal(pursuit.sourcing_cost)
        return CostBasis(cost, cost / qty if qty else None, "pursuit", "pursuit record entered by a user")
    best = lowest_current_total(session, opportunity.id)
    if best is not None and best.total_price is not None:
        cost = Decimal(best.total_price)
        return CostBasis(cost, cost / qty if qty else None, "supplier_quote", f"lowest current supplier quote #{best.id}")
    run = latest_estimate(session, opportunity.id)
    if run is not None:
        unit = Decimal(run.estimate_unit_cost)
        total = Decimal(run.estimated_total_cost) if run.estimated_total_cost is not None else (unit * qty if qty else None)
        return CostBasis(total, unit, "web_estimate",
                         f"web market price estimate (run #{run.id}): {run.estimate_basis}", run.id)
    return CostBasis(None, None, None, None)
