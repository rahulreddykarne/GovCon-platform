"""H12: Anthropic and OpenAI providers share DeepSeek's complete() interface,
and a second pass that resolves to the primary provider/model is flagged."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import httpx2
import pytest

from govcon.ai.gateway import AIGatewayBlocked
from govcon.ai.providers import NoProviderConfigured, get_provider, resolve_provider_model
from govcon.ai.providers.anthropic import FALLBACK_BETA, JSON_INSTRUCTION, AnthropicProvider
from govcon.ai.providers.base import CompletionResult, ProviderAPIError, ProviderRefusal
from govcon.ai.providers.deepseek import DeepSeekProvider, parse_json_response
from govcon.ai.providers.openai import OpenAIProvider
from govcon.compliance.extractor import _pass_provider, independence_warnings
from govcon.config import Settings
from govcon.security.classification import DataClassification

ANTHROPIC_KEY = "sk-ant-test-key-0123456789"
OPENAI_KEY = "sk-openai-test-key-0123456789"
SECRET_ECHO = "PROMPT-ECHO-SHOULD-NOT-LEAK"


def _settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


def _call(provider, **overrides):
    kwargs = {
        "system_prompt": "You extract requirements.",
        "user_prompt": "Solicitation text",
        "temperature": 0.0,
        "json_mode": True,
        "classification": DataClassification.PUBLIC,
        "purpose": "unit_test",
    }
    kwargs.update(overrides)
    return provider.complete(**kwargs)


# ── Anthropic ──


def _message(text: str = '{"ok": true}', *, model: str = "claude-opus-5", stop_reason: str = "end_turn", **extra) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": text},
        ],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 0},
        **extra,
    }


def _anthropic(handler, *, model: str | None = None, settings: Settings | None = None) -> tuple[AnthropicProvider, list]:
    seen: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    provider = AnthropicProvider(
        ANTHROPIC_KEY,
        model=model,
        settings=settings or _settings(),
        http_client=httpx2.Client(transport=httpx2.MockTransport(record)),
        max_retries=0,
    )
    return provider, seen


def test_anthropic_request_shape_and_result() -> None:
    provider, seen = _anthropic(lambda r: httpx2.Response(200, json=_message()))
    result = _call(provider, max_tokens=None)
    assert isinstance(result, CompletionResult)
    assert result.content == '{"ok": true}'  # the thinking block is not part of the answer
    assert (result.provider, result.model, result.finish_reason) == ("anthropic", "claude-opus-5", "end_turn")
    assert result.usage == {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 0}
    assert parse_json_response(result) == {"ok": True}

    request = seen[0]
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == ANTHROPIC_KEY
    assert "anthropic-version" in request.headers
    assert FALLBACK_BETA in request.headers.get("anthropic-beta", "")
    body = json.loads(request.content)
    assert body["model"] == "claude-opus-5"
    assert body["max_tokens"] == 16000
    assert body["system"].endswith(JSON_INSTRUCTION)
    assert body["messages"] == [{"role": "user", "content": "Solicitation text"}]
    assert body["fallbacks"] == "default"
    assert "temperature" not in body  # current Claude models reject sampling parameters


def test_anthropic_without_classifier_fallback() -> None:
    provider, seen = _anthropic(lambda r: httpx2.Response(200, json=_message(model="claude-sonnet-5")), model="claude-sonnet-5")
    _call(provider, json_mode=False, max_tokens=512)
    body = json.loads(seen[0].content)
    assert "fallbacks" not in body and "anthropic-beta" not in seen[0].headers
    assert body["max_tokens"] == 512 and body["system"] == "You extract requirements."

    provider, seen = _anthropic(lambda r: httpx2.Response(200, json=_message()), settings=_settings(anthropic_refusal_fallback=False))
    _call(provider)
    assert "fallbacks" not in json.loads(seen[0].content)


def test_anthropic_records_the_model_that_answered() -> None:
    provider, _ = _anthropic(lambda r: httpx2.Response(200, json=_message(model="claude-opus-4-8")))
    assert _call(provider).model == "claude-opus-4-8"


def test_anthropic_refusal_raises() -> None:
    refusal = _message(text="", stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": None})
    provider, _ = _anthropic(lambda r: httpx2.Response(200, json=refusal))
    with pytest.raises(ProviderRefusal) as info:
        _call(provider)
    assert info.value.category == "cyber"


def test_anthropic_error_keeps_status_but_not_body() -> None:
    error = {"type": "error", "error": {"type": "invalid_request_error", "message": SECRET_ECHO}}
    provider, _ = _anthropic(lambda r: httpx2.Response(400, json=error))
    with pytest.raises(ProviderAPIError) as info:
        _call(provider)
    assert info.value.status_code == 400
    assert SECRET_ECHO not in str(info.value) and info.value.__cause__ is None and info.value.__suppress_context__


def test_anthropic_gateway_blocks_before_any_request() -> None:
    provider, seen = _anthropic(lambda r: httpx2.Response(200, json=_message()))
    with pytest.raises(AIGatewayBlocked):
        _call(provider, classification=DataClassification.PROPRIETARY)
    assert seen == []


# ── OpenAI ──


def _completion(content: str = '{"ok": true}', **message) -> dict:
    return {
        "id": "chatcmpl-test",
        "model": "gpt-5",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content, **message}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
    }


def _openai(responses: list, *, settings: Settings | None = None) -> tuple[OpenAIProvider, list]:
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    provider = OpenAIProvider(
        OPENAI_KEY, settings=settings or _settings(), transport=httpx.MockTransport(handler), backoff_seconds=0
    )
    return provider, seen


def test_openai_request_shape_and_result() -> None:
    provider, seen = _openai([httpx.Response(200, json=_completion())])
    result = _call(provider, max_tokens=2048)
    assert (result.content, result.model, result.provider) == ('{"ok": true}', "gpt-5", "openai")
    assert result.usage["total_tokens"] == 60 and result.finish_reason == "stop"
    request = seen[0]
    assert str(request.url) == "https://api.openai.com/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {OPENAI_KEY}"
    body = json.loads(request.content)
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_completion_tokens"] == 2048
    assert "temperature" not in body and "max_tokens" not in body
    assert body["messages"][0] == {"role": "system", "content": "You extract requirements."}


def test_openai_retries_transient_failures() -> None:
    provider, seen = _openai([
        httpx.Response(503),
        httpx.ConnectError("boom"),
        httpx.Response(200, json=_completion()),
    ])
    assert _call(provider).content == '{"ok": true}'
    assert len(seen) == 3


def test_openai_client_error_is_not_retried_and_body_is_dropped() -> None:
    provider, seen = _openai([httpx.Response(401, json={"error": {"message": SECRET_ECHO}})])
    with pytest.raises(ProviderAPIError) as info:
        _call(provider)
    assert info.value.status_code == 401 and SECRET_ECHO not in str(info.value)
    assert len(seen) == 1


def test_openai_refusal_and_gateway() -> None:
    provider, _ = _openai([httpx.Response(200, json=_completion(content=None, refusal="I can't help with that."))])
    with pytest.raises(ProviderRefusal):
        _call(provider)
    provider, seen = _openai([])
    with pytest.raises(AIGatewayBlocked):
        _call(provider, classification=DataClassification.CUI)
    assert seen == []


# ── Factory ──


def test_factory_builds_each_provider() -> None:
    settings = _settings(
        deepseek_api_key="ds-test-key-0123456789",
        anthropic_api_key=ANTHROPIC_KEY,
        openai_api_key=OPENAI_KEY,
        anthropic_model="claude-sonnet-5",
    )
    assert isinstance(get_provider(settings, provider_name="deepseek"), DeepSeekProvider)
    assert isinstance(get_provider(settings, provider_name="Anthropic"), AnthropicProvider)
    assert isinstance(get_provider(settings, provider_name="openai"), OpenAIProvider)
    assert get_provider(settings, provider_name="anthropic")._model == "claude-sonnet-5"


@pytest.mark.parametrize("name", ["anthropic", "openai", "mystery"])
def test_factory_refuses_missing_keys_and_unknown_names(name: str) -> None:
    with pytest.raises(NoProviderConfigured):
        get_provider(_settings(), provider_name=name)


def test_resolve_provider_model_applies_defaults() -> None:
    settings = _settings(deepseek_model="deepseek-v4-pro")
    assert resolve_provider_model(settings) == ("deepseek", "deepseek-v4-pro")
    assert resolve_provider_model(settings, provider_name="anthropic") == ("anthropic", "claude-opus-5")
    assert resolve_provider_model(settings, provider_name="openai", model="gpt-5-mini") == ("openai", "gpt-5-mini")


# ── Pass independence ──


def _opportunity(value: float | None = None) -> SimpleNamespace:
    return SimpleNamespace(estimated_value_max=value)


def _codes(warnings: list[dict]) -> set[str]:
    return {w["code"] for w in warnings}


def test_pass_b_on_the_primary_model_is_flagged() -> None:
    _, _, warnings = _pass_provider("B", _opportunity(), _settings())
    assert "pass_b_not_independent" in _codes(warnings)
    # Naming the primary explicitly is still the same model.
    _, _, warnings = _pass_provider("B", _opportunity(), _settings(compliance_pass_b_provider="deepseek"))
    assert "pass_b_not_independent" in _codes(warnings)
    _, _, warnings = _pass_provider("A", _opportunity(), _settings())
    assert warnings == []


def test_pass_b_on_another_provider_or_model_is_not_flagged() -> None:
    for overrides in (
        {"compliance_pass_b_provider": "anthropic"},
        {"compliance_pass_b_provider": "deepseek", "compliance_pass_b_model": "deepseek-v4-pro"},
    ):
        _, _, warnings = _pass_provider("B", _opportunity(), _settings(**overrides))
        assert "pass_b_not_independent" not in _codes(warnings), overrides


def test_escalation_provider_is_checked_too() -> None:
    settings = _settings(
        compliance_pass_b_provider="anthropic",
        compliance_escalation_min_value=1_000_000,
        compliance_escalation_provider="deepseek",
    )
    provider, _, warnings = _pass_provider("B", _opportunity(5_000_000), settings)
    assert provider == "deepseek" and "pass_b_not_independent" in _codes(warnings)
    provider, _, warnings = _pass_provider("B", _opportunity(10), settings)
    assert provider == "anthropic" and warnings == []


def test_secondary_validator_independence_helper() -> None:
    settings = _settings(ai_primary_provider="openai")
    assert _codes(independence_warnings(settings, "openai", None, code="secondary_validator_not_independent", what="x")) == {
        "secondary_validator_not_independent"
    }
    assert independence_warnings(settings, "anthropic", None, code="secondary_validator_not_independent", what="x") == []
