"""Catalog callers retain good rows and report malformed rows."""
from urllib.parse import unquote_plus
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_sourcing_company import user
from typer.testing import CliRunner

from govcon.cli import app
from govcon.models import CatalogImport, Product, Supplier
from govcon.sourcing.records import get_or_create_supplier, import_catalog_csv


def catalog_input():
    part = f"part-{uuid4().hex}"
    return part, f"part_number,list_price\n{part},2\n".encode()


def import_through(entry, db, client, tmp_path, data):
    actor, token = user(db)
    name = f"catalog-{uuid4().hex}"
    if entry == "web":
        client.cookies.set("govcon_session", token)
        response = client.post("/suppliers", data={"name": name}, files={"catalog_file": ("catalog.csv", data, "text/csv")})
        assert response.status_code == 303
        output = unquote_plus(response.headers["location"])
    elif entry == "cli":
        path = tmp_path / "catalog.csv"
        path.write_bytes(data)
        result = CliRunner().invoke(app, ["sourcing", "import-catalog", "--supplier", name,
                                        "--file", str(path), "--actor-email", actor.email])
        assert result.exit_code == 0, result.output
        output = result.output
    else:
        supplier, _ = get_or_create_supplier(db, name=name, actor=actor, provenance="test")
        result = import_catalog_csv(db, supplier_id=supplier.id, data=data, filename="catalog.csv", actor=actor)
        db.commit()
        output = str(result.errors)
    supplier = db.scalar(select(Supplier).where(Supplier.name == name))
    run = db.scalar(select(CatalogImport).where(CatalogImport.supplier_id == supplier.id))
    return run, output


@pytest.mark.parametrize("entry", ["web", "cli", "service"])
def test_current_valid_catalog_caller_contracts(db, client, tmp_path, entry):
    part, data = catalog_input()
    run, output = import_through(entry, db, client, tmp_path, data)
    assert (run.rows_total, run.rows_imported, run.errors) == (1, 1, [])
    assert db.scalar(select(Product.id).where(Product.part_number == part)) is not None
    if entry != "service":
        assert "1 of 1" in output


@pytest.mark.parametrize("entry", ["web", "cli", "service"])
def test_surplus_fields_are_skipped_without_losing_valid_rows(db, client, tmp_path, entry):
    part, data = catalog_input()
    run, output = import_through(entry, db, client, tmp_path, data + b"BAD,3,unexpected\n")
    assert (run.rows_total, run.rows_imported) == (2, 1)
    assert len(run.errors) == 1 and run.errors[0]["line"] == 3
    assert db.scalar(select(Product.id).where(Product.part_number == part)) is not None
    if entry != "service":
        assert "1 of 2" in output and "line 3" in output
