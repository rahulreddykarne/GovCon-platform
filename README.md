# GovCon platform

Collaborative federal-first opportunity and bid workspace. Phase 0 is the foundation (schema, auth, CLI). Phase 1 ingests SAM.gov opportunities with immutable snapshots. Phase 4 ingests DIBBS daily index files into the same opportunity table. Phase 5 stores USAspending contract awards and pricing history. Phase 6 adds SAM entity vendor profiles, competitor intelligence, and buyer contact search. Solicitation analysis and proposals are later phases.

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

## Pipeline troubleshooting

Set `LOG_LEVEL=DEBUG` in `.env` and restart the web server, worker, and scheduler
to enable detailed pipeline diagnostics. For a single PowerShell session:

```powershell
$env:LOG_LEVEL = "DEBUG"
govcon worker start
```

Logs go to stderr and `LOG_DIR/govcon.log` (the default directory is `logs`).
Pipeline events contain JSON with `event`, `phase`, `trace_id`, elapsed time,
and task/opportunity IDs when available. Nested phases share a trace ID within
one execution; retries and later executions can be joined by task/opportunity ID.
Ingestion, matching, downloads, extraction/OCR, summaries, AI budget/provider
attempts, replay, compliance, decisions, review, proposals, and submission
assembly have phase boundaries. Worker steps separately trace prepare, execute,
and publish, including checkpoint skips and task failure classification.

Start with `phase.error` and its matching `phase.error_location` for the exception
class and failing file/function/line. Then follow that `trace_id` backwards through
provider selection, retries, OCR pages, batching, and checkpoints. A completed
phase means the function returned; inspect its result/status and warning events
for refusals, partial output, or fallback decisions. `phase.deferred` is normal
AI replay control flow, not a failed provider call.

```powershell
Get-Content logs/govcon.log -Tail 100 -Wait
Select-String -Path logs/govcon.log* -SimpleMatch '"task_id": 123'
```

New diagnostics record metadata and counts, never document/prompt/response
contents, credentials, or exception messages. Set `LOG_LEVEL=INFO` for phase
boundaries without detailed events. To enable rotation, set a separate `LOG_DIR`
for each process and `LOG_MAX_BYTES=5000000`; `LOG_BACKUP_COUNT` defaults to 5.
Rotation defaults to disabled (`LOG_MAX_BYTES=0`) so web, worker, and scheduler
can keep using the existing shared file without competing file renames on Windows.

## Installed deployment

Wheels include the web templates, static assets, prompts, Alembic migrations,
and compliance benchmark resources. Install the wheel, configure
`DATABASE_URL`, run `govcon db upgrade`, then `govcon prompts sync` before
starting `govcon web serve`. Runtime resources resolve inside the installed
package; a source checkout is not required.

The prompt registry is authoritative by default. An inactive or unreadable
registry prompt stops AI execution. `govcon prompts sync` records versions
inactive and activates a disk-active version only through the audited
activation gate (first sync or a new version); it never undoes an operator's
activation or rollback. A prompt version's content is immutable: a file edited
in place (including a shared include) is refused at load time until it is
re-versioned or re-approved with `govcon prompts sync --reapprove-changed`. `PROMPT_ALLOW_DISK_FALLBACK=true` is an
explicit development/bootstrap option for prompts that have never been synced;
it does not bypass registry denials or errors.

Safety-critical activation also requires a model evaluation of the exact
candidate: run `govcon prompts eval NAME@VERSION --live`, then activate or sync
it. Receipts bind the prompt, shared rules, schemas, synthetic task and injection
fixtures, and configured provider/model. Changed inputs require a fresh
evaluation. `PROMPT_REQUIRE_BEHAVIORAL_EVALUATION=false` is an explicit offline
bootstrap option; its audit record states that model behavior is unevaluated.
Recorded-output compliance replay remains a separate deterministic regression
check. Existing active versions retain their registry state during upgrade.

`govcon enrich ingest-file --opportunity-id ID --file PATH --classification CUI
--source-origin "internal engineering"` requires an explicit classification and
origin. Supported classes are PUBLIC, PROPRIETARY, FCI, CUI and SECRET_CREDENTIAL.
The strictest retained source class applies to derived external calls. Legacy
local imports are UNKNOWN and remain blocked even with proprietary opt-in until
explicitly re-ingested; government-feed downloads carry public source
metadata. Changing a source classification invalidates cached source revisions.

AI calls reserve opportunity input and dollar budgets in `ai_call_usage` before
sending. Reservations survive rollback and provider failures; valid usage can
settle them downward. Missing usage retains the full reservation. Inputs use a
conservative UTF-8 byte bound, and every generative call has an output cap. Transient
provider retries use the same accounting (`AI_MAX_PROVIDER_RETRIES`, default 2). Set
`AI_BUDGET_USD_PER_MILLION_TOKENS` to a verified upper rate for all enabled models
when enabling `AI_MAX_COST_USD_PER_OPPORTUNITY`; a dollar cap without that rate
refuses calls. `AI_PROPOSAL_BUDGET_SHARE` (default 0.25) keeps that share of each
opportunity's budget for proposal drafting and review; preparation may spend only the
rest. Budgets are lifetime totals: a task waiting for budget is retried once, then
waits until the limits are raised (restart the worker) or a person retries it on
`/ops`. Truncated summaries record omitted sources and an incomplete-review
warning. Database pools are reused per URL/process and disposed on shutdown.

The authenticated `/notifications` inbox shows only the recipient's records and
supports CSRF-protected mark-read actions. `pytest` creates and removes a fresh
temporary PostgreSQL database; the configured database needs CREATE DATABASE
permission. `scripts/smoke.sh` runs the complete service lifecycle, recovery and
concurrency checks with real proposal/pricing artifacts, plus an installed-wheel
lifecycle outside the checkout. No external submission occurs in these tests.

For HTTPS behind a reverse proxy, set `WEB_PUBLIC_ORIGIN` to the browser's exact
origin, including any nondefault port, and configure a random `WEB_CSRF_SECRET`
shared by all workers. HTTPS origins enable Secure cookies automatically;
`WEB_SECURE_COOKIES=true` can also require them. Session and CSRF cookie lifetimes
follow `SESSION_TTL_HOURS`. Every form mutation, including login, requires a CSRF
token. Login throttling is bounded per process and client address; deployments
with multiple workers can also enforce an aggregate limit at their proxy.

Commercial facts on submitted or terminal pursuits are locked. Before submission,
reviewers set the quote price, sourcing cost and supplier on the workspace's Products
& Suppliers or Pricing tab (or with MCP `update_pursuit`); changes reopen dependent
reviews and drafts. Approvers can append corrections to
submitted facts with `govcon submission correct-commercial --opportunity-id ID
--actor-email EMAIL --expected-version VERSION --reason REASON --quote-price VALUE`.
Corrections retain the original facts and reference the submitted package in the
audit history.

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

## Phase 6 commands

| Command | Purpose |
|---|---|
| `govcon vendors show --uei` | Lazy SAM entity lookup with cache. Prints registration data plus computed award stats from stored USAspending rows. |
| `govcon vendors show --uei --refresh` | Ignore the cache and call SAM.gov again. |
| `govcon vendors competitors --opportunity-id` | Likely historical competitors by NSN, PSC, agency path segments, and office. |
| `govcon contacts search --name --agency --email` | Search buyer contacts harvested from opportunities. |

Vendor lookup calls `https://api.sam.gov/entity-information/v3/entities` and requires `SAM_API_KEY`. Cached rows are reused for `SAM_VENDOR_CACHE_HOURS` (default 24). Competitor intelligence reads stored awards only; it does not predict future winners.

## Background worker

Approving a bid queues proposal and submission-package generation as a durable
task instead of running it inside the web request (ADR-061, ADR-062). Run at
least one worker beside `govcon web serve`:

| Command | Purpose |
|---|---|
| `govcon worker start` | Process queued tasks until Ctrl-C. Several workers can share one database. |
| `govcon worker run` | Process due tasks, then exit. |
| `govcon tasks list [--status failed]` | Show tasks with status, attempts, owner and error. |
| `govcon tasks show ID` | One task with its next action, checkpoint and result. |
| `govcon tasks retry ID --actor-email` | Re-queue a failed or waiting task (owner/approver). |
| `govcon tasks cancel ID --actor-email --reason` | Cancel an active task; a worker running it discards its result. |

The Proposal tab shows generation progress, and `/ops` lists failed and waiting
tasks with an owner and next action. A worker that dies mid-task loses its
lease after `TASK_LEASE_SECONDS`; another worker resumes the task and the dead
worker's late result is refused. Attachments are still stored locally, so run
all workers on the machine that holds `DATA_DIR` for now.

## Automation (roadmap stages 1–5)

| Where | What |
|---|---|
| Inbox | Matches are sorted by an explainable 0–100 rank ("Why this rank"). Highly ranked matches whose eligibility or data is uncertain say "Needs eligibility decision". |
| Workspace → Overview | Automatic preparation: documents (with OCR), summary, compliance, research, decision package and review setup, step by step. "Re-run preparation" queues it again. |
| Workspace → Products | Supplier quotes (CSV/XLSX read locally; PDFs read by AI only with an owner's authorization) and RFQ drafts, which GovCon never sends. |
| Workspace → Submission | Award records that may match the submitted bid. An approver confirms or dismisses; missing award data never means a loss. |
| `/suppliers` | Suppliers and catalog CSV imports. |
| `/settings` (owner) | Automatic preparation, reviewer assignment, auto-pursue guardrails, the single-reviewer deadline exception, AI reading of supplier quotes, and our SAM registration. |
| `/ops` | Background tasks, failures with owner and next action, and measured AI usage. |

OCR needs the Tesseract binary (`winget install UB-Mannheim.TesseractOCR` or `apt install tesseract-ocr`); without it, image-only pages stay flagged as unreadable. CLI: `govcon pursuit start|prepare`, `govcon sourcing import-catalog|add-quote|draft-rfq`, `govcon company refresh`. Decisions are in `DECISIONS.md` (ADR-061 to ADR-074) and progress in `docs/AUTOMATION_PROGRESS.md`.

## Not in this phase

Solicitation analysis, JEV decisions, proposal generation, the review quorum workflow, MCP, semantic search, and the web UI are later phases. Prompt and JEV files under `src/govcon/prompts/` are inactive placeholders.
