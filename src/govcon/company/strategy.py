"""Owner-edited company strategy. A blank field stays missing.

Recommendations read these values through ``load_company_facts``. Nothing here
invents a NAICS code, a margin, or a certification.
"""

from __future__ import annotations

from typing import Any

FIELDS: tuple[tuple[str, str, str], ...] = (
    ("products", "Products", "list"),
    ("naics_codes", "NAICS", "list"),
    ("psc_codes", "PSC", "list"),
    ("nsns", "NSNs", "list"),
    ("certifications", "Certifications", "list"),
    ("agencies", "Agencies", "list"),
    ("geography", "Geography", "list"),
    ("past_performance", "Past performance", "list"),
    ("suppliers", "Suppliers", "list"),
    ("bid_capacity", "Bid capacity", "text"),
    ("pricing_assumptions", "Pricing assumptions", "text"),
    ("minimum_margin_pct", "Minimum margin", "number"),
)


def blank() -> dict[str, Any]:
    return {name: [] if kind == "list" else None for name, _label, kind in FIELDS}


def present(value: Any, kind: str) -> bool:
    if kind == "list":
        return isinstance(value, list) and any(str(item).strip() for item in value)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, str) and bool(value.strip())


def missing_labels(stored: dict[str, Any] | None) -> list[str]:
    data = stored or {}
    return [label for name, label, kind in FIELDS if not present(data.get(name), kind)]


def apply_strategy(facts: dict[str, Any], stored: dict[str, Any] | None) -> dict[str, Any]:
    """Copy only fields the owner filled. File facts remain for anything still blank."""
    merged = dict(facts)
    data = stored or {}
    for name, _label, kind in FIELDS:
        if present(data.get(name), kind):
            merged[name] = data[name]
    merged["_strategy_missing"] = missing_labels(data)
    return merged


def parse_form(values: dict[str, str | None]) -> dict[str, Any]:
    """Turn posted text into the stored shape. An empty box is missing, not zero."""
    clean: dict[str, Any] = {}
    for name, label, kind in FIELDS:
        raw = (values.get(name) or "").strip()
        if kind == "list":
            parts = [part.strip() for part in raw.replace("\n", ",").split(",")]
            clean[name] = [part for part in parts if part]
        elif kind == "number":
            if not raw:
                clean[name] = None
                continue
            try:
                number = float(raw)
            except ValueError as exc:
                raise ValueError(f"{label} must be a percent") from exc
            if not 0 <= number <= 100:
                raise ValueError(f"{label} must be between 0 and 100")
            clean[name] = number
        else:
            clean[name] = raw or None
    return clean


def display_value(stored: dict[str, Any] | None, name: str) -> str:
    data = stored or {}
    value = data.get(name)
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if value is None:
        return ""
    return str(value)
