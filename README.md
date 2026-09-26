# GovCon platform

Collaborative federal-first opportunity and bid workspace. Phase 0 is the foundation (schema, auth, CLI). Phase 1 ingests SAM.gov opportunities with immutable snapshots. Phase 4 ingests DIBBS daily index files into the same opportunity table. Phase 5 stores USAspending contract awards and pricing history. Solicitation analysis and proposals are later phases.

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

## Phase 3 commands

| Command | Purpose |
|---|---|
| `govcon alerts digest` | Send one HTML digest of unalerted `new` matches, grouped by watchlist. Uses SMTP when `SMTP_HOST` and `ALERT_EMAIL_TO` are set; otherwise writes HTML under `OUTBOX_DIR`. An empty run sends nothing. A repeat run does not alert the same match again. When `ALERT_ON_MATERIAL_DEADLINE_CHANGE` is true, a `deadline_changed` event after `alerted_at` can send one amendment alert. |

## Phase 4 commands

| Command | Purpose |
|---|---|
| `govcon ingest dibbs` | Download the newest DIBBS index file (`inYYMMDD.txt`) from the recent RFQ page and upsert it. |
| `govcon ingest dibbs --date YYYY-MM-DD` | Download that post date's index file. |
| `govcon ingest dibbs --file path` | Ingest a local index file. Does not call the network. |

The original index bytes are kept under `DATA_DIR/dibbs/`. Re-running an unchanged file does not add snapshots. NSN and quantity coverage are logged. The per-solicitation PDF zip and the batch-quote template are not downloaded. Set `DIBBS_REQUEST_INTERVAL_SECONDS` to space requests (default 2).

Set `SAM_API_KEY` from the SAM.gov Account Details page. The client calls `https://api.sam.gov/opportunities/v2/search` and never prints the key. Description files and attachment downloads are not part of this ingest.

## Phase 5 commands

| Command | Purpose |
|---|---|
| `govcon ingest usaspending` | Pull contract awards for PSC and NAICS codes on enabled watchlists. The first run looks back 3 years. Later runs use last-modified date since that window. |
| `govcon ingest usaspending --backfill` | Force the 3-year action-date pull. |
| `govcon ingest usaspending --from YYYY-MM-DD --to YYYY-MM-DD` | Pull an explicit action-date window. Add `--modified` to use last-modified date. |
| `govcon ingest usaspending --file path` | Ingest a local spending-by-award JSON file. Does not call the network or move the incremental window. |
| `govcon awards price-history --nsn` | Award history for one NSN. Unit price is printed only when stored. |
| `govcon awards price-history-psc --psc --keywords` | Awards for a PSC prefix. Keywords are whole words. |
| `govcon awards history --agency` | Awards for an awarding agency. |
| `govcon awards top` | Recipients with the most stored awards. |
| `govcon awards recompete` | Older awards from the `award_recompete_candidates` view. |

USAspending search does not use an API key. The client calls `https://api.usaspending.gov/api/v2/search/spending_by_award/`. Award amount is not turned into a unit price. A digest includes recent comps when stored awards match the opportunity NSN or PSC.

## Not in this phase

Vendor profiles, solicitation analysis, JEV decisions, proposal generation, the review quorum workflow, and the web UI are later phases. Prompt and JEV files under `src/govcon/prompts/` are inactive placeholders.
