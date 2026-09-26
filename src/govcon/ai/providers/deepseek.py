"""DeepSeek provider — OpenAI-compatible chat completions.

Verified 2026-09-26 against https://api-docs.deepseek.com/:
- Endpoint: POST https://api.deepseek.com/chat/completions
- Auth: Bearer token in Authorization header
- Models: deepseek-flash (V4.1-Flash), deepseek-v4-pro (V4-Pro-0813)
- JSON output: response_format {"type": "json_object"}
- Temperature has no effect in thinking mode
- Usage: prompt_tokens, completion_tokens, total_tokens
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

import httpx

from govcon.ai.gateway import authorize_external_call
from govcon.security.classification import DataClassification

logger = logging.getLogger("govcon.ai.providers.deepseek")

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"


@dataclass
class DeepSeekResult:
    content: str
    model: str
    provider: str = "deepseek"
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    finish_reason: str | None = None


class DeepSeekProvider:
    name = "deepseek"

    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self._api_key = api_key
        self._model = model or DEFAULT_MODEL
        self._base_url = base_url.rstrip("/")

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = True,
        classification: DataClassification = DataClassification.PUBLIC,
        purpose: str = "solicitation_analysis",
    ) -> DeepSeekResult:
        """Call the DeepSeek chat completions endpoint.

        Authorizes via the AI gateway before making the external call.
        Returns parsed content and usage metadata.
        """
        use_model = model or self._model
        authorize_external_call(
            classification=classification,
            provider=self.name,
            model=use_model,
            purpose=purpose,
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        body: dict = {
            "model": use_model,
            "messages": messages,
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        start = time.monotonic()
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=body,
            )
        latency_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code != 200:
            logger.error(
                "deepseek api error status=%d body=%s",
                resp.status_code,
                resp.text[:500],
            )
            resp.raise_for_status()

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise ValueError("deepseek returned no choices")

        message = choices[0].get("message", {})
        content = message.get("content", "")
        usage = data.get("usage", {})

        return DeepSeekResult(
            content=content,
            model=use_model,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=choices[0].get("finish_reason"),
        )


def parse_json_response(result: DeepSeekResult) -> dict:
    """Extract and parse JSON from the model response.

    Handles cases where the model wraps JSON in markdown code fences.
    """
    text = result.content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    return json.loads(text)
