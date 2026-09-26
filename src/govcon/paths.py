"""Repository path helpers shared by the CLI, migrations, and tests."""

from pathlib import Path


def repo_root() -> Path:
    """Return the repository root containing pyproject.toml and alembic.ini."""
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "alembic.ini").is_file():
            return parent
    raise RuntimeError("repository root not found (expected pyproject.toml and alembic.ini)")
