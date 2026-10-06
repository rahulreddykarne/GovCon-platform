"""Optimistic concurrency for shared mutable records.

Updates are conditional on the stored version so a writer cannot commit over
a version it has not read.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.orm import Session


class StaleRecordError(Exception):
    """Raised when a writer does not hold the current version."""

    def __init__(self, current: int, expected: int) -> None:
        self.current = current
        self.expected = expected
        super().__init__(f"stale version: expected {expected}, current {current}")


def apply_versioned_update(
    session: Session,
    instance: Any,
    expected_version: int,
    changes: dict[str, Any],
) -> dict[str, Any]:
    """Apply ``changes`` when the database row is still at ``expected_version``."""
    if "version" in changes:
        raise ValueError("version is managed by optimistic concurrency")
    if instance.version != expected_version:
        raise StaleRecordError(instance.version, expected_version)
    previous = {key: getattr(instance, key) for key in changes}
    table = instance.__table__
    # A Core UPDATE returns CursorResult; Session's general annotation also
    # covers ORM query results, which do not expose rowcount.
    result = cast(CursorResult[Any], session.execute(
        update(table)
        .where(table.c.id == instance.id, table.c.version == expected_version)
        .values(**changes, version=expected_version + 1)
    ))
    if result.rowcount != 1:
        session.expire(instance)
        session.refresh(instance)
        raise StaleRecordError(instance.version, expected_version)
    session.expire(instance)
    session.refresh(instance)
    return previous
