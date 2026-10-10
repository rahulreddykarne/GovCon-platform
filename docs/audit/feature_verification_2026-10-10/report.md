# GovCon feature and end-to-end verification — 2026-10-10

## Verdict

The configured AI services and local OCR work on synthetic inputs, but the automatic end-to-end workflow is **BROKEN**.
The live worker stopped at the summary stage because its recovery recorder cannot serialize DeepSeek's actual result type.
The isolated suite passed 1,550 tests, with one live SAM test skipped; lint, typechecking, static gates and browser-progress checks passed.
Four confirmed code defects and one local operational blocker remain. No P0 was established in this audit.
These results do not certify a production deployment or every possible model response.

## Scope, recon and method

Audited the current working tree at HEAD `8bba7f0`, including the user's pre-existing uncommitted changes. Source and test files were not changed. This directory contains audit artifacts only.

Read README, pyproject.toml, Docker Compose, GitHub CI and Windows startup scripts. Stack: Python 3.12, Typer CLI, FastAPI/Jinja/HTMX web application, FastMCP, SQLAlchemy/Alembic, PostgreSQL/pgvector, durable PostgreSQL tasks, APScheduler, local attachment files, local sentence-transformer embeddings and local Tesseract OCR. External dependencies include SAM.gov, DIBBS, USAspending, DeepSeek, Anthropic, optional OpenAI, JEV and optional SMTP. Deployment needs a migrated database, prompt registry, shared local file storage, web process, worker and scheduler.

Authentication uses invited users, Argon2 passwords, database sessions, roles and CSRF-protected mutations. Owner settings control sharing and model routes. Approval, submission and outcomes use human-authorized workflow services; MCP fixes its actor at startup. AI classification and budgets are checked before calls. No live emails, portal submissions or production-data mutations were made.

The recent history's most frequently changed files included CLI, config, models, web routing/application, alerts, scheduler, decision engine, solicitation summary, prompt registry, MCP operations, structured AI runner and attachments. These shared paths were prioritized. The five critical journeys were discovery-to-pursuit, document-to-decision preparation, sourcing-to-human bid approval, proposal-to-submission confirmation, and amendment/crash recovery-to-outcome learning.

The configured local database was unreachable. Docker was available with the pgvector PostgreSQL 16 image already installed. A disposable container on loopback port 55432 hosted separate suite and synthetic-live databases. The suite itself created and removed its own fresh database. The container was removed after evidence capture.

Live inputs were generated synthetic solicitation text and an image-only PDF. Only provider credentials were reused from local configuration; real company facts, opportunities and supplier data were not sent. The live lab activated all 23 prompts with behavioral-evaluation enforcement disabled **only in the disposable lab**. This was an explicit bootstrap setting, not verification of production behavioral activation. The lab used a 1,500,000-token opportunity budget matching local configuration and an 8,192-token output cap rather than the locally configured 65,536. It used DeepSeek for the primary pass and Anthropic for the independent extraction pass. No production settings were changed.

## Baseline

| Check | Observed result |
|---|---|
| Initial `.venv/Scripts/python.exe -m pytest -q` | Setup failed: `psycopg.errors.ConnectionTimeout: connection timeout expired`; attempts to localhost:5432 over IPv6 and IPv4 failed. This run did not validate features. |
| Full suite against disposable PostgreSQL | Exit 0. JUnit reports `tests="1551" failures="0" errors="0" skipped="1" time="317.735"`: **1,550 passed, one skipped**. |
| Skipped test | `tests.test_sam_ingestion::test_live_pull_yields_at_least_one_record`: `SAM_API_KEY is not set`. The suite intentionally blanks credentials. |
| Typecheck | `Success: no issues found in 220 source files`. Also emitted the existing annotation-unchecked note for scheduler/runner.py:100. |
| Lint: `ruff check src tests stubs` | `All checks passed!` |
| CI static gate: `python scripts/check_static.py` | `ruff: 0 diagnostics`; `mypy: 0 diagnostics`; `Static gate: PASS`. |
| JavaScript: `node --test tests/web_progress.test.cjs` | Four tests passed; zero failed or skipped. |
| Build/installed distribution | Existing `test_installed_artifact.py` passed within the full suite, including installed-wheel startup, database bootstrap and release lifecycle outside the checkout. |

The suite emitted existing Starlette cookie deprecation and Pydantic Field alias warnings; there were no failing assertions. A separate no-database run was stopped after the complete database-backed suite passed; it did not finish and is not counted as validation.

## Live feature results

| Component | Actual evidence | Result |
|---|---|---|
| DeepSeek | `deepseek-flash` returned the requested JSON; 1,235 ms in the initial probe. A subsequent real solicitation prompt produced an accepted persisted summary containing the scanned quantity. | Direct calls work; worker integration broken, F1. |
| Anthropic | `claude-sonnet-5-5` returned the requested JSON; 1,452 ms. Live compliance extraction pass B completed with nine candidates. | Live calls work on the tested fixture. |
| OpenAI | No configured credential. Adapter request/error tests passed with mocks. | Live use UNVERIFIED. |
| JEV | Initial triage answered through `jev-1.13.0`; all eight preliminary-package bundles subsequently executed through JEV without fallback. Recommendation was `review`, with `human_decision=null`. | Happy path works; malformed-value fallback partial, F4. |
| Rules decisions | All 13 decision fixtures met their expected recommendation/escalation constraints. | WORKS on the fixture set. |
| OCR | Actual Tesseract read generated image-only page 1; extraction `success`, `ocr_pages=[1]`, quantity `500 pairs` present. Real scanned/mixed-page/storage OCR tests also passed in the full suite. | WORKS on the tested fixtures. |
| Local embeddings | Loaded cached MiniLM without network access; returned 384 finite dimensions, norm 1.000000009, resolved model commit `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`. | WORKS on the local smoke input; search integration tested separately. |
| Live compliance | Direct service completed both model extraction passes, reconciliation, deterministic checks, red team and JEV routing; 14 canonical requirements. Pass A initially failed schema validation, then its existing retry succeeded. | Direct service works; worker integration blocked by F1. |
| Readiness semantics | Live compliance retained 12 unresolved mandatory requirements, four unresolved critical requirements and 29 submission blockers. No false-satisfied result was reported. | Completion of analysis correctly did not imply submission readiness. |
| Automatic preparation | Actual worker completed `documents`, then recorded `status=retrying`, `current_step=summary`, `last_error_type=TypeError`, `last_error=AI replay cannot store a DeepSeekResult`. | BROKEN. |

Direct summary/compliance/decision diagnostics deliberately bypassed worker replay to distinguish functioning services from the integration failure. They do not count as a successful automatic end-to-end journey.

## Findings table

| # | Severity | Category | Location | Finding | Confidence |
|---|---|---|---|---|---|
| F1 | P1 | Workflow/recovery | src/govcon/ai/replay.py:314,332; src/govcon/ai/providers/deepseek.py:30,136 | Successful DeepSeek calls cannot be checkpointed; preparation stops at summary. | CONFIRMED — live worker and isolated reproduction |
| F2 | P1 | Crash recovery | src/govcon/ai/replay.py:324,361; src/govcon/ai/structured.py:368; src/govcon/enrich/summarize.py:298 | Restored budget reservations lose the attributes/type required to persist results and re-checkpoint them. | CONFIRMED — executable synthetic restoration |
| F3 | P1 | Model routing/contracts | src/govcon/workflow/preparation.py:274; src/govcon/tasks/handlers/proposal.py:80; src/govcon/ai/providers/__init__.py:22 | Availability checks ignore the saved route, disabling compliance AI or selecting a placeholder proposal despite an available selected model. | CONFIRMED — executable branch probes and code trace |
| F4 | P2 | Provider error handling | src/govcon/decision/providers/jev.py:188; src/govcon/decision/engine.py:383,449 | Invalid JEV answer values bypass the typed rules-fallback path. | CONFIRMED — mocked HTTP response through the real engine |
| F5 | P1 | Local operations/environment | src/govcon/config.py:34; tests/conftest.py:75 | The configured local database was unreachable, preventing normal execution and the initial suite. | CONFIRMED — connection failure and listener/container inspection |

## Findings detail

### F1 — DeepSeek response serialization breaks preparation

**Evidence:** DeepSeekProvider.complete returns a distinct `DeepSeekResult` dataclass. Recorder snapshot serialization recognizes `CompletionResult`, not DeepSeekResult, and raises TypeError for unknown objects. `run_recorded` invokes `persist(recorder.snapshot())` after the provider call. Snapshot construction fails before the preparation persistence callback can save the paid response. The actual synthetic worker stored exactly `AI replay cannot store a DeepSeekResult` and only the documents step was checkpointed.

**Trigger:** Start automatic preparation with a readable document and the configured DeepSeek primary route. The first successful summary response reaches the recovery checkpoint. This occurs with a valid provider response, not an outage or malformed output.

**Impact:** The summary is rolled back; preparation retries instead of advancing through compliance, research, decisions and review. The successful provider result is not retained for restart. Subsequent due retries can call and charge the model again, and the task eventually fails after its bounded attempt limit. Other recorded generative steps using this same result type have the same exposure.

**Fix sketch:** Return the common CompletionResult from DeepSeek, or explicitly serialize/restore its result type. Add a worker regression using the real adapter result type, checking step advancement, saved replay and no duplicate call after restart.

### F2 — Persisted replay cannot restore budget reservations correctly

**Evidence:** `_freeze(Reservation)` retains only a marker. `_thaw` creates `_RestoredReservation`, which has only a no-op finish method. `build_analysis` reads `reservation.cost`, and `_merged_analysis` does likewise. A serialized/restored reservation passed through the real build_analysis function raised `AttributeError: '_RestoredReservation' object has no attribute 'cost'`. In addition, `_freeze` does not recognize `_RestoredReservation`, so snapshotting an already restored outcome after another call can fail as well.

**Trigger:** A worker saves a supported provider response plus its budget reservation, then crashes or loses its lease before completing the step. The replacement worker restores that checkpoint and tries to persist the analysis or checkpoint subsequent calls. Anthropic/OpenAI results are supported by the serializer, so this remains reachable independently of F1.

**Impact:** Recovery cannot finish the step even though the response was saved. A resumed summary/compliance task enters retries or failure instead of the documented no-repeat recovery behavior. Existing crash tests replay plain strings and therefore miss this production-shaped result/reservation pair.

**Fix sketch:** Preserve the necessary reservation metadata, including cost, in a serializable restored representation and support repeated snapshots. Test actual CompletionResult/reservation pairs across snapshot, restart, analysis persistence and another checkpoint, without another provider charge.

### F3 — Saved model route and availability checks disagree

**Evidence:** Structured calls use `analysis_selection(session, settings)` and honor AppSetting model routing. `provider_available(settings)` instead checks only the settings-based primary provider and takes no session. Preparation calls that check before compliance. Proposal preparation uses it before resolving the drafting call. Executable probes produced `selected_provider=anthropic, use_ai=false` and a proposal with no prepared AI call; the draft resolver was never reached.

**Trigger:** An owner saves an Anthropic analysis route with a valid Anthropic key, while the settings-based primary remains DeepSeek and its key is absent. This is a supported saved-route choice and credential state.

**Impact:** Summary calls can use the saved model, but compliance runs without AI extraction and remains incomplete. Proposal generation selects a placeholder instead of calling the available saved model. The routing UI and downstream behavior disagree, interrupting the workflow for this configuration.

**Fix sketch:** Resolve the effective route under the same session before availability checks, and use that provider consistently for preparation and proposal drafting. Regress both a saved route and the documented configured-provider fallback with a missing default key.

### F4 — Invalid JEV enum raises instead of falling back

**Trigger/evidence:** A mocked HTTP 200 from JEV supplied valid JSON, numeric confidence and `priority.choice="urgent"`, outside the allowed priority enum. The provider normalized the value without validating it. The engine's catch for DecisionProviderUnavailable surrounds the call, but bundle schema validation occurs afterward at engine.py:449. The real engine raised ValidationError and never reached rules fallback. **Impact:** A semantically invalid provider answer can abort a decision or compliance-routing step rather than preserve the rules result; it becomes a recoverable task/error flow. **Confidence:** CONFIRMED for this synthetic response; no such answer was observed in the live happy-path calls. **Fix sketch:** Validate merged candidate output inside the provider/fallback boundary, map schema failures to DecisionProviderInvalidResponse, and persist the valid baseline with a visible fallback reason.

### F5 — Configured local database is unavailable

**Evidence/trigger:** The initial suite tried localhost:5432 and failed with psycopg ConnectionTimeout. Local configuration separately resolved to a loopback database on port 5432, with no listener observed. Docker initially had no running containers. **Impact:** The normal local application cannot use its configured datastore, and the initial test run cannot set up any tests. This is an environment finding, not an application-code regression; passing against the disposable container does not establish that the normal deployment is running. **Confidence:** CONFIRMED. **Fix sketch:** Start the intended PostgreSQL instance and reconcile its port/database with configuration and the Windows launcher, then verify schema, prompt registry and live web/worker/scheduler health. The Windows launcher defaults to port 5433, so choose one consistent local setup.

## Journey map

1. **Discovery → pursuit:** SAM/DIBBS ingestion → snapshots → watchlist matching/ranking → inbox action → idempotent pursuit → preparation task. Ingest/match/UI/concurrency tests passed with fixtures and mocked sources. **Live feed unverified; normal local DB blocked by F5.**
2. **Documents → decision:** attachment reference/download or local import → stored bytes → native/OCR pages → summary → independent compliance extraction/reconciliation → research → JEV package → reviewer setup. **Live OCR passed; actual worker breaks at summary, F1.** Direct summary/compliance/package calls passed after bypassing that broken integration. Saved-route mismatch additionally affects compliance, F3.
3. **Sourcing → bid approval:** catalog/quote upload → quote validity/authorization → supplier and price facts → fresh analyses/decision → reviewer comments → review completion/quorum → versioned human approval. Existing tests covered authorized and unauthorized users, expired quotes, duplicate/repeated actions, commercial changes and stale decisions. **Saved-route cases remain partial, F3.**
4. **Proposal → submission → outcome:** human bid approval → durable generation → editable version/export → coverage/red team → assembled artifacts/preflight → final human approval → manual external submission → recorded confirmation → won/lost capture/analytics. Release tests produced actual DOCX/XLSX artifacts, passed a full lifecycle, rejected a corrupted package, recovered after repair and accepted only one concurrent approval. No external submission occurred. **Live AI proposal drafting was not performed; saved-route generation can select placeholders, F3.**
5. **Amendment/crash → renewed review:** changed source/commercial facts → invalidate stale reviews/drafts/readiness → replacement task → lease-guarded recovery → fresh analyses/approval → outcome correction/learning. Existing amendment, lease, cancellation and concurrency tests passed. **Actual AI response restoration is broken, F2; invalid JEV answers can abort routing, F4.**

## Feature verification matrix

WORKS below means the named behavior is supported by the listed run/tests, not unrestricted production certification. Tests used isolated databases, synthetic data and mocked external services unless a live run is explicitly named. PARTIAL records a concrete remaining edge case. Features sharing an entry point are grouped only where their behavior and evidence are parallel.

| Feature | Entry point | Expected behavior / side effects | Verified by | Status |
|---|---|---|---|---|
| Login/logout and session protection | /login, /logout | Authenticate, revoke sessions; reject CSRF and unauthorized mutations | test_web_ui, test_workflow_gates, test_production_readiness | WORKS — tested paths |
| Invite/deactivate/list users | Admin and `users` CLI | Role-checked user writes; sessions revoked on deactivation | test_cli_and_schema, test_web_ui, test_web_review_cleanup | WORKS — tests |
| SAM ingest/backfill/archive sweep | `ingest sam*` | Normalize/upsert notices, immutable snapshots, archive expired rows | test_sam_ingestion, test_opportunity_lifecycle | WORKS — fixture/mocked paths; live feed UNVERIFIED |
| DIBBS ingest and catch-up | `ingest dibbs`, scheduled chain | Parse indexes, retain bytes, deduplicate, retry failed days | test_dibbs, test_source_case_retry, test_severity_fixes | WORKS — tests; live DIBBS UNVERIFIED |
| USAspending ingest and award queries | `ingest usaspending`, `awards` | Store awards; query comps/history without inventing unit prices | test_usaspending, test_fv01_pricing | WORKS — tests; live feed UNVERIFIED |
| Vendor cache/competitors/buyer contacts | /vendors and vendor/contact CLI | Source-backed profiles, award comparisons and contact search | test_vendors, test_intelligence_analyses | WORKS — mocked/cache tests; live entity lookup UNVERIFIED |
| Watchlists | /watchlists, CLI | Create/edit/toggle/delete/rebuild; deactivate stale matches | test_matching, test_web_ui, test_feature_audit_fixes | WORKS — tests |
| Matching, rank and auto-pursue | Inbox, matching jobs | Explain eligibility/rank; guard automatic starts and preserve dismissals | test_matching, test_ranking_notifications, test_r2_auto_pursue_guardrail, test_severity_fixes | WORKS — tests |
| Search/local embeddings | /search, semantic CLI/jobs | Finite local vectors, model/source freshness and restricted results | Actual offline model load; test_semantic_search, test_fv11_model_revision | WORKS — local run and tests |
| Digest alerts | `alerts digest`, chains | One delivery claim per digest; outbox fallback and bounded retry | test_alerts, test_production_readiness | WORKS — test/outbox paths; real SMTP UNVERIFIED |
| Inbox actions and pipeline | /, /inbox/action, /pipeline | Triage and pursue without duplicate/lost state | test_web_ui, test_feature_audit_fixes | WORKS — tests |
| Workspace tabs and progress | /workspace, /progress | Show source-backed state; preserve edited forms and retry failed fetches | test_web_ui, test_feature_audit_fixes, four actual Node tests | WORKS — tests |
| Pursuit creation | Web, CLI, MCP | Create one pursuit/preparation task for concurrent/repeat starts | test_preparation, test_feature_audit_fixes | WORKS — tests; downstream preparation BROKEN |
| Attachment download/import/versioning | `enrich`, preparation documents | Safe URLs/classification, bounded extraction, retained source revisions | test_attachment_download/identity, test_attachments_analysis, test_source_case_retry | WORKS — tests; live source downloads UNVERIFIED |
| PDF/DOCX/XLSX/text extraction | Document import | Extract pages/tables/sheets; reject malformed/oversized containers | test_attachments_analysis, test_page_extraction_ocr, test_full_coverage_extraction | WORKS — tests |
| OCR and unreadable-page handling | Preparation/local imports | Read scans locally; preserve blocking gaps when unreadable | Actual synthetic scan; actual OCR suite tests | WORKS — local run/tests |
| Solicitation summary | Preparation, enrich CLI | Cited structured summary, coverage gaps and cache freshness | Live persisted DeepSeek summary; test_attachments_analysis, test_full_coverage_extraction | PARTIAL — direct service works, F1/F2 affect worker |
| DeepSeek/Anthropic adapters | Structured calls | Authorized, capped provider requests with typed output/errors | Live JSON checks; live summary and dual-model extraction; test_ai_providers | WORKS — tested calls; DeepSeek replay BROKEN |
| OpenAI adapter | Optional configured route | Authorized completions and bounded retries | test_ai_providers mocked requests/errors; no credential | UNVERIFIED — live service |
| Saved model route/rollback | /settings/model-routing | Owner route controls downstream provider; versioned rollback | test_model_routing; executable mismatch probes | PARTIAL — F3 |
| Prompt registry/activation/evals | `prompts` CLI; app startup | Version/hash gates, audited activation and strict loading | test_workflow_gates, test_http_prompts, test_readiness_r3, test_installed_artifact | WORKS — deterministic/mocked gates; production behavioral approval UNVERIFIED |
| AI classification/budgets | All external AI calls | Block disallowed data; reserve costs before calls and retain uncertain charges | test_security_hardening, test_workflow_gates, test_production_readiness | PARTIAL — budget enforcement tested; restored reservation F2 |
| Automatic preparation | Start pursuit, /prepare | Checkpoint documents → summary → compliance → research → decision → review | Actual live worker and test_preparation | BROKEN — F1; F2/F3 on recovery/routes |
| Compliance extraction/reconciliation | Preparation, compliance CLI | Independent extraction and cited canonical requirements; incomplete runs visible | Actual DeepSeek A/Anthropic B run; test_compliance, test_full_coverage_extraction | PARTIAL — direct service works; F1/F3 in worker |
| Clause/conflict/amendment checks | Compliance pipeline | Trace clauses, detect contradictions, reopen affected reviews | test_compliance, test_workflow_gates | WORKS — tests; live untested clause corpus |
| Evidence/matrix/human overrides | Workspace requirements/compliance, CLI | Unknown remains distinct; deterministic failure cannot be cleared by AI; audited human overrides | test_compliance; live unresolved matrix | WORKS — tests and synthetic live matrix |
| Compliance red team | Compliance pipeline | Persist source-backed risks and require human resolution | Actual live red-team run; test_compliance | WORKS — tested fixture |
| Decision package/rules/JEV | Decision CLI, preparation | Persist bundle provenance, hard rules and human authority | Eight actual JEV bundles; all 13 rules fixtures; test_decision_engine/signals | PARTIAL — valid output works; F4 |
| Market/supplier/pricing research | Workspace analyze buttons, preparation | Validate inputs, enqueue bounded jobs, report missing input/policy | test_intelligence_analyses, test_analysis_tasks | WORKS — mocked-provider tests; real research prompts UNVERIFIED |
| Supplier catalog | /suppliers, sourcing CLI | Validate/import finite rows and idempotent supplier products | test_sourcing_company, test_fv06_catalog_rows, test_severity_fixes | WORKS — tests |
| Supplier quote import/use | Products tab, quotes routes, CLI | CSV/XLSX local extraction; owner-authorized AI PDF read; reject stale quotes | test_sourcing_company, test_fv05/07/08/09, test_fv01_pricing | WORKS — tests; live quote PDF inference UNVERIFIED |
| Commercial facts and RFQ drafts | Products/Pricing, RFQ routes/CLI | Versioned finite facts, invalidate dependent review, draft only | test_sourcing_company, test_fv01_pricing, test_fv08_finite_numbers, workflow tests | WORKS — tests; no RFQ sent |
| Company strategy/registration | /settings, company CLI | Owner settings; freshness-aware registration and expiry alerts | test_company_strategy, test_sourcing_company, test_decision_signals | WORKS — mocked-source tests; live SAM registration UNVERIFIED |
| Reviewer assignment/comments/completion | Review tab | Role checks, auditable comments, one completion per assignment, fresh consolidation | test_collaborative_review, test_fv04/10, test_web_review_cleanup, test_severity_fixes | WORKS — tests; live comment AI inference UNVERIFIED |
| Quorum/escalation/human bid approval | Review approval routes | Enforce dual/conditional policy, deadlines, versions and human authority | test_collaborative_review, test_workflow_gates, release lifecycle | WORKS — tests |
| Proposal generation/retry | Approval task, proposal retry route | Draft only after current human approval; atomic version/package publish | test_proposal_generation_task, test_proposal_submission, release lifecycle | PARTIAL — saved-route placeholder F3; live drafting UNVERIFIED |
| Proposal editing/export/coverage/review | Proposal routes/CLI | Immutable versions, real DOCX export, coverage and approval invalidation | test_proposal_submission, test_compliance; actual release artifacts | WORKS — test paths; live red-team prompt UNVERIFIED |
| Package assembly/preflight/final approval | Submission tab, package actions | Real artifacts and hashes; block corruption/missing files; versioned final approval | Actual release/corruption/recovery/concurrent-approval tests | WORKS — tests |
| Submission instructions/confirmation | Submission approval route/CLI | Honest destination/deadline; require human-approved package and manual receipt | test_feature_audit_fixes, test_workflow_gates, release lifecycle | WORKS — synthetic confirmation; real external submission deliberately UNVERIFIED |
| Outcomes/corrections/award suggestions | Submission/learning, outcome CLI | Won/lost requires submission; human attribution; no missing-data loss inference | test_outcome_learning/suggestions, test_fv02_award_attribution, test_fv01_pricing | WORKS — tests |
| Analytics and eval harness | /learning, eval CLI | Correct win/margin formulas and deterministic benchmark blockers | test_outcome_learning, test_eval_harness, test_compliance | WORKS — fixture tests; real historical validation UNVERIFIED |
| Notifications/reminders | /notifications, scheduled jobs | Recipient-only records; CSRF-protected read/acknowledge; bounded email tasks | test_ranking_notifications, test_collaborative_review, test_scheduler | WORKS — tests; actual SMTP UNVERIFIED |
| Task retry/cancel/lease recovery | /ops, tasks CLI, worker | Bounded attempts, lease/cancel guards, visible failures, replay without another AI call | test_r2_worker_resilience, test_chain_tasks, test_severity_fixes; live/probe failures | PARTIAL — F1/F2 |
| Scheduler/chains/clocks | Scheduler/jobs CLI, /settings | Durable chains, overlap handling, source failure visibility and scheduled clocks | test_scheduler, test_chain_tasks, test_schedule_clocks | WORKS — tests; live unattended scheduler UNVERIFIED |
| Bots/orchestrator/approval queue | /bots, bot tasks | Idempotent stages, cancellation/lease guards, human approvals | test_bots, test_severity_fixes, test_operating_platform | PARTIAL — tested orchestration; document/preparation dependency F1 |
| Ops/mission control/trace/health | /ops, /operate, /health | Show actual tasks, source/model attempts and DB/process checks | test_ops_health, test_operating_platform, test_opportunity_trace | WORKS — TestClient/test data; normal runtime blocked by F5 |
| MCP tools | MCP server and operations | Fixed active actor, permissioned writes, structured results and workflow gates | test_mcp, test_workflow_gates; synthetic MCP acceptance workflow | WORKS — tool tests; external MCP client UI UNVERIFIED |
| Installed deployment | Wheel, `db upgrade`, web/worker startup | Package resources/migrations resolve outside checkout | test_installed_artifact and test_release_lifecycle | WORKS — isolated installed-wheel test; production rolling deploy UNVERIFIED |
| State/local feeds and arbitrary portal automation | Roadmap/deferred | Explicitly deferred features, not implemented production integrations | README/phase specs; test_phase20_final_acceptance non-goal checks | UNVERIFIED / not implemented; excluded from implemented-feature certification |

## Readiness checklist

| Area | Status | Evidence / limit |
|---|---|---|
| Build, test, lint and types | PASS | Installed-wheel tests; 1,550 passed; zero static diagnostics; four Node tests. |
| Configured local runtime | FAIL | F5: DB unreachable; this audit did not start the normal scheduler or worker against user data. |
| Automatic core journey | FAIL | F1: live scanned-document worker stops at summary. |
| Supported live AI/OCR connectivity | PASS, scoped | DeepSeek, Anthropic, JEV, Tesseract and cached embeddings ran on synthetic data. OpenAI not configured. |
| Model route consistency | FAIL | F3: compliance/proposal branches use a different availability check than actual calls. |
| Provider and task recovery | FAIL | F1/F2/F4 executable failures; valid live decision responses alone do not cover these cases. |
| Authentication/authorization/CSRF | PASS, tested paths | Role/CSRF/session/MCP actor tests and route traces; not a production penetration test. |
| Classification, secrets and upload bounds | PASS, tested paths | Gateway/secret/SSRF/upload tests, explicit synthetic classifications and bounded extraction. |
| Human approval and submission gates | PASS, tested paths | Actual isolated release lifecycle, stale/version/unauthorized tests, corrupt-package rejection. |
| Atomicity, idempotency and concurrency | PASS, tested paths | Repeat starts, quote/outcome repeats, concurrent approvals, leases, cancellation and source-change regression tests. Recovery defects remain separately failed above. |
| External timeouts and bounded retries | PASS, tested paths | Adapter/outbound-timeout/task tests; code trace. No production dependency outage/soak exercise. |
| Health/failure visibility | PASS, scoped | Health tests check DB and process state; live task retained its step and error. Alert delivery/production monitoring not verified. |
| Query scale, memory and OCR latency at production volume | NOT VERIFIED | No representative production dataset or load/soak run. |
| Live-data migration/rolling deployment/rollback | NOT VERIFIED | Fresh and installed-wheel migration tested; live production data and rolling versions unavailable. |
| Backup/restore, failover and disaster recovery | NOT VERIFIED | No backup or infrastructure exercise performed. |
| Prompt behavioral quality and production approval receipts | NOT VERIFIED | Lab bootstrap disabled behavioral enforcement; deterministic gates and current synthetic outputs do not replace a candidate's full live eval receipts. |

## Not verified

- Live SAM/DIBBS/USAspending ingestion, SAM entity/company refresh and live source attachment download. Suite sources were mocked; the live SAM test was skipped by deliberate credential isolation.
- Live OpenAI calls: no key configured. Its adapter passed mocked request/error tests.
- Real SMTP/email, SMS, payments or government-portal submission. No real side effects were triggered; portal submission is a human step, not an implemented autonomous feature.
- A complete automatic live-model journey through human review, AI proposal drafting and external submission. Preparation failed first. Direct downstream diagnostics and a passing synthetic release lifecycle are separate evidence, not a substitute.
- Live supplier-quote AI reading, market/supplier/pricing research, comment validation, proposal drafting and proposal red-team prompts. Existing schema/mocked/workflow tests passed, but these task-specific real-model outputs were not exercised.
- Production registry state, behavioral activation receipts, user/company data, deployment health, multi-host storage, browser visual interaction, real MCP client attachment, unattended schedules, backups, load and long-term model decision quality. No production database was accessed.
- Arbitrary scan quality/languages, arbitrary third-party documents, every provider/model alias and every possible response. OCR/model smoke success is limited to the stated fixtures.
- The first live-flow harness also had an audit-only reporting error (`Task.error_type` instead of `last_error_type`) after the worker had already returned `retrying`. A separate database query captured the actual task failure, and the isolated serializer probe confirmed it. This harness error is not a product finding.

## Evidence artifacts

- [pytest.log](pytest.log) and [pytest.xml](pytest.xml): full suite output and exact aggregate.
- [live_models.log](live_models.log): real provider checks and 13 rule fixtures.
- [embeddings.log](embeddings.log): actual offline cached model execution.
- [live_worker.log](live_worker.log) and [task_state.log](task_state.log): OCR run, worker failure and independent state capture.
- [live_summary_decisions.log](live_summary_decisions.log): accepted direct summary and eight actual JEV bundle runs.
- [live_compliance.log](live_compliance.log): direct dual-model extraction and complete compliance run with unresolved submission blockers.
- [edge_probes.log](edge_probes.log) and [proposal_route_probe.log](proposal_route_probe.log): confirmed serializer, restoration and route reproductions. The evidence-capture rerun of the JEV portion hit the default budget guard before the response, so that portion does not demonstrate F4.
- [jev_invalid_answer_probe.log](jev_invalid_answer_probe.log): isolated F4 reproduction through the actual engine and HTTP response parser, with mocked HTTP 200, session and budget accounting so the previously spent lab budget cannot mask the answer-handling failure. An earlier disposable-database execution also reproduced the same ValidationError.

Want me to fix the P0s in severity order?
