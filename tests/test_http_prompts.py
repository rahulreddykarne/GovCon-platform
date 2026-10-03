"""HTTP retry foundation and prompt placeholder loading."""

from __future__ import annotations

import httpx

from govcon.config import Settings
from govcon.http import build_client, request_with_retry
from govcon.prompting.loader import iter_markdown_prompts


def test_user_agent_and_retry() -> None:
    seen: list[str] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        seen.append(request.headers["user-agent"])
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, text="ok")

    settings = Settings(http_user_agent="govcon-platform/2.0")
    client = build_client(settings, transport=httpx.MockTransport(handler))
    response = request_with_retry(client, "GET", "https://example.test/health")
    assert response.status_code == 200
    assert calls["n"] == 3
    assert seen == ["govcon-platform/2.0"] * 3


def test_placeholder_prompts_load() -> None:
    root = Settings().resolved_prompt_root()
    assets = iter_markdown_prompts(root)
    assert len(assets) == 23  # + supplier_quote_extraction (ADR-071)
    names = {asset.name for asset in assets}
    assert "solicitation_analysis" in names
    assert "source_security_rules" in names

    activated_phase7 = {
        "solicitation_analysis",
        "source_security_rules",
        "no_fabrication_rules",
        "evidence_rules",
        "company_facts_policy",
    }
    activated_phase9 = {
        "requirement_extraction_a",
        "requirement_extraction_b",
        "requirement_reconciliation",
        "compliance_validator",
        "contradiction_detection",
        "compliance_red_team",
        "amendment_analysis",
        "proposal_coverage",
        "submission_preflight_ai",
    }
    activated_phase10 = {
        "reviewer_comment_validation",
        "consolidated_review",
    }
    activated_phase11 = {
        "proposal_drafting",
        "proposal_red_team",
    }
    activated_phase15 = {
        "outcome_analysis",
    }
    activated_phase20 = {
        "market_analysis",
        "supplier_analysis",
        "pricing_analysis",
    }
    activated_automation_stage4 = {
        "supplier_quote_extraction",  # ADR-071
    }
    all_active = (
        activated_phase7
        | activated_phase9
        | activated_phase10
        | activated_phase11
        | activated_phase15
        | activated_phase20
        | activated_automation_stage4
    )
    for asset in assets:
        assert asset.version == "v1"
        assert len(asset.content_hash) == 64
        if asset.name in all_active:
            assert asset.metadata["status"] == "active", f"{asset.name} should be active"
            assert "Do not activate." not in asset.body
        else:
            assert asset.metadata["status"] == "placeholder", f"{asset.name} should be placeholder"
            assert "PLACEHOLDER" in asset.body.upper() or "Do not activate." in asset.body

    jev = list((root / "jev").glob("*.yaml"))
    assert len(jev) == 13
