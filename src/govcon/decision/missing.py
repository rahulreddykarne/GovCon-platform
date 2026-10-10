"""Human-readable, de-duplicated missing-information labels from analysis output."""

from __future__ import annotations

import re

_FIELD_LABELS = {
    "amendment_status": "Amendment status",
    "source_package": "Unread source pages",
    "delivery": "Delivery terms",
    "eligibility": "Eligibility",
    "submission": "Submission instructions",
    "submission.method": "Submission method",
    "pricing_structure": "Pricing structure",
    "pricing_structure.format": "Pricing structure",
    "items": "Line items",
    "items.quantity": "Line items",
    "key_dates": "Key dates",
    "summary": "Solicitation summary",
    "clin": "CLIN",
    "nsn": "NSN",
}
_ACRONYMS = {
    "clin": "CLIN",
    "nsn": "NSN",
    "fob": "FOB",
    "psc": "PSC",
    "naics": "NAICS",
    "rfq": "RFQ",
    "rfp": "RFP",
}
_PART_LEAK = re.compile(
    r"remaining pages\s*\([^)]*\)|part\s+\d+\s+of\s+\d+|pages?\s+\d+\s*[-–]\s*\d+",
    re.I,
)
_ARTIFACT = re.compile(r"\(\s*['\"]?[ri]['\"]?\s*/\s*['\"]?[ri]['\"]?\s*\)", re.I)
_INDEX = re.compile(r"\[\d*\]")


def _canonical_key(field: str) -> str:
    cleaned = _ARTIFACT.sub("", field)
    cleaned = _INDEX.sub("", cleaned)
    cleaned = cleaned.replace(" ", "_").strip(" ._")
    return cleaned.casefold()


def _label(field: str) -> str:
    key = _canonical_key(field)
    if not key:
        return ""
    if key in _FIELD_LABELS:
        return _FIELD_LABELS[key]
    if "." in key:
        root = key.split(".", 1)[0]
        if root in _FIELD_LABELS:
            return _FIELD_LABELS[root]
        parent = key.rsplit(".", 1)[0]
        if parent in _FIELD_LABELS:
            return _FIELD_LABELS[parent]
    words = [part for part in key.replace(".", " ").replace("_", " ").split() if part]
    mapped = [_ACRONYMS.get(word, word.capitalize()) for word in words]
    return " ".join(mapped)


def normalize_missing_information(items: object) -> list[str]:
    """Dedupe analysis gaps, collapse path near-dupes, and drop chunking artifacts."""
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
        field = _ARTIFACT.sub("", field).strip()
        reason = _ARTIFACT.sub("", reason).strip()
        if _PART_LEAK.search(field) or _PART_LEAK.search(reason):
            reason = _PART_LEAK.sub("", reason).strip(" ;,")
            if not field and not reason:
                continue
        label = _label(field) if field else ""
        if not label and reason:
            # A bare path like "Pricing structure.format" is a field, not a sentence.
            if "." in reason or reason.casefold() in _FIELD_LABELS or _canonical_key(reason) in _FIELD_LABELS:
                label = _label(reason)
        text = label or reason
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out
