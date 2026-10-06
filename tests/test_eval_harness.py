"""The sanitized eval set measures current checks. It does not call a provider."""

from __future__ import annotations

from uuid import uuid4

from test_web_ui import _make_user

from govcon.learning.evals import run_eval_harness


def test_harness_reports_four_cases_and_keeps_blockers() -> None:
    report = run_eval_harness()
    assert report["sample_size"] == 4
    assert report["passed"] == 4
    assert report["failed"] == 0
    by_id = {case["id"]: case for case in report["cases"]}
    assert "incomplete" in by_id["sparse_output"]["after"]
    assert "incomplete" in by_id["missing_citations"]["after"]
    assert "10" in by_id["contradictions"]["after"] and "45" in by_id["contradictions"]["after"]
    assert "no_bid" in by_id["missed_blockers"]["after"]
    assert "blockers kept=1" in by_id["missed_blockers"]["after"]


def test_learning_page_shows_the_eval_set_and_capture_counts(db, client) -> None:
    _, token = _make_user(db, f"learn-{uuid4().hex[:8]}@example.test", "owner")
    client.cookies.set("govcon_session", token)
    page = client.get("/learning").text
    assert "Sanitized eval set" in page
    assert "Sample size 4" in page
    assert "Sparse output" in page
    assert "Missed blocker" in page
    assert "Outcome capture" in page
