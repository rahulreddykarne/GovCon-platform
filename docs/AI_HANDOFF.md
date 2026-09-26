# AI handoff

Append a new entry after each implementation session. Do not rewrite earlier entries. Do not start the next phase in the same session that finishes the current one.

## 2026-09-26 20:00 UTC — PHASE_04_DIBBS

- Agent/model identity: Cursor cloud, model `grok-4.7` (params: reasoning_effort=high, fast=true)
- Datetime (UTC): 2026-09-26 20:00 UTC
- Phase/task: PHASE_04_DIBBS
- Run: https://cursor.com/agents/bc-2058fbc4-7d59-5a06-a61e-fd49eb18a6d6
- Branch: `cursor/phase-04-dibbs-a6d6`
- Base: `main` at `49a3b99` (Phase 3 alert digests, pull request #6)
- Pull request: pending

### Files changed

- `src/govcon/ingest/dibbs.py`
- `src/govcon/ingest/__init__.py`
- `src/govcon/cli.py`
- `src/govcon/config.py`
- `tests/test_dibbs.py`
- `tests/fixtures/dibbs/in260925.txt`
- `.env.example`
- `README.md`
- `IMPLEMENTATION_STATUS.md`
- `DECISIONS.md`
- `SPEC_DEVIATIONS.md`
- `docs/AI_HANDOFF.md` (this file)

### What shipped

- `govcon ingest dibbs` downloads the newest fixed-width DIBBS index (`inYYMMDD.txt`) from the recent RFQ page, retains the original bytes under `DATA_DIR/dibbs/`, and upserts one opportunity per line.
- `govcon ingest dibbs --date YYYY-MM-DD` downloads that post date's index. `govcon ingest dibbs --file` ingests a local index and does not open the network.
- Parsed fields are solicitation number, dashed NSN when the value is 13 digits, nomenclature, quantity, unit, return-by date, set-aside code, buyer code, AMSC, and the RFQ record URL.
- `source_id` is `solicitation:purchase_request`. A changed line writes a snapshot and field events through the Phase 1 upsert. An unchanged re-run does not.
- NSN and quantity coverage are logged. The saved 2026-09-25 file is 521/523 NSNs and 523/523 quantities.
- DIBBS rows are ordinary `source='dibbs'` opportunities. The Phase 2 watchlist engine matches them on NSN, set-aside, keyword, and source without a matcher change.
- `caYYMMDD.zip` and `bqYYMMDD.zip` are not downloaded. Individual RFQ HTML pages are not fetched.

### ADRs / DECISIONS touched

- ADR-020 in `DECISIONS.md`: index layout, line identity, dashed NSN, FSC in `psc_code`, return-by end of UTC day, consent banner, and request spacing.
- DEV-002 in `SPEC_DEVIATIONS.md`: retain the index only.
- Phase 0 ADR-001 through ADR-014, Phase 1 ADR-015 through ADR-017, Phase 2 ADR-018, and Phase 3 ADR-019 were not changed.

### Migrations

None. Phase 4 uses the Phase 0 schema (`97cb081e9a8e`).

### Tests

Command: `pytest`

Result: **81 passed, 1 skipped** (Phase 1 live-pull skip unchanged).

Covered acceptance checks:

- The 2026-09-25 fixture (newest index listed on 2026-09-26) ingests 523 rows.
- Coverage is logged: NSN 521/523 (0.9962), quantity 523/523 (1.0000).
- A second ingest inserts 0, updates 0, and leaves the snapshot count unchanged.
- A changed quantity and return-by write `quantity_changed`, `deadline_changed`, and a second snapshot. A later payload with status `cancelled` writes `cancelled`.
- A dibbs-only NSN watchlist matches the DIBBS row and not a SAM row with the same NSN. A sam+dibbs watchlist matches both. Set-aside `Y` matches 106 DIBBS rows. Keyword `helmet` matches the fixture row.
- CLI `--file` prints coverage and is idempotent. Consent download and newest-index selection are mocked and do not request the zip files.

### VERIFY outcomes

Checked live on 2026-09-26.

- Pages: `https://www.dibbs.bsm.dla.mil/` is DIBBS 6.3.2. Downloads point at `https://www.dibbs.bsm.dla.mil/RFQ/RFQDates.aspx?category=recent`. Documents are on `https://dibbs2.bsm.dla.mil/`.
- Batch mechanism: each post date has `caYYMMDD.zip` (solicitation PDF/HTML), `inYYMMDD.txt` (index), and `bqYYMMDD.zip` (quote template). Layout help is `https://www.dibbs.bsm.dla.mil/Rfq/RfqFileDefs.aspx`. The index is 140 fixed-width characters. The quote template is comma-delimited and documented at the batch-quoting help page (last updated 30-APR-2024).
- Fixture: `tests/fixtures/dibbs/in260925.txt` (74,266 bytes, 523 records) from `https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive/in260925.txt`. The recent page at 2026-09-26 19:48 UTC listed 09-25-2026 as the newest post date. Help text says today's file is posted the next day. 2026-09-26 is a Saturday, so no `in260926.txt` was listed.
- Access: public information may be copied (DLA privacy notice). Unauthorized uploads are prohibited. A session consent cookie is set by posting `butAgree=OK`. Account terms apply to quoting users.
- Robots: `https://www.dibbs.bsm.dla.mil/robots.txt` is HTTP 404 after consent. `https://dibbs2.bsm.dla.mil/robots.txt` is the documents site's file-not-found page. No disallow or crawl-delay is published. This client waits 2 seconds between requests by default.
- One record URL was opened to confirm the pattern: `https://www.dibbs.bsm.dla.mil/RFQ/RfqRec.aspx?sn=SPE1C126T1698` returned the RFQ record. That page is not fetched during ingest.

### Known problems

- The index has a buyer code, not a buyer name or email. Contact rows are not created.
- Two of 523 lines are item type `1` but not 13-digit NSNs (`5815LLNC02443`, `9535LLNCA9756`). They keep quantity and leave `nsn` null.
- The index has no cancellation column. A line missing from a later day is not marked cancelled.
- `psc_code` stores the FSC (first four digits of a 13-digit NSN). The index has no separate PSC or NAICS.
- Return-by is stored as 23:59:59 UTC because the file has a date only.
- If the process stops after the file is saved and before the database commit, the next run ingests the retained file again. The database upsert is still idempotent.

### Unfinished work

- Phase 5 USAspending awards and pricing (`PHASE_05_AWARDS_PRICING.md`) — not started in this run.
- PDF and HTML solicitation files inside `caYYMMDD.zip` stay out of this ingest.
- DIBBS scheduling stays in Phase 17.

### Recommended next task

Phase 5 — USAspending awards and pricing (`PHASE_05_AWARDS_PRICING.md`). **Do not start until this Phase 4 pull request is merged and its gates pass.**

## 2026-09-26 19:10 UTC — PHASE_03_ALERTS

- Agent/model identity: Cursor cloud, model `grok-4.7-high-fast`
- Datetime (UTC): 2026-09-26 19:10 UTC
- Phase/task: PHASE_03_ALERTS
- Run: https://cursor.com/agents/bc-24fe0acb-591b-53a4-ab81-71fab101b177
- Branch: `cursor/phase-03-alerts-b177`
- Base: `main` at `91bdc73` (Phase 2 watchlist matching, pull request #5)
- Pull request: https://github.com/rahulreddykarne/GovCon-platform/pull/6
- Implementation commit: `f8b567c`

### Files changed

- `src/govcon/alerts/digest.py`
- `src/govcon/alerts/__init__.py`
- `src/govcon/cli.py`
- `src/govcon/config.py`
- `tests/test_alerts.py`
- `.env.example`
- `README.md`
- `IMPLEMENTATION_STATUS.md`
- `DECISIONS.md`
- `SPEC_DEVIATIONS.md`
- `docs/AI_HANDOFF.md` (this file)

### What shipped

- `govcon alerts digest` collects unalerted `new` matches on enabled watchlists and groups them by watchlist in one HTML message.
- The message includes title, agency, source, PSC, NAICS, set-aside, deadline, UTC calendar days remaining, estimated value when stored, and a direct http(s) source link.
- SMTP is used when `SMTP_HOST` and `ALERT_EMAIL_TO` are both set. Otherwise the HTML file is written under `OUTBOX_DIR`.
- An empty run sends no message and writes no file.
- A repeat run does not alert the same match. Delivery sets `alerted_at` and moves `new` to `seen`.
- When `ALERT_ON_MATERIAL_DEADLINE_CHANGE` is true (the default), a `deadline_changed` event newer than `alerted_at` can send one amendment alert. The same event is not sent again. Other field changes do not re-alert. Setting the flag false turns the amendment alert off.
- Historical awards, competitors, and bid recommendation status are not rendered. DIBBS, USAspending, vendors, AI analysis, JEV, MCP, semantic search, and the web UI were not started.

### ADRs / DECISIONS touched

- ADR-019 in `DECISIONS.md`: one digest per run, SMTP versus outbox, `alerted_at` watermark, deadline-only re-alert, no new table.
- Phase 0 ADR-001 through ADR-014, Phase 1 ADR-015 through ADR-017, and Phase 2 ADR-018 were not changed.

### Migrations

None. Phase 3 uses the Phase 0 schema (`97cb081e9a8e`).

### Tests

Command: `pytest`

Result: **67 passed, 1 skipped** (Phase 1 live-pull skip unchanged). The same command passed a second time against the database left by the first run.

Covered acceptance checks:

- Empty day sends nothing and writes no outbox file.
- Repeat run does not write a second file and does not move `alerted_at`.
- A material deadline change generates an amendment alert when `ALERT_ON_MATERIAL_DEADLINE_CHANGE` is true, and a third run does not repeat it.
- The same deadline change sends nothing when the flag is false.
- A non-deadline field change does not re-alert.
- HTML includes the required fields, escapes text, and ignores non-http(s) links.
- SMTP delivery is mocked; a failed send leaves the match unalerted and redacts the SMTP password.
- `govcon alerts digest` writes the outbox and a second invocation reports no message.

### VERIFY outcomes

`PHASE_03_ALERTS.md` contains no `⚠️ VERIFY` items. No live external interface was required. Master §2 already names `smtplib`, and the SMTP and `OUTBOX_DIR` settings were added in Phase 0. SMTP behavior was checked with a fake client, not a live mail server.

### Known problems

- If the process dies after SMTP accepts the message, or after the outbox file is written, and before the database commit, the next run can deliver that digest once more.
- A sources-only watchlist still matches every opportunity from its sources, so the digest can be broad until targeting codes are set.
- Days remaining are whole UTC calendar days. The matcher still uses exact 86400-second spans for `min_deadline_days`.
- Digest scheduling is not wired. Phase 17 owns the job clock.

### Unfinished work

- Phase 4 DIBBS ingestion (`PHASE_04_DIBBS.md`) — not started in this run.
- Later digest enrichment (historical awards, likely competitors, bid recommendation status) stays in later phases.
- In-app notification center and bid/proposal notifications were not built.

### Recommended next task

Phase 4 — DIBBS ingestion (`PHASE_04_DIBBS.md`). **Do not start until this Phase 3 pull request is merged and its gates pass.**

## 2026-09-26 18:58 UTC — PHASE_02_MATCHING

- Agent/model identity: Cursor cloud, model `composer-2.5` (CoS-selected)
- Datetime (UTC): 2026-09-26 18:58 UTC
- Phase/task: PHASE_02_MATCHING
- Run: https://cursor.com/agents/bc-a779864f-ed95-592a-a2ab-61aabff2d53f
- Branch: `cursor/phase-02-matching-d53f`
- Pull request: https://github.com/rahulreddykarne/GovCon-platform/pull/5
- Commits: `fef3276`, `90ab931`

### Files changed

- `src/govcon/matching/engine.py`
- `src/govcon/matching/watchlists.py`
- `src/govcon/matching/__init__.py`
- `src/govcon/cli.py`
- `tests/test_matching.py`
- `tests/test_cli_and_schema.py`
- `IMPLEMENTATION_STATUS.md`
- `DECISIONS.md`
- `SPEC_DEVIATIONS.md`
- `docs/AI_HANDOFF.md` (this file)

### What shipped

- Deterministic watchlist matching: every non-empty rule group must pass; values inside a group are OR'd; empty groups are wildcards.
- Rule groups: PSC prefix, NAICS prefix, keywords, exclude keywords (whole-word veto), exact NSN, set-asides, source filters, min/max estimated value when known, minimum days until deadline.
- Unknown opportunity values do not fail value filters; `matched_on.groups.value.status` is `unknown`.
- Rule-hit score and explainable `matched_on` JSON evidence per match.
- Idempotent upsert on `(opportunity_id, watchlist_id)`; existing match `status` is preserved on update.
- CLI: `govcon match run`, `govcon match rebuild --watchlist N`, `govcon watchlist add`, `govcon watchlist list`, `govcon watchlist edit`, `govcon watchlist disable`.

### ADRs / DECISIONS touched

- ADR-018 in `DECISIONS.md`: matching semantics, whole-word keyword/exclude handling, unknown value behavior, rebuild stale-match removal, idempotent upsert.
- Phase 0 ADR-001 through ADR-014 and Phase 1 ADR-015 through ADR-017 were not changed.
- No new table and no Alembic revision.

### Migrations

None. Phase 2 uses the Phase 0 schema (`97cb081e9a8e`).

### Tests

Command: `pytest`

Result: **50 passed, 1 skipped** (Phase 1 live-pull skip unchanged).

Covered acceptance checks:

- PSC prefix match and non-match.
- NAICS prefix match and non-match.
- Exclude keyword veto (whole-word; `unclassified` does not trigger `classified`).
- Wildcard empty PSC group while NAICS still filters.
- Unknown estimated value passes when `max_value` is configured; known out-of-range value fails.
- Idempotent match upsert (second run inserts 0, unchanged 1).
- CLI `match run`, `watchlist add/list/disable`, and `match rebuild` stale removal.

### VERIFY outcomes

`PHASE_02_MATCHING.md` contains no `⚠️ VERIFY` items. No live external interface verification was required for this phase.

### Known problems

- A sources-only watchlist (such as the seeded demo watchlist before targeting arrays are filled) matches every opportunity from the configured sources. Operators should add PSC/NAICS/keyword filters before expecting a narrow shortlist.
- `govcon match run` evaluates all enabled watchlists; disable unused watchlists to avoid broad matches.

### Unfinished work

- Phase 3 alert digests (`PHASE_03_ALERTS.md`) — not started in this run.
- Semantic/vector matching, AI bid decisions, and proposal workflow remain out of scope per phase boundaries.

### Recommended next task

Phase 3 — alert digests (`PHASE_03_ALERTS.md`). **Do not start until pull request #5 is merged and CI gates pass.** Phase 3 (alerts), semantic matching, AI bid decisions, and proposal workflow were not started in this run.

- Follow-up (2026-09-26 18:59 UTC): `IMPLEMENTATION_STATUS.md` Phase 2 Commit/PR column updated to https://github.com/rahulreddykarne/GovCon-platform/pull/5.

## 2026-09-26 18:50 UTC — PHASE_01_SAM_INGESTION

- Agent/model identity: Cursor cloud, model `grok-4.7-high-fast`
- Datetime (UTC): 2026-09-26 18:50 UTC
- Phase/task: PHASE_01_SAM_INGESTION
- Run: https://cursor.com/agents/bc-9e68ed03-0d1b-5e9f-899c-3926d53343a0
- Branch: `cursor/phase-01-sam-ingestion-43a0`
- Pull request: https://github.com/rahulreddykarne/GovCon-platform/pull/3
- Commits: `bba0ca2`, `f9ad00d`, `dbce6ce`

### Files changed

- `src/govcon/ingest/sam_opportunities.py`
- `src/govcon/ingest/snapshots.py`
- `src/govcon/ingest/runs.py`
- `src/govcon/ingest/__init__.py`
- `src/govcon/cli.py`
- `src/govcon/http.py`
- `src/govcon/config.py`
- `src/govcon/models.py` (annotation only: `opportunities.poc` may be a JSON object or array)
- `tests/test_sam_ingestion.py`
- `tests/fixtures/sam_opportunities_search.json`
- `IMPLEMENTATION_STATUS.md`
- `DECISIONS.md`
- `SPEC_DEVIATIONS.md`
- `README.md`
- `.env.example`
- `docs/AI_HANDOFF.md` (this file)

### What shipped

- `govcon ingest sam` calls the SAM.gov Get Opportunities search. The default posted window is the last 3 UTC days, inclusive of today. The command pages until `totalRecords`, using `offset` as a page index.
- `govcon ingest sam-backfill` splits a longer posted-date range into windows of at most one year.
- `govcon ingest sam-archive-sweep` marks stored SAM rows `archived` when `archive_date` is before today UTC. It does not open a network client and does not write a snapshot.
- Each opportunity is upserted on `(source='sam', source_id=noticeId)`. The raw search object is stored. A snapshot and field-diff events are written only when the canonical content hash changes. Events are inserted before the current row is updated.
- Tracked diffs include deadline, set-aside, status, title, description hash, links/attachments, quantity, and estimated value min/max. A deadline change emits `deadline_changed`.
- Buyer contacts are upserted when both email and agency path are present.
- NSN, quantity, and estimated value are parsed only from explicit labels or explicit value fields. `award.amount` is not copied into estimated value.
- Description and attachment URLs are kept on the raw payload and `links`. Their bytes are not downloaded.

### ADRs / DECISIONS touched

- ADR-015 in `DECISIONS.md`: production search URL, auth, pagination, notice id, attachment fields, and retry behavior.
- ADR-016 in `DECISIONS.md`: one SHA-256 for `raw_hash` and `opportunity_snapshots.content_hash`; archive sweep is local and does not snapshot.
- ADR-017 in `DECISIONS.md`: contact unique key, NSN shape, and the rule that quantity and estimated value stay null unless the source states them.
- `SPEC_DEVIATIONS.md` Phase 1: no behavior deviation from `MASTER_SPEC_v2.5.md`. The missing live key is recorded there as an environment limit, not a product change.
- Phase 0 ADR-001 through ADR-014 were not changed.
- HTTP retries stay in `govcon.http.request_with_retry`. SAM passes a longer wait and more attempts. The default wait used by other callers is unchanged.
- No new table and no Alembic revision.

### Migrations

None. Phase 1 uses the Phase 0 schema (`97cb081e9a8e`).

### Tests

Command: `pytest`

Result: **37 passed, 1 skipped** (same tree as this handoff; the 18:50 UTC edit changes only this document).

The skipped test is `test_live_pull_yields_at_least_one_record`. It runs only when `SAM_API_KEY` is set. The key was not set.

Covered acceptance checks:

- Unchanged fixture rerun inserts zero duplicate opportunities and zero duplicate snapshots.
- A changed payload creates one new snapshot.
- A deadline change creates `deadline_changed`, and that event points at the new snapshot.
- Raw source JSON is readable from the opportunity and the snapshot after commit.

### VERIFY outcomes

Source checked live on 2026-09-26: https://open.gsa.gov/api/get-opportunities-public-api/

Every `⚠️ VERIFY` item in `PHASE_01_SAM_INGESTION.md`:

- Current endpoint/version: still `GET https://api.sam.gov/opportunities/v2/search`. Alpha is `https://api-alpha.sam.gov/opportunities/v2/search`. The historical endpoint in the phase spec matches production.
- Authentication method: required `api_key` query parameter. Documented errors are "No api_key was supplied" and "An invalid api_key was supplied".
- Pagination behavior: `limit` is records per page, maximum 1000, API default 1. `offset` is documented as the page index starting at 0. The envelope is `totalRecords`, `limit`, `offset`, `opportunitiesData`. This client requests pages 0, 1, 2. A page that repeats at least half of the previous page's notice ids fails the run instead of truncating.
- Parameter names: required `api_key`, `postedFrom`, `postedTo` (`MM/dd/yyyy`, at most one year apart). Also documented: `limit`, `offset`, `ptype`, `solnum`, `noticeid`, `title`, `state`, `zip`, `organizationCode`, `organizationName`, `typeOfSetAside`, `typeOfSetAsideDescription`, `ncode`, `ccode`, `rdlfrom`, `rdlto`. `deptname` and `subtier` are deprecated. `status` is marked coming soon. The ingest sends `api_key`, `postedFrom`, `postedTo`, `limit`, and `offset`.
- Rate limits: the opportunities page says the daily cap depends on federal, non-federal, or general role and does not list HTTP 429. The SAM.gov System Account User Guide gives default daily caps of 10, 1,000, or 10,000 by account type. This client retries 429 and 5xx through `govcon.http.request_with_retry`, then fails the run.
- Attachment fields: `resourceLinks` is the attachment URL list. `description` is a separate download URL, not the body. `links` and `uiLink` are record links. Bytes are not downloaded in Phase 1.
- Notice/amendment identifiers: `noticeId` (query `noticeid`) is the notice id and is stored as `source_id`. `type` is the current type and `baseType` is the original type. The public API returns only the latest version and has no separate amendment id. `source_version` stores `postedDate` when present.

Some third-party guides treat `offset` as a record offset (`offset = page * limit`). This implementation follows the official page-index wording. That behavior is not yet confirmed with an authenticated second page.

### Known problems

- No `SAM_API_KEY` was available. Requests to the production and alpha search URLs with a demo or invalid key returned an empty HTTP 404 from `istio-envoy`, which does not match the documented invalid-key body. A live SAM record was not fetched.
- The committed fixture is the response example published on the official Get Opportunities page (`noticeId` `5b345bbb7127b91a3ad577b203fc6f68`), not a response captured with a project key.
- Page-index behavior is documented, not confirmed with a successful authenticated second page.

### Unfinished work

- Run `pytest tests/test_sam_ingestion.py::test_live_pull_yields_at_least_one_record` after `SAM_API_KEY` is set, and replace or add a fixture from that live response if the published example should not remain the only real payload.
- Confirm with that key whether `offset` advances by page index. If a live second page shows record-offset behavior, adjust pagination and record the deviation.
- Description-body and attachment downloads belong to a later phase (Phase 7), not this ingest.

### Recommended next task

Phase 2 — watchlist matching engine (`PHASE_02_MATCHING.md`), only after pull request #3 is merged and its gates pass. Phase 2 is not started. `IMPLEMENTATION_STATUS.md` still marks Phase 2 as NOT STARTED.
