"""Scheduler daemon entry point.

Run ``govcon scheduler start`` (or ``python scheduler.py``) to start the
APScheduler daemon with all Phase 17 job chains.
"""

from __future__ import annotations


def main() -> None:
    """Start the blocking APScheduler daemon."""
    from govcon.config import get_settings
    from govcon.scheduler.runner import start_blocking_scheduler

    settings = get_settings()
    start_blocking_scheduler(settings)


if __name__ == "__main__":
    main()
