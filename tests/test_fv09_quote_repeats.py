"""Document quote identity preserves distinct records and makes retries safe."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from test_sourcing_company import opportunity, user
from typer.testing import CliRunner

from govcon.cli import app
from govcon.db import session_scope
from govcon.models import AuditEvent, SupplierQuote, SupplierQuoteLine
from govcon.sourcing.records import (
    get_or_create_supplier,
    record_quote,
    sourcing_revision,
)

CSV = b"description,quantity,unit,unit_price\nGloves,500,PR,2.00\n"


def quote_inputs(db):
    opp = opportunity(db)
    actor, _ = user(db)
    supplier, _ = get_or_create_supplier(db, name=f"repeat-{uuid4().hex}", actor=actor, provenance="test")
    db.commit()
    return {"opportunity_id": opp.id, "supplier_id": supplier.id, "actor": actor, "method": "csv", "total_price": "1000",
                "valid_until": "2099-12-31", "source": {"source_sha256": uuid4().hex * 2}}


@pytest.mark.parametrize("difference", ["opportunity", "supplier", "document", "validity", "withdrawn", "manual"])
def test_current_distinct_quote_identities_remain_distinct(db, difference):
    args = quote_inputs(db)
    if difference == "manual":
        args.update(method="manual", source=None)
    first = record_quote(db, **args)
    second_args = {**args, "source": dict(args["source"]) if args["source"] else None}
    if difference == "opportunity":
        db.commit()
        second_args["opportunity_id"] = opportunity(db).id
    elif difference == "supplier":
        supplier, _ = get_or_create_supplier(db, name=f"other-{uuid4().hex}", actor=args["actor"], provenance="test")
        second_args["supplier_id"] = supplier.id
    elif difference == "document":
        second_args["source"]["source_sha256"] = uuid4().hex * 2
    elif difference == "validity":
        second_args["valid_until"] = "2099-12-30"
    elif difference == "withdrawn":
        first.status = "withdrawn"
    second = record_quote(db, **second_args)
    assert first.id != second.id
    assert first.total_price == second.total_price == Decimal(1000)
    db.rollback()


@pytest.mark.parametrize("method", ["csv", "xlsx", "ai"])
def test_repeated_document_record_keeps_one_quote_lines_audit_and_revision(db, method):
    args = quote_inputs(db)
    args.update(method=method, lines=[{"description": "Gloves", "quantity": 500, "unit": "PR", "unit_price": 2}])
    first = record_quote(db, **args)
    revision = sourcing_revision(db, args["opportunity_id"])
    second = record_quote(db, **args)
    assert second.id == first.id, "the same document was recorded twice"
    assert sourcing_revision(db, args["opportunity_id"]) == revision
    assert db.scalar(select(func.count()).select_from(SupplierQuoteLine).where(SupplierQuoteLine.quote_id == first.id)) == 1
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(
        AuditEvent.opportunity_id == args["opportunity_id"], AuditEvent.action_type == "supplier_quote_recorded")) == 1
    db.rollback()


@pytest.mark.parametrize("entry", ["web", "cli"])
def test_repeated_upload_returns_success_without_duplicate_quote(db, client, tmp_path, entry):
    opp = opportunity(db)
    actor, token = user(db)
    name = f"upload-repeat-{uuid4().hex}"
    path = tmp_path / "quote.csv"
    path.write_bytes(CSV)
    client.cookies.set("govcon_session", token)
    for _ in range(2):
        if entry == "web":
            response = client.post(f"/workspace/{opp.id}/quotes", data={"supplier_name": name},
                                   files={"quote_file": ("quote.csv", CSV, "text/csv")})
            assert response.status_code == 303 and "notice=" in response.headers["location"]
        else:
            result = CliRunner().invoke(app, ["sourcing", "add-quote", "--opportunity-id", str(opp.id),
                                            "--supplier", name, "--file", str(path), "--actor-email", actor.email])
            assert result.exit_code == 0 and "quote_id:" in result.output
    assert db.scalar(select(func.count()).select_from(SupplierQuote).where(SupplierQuote.opportunity_id == opp.id)) == 1


def test_concurrent_document_records_share_one_quote(db):
    args = quote_inputs(db)
    args["actor"] = None
    barrier = Barrier(2)

    def enter():
        with session_scope() as session:
            barrier.wait(timeout=10)
            quote_id = record_quote(session, **args).id
        return quote_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(enter) for _ in range(2)]
        ids = [future.result(timeout=30) for future in futures]
    assert ids[0] == ids[1], "concurrent retries inserted separate quotes"
    assert db.scalar(select(func.count()).select_from(SupplierQuote).where(SupplierQuote.opportunity_id == args["opportunity_id"])) == 1


def test_concurrent_cli_upload_with_new_supplier_keeps_one_supplier_and_quote(db, tmp_path, monkeypatch):
    import time

    from govcon import cli
    from govcon.config import get_settings
    from govcon.models import Supplier
    from govcon.sourcing import records
    opp = opportunity(db)
    actor, _ = user(db)
    path = tmp_path / "quote.csv"
    path.write_bytes(CSV)
    name = f"new-concurrent-supplier-{uuid4().hex}"
    barrier = Barrier(2)
    original_actor = cli._actor
    original_supplier = records.get_or_create_supplier
    settings = get_settings()
    opportunity_id, actor_email = opp.id, actor.email

    def ready_actor(*args):
        result = original_actor(*args)
        barrier.wait(timeout=10)
        return result

    def slow_new_supplier(*args, **kwargs):
        result = original_supplier(*args, **kwargs)
        if result[1]:
            time.sleep(0.2)  # Both old CLI transactions can observe the new name as absent before either commits.
        return result

    monkeypatch.setattr(cli, "_actor", ready_actor)
    monkeypatch.setattr(cli, "_task_settings", lambda: settings)
    monkeypatch.setattr(records, "get_or_create_supplier", slow_new_supplier)

    def upload():
        cli.sourcing_add_quote(opportunity_id=opportunity_id, supplier=name, file=path,
                               actor_email=actor_email, valid_until=None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(upload) for _ in range(2)]
        for future in futures:
            future.result(timeout=30)
    assert db.scalar(select(func.count()).select_from(SupplierQuote).where(SupplierQuote.opportunity_id == opp.id)) == 1, "concurrent CLI uploads bypassed quote identity using new supplier IDs"
    assert db.scalar(select(func.count()).select_from(Supplier).where(Supplier.name == name)) == 1
