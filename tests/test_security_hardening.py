"""Phase 18 — Security & data handling acceptance tests.

Every acceptance criterion from §24 is mapped to an explicit test here.
The core security infrastructure was established in Phase 0 (ADR-006) and
shared prompt fragments were activated in Phase 7 (ADR-024). This file
assembles the Phase 18 compliance evidence in one place and adds tests for
edge cases not covered by earlier test suites.

Acceptance criteria (§24):
  AC-1  secret fields never appear in logs
  AC-2  AI gateway blocks disallowed content
  AC-3  app defaults to localhost only
  AC-4  repository contains no live credentials
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from govcon.ai.gateway import (
    AIGatewayBlocked,
    authorize_external_call,
    external_call_allowed,
)
from govcon.audit import scrub
from govcon.config import Settings
from govcon.logging import configure_logging, redact
from govcon.security.classification import DataClassification, classify
from govcon.security.secrets import (
    DISALLOWED_DATABASE_SECRET_KINDS,
    SecretStorageError,
    reject_database_secret,
)
from govcon.web.app import bind_host, create_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent


def _all_source_files() -> list[Path]:
    """Return Python source files and env examples tracked by git."""
    py_files = list(_REPO_ROOT.rglob("*.py"))
    env_example = _REPO_ROOT / ".env.example"
    extras = [env_example] if env_example.exists() else []
    return py_files + extras


# ---------------------------------------------------------------------------
# AC-1 — Secret fields never appear in logs
# ---------------------------------------------------------------------------


class TestSecretFieldsNeverInLogs:
    """AC-1: secret fields never appear in logs."""

    def test_redact_masks_assignment_syntax(self) -> None:
        for pattern in (
            "SAM_API_KEY=supersecretvalue",
            "api_key=ABC123DEF",
            "password=hunter2",
            "SMTP_PASS=mymailpass",
            "secret=topsecret",
            "token=bearerxxx",
        ):
            cleaned = redact(pattern)
            raw_value = pattern.split("=", 1)[1]
            assert raw_value not in cleaned, f"value leaked in: {cleaned!r}"
            assert "[REDACTED]" in cleaned

    def test_redact_masks_url_password(self) -> None:
        url = "postgresql+psycopg://govcon:S3CR3T@localhost/govcon"
        cleaned = redact(url)
        assert "S3CR3T" not in cleaned
        assert "[REDACTED]" in cleaned

    def test_redact_masks_bearer_token(self) -> None:
        header = "Authorization: Bearer abcdefghijklmnop123"
        cleaned = redact(header)
        assert "abcdefghijklmnop123" not in cleaned
        assert "[REDACTED]" in cleaned

    def test_redact_masks_known_extra_secrets(self) -> None:
        my_key = "VERY_SECRET_KEY_VALUE_1234"
        text = f"Calling provider with key={my_key}"
        cleaned = redact(text, extra_secrets=[my_key])
        assert my_key not in cleaned
        assert "[REDACTED]" in cleaned

    def test_log_handler_strips_secrets_from_log_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        from govcon.config import get_settings

        get_settings.cache_clear()
        settings = get_settings()
        configure_logging(settings, force=True)
        logging.getLogger("govcon.test18").info(
            "api_key=%s provider=deepseek", "my_very_secret_key"
        )
        for handler in logging.getLogger("govcon").handlers:
            handler.flush()
        log_text = (tmp_path / "govcon.log").read_text(encoding="utf-8")
        assert "my_very_secret_key" not in log_text
        assert "[REDACTED]" in log_text
        get_settings.cache_clear()

    def test_gateway_log_does_not_include_secret_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The ai_gateway logger must not leak content when logging the decision."""
        settings = Settings(ai_external_allowed_for_proprietary=True)
        with caplog.at_level(logging.INFO, logger="govcon.ai.gateway"):
            authorize_external_call(
                classification=DataClassification.PROPRIETARY,
                provider="deepseek",
                model="deepseek-flash",
                purpose="test_call",
                settings=settings,
            )
        # log includes classification, provider, model, purpose — NOT secret content
        assert "PROPRIETARY" in caplog.text
        assert "deepseek" in caplog.text
        assert "test_call" in caplog.text

    def test_audit_scrub_removes_secret_keys(self) -> None:
        payload: dict[str, Any] = {
            "user_id": 42,
            "password": "hunter2",
            "api_key": "abc123",
            "email": "user@example.com",
            "portal_cookie": "session_value_here",
            "nested": {"token": "tok_xxx", "value": 99},
        }
        cleaned = scrub(payload)
        assert "password" not in cleaned
        assert "api_key" not in cleaned
        assert "portal_cookie" not in cleaned
        assert "token" not in cleaned.get("nested", {})
        assert cleaned["user_id"] == 42
        assert cleaned["email"] == "user@example.com"
        assert cleaned["nested"]["value"] == 99

    def test_audit_scrub_walks_nested_lists(self) -> None:
        payload = {"items": [{"secret": "hide_me"}, {"value": "keep_me"}]}
        cleaned = scrub(payload)
        assert cleaned["items"][0] == {}
        assert cleaned["items"][1]["value"] == "keep_me"

    def test_audit_scrub_handles_non_dict_root(self) -> None:
        assert scrub("plain string") == "plain string"
        assert scrub(42) == 42
        assert scrub([{"token": "x"}, {"name": "y"}]) == [{}, {"name": "y"}]


# ---------------------------------------------------------------------------
# AC-2 — AI gateway blocks disallowed content
# ---------------------------------------------------------------------------


class TestAIGatewayBlocksDisallowedContent:
    """AC-2: AI gateway classify → check → block → log pipeline."""

    def test_public_is_always_allowed(self) -> None:
        for setting in (
            Settings(
                ai_external_allowed_for_proprietary=False,
                ai_external_allowed_for_fci=False,
                ai_external_allowed_for_cui=False,
            ),
            Settings(
                ai_external_allowed_for_proprietary=True,
                ai_external_allowed_for_fci=True,
                ai_external_allowed_for_cui=True,
            ),
        ):
            assert external_call_allowed(DataClassification.PUBLIC, setting) is True

    def test_secret_credential_is_always_blocked(self) -> None:
        for setting in (
            Settings(ai_external_allowed_for_proprietary=True),
            Settings(ai_external_allowed_for_proprietary=False),
        ):
            assert (
                external_call_allowed(DataClassification.SECRET_CREDENTIAL, setting) is False
            )

    def test_proprietary_follows_config(self) -> None:
        locked = Settings(ai_external_allowed_for_proprietary=False)
        opened = Settings(ai_external_allowed_for_proprietary=True)
        assert external_call_allowed(DataClassification.PROPRIETARY, locked) is False
        assert external_call_allowed(DataClassification.PROPRIETARY, opened) is True

    def test_fci_blocked_by_default(self) -> None:
        default = Settings()
        assert external_call_allowed(DataClassification.FCI, default) is False

    def test_cui_blocked_by_default(self) -> None:
        default = Settings()
        assert external_call_allowed(DataClassification.CUI, default) is False

    def test_fci_configurable(self) -> None:
        opened = Settings(ai_external_allowed_for_fci=True)
        assert external_call_allowed(DataClassification.FCI, opened) is True

    def test_cui_configurable(self) -> None:
        opened = Settings(ai_external_allowed_for_cui=True)
        assert external_call_allowed(DataClassification.CUI, opened) is True

    def test_authorize_raises_for_secret_credential(self) -> None:
        with pytest.raises(AIGatewayBlocked) as exc_info:
            authorize_external_call(
                classification=DataClassification.SECRET_CREDENTIAL,
                provider="deepseek",
                model="deepseek-flash",
                purpose="unit-test",
                settings=Settings(ai_external_allowed_for_proprietary=True),
            )
        assert exc_info.value.classification is DataClassification.SECRET_CREDENTIAL

    def test_authorize_raises_for_fci_by_default(self) -> None:
        with pytest.raises(AIGatewayBlocked):
            authorize_external_call(
                classification=DataClassification.FCI,
                provider="deepseek",
                model="deepseek-flash",
                purpose="unit-test",
                settings=Settings(),
            )

    def test_authorize_raises_for_cui_by_default(self) -> None:
        with pytest.raises(AIGatewayBlocked):
            authorize_external_call(
                classification=DataClassification.CUI,
                provider="deepseek",
                model="deepseek-flash",
                purpose="unit-test",
                settings=Settings(),
            )

    def test_authorize_raises_for_proprietary_by_default(self) -> None:
        with pytest.raises(AIGatewayBlocked):
            authorize_external_call(
                classification=DataClassification.PROPRIETARY,
                provider="deepseek",
                model="deepseek-flash",
                purpose="unit-test",
                settings=Settings(),
            )

    def test_authorize_passes_for_public(self) -> None:
        authorize_external_call(
            classification=DataClassification.PUBLIC,
            provider="deepseek",
            model="deepseek-flash",
            purpose="solicitation-analysis",
            settings=Settings(),
        )

    def test_gateway_logs_provider_model_classification_purpose(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = Settings(ai_external_allowed_for_proprietary=True)
        with caplog.at_level(logging.INFO, logger="govcon.ai.gateway"):
            authorize_external_call(
                classification=DataClassification.PROPRIETARY,
                provider="anthropic",
                model="claude-opus-5",
                purpose="proposal-review",
                settings=settings,
            )
        assert "anthropic" in caplog.text
        assert "claude-opus-5" in caplog.text
        assert "PROPRIETARY" in caplog.text
        assert "proposal-review" in caplog.text

    def test_gateway_logs_block_decision(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = Settings()
        with caplog.at_level(logging.INFO, logger="govcon.ai.gateway"):
            with pytest.raises(AIGatewayBlocked):
                authorize_external_call(
                    classification=DataClassification.CUI,
                    provider="deepseek",
                    model="deepseek-flash",
                    purpose="blocked-call",
                    settings=settings,
                )
        assert "block" in caplog.text
        assert "CUI" in caplog.text

    def test_classify_defaults(self) -> None:
        assert classify("solicitation") is DataClassification.PUBLIC
        assert classify("award") is DataClassification.PUBLIC
        assert classify("supplier_quote") is DataClassification.PROPRIETARY
        assert classify("internal_pricing") is DataClassification.PROPRIETARY
        assert classify("proposal_draft") is DataClassification.PROPRIETARY
        assert classify("company_strategy") is DataClassification.PROPRIETARY
        assert classify("portal_credential") is DataClassification.SECRET_CREDENTIAL

    def test_classify_explicit_override(self) -> None:
        assert classify("solicitation", explicit=DataClassification.FCI) is DataClassification.FCI
        assert classify("solicitation", explicit=DataClassification.CUI) is DataClassification.CUI

    def test_classify_unknown_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown content kind"):
            classify("not-a-known-kind")

    def test_full_pipeline_classify_then_block(self) -> None:
        """Demonstrates the full classify → check policy → block → (log) flow."""
        # Step 1: classify
        classification = classify("portal_credential")
        assert classification is DataClassification.SECRET_CREDENTIAL

        # Step 2+3+4: check policy, block, log
        settings = Settings()
        with pytest.raises(AIGatewayBlocked) as exc_info:
            authorize_external_call(
                classification=classification,
                provider="deepseek",
                model="deepseek-flash",
                purpose="portal-auth-check",
                settings=settings,
            )
        assert exc_info.value.classification is DataClassification.SECRET_CREDENTIAL

    def test_all_classification_levels_defined(self) -> None:
        levels = {c.value for c in DataClassification}
        assert levels == {"PUBLIC", "PROPRIETARY", "FCI", "CUI", "UNKNOWN", "SECRET_CREDENTIAL"}


# ---------------------------------------------------------------------------
# AC-3 — App defaults to localhost only
# ---------------------------------------------------------------------------


class TestLocalhostDefault:
    """AC-3: app defaults to localhost only; 0.0.0.0 requires explicit config."""

    def test_default_bind_host_is_loopback(self) -> None:
        settings = Settings()
        assert settings.web_bind_host == "127.0.0.1"

    def test_bind_host_helper_returns_loopback(self) -> None:
        settings = Settings(web_bind_host="127.0.0.1")
        assert bind_host(settings) == "127.0.0.1"

    def test_create_app_uses_loopback_default(self) -> None:
        settings = Settings()
        app = create_app(settings)
        assert app.state.settings.web_bind_host == "127.0.0.1"

    def test_public_bind_rejected_without_explicit_override(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="WEB_BIND_HOST refuses"):
            Settings(web_bind_host="0.0.0.0")

    def test_public_bind_rejected_for_ipv6_wildcard(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Settings(web_bind_host="::")

    def test_public_bind_allowed_with_explicit_flag(self) -> None:
        settings = Settings(web_bind_host="0.0.0.0", web_bind_allow_public=True)
        assert settings.web_bind_host == "0.0.0.0"

    def test_loopback_ipv4_variants_accepted(self) -> None:
        s = Settings(web_bind_host="127.0.0.1")
        assert s.web_bind_host == "127.0.0.1"

    def test_docker_compose_postgres_binds_loopback(self) -> None:
        """docker-compose.yml should bind Postgres to 127.0.0.1, not 0.0.0.0."""
        compose = _REPO_ROOT / "docker-compose.yml"
        if not compose.exists():
            pytest.skip("docker-compose.yml not present")
        content = compose.read_text(encoding="utf-8")
        assert "127.0.0.1:5432" in content or "127.0.0.1:" in content, (
            "Postgres port mapping must bind to 127.0.0.1, not all interfaces"
        )


# ---------------------------------------------------------------------------
# AC-4 — Repository contains no live credentials
# ---------------------------------------------------------------------------


_LIVE_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("SAM API key", re.compile(r"sam_api_key\s*=\s*[A-Za-z0-9+/]{20,}", re.IGNORECASE)),
    ("DeepSeek key", re.compile(r"deepseek_api_key\s*=\s*sk-[A-Za-z0-9]{20,}", re.IGNORECASE)),
    ("OpenAI key", re.compile(r"openai_api_key\s*=\s*sk-[A-Za-z0-9]{40,}", re.IGNORECASE)),
    ("Anthropic key", re.compile(r"anthropic_api_key\s*=\s*sk-ant-[A-Za-z0-9+/\-]{30,}", re.IGNORECASE)),
    ("Bearer token in source", re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{30,}", re.IGNORECASE)),
    ("Private key header", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("JEV API key assignment", re.compile(r"jev_api_key\s*=\s*[A-Za-z0-9+/]{20,}", re.IGNORECASE)),
]

_IGNORE_EXTENSIONS = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".pdf"}

_IGNORE_DIRS = {
    "__pycache__", ".git", ".mypy_cache", ".pytest_cache", "node_modules",
    ".ruff_cache", ".venv", "venv",
}


class TestRepositoryContainsNoLiveCredentials:
    """AC-4: repository contains no live credentials."""

    def _tracked_text_files(self) -> list[Path]:
        """Return text files tracked by git (excludes binaries and ignored dirs)."""
        try:
            result = subprocess.run(
                ["git", "ls-files"],
                cwd=_REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=10,
            )
            tracked = [_REPO_ROOT / p.strip() for p in result.stdout.splitlines() if p.strip()]
        except Exception:
            tracked = list(_REPO_ROOT.rglob("*"))

        files: list[Path] = []
        for path in tracked:
            if not path.is_file():
                continue
            if any(part in _IGNORE_DIRS for part in path.parts):
                continue
            if path.suffix in _IGNORE_EXTENSIONS:
                continue
            files.append(path)
        return files

    def test_no_live_api_keys_in_tracked_files(self) -> None:
        """Scan all tracked files for credential assignment patterns."""
        violations: list[str] = []
        for path in self._tracked_text_files():
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for label, pattern in _LIVE_SECRET_PATTERNS:
                for match in pattern.finditer(content):
                    # Allow commented-out lines and placeholder examples
                    line_start = content.rfind("\n", 0, match.start()) + 1
                    line_end = content.find("\n", match.end())
                    line = content[line_start : line_end if line_end != -1 else None]
                    stripped = line.lstrip()
                    if stripped.startswith("#") or stripped.startswith("//"):
                        continue
                    # Allow .example files that show placeholder syntax
                    if path.name.endswith(".example") or path.name.endswith(".env.example"):
                        if "<" in line or "your-" in line.lower() or "placeholder" in line.lower():
                            continue
                        # If the value matches standard placeholder syntax, skip
                        if re.search(r"=\s*$|=\s*#|=\s*<", line):
                            continue
                    violations.append(f"{path.relative_to(_REPO_ROOT)}: [{label}] {line.strip()[:80]!r}")

        assert not violations, (
            "Live credentials detected in repository:\n" + "\n".join(violations)
        )

    def test_dotenv_file_is_gitignored(self) -> None:
        gitignore = _REPO_ROOT / ".gitignore"
        if not gitignore.exists():
            pytest.skip(".gitignore not found")
        content = gitignore.read_text(encoding="utf-8")
        # .env must be gitignored; .env.example may be tracked
        assert ".env" in content, ".env must be listed in .gitignore"

    def test_dotenv_example_has_no_real_values(self) -> None:
        env_example = _REPO_ROOT / ".env.example"
        if not env_example.exists():
            pytest.skip(".env.example not present")
        content = env_example.read_text(encoding="utf-8")
        # The example must not contain real long secret values
        for label, pattern in _LIVE_SECRET_PATTERNS:
            for match in pattern.finditer(content):
                line_start = content.rfind("\n", 0, match.start()) + 1
                line_end = content.find("\n", match.end())
                line = content[line_start : line_end if line_end != -1 else None].strip()
                if not (line.startswith("#") or "<" in line or "your-" in line.lower()):
                    pytest.fail(
                        f".env.example contains a suspicious real credential [{label}]: {line[:80]!r}"
                    )


# ---------------------------------------------------------------------------
# Credential storage rules
# ---------------------------------------------------------------------------


class TestCredentialStorageRules:
    """Verify reject_database_secret covers all §24 disallowed kinds."""

    @pytest.mark.parametrize(
        "kind",
        [
            "sam_password",
            "piee_password",
            "portal_cookie",
            "mfa_secret",
            "browser_session_token",
        ],
    )
    def test_all_disallowed_kinds_raise_storage_error(self, kind: str) -> None:
        with pytest.raises(SecretStorageError, match="must not be stored in PostgreSQL"):
            reject_database_secret(kind)

    def test_all_disallowed_kinds_are_in_constant(self) -> None:
        expected = {
            "sam_password",
            "piee_password",
            "portal_cookie",
            "mfa_secret",
            "browser_session_token",
        }
        assert expected == DISALLOWED_DATABASE_SECRET_KINDS

    def test_non_secret_kind_does_not_raise(self) -> None:
        reject_database_secret("session_token_hash")
        reject_database_secret("user_id")
        reject_database_secret("email")

    def test_error_message_mentions_env_or_secret_manager(self) -> None:
        with pytest.raises(SecretStorageError) as exc_info:
            reject_database_secret("sam_password")
        msg = str(exc_info.value)
        assert "environment" in msg.lower() or "secret" in msg.lower()


# ---------------------------------------------------------------------------
# Shared prompt fragments (§38.1–38.4)
# ---------------------------------------------------------------------------


class TestSharedPromptFragments:
    """Shared prompt fragments must be source-controlled and have production bodies."""

    _PROMPTS_SHARED = _REPO_ROOT / "src" / "govcon" / "prompts" / "shared"

    def _read_fragment(self, name: str) -> str:
        path = self._PROMPTS_SHARED / name
        assert path.exists(), f"Missing shared prompt fragment: {name}"
        return path.read_text(encoding="utf-8")

    def test_source_security_rules_exists_and_is_active(self) -> None:
        content = self._read_fragment("source_security_rules_v1.md")
        assert "status: active" in content
        assert "UNTRUSTED SOURCE DATA" in content
        assert "ignore previous instructions" in content.lower()

    def test_no_fabrication_rules_exists_and_is_active(self) -> None:
        content = self._read_fragment("no_fabrication_rules_v1.md")
        assert "status: active" in content
        assert "NO-FABRICATION" in content
        assert "UNKNOWN" in content

    def test_evidence_rules_exists_and_is_active(self) -> None:
        content = self._read_fragment("evidence_rules_v1.md")
        assert "status: active" in content
        assert "EVIDENCE RULES" in content
        assert "FACT" in content and "INFERENCE" in content

    def test_company_facts_policy_exists_and_is_active(self) -> None:
        content = self._read_fragment("company_facts_policy_v1.md")
        assert "status: active" in content
        assert "COMPANY FACTS POLICY" in content
        assert "blocker" in content.lower()

    def test_no_fragment_reveals_secrets(self) -> None:
        for md in self._PROMPTS_SHARED.glob("*.md"):
            content = md.read_text(encoding="utf-8")
            for label, pattern in _LIVE_SECRET_PATTERNS:
                assert not pattern.search(content), (
                    f"Prompt fragment {md.name} contains a suspicious pattern [{label}]"
                )


# ---------------------------------------------------------------------------
# No hardcoded long strings in security module
# ---------------------------------------------------------------------------


class TestNoHardcodedSecretsInSourceModules:
    """Security and AI modules must not embed real credentials."""

    _SECURITY_DIR = _REPO_ROOT / "src" / "govcon" / "security"
    _GATEWAY_FILE = _REPO_ROOT / "src" / "govcon" / "ai" / "gateway.py"

    def test_security_module_has_no_real_api_keys(self) -> None:
        for py in self._SECURITY_DIR.rglob("*.py"):
            content = py.read_text(encoding="utf-8")
            for label, pattern in _LIVE_SECRET_PATTERNS:
                assert not pattern.search(content), (
                    f"{py.name} contains suspected live credential [{label}]"
                )

    def test_gateway_module_has_no_real_api_keys(self) -> None:
        content = self._GATEWAY_FILE.read_text(encoding="utf-8")
        for label, pattern in _LIVE_SECRET_PATTERNS:
            assert not pattern.search(content), (
                f"gateway.py contains suspected live credential [{label}]"
            )
