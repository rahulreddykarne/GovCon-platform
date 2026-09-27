# SPEC_DEVIATIONS

Record only verified deviations from `MASTER_SPEC_v2.5.md`.

## Template

### DEV-001 — Short title
- Phase:
- Date:
- Spec requirement:
- Verified external/repository reality:
- Decision:
- Reason:
- Impact:
- Follow-up:

## Phase 0

No confirmed behavior deviation was required. Choices that add columns or tables required by the specification prose (timestamps, sessions, audit, notifications, `decision_runs`, optimistic `version`) are recorded in `DECISIONS.md`.

## Phase 1

No confirmed behavior deviation from `MASTER_SPEC_v2.5.md` was required. The live Get Opportunities documentation still publishes `https://api.sam.gov/opportunities/v2/search`. Parsing and history choices are recorded in ADR-015, ADR-016, and ADR-017.

On 2026-09-26 this environment had no `SAM_API_KEY`. Unauthenticated and invalid-key requests to the production and alpha search URLs returned an empty HTTP 404 from `istio-envoy`, which does not match the documented error text for a missing or invalid key. The committed fixture is the response example published on the official Get Opportunities page, not a call made with a project API key. The live-pull acceptance check runs only when `SAM_API_KEY` is set.

## Phase 2

No confirmed behavior deviation from `MASTER_SPEC_v2.5.md` was required. Matching semantics and keyword boundary handling are recorded in ADR-018.

## Phase 4

### DEV-002 — DIBBS v1 retains the index file, not the PDF zip or quote template
- Phase: 4
- Date: 2026-09-26
- Spec requirement: Download source batch files and retain the originals. Parse solicitation number, NSN, nomenclature, quantity, unit, return-by date, set-aside, buyer details, and source URL. Do not scrape individual HTML pages unless required and documented.
- Verified external/repository reality: On 2026-09-26 the recent RFQ page listed three files per post date. `inYYMMDD.txt` is the 140-character index and contains every field above except a buyer name or email (it has a 5-character buyer code and AMSC). `caYYMMDD.zip` is the bundle of that day's solicitation PDF/HTML files. `bqYYMMDD.zip` is the prefilled quote-upload template. `RfqRec.aspx?sn=` is a per-solicitation HTML page. Neither `https://www.dibbs.bsm.dla.mil/robots.txt` nor `https://dibbs2.bsm.dla.mil/robots.txt` is published. The newest index listed that day was `in260925.txt` (523 records). Help text says the current day's file is posted the next day.
- Decision: Download and retain `inYYMMDD.txt` only. Store the record-page URL without requesting it. Do not download `caYYMMDD.zip` or `bqYYMMDD.zip`.
- Reason: The index is the structured batch export. The zip files are per-solicitation documents and a quote template. Fetching them would pull individual solicitation files the phase says not to scrape.
- Impact: Buyer details are the buyer code and AMSC. Message digests can link to the RFQ record URL. PDF bytes are absent until a later attachment phase.
- Follow-up: None in Phase 4. Attachment download stays in Phase 7.

## Phase 3

No confirmed behavior deviation from `MASTER_SPEC_v2.5.md` was required. Digest delivery, the deadline re-alert watermark, and calendar-day display are recorded in ADR-019. `PHASE_03_ALERTS.md` has no `⚠️ VERIFY` item.

## Phase 5

No confirmed behavior deviation from `MASTER_SPEC_v2.5.md` was required. The live search contract, the null unit-price rule, incremental windows, and the 18-month recompete view are recorded in ADR-021.

## Phase 6

No confirmed behavior deviation from `MASTER_SPEC_v2.5.md` was required. SAM entity v3 lookup, the vendor cache window, agency-segment matching, and office matching are recorded in ADR-022.

On 2026-09-26 this environment had no live `SAM_API_KEY`. The committed fixture is derived from the official Entity Management API documentation example shape. A live entity pull runs only when `SAM_API_KEY` is set.

## Phase 7

No confirmed behavior deviation from `MASTER_SPEC_v2.5.md` was required. The DeepSeek API contract, prompt registry design, attachment extraction, and analysis persistence are recorded in ADR-023 through ADR-026.

On 2026-09-26 this environment had no `DEEPSEEK_API_KEY`. The AI provider returns a graceful warning when no key is configured. The fixture PDF test uses a mock provider with a known-good solicitation analysis response. A live AI analysis runs only when `DEEPSEEK_API_KEY` is set.

The front-matter parser uses a simplified `key: value` format. YAML list syntax for `includes` is not supported; comma-separated strings are used instead. This is a parser limitation, not a schema deviation.

## Phase 8

No confirmed product-behavior deviation from `MASTER_SPEC_v2.5.md` was required.

On 2026-09-26 this environment had no `JEV_API_KEY` or `JEV_BASE_URL`. Live contract probes were run against public JEV endpoints (`POST /v1/systemone`) and confirmed auth-required responses plus non-support for `/v1/chat/completions`. Phase 8 tests therefore use deterministic rule fallback plus fixture/mocked states for calibration boundaries; full authenticated JEV calls remain environment-gated.

## Phase 9

No product-behavior deviation from `MASTER_SPEC_v2.5.md` was required. `PHASE_09_COMPLIANCE.md` contains no `⚠️ VERIFY` items; AI calls reuse the DeepSeek contract verified in Phase 7 (ADR-023) and JEV routing reuses the Phase 8 contract (ADR-027). Clause-library titles were verified on acquisition.gov on 2026-09-26 (ADR-037). Scope notes, recorded so they are not mistaken for silent deferrals:

### DEV-003 — Compliance UI (§15.21) delivered as data + CLI, not web pages
- Phase: 9
- Date: 2026-09-26
- Spec requirement: §15.21 review workspace columns and filters; reviewer can open the exact source page/section.
- Verified external/repository reality: The web UI is Phase 14; this run is instructed not to start UI work.
- Decision: `compliance.matrix.compliance_matrix()` returns every §15.21 column (requirement, mandatory, severity, source file/page/section/quote/local path/URL, evidence, validator output, status, confidence, independent confirmation, amendment freshness, blocking) and all eight filters. `govcon compliance matrix [--filter …] [--json]` exposes it.
- Impact: Phase 14 renders this data; no compliance logic lives in the UI layer.
- Follow-up: Phase 14.

### DEV-004 — Review reopen after a material amendment is flagged, not performed
- Phase: 9
- Date: 2026-09-26
- Spec requirement: §15.10 "Reopen human review if policy requires".
- Verified external/repository reality: Review sessions, assignments, and quorum policy belong to Phase 10.
- Decision: Amendment revalidation records `impact.review_reopen_required` and a blocking `review_reopen_required` finding when a completed/approved review session exists.
- Impact: The pursuit cannot reach `ready_to_submit` until that finding is handled. Phase 10 performs the reopen.
- Follow-up: Phase 10 consumes the flag.

### DEV-005 — Sourcing/pricing re-runs after an amendment are flagged
- Phase: 9
- Date: 2026-09-26
- Spec requirement: §15.10 "Re-run sourcing/pricing if affected".
- Verified external/repository reality: No automated sourcing or pricing engine exists yet (`intelligence/sourcing.py` is a stub; pricing is manual on `pursuits`).
- Decision: `rerun_sourcing` / `rerun_pricing` impact flags and open findings are raised when delivery/technical/origin/pricing requirements change.
- Impact: Visible, non-blocking findings; compliance validation and JEV re-run automatically.
- Follow-up: The phase that implements sourcing/pricing automation subscribes to these flags.

### DEV-006 — Benchmark replay does not evaluate new model behavior
- Phase: 9
- Date: 2026-09-26
- Spec requirement: §15.20 / §43.8 run the benchmark when prompts/models/parsers change and block on regression.
- Verified external/repository reality: CI has no model keys.
- Decision: CI replays recorded pass outputs through the production stages; this gates parser, scanner, reconciler, precedence, amendment, validator, and status-gate regressions. `govcon compliance benchmark --live` re-runs passes A/B on the configured provider; the activation gate runs the replay suite plus render/variable/schema/injection/secret checks.
- Impact: A changed extraction prompt must be evaluated with `--live` (keys required) before production activation.
- Follow-up: Add recorded live outputs as new cases once `DEEPSEEK_API_KEY` is available.

On 2026-09-26 this environment had no `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, or `JEV_API_KEY`. AI steps were tested with a mocked provider; JEV routing used the Phase 8 rule fallback.

## Phase 10

No confirmed product-behavior deviation from `MASTER_SPEC_v2.5.md` was required.

Follow-up closure from Phase 9:
- DEV-004 (`review_reopen_required` after a material amendment) is now implemented in Phase 10 via collaborative review reopen orchestration (`apply_material_amendment_reopen`) and is covered by DB-backed tests.

## Phase 11

No confirmed product-behavior deviation from `MASTER_SPEC_v2.5.md` was required.

## Phase 12

### DEV-007 — `similar_opportunities` and `learning_summary` expose stored data only until later phases
- Phase: 12
- Date: 2026-09-26
- Spec requirement: §18 lists `similar_opportunities(...)` and `learning_summary(...)` as MCP tools; §19/§21 assign semantic search and outcome analytics to Phases 13 and 15.
- Verified external/repository reality: `matching/semantic.py`, `enrich/embeddings.py`, and `learning/analytics.py` are Phase 0 stubs; `outcome_feedback` rows exist from the foundation schema.
- Decision: Register both MCP tools now. `similar_opportunities` returns heuristic matches on NSN/PSC/agency_path with an explicit note that vector search is Phase 13. `learning_summary` aggregates `outcome_feedback` rows with an explicit note that analytics are Phase 15.
- Impact: MCP clients can call stable tool names without inventing embeddings or analytics logic early.
- Follow-up: Phase 13 replaces the heuristic backend for `similar_opportunities`; Phase 15 replaces `learning_summary` aggregation.

Implementation notes:
- The `proposal_drafting_v1` and `proposal_red_team_v1` prompts were placeholders in earlier phases. They are now activated with full production text from §41.1 and §41.2.
- Proposal red-team severity uses `critical | major | minor` (as defined in §41.2), not the compliance system's `critical | high | medium | low`. Only critical proposal findings create blocking compliance findings (ADR-041).
- `finalize_proposal(APPROVE_FOR_SUBMISSION)` passes the human approval as the override reason for Phase 9's pre-flight check, consistent with the human-final-authority principle (ADR-040).

## Phase 13

### DEV-007 RESOLVED — `similar_opportunities` now uses pgvector vector search
- Phase: 12→13
- Resolved: 2026-09-27
- Original deviation: Phase 12 used NSN/PSC/agency heuristic for `similar_opportunities` with note that Phase 13 would add vector search.
- Resolution: `op_similar_opportunities` now delegates to `matching.semantic.similar_opportunities`, which uses pgvector cosine distance (`<=>` operator) when the target opportunity has an embedding. The HNSW index on `opportunities.embedding` serves the query. Falls back to the Phase 12 heuristic only when the target opportunity has no embedding and no provider is supplied.

### DEV-008 — HNSW index created without CONCURRENTLY in Alembic migration
- Phase: 13
- Date: 2026-09-27
- Spec requirement: §19 task 2 says "Add vector index." The spec does not prescribe CONCURRENTLY.
- Verified reality: `CREATE INDEX CONCURRENTLY` cannot run inside a PostgreSQL transaction block; Alembic's default mode wraps migrations in a transaction.
- Decision: Migration `f2a3b4c5d6e7` creates the HNSW index without CONCURRENTLY. For zero-downtime production upgrades, the index should be created with CONCURRENTLY before running Alembic. A comment in the migration records this.
- Impact: Non-blocking for the dev/test environment. Production teams should create the index manually first.
- Follow-up: None. Production deployment guidance is documented in the migration comment.
## Phase 17

### DEV-011 — analytics_refresh is a no-op pending Phase 15
- Phase: 17
- Date: 2026-09-27
- Spec requirement: Sunday sweep includes "analytics refresh".
- Verified external/repository reality: Phase 15 outcome analytics engine is NOT STARTED. `learning/analytics.py` is a stub.
- Decision: `step_analytics_refresh` logs "Phase 15 analytics engine not yet implemented; step is a no-op" and returns `succeeded`. The step is in the chain and visible in `/ops`, but does no DB work.
- Reason: Phase 17 must not start Phase 15 work.
- Impact: Sunday sweep completes without analytics refresh until Phase 15.
- Follow-up: Phase 15 replaces this no-op with real analytics computation.

### DEV-012 — SAM ingest in scheduler chains skips when SAM_API_KEY is unset
- Phase: 17
- Date: 2026-09-27
- Spec requirement: 06:30 chain: SAM ingest → DIBBS ingest → match → alerts.
- Verified external/repository reality: Live SAM.gov API requires `SAM_API_KEY`. Without it, an unauthenticated request returns an empty 404. The Phase 1 ingest command exits 1 when the key is missing.
- Decision: The scheduler `step_sam_ingest` returns `status="skipped"` (not "failed") when `SAM_API_KEY` is not set. The chain continues to DIBBS ingest. A skipped step is visible in the job run record and the CLI output.
- Reason: A missing API key is a configuration issue, not an ingest failure. DIBBS, match, and alerts can still run usefully. The chain should not abort entirely because of a missing key.
- Impact: Without `SAM_API_KEY`, only DIBBS, match, and alerts run in the morning/evening chains.
- Follow-up: Set `SAM_API_KEY` in production to enable SAM ingest.

## Phase 14

### DEV-009 — Notifications full page deferred
- Phase: 14
- Date: 2026-09-27
- Spec requirement: §24A lists in-app notifications for all review/approval/submission events.
- Verified external/repository reality: `notifications` table and `notify()` function exist from Phase 10. The bell link shows unread count in the top bar.
- Decision: Full `/notifications` list page not implemented in this phase. Unread count badge is visible. Phase 17/18 can add the full page.
- Impact: Users see the count but must navigate to specific pages. No notification functionality is blocked.
- Follow-up: Phase 17 or standalone follow-up.

### DEV-010 — Learning by-agency/by-PSC analytics placeholder
- Phase: 14
- Date: 2026-09-27
- Spec requirement: §20 learning page shows analytics by agency, PSC, repeat competitors.
- Verified external/repository reality: Outcome data exists in `pursuits.stage` + `pursuits.outcome_notes`; grouped analytics (by agency/PSC/competitor) require joins with opportunities.
- Decision: Summary stats (submitted/won/lost/win-rate/avg-margin) are computed. Grouped analytics rows are empty list pending Phase 15 analytics engine.
- Impact: Top-level learning stats work; drill-down tables show "no data" until Phase 15.
- Follow-up: Phase 15.

## Phase 15

### DEV-010 RESOLVED — Learning by-agency/by-PSC analytics now implemented
- Phase: 14→15
- Resolved: 2026-09-27
- Original deviation: Phase 14 `learning` page returned empty `by_agency` and `by_psc` lists pending Phase 15 analytics.
- Resolution: `learning/analytics.py` implements `win_rate_by_psc`, `win_rate_by_agency`, `win_rate_by_size`, `no_bid_reason_counts`, `loss_reason_counts`, `common_competitors`, `reliable_suppliers`, and `avg_cycle_times`. The `learning` web route now calls `outcome_analytics(session)` and renders the full tables. The MCP `op_learning_summary` returns real analytics.
- Impact: Learning page now shows all analytics on every page load. No manual SQL required.

### DEV-011 — `denorm_agency` stores full agency_path (not abbreviated name)
- Phase: 15
- Date: 2026-09-27
- Spec requirement: §21 says capture "agency" on won outcomes.
- Verified reality: `Opportunity.agency_path` stores a full path string (e.g., "DEPT OF DEFENSE > DLA > TROOP SUPPORT"). There is no separate normalized `agency` column.
- Decision: `denorm_agency` stores `agency_path` verbatim. Win-rate-by-agency groups on the full path. This is accurate but may produce more rows than expected in analytics since sub-agencies differ from parents.
- Impact: Analytics are precise, not aggregated by department. Future enhancement could parse the top-level agency from `agency_path`.
- Follow-up: Phase 17 or analytics enhancement.

### DEV-012 — outcome_analysis AI classification not called automatically on record
- Phase: 15
- Date: 2026-09-27
- Spec requirement: §39.8 says `outcome_analysis_v1` is used for evidence-constrained classification.
- Verified reality: No AI key is configured in the test/dev environment. The AI gateway would fail or be mocked.
- Decision: `outcome_analysis_v1.md` is activated and registered. The `outcome_analysis_id` FK in `outcome_feedback` provides the hook for AI classification. Automatic invocation at record time is deferred until an AI key is available. The prompt is production-ready and correctly structures the evidence constraints per §39.8.
- Impact: Analytics do not depend on AI classification (descriptive stats use recorded fields). AI classification is additive and can be run as a batch job when a key is available.
- Follow-up: Wire AI classification call in `record_outcome` when AI key is set.
