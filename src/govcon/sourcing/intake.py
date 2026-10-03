"""Receiving a supplier's quote document (ADR-071)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from govcon.collaboration.users import require_permission
from govcon.config import Settings, get_settings
from govcon.models import SupplierQuote, Task, User
from govcon.sourcing.records import SourcingError, parse_quote_table, record_quote, store_quote_file

MAX_QUOTE_BYTES = 25 * 1024 * 1024


@dataclass
class QuoteIntake:
    quote: SupplierQuote | None = None
    task: Task | None = None


def receive_quote_file(session: Session, *, opportunity_id: int, supplier_id: int, data: bytes, filename: str,
                       valid_until: str | None, actor: User, settings: Settings | None = None) -> QuoteIntake:
    """Store the document, then read it.

    CSV and XLSX tables are parsed locally and recorded at once. Any other
    document (a PDF quote, a scan) is queued for AI reading, which runs only
    with an owner's authorization for the provider.
    """
    from govcon.tasks import queue

    require_permission(actor, "review")
    settings = settings or get_settings()
    name = (filename or "").strip()
    if not data or not name:
        raise SourcingError("choose a quote file to upload")
    if len(data) > MAX_QUOTE_BYTES:
        raise SourcingError("the quote file is larger than 25 MB")
    source = store_quote_file(data, name, opportunity_id, settings)
    lower = name.lower()
    if lower.endswith((".csv", ".xlsx")):
        lines = parse_quote_table(data, name)
        method = "csv" if lower.endswith(".csv") else "xlsx"
        quote = record_quote(session, opportunity_id=opportunity_id, supplier_id=supplier_id, actor=actor,
                             method=method, lines=lines, valid_until=valid_until, source=source)
        return QuoteIntake(quote=quote)
    task, _ = queue.enqueue(
        session, task_type="quote_extraction", opportunity_id=opportunity_id,
        input_revision={"source_sha256": source["source_sha256"], "supplier_id": supplier_id},
        payload={**source, "supplier_id": supplier_id, "valid_until": valid_until},
        actor_user_id=actor.id, settings=settings,
    )
    return QuoteIntake(task=task)
