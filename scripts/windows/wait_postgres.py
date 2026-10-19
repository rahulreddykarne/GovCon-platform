"""Probe that Postgres accepts SELECT 1 on host:port.

Usage: python wait_postgres.py HOST PORT

Does not read .env, DATABASE_URL, or any secret. Uses trust/peer auth to
the ``postgres`` database. Exit 0 on success, 1 otherwise. Prints nothing
on success; a single non-secret line on failure.
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: wait_postgres.py HOST PORT", file=sys.stderr)
        return 2
    host = argv[1]
    try:
        port = int(argv[2])
    except ValueError:
        print("port must be an integer", file=sys.stderr)
        return 2
    try:
        import psycopg
    except ImportError:
        print("psycopg is not installed", file=sys.stderr)
        return 1
    try:
        with psycopg.connect(
            host=host,
            port=port,
            dbname="postgres",
            connect_timeout=8,
            autocommit=True,
        ) as conn:
            conn.execute("SELECT 1")
    except Exception:  # noqa: BLE001  readiness probe must not leak connection details
        print("SELECT 1 failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
