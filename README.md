# GovCon platform

Collaborative federal-first opportunity and bid workspace. Phase 0 is the foundation (schema, auth, CLI). Phase 1 ingests SAM.gov opportunities with immutable snapshots. DIBBS, USAspending, solicitation analysis, and proposals are later phases.

## Prerequisites

- Python 3.12+
- Docker with Compose v2

## Local database

```bash
cp .env.example .env
docker compose up -d
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
govcon db upgrade
govcon db seed-demo-watchlist
govcon status
pytest
```

`docker-compose.yml` runs PostgreSQL 16 with pgvector and publishes it on `127.0.0.1:5432` only. The database name, user, and password match the local URL in `.env.example`. Do not commit `.env`.

## Phase 0 commands

| Command | Purpose |
|---|---|
| `govcon db upgrade` | Apply Alembic migrations |
| `govcon db seed-demo-watchlist` | Insert one empty demo watchlist (idempotent) |
| `govcon status` | Print database connectivity and schema revision |
| `govcon users invite` | Create an invited user |
| `govcon users deactivate` | Disable a user and revoke sessions |
| `govcon users list` | List users without password hashes |
| `govcon ingest sam` | Pull SAM.gov opportunities. Default window is the last 3 UTC days. Requires `SAM_API_KEY`. |
| `govcon ingest sam-backfill` | Pull a longer posted-date range in windows of at most one year. |
| `govcon ingest sam-archive-sweep` | Mark stored SAM rows archived when `archive_date` is past. No network call. |

`scripts/smoke.sh` runs upgrade, the demo seed, and status. It does not call SAM.gov.

Set `SAM_API_KEY` from the SAM.gov Account Details page. The client calls `https://api.sam.gov/opportunities/v2/search` and never prints the key. Description files and attachment downloads are not part of this ingest.

## Not in this phase

DIBBS and USAspending ingestion, solicitation analysis, JEV decisions, proposal generation, the review quorum workflow, and the web UI are later phases. Prompt and JEV files under `src/govcon/prompts/` are inactive placeholders.
