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
    assert len(assets) == 22
    names = {asset.name for asset in assets}
    assert "solicitation_analysis" in names
    assert "source_security_rules" in names
    for asset in assets:
        assert asset.metadata["status"] == "placeholder"
        assert asset.version == "v1"
        assert len(asset.content_hash) == 64
        assert "Do not activate." in asset.body
    jev = list((root / "jev").glob("*.yaml"))
    assert len(jev) == 13
