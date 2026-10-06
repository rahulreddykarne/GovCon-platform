"""Protect pricing inputs while correcting partial supplier quote coverage."""
from decimal import Decimal
from uuid import uuid4

import pytest
from test_sourcing_company import db as db
from test_sourcing_company import opportunity, user

from govcon.config import get_settings
from govcon.intelligence.ai_analyses import _REQUESTS
from govcon.models import Pursuit
from govcon.sourcing.records import get_or_create_supplier, record_quote


def quote(db, opp, *, quantity="500", unit="PR", nsn=None, total=None):
    actor, _ = user(db)
    supplier, _ = get_or_create_supplier(db, name=uuid4().hex, actor=actor, provenance="fixture")
    lines = [] if quantity is None else [{"description": "Gloves", "quantity": Decimal(quantity),
                                         "unit": unit, "nsn": nsn, "unit_price": Decimal("2")}]
    return record_quote(db, opportunity_id=opp.id, supplier_id=supplier.id, actor=actor,
                        method="manual" if quantity is None else "csv", lines=lines, total_price=total)


@pytest.mark.parametrize("explicit", [None, Decimal("0"), Decimal("1250")])
def test_current_full_order_quote_and_explicit_cost_are_preserved(db, explicit):
    opp = opportunity(db)
    quote(db, opp)
    if explicit is not None:
        db.add(Pursuit(opportunity_id=opp.id, stage="sourcing", sourcing_cost=explicit))
        db.flush()
    inputs = _REQUESTS["pricing"](db, opp.id, get_settings())["variables"]["PRICING_INPUTS_JSON"]
    assert inputs["sourcing_cost_total"] == float(explicit if explicit is not None else 1000)
    assert inputs["unit_cost"] == float((explicit if explicit is not None else 1000) / 500)


def test_current_scoped_manual_total_is_preserved(db):
    opp = opportunity(db)
    quote(db, opp, quantity=None, total="900")
    inputs = _REQUESTS["pricing"](db, opp.id, get_settings())["variables"]["PRICING_INPUTS_JSON"]
    assert inputs["sourcing_cost_total"] == 900 and inputs["unit_cost"] == 1.8


@pytest.mark.parametrize("quantity,unit,nsn", [
    ("5", "PR", None), ("500", "BX", None),
    ("500", "PR", "6515-01-000-0001"), ("500", None, None),
])
def test_incompatible_quote_does_not_replace_full_order_cost(db, quantity, unit, nsn):
    opp = opportunity(db)
    quote(db, opp)
    quote(db, opp, quantity=quantity, unit=unit, nsn=nsn, total="10")
    inputs = _REQUESTS["pricing"](db, opp.id, get_settings())["variables"]["PRICING_INPUTS_JSON"]
    assert inputs["sourcing_cost_total"] == 1000
    assert inputs["unit_cost"] == 2
