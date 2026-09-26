"""Process entrypoint reserved for the scheduling phase.

Phase 17 wires APScheduler. Importing this module does not start jobs.
"""


def main() -> None:
    raise SystemExit("Scheduler is implemented in Phase 17.")


if __name__ == "__main__":
    main()
