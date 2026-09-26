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
