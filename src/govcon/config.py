"""Single pydantic-settings object for process configuration.

Missing optional integrations stay unset. Missing required settings fail when a
command needs them, not when this module is imported.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from govcon.paths import repo_root

_PUBLIC_BIND_HOSTS = frozenset({"0.0.0.0", "::", "[::]"})


class ConfigError(RuntimeError):
    """Raised when a command needs a setting that was not provided."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str | None = None

    sam_api_key: str | None = None
    deepseek_api_key: str | None = None
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    deepseek_model: str | None = None
    ai_primary_provider: str = "deepseek"
    ai_review_provider: str | None = None
    ai_secondary_review_provider: str | None = None

    ai_external_allowed_for_proprietary: bool = False
    ai_external_allowed_for_fci: bool = False
    ai_external_allowed_for_cui: bool = False

    prompt_root: Path = Path("./src/govcon/prompts")
    prompt_strict_json: bool = True
    prompt_fail_on_schema_error: bool = True
    prompt_enable_regression_gate: bool = True
    prompt_max_retries_on_invalid_json: int = 1

    jev_enabled: bool = True
    jev_api_key: str | None = None
    jev_base_url: str | None = None
    decision_primary_provider: str = "jev"
    decision_fallback_provider: str = "rules"
    jev_min_confidence: float | None = None
    jev_human_review_threshold: float | None = None

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_pass: str | None = None
    alert_email_to: str | None = None

    embedding_model: str = "all-MiniLM-L6-v2"
    data_dir: Path = Path("./data")
    outbox_dir: Path = Path("./outbox")
    log_dir: Path = Path("./logs")

    ai_max_input_tokens_per_opportunity: int = 120_000
    ai_max_cost_usd_per_opportunity: float | None = None
    attachment_max_mb: int = 100
    http_user_agent: str = "govcon-platform/2.0"

    web_bind_host: str = "127.0.0.1"
    web_bind_allow_public: bool = False
    session_ttl_hours: int = Field(default=12, ge=1)

    @field_validator(
        "database_url",
        "sam_api_key",
        "deepseek_api_key",
        "anthropic_api_key",
        "openai_api_key",
        "deepseek_model",
        "ai_review_provider",
        "ai_secondary_review_provider",
        "jev_api_key",
        "jev_base_url",
        "smtp_host",
        "smtp_user",
        "smtp_pass",
        "alert_email_to",
        mode="before",
    )
    @classmethod
    def blank_string_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator(
        "jev_min_confidence",
        "jev_human_review_threshold",
        "ai_max_cost_usd_per_opportunity",
        mode="before",
    )
    @classmethod
    def blank_float_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @model_validator(mode="after")
    def reject_unsolicited_public_bind(self) -> Settings:
        host = self.web_bind_host.strip()
        self.web_bind_host = host
        if host in _PUBLIC_BIND_HOSTS and not self.web_bind_allow_public:
            raise ValueError(
                "WEB_BIND_HOST refuses a public bind. "
                "Set WEB_BIND_ALLOW_PUBLIC=true only when that exposure is intentional."
            )
        return self

    def require_database_url(self) -> str:
        if not self.database_url:
            raise ConfigError("DATABASE_URL is required for this command")
        return self.database_url

    def resolved_prompt_root(self) -> Path:
        if self.prompt_root.is_absolute():
            return self.prompt_root
        try:
            return (repo_root() / self.prompt_root).resolve()
        except RuntimeError:
            return self.prompt_root.resolve()

    @property
    def email_configured(self) -> bool:
        return bool(self.smtp_host and self.alert_email_to)

    def secret_values(self) -> list[str]:
        """Non-empty secret setting values that must never appear in logs."""
        values: list[str] = []
        for value in (
            self.sam_api_key,
            self.deepseek_api_key,
            self.anthropic_api_key,
            self.openai_api_key,
            self.jev_api_key,
            self.smtp_pass,
            self.smtp_user,
        ):
            if value and len(value) >= 8:
                values.append(value)
        return values


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
