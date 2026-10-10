"""The opportunity page lists every stage, including ones that never ran."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from test_web_ui import _make_user

from govcon.models import BotRun, Opportunity


def test_trace_says_not_run_when_no_bot_has_run(db, client) -> None:
    _, token = _make_user(db, f"trace-{uuid4().hex[:8]}@example.test", "owner")
    opp = Opportunity(
        source="demo", source_id=f"TRACE-{uuid4().hex[:8]}", title="Trace notice, not a real solicitation",
        status="open", raw={"demo": True},
    )
    db.add(opp)
    db.commit()
    client.cookies.set("govcon_session", token)
    page = client.get(f"/workspace/{opp.id}").text
    assert "What ran on this notice" in page
    assert "Notice stored" in page and "stored" in page
    assert "Document" in page and "not run" in page
    assert "Your decision" in page and "not requested" in page
    assert "Solicitation analysis" in page and "not run" in page


def test_a_failed_document_run_is_not_shown_as_finished(db, client) -> None:
    _, token = _make_user(db, f"trace-fail-{uuid4().hex[:8]}@example.test", "owner")
    opp = Opportunity(
        source="demo", source_id=f"TRACEF-{uuid4().hex[:8]}", title="Failed document trace",
        status="open", raw={"demo": True},
    )
    db.add(opp)
    db.flush()
    db.add(BotRun(
        bot_name="document", status="failed", trigger="test", idempotency_key=uuid4().hex,
        opportunity_id=opp.id, attempt=1, started_at=datetime.now(UTC), finished_at=datetime.now(UTC),
        error="attachment was not read", outputs={"state": "incomplete"},
    ))
    db.commit()
    client.cookies.set("govcon_session", token)
    page = client.get(f"/opp/{opp.id}").text
    assert "attachment was not read" in page
    assert "incomplete" in page or "failed" in page
    document = page.split("Document", 1)[1][:500]
    assert "succeeded" not in document
