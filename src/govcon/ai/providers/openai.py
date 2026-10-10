"""OpenAI provider — Chat Completions over HTTPS (same wire shape as DeepSeek).

- Endpoint: POST https://api.openai.com/v1/chat/completions, Bearer auth.
- Model: ``OPENAI_MODEL`` (default ``gpt-5``).
- JSON output: ``response_format {"type": "json_object"}``.
- Output cap: ``max_completion_tokens`` (``max_tokens`` is rejected by the
  reasoning models).
- Sampling: ``temperature`` is accepted for interface parity but not sent; the
  reasoning models reject any value other than the default.
- Errors: 429/5xx and transport errors are retried with backoff; the final
  failure raises :class:`ProviderAPIError` with the status only. Response
  bodies are never logged.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from govcon.ai.gateway import authorize_external_call
from govcon.ai.providers.base import CompletionResult, ProviderAPIError, ProviderRefusal
from govcon.config import Settings, get_settings
from govcon.diagnostics import trace_phase
from govcon.security.classification import DataClassification

logger = logging.getLogger("govcon.ai.providers.openai")

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-5"
REQUEST_TIMEOUT_SECONDS = 300.0
RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        settings: Settings | None = None,
        transport: httpx.BaseTransport | None = None,
        attempts: int = 3,
        backoff_seconds: float = 1.0,
    ) -> None:
        self._api_key = api_key
        self._model = model or DEFAULT_MODEL
        self._base_url = base_url.rstrip("/")
        # The same settings object the factory used drives the gateway policy.
        self._settings = settings or get_settings()
        self._transport = transport
        self._attempts = max(1, attempts)
        self._backoff = backoff_seconds

    @trace_phase("ai.providers.openai.complete")
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
    ) -> CompletionResult:
        """Call Chat Completions once the AI gateway has approved the call."""
        use_model = model or self._model
        authorize_external_call(
            classification=classification,
            provider=self.name,
            model=use_model,
            purpose=purpose,
            settings=self._settings,
        )
        del temperature  # not sent: reasoning models reject non-default sampling

        body: dict[str, Any] = {
            "model": use_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if max_tokens is not None:
            body["max_completion_tokens"] = max_tokens
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"}

        start = time.monotonic()
        resp = self._post(f"{self._base_url}/chat/completions", headers=headers, body=body, purpose=purpose)
        latency_ms = int((time.monotonic() - start) * 1000)

        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise ValueError("openai returned no choices")
        message = choices[0].get("message") or {}
        if message.get("refusal"):
            logger.warning("openai refusal purpose=%s model=%s", purpose, use_model)
            raise ProviderRefusal(self.name, data.get("model") or use_model)
        return CompletionResult(
            content=message.get("content") or "",
            model=data.get("model") or use_model,
            provider=self.name,
            usage=data.get("usage") or {},
            latency_ms=latency_ms,
            finish_reason=choices[0].get("finish_reason"),
        )

    def _post(self, url: str, *, headers: dict[str, str], body: dict[str, Any], purpose: str) -> httpx.Response:
        status: int | None = None
        from govcon.http import build_client

        with build_client(timeout=REQUEST_TIMEOUT_SECONDS, transport=self._transport) as client:
            for attempt in range(self._attempts):
                if attempt:
                    time.sleep(self._backoff * (2 ** (attempt - 1)))
                try:
                    resp = client.post(url, headers=headers, json=body)
                except httpx.TransportError:
                    status = None
                    continue
                if resp.status_code == 200:
                    return resp
                status = resp.status_code
                if status not in RETRY_STATUSES:
                    break
        if status is None:
            logger.error("openai api connection error purpose=%s", purpose)
        else:
            # The response body can echo prompt content; log the status only.
            logger.error("openai api error status=%d purpose=%s", status, purpose)
        raise ProviderAPIError(self.name, status)
