#!/usr/bin/env bash
# GovCon platform - Cloud Agent start (per-boot service reconciliation).
# Brings up PostgreSQL 16 + pgvector and ensures the `govcon` role/database and
# the `vector` extension exist. Idempotent and safe to re-run.
set -euo pipefail

PG_VERSION=16
PG_CLUSTER=main
DB_NAME=govcon
DB_USER=govcon
DB_PASS=govcon

echo "==> [start] Ensuring PostgreSQL ${PG_VERSION} cluster '${PG_CLUSTER}' exists"
if ! pg_lsclusters -h 2>/dev/null | awk '{print $1"/"$2}' | grep -qx "${PG_VERSION}/${PG_CLUSTER}"; then
  sudo pg_createcluster "${PG_VERSION}" "${PG_CLUSTER}"
fi

echo "==> [start] Starting cluster if not already online"
STATUS="$(pg_lsclusters -h 2>/dev/null | awk -v v="${PG_VERSION}" -v c="${PG_CLUSTER}" '$1==v && $2==c {print $4}')"
if [ "${STATUS}" != "online" ]; then
  sudo pg_ctlcluster "${PG_VERSION}" "${PG_CLUSTER}" start
fi

echo "==> [start] Waiting for PostgreSQL to accept connections"
for _ in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then break; fi
  sleep 1
done
sudo -u postgres pg_isready

echo "==> [start] Ensuring role '${DB_USER}', database '${DB_NAME}', and pgvector extension"
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='${DB_USER}') THEN
    CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';
  END IF;
END \$\$;
SQL
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
  sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}"
fi
sudo -u postgres psql -d "${DB_NAME}" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector;"

echo "==> [start] PostgreSQL is ready."
echo "    DATABASE_URL=postgresql+psycopg://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}"
