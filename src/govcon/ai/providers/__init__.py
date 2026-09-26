"""AI provider factory.

Returns the configured provider instance. Phase 7 implements DeepSeek.
Other providers (OpenAI, Anthropic) follow in later phases with the
same interface.
"""

from __future__ import annotations

import logging

from govcon.config import Settings, get_settings

logger = logging.getLogger("govcon.ai.providers")


class NoProviderConfigured(RuntimeError):
    """Raised when no AI provider has a valid API key."""


def get_provider(settings: Settings | None = None, *, provider_name: str | None = None):
    """Return an AI provider instance.

    Tries the configured ``ai_primary_provider`` (default ``deepseek``).
    Raises ``NoProviderConfigured`` when the required API key is missing.
    """
    settings = settings or get_settings()
    name = provider_name or settings.ai_primary_provider

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
        )

    raise NoProviderConfigured(f"unknown AI provider: {name}")
