# GovCon platform

Collaborative federal-first opportunity and bid workspace. This repository is at **Phase 0**: a runnable project skeleton with configuration, database schema, authentication baseline, and CLI. Live SAM.gov, DIBBS, and USAspending ingestion are not implemented yet.

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

`scripts/smoke.sh` runs upgrade, the demo seed, and status. Later phases extend that script.

## Not in this phase

SAM/DIBBS/USAspending ingestion, solicitation analysis, JEV decisions, proposal generation, the review quorum workflow, and the web UI are later phases. Prompt and JEV files under `src/govcon/prompts/` are inactive placeholders.
