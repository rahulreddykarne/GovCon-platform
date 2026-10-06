"""``quote_extraction``: read a supplier's PDF quote with AI, when explicitly authorized (ADR-071).

Supplier quotes are PROPRIETARY. Sending one to an external AI provider needs
both ``AI_EXTERNAL_ALLOWED_FOR_PROPRIETARY`` (checked by the gateway) and an
owner's in-app authorization for that provider that has not expired or been
revoked (roadmap §6.4). Without it the task waits for input and the person
can enter the quote by hand instead.

Steps:
1. ``read``: extract the document's text (OCR included) with no transaction open.
2. ``extract``: check the authorization, call the provider outside any
   transaction, then store the transcribed lines as a quote.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.ai.structured import (
    PreparedCall,
    execute_prepared_call,
    persist_structured_result,
    prepare_structured_call,
)
from govcon.db import shared_session_factory
from govcon.models import Task, User
from govcon.security.classification import DataClassification
from govcon.tasks.errors import TaskBlocked, TaskFailedPermanently
from govcon.tasks.registry import (
    Step,
    StepContext,
    TaskHandler,
    register,
    required_opportunity_id,
)

QUOTE_EXTRACTION_TASK = "quote_extraction"
_AUTH_ACTION = (
    "An owner can authorize AI reading of supplier quotes for this provider on the Settings page, "
    "or enter the quote's lines by hand on the Products tab."
)


def _read_prepare(session: Session, task: Task, ctx: StepContext) -> dict[str, Any]:
    return dict(ctx.payload)


def _read_execute(payload: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
    from govcon.enrich.extract import extract_text, guess_mime_type
    from govcon.enrich.ocr import ocr_config
    from govcon.enrich.storage import get_store

    data = get_store(ctx.settings).read(payload["source_key"])
    if data is None:
        raise TaskFailedPermanently("the stored quote document is missing", owner_role="approver",
                                    next_action="Upload the quote document again.")
    result = extract_text(data, guess_mime_type(payload["source_filename"]), payload["source_filename"],
                          ocr=ocr_config(ctx.settings))
    if not (result.text or "").strip():
        raise TaskFailedPermanently(f"no text could be read from the quote ({result.error or result.status})",
                                    owner_role="approver", next_action="Enter the quote's lines by hand.")
    return {"text": result.text}


def _read_publish(session: Session, task: Task, output: dict[str, Any], ctx: StepContext) -> None:
    ctx.checkpoint_data = output


@dataclass
class _Call:
    prepared: PreparedCall
    executed: Any = None


def _extract_prepare(session: Session, task: Task, ctx: StepContext) -> _Call:
    from govcon.sourcing.records import active_authorization

    provider = (ctx.settings.ai_primary_provider or "").strip().lower()
    if not provider or active_authorization(session, provider=provider) is None:
        raise TaskBlocked(f"no current authorization to send supplier quotes to {provider or 'an AI provider'}",
                          owner_role="owner", next_action=_AUTH_ACTION)
    text = ((task.checkpoint or {}).get("data") or {}).get("read", {}).get("text") or ""
    return _Call(prepared=prepare_structured_call(
        session, opportunity_id=required_opportunity_id(task.opportunity_id), prompt_name="supplier_quote_extraction",
        analysis_type=AnalysisType.QUOTE_EXTRACTION, variables={"QUOTE_TEXT": text},
        context_manifest={"source_sha256": ctx.payload.get("source_sha256"),
                          "source_filename": ctx.payload.get("source_filename")},
        settings=ctx.settings, classification=DataClassification.PROPRIETARY,
    ))


def _extract_execute(call: _Call, ctx: StepContext) -> _Call:
    engine = shared_session_factory(ctx.settings).kw["bind"]
    call.executed = execute_prepared_call(call.prepared, settings=ctx.settings, engine=engine)
    return call


def _extract_publish(session: Session, task: Task, call: _Call, ctx: StepContext) -> None:
    from govcon.matching.pricing import canonical_nsn
    from govcon.sourcing.records import record_quote

    analysis = persist_structured_result(session, call.prepared, call.executed).analysis
    output = call.executed.output
    lines = [{
        "description": line.description, "part_number": line.part_number, "nsn": canonical_nsn(line.nsn or ""),
        "quantity": line.quantity, "unit": line.unit, "unit_price": line.unit_price,
        "extended_price": line.extended_price, "lead_time_days": line.lead_time_days,
    } for line in output.lines]
    actor = session.get(User, task.created_by_user_id) if task.created_by_user_id else None
    payload = ctx.payload
    quote = record_quote(
        session, opportunity_id=required_opportunity_id(task.opportunity_id), supplier_id=payload["supplier_id"], actor=actor, method="ai",
        lines=lines, total_price=output.total_price,
        valid_until=payload.get("valid_until") or _iso_date(output.valid_until),
        source={k: payload.get(k) for k in ("source_filename", "source_sha256", "source_key")},
        notes=("Read by AI; verify against the document. Missing: " + "; ".join(output.missing_information))
        if output.missing_information else "Read by AI; verify against the document.",
    )
    ctx.result = {"quote_id": quote.id, "analysis_id": analysis.id, "lines": len(lines)}


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    from datetime import date

    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None  # an unparseable date stays unknown rather than guessed


register(TaskHandler(task_type=QUOTE_EXTRACTION_TASK, steps=[
    Step("read", prepare=_read_prepare, execute=_read_execute, publish=_read_publish, timeout_seconds=1800),
    Step("extract", prepare=_extract_prepare, execute=_extract_execute, publish=_extract_publish, timeout_seconds=600),
]))
