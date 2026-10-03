"""AI provider factory.

Returns the configured provider instance. DeepSeek, Anthropic and OpenAI all
expose the same ``complete()`` keyword interface and result fields.
"""

from __future__ import annotations

import logging

from govcon.config import Settings, get_settings

logger = logging.getLogger("govcon.ai.providers")

PROVIDER_NAMES = ("deepseek", "anthropic", "openai")


class NoProviderConfigured(RuntimeError):
    """Raised when no AI provider has a valid API key."""


def provider_available(settings: Settings | None = None) -> bool:
    """Check availability through the same factory used for actual calls."""
    try:
        get_provider(settings)
    except NoProviderConfigured:
        return False
    return True


def resolve_provider_model(
    settings: Settings | None = None, *, provider_name: str | None = None, model: str | None = None
) -> tuple[str, str]:
    """Return the ``(provider, model)`` a call would actually use.

    Mirrors :func:`get_provider` and each provider's model default, so callers
    can tell whether two configured passes really differ.
    """
    from govcon.ai.providers.anthropic import DEFAULT_MODEL as ANTHROPIC_DEFAULT
    from govcon.ai.providers.deepseek import DEFAULT_MODEL as DEEPSEEK_DEFAULT
    from govcon.ai.providers.openai import DEFAULT_MODEL as OPENAI_DEFAULT

    settings = settings or get_settings()
    name = (provider_name or settings.ai_primary_provider or "").strip().lower()
    configured = {
        "deepseek": settings.deepseek_model or DEEPSEEK_DEFAULT,
        "anthropic": settings.anthropic_model or ANTHROPIC_DEFAULT,
        "openai": settings.openai_model or OPENAI_DEFAULT,
    }.get(name, "")
    return name, (model or configured)


def get_provider(settings: Settings | None = None, *, provider_name: str | None = None):
    """Return an AI provider instance.

    Uses ``provider_name`` or the configured ``ai_primary_provider`` (default
    ``deepseek``). Raises ``NoProviderConfigured`` when the provider is unknown
    or its API key is missing.
    """
    settings = settings or get_settings()
    name = (provider_name or settings.ai_primary_provider or "").strip().lower()

    if name == "deepseek":
        if not settings.deepseek_api_key:
            raise NoProviderConfigured(
                "DEEPSEEK_API_KEY is required for the DeepSeek provider. "
                "Set it in .env or environment variables."
            )
        from govcon.ai.providers.deepseek import DeepSeekProvider

        return DeepSeekProvider(
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            settings=settings,
        )

    if name == "anthropic":
        if not settings.anthropic_api_key:
            raise NoProviderConfigured(
                "ANTHROPIC_API_KEY is required for the Anthropic provider. "
                "Set it in .env or environment variables."
            )
        from govcon.ai.providers.anthropic import AnthropicProvider

        return AnthropicProvider(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            settings=settings,
            max_retries=0,
        )

    if name == "openai":
        if not settings.openai_api_key:
            raise NoProviderConfigured(
                "OPENAI_API_KEY is required for the OpenAI provider. "
                "Set it in .env or environment variables."
            )
        from govcon.ai.providers.openai import OpenAIProvider

        return OpenAIProvider(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            settings=settings,
            attempts=1,
        )

    raise NoProviderConfigured(f"unknown AI provider: {name or '(empty)'}")
