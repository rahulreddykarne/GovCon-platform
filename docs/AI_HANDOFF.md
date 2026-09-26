# AI handoff

Append a new entry after each implementation session. Do not rewrite earlier entries. Do not start the next phase in the same session that finishes the current one.

`main` was re-read at 2026-09-26 18:50 UTC and is still `e7ba0b1` (Phase 0 merged, pull request #1). No other agent commits were on this branch. This branch is `cursor/phase-01-sam-ingestion-43a0`. One pull request: #3.

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

## 2026-09-26 19:05 UTC — PHASE_02_MATCHING

- Agent/model identity: Cursor cloud, model `composer-2.5`
- Datetime (UTC): 2026-09-26 19:05 UTC
- Phase/task: PHASE_02_MATCHING
- Branch: `cursor/phase-02-matching-67ba`

### Files changed

- `src/govcon/matching/rules.py` (new)
- `src/govcon/matching/engine.py`
- `src/govcon/matching/dedup.py`
- `src/govcon/matching/watchlists.py` (new)
- `src/govcon/matching/__init__.py`
- `src/govcon/cli.py`
- `tests/test_matching.py` (new)
- `IMPLEMENTATION_STATUS.md`
- `DECISIONS.md`
- `SPEC_DEVIATIONS.md`
- `docs/AI_HANDOFF.md` (this file)

### What shipped

- Deterministic watchlist matching: every non-empty rule group must pass; values inside a group are OR'd. Empty groups are wildcards.
- Rule groups: PSC prefix, NAICS prefix, keywords, exclude keywords (veto), exact NSN list, set-asides, source filters, min/max estimated value when known, minimum days until deadline.
- Unknown opportunity values are not fabricated. When `max_value` or `min_value` is configured but the opportunity has no estimated value, the filter records `unknown` in `matched_on` and does not reject.
- Score is rule-hit based (count of passed non-empty groups). Explainable evidence is stored in `matched_on`.
- Idempotent match upsert on `(opportunity_id, watchlist_id)`; existing `status` is preserved on update.
- CLI: `govcon match run`, `govcon match rebuild --watchlist N`, `govcon watchlist add`, `govcon watchlist list`, `govcon watchlist edit`, `govcon watchlist disable`.

### ADRs / DECISIONS touched

- ADR-018 in `DECISIONS.md`: deterministic matching semantics, unknown-value handling, exclude veto, score, and upsert behavior.
- ADR-019 in `DECISIONS.md`: match run scope (non-archived opportunities, rebuild removes stale rows).
- Phase 0 ADR-001 through ADR-017 and Phase 1 ADR-015 through ADR-017 were not changed.
- `SPEC_DEVIATIONS.md` Phase 2: no behavior deviation. No `⚠️ VERIFY` items in the phase spec.

### Migrations

None. Phase 2 uses the Phase 0 schema (`97cb081e9a8e`).

### Tests

Command: `pytest`

Result: **47 passed, 1 skipped** (full suite).

Covered acceptance checks:

- PSC prefix matching (`test_psc_prefix_match`)
- NAICS prefix matching (`test_naics_prefix_match`)
- Exclude keyword veto (`test_exclude_keyword_veto`)
- Wildcard empty group (`test_wildcard_empty_group`)
- Unknown estimated value does not reject (`test_unknown_estimated_value_does_not_reject`)
- Idempotent match upsert (`test_idempotent_match_upsert`, `test_match_run_is_idempotent`)
- Integration: match run, rebuild, and CLI commands (`test_match_run_and_rebuild`, `test_cli_watchlist_and_match_commands`)

### VERIFY outcomes

`PHASE_02_MATCHING.md` contains no `⚠️ VERIFY` items. No external interface verification was required.

### Known problems

None identified in this run.

### Unfinished work

- Phase 3 alert digests (`PHASE_03_ALERTS.md`) — not started in this run.
- Semantic/vector matching remains Phase 13.
- AI bid decisions and proposal workflow remain later phases.

### Recommended next task

Phase 3 — alert digests (`PHASE_03_ALERTS.md`). Requires Phase 2 merged and gates passing. Collect unalerted `new` matches, group by watchlist, render HTML digest, SMTP or outbox delivery.
