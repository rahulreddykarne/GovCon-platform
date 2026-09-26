# AI handoff

Append a new entry after each implementation session. Do not rewrite earlier entries. Do not start the next phase in the same session that finishes the current one.

`main` was read at `e7ba0b1` (Phase 0 complete) before this entry. This branch is `cursor/phase-01-sam-ingestion-43a0`.

## 2026-09-26 18:49 UTC — Phase 1 SAM ingestion

- Agent: Cursor cloud
- Model: grok-4.7-high-fast
- Run: https://cursor.com/agents/bc-9e68ed03-0d1b-5e9f-899c-3926d53343a0
- Phase: Phase 1 SAM ingestion
- Branch: `cursor/phase-01-sam-ingestion-43a0`
- Pull request: https://github.com/rahulreddykarne/GovCon-platform/pull/3
- Implementation commits: `bba0ca2`, `f9ad00d`

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

### Functionality

- `govcon ingest sam` calls the SAM.gov Get Opportunities search. The default posted window is the last 3 UTC days, inclusive of today. The command pages until `totalRecords`, using `offset` as a page index.
- `govcon ingest sam-backfill` splits a longer posted-date range into windows of at most one year.
- `govcon ingest sam-archive-sweep` marks stored SAM rows `archived` when `archive_date` is before today UTC. It does not open a network client and does not write a snapshot.
- Each opportunity is upserted on `(source='sam', source_id=noticeId)`. The raw search object is stored. A snapshot and field-diff events are written only when the canonical content hash changes. Events are inserted before the current row is updated.
- Tracked diffs include deadline, set-aside, status, title, description hash, links/attachments, quantity, and estimated value min/max. A deadline change emits `deadline_changed`.
- Buyer contacts are upserted when both email and agency path are present.
- NSN, quantity, and estimated value are parsed only from explicit labels or explicit value fields. `award.amount` is not copied into estimated value.
- Description and attachment URLs are kept on the raw payload and `links`. Their bytes are not downloaded.

### Architectural decisions

- ADR-015: production search URL, auth, pagination, notice id, attachment fields, and retry behavior.
- ADR-016: one SHA-256 for `raw_hash` and `opportunity_snapshots.content_hash`; archive sweep is local and does not snapshot.
- ADR-017: contact unique key, NSN shape, and the rule that quantity and estimated value stay null unless the source states them.
- HTTP retries stay in `govcon.http.request_with_retry`. SAM passes a longer wait and more attempts. The default wait used by other callers is unchanged.
- No new table and no Alembic revision.

### Migrations

None. Phase 1 uses the Phase 0 schema (`97cb081e9a8e`).

### Tests

Command: `pytest`

Result: **37 passed, 1 skipped**.

The skipped test is `test_live_pull_yields_at_least_one_record`. It runs only when `SAM_API_KEY` is set. The key was not set.

Covered acceptance checks:

- Unchanged fixture rerun inserts zero duplicate opportunities and zero duplicate snapshots.
- A changed payload creates one new snapshot.
- A deadline change creates `deadline_changed`, and that event points at the new snapshot.
- Raw source JSON is readable from the opportunity and the snapshot after commit.

### SAM VERIFY findings (2026-09-26)

Source: https://open.gsa.gov/api/get-opportunities-public-api/

- Endpoint/version: production `https://api.sam.gov/opportunities/v2/search`. Alpha is `https://api-alpha.sam.gov/opportunities/v2/search`.
- Auth: required `api_key` query parameter. Documented errors are "No api_key was supplied" and "An invalid api_key was supplied".
- Pagination: `limit` max 1000, API default 1. `offset` is documented as the page index starting at 0. Response fields are `totalRecords`, `limit`, `offset`, `opportunitiesData`.
- Dates: `postedFrom` and `postedTo` are mandatory `MM/dd/yyyy` and must not be more than one year apart.
- Rate limits: the opportunities page says the daily cap depends on federal, non-federal, or general role and does not list HTTP 429. The SAM.gov System Account User Guide gives default daily caps of 10, 1,000, or 10,000 by account type. This client retries 429 and 5xx, then fails the run.
- Attachments: `resourceLinks` is the attachment URL list. `description` is a separate download URL, not the body.
- Notice/amendment ids: `noticeId` (query `noticeid`) identifies the notice. `type` is the current type and `baseType` is the original type. The public API returns only the latest version and has no separate amendment id. `source_version` stores `postedDate` when present.
- Some third-party guides treat `offset` as a record offset (`offset = page * limit`). This implementation follows the official page-index wording. If a later page repeats at least half of the previous page's notice ids, the run fails instead of saving a short page.

### Known problems

- No `SAM_API_KEY` was available. Requests to the production and alpha search URLs with a demo or invalid key returned an empty HTTP 404 from `istio-envoy`, which does not match the documented invalid-key body. A live SAM record was not fetched.
- The committed fixture is the response example published on the official Get Opportunities page (`noticeId` `5b345bbb7127b91a3ad577b203fc6f68`), not a response captured with a project key.
- Page-index behavior is documented, not confirmed with a successful authenticated second page.

### Unfinished work

- Run `pytest tests/test_sam_ingestion.py::test_live_pull_yields_at_least_one_record` after `SAM_API_KEY` is set, and replace or add a fixture from that live response if the published example should not remain the only real payload.
- Confirm with that key whether `offset` advances by page index. If a live second page shows record-offset behavior, adjust pagination and record the deviation.
- Description-body and attachment downloads belong to a later phase (Phase 7), not this ingest.

### Recommended next task

Phase 2 — watchlist matching engine (`PHASE_02_MATCHING.md`), after this pull request is merged and its gates pass. Do not start Phase 2 in the Phase 1 pull request.
