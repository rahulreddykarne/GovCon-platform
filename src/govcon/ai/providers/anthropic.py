"""Anthropic provider — Claude Messages API through the official ``anthropic`` SDK.

- Model: ``ANTHROPIC_MODEL`` (default ``claude-opus-5``).
- JSON output: the prompts already require a JSON object; ``json_mode`` adds a
  closing instruction to the system prompt. Assistant prefill is not used (the
  current models reject it) and ``parse_json_response`` strips code fences.
- Sampling: ``temperature`` is accepted for interface parity but not sent; the
  current Claude models reject sampling parameters. Output is pinned by schema
  validation in ``run_structured_prompt`` instead.
- Refusals: on models that run safety classifiers the request opts into the
  server-side ``fallbacks: "default"`` beta, which re-runs a declined request on
  Anthropic's recommended fallback model. ``result.model`` is the model that
  actually answered. A refusal that survives the fallback raises
  :class:`ProviderRefusal`.
- Errors: the SDK retries 408/409/429/5xx and connection errors. Anything it
  gives up on becomes :class:`ProviderAPIError` carrying the status only — the
  SDK's own error text includes the response body, which can echo prompt
  content, so it is dropped.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import anthropic
import httpx2
from anthropic.types import Message
from anthropic.types.beta import BetaMessage

from govcon.ai.gateway import authorize_external_call
from govcon.ai.providers.base import CompletionResult, ProviderAPIError, ProviderRefusal
from govcon.config import Settings, get_settings
from govcon.security.classification import DataClassification

logger = logging.getLogger("govcon.ai.providers.anthropic")

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_MAX_TOKENS = 16000
REQUEST_TIMEOUT_SECONDS = 300.0
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models whose safety classifiers can decline a request and that accept
# ``fallbacks: "default"``.
FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5", "claude-fable-5-1"})
# Larger outputs are streamed: a non-streaming request that long can outlive
# the HTTP timeout (SDK guidance). The final message is the same either way.
STREAM_ABOVE_MAX_TOKENS = DEFAULT_MAX_TOKENS
JSON_INSTRUCTION = "\n\nRespond with a single JSON object only: no prose before or after it and no code fences."


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        settings: Settings | None = None,
        base_url: str | None = None,
        http_client: httpx2.Client | None = None,
        max_retries: int = 2,
    ) -> None:
        self._model = model or DEFAULT_MODEL
        # The same settings object the factory used drives the gateway policy.
        self._settings = settings or get_settings()
        self._client = anthropic.Anthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=max_retries,
            http_client=http_client,
        )

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
        """Call the Messages API once the AI gateway has approved the call."""
        use_model = model or self._model
        authorize_external_call(
            classification=classification,
            provider=self.name,
            model=use_model,
            purpose=purpose,
            settings=self._settings,
        )
        del temperature  # not sent: current Claude models reject sampling parameters

        request: dict[str, Any] = {
            "model": use_model,
            "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
            "system": system_prompt + (JSON_INSTRUCTION if json_mode else ""),
            "messages": [{"role": "user", "content": user_prompt}],
        }
        use_fallback = use_model in FALLBACK_MODELS and self._settings.anthropic_refusal_fallback

        start = time.monotonic()
        response: Message | BetaMessage
        try:
            stream = request["max_tokens"] > STREAM_ABOVE_MAX_TOKENS
            if use_fallback and stream:
                with self._client.beta.messages.stream(**request, betas=[FALLBACK_BETA], fallbacks="default") as events:
                    response = events.get_final_message()
            elif use_fallback:
                response = self._client.beta.messages.create(**request, betas=[FALLBACK_BETA], fallbacks="default")
            elif stream:
                with self._client.messages.stream(**request) as events:
                    response = events.get_final_message()
            else:
                response = self._client.messages.create(**request)
        except anthropic.APIStatusError as exc:
            logger.error("anthropic api error status=%d purpose=%s", exc.status_code, purpose)
            raise ProviderAPIError(self.name, exc.status_code) from None
        except anthropic.APIConnectionError:
            logger.error("anthropic api connection error purpose=%s", purpose)
            raise ProviderAPIError(self.name, None) from None
        latency_ms = int((time.monotonic() - start) * 1000)

        served_model = getattr(response, "model", None) or use_model
        if served_model != use_model:
            logger.info("anthropic fallback served purpose=%s requested=%s served=%s", purpose, use_model, served_model)
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
            logger.warning("anthropic refusal purpose=%s model=%s category=%s", purpose, served_model, category)
            raise ProviderRefusal(self.name, served_model, category)

        # Thinking and fallback blocks carry no answer text; keep text blocks only.
        content = "".join(block.text for block in response.content if block.type == "text")
        return CompletionResult(
            content=content,
            model=served_model,
            provider=self.name,
            usage=_usage(response.usage),
            latency_ms=latency_ms,
            finish_reason=response.stop_reason,
        )


def _usage(usage: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            out[key] = value
    return out
