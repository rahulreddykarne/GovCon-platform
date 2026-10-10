"""Reserve each provider attempt before sending; retain uncertain charges.

Accounting commits on its own connection, so invalid output, business rollback,
process failure and simultaneous requests cannot reset an opportunity's budget.
The UTF-8 byte estimate is deliberately conservative without model tokenizers.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Connection, Engine, func, select, text
from sqlalchemy.orm import Session

from govcon.config import Settings
from govcon.models import AICallUsage


class AIBudgetExceeded(RuntimeError):
    pass


# Proposal-stage prompts. They may spend the share of an opportunity's budget
# (``AI_PROPOSAL_BUDGET_SHARE``) that preparation and research may not.
PROPOSAL_PURPOSES = frozenset({"proposal_drafting", "proposal_red_team", "proposal_coverage",
                               "submission_preflight_ai"})


def budget_fingerprint(settings: Settings) -> str:
    """Identifies the configured limits; a budget-blocked task resumes when it changes."""
    limits = (settings.ai_max_input_tokens_per_opportunity, settings.ai_max_input_tokens_per_call,
              settings.ai_max_output_tokens_per_call, settings.ai_max_cost_usd_per_opportunity,
              settings.ai_budget_usd_per_million_tokens, settings.ai_proposal_budget_share)
    return hashlib.sha256(repr(limits).encode()).hexdigest()[:16]


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


def reserve(session: Session | None, *, opportunity_id: int | None, settings: Settings,
            system_prompt: str, user_prompt: str, purpose: str, provider: str,
            model: str | None = None, engine: Engine | Connection | None = None) -> Reservation | None:
    """Reserve one attempt. ``engine`` serves callers that hold no session.

    Workers pass ``engine`` so provider calls run with no business transaction
    open (ADR-061); accounting commits on its own connection either way.
    """
    tokens = input_bound(system_prompt, user_prompt)
    if tokens > min(settings.ai_max_input_tokens_per_call, settings.ai_max_input_tokens_per_opportunity):
        raise AIBudgetExceeded("Combined context exceeds the input limit; no source text was sent. Reduce the source set or explicitly chunk it; omitted sources remain unreviewed.")
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
    cost = Decimal(tokens + settings.ai_max_output_tokens_per_call) * rate / 1_000_000 if rate is not None else None
    with Session(engine) as db, db.begin():
        if opportunity_id is not None:
            if engine.dialect.name != "postgresql":
                raise AIBudgetExceeded("Opportunity reservations require PostgreSQL atomic budget locking.")
            key = int.from_bytes(hashlib.sha256(f"govcon:ai-budget:{opportunity_id}".encode()).digest()[:8], "big", signed=True)
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
            totals = db.execute(select(
                func.coalesce(func.sum(AICallUsage.input_tokens), 0),
                func.coalesce(func.sum(AICallUsage.output_tokens), 0),
                func.coalesce(func.sum(AICallUsage.cost_usd), 0),
                func.coalesce(func.sum(AICallUsage.input_tokens + AICallUsage.output_tokens).filter(AICallUsage.cost_usd.is_(None)), 0),
            ).where(AICallUsage.opportunity_id == opportunity_id)).one()
            share = _spendable_share(settings, purpose)
            kept = (f" The last {settings.ai_proposal_budget_share:.0%} is kept for proposal drafting and review "
                    "(AI_PROPOSAL_BUDGET_SHARE).") if share < 1 else ""
            if totals[0] + tokens > settings.ai_max_input_tokens_per_opportunity * share:
                raise AIBudgetExceeded("Opportunity input budget exhausted, including previous calls and retries." + kept)
            if settings.ai_max_cost_usd_per_opportunity is not None:
                if rate is None or cost is None:
                    raise AIBudgetExceeded("A dollar budget requires AI_BUDGET_USD_PER_MILLION_TOKENS covering all enabled models.")
                previous_cost = Decimal(totals[2] or 0) + Decimal(totals[3]) * rate / 1_000_000
                if previous_cost + cost > Decimal(str(settings.ai_max_cost_usd_per_opportunity)) * share:
                    raise AIBudgetExceeded("Opportunity dollar budget exhausted, including pending reservations." + kept)
        row = AICallUsage(opportunity_id=opportunity_id, purpose=purpose, provider=provider,
                         model=model, status="reserved", input_tokens=tokens,
                         output_tokens=settings.ai_max_output_tokens_per_call, cost_usd=cost)
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


def complete_with_budget(provider, session: Session | None, *, opportunity_id: int | None,
                         settings: Settings, engine: Engine | None = None, **kwargs):
    from govcon.ai.gateway import authorize_external_call
    from govcon.security.classification import opportunity_classification
    if session is not None and opportunity_id is not None:
        kwargs["classification"] = opportunity_classification(session, opportunity_id, kwargs["classification"])
    try:
        authorize_external_call(classification=kwargs["classification"], provider=provider.name,
                                model=kwargs.get("model") or "default", purpose=kwargs["purpose"], settings=settings)
    except Exception as exc:
        from govcon.ai.gateway import AIGatewayBlocked
        if isinstance(exc, AIGatewayBlocked):
            from govcon.ai.usage_log import record_call
            record_call(session, provider=str(provider.name), purpose=kwargs["purpose"], status="blocked",
                        model=kwargs.get("model"), opportunity_id=opportunity_id, engine=engine)
        raise
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


def _call_provider(provider, session: Session | None, *, opportunity_id: int | None, settings: Settings,
                   engine: Engine | None, requested_model: str | None, kwargs: dict):
    for attempt in range(1 + settings.ai_max_provider_retries):
        reservation = reserve(session, opportunity_id=opportunity_id, settings=settings,
                              system_prompt=kwargs["system_prompt"], user_prompt=kwargs["user_prompt"],
                              purpose=kwargs["purpose"], provider=str(provider.name), model=requested_model,
                              engine=engine)
        try:
            result = provider.complete(**kwargs, max_tokens=settings.ai_max_output_tokens_per_call)
        except Exception as exc:
            if reservation is not None:
                reservation.finish()
            from govcon.ai.usage_log import record_call
            record_call(session, provider=str(provider.name), purpose=kwargs["purpose"], status="failed",
                        model=requested_model, opportunity_id=opportunity_id, engine=engine,
                        latency_ms=getattr(exc, "latency_ms", None))
            if attempt < settings.ai_max_provider_retries and _retryable(exc):
                time.sleep(0.5 * 2 ** attempt)
                continue
            raise
        if reservation is not None:
            reservation.finish(result)
        from govcon.ai.usage_log import record_call
        call_id = record_call(
            session, provider=str(getattr(result, "provider", None) or provider.name),
            purpose=kwargs["purpose"], status="succeeded",
            model=getattr(result, "model", None) or requested_model,
            opportunity_id=opportunity_id, usage=getattr(result, "usage", None),
            latency_ms=getattr(result, "latency_ms", None), engine=engine,
        )
        try:
            setattr(result, "usage_call_id", call_id)
        except (AttributeError, TypeError):
            pass
        return result, reservation
    raise AssertionError("unreachable provider retry state")
