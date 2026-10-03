#!/usr/bin/env bash
# All database fixtures run in a fresh database created and removed by conftest.
set -euo pipefail
case "${GOVCON_TEST_NO_DB:-}" in 1|true|TRUE|yes|YES) echo "Release smoke requires database-backed checks." >&2; exit 1;; esac
cd "$(dirname "$0")/.."
exec python3 -m pytest tests/test_release_lifecycle.py tests/test_installed_artifact.py -q
