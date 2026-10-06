"""Single pydantic-settings object for process configuration.

Missing optional integrations stay unset. Missing required settings fail when a
command needs them, not when this module is imported.
"""

from __future__ import annotations

from contextvars import ContextVar
from functools import _CacheInfo, lru_cache
from pathlib import Path
from typing import Callable, Literal, Protocol, cast

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from govcon.paths import package_root, repo_root

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
    anthropic_model: str | None = None
    openai_model: str | None = None
    # Opt Claude models with safety classifiers into server-side refusal fallback.
    anthropic_refusal_fallback: bool = True
    ai_primary_provider: str = "deepseek"
    ai_review_provider: str | None = None
    ai_secondary_review_provider: str | None = None

    ai_external_allowed_for_proprietary: bool = False
    ai_external_allowed_for_fci: bool = False
    ai_external_allowed_for_cui: bool = False

    prompt_root: Path = Path("./src/govcon/prompts")
    # Explicit bootstrap mode permits disk prompts only when no registry entry exists.
    prompt_allow_disk_fallback: bool = False
    prompt_strict_json: bool = True
    prompt_fail_on_schema_error: bool = True
    prompt_enable_regression_gate: bool = True
    prompt_require_behavioral_evaluation: bool = True
    prompt_behavioral_evidence_dir: Path | None = None
    prompt_max_retries_on_invalid_json: int = 1

    jev_enabled: bool = True
    jev_api_key: str | None = None
    jev_base_url: str | None = None
    decision_primary_provider: str = "jev"
    decision_fallback_provider: str = "rules"
    jev_min_confidence: float | None = None
    jev_human_review_threshold: float | None = None

    compliance_pass_b_provider: str | None = None
    compliance_pass_b_model: str | None = None
    compliance_escalation_provider: str | None = None
    compliance_escalation_model: str | None = None
    compliance_escalation_min_value: float | None = None
    compliance_high_confidence_threshold: float | None = None
    compliance_low_confidence_threshold: float | None = None
    compliance_merge_similarity: float = Field(default=0.72, gt=0, le=1)
    compliance_possible_duplicate_similarity: float = Field(default=0.4, gt=0, le=1)
    compliance_coverage_min_overlap: float = Field(default=0.6, gt=0, le=1)
    compliance_coverage_partial_overlap: float = Field(default=0.3, gt=0, le=1)
    company_facts_path: Path | None = None

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_pass: str | None = None
    # Delivery requires TLS (port 465 or STARTTLS, certificate verified). This
    # permits a plaintext session only to a relay on localhost/loopback.
    smtp_allow_plaintext_local_relay: bool = False
    alert_email_to: str | None = None
    alert_on_material_deadline_change: bool = True

    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_model_revision: str | None = None
    data_dir: Path = Path("./data")
    outbox_dir: Path = Path("./outbox")
    log_dir: Path = Path("./logs")

    ai_max_input_tokens_per_opportunity: int = Field(default=120_000, ge=1)
    ai_max_input_tokens_per_call: int = Field(default=48_000, ge=1)
    ai_max_output_tokens_per_call: int = Field(default=8_192, ge=1)
    # Source text per extraction/summary call. Kept apart from the per-call input
    # limit: single-call steps (reconciliation, contradictions) need a high limit,
    # while smaller source batches keep long answers under the output cap.
    ai_source_batch_bytes: int = Field(default=24_000, ge=2_000)
    ai_max_provider_retries: int = Field(default=2, ge=0, le=5)
    ai_max_cost_usd_per_opportunity: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    # Operator-supplied upper rate covering input/output, caching, and fallback
    # models. A configured dollar cap fails closed until this rate is supplied.
    ai_budget_usd_per_million_tokens: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    # Share of each opportunity's token and dollar budget that only proposal
    # drafting and proposal review may spend, so preparation cannot use it all.
    ai_proposal_budget_share: float = Field(default=0.25, ge=0, lt=1, allow_inf_nan=False)
    # Email each in-app notification to its user through SMTP_HOST (ADR-070).
    notify_email_enabled: bool = False
    # Review reminders and escalations (ADR-070).
    review_reminder_hours: int = Field(default=48, ge=1)
    review_overdue_hours: int = Field(default=96, ge=1)
    review_escalate_days_before_deadline: int = Field(default=3, ge=0)
    # Our own SAM registration refresh (ADR-072); falls back to ``uei`` in the facts file.
    company_uei: str | None = None
    sam_expiry_alert_days: int = Field(default=60, ge=1)
    company_facts_max_age_days: int = Field(default=3, ge=1)
    # Durable background tasks (ADR-061).
    task_lease_seconds: int = Field(default=300, ge=1)
    task_default_max_attempts: int = Field(default=5, ge=1, le=50)
    task_retry_base_seconds: int = Field(default=30, ge=0)
    task_retry_max_seconds: int = Field(default=3600, ge=1)
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    # OCR for PDF pages without a text layer (ADR-065). Without the Tesseract
    # binary those pages stay flagged as unreadable, as before.
    ocr_enabled: bool = True
    tesseract_cmd: str | None = None
    ocr_lang: str = "eng"
    ocr_dpi: int = Field(default=300, ge=72, le=600)
    ocr_max_pages_per_file: int = Field(default=300, ge=0)
    ocr_min_native_chars_per_page: int = Field(default=20, ge=0)
    # Where attachment bytes live (gap 11). Only "local" exists; every worker
    # must then run on the machine that holds DATA_DIR.
    attachment_store: Literal["local"] = "local"
    attachment_max_mb: int = 100
    # Attachment downloads are HTTPS-only unless this is set deliberately.
    attachment_allow_http: bool = False
    http_user_agent: str = "govcon-platform/2.0"
    dibbs_request_interval_seconds: float = Field(default=2.0, ge=0)
    sam_vendor_cache_hours: int = Field(default=24, ge=1)

    web_bind_host: str = "127.0.0.1"
    web_bind_allow_public: bool = False
    session_ttl_hours: int = Field(default=12, ge=1)
    web_public_origin: str | None = None
    web_secure_cookies: bool = False
    web_csrf_secret: str | None = None
    login_attempt_limit: int = Field(default=10, ge=1)
    login_attempt_window_seconds: int = Field(default=300, ge=1)
    review_conditional_triggers: str = (
        "critical_compliance_risk,low_jev_confidence,reviewer_ai_disagreement,"
        "reviewer_requested_second_review,material_amendment"
    )
    review_high_value_threshold: float | None = None
    review_short_deadline_days: int = Field(default=5, ge=0)
    review_override_allowed: bool = True
    mcp_actor_email: str | None = None

    @field_validator(
        "database_url",
        "tesseract_cmd",
        "company_uei",
        "sam_api_key",
        "deepseek_api_key",
        "anthropic_api_key",
        "openai_api_key",
        "deepseek_model",
        "anthropic_model",
        "openai_model",
        "ai_review_provider",
        "ai_secondary_review_provider",
        "jev_api_key",
        "jev_base_url",
        "compliance_pass_b_provider",
        "compliance_pass_b_model",
        "compliance_escalation_provider",
        "compliance_escalation_model",
        "company_facts_path",
        "embedding_model_revision",
        "smtp_host",
        "smtp_user",
        "smtp_pass",
        "alert_email_to",
        "mcp_actor_email",
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
        "ai_budget_usd_per_million_tokens",
        "compliance_escalation_min_value",
        "compliance_high_confidence_threshold",
        "compliance_low_confidence_threshold",
        "review_high_value_threshold",
        mode="before",
    )
    @classmethod
    def blank_float_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("dibbs_request_interval_seconds", mode="before")
    @classmethod
    def blank_dibbs_interval_uses_default(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return 2.0
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

    def require_sam_api_key(self) -> str:
        if not self.sam_api_key:
            raise ConfigError("SAM_API_KEY is required for this command")
        return self.sam_api_key

    def resolved_prompt_root(self) -> Path:
        if self.prompt_root == Path("./src/govcon/prompts"):
            return package_root() / "prompts"
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
            self.web_csrf_secret,
        ):
            if value and len(value) >= 8:
                values.append(value)
        return values


settings_context: ContextVar[Settings | None] = ContextVar("app_settings", default=None)


@lru_cache(maxsize=1)
def _default_settings() -> Settings:
    return Settings()


class _SettingsGetter(Protocol):
    """Context-aware getter with the default getter's cache controls."""

    cache_clear: Callable[[], None]
    cache_info: Callable[[], _CacheInfo]

    def __call__(self) -> Settings: ...


def _read_settings() -> Settings:
    return settings_context.get() or _default_settings()


# Preserve the public cache management interface used by CLI/tests.
get_settings = cast(_SettingsGetter, _read_settings)
get_settings.cache_clear = _default_settings.cache_clear
get_settings.cache_info = _default_settings.cache_info
