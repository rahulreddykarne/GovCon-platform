"""Content-free pipeline tracing shared by CLI, web and background workers."""

from __future__ import annotations

import inspect
import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger("govcon.pipeline")
_context: ContextVar[dict[str, Any]] = ContextVar("pipeline_diagnostics", default={})
_identifiers = ("task_id", "opportunity_id", "proposal_id", "proposal_version_id", "requirement_id")
_numbers = frozenset({
    *_identifiers, "result_id", "attempt", "attempts", "part", "parts", "page",
    "pages", "files", "bytes", "characters", "candidates", "requirements",
    "warnings", "blockers", "calls", "passes", "replayed_calls", "divergences",
    "completed_steps", "elapsed_ms", "result_items", "result_fields", "latency_ms",
    "input_tokens", "output_tokens", "max_output_tokens", "http_status", "timeout_seconds", "confidence",
    "position", "dropped_keys",
})
_labels = frozenset({
    "provider", "model", "prompt", "prompt_version", "schema", "status", "quality",
    "reason", "result_type", "error_type", "task_type", "step", "stage", "bundle",
    "classification", "source", "text_source", "pass_label", "worker_id",
    # A JSON parser message ("Expecting ',' delimiter") and a stop reason: never response text.
    "parse_error", "finish_reason",
})
_flags = frozenset({"use_ai", "force", "cached", "independent", "ready", "lease_lost", "restored"})
_error_reasons = frozenset({
    "blocked_by_policy", "budget_exceeded", "provider_error", "invalid_output",
    "output_truncated", "schema_error", "no_provider", "registry_absent",
    "registry_unavailable", "registry_error", "prompt_unavailable", "render_error",
})


def _safe_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Accept diagnostic metadata only, never payloads, credentials or error messages."""
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        if key in _numbers and isinstance(value, (int, float)) and not isinstance(value, bool):
            safe[key] = value
        elif key in _labels and isinstance(value, str):
            safe[key] = value[:100]
        elif key in _flags and isinstance(value, bool):
            safe[key] = value
    return safe


def diagnostic_event(event: str, *, level: int = logging.DEBUG, **fields: Any) -> None:
    if logger.isEnabledFor(level):
        logger.log(level, json.dumps({"event": event, **_context.get(), **_safe_fields(fields)}, sort_keys=True))


@contextmanager
def pipeline_phase(name: str, **fields: Any) -> Iterator[None]:
    """Log phase boundaries and safe traceback locations, preserving original exceptions."""
    parent = _context.get()
    context = {**parent, "trace_id": parent.get("trace_id") or uuid4().hex[:16], "phase": name}
    context.update(_safe_fields(fields))
    token = _context.set(context)
    started = time.monotonic()
    diagnostic_event("phase.start", level=logging.INFO)
    try:
        yield
    except BaseException as exc:
        deferred = type(exc).__name__ == "CallNeeded" and type(exc).__module__ == "govcon.ai.replay"
        level = logging.DEBUG if deferred else logging.ERROR
        reason = vars(exc).get("reason")
        diagnostic_event("phase.deferred" if deferred else "phase.error", level=level,
                         error_type=type(exc).__name__, reason=reason if isinstance(reason, str) and reason in _error_reasons else None,
                         elapsed_ms=round((time.monotonic() - started) * 1000, 2))
        # Exception messages, locals and source lines can contain submitted data.
        # Locations are sufficient to identify the failing statement without leaking it.
        frames: list[dict[str, Any]] = []
        tb = exc.__traceback__
        while tb is not None:
            frames.append({"file": Path(tb.tb_frame.f_code.co_filename).name,
                           "function": tb.tb_frame.f_code.co_name, "line": tb.tb_lineno})
            tb = tb.tb_next
        if logger.isEnabledFor(level):
            logger.log(level, json.dumps({"event": "phase.deferred_location" if deferred else "phase.error_location", **context,
                                         "frames": frames[-8:]}, sort_keys=True))
        raise
    else:
        diagnostic_event("phase.complete", level=logging.INFO,
                         elapsed_ms=round((time.monotonic() - started) * 1000, 2))
    finally:
        _context.reset(token)


def _attributes(value: Any) -> Mapping[str, Any]:
    """Read already-loaded fields only; never invoke ORM lazy loading for logging."""
    try:
        return vars(value)
    except TypeError:
        return {}


def _call_identifiers(arguments: Mapping[str, Any]) -> dict[str, Any]:
    fields = _safe_fields(arguments)
    if "prompt_name" in arguments:
        fields["prompt"] = arguments["prompt_name"]
    if "bundle_name" in arguments:
        fields["bundle"] = arguments["bundle_name"]
    for name in ("ctx", "claim", "task", "opportunity", "prepared"):
        attributes = _attributes(arguments.get(name))
        for key in _identifiers:
            if key in attributes:
                fields.setdefault(key, attributes[key])
        if name == "claim":
            for key in ("task_type", "worker_id"):
                if key in attributes:
                    fields.setdefault(key, attributes[key])
        if name in {"task", "opportunity"} and "id" in attributes:
            fields.setdefault(f"{name}_id", attributes["id"])
    return fields


def _result_metadata(value: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {"result_type": type(value).__name__}
    data = value if isinstance(value, Mapping) else _attributes(value)
    fields.update({key: data[key] for key in (*_identifiers, "status", "provider", "model", "quality", "ready") if key in data})
    if "id" in data:
        fields["result_id"] = data["id"]
    if isinstance(value, Mapping):
        fields["result_fields"] = len(value)
    elif isinstance(value, (list, tuple)):
        fields["result_items"] = len(value)
    if isinstance(value, str) and value in {
        "completed", "failed", "blocked", "cancelled", "running", "queued", "lease_lost",
        "waiting_for_input", "waiting_for_budget", "retrying",
    }:
        fields["status"] = value
    for attribute, label in (("page_count", "pages"), ("latency_ms", "latency_ms")):
        if attribute in data:
            fields[label] = data[attribute]
    if isinstance(data.get("text"), str):
        fields["characters"] = len(data["text"])
    return fields


def trace_phase[**P, R](name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Trace a synchronous pipeline function without changing its signature or result."""
    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        signature = inspect.signature(function)

        @wraps(function)
        def traced(*args: P.args, **kwargs: P.kwargs) -> R:
            arguments = signature.bind_partial(*args, **kwargs).arguments
            with pipeline_phase(name, **_call_identifiers(arguments)):
                result = function(*args, **kwargs)
                diagnostic_event("phase.result", **_result_metadata(result))
                return result

        return traced
    return decorate
