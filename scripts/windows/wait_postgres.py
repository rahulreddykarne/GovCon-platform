"""Probe that Postgres accepts SELECT 1 using the app's DATABASE_URL.

Usage: python wait_postgres.py [--timeout SECONDS]

Loads settings the same way the app does (process env and ``.env`` via
``govcon.config``). Never prints DATABASE_URL or the password. Exit 0 on
success, 1 on a failed probe, 2 on usage/config errors.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, ContextManager

from sqlalchemy.engine import make_url


def _ensure_src_path() -> None:
    root = Path(__file__).resolve().parents[2]
    src = root / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


def _redact(text: str, extra: list[str] | None = None) -> str:
    _ensure_src_path()
    from govcon.logging import redact

    return redact(text, extra)


def _password(url: str) -> str | None:
    try:
        value = make_url(url).password
    except Exception:  # noqa: BLE001  never raise while redacting
        return None
    return value or None


def extra_secrets(url: str) -> list[str]:
    password = _password(url)
    return [password] if password else []


def redacted_url(url: str) -> str:
    """Return a log-safe form of the URL (password always hidden)."""
    try:
        hidden = make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001  never leak a malformed URL
        hidden = "postgresql://***"
    return _redact(hidden, extra_secrets(url))


def safe_error(prefix: str, url: str | None, exc: BaseException | None = None) -> str:
    """One-line failure text with credentials stripped."""
    parts = [prefix]
    if url:
        parts.append(f"({redacted_url(url)})")
    if exc is not None:
        parts.append(_redact(str(exc), extra_secrets(url) if url else None))
    return _redact(" ".join(parts), extra_secrets(url) if url else None)


def connect_kwargs(url: str, timeout: int) -> dict[str, Any]:
    """psycopg kwargs from DATABASE_URL. Requires an explicit username.

    Omitting the user would make libpq use the OS account (the laptop bug:
    role "suhas" does not exist).
    """
    parsed = make_url(url)
    backend = parsed.get_backend_name()
    if backend not in {"postgresql", "postgres"}:
        raise ValueError("DATABASE_URL is not PostgreSQL")
    if not parsed.username:
        raise ValueError("DATABASE_URL has no username")
    if not parsed.database:
        raise ValueError("DATABASE_URL has no database name")
    kwargs: dict[str, Any] = {
        "host": parsed.host or "127.0.0.1",
        "port": int(parsed.port or 5432),
        "user": parsed.username,
        "dbname": parsed.database,
        "connect_timeout": int(timeout),
        "autocommit": True,
    }
    if parsed.password is not None:
        kwargs["password"] = parsed.password
    return kwargs


def load_database_target() -> tuple[str, int]:
    """URL and connect timeout from the same Settings object the app uses."""
    _ensure_src_path()
    from govcon.config import get_settings

    settings = get_settings()
    return settings.require_database_url(), int(settings.db_connect_timeout_seconds)


def _default_connect(**kwargs: Any) -> ContextManager[Any]:
    import psycopg

    return psycopg.connect(**kwargs)


def select_one(
    url: str,
    timeout: int,
    *,
    connect: Callable[..., ContextManager[Any]] | None = None,
) -> None:
    """Run SELECT 1. Raises on failure. Does not log the URL or password."""
    kwargs = connect_kwargs(url, timeout)
    opener = connect or _default_connect
    with opener(**kwargs) as conn:
        conn.execute("SELECT 1")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(prog="wait_postgres.py", add_help=True)
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="TCP connect timeout in seconds (default: DB_CONNECT_TIMEOUT_SECONDS)",
    )
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit as exc:
        return int(exc.code or 2)

    try:
        url, default_timeout = load_database_target()
    except Exception as exc:  # noqa: BLE001  config errors must not leak secrets
        print(safe_error("DATABASE_URL is not available", None, exc), file=sys.stderr)
        return 2
    timeout = default_timeout if args.timeout is None else int(args.timeout)
    if timeout < 1:
        print("timeout must be >= 1", file=sys.stderr)
        return 2
    try:
        select_one(url, timeout)
    except Exception as exc:  # noqa: BLE001  probe must not leak connection details
        print(safe_error("SELECT 1 failed", url, exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
