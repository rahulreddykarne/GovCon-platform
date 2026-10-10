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
import re
import time
from dataclasses import dataclass

from govcon.ai.gateway import authorize_external_call
from govcon.ai.providers.base import CompletionResult
from govcon.config import Settings, get_settings
from govcon.diagnostics import trace_phase
from govcon.security.classification import DataClassification

logger = logging.getLogger("govcon.ai.providers.deepseek")

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"


@dataclass
class DeepSeekResult(CompletionResult):
    """The shared result type, so recorded runs can checkpoint DeepSeek responses."""

    provider: str = "deepseek"


class DeepSeekAPIError(RuntimeError):
    """DeepSeek returned a non-200 status. The body is deliberately not kept."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"DeepSeek API returned HTTP {status_code}")


class DeepSeekProvider:
    name = "deepseek"

    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        settings: Settings | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model or DEFAULT_MODEL
        self._base_url = base_url.rstrip("/")
        # The same settings object the factory used drives the gateway policy.
        self._settings = settings or get_settings()

    @trace_phase("ai.providers.deepseek.complete")
    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = True,
        classification: DataClassification,
        purpose: str,
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
            settings=self._settings,
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
        from govcon.http import build_client

        with build_client(timeout=120.0) as client:
            resp = client.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=body,
            )
        latency_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code != 200:
            # The response body can echo prompt content; log the status only.
            logger.error("deepseek api error status=%d purpose=%s", resp.status_code, purpose)
            raise DeepSeekAPIError(resp.status_code)

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


_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*$", re.MULTILINE)


def parse_json_response(result: CompletionResult) -> dict:
    """Extract and parse the JSON object in a model response.

    Tolerates what JSON-mode models still emit: markdown code fences anywhere,
    raw newlines or tabs inside strings (``strict=False``), and a sentence
    before or after the object. An empty answer, or text with no JSON object,
    still raises :class:`json.JSONDecodeError`.
    """
    text = _FENCE.sub("", result.content or "").strip()
    if not text:
        raise json.JSONDecodeError("empty response", "", 0)
    decoder = json.JSONDecoder(strict=False)
    try:
        return decoder.decode(text)
    except json.JSONDecodeError as first:
        start = text.find("{")
        while start != -1:
            try:
                value, _end = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                start = text.find("{", start + 1)
                continue
            if isinstance(value, dict):
                return value
            start = text.find("{", start + 1)
        raise first
