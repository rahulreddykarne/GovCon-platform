"""Visible model route. A missing key uses the known-good route. It does not invent a call.

The code default stays DeepSeek ``deepseek-flash`` for analysis and rules for a
decision when JEV does not run. An owner-saved route is a new version. Rollback
restores an older saved version and keeps the one it replaced in the history.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from govcon.config import Settings
from govcon.models import AppSetting, User
from govcon.workflow.app_settings import SettingError, set_setting

MODEL_ROUTING = "model_routing"
KNOWN_GOOD_PROVIDER = "deepseek"
KNOWN_GOOD_MODEL = "deepseek-flash"
LA = ZoneInfo("America/Los_Angeles")
_ANALYSIS_PROVIDERS = {"deepseek", "anthropic", "openai"}


def describe_route(session: Session | None, settings: Settings) -> dict[str, Any]:
    saved = _saved(session)
    route = saved if saved is not None else _from_settings(settings)
    provider = str(route["analysis"]["provider"])
    model = str(route["analysis"]["model"])
    reason = None
    if not _configured(settings, provider):
        if _configured(settings, KNOWN_GOOD_PROVIDER):
            reason = f"{provider} is not configured. Analysis would use known-good {KNOWN_GOOD_PROVIDER} {KNOWN_GOOD_MODEL}."
            provider = KNOWN_GOOD_PROVIDER
            model = KNOWN_GOOD_MODEL
        else:
            reason = f"{provider} is not configured, and known-good {KNOWN_GOOD_PROVIDER} has no key either. No analysis call is assumed."
    return {
        "version": route["version"],
        "source": "saved" if saved is not None else "settings",
        "analysis_provider": provider,
        "analysis_model": model,
        "known_good_provider": KNOWN_GOOD_PROVIDER,
        "known_good_model": KNOWN_GOOD_MODEL,
        "fallback_reason": reason,
        "decision_provider": route["decision"]["provider"],
        "decision_fallback": route["decision"]["fallback_provider"],
        "history": list(route.get("history") or []),
    }


def analysis_selection(
    session: Session | None, settings: Settings, *, provider_name: str | None, model: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Apply the saved route only when the caller did not name a provider."""
    if provider_name:
        return provider_name, model, None
    described = describe_route(session, settings)
    chosen = model or described["analysis_model"]
    return described["analysis_provider"], chosen, described["fallback_reason"]


def analysis_available(session: Session | None, settings: Settings) -> bool:
    """Whether an analysis call has a provider with a key on the effective route.

    Follows the saved route (and its known-good fallback) the way
    :func:`analysis_selection` does, so a saved Anthropic route counts as
    available even when the configured primary provider has no key.
    """
    return _configured(settings, describe_route(session, settings)["analysis_provider"])


def decision_providers(session: Session | None, settings: Settings) -> tuple[str, str]:
    saved = _saved(session)
    if saved is None:
        primary = (settings.decision_primary_provider or "jev").strip().lower()
        fallback = (settings.decision_fallback_provider or "rules").strip().lower()
        return primary, fallback
    return saved["decision"]["provider"], saved["decision"]["fallback_provider"]


def save_analysis_route(
    session: Session, settings: Settings, *, provider: str, model: str, actor: User,
) -> dict[str, Any]:
    provider = provider.strip().lower()
    model = model.strip()
    if provider not in _ANALYSIS_PROVIDERS:
        raise SettingError("analysis provider must be deepseek, anthropic, or openai")
    if not model or len(model) > 80:
        raise SettingError("the model id must be 1 to 80 characters")
    current = describe_route(session, settings)
    saved = _saved(session) or _from_settings(settings)
    history = list(saved.get("history") or [])
    history.append({
        "version": saved["version"],
        "analysis": dict(saved["analysis"]),
        "decision": dict(saved["decision"]),
        "retired_at": datetime.now(UTC).isoformat(),
    })
    version = datetime.now(LA).strftime("%Y-%m-%d.%H%M%S")
    value = {
        "version": version,
        "analysis": {"provider": provider, "model": model},
        "decision": {"provider": current["decision_provider"], "fallback_provider": "rules"},
        "history": history[-20:],
    }
    return set_setting(session, MODEL_ROUTING, value, actor=actor)


def rollback_route(session: Session, *, version: str, actor: User) -> dict[str, Any]:
    saved = _saved(session)
    if saved is None:
        raise SettingError("there is no saved route to roll back")
    match = next((item for item in saved.get("history") or [] if item.get("version") == version), None)
    if match is None:
        raise SettingError("that route version is not in the history")
    history = [item for item in saved.get("history") or [] if item.get("version") != version]
    history.append({
        "version": saved["version"],
        "analysis": dict(saved["analysis"]),
        "decision": dict(saved["decision"]),
        "retired_at": datetime.now(UTC).isoformat(),
    })
    value = {
        "version": match["version"],
        "analysis": dict(match["analysis"]),
        "decision": {"provider": match["decision"]["provider"], "fallback_provider": "rules"},
        "history": history[-20:],
    }
    return set_setting(session, MODEL_ROUTING, value, actor=actor)


def _saved(session: Session | None) -> dict[str, Any] | None:
    if session is None:
        return None
    row = session.get(AppSetting, MODEL_ROUTING)
    if row is None or not isinstance(row.value, dict) or "analysis" not in row.value:
        return None
    return row.value


def _from_settings(settings: Settings) -> dict[str, Any]:
    from govcon.ai.providers import resolve_provider_model

    provider = (settings.ai_primary_provider or KNOWN_GOOD_PROVIDER).strip().lower()
    _, model = resolve_provider_model(settings, provider_name=provider)
    return {
        "version": "settings",
        "analysis": {"provider": provider, "model": model},
        "decision": {
            "provider": (settings.decision_primary_provider or "jev").strip().lower(),
            "fallback_provider": (settings.decision_fallback_provider or "rules").strip().lower(),
        },
        "history": [],
    }


def _configured(settings: Settings, provider: str) -> bool:
    if provider == "deepseek":
        return bool(settings.deepseek_api_key)
    if provider == "anthropic":
        return bool(settings.anthropic_api_key)
    if provider == "openai":
        return bool(settings.openai_api_key)
    if provider == "rules":
        return True
    return False
