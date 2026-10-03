# GovCon feature verification — October 2, 2026

## 1. Verdict

**The service layer passes its existing checks, but the complete web workflow is not ready for a production pilot.**
The current working tree produced **1,030 passing tests, one skipped live test, and no test failures**.
Additional executed probes found **eight confirmed defects: five P1 and three P2**. No P0 was established in this feature audit.
The failures affect starting and retaining pursuits, review initiation, generation recovery, bid analysis over MCP, and information presented to submission users.
Live integrations remain unverified; passing mocked provider tests does not establish live service compatibility or model quality.

### Scope and system map

This is a **feature audit**, using Phase 0 reconnaissance and Phase 3 verification. It is not a replacement for a repository-wide security or performance audit. The target is the current working tree at Git HEAD `a39c637`, including the extensive changes and untracked implementation files that were already present. The older `PRODUCTION_READINESS_AUDIT.md` is historical evidence and was not treated as a current test result or overwritten.

The stack is Python 3.12, Typer CLI, FastAPI/Jinja2/HTMX web UI, FastMCP, SQLAlchemy/Alembic, PostgreSQL 16 with pgvector, and APScheduler. Entry points include **84 CLI commands, 32 method-specific web routes, and 22 MCP tools**. Wheels carry templates, static files, prompts, migrations, and benchmark fixtures; deployment requires database migration and prompt registry initialization before serving. No typecheck or lint gate is declared in the manifest or CI.

PostgreSQL holds opportunities and immutable snapshots, matches/watchlists, public vendor/award/contact intelligence, classified documents, AI analyses and usage reservations, decision runs, review assignments/comments, proposals/versions, submission manifests, outcomes, notifications, audit events, and job runs. Local directories hold downloaded/extracted files, exports, logs, and offline alert HTML. External boundaries are SAM.gov opportunity/entity APIs, DIBBS downloads, USAspending, model providers/JEV, and optional SMTP. Web mutations require a session, CSRF token, and appropriate role. MCP writes use the configured actor; human approvals and submission confirmations are excluded from its tool set. CLI administration assumes trusted local access.

Critical journeys: **discover and pursue; analyze and qualify; collaborate and approve; prepare and record submission; capture outcome and learn**. Git history hotspots were examined first: `cli.py` appeared in 17 of the last 50 commits; configuration in eight; models in seven; web routes in five. Documentation/status files have more churn. The README's closing “Not in this phase” section contradicts the implemented later phases, so feature inventory also uses actual routes, menus, commands, tests, and phase specifications.

## 2. Baseline and verification evidence

| Check | Actual result | Interpretation |
|---|---|---|
| Full suite, `.venv/Scripts/python.exe -m pytest -q --junitxml=…` | Exit 0; JUnit: 1,031 cases, 0 failures, 0 errors, 1 skipped; 105.641 seconds | **1,030 passed, 1 skipped** |
| Skipped case | `test_live_pull_yields_at_least_one_record`: `SAM_API_KEY is not set` | Live SAM ingestion unverified |
| Warnings | 21 in intelligence tests and 66 in web tests | Starlette deprecation of per-request `cookies=` in the test client |
| Wheel build/install/bootstrap | `test_clean_installed_wheel_startup_and_database_upgrade` passed in the full suite | Wheel starts outside checkout, serves login/CSS, migrates, syncs prompts, and runs the release lifecycle |
| Release smoke equivalent | All three `test_release_lifecycle.py` cases passed, plus installed wheel case | Real DOCX/XLSX, failure recovery, and concurrent approval; `smoke.sh` invokes these modules |
| Database migrations | All migrations through `7f4d81a9c630` applied successfully to fresh databases | No configured production database was migrated |
| Dependency compatibility | `No broken requirements found.` | Installed package requirements consistent |
| Typecheck | `No module named mypy` | Not run: no project configuration or environment installation |
| Lint | `No module named ruff` | Not run: no project configuration or environment installation |
| CLI reachability | 84 command-specific `--help` calls, all exit 0 | Parser/wiring verified; help alone does not prove business behavior |
| Additional web probes | Six executed counterexamples, all reproduced | See F1–F6 and JSON evidence |
| Empty compliance instruction | `No such option: --opp-id (Possible options: --no-ai, --opportunity-id)` | F7; parser rejects before database work |
| MCP protocol exercise | All 22 tools called; 21 returned `ok: true`; one failed | F8; in-process FastMCP client, synthetic data, cached vendor, mocked comment AI |
| Real local semantic search | Four opportunities embedded, zero skipped; method `vector` | Cached MiniLM model, network disabled; related candidate distance 0.4523 versus unrelated bridge work 0.9453 |

Raw evidence: [JUnit](audit/feature_verification_2026-10-02/baseline.xml), [web probes](audit/feature_verification_2026-10-02/web_probes.json), [MCP and semantic probes](audit/feature_verification_2026-10-02/mcp_semantic_probes.json), and [entry-point/test inventory](audit/feature_verification_2026-10-02/inventory.json).

Tests and probes created and dropped disposable databases with fixed prefixes and random UUIDs. Government/model keys and SMTP were cleared in the child processes. Model and government responses in the suite were fixtures/mocks; supplemental semantic inference used cached local weights with Hugging Face/Transformers offline mode. No real bid submission, payment, email, SMS, or production-record mutation was performed. No application source or existing test was changed.

## 3. Findings table

| # | Severity | Category | Location | Finding | Confidence |
|---|---|---|---|---|---|
| F1 | P1 | Workflow | `src/govcon/web/routes/__init__.py:346`, `:1161` | Pursue removes a fresh match from both Inbox and Pipeline without creating a pursuit | CONFIRMED |
| F2 | P1 | Workflow | `src/govcon/web/routes/__init__.py:553`; `src/govcon/web/templates/workspace/review.html:2` | Fresh workspace has no reachable review-start or assignment control | CONFIRMED |
| F3 | P1 | Recovery | `src/govcon/web/routes/__init__.py:965` | Generation failure is swallowed; approval succeeds with no draft or web retry | CONFIRMED |
| F4 | P2 | Display/contracts | `src/govcon/web/routes/__init__.py:609`; `src/govcon/web/templates/workspace/compliance.html:38` | Latest unrelated compliance run resets displayed counts and hides preflight result | CONFIRMED |
| F5 | P2 | Submission UX | `src/govcon/web/templates/workspace/submission.html:14`, `:25` | Submission page omits actual files and destination and references a nonexistent instructions field | CONFIRMED |
| F6 | P1 | Data correctness | `src/govcon/web/routes/__init__.py:1025`; `src/govcon/web/templates/workspace/submission.html:42` | Browser-local submission time is silently interpreted as UTC | CONFIRMED |
| F7 | P2 | Recovery instructions | `src/govcon/web/templates/workspace/compliance.html:4` | Empty compliance state tells users to execute an invalid CLI option | CONFIRMED |
| F8 | P1 | MCP/contracts | `src/govcon/mcp/operations.py:370` | Bid analysis fails whenever a stored decision run is included | CONFIRMED |

## 4. Findings detail

### F1 — Pursue hides the opportunity instead of adding it to the pursuit workflow

**Trigger:** An authorized user clicks Pursue on a new active match whose opportunity has no pursuit and no other visible match. The inbox form posts `action=pursuing`. `inbox_action` changes only `Match.status`; Pipeline admits unmatched opportunities only with statuses `new`, `seen`, or `reviewing`.

**Evidence:** A request with the rendered CSRF token returned HTTP 200. The stored match became `pursuing`, no pursuit existed, and the unique opportunity title disappeared from both Inbox and Pipeline. The row remains in the database and can be found via Search; this is a workflow disappearance, not record deletion.

**Impact:** A core user action appears successful but removes the user's candidate from their normal work queues. The expected pursuit never starts.

**Fix sketch:** Atomically create or reuse the pursuit when pursuing, audit both changes, and direct the user to its workspace. Cover a new match, repeat submission, and simultaneous pursue actions.

### F2 — A fresh workspace cannot start collaborative review through the visible UI

**Trigger:** Start Workspace on a fresh opportunity with no review session. The handler creates a `Pursuit` only. The Review template puts assignment and review controls behind the branch that requires an existing session. Overview has no start-review form.

**Evidence:** HTTP 303 created a pursuit, but no `ReviewSession`. Both rendered Overview and Review pages contained only the Logout form. Review said “No review session yet. Start one from Overview.” The assignment endpoint can initialize a session, but its form is inaccessible in this state.

**Impact:** Users cannot initiate review, assign reviewers, or reach bid approval through the promised web workflow. Existing tests pre-create review sessions or assignments and miss this entry-state failure. CLI/MCP or a handcrafted request is a workaround.

**Fix sketch:** Add an authorized initial-review/assignment action outside the existing-session branch, or create the session at the appropriate workspace transition. Test from a fresh opportunity using only rendered controls.

### F3 — Auto-generation failure produces a successful decision message and an unrecoverable web state

**Trigger:** After a real completed review, proposal or submission-package generation raises, for example because of a temporary dependency or artifact/database error. `_trigger_proposal_generation` rolls back its savepoint, logs, and returns without exposing failure.

**Evidence:** Injecting a synthetic generation exception left review status `approved_to_bid`, created no proposal, and returned a redirect with `notice=Decision recorded: approve to bid.` The Proposal page said “Proposal generation in progress or not yet triggered.” It had no retry form. The generation attempt is synchronous; no background job is pending in this path.

**Impact:** The approved pursuit is stuck for web users, and the page implies progress that is not happening. Keeping the human approval is reasonable, but the failed follow-on work needs an explicit state and recovery path.

**Fix sketch:** Preserve approval, persist/report generation failure, and expose a permission-checked idempotent retry or retryable job. Test fail-then-recover without requiring the user to revoke and reapprove the bid.

### F4 — Compliance summary reads the wrong run and mismatched preflight fields

**Trigger/impact:** After matrix validation, a later submission-destination, proposal-coverage, or preflight run becomes the latest `ComplianceRun`. The workspace query does not filter `run_type`, so it renders that run's default zero counters. The template also expects `preflight_passed`/`blocking_items`, whereas preflight stores `ready`/`items`. The executed lifecycle had one active mandatory requirement and a ready preflight, but displayed every count as zero and no preflight indicator. **Fix sketch:** Load the latest matrix and latest preflight separately, or compute current counts from active requirements; render each run's real schema and staleness. Confidence: **CONFIRMED**.

### F5 — Submission checklist displays keys instead of actionable instructions

**Trigger/impact:** A generated package has `required_files={files: [proposal.docx, pricing.xlsx]}` and a known recipient. The template lists only keys from `required_actions`, ignores `required_files` and the actual destination fields, and conditionally renders `submission.instructions`, which `Submission` does not define (`models.py:807`). The executed page contained neither required filename nor the stored synthetic recipient; it showed field labels such as `required_filenames`. Users cannot use this page as the promised manual submission guide. **Fix sketch:** Pass `generate_final_checklist` and `generate_step_by_step_instructions` outputs to the page, rendering actual file/action values, destination, deadline, timezone, and unknowns. Confidence: **CONFIRMED**.

### F6 — Local submission times are stored with the wrong timezone

**Trigger:** A user outside UTC fills the optional `datetime-local` field. Browsers submit a wall-clock value without an offset. The route parses it and uses `replace(tzinfo=UTC)` rather than translating the user's timezone.

**Evidence:** A successful manual-submission request entered `2026-10-02T21:31` and stored `2026-10-02T21:31:00+00:00`. If the user's timezone is America/Chicago, that wall time on October 2 is `2026-10-03T02:31:00+00:00`; the record is five hours early. In standard time the error is six hours. The submission service compares this incorrect instant with the actual UTC deadline.

**Impact:** Submission history is silently wrong. Deadline validation can reject valid evidence or accept an incorrectly early recorded time, depending on the user's offset. Omitting the field uses server UTC and avoids this path.

**Fix sketch:** Submit an explicit ISO offset/UTC instant from the browser, or require a named timezone and convert with `zoneinfo`. Reject ambiguous timezone-free inputs; test Chicago daylight and standard time and both sides of a deadline.

### F7 — Compliance empty-state recovery command cannot execute

**Trigger/impact:** With no compliance run, copy the command shown in the page. `govcon compliance run --opp-id 1` is rejected by the actual CLI: `No such option: --opp-id (Possible options: --no-ai, --opportunity-id)`. The valid option declared at `cli.py:2048` is `--opportunity-id`. This blocks the offered recovery action. **Fix sketch:** Correct the displayed option and check displayed command examples against CLI parsing. Confidence: **CONFIRMED**.

### F8 — MCP bid analysis breaks when decision history exists

**Trigger:** Call `get_bid_analysis` for an opportunity with a bid decision/decision package and at least one stored `DecisionRun`. `list_decision_runs` returns ORM `DecisionRun` objects; the serializer reads `run.status`, which is not a field or property on that model (`models.py:578`). Normal bundle/package execution writes such rows; the executed release review also produced decision history.

**Evidence:** Through the real in-process FastMCP client, the tool returned `ok: false`, code `operation_failed`, message `AttributeError: 'DecisionRun' object has no attribute 'status'`. The existing MCP happy-path test stores a bid decision but no decision run, so its list comprehension is empty and never accesses the missing field.

**Impact:** MCP consumers cannot retrieve bid analysis for opportunities with actual decision history, despite the suite's green acceptance result.

**Fix sketch:** Serialize defined fields from `DecisionRun.result` or another explicitly defined contract; do not invent a model status. Add a regression invoking the registered tool after a real persisted decision bundle/package run.

## 5. Journey map

| Critical journey | Traced path and evidence | Break points |
|---|---|---|
| Discover and pursue | Fixture SAM/DIBBS → snapshots/events → watchlist matching → offline digest → Inbox → match action → pursuit/workspace/Pipeline | **F1:** Pursue loses queue visibility; **F2:** fresh workspace stops before review |
| Analyze and qualify | Download/local import → classification/extraction → prompt registry/gateway/budget → solicitation/market/sourcing/pricing analysis → decision bundles/package → compliance extraction, evidence, amendments | Service fixture/provider tests pass. **F4/F7:** web compliance reporting/recovery; **F8:** MCP analysis after bundle history |
| Collaborate and approve | Invite/login → assignment → comments/AI side opinion → reviewer completion/quorum → versioned human approval → proposal/package generation | Quorum, auth, concurrency and normal generation tested. **F2:** entry state; **F3:** generation exception after approval |
| Prepare and record submission | Immutable proposal edit/export → package assembly → integrity/coverage/preflight → human final approval → manual submission evidence | Real DOCX/XLSX lifecycle and recovery pass. Assembly/preflight exercised through services/CLI tests. **F5:** web instructions missing; **F6:** optional local timestamp |
| Capture outcome and learn | Recorded submission/no-bid → validated outcome and immutable history → current-outcome analytics → decision learning and semantic profiles | DB-backed outcome tests and MCP protocol no-bid recording pass; real local embedding/vector query runs. External outcome analysis model quality unverified |

## 6. Feature verification matrix

**WORKS** means the named behavior was executed in tests/probes or a complete caller/service trace. It does not mean every conceivable edge case or production dependency was checked. **PARTIAL** identifies a working path with a named failing edge; **BROKEN** identifies a reproduced offered workflow; **UNVERIFIED** means the actual dependency or environment was not exercised. For AI/government/SMTP features, offline behavior and live integration are listed separately. The inventory JSON enumerates every CLI command and web route, avoiding reliance on the outdated README. The MCP appendix gives all 22 individual tool results.

Common checks used where applicable: empty/missing data; malformed/nonfinite input; absent keys or failed dependencies; permission/CSRF refusal; repeat ingestion/action; optimistic version conflicts; amendment invalidation; changed/missing package bytes. Evidence is from the full suite unless marked **probe**. Service tests do not establish a browser-only end-to-end journey.

| Feature | Entry point | Expected behavior / side effects | Verified by | Status |
|---|---|---|---|---|
| Database bootstrap/demo seed | `db upgrade`, `db seed-demo-watchlist` | Fresh migrations; idempotent demo watchlist | CLI/schema and installed-wheel tests; fresh probe DBs | WORKS |
| Status/health reporting | `status`, `/ops` | DB/revision, opportunity counts, visible job results | CLI/schema, scheduler and web tests | WORKS |
| Installed distribution | wheel; `web serve` resources | Start outside checkout, templates/static/migrations/prompts available | Installed artifact test including release lifecycle | WORKS |
| User invitation/list/deactivation | `users invite/list/deactivate`, admin invite page | Validate user, hash password, revoke sessions, audit; list without hashes | CLI caller trace to users service + CLI/schema auth tests; web tests | WORKS |
| Login/logout/session/roles | `/login`, `/logout` | Session creation/revocation; expired/inactive/unauthorized refusal | Web, schema, workflow and security tests | WORKS |
| CSRF/login throttling/public-bind policy | All form POSTs; `web serve` | Reject missing/wrong tokens and disallowed exposure | Workflow/config/security tests; real rendered-token probes | WORKS |
| SAM fixture ingestion | `ingest sam --file` | Normalize records, snapshots/events/contacts, unchanged replay safe | SAM, release gate and regression tests | WORKS |
| Live SAM pull/backfill | `ingest sam`, `sam-backfill` | Page/window remote opportunities into DB/raw archive | Client fixture/window tests only; live test skipped | UNVERIFIED |
| Archive and expiry sweeps | `sam-archive-sweep`, `close-expired` | Update lifecycle locally; retain history/invalidate affected work | SAM and opportunity-lifecycle tests | WORKS |
| DIBBS local daily index | `ingest dibbs --file` | NSN/quantity/deadline normalization, unchanged replay | DIBBS parser/ingestion/timezone tests | WORKS |
| Live DIBBS discovery/date download | `ingest dibbs [--date]` | Fetch recent/specific index with request spacing | Fixture/client tests; no remote fetch | UNVERIFIED |
| USAspending fixture/delta logic | `ingest usaspending --file`; scheduled step tests | Award upsert, paging, per-code watermark/backfill, no invented unit prices | 19 USAspending tests and regressions | WORKS |
| Live USAspending pull | `ingest usaspending` date/backfill/modified options | Retrieve current remote awards | Captured/mocked responses only | UNVERIFIED |
| Watchlist add/list/edit/disable | `watchlist *`; `/watchlists` forms | Valid filters, updates/toggle, audit, enabled state | Matching and web CRUD tests; CLI caller trace | WORKS |
| Rule matching/rebuild/dedup | `match run/rebuild`; web rebuild | Explainable matches; stale reconciliation; idempotency | Matching and ingest/regression tests | WORKS |
| Offline digest/amendment re-alert/comps | `alerts digest` with no SMTP | HTML outbox; empty run none; repeat no duplicate; stored price comps | 21 alerts tests and USAspending tests | WORKS |
| SMTP transport behavior | Digest SMTP branch | TLS/refusal/retry/accounting handling | Mock SMTP alert/regression tests | WORKS |
| Actual email delivery | Configured SMTP digest | Deliver to intended mailbox exactly as configured | No SMTP service used | UNVERIFIED |
| Award histories/rankings/recompetes | `awards price-history/price-history-psc/history/top/recompete`; workspace awards | Stored NSN/PSC/agency history, whole-word filters, rankings, no invented prices | USAspending/vendor tests | WORKS |
| Cached vendor profile/refresh logic | `vendors show`, MCP `vendor_profile` | Registration plus award stats; freshness/refresh behavior | Vendor tests with mock API; protocol cached-vendor probe | WORKS |
| Live SAM entity lookup | `vendors show --refresh` or cache miss | Current authenticated SAM registration response | No live credentials/request | UNVERIFIED |
| Historical competitors and contacts | `vendors competitors`, `contacts search`; workspace competitors | Stored NSN/PSC/agency/office intelligence and buyer filters | Vendor tests and MCP probe | WORKS |
| Attachment references/safe download | `enrich download/process` | Preserve source URLs; block unsafe targets, enforce limits, retain content versions | 32 attachment-download tests plus regressions | WORKS |
| Local import/extraction/classification | `enrich ingest-file` | PDF/DOCX tables/XLSX/text; explicit origin/class; bounded extraction | Extraction/import/workflow tests; real DOCX lifecycle | WORKS |
| Prompt sync/list/render/validate/diff | `prompts sync/list/render/validate/diff` | Registry version/hash contract and packaged prompt resources | Prompt, portability, release gate tests | WORKS |
| Prompt activation/rollback/immutability | `prompts activate/rollback`, reapproval | Fail closed, audited changes and exact-input evaluation binding | Workflow/regression/prompt tests | WORKS |
| Offline prompt evaluation/compliance benchmark | `prompts eval`, `compliance benchmark` | Deterministic replay and regression refusal | Compliance and release gate tests | WORKS |
| Live behavioral prompt evaluation | `prompts eval --live` | Exact candidate evaluated by actual configured model | Mock receipts/providers only | UNVERIFIED |
| Model adapters/gateway/budget/retries | DeepSeek/OpenAI/Anthropic adapters; structured runner | Policy refusal, caps, durable reservations, typed errors, bounded retries | 19 provider tests; classification/budget regressions | WORKS |
| Live model APIs/JEV | Generative commands/remote decision provider | Current authenticated model/service behavior | Mock responses/rule fallback only | UNVERIFIED |
| Solicitation analysis | `enrich analyze/process`; Overview | Typed source-backed summary and revision metadata; honest missing-key state | Attachment analysis and intelligence tests | WORKS |
| Market intelligence | `enrich intelligence`; Market run button | Public stored award inputs; typed AI result persisted/displayed | Intelligence producer + HTTP tests with fake model | WORKS |
| Supplier/sourcing analysis | Supplier run button; `enrich intelligence` | Require recorded commercial inputs; respect proprietary policy | Intelligence tests with fake model, missing-input/policy cases | WORKS |
| Pricing analysis | Pricing run button; `enrich intelligence` | Use stored facts, typed advisory result, proprietary refusal | Intelligence tests with fake model and web policy message | WORKS |
| Rule/JEV-fallback decisions and packages | `decision run-bundle/run-package/runs`; AI Decision tab | Bundle schemas, hard-rule overrides, persisted decision history/package | Decision/signals/workflow tests | WORKS |
| Compliance inventory/extraction/reconciliation | `compliance run/inventory/matrix/coverage/findings/clauses-seed` | Source-backed matrix, retain uncertainty/conflicts and clause library | 26 compliance cases plus source/evidence regressions | WORKS |
| Compliance evidence/overrides/amendments/red team | `evidence-add/override`; source-change services | Permission/version/reason/audit; deterministic veto; amendment invalidation | Compliance/workflow/regression tests | WORKS |
| Proposal coverage and preflight/readiness | `proposal-coverage/preflight/ready` | Real version/artifact integrity, current source, blockers, authorized transition | Real release lifecycle, corruption recovery, concurrency tests | WORKS |
| Web compliance summary | Requirements/Compliance tabs | Current counts and visible preflight result | **Probe**: active mandatory row but all counters zero; F4 | PARTIAL |
| Empty compliance recovery | Compliance empty state | Executable displayed recovery command | Actual CLI invocation fails; F7 | BROKEN |
| Notifications/read action | `/notifications`, mark-read POST | Recipient-only inbox, read state and audit/CSRF | Workflow tests and route/service trace | WORKS |
| Opportunity search/detail/source links | `/search`, `/opp/{id}` | Filter/full-text search, fields and actual external source URLs, missing ID handling | Web/real-source-link/intelligence tests | WORKS |
| Inbox Seen/Dismiss/Review | `/`, `/inbox/action`, detail link | Update visible match with permissions/audit, or open detail | Web inbox tests; rendered CSRF probes | WORKS |
| Inbox Pursue | `/inbox/action` with `pursuing` | Retain candidate as actionable pursuit | **Probe**: no pursuit and absent from queues; F1 | BROKEN |
| Start Workspace → start review | Start Workspace; Overview/Review tabs | New pursuit can enter review and assign users | **Probe**: pursuit exists, no session/control; F2 | BROKEN |
| Existing-session assignment/comments/completion | Review forms; `review assign/comment/complete/context` | Assigned human contribution, identity, AI side opinion, quorum | Collaborative review and web gate tests | WORKS |
| Review policy/approval/reopen | `review approve/reopen-for-amendment`; web approval | Quorum/override/version gate, audit, amendment revoke | Collaborative/workflow/concurrency tests | WORKS |
| Normal proposal/package auto-generation | Approve to Bid | Approved bid creates immutable draft and submission record | Web approve-to-generation tests | WORKS |
| Generation failure recovery in web | Same approval request | Expose failure and let user retry | **Probe**: successful notice, missing proposal, no retry; F3 | BROKEN |
| Proposal generate/status/version editing | `proposal generate/status`; MCP new version | Immutable versions, stale approval invalidation, placeholder blockers | Proposal/workflow tests; MCP protocol version probe | WORKS |
| Proposal red team/final approve/return/cancel | `proposal red-team/approve/return/cancel`; web final approval | Human gate, readiness refusal, audit and revoke | Proposal/workflow/release tests | WORKS |
| DOCX/PDF/XLSX/ZIP exports | `proposal export` | Valid artifacts, linked requirements, included supporting files, safe spreadsheet values | Proposal/export regressions; real DOCX/XLSX lifecycle | WORKS |
| Submission package/instructions/checklist/email draft | `submission package/assemble/checklist/instructions/email-draft` | Structured destination, artifacts/integrity, manual guide and draft only | Proposal/submission/export/regression tests | WORKS |
| Submission guide in web | Submission tab | Show actual required files and destination | **Probe**: files/recipient omitted; F5 | BROKEN |
| Manual confirmation/idempotency | `submission confirm`; web confirmation without local timestamp | Require approved current package, proof, version; record once; no portal send | Release/proposal/web and regression tests | WORKS |
| Optional local submission timestamp | Web `submitted_at` input | Store correct instant and compare actual deadline | **Probe**: naive local value saved as UTC; F6 | BROKEN |
| Submitted commercial corrections | `submission correct-commercial` | Append audited correction without rewriting submitted facts | Commercial/workflow regressions and CLI-to-service trace | WORKS |
| Outcomes and structured capture/history | Web outcome POST; MCP `record_outcome` | Validate values/stage, preserve history, current outcome once | Outcome/regression tests; MCP no-bid probe | WORKS |
| Outcome analytics/learning view | `/learning`, MCP `learning_summary` | Current win/loss/no-bid data, supplier/margin/cycle statistics, small sample labels | Outcome analytics tests and MCP protocol probe | WORKS |
| Embedding generation/profiles | `embed run/watchlists` | Local 384-d vectors, source/model freshness, profile minimums | **Probe** real cached model; semantic/freshness tests | WORKS |
| Similar/recommended opportunities | `semantic similar/recommendations`; MCP similarity | Vector ranking, heuristic fallback, eligibility flag, five categories | **Probe** real MiniLM + pgvector; 24 semantic tests | WORKS |
| Pipeline workspace tabs/activity | `/pipeline`, 13 workspace tabs | Display staged pursuits and contextual data/audit | Web/intelligence tests; **probe** reveals Pursue exclusion F1 | PARTIAL |
| MCP bid analysis | MCP `get_bid_analysis` | Return recommendation/package plus stored recent runs | **Protocol probe** fails on `DecisionRun.status`; F8 | PARTIAL |
| Other MCP read/write tools | 21 other registered tools | Return compact data or authorized audited writes | Each called through in-process protocol client; fixture/mock limits apply | WORKS |
| Job list/run/chain failure and recovery | `jobs list/run`, `/ops` | Visible step results, soft/hard failure rules, cross-worker locks/recovery | Scheduler and PostgreSQL lock/regression tests | WORKS |
| Persistent scheduling configuration | `scheduler start` configuration layer | Six UTC cron chains, persisted due times and serializable callbacks | Scheduler and persistent-job tests | WORKS |
| Unattended deployment and multi-day scheduler operation | Web/MCP stdio/daemon under supervisor/proxy | Real process lifecycle/restarts, proxy/cookie and scheduled dependencies | No deployed supervisor/proxy or long-running cron observation | UNVERIFIED |
| State/local adapters and automatic portal submission | Phase 16 / reserved adapters | Explicitly deferred optional adapters; v1 human/manual submission | Implementation status and source/test intent | UNVERIFIED — outside implemented scope |

## 7. Readiness checklist

| Item | Result | Evidence / limit |
|---|---|---|
| Fresh PostgreSQL bootstrap and installed wheel | PASS | Full suite and several fresh probe databases; wheel runs outside checkout |
| Repo test gate | PASS | 1,030 passing, one explicitly skipped live test |
| Complete discover-to-review web journey | FAIL | F1/F2 reproduced from fresh entry state |
| Recovery from generation dependency failure | FAIL | F3 successful notice without draft or retry |
| Accurate submission-facing information | FAIL | F4/F5/F6/F7 |
| MCP bid analysis with real decision history | FAIL | F8; other 21 protocol tool calls succeed in synthetic scenario |
| Role, CSRF, classification, budget and version controls exercised | PASS in tested scope | Security/workflow/provider/regression tests; not exhaustive route IDOR or security review |
| Integrity, amendments, repeat actions and concurrency exercised | PASS in tested service scope | Release recovery/concurrent approval, snapshots, package hashes, outcome/submission uniqueness tests |
| Real semantic inference plus DB ranking | PASS in small local scenario | Cached offline MiniLM, four real embeddings, pgvector ranking; no production-scale recall/latency claim |
| Honest failure and actionable UI state | FAIL | F2/F3/F7 |
| Job outcomes visible and coordination tested | PASS in tested scope | Scheduler job runs, advisory locks, persistent due times, failure tests |
| Required live API, model, SMTP compatibility | NOT VERIFIED | No live credentials or remote sends/requests used |
| Typecheck and lint | NOT VERIFIED | Neither configured nor installed in the project environment |
| Production load, safe rolling migrations, backup/restore, aggregate throttling, supervisor/proxy | NOT VERIFIED | Outside executed feature audit environment |

## 8. Not verified

- Current live SAM opportunity/entity, DIBBS, USAspending, JEV, DeepSeek, OpenAI, and Anthropic service behavior. Captured payloads and mocked request contracts were exercised. No paid model call or government request was needed for safe local verification.
- Model judgment quality, live exact-candidate behavioral evaluations, and real SMTP mailbox delivery. The suite deliberately uses an offline behavioral-evaluation bootstrap setting; live activation quality is not established by it.
- A real browser's JavaScript/HTMX behavior, layouts, navigation, and cross-browser behavior. Web probes used the actual FastAPI app, parsed rendered forms, and submitted their CSRF tokens with TestClient; they do not execute JavaScript. The submission timezone finding traces the browser input contract and actual handler behavior.
- MCP stdio transport under an external client's subprocess supervision. All 22 calls ran through the in-process FastMCP protocol client; existing tests also cover registration, actor permissions and service behavior.
- Long-running scheduler execution with actual remote feeds, multiworker reverse-proxy deployment, production-sized data, load testing, rollback under rolling deploys, or backup restore. Fresh migrations and specific concurrency/recovery cases did execute.
- Stable artifact identity of the cached embedding model: local inference worked but its reported version was `all-MiniLM-L6-v2@unversioned`. The ranking probe establishes local inference functionality, not proof of model-update cache invalidation for this particular installed cache.
- Optional Phase 16 state/local adapters are explicitly deferred. Automatic external portal submission is intentionally absent in v1 and was not counted as a defect.
- Some final acceptance tests check presence/importability/schema registration rather than run an entire feature. This report uses functional module tests and additional probes for its WORKS claims; the structural acceptance checklist alone is not proof.

The complete command/route inventory and per-module passing counts are in the linked JSON. The appendix below records every registered MCP tool's executed result.

## Appendix: individual MCP protocol results

All calls used disposable PostgreSQL records. Vendor lookup hit a synthetic fresh cache; comment AI validation used a synthetic schema-valid result; no model API was called.

| Tool | Result | Status / limitation |
|---|---|---|
| `search_opportunities` | `ok: true` | WORKS: synthetic DB search/empty handling; filter edges covered by tests |
| `get_opportunity` | `ok: true` | WORKS: synthetic opportunity serialization |
| `get_opportunity_history` | `ok: true` | WORKS: history response; snapshot semantics also covered by ingestion tests |
| `price_history` | `ok: true` | WORKS: protocol empty result; nonempty price/keyword cases covered by tests |
| `vendor_profile` | `ok: true` | WORKS: cached synthetic vendor; live miss unverified |
| `competitor_summary` | `ok: true` | WORKS: protocol response; nonempty award grouping tested |
| `list_matches` | `ok: true` | WORKS: synthetic DB matches and tested filters |
| `pipeline_summary` | `ok: true` | WORKS: synthetic stage aggregation |
| `get_bid_analysis` | `ok: false`, `AttributeError` | PARTIAL: existing no-history test passes; persisted runs fail, F8 |
| `get_compliance_matrix` | `ok: true` | WORKS: actual lifecycle requirement serialization |
| `get_proposal` | `ok: true` | WORKS: actual prepared proposal/version |
| `submission_status` | `ok: true` | WORKS: actual assembled submission record |
| `learning_summary` | `ok: true` | WORKS: protocol response; populated analytics covered by tests |
| `similar_opportunities` | `ok: true` | WORKS: real locally generated vector query |
| `update_match` | `ok: true` | WORKS: Seen update; dismissal confirmation and permissions tested |
| `add_pursuit` | `ok: true` | WORKS: fresh evaluating pursuit |
| `update_pursuit` | `ok: true` | WORKS: versioned supplier/cost/quote update; locked-stage tests also pass |
| `assign_reviewer` | `ok: true` | WORKS: real assignment/notification; visible web initiation separately fails F2 |
| `add_review_comment` | `ok: true` | WORKS: assigned actor comment; AI disabled in this protocol call |
| `request_ai_comment_validation` | `ok: true` | WORKS with mocked AI result; live model unverified |
| `create_proposal_version` | `ok: true` | WORKS: real immutable new version; invalidation tested |
| `record_outcome` | `ok: true` | WORKS: real synthetic no-bid outcome; win/loss/history covered by tests |

Want me to fix the P0s in severity order?
