# GovCon module logic assessment — October 3, 2026

## Verdict

**Overall logical quality: 86/100.** This is the rounded, equally weighted mean of the 24 subsystem scores below, not a measured success probability or performance benchmark.
Most implemented workflows have substantial integration and regression evidence; ingestion, review, document processing, and submission are particularly strong.
Two company-registration paths can incorrectly turn absent or stale evidence into positive eligibility evidence, and missing shared prompt fragments do not stop prompt execution.
The project is **not ready for an unconditional production sign-off**: the ordinary test suite hangs in a regression test, and static checks are not green. One additional baseline failure was corrected on disk during the audit and its affected five tests now pass. No P0 was established in this assessment.

This covers the current working tree, including its substantial uncommitted work. It assesses the implemented subsystems rather than assigning scores to all 176 Python files. State/local adapters are explicitly deferred and have no implemented logic to score. This is a broad assessment with deeper traces of the highest-risk journeys, not a claim that every branch of approximately 40,000 lines was exhaustively reviewed. Live integrations and production scale remain unverified.

## Scores and method

The judgment considers intended behavior, invalid/empty inputs, state transitions, concurrency, error handling, and verification strength. Scores are approximate, with about a five-point judgment margin. A 90+ module has strong evidence and few material gaps in the examined scope; 80–89 is sound with limitations; 70–79 has a named correctness weakness. Type and lint totals are not treated as counts of runtime bugs, and style diagnostics do not directly reduce logic scores.

| Module / scope | Score | Logical assessment and evidence |
|---|---:|---|
| Foundation: models, database, configuration, audit, concurrency | 88 | Transactions, version checks, pooled connections, secret scrubbing, schema and installed-artifact checks are well established. Production migration/restore behavior is unverified. `db.py`, `concurrency.py`, `test_cli_and_schema.py`, `test_installed_artifact.py`. |
| CLI | 86 | Broad command wiring and service reuse; permissions and workflow errors are exercised. Operational commands still have a large typing backlog. `cli.py`, CLI/schema and feature regression tests. |
| Ingestion: SAM, DIBBS, USAspending, entity cache | 91 | Immutable snapshots, repeated-content handling, material-change events, pagination guards, backfill planning, retries and incremental history have good evidence. Feed behavior is exercised with fixtures and mocked HTTP, not live feeds. `test_sam_ingestion.py`, `test_dibbs.py`, `test_usaspending.py`, `test_opportunity_lifecycle.py`. |
| Matching: watchlists, rules, ranking, semantic recommendations, auto-pursue | 89 | Rules reconcile inactive matches without losing triage history; uncertain eligibility stays explicit; automated pursuits retain human approval. Semantic search uses bounded pgvector queries. Ranking has query amplification (F8); retrieval quality with a real model remains unverified. `test_matching.py`, `test_semantic_search.py`, `test_ranking_notifications.py`, `test_r2_auto_pursue_guardrail.py`. |
| Alerts | 82 | Empty runs, successful-repeat suppression, amendments and failure handling are tested. Delivery precedes the database watermark, so a crash after SMTP delivery can produce a duplicate; this is documented in `alerts/digest.py:99`. Real SMTP is unverified. |
| Enrichment: attachments, extraction, OCR, summaries, embeddings | 87 | Byte/archive/page limits, source classification, versioned attachments and coverage gaps are substantial strengths. OCR/model availability and extraction quality depend on deployment. Attachment, OCR, analysis and full-coverage tests. |
| Documents: cited chunking and batching | 92 | Preserves page/file citations and records omitted chunks when downstream budgets are exhausted. Multi-call and Unicode/byte-bound behavior have focused evidence. `documents/chunking.py`, `test_full_coverage_extraction.py`, `test_f29_f33_regressions.py`. |
| AI: providers, structured calls, budgets, replay | 88 | Policy checks precede sending; schemas reject invalid output; independent budget reservations survive rollback and retries. Replay avoids most long provider transactions, but deliberately falls back to in-transaction calls after divergence limits. Live model accuracy is unverified. Provider, budget, preparation and workflow tests. |
| Prompt management | 81 | Version/hash checks, audited activation and behavioral receipts are strong. A missing required shared fragment is silently omitted at load/render time (F4), weakening the exact-approved-content guarantee. Prompt portability/evaluation tests plus the missing-fragment probe. |
| Decision engine: JEV, rules, signals, bundles | 78 | Hard rules override provider recommendations and human review remains explicit. Stale vendor registration can bypass the freshness policy and be labeled high-confidence active (F1). Decision engine/signal tests plus the registration probe. |
| Compliance | 88 | Independent extraction, verified citations, deterministic failures, critical-item redundancy, stale-evidence checks and preflight gates are thoughtfully enforced. The registration input weakness in F2 affects its validator; real solicitation recall remains unverified. Compliance, workflow gate and release lifecycle tests. |
| Collaboration: reviewers, quorum, comments, approval, notifications | 91 | Role checks, optimistic versions, separation of duties, amendment reopening and preserved reviewer history have strong integration evidence. Collaborative review, review-cycle and reassignment tests. |
| Workflow: pursuit, preparation, invalidation, commercial facts | 85 | Shared pursuit creation, source revisions, atomic checkpoints, invalidation and commercial locks protect the main flow. Cancellation regression verification needs repair (F5); large preparation/replay workloads remain unverified. Preparation and workflow gate tests. |
| Proposals | 89 | Generation requires bid approval, writes immutable versions and performs coverage checks; approval pins the current version and rejects stale sources. Real AI drafting quality remains unverified. Proposal generation, submission and release lifecycle tests. |
| Submissions | 91 | Recomputed instructions, exact-byte manifests, file hashes, pinned versions, non-overridable integrity blockers and idempotent confirmation are strong. Submission is manual; no actual external submission was tested. Proposal/submission and release lifecycle tests. |
| Intelligence: awards, vendors, competitors, buyer contacts, AI analyses | 86 | Distinguishes unit prices from award totals, scopes supplier costs and uses bounded historical queries. Conclusions depend on feed completeness and heuristic comparability. Intelligence/vendor/award and pricing regression tests. |
| Sourcing: suppliers, catalogs, quotes, RFQ drafts, AI authorization | 87 | Local CSV/XLSX intake, finite-number validation, quote repeat handling, full-order quantity/unit checks and owner-authorized PDF analysis are tested. RFQs are drafts; actual supplier response behavior is unverified. Sourcing and FV01/FV05–FV09 tests. |
| Company registration | 72 | Refresh, provenance, expiry notifications and stale-data removal exist, but fresh unknown fields retain older assertions (F2), and decision fallback can undo stale-data removal (F1). Actual SAM refresh is unverified. Company tests plus two reproductions. |
| Learning: outcomes, award suggestions, analytics | 88 | Outcome recording follows submission gates, maintains correction history and avoids double counting; multi-award records do not automatically establish losses. Suggestions require human confirmation. Outcome, suggestion and analytics tests. |
| Durable tasks / workers | 88 | Deduplication, SKIP LOCKED claims, fencing tokens, lease checks, bounded attempts, budget parking and visible recovery actions have strong evidence. Task/worker/recovery tests; pathological preparation cancellation tests are not validated without artificial lock timeouts. |
| Scheduler / operations | 87 | Persistent schedules, leadership and chain locks, durable checkpoints, soft-step failures and analytics snapshots are exercised. Long-running daemon/failover and production pool pressure are unverified. 44 scheduler tests plus chain and worker tests. |
| Web UI | 80 | Authenticated routes, CSRF/origin controls, honest task state and shared service gates are well exercised. Watchlist validation can drop constraints or return 500 (F3). 51 web tests, feature regressions and live local HTTP probes. |
| MCP | 88 | Actor is fixed at server startup, permissions are checked and writes use service transitions. Bounded outputs and protocol operations have tests; transport/host deployment is unverified. MCP, protocol and workflow regression tests. |
| Security helpers / boundaries | 89 | Default classification denies, credential blocking, redaction, hashed sessions, CSRF and public-bind safeguards are tested. This is not a penetration-test score: adversarial DNS rebinding, infrastructure access controls and real deployment boundaries remain unverified. Security, config and workflow tests. |

Separately, **release verification is approximately 70/100** because its main gate can hang and type/lint checks are not passing or configured in CI. The additional failing assertion in the baseline now passes after a concurrent wording change. This is not included as a 25th product module in the overall mean.

## Baseline — actual results

All execution used synthetic data, disposable PostgreSQL databases, temporary storage and disabled live credentials/SMTP. No application source or existing tests were edited. Diagnostic scripts and this report were added under `docs/`.

| Check | Command / condition | Actual result |
|---|---|---|
| Ordinary full suite | `.venv/Scripts/python.exe -m pytest -q -rs -p no:cacheprovider` with JUnit requested | Did not complete: 470 pass markers and four failure markers, then blocked in `test_service_writes_are_discarded_when_work_is_invalidated[cancel-summary]`. Stack captured; only this assessment's process was stopped and its disposable database removed. No JUnit completion result exists. |
| Full suite with database lock bounds | Same suite, `PGOPTIONS='-c lock_timeout=3000 -c statement_timeout=60000'` | JUnit: `tests=1297 failures=1 errors=0 skipped=1 time=282.472`; derived **1295 passed, 1 failed, 1 skipped**; pytest exit 1. This is a diagnostic run, not an unchanged baseline. |
| Verification without database lock bounds | Same suite with `-k 'not test_service_writes_are_discarded_when_work_is_invalidated'` | JUnit: `tests=1285 failures=1 errors=0 skipped=1 time=261.929`; derived **1283 passed, 1 failed, 1 skipped**, with **12 deselected**; pytest exit 1. |
| Typecheck, proper project target | `uvx mypy --python-version 3.12 --python-executable .venv/Scripts/python.exe src/govcon` | `Found 367 errors in 62 files (checked 176 source files)`; mypy exit 1. |
| Initial typecheck without explicit target | Same mypy command without `--python-version 3.12` | `Found 421 errors in 77 files (checked 176 source files)`; exit 1. This includes tool-default Python-version diagnostics and is superseded for scoring by the proper-target result. |
| Lint | `uvx ruff check --output-format concise src tests` | `Found 897 errors.`; Ruff exit 1. Most are style/import/annotation diagnostics, not proven runtime bugs. |
| Post-change focused recheck | `pytest tests/test_analysis_tasks.py` without database lock bounds | `..... [100%]`; JUnit: 5 tests, 0 failures/errors/skips; exit 0. |
| Wheel build | `.venv/Scripts/python.exe -m pip wheel --no-deps --no-build-isolation --wheel-dir <temporary-directory> .` | `Successfully built govcon`; exit 0. |

Both broad completed runs failed `tests/test_analysis_tasks.py::test_inputs_removed_after_queueing_wait_for_the_approver`. F6 explains the escaped-HTML assertion failure. At 13:27 America/Chicago, `tasks/handlers/analysis.py` changed on disk: its guidance no longer contains the HTML-escaped apostrophe. A fresh, unbounded targeted run of `tests/test_analysis_tasks.py` then passed all five tests (exit 0). This change was not made by the assessment; no full-suite result for the post-change tree is claimed. The live SAM smoke test skips because `SAM_API_KEY is not set`. The four failure markers in the aborted process cannot be assigned reliable final identities; `web/routes/__init__.py` also changed on disk after that process started, so that incomplete result is preserved rather than presented as the final current-code baseline.

Evidence: [baseline summary](audit/module_logic_2026-10-03/baseline-summary.json), [bounded JUnit](audit/module_logic_2026-10-03/bounded.xml), [verification JUnit](audit/module_logic_2026-10-03/verified.xml), [full-run stack](audit/module_logic_2026-10-03/hung-test-stack.log), [probe results](audit/module_logic_2026-10-03/probe-results.json), [original source manifest](audit/module_logic_2026-10-03/source-manifest.json), [final source manifest](audit/module_logic_2026-10-03/source-manifest-final.json), [guidance recheck](audit/module_logic_2026-10-03/guidance-recheck.xml). Full logs are beside these files.

## Findings table

| # | Severity | Category | Location | Finding | Confidence |
|---|---|---|---|---|---|
| F1 | P1 | Correctness / eligibility | `src/govcon/decision/signals.py:96`, `:135` | Stale cached vendor status reestablishes a positive SAM signal after the company freshness policy removed it. | CONFIRMED |
| F2 | P1 | Data integrity / eligibility | `src/govcon/company/registration.py:128` | Fresh missing registration fields retain older company-file assertions and can pass validation. | CONFIRMED |
| F3 | P2 | Input validation / workflow | `src/govcon/web/routes/__init__.py:1454`, `:1494` | Invalid monetary constraints disappear, inverted ranges save, and invalid deadline values cause HTTP 500. | CONFIRMED |
| F4 | P2 | Prompt integrity / reliability | `src/govcon/prompting/registry.py:368`, `src/govcon/prompting/renderer.py:32` | Required missing shared prompt fragments are silently skipped. | CONFIRMED |
| F5 | P1 | Release verification | `tests/test_fv03_preparation_commits.py:59`, `src/govcon/workflow/preparation.py:186` | Cancellation regression test waits synchronously for a route that needs the task lock held by its own paused service. | CONFIRMED |
| F6 | P2, RESOLVED | Release verification | `tests/test_analysis_tasks.py:104` | Baseline raw-string assertion rejected escaped guidance; concurrent wording update now passes the affected five tests. | CONFIRMED |
| F7 | P2 | Release verification | `.github/workflows/pytest.yml:42`, `pyproject.toml:42` | Static checks fail and no project/CI typecheck or lint gate is defined. | CONFIRMED |
| F8 | P2 | Performance | `src/govcon/matching/ranking.py:98`, `:127` | Ranking executes per-match recommendation queries and per-opportunity competitor queries without batching. | CONFIRMED |

## Findings detail

**F1 — stale cached SAM evidence overrides the freshness decision (P1).**

Location: `decision/signals.py:96–109` and `_own_vendor` at `:135`; company freshness removal is at `company/registration.py:114`.
Trigger: a company registration refresh is older than `COMPANY_FACTS_MAX_AGE_DAYS`, while the corresponding `Vendor` cache still says Active. The overlay removes the stale registration status, deterministic validation returns unknown, and eligibility falls back to the Vendor row. `_own_vendor` does not check its age or expiration and does not refresh an existing row.
Evidence: the synthetic probe used a 30-day-old Active vendor record with an expiration 20 days in the past. The overlay removed status, yet `eligibility_signals` returned `sam_active=true`, source `sam_entity_api (own UEI)`, confidence `high`.
Impact: the decision state reports positive eligibility from evidence the freshness policy intended to treat as unknown. The human approval gates still exist; this is incorrect decision support, not proof of automatic submission.
Confidence: **CONFIRMED**, by execution and full signal trace into `build_decision_state` / hard rules.
Fix sketch: enforce the same age and deadline/expiration rules on fallback vendor evidence; refresh through `ensure_vendor` when safe, otherwise retain unknown and its provenance.

**F2 — fresh unknown registration values preserve old assertions (P1).**

Location: `company/registration.py:122–130`; downstream consumer `compliance/deterministic.py:379`.
Trigger: the registration row is fresh but status or expiration is absent, and the company facts file still contains an older Active status / future expiration. `merged.update` copies only non-null refreshed values, leaving the older values in place.
Evidence: the probe used a fresh registration with both fields null and company facts Active / 2099-12-31. The merged facts retained both old fields and `sam_registration_known` returned `pass`.
Impact: refreshed unknowns become apparently current positive evidence in eligibility/compliance instead of requiring review. This can also leave an old expiration date in place beside a refreshed status.
Confidence: **CONFIRMED**, by execution and code trace.
Fix sketch: replace or remove each SAM-authoritative field even when its refreshed value is missing; missing evidence must remain unknown, with field-level provenance.

**F3 — watchlist input handling (P2).** At `web/routes/__init__.py:1454–1500`, monetary parsing catches `ValueError` and returns None, while deadline parsing calls `int` without a validation handler. A synthetic authenticated HTTP POST with `min_value=not-a-number` returned 303 and saved no minimum; min 100 / max 10 also saved; `min_deadline_days=not-an-integer` returned 500. Users can lose a requested constraint, create a range that matches nothing, or get a generic failure. **CONFIRMED** by real local HTTP/DB probes. Fix: validate finite decimal values, integer deadline bounds and min≤max before writes; return the form with its submitted values and explicit field errors.

**F4 — missing shared prompt fragments (P2).** `_verify_includes` at `prompting/registry.py:368` continues when resolution returns None, and `render_system_prompt` omits it at `renderer.py:32`. After syncing a temporary copy of the real prompt set, the probe removed only `shared/source_security_rules_v1.md`; loading and rendering `solicitation_analysis` still succeeded, with 845 characters of approved instructions absent. An incomplete installation or deleted resource therefore changes executed system instructions without a registry refusal. **CONFIRMED** by execution; no live AI call was made and no actual information leak is claimed. Fix: require every declared include to resolve and match its approved content; raise a registry/render error before provider execution.

**F5 — self-blocking cancellation regression test (P1).**

Location: `tests/test_fv03_preparation_commits.py:59`, `workflow/preparation.py:186`, task route lock at `web/routes/__init__.py:1735`.
Trigger: the monkeypatched summary service synchronously POSTs cancellation from inside a recorded pass that already holds the opportunity/task locks.
Evidence: the captured stack shows the main thread waiting for TestClient cancellation, the route thread waiting for `lock_one(Task)`, and the heartbeat waiting on task lease renewal. The ordinary suite stopped making progress there and had to be terminated. A database-lock-timeout run completes because that artificial timeout breaks the wait; its passing cancellation assertions do not prove successful cancellation behavior.
Impact: the repo's ordinary release check can hang indefinitely. This reproduces a test-harness lock cycle; it does **not** establish that a normal browser cancellation causes a production deadlock.
Confidence: **CONFIRMED** by stack capture, database wait inspection and test/source trace.
Fix sketch: inject concurrent invalidation during the provider execution phase when business locks are released, then assert cancellation actually took effect; bound each concurrency test so failures terminate with evidence.

**F6 — escaped HTML causes a failing regression assertion (P2).** At `tests/test_analysis_tasks.py:104`, the task guidance contains `workspace's` and is correctly HTML-escaped by `_ai_macros.html`. Comparing that raw string with the response source fails; comparing decoded text succeeds. This is the one failure in both completed suite runs, although the task transitions to waiting_for_input and the visible guidance is correct. **CONFIRMED** by repeated tests, template trace and `escaped-message-probe.json`. A concurrent wording change removed the apostrophe and the affected five tests now pass. A robust future fix is still to compare parsed/decoded visible text, keeping template autoescaping enabled, so equivalent guidance punctuation does not break the gate.

**F7 — static validation is not a green release gate (P2).** CI installs dependencies and runs pytest but declares no mypy/Ruff invocation; `pyproject.toml` has no corresponding configured checks or dev dependencies. Running those checks against the current tree yields 367 proper-target type errors and 897 lint diagnostics, both exit 1. **CONFIRMED**; these are not 1,264 independently proven logic bugs. Impact: the project cannot currently use passing static validation as release evidence, and CI does not enforce it. Fix: choose and pin a typing/lint policy, triage the baseline and add enforceable CI commands; prioritize real contract/nullability errors over style cleanup.

**F8 — ranking query amplification (P2).** `rank_active_matches` visits every active Match; each `rank_match` calls `active_similarity` twice, each issuing a query. `_competition` additionally queries competitor buckets once per distinct opportunity. With 5,000 active matches, recommendation lookups alone therefore issue 10,000 SQL queries before competitor research. **CONFIRMED** by the scheduler-to-ranking call trace; actual latency was not benchmarked. Impact: the scheduled rank step has database work proportional to matches multiplied by query families and can become expensive on a large inbox. Fix: prefetch recommendation scores and batch competitor counts, then rank from the fetched maps or bounded chunks.

## System and journey map

Stack: Python 3.12+, SQLAlchemy/psycopg, PostgreSQL 16 + pgvector, Alembic, FastAPI/Jinja2/HTMX, Typer, APScheduler and a PostgreSQL-backed durable task queue. Entry points are `govcon.cli:main`, `web.create_app`, MCP server and scheduler/worker commands. PostgreSQL stores workflows, snapshots, sessions, analyses, task leases and audit history; document bytes live in local DATA_DIR, while OUTBOX_DIR holds offline email artifacts. External boundaries are SAM, DIBBS, USAspending, JEV/generative providers and SMTP. Invite-only role checks protect web/service writes; MCP binds one configured actor; CLI is an operator-facing local entry point.

History recon identified `cli.py` (18 appearances), `config.py` (9), `models.py` (8), web routes (6), AI schemas (6), prompt registry/evaluation and MCP operations among the most changed files in the last 60 commits. The current uncommitted automation work adds significant workflow/task/sourcing behavior beyond the committed history.

| Critical journey | Traced path | Break points / result |
|---|---|---|
| Invite and sign in | User invitation → Argon2 password → authentication → hashed session token → route role checks / CSRF | Auth/config/security tests pass in tested scope; proxy-level aggregate throttling and real deployment are unverified. |
| Discover and pursue a contract | Feed normalization → immutable snapshot/event → matching/reconciliation → ranking/inbox → triage → idempotent pursuit → preparation task | Ingestion/matching/triage tests pass. F3 affects watchlist constraints; F8 affects rank scaling. Automatic pursuit is serialized by the shared scheduler ingest lock. |
| Prepare, assess and approve a bid | Documents/OCR → summary → compliance → research → company/decision signals → reviewer assignments → quorum/version check → human bid approval | Most service paths are tested. **F1/F2** corrupt registration evidence; **F4** can omit prompt instructions. **F5** blocks an existing cancellation regression test. |
| Generate, approve and record submission | Bid approval + task enqueue → draft/version/coverage → assembled files/hashes → preflight → proposal approval/pinned version → manual confirmation | Real synthetic DOCX/XLSX/ZIP lifecycle tests and gate regressions pass. Actual AI writing quality and external submission are unverified. |
| Learn from outcomes and recover operations | Submitted bid → award suggestions → human outcome confirmation → current-outcome analytics; task failures → next action → retry/cancel; scheduler checkpoints | Outcome, worker and scheduler tests pass in their tested scope. F6 broke the baseline HTML assertion rather than the visible guidance; the focused post-change recheck passes. |

## Feature verification matrix

WORKS means the named behavior was exercised by the current tests within their documented synthetic/mock scope; it is not certification of live dependencies or every production edge. PARTIAL means a named failure/limit remains. The matrix groups related commands that share a service, rather than pretending each CLI alias is a separate implementation.

| Feature | Entry point | Expected behavior / side effects | Verified by | Status |
|---|---|---|---|---|
| Database upgrade, demo seed, status, installed deployment | CLI db/status; installed wheel | Migrate, idempotent seed, real DB status, resources available outside checkout | CLI/schema + installed-artifact tests; probe DB upgrade; wheel build | WORKS |
| Invite/list/deactivate users; login/logout | CLI users; /admin/users/invite; /login | Role-gated account creation, hashed credentials/sessions, revocation | Auth/schema, web and security tests | WORKS |
| SAM pull/backfill/archive sweep | CLI ingest sam*; scheduler | Store snapshots/events, process pages, archive eligible rows | SAM/lifecycle tests, mocked feed | WORKS |
| DIBBS latest/date/file ingestion | CLI ingest dibbs; scheduler | Normalize index, retain bytes, compute deadlines, repeated-file safety | DIBBS tests with local/mocked input | WORKS |
| USAspending incremental/backfill/file ingestion | CLI ingest usaspending; scheduler | Persist awards, backfill new code coverage, correct watermark | USAspending/regression tests | WORKS |
| Watchlist create/edit/toggle/rebuild | /watchlists; CLI watchlists/match | Valid filters persist; stale matches reconcile | Web/matching tests + HTTP invalid-input probes | PARTIAL — F3 |
| Deterministic matching, triage, inbox rank explanations | Inbox actions; matching engine | Rule matches, preserved triage and explicit unknown eligibility | Matching/ranking/feature regressions | PARTIAL — behavior tested; F8 scaling |
| Controlled auto-pursue | Scheduler auto_pursue | Apply eligibility/data/score/daily-cap policy; enqueue preparation | Ranking/guardrail tests + caller lock trace | WORKS within supported serialized scheduler paths |
| Semantic search and recommendation profiles | Search/MCP; embed/recommend CLI | Vector retrieval, eligibility flags, versioned embeddings | Semantic/model-revision tests with controlled providers | WORKS for tested retrieval contracts |
| Real-model semantic relevance | Real embedding provider | Useful ranking against actual opportunity corpus | No current real-model/corpus evaluation | UNVERIFIED |
| Digests, amendment alerts, offline outbox | CLI alerts digest; scheduler | Empty run silent; repeat suppression; HTML/SMTP output | Alerts tests, mocked SMTP/offline artifacts | PARTIAL — documented delivery/commit duplicate window |
| Awards/pricing/competitors/buyer contacts/vendor profile | CLI awards/vendors/contacts; /vendors; MCP | Bounded historical queries, explicit unit-price provenance, entity cache | USAspending/vendor/intelligence and pricing tests | WORKS with stored/fixture data |
| Downloads, local imports, extraction and OCR | enrich CLI; preparation | Limits and classification, versioned bytes, readable pages / visible gaps | Attachment, OCR, extraction and security tests | WORKS in tested formats/fixtures |
| Whole-document summary and cited chunking | Preparation; analysis services | Read batches, preserve citations, name unsent coverage | Analysis/full-coverage/budget tests, fake providers | WORKS for tested contracts |
| Structured AI calls, budgets, policy and retry | AI service layer | Reject invalid/policy-denied outputs; retain accounting | AI provider, security, budget and regression tests | WORKS with mocked providers |
| Prompt sync/activate/rollback/evaluate/load | CLI prompts | Audit exact approved assets and declared includes | Prompt tests + missing-fragment probe | PARTIAL — F4 |
| Company registration refresh/expiry/freshness | company refresh; /settings; scheduler | Store source/time, alert expiry, unknown stale evidence | Company tests + registration probes | PARTIAL — F1/F2 |
| Bid decision bundles and evidence | decision CLI; preparation; Review tab | Hard blockers override AI; unknowns honest; human review | Decision engine/signal tests + probes | PARTIAL — F1 |
| Compliance inventory/extraction/reconciliation | compliance CLI; Compliance tab | Source-backed matrix, redundant critical validation, coverage warnings | Compliance and full-coverage tests | PARTIAL — F2 input defect; live recall unverified |
| Clause/conflict/amendment/red-team processing | compliance services; source-change scheduler | Visible blockers and reopen stale approvals | Compliance/workflow and feature regressions | WORKS with tested synthetic sources |
| Compliance evidence, human verification, overrides | Compliance CLI/services | Permission/version checks, preserved evidence, audited overrides | Compliance/workflow gate tests | WORKS |
| Pursuit creation/preparation and commercial facts | Inbox/workspace; CLI pursuit; MCP | One pursuit, queued preparation, revision invalidation, terminal locks | Preparation, commercial and workflow tests | WORKS in tested paths |
| Reviewer assignment/comments/completion/reopen | Review tab; collaboration CLI/MCP | Preserve history and enforce substantive review/quorum | Collaboration, review-cycle and reassignment tests | WORKS |
| Bid approval/no-bid/return; deadline exception | Review approval; CLI services | Role/version/quorum gate and audited explicit exception | Collaboration/ranking/workflow tests | WORKS |
| Supplier/catalog import and quote intake | /suppliers; Products tab; sourcing CLI | Validate local rows/numbers, prevent repeats, respect quantities/units | Sourcing and FV regression tests | WORKS |
| AI supplier-quote authorization and RFQ drafts | /settings/ai-sharing; sourcing services | Owner authorization, local retention, drafts without sending | Quote-permission/sourcing/task tests | WORKS with mocked AI |
| Market/supplier/pricing AI research | Workspace analyze; CLI analysis | Check inputs, queue once, supersede changed inputs, park missing inputs | Intelligence/task tests; HTML decoded-message probe | WORKS for tested service/UI behavior; post-change recheck 5/5 |
| Proposal draft/version/red-team/exports | Proposal tab; proposal CLI/MCP | Approval required, immutable versions, coverage, real export bytes | Proposal tests + synthetic release lifecycle | WORKS for tested behavior |
| Submission instructions/checklist/email draft/ZIP | Submission tab; submission CLI | Recompute source-derived instructions, bounded package content | Submission/export/lifecycle tests | WORKS |
| Final approval/preflight/manual confirmation | Proposal/Submission tabs; approval services | Gate integrity/source/version/deadline; repeat confirmation safe | 67 workflow gate tests + release lifecycle | WORKS |
| Record/correct outcomes and analytics | Submission/Learning; outcome CLI/MCP | Submission required for wins/losses, corrections retained, no double count | Outcome/learning tests | WORKS |
| Suggest/confirm/dismiss award outcomes | Submission suggestions; scheduler | Evidence-matched suggestions, no loss from absent feed data | Outcome-suggestion/FV02 tests | WORKS |
| In-app notifications/read/acknowledge/escalations | /notifications; review scheduler | Recipient-scoped, CSRF-protected actions, visible reminders | Notification/escalation/security regressions | WORKS |
| Scheduler chain execution/recovery and analytics snapshot | scheduler CLI; /ops | Chain leadership, shared locks, checkpoints and visible failures | 44 scheduler tests + chain recovery tests | WORKS in tested scenarios |
| Task list/show/retry/cancel/progress | tasks/worker CLI; /ops; Proposal tab | Ownership/actions, retry bounds, leases and stale-result refusal | Worker/task/proposal tests | PARTIAL — normal cases tested; F5 regression harness blocks cancellation verification |
| Owner settings and sharing controls | /settings | Validated owner-only workflow/authorization settings | Settings, sourcing, ranking and preparation tests | WORKS |
| MCP reads and permitted writes | MCP server tools | Fixed actor, permission checks, service gates, bounded serialization | MCP/protocol/workflow tests | WORKS for tested operations |
| State/local adapters | Deferred Phase 16 | No implemented functionality | Status/spec recon | UNVERIFIED — deliberately deferred, not scored |

## Readiness checklist

| Area | Status | Evidence / limit |
|---|---|---|
| Main workflow state gates and stale-source invalidation | PASS in tested scope | 67 workflow-gate tests plus review/proposal/source-change regressions |
| Data classification and credential restrictions | PASS in tested scope | Security/config tests; explicit classification before provider execution |
| Role checks, sessions, CSRF and origin validation | PASS in tested scope | Web/security/workflow tests; real authenticated probe used CSRF-aware client |
| Company freshness / unknown eligibility evidence | FAIL | F1/F2 executed probes |
| Watchlist input contracts | FAIL | F3 HTTP/DB probes |
| Immutable approved prompt dependencies | FAIL | F4 missing-fragment probe |
| Basic task deduplication, fencing and retry bounds | PASS in tested scope | Task/worker/recovery regression tests |
| Cancellation regression release gate | FAIL | F5 stack capture; artificial DB timeouts do not substitute for this test |
| Complete unchanged green test suite | FAIL | Ordinary suite hung; filtered baseline had F6 failure, now resolved by focused recheck. No complete unchanged post-change run. |
| Typecheck / lint release evidence | FAIL | F7 actual nonzero check output; absent CI commands |
| Installed wheel / empty database upgrade | PASS in tested scope | Wheel build, installed-artifact test, disposable DB upgrades |
| Submission package bytes / pinned version / preflight | PASS in tested scope | Real synthetic artifact lifecycle and integrity regressions |
| External call timeouts / bounded retry code | PASS for examined paths | HTTP/provider/task traces and failure tests; live outage behavior unverified |
| Rank-step query growth | FAIL at design level | F8 confirmed per-match query calls; production latency unverified |
| Real operational visibility | PASS in tested scope | /ops task states/owners/actions, job runs, AI usage; scheduler/worker tests |
| Alerts/metrics/restore at production scale | NOT VERIFIED | No production monitoring/load/backup exercise |
| Rolling migrations and rollback with large live data | NOT VERIFIED | Fresh database upgrades only; HNSW migration uses ordinary index creation |
| Multi-host workers and attachment storage | NOT VERIFIED | DATA_DIR is local; README constrains workers to the document-holding machine |
| Infrastructure security / adversarial SSRF / transport exposure | NOT VERIFIED | No penetration test or real network deployment audit |

## Not verified and rejected candidates

- Live SAM, DIBBS, USAspending, JEV, generative AI, SMTP and external submission. Fixtures/mocks were used; no real emails, purchases, submissions or production-data changes occurred.
- Real model decision accuracy, hallucination rate, compliance extraction recall, OCR quality across actual solicitation documents, and semantic recommendation relevance. Passing contract tests cannot establish these metrics.
- Production load, pool saturation, daemon failover, reverse-proxy behavior, large-data migration locks, backup/restore, monitoring/alerts and multi-host document access.
- Complete branch coverage and every CLI/route edge across the approximately 40,000-line tree. The feature matrix reports actual test/probe scope and the named gaps rather than claiming exhaustive proof.
- The 12 synchronous preparation invalidation cases are not validated without artificial database timeouts. Their bounded-run pass result is not acceptance evidence for their intended concurrency behavior.
- A direct two-session auto-pursue probe created two pursuits with a daily cap of one. **This is not a finding:** both supported auto-pursue scheduler paths use the same `opportunity_ingest` advisory-lock group (`scheduler/chains.py:96`), so the current callers prevent that interleaving. No module score was reduced for it.
- Alert crash-after-delivery duplicates are a documented at-least-once limitation, not an unreported exactly-once guarantee.

Want me to fix the P0s in severity order?
