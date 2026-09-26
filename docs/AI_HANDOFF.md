# AI handoff

Append a new entry after each implementation session. Do not rewrite earlier entries. Do not start the next phase in the same session that finishes the current one.

`main` was re-read at 2026-09-26 19:15 UTC and is `26ed863` (Phase 0 and Phase 1 merged). This branch is `cursor/phase-02-matching-d53f`.

## 2026-09-26 19:15 UTC — PHASE_02_MATCHING

- Agent/model identity: Cursor cloud, model `composer-2.5`
- Datetime (UTC): 2026-09-26 19:15 UTC
- Phase/task: PHASE_02_MATCHING
- Run: https://cursor.com/agents/bc-a779864f-ed95-592a-a2ab-61aabff2d53f
- Branch: `cursor/phase-02-matching-d53f`
- Pull request: (opened at end of this session)

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

Phase 3 — alert digests (`PHASE_03_ALERTS.md`), only after this pull request is merged and its gates pass.

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
