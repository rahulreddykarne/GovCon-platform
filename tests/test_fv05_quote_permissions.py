"""Human quote entry preserves its role and CLI contracts."""
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from test_sourcing_company import opportunity, user
from typer.testing import CliRunner

from govcon.cli import app
from govcon.models import Supplier, SupplierQuote
from govcon.sourcing.intake import receive_quote_file
from govcon.sourcing.records import get_or_create_supplier, record_quote

CSV = b"description,quantity,unit,unit_price\nGloves,500,PR,2.00\n"


def supplier_for(db, actor):
    supplier, _ = get_or_create_supplier(db, name=f"permission-{uuid4().hex}", actor=actor, provenance="test")
    db.commit()
    return supplier


def cli_quote(opp, actor, tmp_path, supplier):
    path = tmp_path / "quote.csv"
    path.write_bytes(CSV)
    return CliRunner().invoke(app, ["sourcing", "add-quote", "--opportunity-id", str(opp.id),
                                   "--supplier", supplier, "--file", str(path), "--actor-email", actor.email])


@pytest.mark.parametrize("role", ["reviewer", "approver", "owner"])
def test_current_authorized_cli_upload_contract(db, tmp_path, role):
    opp = opportunity(db)
    actor, _ = user(db, role)
    result = cli_quote(opp, actor, tmp_path, f"cli-{uuid4().hex}")
    assert result.exit_code == 0, result.output
    assert "quote_id:" in result.output
    quote = db.scalar(select(SupplierQuote).where(SupplierQuote.opportunity_id == opp.id))
    assert quote.total_price == Decimal(1000) and quote.entered_by_user_id == actor.id


@pytest.mark.parametrize("role", ["reviewer", "approver", "owner"])
def test_current_authorized_direct_intake(db, role):
    opp = opportunity(db)
    actor, _ = user(db, role)
    supplier = supplier_for(db, actor)
    intake = receive_quote_file(db, opportunity_id=opp.id, supplier_id=supplier.id, data=CSV,
                               filename="quote.csv", valid_until=None, actor=actor)
    assert intake.task is None and intake.quote.total_price == Decimal(1000)
    db.rollback()


@pytest.mark.parametrize("role", [None, "reviewer", "approver", "owner"])
def test_current_direct_record_and_system_actor(db, role):
    opp = opportunity(db)
    actor = user(db, role)[0] if role else None
    supplier = supplier_for(db, actor)
    quote = record_quote(db, opportunity_id=opp.id, supplier_id=supplier.id, actor=actor,
                         method="manual" if actor else "ai", total_price="0")
    assert quote.total_price == 0 and quote.entered_by_user_id == (actor.id if actor else None)
    db.rollback()


def test_current_web_permission_denial(db, client):
    opp = opportunity(db)
    _, token = user(db, "read_only")
    client.cookies.set("govcon_session", token)
    response = client.post(f"/workspace/{opp.id}/quotes", data={"supplier_name": "Forbidden", "total_price": "1"})
    assert response.status_code == 303 and "error=" in response.headers["location"]
    assert db.scalar(select(func.count()).select_from(SupplierQuote).where(SupplierQuote.opportunity_id == opp.id)) == 0


def test_read_only_cli_is_denied_before_supplier_or_quote_write(db, tmp_path):
    opp = opportunity(db)
    actor, _ = user(db, "read_only")
    name = f"denied-{uuid4().hex}"
    result = cli_quote(opp, actor, tmp_path, name)
    assert result.exit_code == 2, "read-only actor successfully entered a quote"
    assert "cannot review" in result.output
    assert db.scalar(select(Supplier.id).where(Supplier.name == name)) is None
    assert db.scalar(select(SupplierQuote.id).where(SupplierQuote.opportunity_id == opp.id)) is None


def test_read_only_direct_record_is_denied(db):
    from govcon.collaboration.users import PermissionDenied
    opp = opportunity(db)
    actor, _ = user(db, "read_only")
    supplier = supplier_for(db, actor)
    with pytest.raises(PermissionDenied):
        record_quote(db, opportunity_id=opp.id, supplier_id=supplier.id, actor=actor, method="manual", total_price="1")
    assert db.scalar(select(SupplierQuote.id).where(SupplierQuote.opportunity_id == opp.id)) is None


def test_read_only_direct_intake_is_denied_before_file_write(db, monkeypatch):
    from govcon.collaboration.users import PermissionDenied
    opp = opportunity(db)
    actor, _ = user(db, "read_only")
    supplier = supplier_for(db, actor)
    writes = []

    def store(*args):
        writes.append(args)
        return {}

    monkeypatch.setattr("govcon.sourcing.intake.store_quote_file", store)
    with pytest.raises(PermissionDenied):
        receive_quote_file(db, opportunity_id=opp.id, supplier_id=supplier.id, actor=actor,
                           data=CSV, filename="quote.csv", valid_until=None)
    assert writes == []
