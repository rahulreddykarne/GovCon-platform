#!/usr/bin/env bash
# GovCon platform - Cloud Agent install (idempotent repository bootstrap).
# Runs after the repo is checked out. Safe to run repeatedly.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> [install] Ensuring system packages (PostgreSQL 16 + pgvector, build tooling)"
# PostgreSQL 16 and pgvector ship directly in the Ubuntu 24.04 repos.
export DEBIAN_FRONTEND=noninteractive
if ! command -v pg_ctlcluster >/dev/null 2>&1 || ! dpkg -s postgresql-16-pgvector >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y --no-install-recommends \
    postgresql-16 postgresql-16-pgvector libpq-dev \
    build-essential python3-venv python3-dev
else
  echo "    system packages already present; skipping apt install"
fi

echo "==> [install] Ensuring Python virtualenv at .venv"
if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip --quiet

echo "==> [install] Installing Python dependencies"
if [ -f pyproject.toml ]; then
  # Phase 0+ defines the real dependency set; prefer it once it exists.
  echo "    pyproject.toml found -> pip install -e ."
  python -m pip install -e ".[dev]" 2>/dev/null || python -m pip install -e .
elif [ -f requirements.txt ]; then
  echo "    requirements.txt found -> pip install -r requirements.txt"
  python -m pip install -r requirements.txt
else
  # No project manifest yet (pre-Phase-0). Install the foundation stack from
  # SHARED_ARCHITECTURE_REFERENCE.md section 2 so the environment is usable and
  # the database foundation can be exercised end to end.
  echo "    no project manifest yet -> installing foundation stack from the spec"
  python -m pip install --quiet \
    "sqlalchemy>=2.0" "psycopg[binary]>=3.1" "pgvector>=0.2.5" "alembic>=1.13" \
    "pydantic>=2.6" "pydantic-settings>=2.2" "typer>=0.12" "httpx>=0.27" "tenacity>=8.2" \
    "apscheduler>=3.10" "fastapi>=0.110" "uvicorn[standard]>=0.29" "jinja2>=3.1" \
    "python-dotenv>=1.0" "itsdangerous>=2.1" "passlib[bcrypt]>=1.7" "pytest>=8.0" "fastmcp>=2.0"
fi

echo "==> [install] Done. Activate with: source .venv/bin/activate"
