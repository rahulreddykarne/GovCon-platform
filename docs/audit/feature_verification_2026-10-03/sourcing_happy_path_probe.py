"""Run valid XLSX and manual quote intake through the real web routes safely."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import io
import json
from pathlib import Path
import sys
from uuid import uuid4

from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))
from test_web_ui import _make_user
from web_client import CsrfTestClient
from govcon.config import get_settings
from govcon.models import Opportunity, SupplierQuote, SupplierQuoteLine
from govcon.web.app import create_app


def test_valid_xlsx_and_manual_quote_routes(upgraded_engine, monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    with Session(upgraded_engine) as db:
        actor, token = _make_user(db, f"audit-xlsx-{uuid4().hex}@example.test", "owner")
        opp = Opportunity(source="sam", source_id=uuid4().hex, title="Medical gloves",
                          status="open", quantity=Decimal("500"), unit="PR", raw={},
                          response_deadline=datetime.now(UTC) + timedelta(days=30))
        db.add(opp)
        db.commit()
        book = Workbook()
        book.active.append(["description", "quantity", "unit", "unit_price"])
        book.active.append(["Gloves", 500, "PR", 2])
        stream = io.BytesIO()
        book.save(stream)
        book.close()
        with CsrfTestClient(create_app(), follow_redirects=False) as client:
            client.cookies.set("govcon_session", token)
            upload = client.post(f"/workspace/{opp.id}/quotes", data={"supplier_name": "Valid workbook"},
                                 files={"quote_file": ("quote.xlsx", stream.getvalue(),
                                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
            manual = client.post(f"/workspace/{opp.id}/quotes", data={"supplier_name": "Manual supplier",
                                                                       "total_price": "1200.50"})
            assert upload.status_code == manual.status_code == 303
            assert "notice=" in upload.headers["location"]
            assert "notice=" in manual.headers["location"]
        db.expire_all()
        quotes = list(db.scalars(select(SupplierQuote).where(SupplierQuote.opportunity_id == opp.id)
                                .order_by(SupplierQuote.id)))
        assert [q.extraction_method for q in quotes] == ["xlsx", "manual"]
        assert [q.total_price for q in quotes] == [Decimal("1000"), Decimal("1200.50")]
        assert all(q.entered_by_user_id == actor.id for q in quotes)
        lines = list(db.scalars(select(SupplierQuoteLine).where(SupplierQuoteLine.quote_id == quotes[0].id)))
        assert len(lines) == 1 and lines[0].quantity == Decimal("500")
        evidence = {"xlsx_http_status": upload.status_code, "manual_http_status": manual.status_code,
                    "methods": [q.extraction_method for q in quotes],
                    "totals": [str(q.total_price) for q in quotes], "xlsx_lines": len(lines)}
        Path(__file__).with_name("sourcing-happy-path-results.json").write_text(
            json.dumps(evidence, indent=2), encoding="utf-8")
        print(json.dumps(evidence))
