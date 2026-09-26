"""Configuration, logging redaction, classification, and the AI gateway."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from govcon.ai.gateway import AIGatewayBlocked, authorize_external_call, external_call_allowed
from govcon.config import Settings, get_settings
from govcon.logging import configure_logging, redact
from govcon.security.classification import DataClassification, classify
from govcon.security.secrets import SecretStorageError, reject_database_secret
from govcon.web.app import bind_host, create_app


def test_settings_import_allows_missing_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.database_url is None
    with pytest.raises(Exception, match="DATABASE_URL"):
        settings.require_database_url()


def test_default_bind_is_loopback() -> None:
    settings = Settings(web_bind_host="127.0.0.1")
    assert bind_host(settings) == "127.0.0.1"
    app = create_app(settings)
    assert app.state.settings.web_bind_host == "127.0.0.1"


def test_public_bind_requires_explicit_override() -> None:
    with pytest.raises(ValidationError):
        Settings(web_bind_host="0.0.0.0")
    allowed = Settings(web_bind_host="0.0.0.0", web_bind_allow_public=True)
    assert allowed.web_bind_host == "0.0.0.0"


def test_redact_masks_assignments_urls_and_bearers() -> None:
    text = (
        "SAM_API_KEY=supersecretvalue "
        "url=postgresql+psycopg://govcon:govcon@localhost/govcon "
        "Authorization: Bearer abcdefghijklmnop"
    )
    cleaned = redact(text)
    assert "supersecretvalue" not in cleaned
    assert "govcon:govcon@" not in cleaned
    assert "abcdefghijklmnop" not in cleaned
    assert "[REDACTED]" in cleaned


def test_log_handler_redacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    get_settings.cache_clear()
    settings = get_settings()
    configure_logging(settings, force=True)
    logger = logging.getLogger("govcon")
    logging.getLogger("govcon.ai.gateway").info("api_key=%s", "supersecretvalue")
    for handler in logger.handlers:
        handler.flush()
    text = (tmp_path / "govcon.log").read_text()
    assert "supersecretvalue" not in text
    assert "[REDACTED]" in text


def test_classification_defaults_and_explicit_controlled_data() -> None:
    assert classify("solicitation") is DataClassification.PUBLIC
    assert classify("award") is DataClassification.PUBLIC
    assert classify("supplier_quote") is DataClassification.PROPRIETARY
    assert classify("proposal_draft") is DataClassification.PROPRIETARY
    assert classify("portal_credential") is DataClassification.SECRET_CREDENTIAL
    assert classify("solicitation", explicit=DataClassification.FCI) is DataClassification.FCI
    assert classify("solicitation", explicit=DataClassification.CUI) is DataClassification.CUI
    with pytest.raises(ValueError):
        classify("not-a-kind")


def test_gateway_blocks_disallowed_classes() -> None:
    locked = Settings(
        ai_external_allowed_for_proprietary=False,
        ai_external_allowed_for_fci=False,
        ai_external_allowed_for_cui=False,
    )
    assert external_call_allowed(DataClassification.PUBLIC, locked) is True
    assert external_call_allowed(DataClassification.PROPRIETARY, locked) is False
    assert external_call_allowed(DataClassification.FCI, locked) is False
    assert external_call_allowed(DataClassification.CUI, locked) is False
    assert external_call_allowed(DataClassification.SECRET_CREDENTIAL, locked) is False

    opened = Settings(
        ai_external_allowed_for_proprietary=True,
        ai_external_allowed_for_fci=True,
        ai_external_allowed_for_cui=True,
    )
    assert external_call_allowed(DataClassification.PROPRIETARY, opened) is True
    assert external_call_allowed(DataClassification.SECRET_CREDENTIAL, opened) is False
    with pytest.raises(AIGatewayBlocked):
        authorize_external_call(
            classification=DataClassification.SECRET_CREDENTIAL,
            provider="deepseek",
            model="deepseek-flash",
            purpose="unit-test",
            settings=opened,
        )


def test_portal_secrets_are_rejected() -> None:
    with pytest.raises(SecretStorageError):
        reject_database_secret("sam_password")
    with pytest.raises(SecretStorageError):
        reject_database_secret("browser_session_token")
    reject_database_secret("session_token_hash")
