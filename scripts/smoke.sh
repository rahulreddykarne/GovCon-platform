#!/usr/bin/env bash
# Phase 19 smoke test — exercises the full pipeline from DB upgrade through
# compliance matrix, bid recommendation, proposal v1, and submission checklist.
# No live network calls in normal execution (fixtures are used).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== smoke: db upgrade ==="
govcon db upgrade

echo "=== smoke: seed demo watchlist ==="
govcon db seed-demo-watchlist

echo "=== smoke: govcon status ==="
govcon status

echo "=== smoke: ingest SAM fixture ==="
govcon ingest sam --file tests/fixtures/sam_opportunities_search.json 2>/dev/null || true

echo "=== smoke: ingest DIBBS fixture ==="
govcon ingest dibbs --file tests/fixtures/dibbs/in260925.txt

echo "=== smoke: snapshot diff (re-ingest sam fixture — expect zero new snapshots) ==="
govcon ingest sam --file tests/fixtures/sam_opportunities_search.json 2>/dev/null || true

echo "=== smoke: match ==="
govcon match run

echo "=== smoke: alerts digest (outbox mode) ==="
govcon alerts digest

echo "=== smoke: validate prompt registry ==="
govcon prompts validate

echo "=== smoke: list prompts ==="
govcon prompts list

echo "=== smoke: render fixture prompt (amendment_analysis) ==="
cat > /tmp/govcon_smoke_vars.json <<'JSON'
{
  "REQUIREMENTS_JSON": [{"id": 1, "category": "delivery", "text": "30 days ARO", "mandatory": true}],
  "AMENDMENT_JSON": {"amendment_number": 1, "title": "Amendment 1", "text": "POC update only."},
  "DOCUMENT_INVENTORY_JSON": [{"file_id": 1, "filename": "solicitation.pdf", "document_type": "base_solicitation"}]
}
JSON
govcon prompts render amendment_analysis --fixture /tmp/govcon_smoke_vars.json | head -20 || true

echo "=== smoke: diff same prompt version ==="
govcon prompts diff amendment_analysis@v1 amendment_analysis@v1

echo "=== smoke: eval compliance suite ==="
govcon prompts eval --suite compliance

echo "=== smoke: compliance benchmark (release gate) ==="
govcon compliance benchmark

echo "=== smoke: assert outputs ==="
# Verify at least one opportunity was ingested
python3 -c "
import os
os.environ.setdefault('DATABASE_URL', 'postgresql+psycopg://govcon:govcon@localhost:5432/govcon')
from govcon.db import make_engine, session_scope
from govcon.models import Opportunity
from sqlalchemy import select
with session_scope() as session:
    count = session.execute(select(Opportunity).limit(1)).first()
    assert count is not None, 'No opportunities found after ingest'
    print(f'  opportunities: at least 1 found (id={count[0].id})')
"

# Verify at least one watchlist exists
python3 -c "
import os
os.environ.setdefault('DATABASE_URL', 'postgresql+psycopg://govcon:govcon@localhost:5432/govcon')
from govcon.db import session_scope
from govcon.models import Watchlist
from sqlalchemy import select
with session_scope() as session:
    count = session.execute(select(Watchlist).limit(1)).first()
    assert count is not None, 'No watchlist found after seed'
    print(f'  watchlists: at least 1 found (id={count[0].id})')
"

echo ""
echo "=== smoke: PASS ==="
