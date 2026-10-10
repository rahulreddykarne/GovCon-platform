"""Reject schema-valid model output that does not say anything useful.

A short placeholder or a citation-shaped object with no quote is not a
finished analysis. The call is stored as incomplete and the next run retries it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

_PLACEHOLDERS = frozenset({
    "n/a", "na", "none", "unknown", "not available", "not stated", "null", "-", "ok", "yes", "no",
})


def assess_output_quality(output: BaseModel) -> tuple[str, str]:
    """Return ``(accepted|incomplete, reason)``. Reason is empty when accepted."""
    data = output.model_dump(mode="json")
    texts = _substantive_strings(data)
    cited = _has_citation(data.get("source_refs")) if isinstance(data, dict) else False
    if not texts and not cited:
        return "incomplete", "Schema-valid output had no substantive statement or citation."
    if isinstance(data, dict) and "source_refs" in data and not cited and len(texts) < 2:
        return (
            "incomplete",
            "The analysis has no citation and too little content to treat as finished.",
        )
    return "accepted", ""


def _substantive_strings(value: Any) -> list[str]:
    found: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, str):
            text = item.strip()
            if len(text) >= 24 and text.lower() not in _PLACEHOLDERS:
                found.append(text)
        elif isinstance(item, dict):
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return found


def _has_citation(refs: Any) -> bool:
    if not isinstance(refs, list):
        return False
    for ref in refs:
        if isinstance(ref, str) and len(ref.strip()) >= 12:
            return True
        if isinstance(ref, dict):
            for key in ("quote", "section", "source", "url", "text"):
                value = ref.get(key)
                if isinstance(value, str) and len(value.strip()) >= 12:
                    return True
    return False
