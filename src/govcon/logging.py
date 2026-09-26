"""Logging setup that never emits secret values."""

from __future__ import annotations

import logging
import re

from govcon.config import Settings, get_settings

_ASSIGNMENT = re.compile(
    r"(?i)([A-Za-z0-9_]*?(?:api[_-]?key|password|passwd|secret|token|cookie|"
    r"authorization|smtp_pass|credential|pwd))"
    r"(\s*[:=]\s*)(\S+)"
)
_URL_PASSWORD = re.compile(r"(://[^:/\s]+:)([^@/\s]+)(@)")
_BEARER = re.compile(r"(?i)\b(bearer\s+)(\S+)")


def redact(text: str, extra_secrets: list[str] | None = None) -> str:
    """Mask credential assignments, URL passwords, bearer tokens, and known secrets."""
    redacted = _URL_PASSWORD.sub(r"\1[REDACTED]\3", text)
    redacted = _BEARER.sub(r"\1[REDACTED]", redacted)
    redacted = _ASSIGNMENT.sub(r"\1\2[REDACTED]", redacted)
    for secret in extra_secrets or []:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


class RedactionFilter(logging.Filter):
    """Apply :func:`redact` to every log record that reaches a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            extra = get_settings().secret_values()
        except Exception:
            extra = []
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        record.msg = redact(rendered, extra)
        record.args = ()
        return True


def configure_logging(settings: Settings | None = None, *, force: bool = False) -> None:
    """Attach stderr and file handlers to the ``govcon`` logger."""
    settings = settings or get_settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("govcon")
    logger.disabled = False
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for name, existing in logging.root.manager.loggerDict.items():
        if isinstance(existing, logging.Logger) and name.startswith("govcon."):
            existing.disabled = False
    if logger.handlers and not force:
        return
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream.addFilter(RedactionFilter())
    file_handler = logging.FileHandler(settings.log_dir / "govcon.log")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(RedactionFilter())
    logger.addHandler(stream)
    logger.addHandler(file_handler)
