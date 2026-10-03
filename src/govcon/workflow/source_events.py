"""Which ingest field events count as a material change to the solicitation.

Kept free of service imports so the ingest layer can use it without cycles.
"""

from __future__ import annotations

MATERIAL_SOURCE_CHANGE_EVENT = "material_source_change"
MATERIAL_SOURCE_CHANGE_HANDLED_EVENT = "material_source_change_handled"
# One row per failed processing attempt; the event stays pending and is retried with backoff.
MATERIAL_SOURCE_CHANGE_FAILED_EVENT = "material_source_change_failed"

# Changes that can alter what the government requires: approvals, proposal
# content, and the compliance matrix built from the old source are stale.
FULL_REVALIDATION_EVENTS = frozenset(
    {
        "description_changed",
        "files_added",
        "files_removed",
        "set_aside_changed",
        "quantity_changed",
        "cancelled",
    }
)

# Changes that only invalidate submission readiness (pre-flight timing checks).
READINESS_ONLY_EVENTS = frozenset({"deadline_changed"})

MATERIAL_EVENT_TYPES = FULL_REVALIDATION_EVENTS | READINESS_ONLY_EVENTS


def change_level(event_types: set[str] | frozenset[str]) -> str | None:
    """``"material"`` / ``"readiness"`` / ``None`` for a set of field-event types."""
    if event_types & FULL_REVALIDATION_EVENTS:
        return "material"
    if event_types & READINESS_ONLY_EVENTS:
        return "readiness"
    return None
