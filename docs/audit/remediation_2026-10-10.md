# Production audit remediation — 2026-10-10

The audit findings were fixed in severity order. Regression coverage is in
`tests/test_severity_fixes.py`, with existing workflow tests retained and updated
where their fixtures relied on the faulty behavior. Changes are local; no
production deployment or production-data migration was performed.

## Fixes

| # | Severity | Issue | Result and regression evidence |
|---|---|---|---|
| 1 | P0 | Windows worker liveness probes can interrupt a live process | `bots/store.py:owner_still_running` uses a non-signalling Windows process handle and a zero-time wait. Native Windows regression checks this process and verifies `os.kill` is never called. Stale and invalid owners are rejected before probing. |
| 2 | P1 | Anthropic/OpenAI inherit the DeepSeek model default | `ai/routing.py:_from_settings` resolves the selected provider's own default. Parameterized tests cover all three providers. |
| 3 | P1 | Old source documents and notices beyond the first 200 stop progressing | `scheduler/jobs.py:step_source_documents` scans ordered pages with a durable cursor and a 40-download limit. Tests reach an older notice after 200 retained files and advance past a full batch of failed downloads. |
| 4 | P1 | DIBBS catch-up skips older indexes and loses failed days below the watermark | `ingest/dibbs.py` caps catch-up from the oldest end and replays run history to retain gaps. Failed attempts rotate behind unattempted gaps, with room reserved for new indexes. Tests cover a 20-index backlog, a failed day below a newer success, and 14 unavailable archives alongside a new index. |
| 5 | P1 | Cancelled or expired bot claims can publish business results | `tasks/handlers/bots.py` checks the lease and completes the task in the same transaction as the results. `queue.guard_publish` uses PostgreSQL wall-clock time rather than transaction-start time. Tests cancel during execution and expire a lease during an open transaction. |
| 6 | P1 | A reclaimed task can report an unfinished bot run as success | Orchestrator inputs record the task ID and fencing token. A newer claim resumes an abandoned execution; another active execution cannot be completed by the claimant. A fresh-run takeover regression verifies work actually resumes. |
| 7 | P1 | Bot caches ignore changed watchlists, award history, and commercial inputs | Matching, awards, and bid decisions fingerprint their dependencies. Matching also fingerprints deadline/eligibility outcomes; decision keys use the effective saved provider route. Tests change criteria, add award history, change a quote, and cross a deadline threshold. Identical successful inputs still reuse the run. |
| 8 | P1 | Multipart requests, quotes, and catalogs can consume unbounded resources | Requests are limited before form parsing; quote/catalog reads are bounded. Catalogs reject files over 25 MiB or 10,000 rows before writing products. Tests prove oversized streams never reach the app and rejected catalogs perform no product writes. |
| 9 | P2 | Failed bot runs appear successful in task history and manual feedback | Failed/incomplete runs take bounded task retries, preserving their result and error. Partial bot runs can restart. Manual runs show failure feedback. Parameterized regressions reject task success for both failure statuses. |
| 10 | P2 | “Return for AI analysis” changes state without starting fresh analysis | The action atomically cancels old preparation, queues a distinct review revision, forces summary/compliance refresh, and reopens earlier reviewer completions. Repeated submissions reuse active preparation even after workspace loading restarts an assignment. Tests cover completed and unfinished reviewers, another completed reviewer, duplicate submissions, and forced analysis. |
| 11 | P2 | Consolidated review ignores changed comments and asynchronous opinions | Consolidation fingerprints comment content, citations, and AI opinions. Comment writes invalidate the summary and increment the review version; old approval forms are rejected. Worker publication invalidates without adding AI calls inside its publication transaction. Tests verify changed signatures, version changes, risks, and missing evidence. |
| 12 | P2 | Semantic search compares incompatible or stale vectors | Vector queries require matching model identity, dimension, and current text hash before limiting results. Target refresh uses the normal provenance-aware embedding path; legacy target vectors fall back to heuristic search. Tests exclude another model and changed text, while existing match-exclusion and semantic-search tests remain covered. |
| 13 | P2 | Worker resilience unit tests accidentally depend on database startup | `tests/test_r2_worker_resilience.py` isolates startup reconciliation, prompt setup, and heartbeat writes while preserving scripted database failures in the worker loop. All six tests pass in offline mode. The collaborative-review provider test also initializes its required prompt registry explicitly. |

## Operational effects

- No new schema migration is required. Document scanning uses an existing
  `app_settings` row; task claims and embedding provenance use existing columns.
- Source documents are scanned even without a recent ingest. Failed downloads
  remain eligible on later passes instead of expiring out of a time window.
- Existing vectors without provenance must be rebuilt with the embedding job
  before they participate in vector search. Their presence alone is no longer
  treated as proof of compatibility.
- AI preparation continues to honor sharing policy, provider availability, and
  budgets. A request for new analysis does not bypass those controls.

## Validation

Final checks passed:

| Check | Actual result |
|---|---|
| Complete database-backed Python suite | `1550 passed, 1 skipped, 230 warnings in 348.21s (0:05:48)` |
| New severity regression tests | `30 passed in 7.81s` |
| Offline Python suite | `658 passed, 893 skipped in 25.90s` |
| Static gate | `ruff: 0 diagnostics`, `mypy: 0 diagnostics`, `Static gate: PASS` |
| Browser progress controller | 4 passed, 0 failed |
| Wheel build | `Successfully built govcon` |
| Patch whitespace check | `git diff --check` passed |

The database-backed suite's one skipped test is
`tests/test_sam_ingestion.py::test_live_pull_yields_at_least_one_record` because
the test environment deliberately blanks `SAM_API_KEY`. Offline mode skips
database-backed tests by design. Existing test-client deprecation warnings
remain warnings; they did not cause failures.

Full outputs: [database-backed suite](remediation_2026-10-10_tests.log) and
[offline suite](remediation_2026-10-10_offline.log).

The environment uses a disposable PostgreSQL 16/pgvector container and the
test suite's temporary databases, synthetic credentials, and mocked external
providers. Live SAM, DIBBS, AI, email delivery, and external submission were not
exercised. No real payment, message, or production-data mutation was performed.
The disposable container was removed after verification; the developer's
configured database and `.env` were not changed.
