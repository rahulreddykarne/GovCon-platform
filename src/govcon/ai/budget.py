"""Reserve each provider attempt before sending; retain uncertain charges.

Accounting commits on its own connection, so invalid output, business rollback,
process failure and simultaneous requests cannot reset an opportunity's budget.
The UTF-8 byte estimate is deliberately conservative without model tokenizers.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Connection, Engine, func, select, text
from sqlalchemy.orm import Session

from govcon.config import Settings
from govcon.diagnostics import diagnostic_event, trace_phase
from govcon.models import AICallUsage, Opportunity


class AIBudgetExceeded(RuntimeError):
    """Raised when a call would exceed a configured token or dollar cap.

    ``used`` / ``limit`` / ``spendable`` are set for opportunity input-budget
    exhaustion so the UI can show exact numbers.
    """

    def __init__(
        self,
        message: str,
        *,
        used: int | None = None,
        limit: int | None = None,
        spendable: int | None = None,
        requested: int | None = None,
    ) -> None:
        super().__init__(message)
        self.used = used
        self.limit = limit
        self.spendable = spendable
        self.requested = requested


# Proposal-stage prompts. They may spend the share of an opportunity's budget
# (``AI_PROPOSAL_BUDGET_SHARE``) that preparation and research may not.
PROPOSAL_PURPOSES = frozenset({"proposal_drafting", "proposal_red_team", "proposal_coverage",
                               "submission_preflight_ai"})

# Provider stop reasons meaning the completion hit the output-token cap.
TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens"})

# Models that accept a larger completion than the DeepSeek-era 8,192 default.
_PROVIDER_OUTPUT_FLOORS = {
    "anthropic": 16_384,
    "openai": 16_384,
}


def output_token_limit(settings: Settings, provider_name: str | None) -> int:
    """Per-call output cap: the configured value, raised where the model allows it."""
    configured = settings.ai_max_output_tokens_per_call
    floor = _PROVIDER_OUTPUT_FLOORS.get((provider_name or "").strip().lower())
    return max(configured, floor) if floor is not None else configured


def ledger_status_for_result(result: object) -> str:
    """A capped completion is not a plain success; the ledger must say truncated."""
    reason = getattr(result, "finish_reason", None)
    if reason in TRUNCATED_FINISH_REASONS:
        return "truncated"
    return "succeeded"


def budget_fingerprint(settings: Settings) -> str:
    """Identifies the configured limits. Changing them does not auto-start parked tasks."""
    limits = (settings.ai_max_input_tokens_per_opportunity, settings.ai_max_input_tokens_per_call,
              settings.ai_max_output_tokens_per_call, settings.ai_max_cost_usd_per_opportunity,
              settings.ai_budget_usd_per_million_tokens, settings.ai_proposal_budget_share)
    return hashlib.sha256(repr(limits).encode()).hexdigest()[:16]


def opportunity_input_limit(session: Session, opportunity_id: int | None, settings: Settings) -> int:
    """Per-opportunity override when set; otherwise the process default (not silently changed)."""
    if opportunity_id is not None:
        opp = session.get(Opportunity, opportunity_id)
        if opp is not None and opp.ai_max_input_tokens is not None and opp.ai_max_input_tokens > 0:
            return int(opp.ai_max_input_tokens)
    return settings.ai_max_input_tokens_per_opportunity


def _counts_toward_budget(row: AICallUsage) -> bool:
    """A truncated parent that was split and retried is not counted again."""
    usage = row.usage if isinstance(row.usage, dict) else {}
    return usage.get("replaced_by_split") is not True


def opportunity_input_used(session: Session, opportunity_id: int) -> int:
    """Lifetime input tokens that still count against the opportunity cap."""
    rows = session.scalars(select(AICallUsage).where(AICallUsage.opportunity_id == opportunity_id)).all()
    return sum(int(row.input_tokens or 0) for row in rows if _counts_toward_budget(row))


def opportunity_budget_status(session: Session, opportunity_id: int, settings: Settings) -> dict[str, int | float | None]:
    """Used / limit numbers for the workspace budget panel."""
    used = opportunity_input_used(session, opportunity_id)
    limit = opportunity_input_limit(session, opportunity_id, settings)
    share = float(_spendable_share(settings, "solicitation_analysis"))
    spendable = int(limit * share)
    opp = session.get(Opportunity, opportunity_id)
    return {
        "used": used,
        "limit": limit,
        "spendable": spendable,
        "share": share,
        "override": None if opp is None else opp.ai_max_input_tokens,
    }


def mark_replaced_by_split(reservation: Reservation | None) -> None:
    """The truncated parent reservation must not count against the split children."""
    if reservation is None:
        return
    with Session(reservation.engine) as db, db.begin():
        row = db.get(AICallUsage, reservation.id, with_for_update=True)
        if row is None:
            return
        usage = dict(row.usage or {})
        usage["replaced_by_split"] = True
        row.usage = usage


def _spendable_share(settings: Settings, purpose: str) -> Decimal:
    if purpose in PROPOSAL_PURPOSES:
        return Decimal(1)
    return Decimal(1) - Decimal(str(settings.ai_proposal_budget_share))


def input_bound(system_prompt: str, user_prompt: str) -> int:
    # Covers message framing and the providers' extra JSON instructions.
    return len(system_prompt.encode("utf-8")) + len(user_prompt.encode("utf-8")) + 256


def _engine_for_accounting(bind: Engine | Connection) -> Engine:
    """The engine behind a session bind, so accounting opens its own connection."""
    if isinstance(bind, Connection):
        return bind.engine
    return bind


def _decimal_amount(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    return Decimal(0)


@dataclass
class Reservation:
    engine: Engine | Connection
    id: int
    rate: Decimal | None
    cost: Decimal | None

    def finish(self, result: object | None = None) -> None:
        usage_attr = getattr(result, "usage", None)
        usage = usage_attr if isinstance(usage_attr, dict) else {}
        with Session(self.engine) as db, db.begin():
            row = db.get(AICallUsage, self.id, with_for_update=True)
            if row is None:
                raise RuntimeError("AI usage reservation no longer exists; review budget accounting before retrying")
            if row.status != "reserved":
                return
            row.status = "succeeded" if result is not None else "failed"
            row.usage = usage or None
            if result is not None:
                # Missing or malformed usage retains its full reservation.
                inp = usage.get("prompt_tokens", usage.get("input_tokens"))
                out = usage.get("completion_tokens", usage.get("output_tokens"))
                total = usage.get("total_tokens")
                # A trustworthy total bounds each component even when a
                # provider omits the split; charging it twice is conservative.
                if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                    if inp is None:
                        inp = min(total, row.input_tokens)
                    if out is None:
                        out = min(total, row.output_tokens)
                if isinstance(inp, int) and not isinstance(inp, bool) and inp >= 0:
                    inp += sum(v for k in ("cache_creation_input_tokens", "cache_read_input_tokens")
                               if isinstance((v := usage.get(k)), int) and not isinstance(v, bool) and v >= 0)
                    row.input_tokens = inp
                if isinstance(out, int) and not isinstance(out, bool) and out >= 0:
                    row.output_tokens = out
                row.model = getattr(result, "model", None) or row.model
            if self.rate is not None:
                row.cost_usd = Decimal(row.input_tokens + row.output_tokens) * self.rate / 1_000_000
            self.cost = row.cost_usd


@trace_phase("ai.budget.reserve")
def reserve(session: Session | None, *, opportunity_id: int | None, settings: Settings,
            system_prompt: str, user_prompt: str, purpose: str, provider: str,
            model: str | None = None, engine: Engine | Connection | None = None) -> Reservation | None:
    """Reserve one attempt. ``engine`` serves callers that hold no session.

    Workers pass ``engine`` so provider calls run with no business transaction
    open (ADR-061); accounting commits on its own connection either way.
    """
    tokens = input_bound(system_prompt, user_prompt)
    max_output = output_token_limit(settings, provider)
    diagnostic_event("ai.budget_check", provider=provider, model=model, prompt=purpose,
                     input_tokens=tokens, max_output_tokens=max_output)
    opp_limit = settings.ai_max_input_tokens_per_opportunity
    if session is not None:
        opp_limit = opportunity_input_limit(session, opportunity_id, settings)
    if tokens > min(settings.ai_max_input_tokens_per_call, opp_limit):
        raise AIBudgetExceeded(
            f"Combined context exceeds the input limit ({tokens:,} requested; "
            f"per-call {settings.ai_max_input_tokens_per_call:,}, opportunity {opp_limit:,}); "
            "no source text was sent. Reduce the source set or explicitly chunk it; "
            "omitted sources remain unreviewed.",
            used=0, limit=opp_limit, spendable=opp_limit, requested=tokens,
        )
    rate = Decimal(str(settings.ai_budget_usd_per_million_tokens)) if settings.ai_budget_usd_per_million_tokens is not None else None
    if settings.ai_max_cost_usd_per_opportunity is not None and rate is None:
        raise AIBudgetExceeded("A dollar budget requires AI_BUDGET_USD_PER_MILLION_TOKENS covering all enabled models.")
    if session is None and engine is None:
        if opportunity_id is not None or settings.ai_max_cost_usd_per_opportunity is not None:
            raise AIBudgetExceeded("Persistent budget accounting requires a database session.")
        return None  # Explicit offline evaluations still obey per-call limits.
    if engine is None:
        if session is None:
            raise AIBudgetExceeded("Persistent budget accounting requires a database session.")
        bind = session.get_bind()
        engine = getattr(bind, "engine", bind)
    cost = Decimal(tokens + max_output) * rate / 1_000_000 if rate is not None else None
    with Session(engine) as db, db.begin():
        if opportunity_id is not None:
            if engine.dialect.name != "postgresql":
                raise AIBudgetExceeded("Opportunity reservations require PostgreSQL atomic budget locking.")
            key = int.from_bytes(hashlib.sha256(f"govcon:ai-budget:{opportunity_id}".encode()).digest()[:8], "big", signed=True)
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
            counted = [row for row in db.scalars(
                select(AICallUsage).where(AICallUsage.opportunity_id == opportunity_id)
            ).all() if _counts_toward_budget(row)]
            used_input = sum(int(row.input_tokens or 0) for row in counted)
            used_cost = sum(_decimal_amount(row.cost_usd) for row in counted if row.cost_usd is not None)
            unpriced = sum(int(row.input_tokens or 0) + int(row.output_tokens or 0)
                           for row in counted if row.cost_usd is None)
            opp_limit = opportunity_input_limit(db, opportunity_id, settings)
            share = _spendable_share(settings, purpose)
            spendable = int(opp_limit * share)
            kept = (f" The last {settings.ai_proposal_budget_share:.0%} is kept for proposal drafting and review "
                    "(AI_PROPOSAL_BUDGET_SHARE).") if share < 1 else ""
            if used_input + tokens > spendable:
                raise AIBudgetExceeded(
                    f"Opportunity input budget exhausted: {used_input:,} used of {spendable:,} spendable "
                    f"(limit {opp_limit:,}).{kept} Raise the budget for this opportunity, then resume the task.",
                    used=used_input, limit=opp_limit, spendable=spendable, requested=tokens,
                )
            if settings.ai_max_cost_usd_per_opportunity is not None:
                if rate is None or cost is None:
                    raise AIBudgetExceeded("A dollar budget requires AI_BUDGET_USD_PER_MILLION_TOKENS covering all enabled models.")
                previous_cost = used_cost + Decimal(unpriced) * rate / 1_000_000
                dollar_cap = Decimal(str(settings.ai_max_cost_usd_per_opportunity)) * share
                if previous_cost + cost > dollar_cap:
                    raise AIBudgetExceeded(
                        f"Opportunity dollar budget exhausted: {previous_cost} used of {dollar_cap} spendable."
                        + kept,
                        used=used_input, limit=opp_limit, spendable=spendable, requested=tokens,
                    )
        row = AICallUsage(opportunity_id=opportunity_id, purpose=purpose, provider=provider,
                         model=model, status="reserved", input_tokens=tokens,
                         output_tokens=max_output, cost_usd=cost)
        db.add(row)
        db.flush()
        reservation = Reservation(engine, row.id, rate, cost)
    return reservation


def _retryable(exc: Exception) -> bool:
    import httpx

    from govcon.ai.providers.base import ProviderAPIError
    statuses = {408, 409, 429, 500, 502, 503, 504}
    if isinstance(exc, ProviderAPIError):
        return exc.status_code is None or exc.status_code in statuses
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in statuses
    return isinstance(exc, httpx.TransportError)


def _current_classification(session: Session | None, engine: Engine | None, opportunity_id: int | None,
                            declared):
    """Strictest of the declared class and the opportunity's stored files, read now."""
    from govcon.security.classification import opportunity_classification
    if opportunity_id is None:
        return declared
    if session is not None:
        return opportunity_classification(session, opportunity_id, declared)
    if engine is not None:
        with Session(engine) as db:
            return opportunity_classification(db, opportunity_id, declared)
    return declared


def _authorize_attempt(provider, session: Session | None, *, opportunity_id: int | None, settings: Settings,
                       engine: Engine | None, model: str | None, kwargs: dict) -> None:
    """Gate one attempt. A refusal is recorded as a blocked call with zero tokens, then re-raised."""
    from govcon.ai.gateway import AIGatewayBlocked, authorize_external_call
    kwargs["classification"] = _current_classification(session, engine, opportunity_id, kwargs["classification"])
    try:
        authorize_external_call(classification=kwargs["classification"], provider=provider.name,
                                model=model or "default", purpose=kwargs["purpose"], settings=settings)
    except AIGatewayBlocked:
        from govcon.ai.usage_log import record_call
        record_call(session, provider=str(provider.name), purpose=kwargs["purpose"], status="blocked",
                    model=model, opportunity_id=opportunity_id, engine=engine)
        raise


@trace_phase("ai.budget.complete_with_budget")
def complete_with_budget(provider, session: Session | None, *, opportunity_id: int | None,
                         settings: Settings, engine: Engine | None = None, **kwargs):
    _authorize_attempt(provider, session, opportunity_id=opportunity_id, settings=settings, engine=engine,
                       model=kwargs.get("model"), kwargs=kwargs)
    from govcon.ai.providers import resolve_provider_model
    from govcon.ai.replay import active_recorder
    requested_model = resolve_provider_model(settings, provider_name=provider.name, model=kwargs.get("model"))[1]
    recorder = active_recorder()
    if recorder is not None:
        # A recorded run (ADR-061, DEV-023): the provider is called later with
        # no transaction open, then the run is replayed with its response.
        if engine is None and session is not None:
            engine = _engine_for_accounting(session.get_bind())
        return recorder.call(
            ("provider", str(provider.name), requested_model, kwargs),
            lambda: _call_provider(provider, None, opportunity_id=opportunity_id, settings=settings,
                                   engine=engine, requested_model=requested_model, kwargs=kwargs),
        )
    return _call_provider(provider, session, opportunity_id=opportunity_id, settings=settings, engine=engine,
                          requested_model=requested_model, kwargs=kwargs)


@trace_phase("ai.budget.call_provider")
def _call_provider(provider, session: Session | None, *, opportunity_id: int | None, settings: Settings,
                   engine: Engine | None, requested_model: str | None, kwargs: dict):
    from govcon.ai.usage_log import record_call
    kwargs = dict(kwargs)
    for attempt in range(1 + settings.ai_max_provider_retries):
        diagnostic_event("ai.provider_attempt", provider=str(provider.name), model=requested_model,
                         attempt=attempt + 1, attempts=1 + settings.ai_max_provider_retries)
        # Every attempt, including retries and replayed runs, passes the gateway
        # with the classification as it stands now.
        _authorize_attempt(provider, session, opportunity_id=opportunity_id, settings=settings, engine=engine,
                           model=requested_model, kwargs=kwargs)
        reservation = reserve(session, opportunity_id=opportunity_id, settings=settings,
                              system_prompt=kwargs["system_prompt"], user_prompt=kwargs["user_prompt"],
                              purpose=kwargs["purpose"], provider=str(provider.name), model=requested_model,
                              engine=engine)
        try:
            result = provider.complete(**kwargs, max_tokens=output_token_limit(settings, str(provider.name)))
        except Exception as exc:
            diagnostic_event("ai.provider_failed", level=logging.WARNING, error_type=type(exc).__name__,
                             provider=str(provider.name), model=requested_model, attempt=attempt + 1,
                             http_status=getattr(exc, "status_code", None))
            if reservation is not None:
                reservation.finish()
            from govcon.ai.gateway import AIGatewayBlocked
            record_call(session, provider=str(provider.name), purpose=kwargs["purpose"],
                        status="blocked" if isinstance(exc, AIGatewayBlocked) else "failed",
                        model=requested_model, opportunity_id=opportunity_id, engine=engine,
                        usage=getattr(exc, "usage", None), latency_ms=getattr(exc, "latency_ms", None))
            if attempt < settings.ai_max_provider_retries and _retryable(exc):
                diagnostic_event("ai.provider_retry", attempt=attempt + 1, timeout_seconds=0.5 * 2 ** attempt)
                time.sleep(0.5 * 2 ** attempt)
                continue
            raise
        if reservation is not None:
            reservation.finish(result)
        finish_reason = getattr(result, "finish_reason", None)
        call_id = record_call(
            session, provider=str(getattr(result, "provider", None) or provider.name),
            purpose=kwargs["purpose"], status=ledger_status_for_result(result),
            model=getattr(result, "model", None) or requested_model,
            opportunity_id=opportunity_id, usage=getattr(result, "usage", None),
            latency_ms=getattr(result, "latency_ms", None), engine=engine,
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
        )
        try:
            result.usage_call_id = call_id
        except (AttributeError, TypeError):
            pass
        diagnostic_event("ai.provider_completed", provider=str(provider.name), model=getattr(result, "model", None),
                         latency_ms=getattr(result, "latency_ms", None), attempt=attempt + 1)
        return result, reservation
    raise AssertionError("unreachable provider retry state")
