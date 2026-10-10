"""Human-readable, de-duplicated missing-information labels from analysis output."""

from __future__ import annotations

import re

_FIELD_LABELS = {
    "amendment_status": "Amendment status",
    "source_package": "Unread source pages",
    "delivery": "Delivery terms",
    "eligibility": "Eligibility",
    "submission": "Submission instructions",
    "pricing_structure": "Pricing structure",
    "items": "Line items",
    "key_dates": "Key dates",
    "summary": "Solicitation summary",
}
_PART_LEAK = re.compile(
    r"remaining pages\s*\([^)]*\)|part\s+\d+\s+of\s+\d+|pages?\s+\d+\s*[-–]\s*\d+",
    re.I,
)


def _label(field: str) -> str:
    key = field.strip()
    if not key:
        return ""
    return _FIELD_LABELS.get(key, key.replace("_", " ").strip().capitalize())


def normalize_missing_information(items: object) -> list[str]:
    """Dedupe analysis gaps and drop per-part page notes that leak chunking."""
    if not items:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in items if isinstance(items, list) else [items]:
        field = ""
        reason = ""
        if isinstance(item, dict):
            field = str(item.get("field") or "").strip()
            reason = str(item.get("reason") or "").strip()
        elif isinstance(item, str):
            reason = item.strip()
        else:
            continue
        if _PART_LEAK.search(field) or _PART_LEAK.search(reason):
            reason = _PART_LEAK.sub("", reason).strip(" ;,")
            if not field and not reason:
                continue
        label = _label(field)
        text = label or reason
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out
