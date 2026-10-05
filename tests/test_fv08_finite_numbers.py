"""Sourcing numeric validation preserves finite inputs and rejects nonfinite values."""
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_sourcing_company import opportunity, user

from govcon.models import SupplierQuote
from govcon.sourcing.records import (
    SourcingError,
    _decimal,
    get_or_create_supplier,
    import_catalog_csv,
    parse_quote_table,
)


@pytest.mark.parametrize("value,expected", [(None, None), ("", None), (" ", None), (0, Decimal(0)),
                                           ("$1,200.50", Decimal("1200.50")), (" 2.00 ", Decimal("2.00")),
                                           (Decimal("3.125"), Decimal("3.125"))])
def test_current_finite_and_blank_numeric_contract(value, expected):
    assert _decimal(value, "price") == expected


@pytest.mark.parametrize("value", ["-1", "abc"])
def test_current_invalid_number_contract(value):
    with pytest.raises(SourcingError):
        _decimal(value, "price")


def test_current_formatted_manual_total_web_contract(db, client):
    opp = opportunity(db)
    _actor, token = user(db)
    client.cookies.set("govcon_session", token)
    response = client.post(f"/workspace/{opp.id}/quotes", data={"supplier_name": f"finite-{uuid4().hex}",
                                                               "total_price": "$1,200.50"})
    assert response.status_code == 303 and "notice=" in response.headers["location"]
    assert db.scalar(select(SupplierQuote.total_price).where(SupplierQuote.opportunity_id == opp.id)) == Decimal("1200.50")


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity"])
def test_nonfinite_numbers_use_the_sourcing_error_contract(value):
    with pytest.raises(SourcingError, match="finite"):
        _decimal(value, "price")


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity"])
def test_nonfinite_manual_total_is_rejected_cleanly(db, client, value):
    opp = opportunity(db)
    _, token = user(db)
    client.cookies.set("govcon_session", token)
    response = client.post(f"/workspace/{opp.id}/quotes", data={"supplier_name": f"nonfinite-{uuid4().hex}",
                                                               "total_price": value})
    assert response.status_code == 303 and "error=" in response.headers["location"]
    assert db.scalar(select(SupplierQuote.id).where(SupplierQuote.opportunity_id == opp.id)) is None


@pytest.mark.parametrize("value", ["NaN", "Infinity"])
def test_nonfinite_quote_quantities_are_rejected(value):
    with pytest.raises(SourcingError, match="finite"):
        parse_quote_table(f"description,quantity,unit_price\nGloves,{value},2\n".encode(), "quote.csv")


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity"])
def test_nonfinite_catalog_price_is_a_row_error(db, value):
    actor, _ = user(db)
    supplier, _ = get_or_create_supplier(db, name=f"numbers-{uuid4().hex}", actor=actor, provenance="test")
    data = f"part_number,list_price\nGOOD-{uuid4().hex},2\nBAD,{value}\n".encode()
    result = import_catalog_csv(db, supplier_id=supplier.id, actor=actor, data=data, filename="catalog.csv")
    assert (result.rows_total, result.rows_imported) == (2, 1)
    assert len(result.errors) == 1 and result.errors[0]["line"] == 3
    db.rollback()
