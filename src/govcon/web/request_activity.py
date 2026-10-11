"""Bounded per-process HTTP telemetry. Never retain URLs, queries, bodies, or identities."""
from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from threading import Lock
from time import monotonic
from uuid import uuid4

from govcon.display_time import format_pt


class RequestActivity:
    def __init__(self) -> None:
        self._lock = Lock()
        self._active: dict[str, dict] = {}
        self._recent: deque[dict] = deque(maxlen=30)
        self._completed = 0
        self._errors = 0
        self._elapsed = 0.0
        self._started = datetime.now(UTC)

    def begin(self, method: str, route: str) -> str:
        key = uuid4().hex
        with self._lock:
            self._active[key] = {"method": method, "route": route,
                                 "started": format_pt(datetime.now(UTC), seconds=True), "clock": monotonic()}
        return key

    def finish(self, key: str, status: int) -> None:
        with self._lock:
            item = self._active.pop(key, None)
            if item is None:
                return
            elapsed = max(0.0, (monotonic() - item.pop("clock")) * 1000)
            self._completed += 1
            self._errors += int(status >= 500)
            self._elapsed += elapsed
            self._recent.appendleft({**item, "status": status, "duration_ms": round(elapsed, 1)})

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "started": format_pt(self._started), "completed": self._completed,
                "active_count": len(self._active),
                "server_errors": self._errors,
                "mean_ms": round(self._elapsed / self._completed, 1) if self._completed else None,
                "active": [{"method": row["method"], "route": row["route"], "started": row["started"],
                            "duration_ms": round(max(0.0, (monotonic() - row["clock"]) * 1000), 1)}
                           for row in list(self._active.values())[:30]],
                "recent": [dict(row) for row in self._recent],
            }
